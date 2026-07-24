import asyncio
import hashlib
import json
import os
import re
import shutil
import threading
import time
from collections.abc import Callable
from datetime import date
from inspect import isawaitable
from pathlib import Path
from typing import Any
from urllib.parse import unquote, urlparse

import httpx
import numpy as np
from dotenv import dotenv_values
from lightrag import QueryParam
from lightrag.utils import EmbeddingFunc

from backend.config.loggers import get_logger
from backend.config.settings import settings
from backend.domain.knowledge_base.backend_policy import (
    HYBRID_RETRIEVAL_BACKEND,
    LEGACY_BACKEND,
    SELF_BACKEND,
)
from backend.rag.raganything.config import RAGAnythingConfig
from backend.rag.raganything.raganything import RAGAnything
from backend.rag.runtime import (
    DocumentStatusService,
    KbRuntimeRegistry,
    LoopLocalServicePool,
    RagRuntimeManager,
)

logger = get_logger(__name__)

_LIGHRAG_MILVUS_DELETE_PATCHED = False
_LIGHRAG_MILVUS_UPSERT_PATCHED = False
_LIGHRAG_MILVUS_INDEX_PATCHED = False
_LIGHRAG_MILVUS_QUERY_PATCHED = False
_NEBULA_STORAGE_REGISTERED = False
_LIGHRAG_SHARED_STORAGE_LOOP_PATCHED = False
_LIGHRAG_ASYNC_LIMITERS_PATCHED = False

_RAG_DOTENV_OVERRIDE_PREFIXES = (
    "RAG_",
    "EMBEDDING_",
    "VL_EMBEDDING_",
    "VLM_",
    "MILVUS_",
    "REDIS_",
    "NEO4J_",
    "GRAPH_",
    "LLM_",
    "MAX_",
    "ENTITY_",
    "RETRY_",
    "QUERY_",
)


def _rag_http_timeout_seconds() -> float:
    try:
        timeout = float(os.getenv("RAG_HTTP_TIMEOUT_SECONDS", "120"))
    except (TypeError, ValueError):
        timeout = 120.0
    return max(timeout, 1.0)


def _register_nebula_storage() -> None:
    """注册 NebulaGraphStorage 到 LightRAG 的存储实现"""
    global _NEBULA_STORAGE_REGISTERED
    if _NEBULA_STORAGE_REGISTERED:
        return

    try:
        from lightrag.kg import STORAGE_IMPLEMENTATIONS, STORAGES

        STORAGES["NebulaGraphStorage"] = "backend.rag.storages.nebula_storage"

        if "GRAPH_STORAGE" in STORAGE_IMPLEMENTATIONS:
            if (
                "NebulaGraphStorage"
                not in STORAGE_IMPLEMENTATIONS["GRAPH_STORAGE"]["implementations"]
            ):
                STORAGE_IMPLEMENTATIONS["GRAPH_STORAGE"]["implementations"].append(
                    "NebulaGraphStorage"
                )

        logger.info("NebulaGraphStorage registered to LightRAG STORAGES")
        _NEBULA_STORAGE_REGISTERED = True
    except ImportError as e:
        logger.debug(f"NebulaGraphStorage not available: {e}")
    except Exception as e:
        logger.warning(f"Failed to register NebulaGraphStorage: {e}")


def _override_rag_env_from_dotenv(dotenv_path: Path) -> None:
    try:
        values = dotenv_values(dotenv_path)
    except Exception:
        return
    if not values:
        return
    overridden: list[str] = []
    for k, v in values.items():
        if not k or v is None:
            continue
        if k in {"RAG_WORK_DIR", "RAG_WORKSPACE"} or k.startswith(
            _RAG_DOTENV_OVERRIDE_PREFIXES
        ):
            os.environ[k] = str(v)
            overridden.append(k)
    raw_workspace = values.get("RAG_WORKSPACE")
    raw_work_dir = values.get("RAG_WORK_DIR")
    if (raw_workspace is None or not str(raw_workspace).strip()) and raw_work_dir:
        try:
            os.environ["RAG_WORKSPACE"] = (
                Path(str(raw_work_dir)).expanduser().resolve().name or "default"
            )
        except Exception:
            os.environ["RAG_WORKSPACE"] = "default"
        overridden.append("RAG_WORKSPACE")
    if overridden:
        logger.info(
            f"RAG env overridden from backend/.env: {', '.join(sorted(overridden))}"
        )


def _parse_milvus_version(v: str | None) -> tuple[int, int, int] | None:
    if not v:
        return None
    s = v.strip()
    if s.startswith(("v", "V")):
        s = s[1:]
    parts = s.split(".")
    if len(parts) < 2:
        return None
    try:
        major = int(parts[0])
        minor = int(parts[1])
        patch = int(parts[2]) if len(parts) >= 3 and parts[2].isdigit() else 0
        return major, minor, patch
    except Exception:
        return None


def _patch_lightrag_shared_storage_loop_locks() -> None:
    """Patch LightRAG shared_storage locks to be event-loop local.

    LightRAG keeps process-global asyncio locks in shared_storage. When subagents run
    in dedicated threads with different event loops, reusing these locks triggers:
    "bound to a different event loop" errors. This patch keeps lock semantics while
    scoping async gate locks to current thread+loop.
    """
    global _LIGHRAG_SHARED_STORAGE_LOOP_PATCHED
    if _LIGHRAG_SHARED_STORAGE_LOOP_PATCHED:
        return

    try:
        import lightrag.kg.shared_storage as shared_storage

        loop_local_core_locks: dict[tuple[str, int, int], asyncio.Lock] = {}

        def _loop_identity() -> tuple[int, int]:
            thread_id = threading.get_ident()
            try:
                loop_id = id(asyncio.get_running_loop())
            except RuntimeError:
                loop_id = 0
            return thread_id, loop_id

        def _get_loop_local_lock(kind: str) -> asyncio.Lock:
            thread_id, loop_id = _loop_identity()
            key = (kind, thread_id, loop_id)
            lock = loop_local_core_locks.get(key)
            if lock is None:
                lock = asyncio.Lock()
                loop_local_core_locks[key] = lock
            return lock

        def _patched_get_internal_lock(enable_logging: bool = False):
            raw_lock = getattr(shared_storage, "_internal_lock", None)
            if raw_lock is None:
                raise RuntimeError(
                    "Shared data not initialized. Call initialize_share_data() before using locks!"
                )

            is_multiprocess = bool(getattr(shared_storage, "_is_multiprocess", False))
            thread_id, loop_id = _loop_identity()
            async_gate = _get_loop_local_lock("internal")
            if is_multiprocess:
                return shared_storage.UnifiedLock(
                    lock=raw_lock,
                    is_async=False,
                    name=f"internal_lock:t{thread_id}:l{loop_id}",
                    enable_logging=enable_logging,
                    async_lock=async_gate,
                )

            # In single-process mode, the original shared asyncio lock is process-global
            # and may be created on a different event loop. Always use loop-local lock.
            return shared_storage.UnifiedLock(
                lock=async_gate,
                is_async=True,
                name=f"internal_lock:t{thread_id}:l{loop_id}",
                enable_logging=enable_logging,
                async_lock=None,
            )

        def _patched_get_data_init_lock(enable_logging: bool = False):
            raw_lock = getattr(shared_storage, "_data_init_lock", None)
            if raw_lock is None:
                raise RuntimeError(
                    "Shared data not initialized. Call initialize_share_data() before using locks!"
                )

            is_multiprocess = bool(getattr(shared_storage, "_is_multiprocess", False))
            thread_id, loop_id = _loop_identity()
            async_gate = _get_loop_local_lock("data_init")
            if is_multiprocess:
                return shared_storage.UnifiedLock(
                    lock=raw_lock,
                    is_async=False,
                    name=f"data_init_lock:t{thread_id}:l{loop_id}",
                    enable_logging=enable_logging,
                    async_lock=async_gate,
                )

            # In single-process mode, avoid reusing a lock bound to another loop.
            return shared_storage.UnifiedLock(
                lock=async_gate,
                is_async=True,
                name=f"data_init_lock:t{thread_id}:l{loop_id}",
                enable_logging=enable_logging,
                async_lock=None,
            )

        def _loop_scoped_combined_key(namespace: str, key: str) -> str:
            base = shared_storage._get_combined_key(namespace, key)
            thread_id, loop_id = _loop_identity()
            return f"{base}::t{thread_id}::l{loop_id}"

        def _patched_get_lock_for_key(
            self, namespace: str, key: str, enable_logging: bool = False
        ):
            combined_key = shared_storage._get_combined_key(namespace, key)
            async_combined_key = _loop_scoped_combined_key(namespace, key)

            async_lock = self._get_or_create_async_lock(async_combined_key)
            raw_lock = shared_storage._get_or_create_shared_raw_mp_lock(namespace, key)
            is_multiprocess = raw_lock is not None
            if not is_multiprocess:
                raw_lock = async_lock

            if is_multiprocess:
                return shared_storage.UnifiedLock(
                    lock=raw_lock,
                    is_async=False,
                    name=combined_key,
                    enable_logging=enable_logging,
                    async_lock=async_lock,
                )
            return shared_storage.UnifiedLock(
                lock=raw_lock,
                is_async=True,
                name=combined_key,
                enable_logging=enable_logging,
                async_lock=None,
            )

        def _patched_release_lock_for_key(self, namespace: str, key: str):
            async_combined_key = _loop_scoped_combined_key(namespace, key)
            self._release_async_lock(async_combined_key)
            shared_storage._release_shared_raw_mp_lock(namespace, key)

        shared_storage.get_internal_lock = _patched_get_internal_lock
        shared_storage.get_data_init_lock = _patched_get_data_init_lock
        shared_storage.KeyedUnifiedLock._get_lock_for_key = _patched_get_lock_for_key
        shared_storage.KeyedUnifiedLock._release_lock_for_key = (
            _patched_release_lock_for_key
        )

        # Extra: patch redis_impl and other implementations that imported it statically
        import sys

        for mod_name, mod in list(sys.modules.items()):
            if mod_name.startswith("lightrag.kg."):
                if (
                    hasattr(mod, "get_data_init_lock")
                    and mod.get_data_init_lock is not _patched_get_data_init_lock
                ):
                    mod.get_data_init_lock = _patched_get_data_init_lock
                if (
                    hasattr(mod, "get_internal_lock")
                    and mod.get_internal_lock is not _patched_get_internal_lock
                ):
                    mod.get_internal_lock = _patched_get_internal_lock

        _LIGHRAG_SHARED_STORAGE_LOOP_PATCHED = True
        logger.info("LightRAG shared_storage loop-local lock patch applied")
    except Exception as exc:
        logger.warning("Failed to patch LightRAG shared_storage locks: %s", exc)


def _patch_lightrag_async_limiters_loop_local() -> None:
    """Patch LightRAG async limiter decorators to be loop-local.

    LightRAG's priority_limit_async_func_call builds asyncio primitives inside
    decorator closures. In multi-thread + multi-loop environments (subagent
    deep-research), wrapped functions may be invoked across loops and trigger
    lock/Future loop-binding errors. This patch provides loop-local semaphore
    limiters while preserving max concurrency semantics.
    """
    global _LIGHRAG_ASYNC_LIMITERS_PATCHED
    if _LIGHRAG_ASYNC_LIMITERS_PATCHED:
        return

    try:
        import lightrag.utils as lightrag_utils

        def _loop_identity() -> tuple[int, int]:
            thread_id = threading.get_ident()
            try:
                loop_id = id(asyncio.get_running_loop())
            except RuntimeError:
                loop_id = 0
            return thread_id, loop_id

        def _build_loop_local_limiter(max_size: int):
            # One semaphore per thread+loop, avoids cross-loop lock reuse.
            semaphores: dict[tuple[int, int], asyncio.Semaphore] = {}

            def _decorator(func):
                if not callable(func):
                    raise TypeError(f"Expected callable, got {type(func)}")

                async def _wrapped(
                    *args,
                    _priority=10,
                    _timeout=None,
                    _queue_timeout=None,
                    **kwargs,
                ):
                    key = _loop_identity()
                    sem = semaphores.get(key)
                    if sem is None:
                        sem = asyncio.Semaphore(max(1, int(max_size or 1)))
                        semaphores[key] = sem

                    async with sem:
                        # Keep compatibility with LightRAG priority wrapper contract:
                        # internal control kwargs should not be forwarded to business funcs.
                        kwargs.pop("_priority", None)
                        kwargs.pop("_timeout", None)
                        kwargs.pop("_queue_timeout", None)
                        result = func(*args, **kwargs)
                        if isawaitable(result):
                            return await result
                        return result

                return _wrapped

            return _decorator

        def _patched_limit_async_func_call(
            max_size: int, waitting_time: float = 0.0001
        ):
            _ = waitting_time
            return _build_loop_local_limiter(max_size)

        def _patched_priority_limit_async_func_call(
            max_size: int,
            llm_timeout: float = None,
            max_execution_timeout: float = None,
            max_task_duration: float = None,
            max_queue_size: int = 1000,
            cleanup_timeout: float = 2.0,
            queue_name: str = "limit_async",
        ):
            _ = llm_timeout
            _ = max_execution_timeout
            _ = max_task_duration
            _ = max_queue_size
            _ = cleanup_timeout
            _ = queue_name
            return _build_loop_local_limiter(max_size)

        lightrag_utils.limit_async_func_call = _patched_limit_async_func_call
        lightrag_utils.priority_limit_async_func_call = (
            _patched_priority_limit_async_func_call
        )

        try:
            import lightrag.lightrag as lightrag_mod

            lightrag_mod.priority_limit_async_func_call = (
                _patched_priority_limit_async_func_call
            )
            if hasattr(lightrag_mod, "limit_async_func_call"):
                lightrag_mod.limit_async_func_call = _patched_limit_async_func_call
        except ImportError:
            pass

        _LIGHRAG_ASYNC_LIMITERS_PATCHED = True
        logger.info("LightRAG async limiters loop-local patch applied")
    except Exception as exc:
        logger.warning("Failed to patch LightRAG async limiters: %s", exc)


def _get_milvus_index_config() -> tuple[str, dict]:
    """获取 Milvus 索引配置"""
    from backend.config.settings import settings

    index_type = settings.milvus_index_type.upper()

    if index_type == "HNSW":
        params = {
            "M": settings.milvus_hnsw_m,
            "efConstruction": settings.milvus_hnsw_ef_construction,
        }
    elif index_type == "DISKANN":
        params = {}
    elif index_type == "IVF_FLAT":
        params = {"nlist": settings.milvus_ivf_nlist}
    elif index_type == "IVF_PQ":
        params = {
            "nlist": settings.milvus_ivf_nlist,
            "m": settings.milvus_pq_m,
            "nbits": settings.milvus_pq_nbits,
        }
    else:
        index_type = "HNSW"
        params = {"M": 16, "efConstruction": 256}

    logger.debug(f"Milvus index config: type={index_type}, params={params}")
    return index_type, params


def _patch_lightrag_milvus_index_compat() -> None:
    global _LIGHRAG_MILVUS_INDEX_PATCHED
    if _LIGHRAG_MILVUS_INDEX_PATCHED:
        return
    try:
        from lightrag.kg.milvus_impl import MilvusVectorDBStorage
        from lightrag.utils import logger as lightrag_logger
    except Exception:
        return

    def _pick_scalar_index_type(self: Any) -> str:
        configured = (os.getenv("MILVUS_SCALAR_INDEX_TYPE") or "").strip()
        if configured:
            return configured
        try:
            v = self._client.get_server_version()
        except Exception:
            v = None
        parsed = _parse_milvus_version(str(v) if v else None)
        if parsed is None:
            return "INVERTED"
        major, minor, _patch = parsed
        if major == 2 and minor <= 3:
            return "Trie"
        return "INVERTED"

    def _get_index_params(self: Any) -> Any | None:
        try:
            if hasattr(self._client, "prepare_index_params"):
                return self._client.prepare_index_params()
        except Exception:
            pass
        try:
            from pymilvus.milvus_client.index import IndexParams

            return IndexParams()
        except Exception:
            return None

    def _create_vector_index_fallback(self: Any) -> None:
        try:
            idx = _get_index_params(self)
            if idx is None:
                return
            index_type, index_params = _get_milvus_index_config()
            idx.add_index(
                field_name="vector",
                index_type=index_type,
                metric_type="COSINE",
                params=index_params,
            )
            self._client.create_index(
                collection_name=self.final_namespace, index_params=idx
            )
        except Exception as e:
            lightrag_logger.info(
                f"[{self.workspace}] Could not create vector index using fallback method: {e}"
            )

    def _create_scalar_index_fallback(
        self: Any, field_name: str, index_type: str
    ) -> None:
        try:
            idx = _get_index_params(self)
            if idx is None:
                return
            idx.add_index(field_name=field_name, index_type=index_type)
            self._client.create_index(
                collection_name=self.final_namespace, index_params=idx
            )
        except Exception as e:
            lightrag_logger.info(
                f"[{self.workspace}] Could not create {field_name} index using fallback method: {e}"
            )

    def _create_indexes_after_collection(self: Any) -> None:
        try:
            idx = _get_index_params(self)
            index_type, index_params = _get_milvus_index_config()
            if idx is not None:
                try:
                    idx.add_index(
                        field_name="vector",
                        index_type=index_type,
                        metric_type="COSINE",
                        params=index_params,
                    )
                    self._client.create_index(
                        collection_name=self.final_namespace, index_params=idx
                    )
                except Exception:
                    _create_vector_index_fallback(self)
            else:
                _create_vector_index_fallback(self)

            scalar_type = _pick_scalar_index_type(self)
            scalar_candidates = [scalar_type]
            if scalar_type.lower() != "trie":
                scalar_candidates.append("Trie")
            if scalar_type.lower() != "inverted":
                scalar_candidates.append("INVERTED")

            def _try_scalar(field_name: str) -> None:
                last_exc: Exception | None = None
                for t in scalar_candidates:
                    try:
                        idx2 = _get_index_params(self)
                        if idx2 is None:
                            continue
                        idx2.add_index(field_name=field_name, index_type=t)
                        self._client.create_index(
                            collection_name=self.final_namespace, index_params=idx2
                        )
                        return
                    except Exception as e:
                        last_exc = e
                        continue
                if last_exc is not None:
                    _create_scalar_index_fallback(
                        self, field_name, scalar_candidates[0]
                    )

            if self.namespace.endswith("entities"):
                _try_scalar("entity_name")
            elif self.namespace.endswith("relationships"):
                _try_scalar("src_id")
                _try_scalar("tgt_id")
            elif self.namespace.endswith("chunks"):
                _try_scalar("full_doc_id")

            lightrag_logger.info(
                f"[{self.workspace}] Created indexes for collection: {self.namespace}"
            )
        except Exception as e:
            lightrag_logger.warning(
                f"[{self.workspace}] Failed to create some indexes for {self.namespace}: {e}"
            )

    MilvusVectorDBStorage._create_indexes_after_collection = (
        _create_indexes_after_collection
    )
    MilvusVectorDBStorage._create_scalar_index_fallback = _create_scalar_index_fallback
    MilvusVectorDBStorage._create_vector_index_fallback = _create_vector_index_fallback

    _LIGHRAG_MILVUS_INDEX_PATCHED = True


def _patch_lightrag_milvus_delete() -> None:
    global _LIGHRAG_MILVUS_DELETE_PATCHED
    if _LIGHRAG_MILVUS_DELETE_PATCHED:
        return
    try:
        from lightrag.kg.milvus_impl import MilvusVectorDBStorage
        from lightrag.utils import logger as lightrag_logger
    except Exception:
        return

    def _extract_delete_count(result: Any) -> int:
        if result is None:
            return 0
        if isinstance(result, dict):
            try:
                return int(result.get("delete_count") or 0)
            except Exception:
                return 0
        if isinstance(result, list):
            total = 0
            for item in result:
                total += _extract_delete_count(item)
            return total
        if hasattr(result, "delete_count"):
            try:
                return int(result.delete_count or 0)
            except Exception:
                return 0
        return 0

    async def _delete(self: Any, ids: list[str]) -> None:
        try:
            self._ensure_collection_loaded()
            result = self._client.delete(collection_name=self.final_namespace, pks=ids)
            delete_count = _extract_delete_count(result)
            if delete_count > 0:
                lightrag_logger.debug(
                    f"[{self.workspace}] Successfully deleted {delete_count} vectors from {self.namespace}"
                )
            else:
                lightrag_logger.debug(
                    f"[{self.workspace}] No vectors were deleted from {self.namespace}"
                )
        except Exception as e:
            lightrag_logger.error(
                f"[{self.workspace}] Error while deleting vectors from {self.namespace}: {e}"
            )

    async def _delete_entity(self: Any, entity_name: str) -> None:
        try:
            from lightrag.utils import compute_mdhash_id

            entity_id = compute_mdhash_id(entity_name, prefix="ent-")
            lightrag_logger.debug(
                f"[{self.workspace}] Attempting to delete entity {entity_name} with ID {entity_id}"
            )
            result = self._client.delete(
                collection_name=self.final_namespace, pks=[entity_id]
            )
            delete_count = _extract_delete_count(result)
            if delete_count > 0:
                lightrag_logger.debug(
                    f"[{self.workspace}] Successfully deleted entity {entity_name}"
                )
            else:
                lightrag_logger.debug(
                    f"[{self.workspace}] Entity {entity_name} not found in storage"
                )
        except Exception as e:
            lightrag_logger.error(
                f"[{self.workspace}] Error deleting entity {entity_name}: {e}"
            )

    async def _delete_entity_relation(self: Any, entity_name: str) -> None:
        try:
            self._ensure_collection_loaded()
            expr = f'src_id == "{entity_name}" or tgt_id == "{entity_name}"'
            results = self._client.query(
                collection_name=self.final_namespace, filter=expr, output_fields=["id"]
            )
            if not results:
                lightrag_logger.debug(
                    f"[{self.workspace}] No relations found for entity {entity_name}"
                )
                return
            relation_ids = []
            for item in results:
                if isinstance(item, dict) and item.get("id"):
                    relation_ids.append(item["id"])
            if not relation_ids:
                return
            delete_result = self._client.delete(
                collection_name=self.final_namespace, pks=relation_ids
            )
            delete_count = _extract_delete_count(delete_result)
            lightrag_logger.debug(
                f"[{self.workspace}] Deleted {delete_count} relations for {entity_name}"
            )
        except Exception as e:
            lightrag_logger.error(
                f"[{self.workspace}] Error deleting relations for {entity_name}: {e}"
            )

    MilvusVectorDBStorage.delete = _delete
    MilvusVectorDBStorage.delete_entity = _delete_entity
    MilvusVectorDBStorage.delete_entity_relation = _delete_entity_relation
    _LIGHRAG_MILVUS_DELETE_PATCHED = True


def _patch_lightrag_milvus_upsert_limits() -> None:
    global _LIGHRAG_MILVUS_UPSERT_PATCHED
    if _LIGHRAG_MILVUS_UPSERT_PATCHED:
        return
    try:
        from lightrag.kg.milvus_impl import MilvusVectorDBStorage
    except Exception:
        return

    original_upsert = getattr(MilvusVectorDBStorage, "upsert", None)
    if original_upsert is None:
        return

    max_total_chars = int(os.getenv("MILVUS_DYNAMIC_FIELD_TOTAL_MAX_CHARS", "60000"))
    max_content_chars = int(
        os.getenv("MILVUS_DYNAMIC_FIELD_CONTENT_MAX_CHARS", "16000")
    )
    max_value_chars = int(os.getenv("MILVUS_DYNAMIC_FIELD_VALUE_MAX_CHARS", "4000"))
    max_source_id_items = int(os.getenv("MILVUS_SOURCE_ID_MAX_ITEMS", "50"))

    def _str_len(v: Any) -> int:
        try:
            return len(str(v))
        except Exception:
            return 0

    def _shrink_value(key: str, v: Any, remaining: int) -> Any:
        if remaining <= 0:
            return ""
        hard = max_value_chars
        if key == "content":
            hard = max_content_chars
        if v is None:
            return v
        if isinstance(v, str):
            limit = min(len(v), hard, remaining)
            return v[:limit]
        if isinstance(v, (int, float, bool)):
            return v
        if isinstance(v, (list, tuple)):
            if key in {"source_id", "source_ids"}:
                trimmed = list(v)[:max_source_id_items]
                if _str_len(trimmed) > min(hard, remaining):
                    s = ",".join([str(x) for x in trimmed])
                    limit = min(len(s), hard, remaining)
                    return s[:limit]
                return trimmed
            s = str(v)
            limit = min(len(s), hard, remaining)
            return s[:limit]
        if isinstance(v, dict):
            s = str(v)
            limit = min(len(s), hard, remaining)
            return s[:limit]
        s = str(v)
        limit = min(len(s), hard, remaining)
        return s[:limit]

    def _sanitize_meta(meta: dict[str, Any]) -> dict[str, Any]:
        remaining = max_total_chars
        out: dict[str, Any] = {}
        keys = list(meta.keys())
        for k in keys:
            if k in {"content", "source_id", "source_ids", "description"}:
                continue
            v2 = _shrink_value(k, meta.get(k), remaining)
            out[k] = v2
            remaining -= _str_len(v2)
            if remaining <= 0:
                return out

        for k in ("source_id", "source_ids"):
            if k in meta and remaining > 0:
                v2 = _shrink_value(k, meta.get(k), remaining)
                out[k] = v2
                remaining -= _str_len(v2)

        if "description" in meta and remaining > 0:
            v2 = _shrink_value("description", meta.get("description"), remaining)
            out["description"] = v2
            remaining -= _str_len(v2)

        if "content" in meta and remaining > 0:
            v2 = _shrink_value("content", meta.get("content"), remaining)
            out["content"] = v2
        return out

    async def _upsert(self: Any, data: dict[str, dict[str, Any]]) -> Any:
        if not data:
            return None
        sanitized: dict[str, dict[str, Any]] = {}
        for k, meta in data.items():
            if not isinstance(meta, dict):
                continue
            sanitized[k] = _sanitize_meta(meta)
        return await original_upsert(self, sanitized)

    MilvusVectorDBStorage.upsert = _upsert
    _LIGHRAG_MILVUS_UPSERT_PATCHED = True


def _patch_lightrag_milvus_query_for_diskann() -> None:
    global _LIGHRAG_MILVUS_QUERY_PATCHED
    if _LIGHRAG_MILVUS_QUERY_PATCHED:
        return
    try:
        from lightrag.kg.milvus_impl import MilvusVectorDBStorage
    except Exception:
        return

    original_query = getattr(MilvusVectorDBStorage, "query", None)
    if original_query is None:
        return

    async def _query(
        self: Any, query: str, top_k: int, query_embedding: list[float] = None
    ) -> list[dict[str, Any]]:
        self._ensure_collection_loaded()

        if query_embedding is not None:
            embedding = [query_embedding]
        else:
            embedding = await self.embedding_func([query], _priority=5)

        output_fields = list(self.meta_fields)

        index_type = settings.milvus_index_type.upper()
        if index_type == "DISKANN":
            search_params = {
                "metric_type": "COSINE",
                "params": {},
            }
        elif index_type == "HNSW":
            search_params = {
                "metric_type": "COSINE",
                "params": {"ef": 64},
            }
        elif index_type in ("IVF_FLAT", "IVF_PQ", "IVF_SQ8"):
            search_params = {
                "metric_type": "COSINE",
                "params": {"nprobe": 16},
            }
        else:
            search_params = {
                "metric_type": "COSINE",
                "params": {},
            }

        results = self._client.search(
            collection_name=self.final_namespace,
            data=embedding,
            limit=top_k,
            output_fields=output_fields,
            search_params=search_params,
        )

        filtered_results = []
        for dp in results[0]:
            distance = dp.get("distance", 1.0)
            if distance >= self.cosine_better_than_threshold:
                filtered_results.append(
                    {
                        **dp["entity"],
                        "id": dp["id"],
                        "distance": distance,
                        "created_at": dp.get("created_at"),
                    }
                )

        return filtered_results

    MilvusVectorDBStorage.query = _query
    _LIGHRAG_MILVUS_QUERY_PATCHED = True


def _patch_lightrag_redis_connection_pool_loop_local() -> None:
    """
    Patch `RedisConnectionManager` in `lightrag.kg.redis_impl` to keep ConnectionPool loop-local.
    Otherwise, redis connections shared across asyncio loops cause cross-loop 'Future' attachment errors.
    """
    try:
        import asyncio
        import threading

        from lightrag.kg.redis_impl import RedisConnectionManager
        from redis.asyncio import ConnectionPool

        # Keep original class method behaviour if already patched to avoid recursive issues
        if getattr(RedisConnectionManager, "_patched_for_loop_local", False):
            return

        class LoopLocalRedisConnectionManager:
            _patched_for_loop_local = True

            # (redis_url, loop_id) -> pool
            _pools = {}
            _pool_refs = {}
            _lock = threading.Lock()

            @classmethod
            def _get_key(cls, redis_url: str) -> tuple:
                try:
                    loop = asyncio.get_running_loop()
                    loop_id = id(loop)
                except RuntimeError:
                    loop_id = 0
                return (redis_url, loop_id)

            @classmethod
            def get_pool(cls, redis_url: str):
                key = cls._get_key(redis_url)
                with cls._lock:
                    if key not in cls._pools:
                        cls._pools[key] = ConnectionPool.from_url(
                            redis_url,
                            decode_responses=True,
                            max_connections=int(
                                os.getenv("REDIS_MAX_CONNECTIONS", "200")
                            ),
                            socket_timeout=float(
                                os.getenv("REDIS_SOCKET_TIMEOUT", "30.0")
                            ),
                            socket_connect_timeout=float(
                                os.getenv("REDIS_CONNECT_TIMEOUT", "10.0")
                            ),
                        )
                        cls._pool_refs[key] = 0
                    cls._pool_refs[key] += 1
                return cls._pools[key]

            @classmethod
            def release_pool(cls, redis_url: str):
                key = cls._get_key(redis_url)
                with cls._lock:
                    if key in cls._pool_refs:
                        cls._pool_refs[key] -= 1
                        if cls._pool_refs[key] <= 0:
                            pool = cls._pools.pop(key, None)
                            cls._pool_refs.pop(key, None)
                            if pool:
                                try:
                                    disconnect_result = pool.disconnect()
                                    if isawaitable(disconnect_result):
                                        try:
                                            loop = asyncio.get_running_loop()
                                        except RuntimeError:
                                            asyncio.run(disconnect_result)
                                        else:
                                            task = loop.create_task(
                                                disconnect_result,
                                                name="lightrag-redis-pool-disconnect",
                                            )
                                            task.add_done_callback(
                                                lambda finished: finished.exception()
                                                if not finished.cancelled()
                                                else None
                                            )
                                except Exception as exc:
                                    logger.warning(
                                        "LightRAG Redis 连接池释放失败: %s",
                                        exc,
                                    )

        import lightrag.kg.redis_impl as redis_impl

        redis_impl.RedisConnectionManager = LoopLocalRedisConnectionManager
        import logging

        logger = logging.getLogger(__name__)
        logger.info("LightRAG redis connection pool loop-local patch applied")
    except ImportError:
        pass


class RagService:
    def __init__(self):
        self._runtime = RagRuntimeManager(
            logger=logger,
            env_override_callback=_override_rag_env_from_dotenv,
        )
        self._kb_registry = KbRuntimeRegistry(logger=logger)
        self._doc_status_service = DocumentStatusService(logger=logger)

        self._initialized = False
        self._initialization_lock = asyncio.Lock()
        self._initialization_task: asyncio.Task[None] | None = None
        self._http: httpx.AsyncClient | None = None
        self._rag: RAGAnything | None = None
        self._rag_work_dir: str | None = None
        self._runtime_settings: Any | None = None
        self._linearrag_http_ready = False
        self._query_llm_model_func: Callable[..., Any] | None = None
        self._query_llm_max_async: int = 50

        self._kb_manager: Any | None = None
        self._default_kb_id: str | None = None
        self._default_kb_code: str = "default"
        self._session_factory: Any | None = None

    @property
    def is_ready(self) -> bool:
        """Return whether the initialized RAG runtime can still serve requests."""
        return bool(
            self._initialized
            and self._rag is not None
            and self._http is not None
            and not self._http.is_closed
        )

    def _sync_runtime_fields(self) -> None:
        self._http = self._runtime.http
        self._runtime_settings = self._runtime.runtime_settings
        self._rag_work_dir = self._runtime.rag_work_dir

    def _sync_kb_fields(self) -> None:
        self._kb_manager = self._kb_registry.kb_manager
        self._session_factory = self._kb_registry.session_factory
        self._default_kb_id = self._kb_registry.default_kb_id
        self._default_kb_code = self._kb_registry.default_kb_code

    async def _ensure_http_client(self) -> None:
        self._http = await self._runtime.ensure_http_client()
        self._sync_runtime_fields()

    async def _initialize_kb_manager(self) -> None:
        """初始化知识库管理器"""
        await self._kb_registry.initialize()
        self._sync_kb_fields()

    def _get_session(self):
        """获取数据库会话"""
        return self._kb_registry.get_session()

    async def _ensure_default_kb(self) -> str:
        """确保默认知识库存在"""
        kb_id = await self._kb_registry.ensure_default_kb()
        self._sync_kb_fields()
        return kb_id

    async def _get_rag_instance(self, kb_id: str | None = None) -> RAGAnything:
        """
        获取 RAG 实例

        Args:
            kb_id: 知识库 ID，为空则使用默认知识库

        Returns:
            RAGAnything 实例
        """
        rag = await self._kb_registry.get_rag_instance(
            kb_id,
            initialize_default=self.initialize,
            fallback_rag_provider=lambda: self._rag,
        )
        self._sync_kb_fields()
        return rag

    async def _resolve_query_backend(self, kb_id: str | None) -> str:
        """Resolve retrieval backend and fail closed to legacy until self retriever is ready."""
        backend = await self._kb_registry.resolve_query_backend(kb_id)
        self._sync_kb_fields()
        return backend

    async def _retrieve_self_context(
        self,
        query: str,
        kb_id: str | None,
        *,
        chunk_top_k: int = 10,
    ) -> tuple[str, list[dict[str, Any]]]:
        result = await self._kb_registry.retrieve_self_context(
            query,
            kb_id,
            chunk_top_k=chunk_top_k,
        )
        self._sync_kb_fields()
        return result

    async def _aquery_self(
        self,
        query: str,
        kb_id: str | None,
        **kwargs: Any,
    ) -> str:
        chunk_top_k = int(kwargs.get("chunk_top_k") or kwargs.get("top_k") or 10)
        context, _chunks = await self._retrieve_self_context(
            query,
            kb_id,
            chunk_top_k=chunk_top_k,
        )
        if kwargs.get("only_need_prompt"):
            return context
        if not context.strip():
            return "未在自研知识库索引中检索到相关内容。"

        await self._ensure_http_client()
        runtime = (
            self._runtime_settings if self._runtime_settings is not None else settings
        )
        llm_config = runtime.active_llm_config
        api_base = (llm_config.get("base_url") or "").rstrip("/")
        api_key = llm_config.get("api_key")
        model = llm_config.get("model")
        if not api_base or not model:
            return context

        from backend.core.models.context_limits import resolve_max_tokens

        max_tokens = int(
            kwargs.get("max_tokens")
            or llm_config.get("max_tokens")
            or resolve_max_tokens(llm_config, api_model=model)
        )
        messages = [
            {
                "role": "system",
                "content": "你是知识库问答助手。请只依据给定上下文回答；如果上下文不足，说明未检索到足够依据。",
            },
            {
                "role": "user",
                "content": f"问题：{query}\n\n上下文：\n{context}",
            },
        ]
        response = await self._http.post(
            f"{api_base}/chat/completions",
            headers={
                "Authorization": f"Bearer {api_key}" if api_key else "",
                "Content-Type": "application/json",
            },
            json={
                "model": model,
                "messages": messages,
                "temperature": float(kwargs.get("temperature", runtime.temperature)),
                "max_tokens": max_tokens,
                "stream": False,
            },
            timeout=_rag_http_timeout_seconds(),
        )
        response.raise_for_status()
        data = response.json()
        return (
            data.get("choices", [{}])[0].get("message", {}).get("content", "").strip()
            or context
        )

    async def _aquery_hybrid(
        self,
        query: str,
        *,
        mode: str = "mix",
        kb_id: str | None = None,
        **kwargs: Any,
    ) -> str:
        """True hybrid retrieval: run legacy + self in parallel via HybridRetriever."""
        from backend.rag.retriever_v2 import HybridRetriever

        chunk_top_k = int(kwargs.get("chunk_top_k") or kwargs.get("top_k") or 10)

        async def _legacy_retrieve(q: str, top_k: int):
            try:
                rag = await self._get_rag_instance(kb_id)
                query_param_kwargs = {
                    k: v
                    for k, v in kwargs.items()
                    if k in QueryParam.__dataclass_fields__
                }
                context = await rag.aquery(
                    q, mode=mode, only_need_context=True, **query_param_kwargs
                )
                return [
                    {
                        "content": context,
                        "source_backend": LEGACY_BACKEND,
                        "_chunk_score": 1.0,
                    }
                ]
            except Exception as exc:
                logger.warning("[hybrid] legacy retrieval failed: %s", exc)
                return []

        async def _self_retrieve(q: str, top_k: int):
            try:
                _ctx, chunks = await self._retrieve_self_context(
                    q, kb_id, chunk_top_k=top_k
                )
                return chunks
            except Exception as exc:
                logger.warning("[hybrid] self retrieval failed: %s", exc)
                return []

        hybrid = HybridRetriever(
            legacy_retriever=_legacy_retrieve,
            self_retriever=_self_retrieve,
        )
        merged = await hybrid.retrieve(query, top_k=chunk_top_k)

        context_parts = []
        for item in merged:
            content = item.get("content", "")
            if content.strip():
                backend = item.get("source_backend", "unknown")
                context_parts.append(f"[{backend}] {content}")
        context = "\n\n---\n\n".join(context_parts)

        if kwargs.get("only_need_prompt"):
            return context
        if not context.strip():
            return "未在知识库中检索到相关内容。"

        await self._ensure_http_client()
        runtime = (
            self._runtime_settings if self._runtime_settings is not None else settings
        )
        llm_config = runtime.active_llm_config
        api_base = (llm_config.get("base_url") or "").rstrip("/")
        api_key = llm_config.get("api_key")
        model = llm_config.get("model")
        if not api_base or not model:
            return context

        from backend.core.models.context_limits import resolve_max_tokens

        max_tokens = int(
            kwargs.get("max_tokens")
            or llm_config.get("max_tokens")
            or resolve_max_tokens(llm_config, api_model=model)
        )
        messages = [
            {
                "role": "system",
                "content": "你是知识库问答助手。请只依据给定上下文回答；如果上下文不足，说明未检索到足够依据。",
            },
            {"role": "user", "content": f"问题：{query}\n\n上下文：\n{context}"},
        ]
        response = await self._http.post(
            f"{api_base}/chat/completions",
            headers={
                "Authorization": f"Bearer {api_key}" if api_key else "",
                "Content-Type": "application/json",
            },
            json={
                "model": model,
                "messages": messages,
                "temperature": float(kwargs.get("temperature", runtime.temperature)),
                "max_tokens": max_tokens,
                "stream": False,
            },
            timeout=_rag_http_timeout_seconds(),
        )
        response.raise_for_status()
        data = response.json()
        return (
            data.get("choices", [{}])[0].get("message", {}).get("content", "").strip()
            or context
        )

    async def initialize(self) -> None:
        """Initialize this loop-local service exactly once.

        Startup prewarming and readiness initialization intentionally run in
        parallel.  They must share one initialization task instead of creating
        and overwriting independent LightRAG/HTTP client instances.
        """
        async with self._initialization_lock:
            if self._initialized:
                return
            task = self._initialization_task
            if task is None:
                task = asyncio.create_task(
                    self._initialize_once(),
                    name="rag-service-initialize",
                )
                self._initialization_task = task

        try:
            # A timed-out startup waiter must not cancel initialization for
            # other callers; the same task can be awaited again.
            await asyncio.shield(task)
        except asyncio.CancelledError:
            raise
        except Exception:
            async with self._initialization_lock:
                if self._initialization_task is task:
                    # Keep the lock until partial resources are closed.  If we
                    # clear the task first, a retry can start and then have its
                    # fresh client closed by this failed attempt's cleanup.
                    await self._close_resources(cancel_initialization=False)
            raise
        else:
            async with self._initialization_lock:
                if self._initialization_task is task:
                    self._initialization_task = None

    async def _initialize_once(self) -> None:
        if self._initialized:
            return

        _patch_lightrag_async_limiters_loop_local()
        _patch_lightrag_redis_connection_pool_loop_local()
        _patch_lightrag_shared_storage_loop_locks()

        self._runtime.load_backend_env()

        from backend.config.settings import Settings as SettingsCls

        self._runtime_settings = SettingsCls()
        self._http = httpx.AsyncClient(
            trust_env=False,
            timeout=_rag_http_timeout_seconds(),
        )
        self._runtime.set_runtime_settings(self._runtime_settings)
        self._runtime.set_http_client(self._http)

        runtime = self._runtime_settings
        rag_work_dir = (
            runtime.rag_work_dir
            or os.getenv("RAG_WORK_DIR")
            or os.getenv("WORKING_DIR")
            or "./rag_storage"
        )
        self._rag_work_dir = rag_work_dir
        self._runtime.set_rag_work_dir(rag_work_dir)
        rag_workspace = (os.getenv("RAG_WORKSPACE") or "").strip()
        if not rag_workspace:
            try:
                rag_workspace = (
                    Path(rag_work_dir).expanduser().resolve().name or "default"
                )
            except Exception:
                rag_workspace = "default"
        embedding_config = runtime.embedding_config
        embedding_api_base = embedding_config.get("api_base") or os.getenv(
            "EMBEDDING_API_BASE"
        )
        embedding_api_key = embedding_config.get("api_key") or os.getenv(
            "EMBEDDING_API_KEY"
        )
        embedding_model = embedding_config.get("model") or os.getenv("EMBEDDING_MODEL")

        lightrag_kwargs: dict[str, Any] = {}

        if settings.redis_host:
            redis_password = settings.redis_password or ""
            auth = f":{redis_password}@" if redis_password else ""
            redis_uri = f"redis://{auth}{settings.redis_host}:{settings.redis_port or 6379}/{settings.redis_db or 0}"
            os.environ["REDIS_URI"] = redis_uri
            lightrag_kwargs.update(
                {
                    "kv_storage": "RedisKVStorage",
                    "doc_status_storage": "RedisDocStatusStorage",
                }
            )
        lightrag_kwargs.setdefault("workspace", rag_workspace)
        if settings.milvus_connection_uri:
            os.environ["MILVUS_URI"] = settings.milvus_connection_uri
            os.environ["MILVUS_DB_NAME"] = settings.milvus_connection_db_name or ""
            if settings.milvus_user:
                os.environ["MILVUS_USER"] = settings.milvus_user
            if settings.milvus_password:
                os.environ["MILVUS_PASSWORD"] = settings.milvus_password
            _patch_lightrag_milvus_delete()
            _patch_lightrag_milvus_upsert_limits()
            _patch_lightrag_milvus_index_compat()
            _patch_lightrag_milvus_query_for_diskann()
            lightrag_kwargs.update({"vector_storage": "MilvusVectorDBStorage"})

        # 注册 NebulaGraphStorage 到 LightRAG 的 STORAGES 字典
        _register_nebula_storage()

        if os.getenv("NEBULA_HOST"):
            lightrag_kwargs.update({"graph_storage": "NebulaGraphStorage"})
        elif os.getenv("NEO4J_URI"):
            lightrag_kwargs.update({"graph_storage": "Neo4JStorage"})
        concurrency_config = runtime.rag_ingestion_config.get("concurrency", {})
        chunking_config = runtime.rag_ingestion_config.get("chunking", {})
        lightrag_kwargs.setdefault(
            "chunk_token_size",
            chunking_config.get("rag_chunk_token_size")
            or int(os.getenv("RAG_CHUNK_TOKEN_SIZE", "600")),
        )
        lightrag_kwargs.setdefault(
            "chunk_overlap_token_size",
            chunking_config.get("rag_chunk_overlap_token_size")
            or int(os.getenv("RAG_CHUNK_OVERLAP_TOKEN_SIZE", "120")),
        )
        lightrag_kwargs.setdefault(
            "entity_extract_max_gleaning",
            chunking_config.get("entity_extract_max_gleaning")
            or int(os.getenv("ENTITY_EXTRACTION_MAX_GLEANING", "1")),
        )
        lightrag_kwargs.setdefault(
            "max_parallel_insert",
            concurrency_config.get("max_parallel_insert")
            or int(os.getenv("MAX_PARALLEL_INSERT", "80")),
        )

        embedding_dim = None
        embedding_max_token_size = embedding_config.get("max_token_size") or int(
            os.getenv("EMBEDDING_MAX_TOKEN_SIZE", "8192")
        )
        if embedding_api_base and embedding_model:
            try:
                embedding_dim = await self._infer_embedding_dim(
                    api_base=embedding_api_base,
                    api_key=embedding_api_key,
                    model=embedding_model,
                )
                embedding_func = EmbeddingFunc(
                    embedding_dim=embedding_dim,
                    max_token_size=embedding_max_token_size,
                    func=self._build_embedding_func(
                        api_base=embedding_api_base,
                        api_key=embedding_api_key,
                        model=embedding_model,
                    ),
                )
            except Exception as e:
                embedding_func = None
                logger.warning(f"推断嵌入维度失败，将尝试降级：{type(e).__name__}: {e}")
        else:
            embedding_func = None

        if embedding_func is None:
            allow_mock = (os.getenv("RAG_ALLOW_MOCK_EMBEDDINGS") or "").lower() in {
                "1",
                "true",
                "yes",
            }
            configured_dim = embedding_config.get("dim") or os.getenv("EMBEDDING_DIM")
            if configured_dim and (
                isinstance(configured_dim, int)
                or (
                    isinstance(configured_dim, str) and configured_dim.strip().isdigit()
                )
            ):
                embedding_dim = int(configured_dim)
            if allow_mock:
                embedding_dim = embedding_dim or 384
                embedding_func = EmbeddingFunc(
                    embedding_dim=embedding_dim,
                    max_token_size=embedding_max_token_size,
                    func=self._build_mock_embedding_func(embedding_dim),
                )
                lightrag_kwargs.setdefault(
                    "cosine_threshold",
                    float(os.getenv("RAG_MOCK_COSINE_THRESHOLD", "-1.0")),
                )
            else:
                raise RuntimeError(
                    "嵌入服务不可用，且未启用 RAG_ALLOW_MOCK_EMBEDDINGS；"
                    "请配置可用的 EMBEDDING_API_BASE/EMBEDDING_MODEL，或开启 mock 嵌入用于本地联调"
                )

        # 知识库入库使用独立的 LLM 配置 (MiniMax)
        llm_model_func = self._build_llm_model_func(for_ingestion=True)
        vision_model_func = self._build_vlm_model_func()
        rerank_model_func = self._build_rerank_model_func()

        multimodal_config = runtime.rag_ingestion_config.get("multimodal", {})
        chunking_config = runtime.rag_ingestion_config.get("chunking", {})

        config = RAGAnythingConfig(
            working_dir=rag_work_dir,
            enable_image_processing=multimodal_config.get(
                "enable_image_processing", True
            ),
            enable_table_processing=multimodal_config.get(
                "enable_table_processing", True
            ),
            enable_equation_processing=multimodal_config.get(
                "enable_equation_processing", False
            ),
            max_concurrent_files=concurrency_config.get("max_concurrent_files", 5),
            max_concurrent_entity_batches=concurrency_config.get(
                "max_concurrent_entity_batches", 5
            ),
            max_concurrent_entity_nodes=concurrency_config.get(
                "max_concurrent_entity_nodes", 5
            ),
            max_concurrent_text_processing=concurrency_config.get(
                "max_concurrent_text_processing", 20
            ),
            max_concurrent_vision_processing=concurrency_config.get(
                "max_concurrent_vision_processing", 40
            ),
            entity_extraction_batch_size=concurrency_config.get(
                "entity_extraction_batch_size", 5
            ),
            llm_model_max_async=concurrency_config.get("llm_model_max_async", 15),
            use_semantic_chunking=chunking_config.get("use_semantic_chunking", True),
            semantic_chunking_threshold=chunking_config.get(
                "semantic_chunking_threshold", 95
            ),
            embedding_batch_size=chunking_config.get("embedding_batch_size", 200),
        )

        # 将 rerank_model_func 添加到 lightrag_kwargs 中
        if rerank_model_func:
            lightrag_kwargs["rerank_model_func"] = rerank_model_func

        self._rag = RAGAnything(
            config=config,
            llm_model_func=llm_model_func,
            vision_model_func=vision_model_func,
            embedding_func=embedding_func,
            lightrag_kwargs=lightrag_kwargs,
        )

        init_result = await self._rag._ensure_lightrag_initialized()
        if not init_result.get("success"):
            raise RuntimeError(init_result.get("error") or "LightRAG 初始化失败")

        # 创建查询专用的 LLM 函数（独立并发池）
        self._query_llm_max_async = config.query_llm_model_max_async
        query_llm_func = self._build_query_llm_model_func()
        if query_llm_func is not None:
            from functools import partial

            from lightrag.utils import priority_limit_async_func_call

            self._query_llm_model_func = priority_limit_async_func_call(
                self._query_llm_max_async,
                llm_timeout=300,
                queue_name="Query LLM",
            )(partial(query_llm_func))
        else:
            self._query_llm_model_func = None

        self._initialized = True
        logger.info(
            f"RAG service initialized (working_dir={rag_work_dir}, workspace={rag_workspace}, embedding_dim={embedding_dim}, query_max_async={self._query_llm_max_async})"
        )

        if self._rag and self._rag.lightrag and self._rag.lightrag.embedding_func:
            try:
                logger.info("[预热] 开始预热 Embedding workers...")
                import asyncio

                warmup_task = self._rag.lightrag.embedding_func(["__warmup__"])
                await asyncio.wait_for(warmup_task, timeout=30.0)
                logger.info("[预热] Embedding workers 预热完成")
            except TimeoutError:
                logger.warning("[预热] Embedding workers 预热超时")
            except Exception as e:
                logger.warning(f"[预热] Embedding workers 预热失败: {e}")

    async def aquery(
        self, query: str, mode: str = "mix", kb_id: str | None = None, **kwargs: Any
    ) -> str:
        """
        执行查询

        Args:
            query: 查询文本
            mode: 查询模式
            kb_id: 知识库 ID（可选，默认使用默认知识库）
        """
        query_backend = await self._resolve_query_backend(kb_id)
        if query_backend == SELF_BACKEND:
            return await self._aquery_self(query, kb_id, **kwargs)
        if query_backend == HYBRID_RETRIEVAL_BACKEND:
            return await self._aquery_hybrid(query, mode=mode, kb_id=kb_id, **kwargs)
        rag = await self._get_rag_instance(kb_id)

        query_param_kwargs = {
            k: v for k, v in kwargs.items() if k in QueryParam.__dataclass_fields__
        }

        return await rag.aquery(query, mode=mode, **query_param_kwargs)

    async def _get_images_for_query(
        self,
        query: str,
        mode: str = "mix",
        rag: RAGAnything | None = None,
        context: str | None = None,
    ) -> list[dict[str, str]]:
        """
        获取查询相关的图片信息

        Args:
            query: 查询文本
            mode: 查询模式
            rag: RAG 实例
            context: 查询上下文（用于提取图片信息）
        """
        if rag is None:
            await self.initialize()
            rag = self._rag
        if rag is None:
            raise RuntimeError("RAG service failed to initialize")

        from backend.application.image_pipeline import normalize_image_url
        from backend.rag.media_blocks import iter_image_blocks

        images: list[dict[str, str]] = []
        seen_paths = set()

        images_raw = getattr(rag, "_last_query_images", []) or []
        for item in images_raw:
            if not isinstance(item, dict):
                continue
            marker = str(item.get("marker") or "").strip()
            path = self._clean_image_path_value(str(item.get("path") or ""))
            if not marker or not path:
                continue
            path = normalize_image_url(path)
            if path in seen_paths:
                continue
            seen_paths.add(path)
            images.append({"marker": marker, "url": path, "path": path})

        if context:
            for block in iter_image_blocks(context):
                p = str(block.get("image_path") or "").strip()
                if not p:
                    continue
                p = normalize_image_url(p)
                if p in seen_paths:
                    continue
                seen_paths.add(p)

                marker = f"IMAGE_{len(images) + 1}"
                images.append(
                    {
                        "marker": marker,
                        "url": p,
                        "path": p,
                        "caption": str(block.get("caption") or "").strip(),
                        "footnote": str(block.get("footnote") or "").strip(),
                        "visual_analysis": str(
                            block.get("visual_analysis") or ""
                        ).strip(),
                    }
                )

        return images

    async def aquery_rich(
        self, query: str, mode: str = "mix", kb_id: str | None = None, **kwargs: Any
    ) -> dict[str, Any]:
        """
        执行查询并返回丰富的结果，包含原始回答和相关图片信息。
        图片标注和格式化由调用方（Agent）负责处理。

        Args:
            query: 查询文本
            mode: 查询模式
            kb_id: 知识库 ID（可选）
        """
        query_backend = await self._resolve_query_backend(kb_id)
        if query_backend in {SELF_BACKEND, HYBRID_RETRIEVAL_BACKEND}:
            try:
                answer = await self._aquery_self(query, kb_id, **kwargs)
                _context, chunks = await self._retrieve_self_context(
                    query,
                    kb_id,
                    chunk_top_k=int(
                        kwargs.get("chunk_top_k") or kwargs.get("top_k") or 10
                    ),
                )
                return {"answer": answer, "images": [], "chunks": chunks}
            except Exception as exc:
                if query_backend == SELF_BACKEND:
                    raise
                logger.warning("[查询路由] hybrid self 查询失败，回退 legacy: %s", exc)
        rag = await self._get_rag_instance(kb_id)

        query_param_kwargs = {
            k: v for k, v in kwargs.items() if k in QueryParam.__dataclass_fields__
        }

        answer = await rag.aquery(query, mode=mode, **query_param_kwargs)

        images = await self._get_images_for_query(query, mode=mode, rag=rag)

        return {"answer": answer, "images": images}

    async def aquery_context_only(
        self, query: str, mode: str = "mix", kb_id: str | None = None, **kwargs: Any
    ) -> str:
        """
        仅检索上下文，不生成答案（用于 Agent 检索阶段）
        使用独立的查询并发池，不受入库影响

        Args:
            query: 查询文本
            mode: 查询模式
            kb_id: 知识库 ID（可选）
        """
        query_backend = await self._resolve_query_backend(kb_id)
        if query_backend == SELF_BACKEND:
            context, _chunks = await self._retrieve_self_context(
                query,
                kb_id,
                chunk_top_k=int(kwargs.get("chunk_top_k") or kwargs.get("top_k") or 10),
            )
            return context
        if query_backend == HYBRID_RETRIEVAL_BACKEND:
            context, _chunks = await self._retrieve_self_context(
                query,
                kb_id,
                chunk_top_k=int(kwargs.get("chunk_top_k") or kwargs.get("top_k") or 10),
            )
            if context.strip():
                return context
        rag = await self._get_rag_instance(kb_id)

        kwargs.setdefault("only_need_prompt", True)
        query_param_kwargs = {
            k: v for k, v in kwargs.items() if k in QueryParam.__dataclass_fields__
        }
        query_param = QueryParam(mode=mode, **query_param_kwargs)

        if self._query_llm_model_func is not None:
            query_param.model_func = self._query_llm_model_func

        return await rag.lightrag.aquery(query, param=query_param)

    async def astream_query(
        self,
        query: str,
        mode: str = "mix",
        system_prompt: str | None = None,
        **kwargs: Any,
    ):
        """
        流式查询接口 - 真正实现流式输出

        工作流程：
        1. 使用 LightRAG 检索相关上下文（非流式）
        2. 使用 LLM 的流式接口生成回答（真正的流式）

        Args:
            query: 查询文本
            mode: 查询模式
            system_prompt: 系统提示词
            **kwargs: 其他参数

        Yields:
            str: 文本片段
        """
        await self.initialize()
        if self._rag is None:
            raise RuntimeError("RAG service failed to initialize")
        if self._http is None:
            raise RuntimeError("HTTP client not initialized")

        runtime = (
            self._runtime_settings if self._runtime_settings is not None else settings
        )
        llm_config = runtime.active_llm_config
        api_base = (llm_config.get("base_url") or "").rstrip("/")
        api_key = llm_config.get("api_key")
        model = llm_config.get("model")

        try:
            # 步骤1：使用 LightRAG 检索上下文（非流式）
            # 使用 naive 模式获取原始上下文，而不是直接生成答案
            from lightrag import QueryParam

            base_kwargs = {
                k: v
                for k, v in kwargs.items()
                if k
                not in [
                    "stream",
                    "vlm_enhanced",
                    "only_need_prompt",
                    "top_k",
                    "chunk_top_k",
                ]
            }
            QueryParam(
                mode=mode,
                stream=False,
                only_need_prompt=True,
                **base_kwargs,
            )

            prompt = await self.aquery_context_only(
                query, mode=mode, only_need_prompt=True, **base_kwargs
            )

            messages: list[dict[str, Any]] = []
            if system_prompt:
                messages.append({"role": "system", "content": system_prompt})
            messages.append({"role": "user", "content": prompt})

            # 发送流式请求

            # 确保 API Base URL 格式正确 (移除 /v1 后缀，因为下面会拼接)
            # MiniMax API URL 格式: https://api.minimax.chat/v1/text/chatcompletion_v2
            # 这里的实现是基于 OpenAI 兼容接口，通常是 /v1/chat/completions

            from backend.core.models.context_limits import resolve_max_tokens

            max_tokens = int(
                kwargs.get(
                    "max_tokens",
                    llm_config.get("max_tokens")
                    or resolve_max_tokens(llm_config, api_model=model),
                )
            )

            # 构造请求数据
            request_json = {
                "model": model,
                "messages": messages,
                "temperature": float(kwargs.get("temperature", runtime.temperature)),
                "max_tokens": max_tokens,
                "stream": True,  # 启用流式输出
            }

            # 记录详细的调试日志
            logger.info(f"[LLM调用] URL: {api_base}/chat/completions")
            logger.info(f"[LLM调用] Model: {model}")
            logger.info(f"[LLM调用] MaxTokens: {max_tokens}")
            logger.info(f"[LLM调用] Messages Count: {len(messages)}")

            # 计算总字符长度
            total_chars = 0
            for msg in messages:
                content_len = len(msg.get("content", "") or "")
                total_chars += content_len
                role = msg.get("role", "unknown")
                # 记录每条消息的简要信息
                logger.info(f"[LLM调用] Msg[{role}] Length: {content_len}")

            logger.info(f"[LLM调用] Total Content Chars: {total_chars}")

            # 记录请求头信息（隐藏敏感 Key）
            masked_key = (
                f"{api_key[:4]}...{api_key[-4:]}"
                if api_key and len(api_key) > 8
                else "***"
            )
            logger.info(f"[LLM调用] Headers Auth: Bearer {masked_key}")

            async with self._http.stream(
                "POST",
                f"{api_base}/chat/completions",
                json=request_json,
                headers={
                    "Authorization": f"Bearer {api_key}",
                    "Content-Type": "application/json",
                },
                timeout=_rag_http_timeout_seconds(),
            ) as response:
                response.raise_for_status()

                # 处理 SSE 流
                async for line in response.aiter_lines():
                    line = line.strip()
                    if not line or line.startswith(":"):
                        continue

                    # 处理 SSE 格式：data: {...}
                    if line.startswith("data: "):
                        data_str = line[6:]  # 去掉 "data: " 前缀

                        # 检查是否是结束标记
                        if data_str == "[DONE]":
                            break

                        try:
                            data = json.loads(data_str)
                            # 提取 delta 中的 content
                            delta = data.get("choices", [{}])[0].get("delta", {})
                            content = delta.get("content", "")
                            if content:
                                yield content
                        except json.JSONDecodeError:
                            continue
                        except Exception:
                            continue

        except Exception as e:
            logger.error(f"流式查询失败: {e}")
            yield f"\n\n查询失败: {str(e)}"

    def _clean_image_path_value(self, image_path: str) -> str:
        s = (image_path or "").strip()
        if not s:
            return ""
        for sep in ("\\n", "\n", "\r"):
            if sep in s:
                s = s.split(sep, 1)[0].strip()
        for token in ("Captions:", "Footnotes:", "Visual Analysis:"):
            idx = s.find(token)
            if idx != -1:
                s = s[:idx].strip()
        s = s.strip(" \t\"'")
        return s

    async def aquery_with_multimodal(
        self,
        query: str,
        multimodal_content: list[dict[str, Any]],
        mode: str = "mix",
        **kwargs: Any,
    ) -> str:
        await self.initialize()
        if self._rag is None:
            raise RuntimeError("RAG service failed to initialize")
        return await self._rag.aquery_with_multimodal(
            query=query, multimodal_content=multimodal_content, mode=mode, **kwargs
        )

    async def insert_content_list(
        self,
        content_list: list[dict[str, Any]],
        file_path: str = "unknown_document",
        kb_id: str | None = None,
        **kwargs: Any,
    ) -> dict[str, Any]:
        """
        插入内容列表到指定知识库

        Args:
            content_list: 内容列表
            file_path: 文件路径
            kb_id: 知识库 ID（可选）
        """
        rag = await self._get_rag_instance(kb_id)
        result = await rag.insert_content_list(
            content_list=content_list, file_path=file_path, **kwargs
        )

        if result.get("success") and result.get("doc_id"):
            (
                final_chunk_count,
                final_entity_count,
                final_relation_count,
            ) = await self._finalize_document_after_ingest(
                rag=rag,
                kb_id=kb_id,
                doc_id=result["doc_id"],
                file_path=file_path,
                document_date=kwargs.get("document_date"),
                chunk_count=int(result.get("chunk_count") or 0),
            )
            result["chunk_count"] = final_chunk_count
            result["entity_count"] = final_entity_count
            result["relation_count"] = final_relation_count

        return result

    async def _record_document(
        self,
        kb_id: str | None,
        doc_id: str,
        file_path: str,
        file_name: str | None = None,
        document_date: date | None = None,
        category_path: str | None = None,
        chunk_count: int = 0,
        entity_count: int = 0,
        relation_count: int = 0,
    ) -> None:
        """记录文档到知识库"""
        if not kb_id:
            return
        try:
            if self._kb_manager is None:
                await self._initialize_kb_manager()
            if self._session_factory is None:
                return
            async with self._get_session() as session:
                clean_name, file_path_clean = self._clean_file_path(file_path)
                # 优先使用传入的 file_name
                final_file_name = file_name or clean_name

                await self._kb_manager.add_document(
                    session=session,
                    kb_id=kb_id,
                    doc_id=doc_id,
                    file_name=final_file_name,
                    file_path=file_path_clean,
                    document_date=document_date,
                    category_path=category_path,
                    chunk_count=chunk_count,
                    entity_count=entity_count,
                    relation_count=relation_count,
                )

                await self._kb_manager.update_knowledge_base_stats(session, kb_id)
        except Exception as e:
            logger.warning(f"记录文档失败: {e}")

    async def _wait_doc_processed_status(
        self,
        rag: Any,
        doc_id: str,
        poll_interval: float = 1.0,
        timeout_seconds: float | None = None,
    ) -> dict[str, Any] | None:
        """等待文档进入 processed/failed 状态，避免异常任务永久占用请求。"""
        lightrag = getattr(rag, "lightrag", rag)
        if not hasattr(lightrag, "doc_status"):
            return None

        if timeout_seconds is None:
            try:
                timeout_seconds = float(
                    os.getenv("RAG_DOC_PROCESS_TIMEOUT_SECONDS", "900") or "900"
                )
            except (TypeError, ValueError):
                timeout_seconds = 900.0
        deadline = asyncio.get_running_loop().time() + max(1.0, timeout_seconds)
        while True:
            status_data = await lightrag.doc_status.get_by_id(doc_id)
            status_value = ""
            if isinstance(status_data, dict):
                status_value = str(status_data.get("status") or "").lower()
            else:
                status_value = str(getattr(status_data, "status", "") or "").lower()

            if status_value in {"processed", "failed"}:
                return status_data

            remaining = deadline - asyncio.get_running_loop().time()
            if remaining <= 0:
                raise TimeoutError(
                    f"等待文档处理超时: doc_id={doc_id}, timeout={timeout_seconds:.1f}s"
                )
            await asyncio.sleep(min(max(0.05, poll_interval), remaining))

    async def _collect_doc_graph_counts(
        self,
        rag: Any,
        doc_id: str,
        fallback_chunk_count: int = 0,
    ) -> tuple[int, int, int]:
        """读取文档的分块、实体、关系统计。"""
        lightrag = getattr(rag, "lightrag", rag)
        chunk_count = fallback_chunk_count
        entity_count = 0
        relation_count = 0

        try:
            if hasattr(lightrag, "doc_status"):
                status_data = await lightrag.doc_status.get_by_id(doc_id)
                if isinstance(status_data, dict):
                    chunk_count = int(
                        status_data.get("chunks_count") or chunk_count or 0
                    )
                else:
                    chunk_count = int(
                        getattr(status_data, "chunks_count", chunk_count) or 0
                    )

            if hasattr(lightrag, "full_entities"):
                entity_data = await lightrag.full_entities.get_by_id(doc_id)
                if isinstance(entity_data, dict):
                    entity_count = int(entity_data.get("count") or 0)

            if hasattr(lightrag, "full_relations"):
                relation_data = await lightrag.full_relations.get_by_id(doc_id)
                if isinstance(relation_data, dict):
                    relation_count = int(relation_data.get("count") or 0)
        except Exception as e:
            logger.warning(f"读取文档统计失败 doc={doc_id}: {e}")

        return chunk_count, entity_count, relation_count

    async def _finalize_document_after_ingest(
        self,
        rag: Any,
        kb_id: str | None,
        doc_id: str,
        file_path: str,
        file_name: str | None = None,
        document_date: date | None = None,
        category_path: str | None = None,
        chunk_count: int = 0,
    ) -> tuple[int, int, int]:
        """先更新 chunk，再等待处理完成后回填实体/关系并触发索引同步。"""
        target_kb = kb_id or self._default_kb_id

        await self._record_document(
            target_kb,
            doc_id,
            file_path,
            file_name=file_name,
            document_date=document_date,
            category_path=category_path,
            chunk_count=chunk_count,
            entity_count=0,
            relation_count=0,
        )

        status_data = await self._wait_doc_processed_status(rag, doc_id)
        status_value = ""
        if isinstance(status_data, dict):
            status_value = str(status_data.get("status") or "").lower()
        else:
            status_value = str(getattr(status_data, "status", "") or "").lower()

        (
            final_chunk_count,
            entity_count,
            relation_count,
        ) = await self._collect_doc_graph_counts(
            rag,
            doc_id,
            fallback_chunk_count=chunk_count,
        )

        # 只有在传入了这些值时才记录，避免被覆盖为 None
        record_kwargs = {
            "file_name": file_name,
            "chunk_count": final_chunk_count,
            "entity_count": entity_count,
            "relation_count": relation_count,
        }
        if document_date is not None:
            record_kwargs["document_date"] = document_date
        if category_path is not None:
            record_kwargs["category_path"] = category_path

        await self._record_document(target_kb, doc_id, file_path, **record_kwargs)

        if status_value == "processed":
            await self._trigger_index_sync(target_kb, doc_id)

        return final_chunk_count, entity_count, relation_count

    def _clean_file_path(self, file_path: str) -> tuple:
        """
        清理文件路径，返回 (file_name, clean_file_path)

        1. 移除 URL 查询参数
        2. file_name 只保留路径最后一段
        """

        clean_path = file_path

        if "?" in clean_path:
            clean_path = clean_path.split("?")[0]

        parsed = urlparse(clean_path)
        raw_name = parsed.path if parsed.scheme and parsed.netloc else clean_path
        path_obj = Path(raw_name)
        file_name = path_obj.name
        for _ in range(2):
            decoded_name = unquote(file_name)
            if decoded_name == file_name:
                break
            file_name = decoded_name

        return file_name, clean_path

    async def _trigger_index_sync(self, kb_id: str | None, doc_id: str) -> None:
        """触发索引同步"""
        if not kb_id or not doc_id:
            return

        try:
            from backend.rag.knowledge_base import get_kb_manager
            from backend.rag.sync import get_rag_index_sync_manager

            kb_manager = get_kb_manager()

            async def get_kb_info():
                if self._session_factory is None:
                    return None
                async with self._get_session() as session:
                    return await kb_manager.get_knowledge_base(session, kb_id)

            kb = await get_kb_info()

            if kb:
                sync_manager = get_rag_index_sync_manager()
                await sync_manager.initialize()
                await sync_manager.sync_after_insert(kb.kb_code, kb_id, [doc_id])
                logger.info(f"[RAGService] 触发索引同步: kb={kb.kb_code}, doc={doc_id}")

        except Exception as e:
            logger.warning(f"[RAGService] 触发索引同步失败: {e}")

    async def insert_docx(
        self, file_path: str, kb_id: str | None = None, **kwargs: Any
    ) -> dict[str, Any]:
        """
        插入 DOCX 文档到指定知识库

        Args:
            file_path: DOCX 文件路径
            kb_id: 知识库 ID（可选）
        """
        rag = await self._get_rag_instance(kb_id)
        resolved = Path(file_path).expanduser()
        if not resolved.exists():
            raise FileNotFoundError(f"DOCX 文件不存在: {file_path}")

        output_dir = kwargs.pop("output_dir", None)
        parse_method = kwargs.pop("parse_method", None)
        display_stats = kwargs.pop("display_stats", None)
        parser = kwargs.pop("parser", None)

        parse_param_keys = {
            "lang",
            "device",
            "start_page",
            "end_page",
            "formula",
            "table",
            "backend",
            "source",
            "timeout",
        }
        parse_kwargs: dict[str, Any] = {}
        for k in list(kwargs.keys()):
            if k in parse_param_keys:
                parse_kwargs[k] = kwargs.pop(k)

        original_parser = getattr(getattr(rag, "config", None), "parser", None)
        try:
            if parser:
                rag.config.parser = str(parser)
            content_list, doc_id = await rag.parse_document(
                file_path=str(resolved),
                output_dir=output_dir,
                parse_method=parse_method,
                display_stats=display_stats,
                **parse_kwargs,
            )
        finally:
            if original_parser and parser:
                rag.config.parser = original_parser

        result = await rag.insert_content_list(
            content_list=content_list,
            file_path=str(resolved),
            doc_id=doc_id,
            **kwargs,
        )

        if result.get("success") and doc_id:
            (
                final_chunk_count,
                final_entity_count,
                final_relation_count,
            ) = await self._finalize_document_after_ingest(
                rag=rag,
                kb_id=kb_id,
                doc_id=doc_id,
                file_path=str(resolved),
                document_date=kwargs.get("document_date"),
                category_path=kwargs.get("category_path"),
                chunk_count=int(result.get("chunk_count") or 0),
            )
            result["chunk_count"] = final_chunk_count
            result["entity_count"] = final_entity_count
            result["relation_count"] = final_relation_count

        return result

    async def process_multimodal(
        self, file_path: str, kb_id: str | None = None, **kwargs: Any
    ) -> dict[str, Any]:
        """
        处理多模态文档 - 简化流程：
        1. 调用 MinerU 解析 PDF 获取 MD
        2. 使用 MDIngestor 直接入库

        Args:
            file_path: 文件路径
            kb_id: 知识库 ID（可选）
        """
        rag = await self._get_rag_instance(kb_id)
        resolved = Path(file_path).expanduser()
        if not resolved.exists():
            raise FileNotFoundError(f"文件不存在: {file_path}")

        doc_id = kwargs.pop("doc_id", None)
        file_name_override = kwargs.pop("file_name", None)
        document_date = kwargs.pop("document_date", None)
        category_path = kwargs.pop("category_path", None)

        # 记录原始文件名
        original_file_name = file_name_override or resolved.name

        # 使用 MinerU 解析 PDF 获取 MD 内容
        from backend.core.tools.ocr.mineru_ocr import MinerUOCR, OCRConfig

        config = OCRConfig(
            backend=kwargs.get("backend", settings.mineru_backend),
            lang=kwargs.get("lang", settings.mineru_lang),
            method=kwargs.get("method", settings.mineru_method),
            formula_enable=kwargs.get("formula", settings.mineru_formula_enable),
            table_enable=kwargs.get("table", settings.mineru_table_enable),
        )

        mineru = MinerUOCR(api_url=settings.mineru_api_url, config=config)

        try:
            # 提交任务
            logger.info(f"提交 MinerU 任务: {resolved}")
            task_id = await mineru.submit_task_async(
                file_path=str(resolved),
                config=config,
            )
            logger.info(f"MinerU 任务已提交: {task_id}")

            # 等待任务完成
            result = await mineru.wait_for_completion_async(
                task_id=task_id,
                timeout=kwargs.get("timeout", 300),
                poll_interval=kwargs.get("poll_interval", 2),
            )

            if result.status.value != "completed":
                raise RuntimeError(f"MinerU 解析失败: {result.error_message}")

            # 获取 MD 内容
            md_result = await mineru.get_task_data_async(
                task_id,
                include_fields=["md", "markdown"],
            )

            md_content = md_result.markdown
            if not md_content:
                raise ValueError("MinerU 返回的 MD 内容为空")

            md_url = md_result.md_url
            logger.info(
                f"MinerU 解析完成，MD 长度: {len(md_content)}, MD URL: {md_url}"
            )

        except Exception as e:
            logger.error(f"MinerU 解析失败: {e}")
            raise
        finally:
            await mineru.close()

        file_path_for_storage = md_url or str(resolved)
        if file_path_for_storage.startswith("http"):
            _, file_path_for_storage = self._clean_file_path(file_path_for_storage)

        from backend.rag.raganything.md_ingestor import MDIngestor

        md_ingestor = MDIngestor(
            rag,
            config={
                "context_window": kwargs.get("context_window", 500),
            },
        )

        try:
            result = await md_ingestor.ingest_from_content(
                md_content=md_content,
                doc_id=doc_id,
                file_path=file_path_for_storage,
                base_url=None,
            )

            if result.get("success"):
                doc_id = result.get("doc_id")
                if doc_id:
                    (
                        final_chunk_count,
                        final_entity_count,
                        final_relation_count,
                    ) = await self._finalize_document_after_ingest(
                        rag=rag,
                        kb_id=kb_id,
                        doc_id=doc_id,
                        file_path=file_path_for_storage,
                        file_name=original_file_name,
                        document_date=document_date,
                        category_path=category_path,
                        chunk_count=int(result.get("chunk_count") or 0),
                    )
                    result["chunk_count"] = final_chunk_count
                    result["entity_count"] = final_entity_count
                    result["relation_count"] = final_relation_count

            return {
                "success": result.get("success", False),
                "doc_id": result.get("doc_id"),
                "file_path": file_path_for_storage,
                "file_name": original_file_name,
                "text_length": result.get("text_length", 0),
                "images_count": result.get("images_count", 0),
                "chunk_count": result.get("chunk_count", 0),
                "entity_count": result.get("entity_count", 0),
                "relation_count": result.get("relation_count", 0),
                "error": result.get("error"),
            }

        finally:
            await md_ingestor.close()

    async def process_md_content(
        self,
        md_content: str,
        kb_id: str | None = None,
        **kwargs: Any,
    ) -> dict[str, Any]:
        rag = await self._get_rag_instance(kb_id)

        doc_id = kwargs.pop("doc_id", None)
        file_name = kwargs.pop("file_name", "untitled.md")
        document_date = kwargs.pop("document_date", None)
        category_path = kwargs.pop("category_path", None)

        from backend.rag.raganything.md_ingestor import MDIngestor

        md_ingestor = MDIngestor(
            rag,
            config={
                "context_window": kwargs.get("context_window", 500),
            },
        )

        try:
            result = await md_ingestor.ingest_from_content(
                md_content=md_content,
                doc_id=doc_id,
                file_path=file_name,
                base_url=None,
            )

            if result.get("success"):
                doc_id = result.get("doc_id")
                if doc_id:
                    (
                        final_chunk_count,
                        final_entity_count,
                        final_relation_count,
                    ) = await self._finalize_document_after_ingest(
                        rag=rag,
                        kb_id=kb_id,
                        doc_id=doc_id,
                        file_path=file_name,
                        file_name=file_name,
                        document_date=document_date,
                        category_path=category_path,
                        chunk_count=int(result.get("chunk_count") or 0),
                    )
                    result["chunk_count"] = final_chunk_count
                    result["entity_count"] = final_entity_count
                    result["relation_count"] = final_relation_count

            return {
                "success": result.get("success", False),
                "doc_id": result.get("doc_id"),
                "file_path": file_name,
                "file_name": file_name,
                "text_length": result.get("text_length", 0),
                "images_count": result.get("images_count", 0),
                "chunk_count": result.get("chunk_count", 0),
                "entity_count": result.get("entity_count", 0),
                "relation_count": result.get("relation_count", 0),
                "error": result.get("error"),
            }

        finally:
            await md_ingestor.close()

    def _build_linearrag_chunks_from_docx(self, file_path: str) -> list[str]:
        resolved = Path(file_path).expanduser()
        if not resolved.exists():
            raise FileNotFoundError(f"DOCX 文件不存在: {file_path}")
        from docx import Document

        max_chars = int(os.getenv("LINEARRAG_DOCX_BLOCK_MAX_CHARS", "1500") or "1500")
        doc = Document(str(resolved))

        chunks: list[str] = []
        current: list[str] = []
        current_len = 0

        def flush() -> None:
            nonlocal current, current_len
            text = "\n".join([t for t in current if str(t).strip()]).strip()
            if text:
                text = re.sub(r"\s+", " ", text).strip()
                if text:
                    chunks.append(text)
            current = []
            current_len = 0

        for para in doc.paragraphs:
            raw = para.text or ""
            text = re.sub(r"\s+", " ", raw).strip()
            if not text:
                continue

            style_name = getattr(getattr(para, "style", None), "name", "") or ""
            style_name_lower = style_name.lower()
            is_heading = style_name_lower.startswith("heading") or (
                "标题" in style_name
            )
            if is_heading:
                flush()
                chunks.append(text)
                continue

            if current_len and (current_len + len(text) + 1 > max_chars):
                flush()
            current.append(text)
            current_len += len(text) + 1

        flush()

        for table in doc.tables:
            rows: list[str] = []
            for row in table.rows:
                cells = [
                    (cell.text or "").strip().replace("\n", " ") for cell in row.cells
                ]
                if any(cells):
                    rows.append("| " + " | ".join(cells) + " |")
            if rows:
                table_md = "\n".join(rows)
                table_md = re.sub(r"\s+", " ", table_md).strip()
                if table_md:
                    chunks.append(table_md)

        return chunks

    async def linearrag_index_docx(
        self,
        file_path: str,
        dataset_name: str | None = None,
        working_dir: str | None = None,
        base_url: str | None = None,
        retrieval_top_k: int | None = None,
    ) -> dict[str, Any]:
        await self._ensure_http_client()
        assert self._http is not None
        base_url = (
            (base_url or "").strip()
            or (os.getenv("LINEARRAG_API_BASE") or "").strip()
            or "http://127.0.0.1:18099"
        ).rstrip("/")
        dataset_name = (
            (dataset_name or "").strip()
            or (os.getenv("LINEARRAG_DATASET_NAME") or "").strip()
            or "nuclear_docx"
        )
        working_dir = (
            (working_dir or "").strip()
            or (os.getenv("LINEARRAG_WORKING_DIR") or "").strip()
            or str(Path(__file__).resolve().parents[2] / "linearrag_storage")
        )
        if retrieval_top_k is None:
            retrieval_top_k = int(os.getenv("LINEARRAG_RETRIEVAL_TOP_K", "5") or "5")
        chunks = await asyncio.to_thread(
            self._build_linearrag_chunks_from_docx,
            file_path,
        )
        resp = await self._http.post(
            f"{base_url}/index",
            json={
                "dataset_name": dataset_name,
                "working_dir": working_dir,
                "retrieval_top_k": int(retrieval_top_k),
                "chunks": chunks,
            },
        )
        try:
            resp.raise_for_status()
        except httpx.HTTPStatusError as e:
            detail = (resp.text or "").strip()
            raise RuntimeError(
                f"LinearRAG 入库失败: {e.response.status_code} {detail}"
            ) from e
        data = resp.json()
        if isinstance(data, dict):
            data.setdefault("chunks", len(chunks))
        return {"success": True, "result": data}

    async def linearrag_query(
        self,
        question: str,
        top_k: int = 5,
        base_url: str | None = None,
    ) -> dict[str, Any]:
        await self._ensure_http_client()
        assert self._http is not None
        base_url = (
            (base_url or "").strip()
            or (os.getenv("LINEARRAG_API_BASE") or "").strip()
            or "http://127.0.0.1:18099"
        ).rstrip("/")
        resp = await self._http.post(
            f"{base_url}/query",
            json={"question": question, "top_k": int(top_k)},
        )
        try:
            resp.raise_for_status()
        except httpx.HTTPStatusError as e:
            detail = (resp.text or "").strip()
            raise RuntimeError(
                f"LinearRAG 查询失败: {e.response.status_code} {detail}"
            ) from e
        data = resp.json()
        return {"success": True, "result": data}

    async def delete_doc_by_id(
        self, doc_id: str, delete_llm_cache: bool = False, kb_id: str | None = None
    ) -> dict[str, Any]:
        """
        从指定知识库删除文档

        Args:
            doc_id: 文档 ID
            delete_llm_cache: 是否删除 LLM 缓存
            kb_id: 知识库 ID（可选）
        """
        target_kb_id = kb_id or self._default_kb_id
        if target_kb_id:
            try:
                if self._kb_manager is None:
                    await self._initialize_kb_manager()
                if self._session_factory is not None:
                    from sqlalchemy import select

                    from backend.db.models import KnowledgeBaseDocument
                    from backend.domain.knowledge_base.backend_policy import (
                        SELF_BACKEND,
                    )
                    from backend.rag.ingestion.delete_service import self_delete_service

                    async with self._get_session() as session:
                        doc = (
                            await session.execute(
                                select(KnowledgeBaseDocument).where(
                                    KnowledgeBaseDocument.kb_id == target_kb_id,
                                    KnowledgeBaseDocument.doc_id == doc_id,
                                )
                            )
                        ).scalar_one_or_none()
                        if doc and (doc.ingestion_backend or "") == SELF_BACKEND:
                            result = await self_delete_service.soft_delete(
                                session,
                                kb_id=target_kb_id,
                                doc_id=doc_id,
                            )
                            if self._kb_manager is not None:
                                await self._kb_manager.update_knowledge_base_stats(
                                    session, target_kb_id
                                )
                            return result
            except Exception:
                logger.exception(
                    "[删除路由] self_ingestion 删除失败: kb_id=%s doc_id=%s",
                    target_kb_id,
                    doc_id,
                )
                raise

        rag = await self._get_rag_instance(kb_id)
        result = await rag.lightrag.adelete_by_doc_id(
            doc_id, delete_llm_cache=delete_llm_cache
        )

        if result:
            try:
                if self._kb_manager is None:
                    await self._initialize_kb_manager()
                if self._session_factory and (kb_id or self._default_kb_id):
                    async with self._get_session() as session:
                        await self._kb_manager.remove_document(
                            session=session,
                            kb_id=kb_id or self._default_kb_id,
                            doc_id=doc_id,
                        )
            except Exception as e:
                logger.warning(f"移除文档记录失败: {e}")

        payload: Any
        if isinstance(result, dict):
            payload = result
        elif hasattr(result, "model_dump"):
            payload = result.model_dump()
        elif hasattr(result, "__dict__"):
            payload = dict(result.__dict__)
        else:
            payload = str(result)
        return {"success": True, "doc_id": doc_id, "result": payload}

    async def query_multiple_kbs(
        self, query_text: str, kb_ids: list[str], mode: str = "mix", **kwargs: Any
    ) -> dict[str, dict[str, Any]]:
        """
        跨知识库查询

        在多个知识库中执行相同查询，返回各知识库的结果

        Args:
            query_text: 查询文本
            kb_ids: 知识库 ID 列表
            mode: 查询模式

        Returns:
            Dict[kb_id, query_result]
        """
        results: dict[str, dict[str, Any]] = {}

        async def query_one(kb_id: str) -> tuple[str, dict[str, Any]]:
            try:
                result = await self.aquery_rich(
                    query_text, mode=mode, kb_id=kb_id, **kwargs
                )
                return kb_id, {"success": True, **result}
            except Exception as e:
                return kb_id, {"success": False, "error": str(e)}

        tasks = [query_one(kb_id) for kb_id in kb_ids]
        query_results = await asyncio.gather(*tasks, return_exceptions=False)

        for kb_id, result in query_results:
            results[kb_id] = result

        return results

    async def _close_resources(self, *, cancel_initialization: bool) -> None:
        task = self._initialization_task
        if cancel_initialization and task is not None and not task.done():
            task.cancel()
            await asyncio.gather(task, return_exceptions=True)
        self._initialization_task = None
        if self._rag is not None:
            try:
                await self._rag.finalize_storages()
            except Exception as e:
                logger.warning(f"RAG finalize failed: {e}")
        self._rag = None
        if self._http is not None:
            try:
                await self._http.aclose()
            except Exception as exc:
                logger.warning("RAG HTTP client close failed: %s", exc)
        self._http = None
        self._runtime.set_http_client(None)
        self._initialized = False

    async def close(self) -> None:
        # Prevent a new initialization attempt from starting while shutdown is
        # cancelling and disposing the current one.
        async with self._initialization_lock:
            await self._close_resources(cancel_initialization=True)

    async def clear_failed_docs(
        self, include_processing: bool = True
    ) -> dict[str, Any]:
        """Clear failed/processing document statuses to prevent automatic retry on next insert.

        This method deletes both the doc_status entry and the full_docs content
        for failed/processing documents, ensuring they won't be automatically retried.

        Args:
            include_processing: If True, also clear PROCESSING status documents (default: True)

        Returns:
            Dict with success status and count of cleared documents.
        """
        await self.initialize()
        if self._rag is None:
            raise RuntimeError("RAG service failed to initialize")
        lightrag = self._rag.lightrag
        if lightrag is None:
            return {"success": False, "error": "LightRAG 未初始化"}

        try:
            from lightrag.kg.json_doc_status_impl import DocStatus
        except Exception as e:
            return {
                "success": False,
                "error": f"无法加载 DocStatus: {type(e).__name__}: {e}",
            }

        try:
            doc_ids_to_clear: set[str] = set()
            status_counts: dict[str, int] = {}

            failed_docs = lightrag.get_docs_by_status(DocStatus.FAILED)
            if isawaitable(failed_docs):
                failed_docs = await failed_docs
            if failed_docs:
                failed_ids = (
                    list(failed_docs.keys()) if isinstance(failed_docs, dict) else []
                )
                doc_ids_to_clear.update(failed_ids)
                status_counts["FAILED"] = len(failed_ids)

            if include_processing:
                processing_docs = lightrag.get_docs_by_status(DocStatus.PROCESSING)
                if isawaitable(processing_docs):
                    processing_docs = await processing_docs
                if processing_docs:
                    processing_ids = (
                        list(processing_docs.keys())
                        if isinstance(processing_docs, dict)
                        else []
                    )
                    doc_ids_to_clear.update(processing_ids)
                    status_counts["PROCESSING"] = len(processing_ids)

            if not doc_ids_to_clear:
                return {"success": True, "cleared": 0, "message": "没有需要清除的文档"}

            cleared_count = 0
            ordered_ids = list(doc_ids_to_clear)
            for offset in range(0, len(ordered_ids), 500):
                batch = ordered_ids[offset : offset + 500]
                try:
                    await lightrag.doc_status.delete(batch)
                    await lightrag.full_docs.delete(batch)
                    logger.info("已批量清除文档状态和内容: count=%d", len(batch))
                    cleared_count += len(batch)
                except Exception as e:
                    logger.warning(
                        "批量清除文档状态时出错: offset=%d count=%d error=%s",
                        offset,
                        len(batch),
                        e,
                    )

            return {
                "success": True,
                "cleared": cleared_count,
                "doc_ids": list(doc_ids_to_clear),
                "status_counts": status_counts,
            }
        except Exception as e:
            return {
                "success": False,
                "error": f"清除文档状态时出错: {type(e).__name__}: {e}",
            }

    async def purge_all_docs(self, delete_llm_cache: bool = True) -> dict[str, Any]:
        await self.initialize()
        if self._rag is None:
            raise RuntimeError("RAG service failed to initialize")
        lightrag = self._rag.lightrag
        if lightrag is None:
            return {"success": False, "error": "LightRAG 未初始化"}

        try:
            from lightrag.kg.json_doc_status_impl import DocStatus
        except Exception as e:
            return {
                "success": False,
                "error": f"无法加载 DocStatus: {type(e).__name__}: {e}",
            }

        concurrency = int(os.getenv("RAG_PURGE_CONCURRENCY", "2") or "2")
        if concurrency <= 0:
            concurrency = 1
        max_seconds = float(os.getenv("RAG_PURGE_MAX_SECONDS", "0") or "0")
        deadline = time.monotonic() + max_seconds if max_seconds > 0 else None

        deleted: list[str] = []
        failed: dict[str, str] = {}
        total = 0
        semaphore = __import__("asyncio").Semaphore(concurrency)

        async def _delete_one(doc_id: str) -> None:
            if deadline is not None and time.monotonic() >= deadline:
                failed[doc_id] = "timeout"
                return
            async with semaphore:
                if deadline is not None and time.monotonic() >= deadline:
                    failed[doc_id] = "timeout"
                    return
                try:
                    await lightrag.adelete_by_doc_id(
                        doc_id, delete_llm_cache=delete_llm_cache
                    )
                    deleted.append(doc_id)
                except Exception as e:
                    failed[doc_id] = f"{type(e).__name__}: {e}"

        batch_size = max(1, int(os.getenv("RAG_PURGE_BATCH_SIZE", "200") or "200"))
        for status in DocStatus:
            try:
                docs = lightrag.get_docs_by_status(status)
                if isawaitable(docs):
                    docs = await docs
                keys = docs.keys() if hasattr(docs, "keys") else ()
                batch: list[str] = []
                for raw_doc_id in keys:
                    if not raw_doc_id:
                        continue
                    doc_id = str(raw_doc_id)
                    total += 1
                    if deadline is not None and time.monotonic() >= deadline:
                        failed[doc_id] = "timeout"
                        continue
                    batch.append(doc_id)
                    if len(batch) >= batch_size:
                        await asyncio.gather(
                            *(_delete_one(item) for item in batch),
                        )
                        batch.clear()
                if batch:
                    await asyncio.gather(*(_delete_one(item) for item in batch))
            except Exception as exc:
                logger.warning("枚举待清理文档失败: status=%s error=%s", status, exc)

        return {
            "success": True,
            "total": total,
            "deleted": len(deleted),
            "failed": failed,
        }

    async def reset(
        self,
        new_work_dir: str | None = None,
        clear_old_work_dir: bool = True,
    ) -> dict[str, Any]:
        purge_result: dict[str, Any] | None = None
        uses_external_storages = bool(
            settings.redis_host
            or settings.milvus_connection_uri
            or os.getenv("NEO4J_URI")
        )
        if clear_old_work_dir and not uses_external_storages:
            try:
                purge_result = await self.purge_all_docs(delete_llm_cache=True)
            except Exception as e:
                purge_result = {"success": False, "error": f"{type(e).__name__}: {e}"}
        elif clear_old_work_dir and uses_external_storages:
            purge_result = {
                "success": True,
                "skipped": True,
                "reason": "外部存储已启用，reset 通过切换 workspace 达到清库效果",
            }

        old_work_dir = (
            self._rag_work_dir
            or os.getenv("RAG_WORK_DIR")
            or os.getenv("WORKING_DIR")
            or "./rag_storage"
        )
        old_workspace = (os.getenv("RAG_WORKSPACE") or "").strip() or None
        if new_work_dir is None:
            old_path = Path(old_work_dir).expanduser()
            stamp = time.strftime("%Y%m%d_%H%M%S", time.localtime())
            new_work_dir = str(old_path.parent / f"{old_path.name}_reset_{stamp}")
        await self.close()
        if clear_old_work_dir:
            await asyncio.to_thread(self._safe_clear_dir, old_work_dir)
        os.environ["RAG_WORK_DIR"] = new_work_dir
        self._rag_work_dir = new_work_dir
        self._runtime.set_rag_work_dir(new_work_dir)
        try:
            os.environ["RAG_WORKSPACE"] = (
                Path(new_work_dir).expanduser().resolve().name or "default"
            )
        except Exception:
            os.environ["RAG_WORKSPACE"] = "default"
        return {
            "success": True,
            "old_work_dir": old_work_dir,
            "old_workspace": old_workspace,
            "new_work_dir": new_work_dir,
            "new_workspace": os.environ.get("RAG_WORKSPACE"),
            "cleared_old_work_dir": bool(clear_old_work_dir),
            "purge_result": purge_result,
        }

    def _safe_clear_dir(self, dir_path: str) -> bool:
        try:
            p = Path(dir_path).expanduser().resolve()
        except Exception:
            return False
        if not p.exists() or not p.is_dir():
            return False
        if str(p) in {"/", str(Path.home())}:
            return False
        project_root = Path(__file__).resolve().parents[2]
        allowed = False
        try:
            allowed = p.is_relative_to(project_root)
        except Exception:
            allowed = str(p).startswith(str(project_root))
        if not allowed and not str(p).startswith("/data/"):
            return False
        try:
            shutil.rmtree(p, ignore_errors=True)
            p.mkdir(parents=True, exist_ok=True)
            return True
        except Exception:
            return False

    def _build_llm_model_func(self, for_ingestion: bool = False) -> Callable[..., Any]:
        runtime = (
            self._runtime_settings if self._runtime_settings is not None else settings
        )
        # 知识库入库使用独立的 LLM 配置
        llm_config = (
            runtime.ingestion_llm_config if for_ingestion else runtime.active_llm_config
        )
        api_base = (llm_config.get("base_url") or "").rstrip("/")
        api_key = llm_config.get("api_key")
        model = llm_config.get("model")
        from backend.core.models.context_limits import (
            resolve_max_input_tokens,
            resolve_max_tokens,
        )

        model_max_tokens = resolve_max_tokens(llm_config, api_model=model)
        model_context_limit = resolve_max_input_tokens(llm_config, api_model=model)

        # 记录使用的模型配置
        provider_type = "知识库入库" if for_ingestion else "智能体回答"
        logger.info(
            f"[{provider_type}] LLM 配置: provider={runtime.ingestion_llm_provider if for_ingestion else runtime.llm_provider}, "
            f"model={model}, api_base={api_base}, context_limit={model_context_limit}"
        )

        async def llm_model_func(
            prompt: str,
            system_prompt: str | None = None,
            history_messages: list[dict[str, Any]] | None = None,
            **kwargs: Any,
        ) -> str:
            if self._http is None:
                raise RuntimeError("HTTP client not initialized")

            messages: list[dict[str, Any]] = []
            if system_prompt:
                messages.append({"role": "system", "content": system_prompt})
            if history_messages:
                messages.extend(history_messages)
            messages.append({"role": "user", "content": prompt})

            # 记录 LLM 调用日志
            prompt_preview = prompt[:100] + "..." if len(prompt) > 100 else prompt
            logger.info(
                f"[{provider_type}] LLM 调用: model={model}, prompt_preview={prompt_preview}"
            )

            max_retries = 3
            last_exception = None

            for attempt in range(max_retries):
                try:
                    # 计算合适的 max_tokens，避免超过模型上下文限制（来自 MySQL / 配置，默认 128K）
                    RESERVED_TOKENS = 4000
                    input_chars = sum(len(str(m.get("content", ""))) for m in messages)
                    estimated_input_tokens = input_chars // 4
                    available_tokens = (
                        model_context_limit - estimated_input_tokens - RESERVED_TOKENS
                    )
                    requested_max_tokens = int(
                        kwargs.get("max_tokens", model_max_tokens)
                    )
                    max_tokens = min(requested_max_tokens, available_tokens)
                    max_tokens = max(max_tokens, 1000)  # 至少保留 1000 tokens

                    resp = await self._http.post(
                        f"{api_base}/chat/completions",
                        json={
                            "model": model,
                            "messages": messages,
                            "temperature": float(
                                kwargs.get("temperature", runtime.temperature)
                            ),
                            "max_tokens": max_tokens,
                        },
                        headers={
                            "Authorization": f"Bearer {api_key}",
                            "Content-Type": "application/json",
                        },
                    )
                    resp.raise_for_status()
                    data = resp.json()
                    content = (
                        data.get("choices", [{}])[0]
                        .get("message", {})
                        .get("content", "")
                        or ""
                    )
                    content_preview = (
                        content[:100] + "..." if len(content) > 100 else content
                    )
                    logger.info(
                        f"[{provider_type}] LLM 响应: content_preview={content_preview}"
                    )
                    return content

                except httpx.ReadTimeout as e:
                    last_exception = e
                    if attempt < max_retries - 1:
                        wait_time = 2**attempt
                        logger.warning(
                            f"[{provider_type}] LLM 调用超时，{wait_time}秒后重试 "
                            f"(尝试 {attempt + 1}/{max_retries}): {e}"
                        )
                        await asyncio.sleep(wait_time)
                    else:
                        logger.error(
                            f"[{provider_type}] LLM 调用超时，已达到最大重试次数: {e}"
                        )
                        raise

                except httpx.HTTPStatusError as e:
                    detail = (resp.text or "").strip()
                    logger.error(
                        f"[{provider_type}] LLM 请求失败: {e.response.status_code} {detail}"
                    )
                    raise RuntimeError(
                        f"LLM 请求失败: {e.response.status_code} {detail}"
                    ) from e

                except Exception as e:
                    last_exception = e
                    if attempt < max_retries - 1:
                        wait_time = 2**attempt
                        logger.warning(
                            f"[{provider_type}] LLM 调用异常，{wait_time}秒后重试 "
                            f"(尝试 {attempt + 1}/{max_retries}): {e}"
                        )
                        await asyncio.sleep(wait_time)
                    else:
                        logger.error(
                            f"[{provider_type}] LLM 调用失败，已达到最大重试次数: {e}"
                        )
                        raise

            raise last_exception or RuntimeError("LLM 调用失败")

        return llm_model_func

    def _build_vlm_model_func(self) -> Callable[..., Any]:
        runtime = (
            self._runtime_settings if self._runtime_settings is not None else settings
        )
        api_base = (runtime.vlm_api_base or "").rstrip("/")
        api_key = runtime.vlm_api_key
        model = runtime.vlm_model or "glm-4.6v"

        async def vision_model_func(
            prompt: str,
            system_prompt: str | None = None,
            history_messages: list[dict[str, Any]] | None = None,
            image_data: str | None = None,
            **kwargs: Any,
        ) -> str:
            if self._http is None:
                raise RuntimeError("HTTP client not initialized")

            direct_messages = kwargs.get("messages")
            if isinstance(direct_messages, list) and direct_messages:
                max_retries = 3
                last_exception = None

                for attempt in range(max_retries):
                    try:
                        resp = await self._http.post(
                            f"{api_base}/chat/completions",
                            json={
                                "model": model,
                                "messages": direct_messages,
                                "temperature": float(kwargs.get("temperature", 0.3)),
                                "max_tokens": int(kwargs.get("max_tokens", 32000)),
                            },
                            headers={
                                "Authorization": f"Bearer {api_key}",
                                "Content-Type": "application/json",
                            },
                        )
                        resp.raise_for_status()
                        data = resp.json()
                        return (
                            data.get("choices", [{}])[0]
                            .get("message", {})
                            .get("content", "")
                            or ""
                        )
                    except httpx.ReadTimeout as e:
                        last_exception = e
                        if attempt < max_retries - 1:
                            wait_time = 2**attempt
                            logger.warning(
                                f"[VLM] 调用超时，{wait_time}秒后重试 "
                                f"(尝试 {attempt + 1}/{max_retries}): {e}"
                            )
                            await asyncio.sleep(wait_time)
                        else:
                            logger.error(f"[VLM] 调用超时，已达到最大重试次数: {e}")
                            raise
                    except Exception as e:
                        last_exception = e
                        if attempt < max_retries - 1:
                            wait_time = 2**attempt
                            logger.warning(
                                f"[VLM] 调用异常，{wait_time}秒后重试 "
                                f"(尝试 {attempt + 1}/{max_retries}): {e}"
                            )
                            await asyncio.sleep(wait_time)
                        else:
                            logger.error(f"[VLM] 调用失败，已达到最大重试次数: {e}")
                            raise
                raise last_exception or RuntimeError("VLM 调用失败")

            messages: list[dict[str, Any]] = []
            if system_prompt:
                messages.append({"role": "system", "content": system_prompt})
            if history_messages:
                messages.extend(history_messages)

            if image_data:
                messages.append(
                    {
                        "role": "user",
                        "content": [
                            {"type": "text", "text": prompt},
                            {
                                "type": "image_url",
                                "image_url": {
                                    "url": f"data:image/jpeg;base64,{image_data}"
                                },
                            },
                        ],
                    }
                )
            else:
                messages.append({"role": "user", "content": prompt})

            max_retries = 3
            last_exception = None

            for attempt in range(max_retries):
                try:
                    resp = await self._http.post(
                        f"{api_base}/chat/completions",
                        json={
                            "model": model,
                            "messages": messages,
                            "temperature": float(kwargs.get("temperature", 0.3)),
                            "max_tokens": int(kwargs.get("max_tokens", 32000)),
                        },
                        headers={
                            "Authorization": f"Bearer {api_key}",
                            "Content-Type": "application/json",
                        },
                    )
                    resp.raise_for_status()
                    data = resp.json()
                    return (
                        data.get("choices", [{}])[0]
                        .get("message", {})
                        .get("content", "")
                        or ""
                    )
                except httpx.ReadTimeout as e:
                    last_exception = e
                    if attempt < max_retries - 1:
                        wait_time = 2**attempt
                        logger.warning(
                            f"[VLM] 调用超时，{wait_time}秒后重试 "
                            f"(尝试 {attempt + 1}/{max_retries}): {e}"
                        )
                        await asyncio.sleep(wait_time)
                    else:
                        logger.error(f"[VLM] 调用超时，已达到最大重试次数: {e}")
                        raise
                except Exception as e:
                    last_exception = e
                    if attempt < max_retries - 1:
                        wait_time = 2**attempt
                        logger.warning(
                            f"[VLM] 调用异常，{wait_time}秒后重试 "
                            f"(尝试 {attempt + 1}/{max_retries}): {e}"
                        )
                        await asyncio.sleep(wait_time)
                    else:
                        logger.error(f"[VLM] 调用失败，已达到最大重试次数: {e}")
                        raise
            raise last_exception or RuntimeError("VLM 调用失败")

        return vision_model_func

    def _build_query_llm_model_func(self) -> Callable[..., Any]:
        """构建查询专用的 LLM 函数（独立并发池）"""
        runtime = (
            self._runtime_settings if self._runtime_settings is not None else settings
        )
        from backend.core.models.context_limits import resolve_max_tokens

        llm_config = runtime.active_llm_config
        api_base = (llm_config.get("base_url") or "").rstrip("/")
        api_key = llm_config.get("api_key")
        model = llm_config.get("model")

        logger.info(
            f"[查询专用] LLM 配置: provider={runtime.llm_provider}, model={model}, api_base={api_base}"
        )

        async def query_llm_model_func(
            prompt: str,
            system_prompt: str | None = None,
            history_messages: list[dict[str, Any]] | None = None,
            **kwargs: Any,
        ) -> str:
            if self._http is None:
                raise RuntimeError("HTTP client not initialized")

            messages: list[dict[str, Any]] = []
            if system_prompt:
                messages.append({"role": "system", "content": system_prompt})
            if history_messages:
                messages.extend(history_messages)
            messages.append({"role": "user", "content": prompt})

            prompt_preview = prompt[:100] + "..." if len(prompt) > 100 else prompt
            logger.info(
                f"[查询专用] LLM 调用: model={model}, prompt_preview={prompt_preview}"
            )

            max_retries = 3
            last_exception = None

            for attempt in range(max_retries):
                try:
                    resp = await self._http.post(
                        f"{api_base}/chat/completions",
                        json={
                            "model": model,
                            "messages": messages,
                            "temperature": float(kwargs.get("temperature", 0.3)),
                            "max_tokens": int(
                                kwargs.get(
                                    "max_tokens",
                                    resolve_max_tokens(llm_config, api_model=model),
                                )
                            ),
                        },
                        headers={
                            "Authorization": f"Bearer {api_key}",
                            "Content-Type": "application/json",
                        },
                        timeout=300.0,
                    )
                    resp.raise_for_status()
                    data = resp.json()
                    return (
                        data.get("choices", [{}])[0]
                        .get("message", {})
                        .get("content", "")
                        or ""
                    )
                except httpx.ReadTimeout as e:
                    last_exception = e
                    if attempt < max_retries - 1:
                        wait_time = 2**attempt
                        logger.warning(
                            f"[查询专用] LLM 调用超时，{wait_time}秒后重试 "
                            f"(尝试 {attempt + 1}/{max_retries}): {e}"
                        )
                        await asyncio.sleep(wait_time)
                    else:
                        logger.error(
                            f"[查询专用] LLM 调用超时，已达到最大重试次数: {e}"
                        )
                        raise
                except httpx.HTTPStatusError as e:
                    detail = ""
                    try:
                        detail = e.response.text
                    except Exception:
                        pass
                    if e.response.status_code >= 500 and attempt < max_retries - 1:
                        wait_time = 2**attempt
                        logger.warning(
                            f"[查询专用] LLM 服务端错误 {e.response.status_code}，{wait_time}秒后重试 "
                            f"(尝试 {attempt + 1}/{max_retries}): {detail}"
                        )
                        await asyncio.sleep(wait_time)
                    else:
                        logger.error(
                            f"[查询专用] LLM 请求失败: {e.response.status_code} {detail}"
                        )
                        raise RuntimeError(
                            f"LLM 请求失败: {e.response.status_code} {detail}"
                        ) from e
                except Exception as e:
                    last_exception = e
                    if attempt < max_retries - 1:
                        wait_time = 2**attempt
                        logger.warning(
                            f"[查询专用] LLM 调用异常，{wait_time}秒后重试 "
                            f"(尝试 {attempt + 1}/{max_retries}): {e}"
                        )
                        await asyncio.sleep(wait_time)
                    else:
                        logger.error(
                            f"[查询专用] LLM 调用失败，已达到最大重试次数: {e}"
                        )
                        raise

            raise last_exception or RuntimeError("LLM 调用失败")

        return query_llm_model_func

    def _build_rerank_model_func(self) -> Callable[..., Any]:
        """构建 Rerank 模型函数"""
        runtime = (
            self._runtime_settings if self._runtime_settings is not None else settings
        )
        api_base = (runtime.rerank_api_base or "").rstrip("/")
        api_key = runtime.rerank_api_key
        model = runtime.rerank_model or "Qwen3-Reranker-8B"

        if not api_base:
            logger.warning("Rerank API base not configured, rerank will be disabled")
            return None

        logger.info(f"Initializing rerank model with API base: {api_base}")

        async def rerank_model_func(
            query: str,
            documents: list[str],
            top_n: int | None = None,
            **kwargs: Any,
        ) -> list[dict[str, Any]]:
            """Rerank documents based on query relevance"""
            if self._http is None:
                logger.error("HTTP client not initialized")
                raise RuntimeError("HTTP client not initialized")

            logger.debug(
                f"Rerank request: query='{query[:50]}...', docs={len(documents)}"
            )
            top_n_val = top_n or len(documents)

            # 详细计时开始
            total_start_time = time.time()
            logger.info(
                f"[计时-重排序] 开始 | docs={len(documents)} | top_n={top_n_val}"
            )

            # 使用原生 Rerank API (支持 DashScope qwen3-rerank 和 vLLM Qwen3-Reranker-8B)
            try:
                method_start_time = time.time()
                # vLLM 使用 /v1/rerank，DashScope 使用 /reranks
                # 根据 api_base 判断使用哪个端点：
                # - DashScope: https://dashscope.aliyuncs.com/compatible-mode/v1
                # - vLLM: http://x.x.x.x:8012/v1 (本地IP地址)
                if "/v1" in api_base and "dashscope" not in api_base.lower():
                    rerank_endpoint = f"{api_base}/rerank"
                else:
                    rerank_endpoint = f"{api_base}/reranks"
                resp = await self._http.post(
                    rerank_endpoint,
                    json={
                        "model": model,
                        "query": query,
                        "documents": documents,
                        "top_n": top_n_val,
                    },
                    headers={
                        "Authorization": f"Bearer {api_key}",
                        "Content-Type": "application/json",
                    },
                    timeout=180.0,
                )
                method_duration = time.time() - method_start_time

                if resp.status_code == 200:
                    data = resp.json()
                    if "results" in data and data["results"]:
                        results = data["results"]
                        total_duration = time.time() - total_start_time
                        logger.info(
                            f"[计时-重排序] 原生API成功 | 耗时={method_duration:.3f}s | 总耗时={total_duration:.3f}s"
                        )
                        logger.info(
                            f"Rerank native API success: {len(results)} results"
                        )
                        return [
                            {
                                "index": result.get("index", 0),
                                "document": documents[result.get("index", 0)],
                                "relevance_score": result.get("relevance_score", 0.0),
                            }
                            for result in results
                        ]
                    else:
                        logger.warning(f"Rerank API returned no results: {data}")
                else:
                    logger.warning(f"Rerank API returned status {resp.status_code}")
            except Exception as e:
                logger.error(f"Rerank API failed: {e}")

            # Rerank API 失败，返回原始列表（降级方案）
            total_duration = time.time() - total_start_time
            logger.warning(
                f"[计时-重排序] Rerank API 失败 | 总耗时={total_duration:.3f}s"
            )
            logger.warning("Rerank failed, returning original order")
            return [
                {"index": i, "document": doc, "relevance_score": 0.0}
                for i, doc in enumerate(documents)
            ]

        return rerank_model_func

    def _build_embedding_func(
        self, api_base: str, api_key: str | None, model: str
    ) -> Callable[[list[str]], Any]:
        api_base = api_base.rstrip("/")

        async def embed(texts: list[str]) -> Any:
            if self._http is None:
                raise RuntimeError("HTTP client not initialized")

            batch_size = int(os.getenv("EMBEDDING_API_BATCH_SIZE", "64"))
            max_request_tokens = int(
                os.getenv("EMBEDDING_API_MAX_REQUEST_TOKENS", "8000")
            )
            max_request_chars = int(
                os.getenv(
                    "EMBEDDING_API_MAX_CHARS",
                    str(max_request_tokens * 4),
                )
            )
            vectors: list[list[float]] = []

            def estimate_tokens(s: str) -> int:
                if not s:
                    return 1
                sample = s[:2000]
                cjk = 0
                for ch in sample:
                    if "\u4e00" <= ch <= "\u9fff":
                        cjk += 1
                if cjk / max(1, len(sample)) > 0.2:
                    return max(1, int(len(s) / 1.2))
                return max(1, int(len(s) / 4))

            def normalize_text(s: str) -> str:
                if len(s) <= max_request_chars:
                    return s
                return s[:max_request_chars]

            async def send_batch(batch: list[str], attempt: int = 0) -> None:
                resp = await self._http.post(
                    f"{api_base}/embeddings",
                    json={"model": model, "input": batch},
                    headers={
                        "Authorization": f"Bearer {api_key}" if api_key else "",
                        "Content-Type": "application/json",
                    },
                )
                try:
                    resp.raise_for_status()
                except httpx.HTTPStatusError as e:
                    detail = (resp.text or "").strip()
                    is_ctx = e.response.status_code == 400 and (
                        "maximum context length" in detail.lower()
                        or "input tokens" in detail.lower()
                        or "input_tokens" in detail.lower()
                        or "context length" in detail.lower()
                    )
                    if is_ctx and len(batch) > 1:
                        mid = max(1, len(batch) // 2)
                        await send_batch(batch[:mid], attempt=attempt)
                        await send_batch(batch[mid:], attempt=attempt)
                        return
                    if is_ctx and len(batch) == 1 and attempt < 2:
                        s = batch[0] or ""
                        new_len = max(1000, int(len(s) * 0.5))
                        await send_batch([s[:new_len]], attempt=attempt + 1)
                        return
                    raise RuntimeError(
                        f"Embedding 请求失败: {e.response.status_code} {detail}"
                    ) from e
                data = resp.json()
                if isinstance(data, dict) and "error" in data and not data.get("data"):
                    error_obj = data.get("error")
                    if isinstance(error_obj, dict):
                        raise RuntimeError(error_obj.get("message") or str(error_obj))
                    raise RuntimeError(str(error_obj))
                items = data.get("data") or []
                vectors.extend([(it.get("embedding") or []) for it in items])

            i = 0
            while i < len(texts):
                batch: list[str] = []
                batch_tokens = 0
                while i < len(texts) and len(batch) < batch_size:
                    t = normalize_text(texts[i] or "")
                    t_tokens = estimate_tokens(t)
                    if batch and batch_tokens + t_tokens > max_request_tokens:
                        break
                    batch.append(t)
                    batch_tokens += t_tokens
                    i += 1
                if not batch:
                    batch.append(normalize_text(texts[i] or ""))
                    i += 1
                await send_batch(batch)

            return np.asarray(vectors, dtype=np.float32)

        return embed

    def _build_mock_embedding_func(
        self, embedding_dim: int
    ) -> Callable[[list[str]], Any]:
        async def embed(texts: list[str]) -> Any:
            out = np.zeros((len(texts), embedding_dim), dtype=np.float32)
            for i, text in enumerate(texts):
                tokens = re.findall(r"\w+", (text or "").lower())
                if not tokens:
                    continue
                vec = out[i]
                for tok in tokens:
                    h = hashlib.md5(tok.encode("utf-8", errors="ignore")).digest()
                    idx = int.from_bytes(h[:4], "big", signed=False) % embedding_dim
                    sign = 1.0 if (h[4] & 1) == 0 else -1.0
                    vec[idx] += sign
                norm = float(np.linalg.norm(vec))
                if norm > 0:
                    vec /= norm
            return out

        return embed

    async def _infer_embedding_dim(
        self, api_base: str, api_key: str | None, model: str
    ) -> int:
        if self._http is None:
            raise RuntimeError("HTTP client not initialized")

        api_base = api_base.rstrip("/")
        infer_timeout = float(os.getenv("EMBEDDING_INFER_TIMEOUT", "10") or "10")
        resp = await self._http.post(
            f"{api_base}/embeddings",
            json={"model": model, "input": ["dimension probe"]},
            headers={
                "Authorization": f"Bearer {api_key}" if api_key else "",
                "Content-Type": "application/json",
            },
            timeout=httpx.Timeout(infer_timeout),
        )
        resp.raise_for_status()
        data = resp.json()
        if isinstance(data, dict) and "error" in data and not data.get("data"):
            error_obj = data.get("error")
            if isinstance(error_obj, dict):
                raise RuntimeError(error_obj.get("message") or str(error_obj))
            raise RuntimeError(str(error_obj))
        items = data.get("data") or []
        if not items:
            raise RuntimeError("嵌入服务返回空数据，无法推断 embedding_dim")
        embedding = items[0].get("embedding") or []
        if not embedding:
            raise RuntimeError("嵌入服务未返回 embedding 向量")
        return int(len(embedding))

    async def get_all_docs_status(self) -> dict[str, Any]:
        """获取知识库中所有文档的状态信息

        Returns:
            Dict包含:
            - status_counts: 各状态文档数量统计
            - docs: 所有文档的详细信息列表
        """
        await self.initialize()
        if self._rag is None:
            raise RuntimeError("RAG service failed to initialize")
        return await self._doc_status_service.get_all_docs_status(self._rag.lightrag)

    async def get_processed_docs(self) -> list[dict[str, Any]]:
        """获取已处理完成的文档列表

        Returns:
            已处理完成(PROCESSED状态)的文档列表
        """
        await self.initialize()
        if self._rag is None:
            raise RuntimeError("RAG service failed to initialize")
        return await self._doc_status_service.get_processed_docs(self._rag.lightrag)


def get_rag_service() -> RagService:
    return _RAG_SERVICE_POOL.get()


_RAG_SERVICE_POOL = LoopLocalServicePool(RagService)
