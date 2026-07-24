"""Runtime helpers for the RAG service.

This module keeps process/loop scoped runtime state out of ``service.py`` while
preserving the public ``RagService`` API.
"""

from __future__ import annotations

import asyncio
import contextlib
import threading
import weakref
from collections.abc import AsyncIterator, Awaitable, Callable
from inspect import isawaitable
from pathlib import Path
from typing import Any

import httpx

from backend.config.settings import settings
from backend.domain.knowledge_base.backend_policy import (
    BLOCKED_BACKEND,
    LEGACY_BACKEND,
    resolve_retrieval_backend,
)


class RagRuntimeManager:
    """Owns generic RAG process runtime state such as HTTP and env loading."""

    def __init__(
        self,
        *,
        logger: Any,
        env_override_callback: Callable[[Path], None],
    ) -> None:
        self.logger = logger
        self.env_override_callback = env_override_callback
        self.http: httpx.AsyncClient | None = None
        self.runtime_settings: Any | None = None
        self.rag_work_dir: str | None = None

    def load_backend_env(self) -> None:
        backend_env = Path(__file__).resolve().parents[1] / ".env"
        if not backend_env.exists():
            return
        from dotenv import load_dotenv

        load_dotenv(backend_env, override=False)
        self.env_override_callback(backend_env)

    async def ensure_http_client(self) -> httpx.AsyncClient:
        if self.http is None:
            self.load_backend_env()
            self.http = httpx.AsyncClient(trust_env=False, timeout=600.0)
        return self.http

    def set_http_client(self, client: httpx.AsyncClient | None) -> None:
        self.http = client

    def set_runtime_settings(self, runtime_settings: Any | None) -> None:
        self.runtime_settings = runtime_settings

    def set_rag_work_dir(self, rag_work_dir: str | None) -> None:
        self.rag_work_dir = rag_work_dir

    async def close_http_client(self) -> None:
        if self.http is not None:
            await self.http.aclose()
            self.http = None


class KbRuntimeRegistry:
    """Resolves KB-scoped runtime dependencies and RAG instances."""

    def __init__(self, *, logger: Any, default_kb_code: str = "default") -> None:
        self.logger = logger
        self.default_kb_code = default_kb_code
        self.default_kb_id: str | None = None
        self.kb_manager: Any | None = None
        self.session_factory: Any | None = None

    async def initialize(self) -> None:
        from backend.rag.knowledge_base import get_kb_manager

        self.kb_manager = get_kb_manager()
        try:
            from backend.db.connection import get_async_session_factory

            self.session_factory = get_async_session_factory()
        except Exception as exc:
            self.logger.warning("获取 session factory 失败: %s", exc)
            self.session_factory = None

    @contextlib.asynccontextmanager
    async def get_session(self) -> AsyncIterator[Any]:
        if self.session_factory is None:
            raise RuntimeError("Session factory not initialized")
        async with self.session_factory() as session:
            try:
                yield session
                await session.commit()
            except Exception:
                await session.rollback()
                raise

    async def ensure_default_kb(self) -> str:
        if self.default_kb_id:
            return self.default_kb_id

        if self.kb_manager is None:
            await self.initialize()

        if self.session_factory is None:
            self.logger.warning("Session factory 未初始化，跳过默认知识库创建")
            return ""

        async with self.get_session() as session:
            kb = await self.kb_manager.get_knowledge_base_by_code(
                session,
                self.default_kb_code,
            )
            if not kb:
                self.logger.info("默认知识库不存在，自动创建...")
                try:
                    kb = await self.kb_manager.create_knowledge_base(
                        session=session,
                        kb_code=self.default_kb_code,
                        kb_name="默认知识库",
                        description="系统自动创建的默认知识库",
                        is_default=True,
                    )
                except Exception as exc:
                    self.logger.warning("创建默认知识库失败: %s", exc)
                    return ""

            self.default_kb_id = kb.kb_id
            return self.default_kb_id

    async def get_rag_instance(
        self,
        kb_id: str | None,
        *,
        initialize_default: Callable[[], Awaitable[None]],
        fallback_rag_provider: Callable[[], Any | None],
    ) -> Any:
        if kb_id is None:
            await initialize_default()
            fallback_rag = fallback_rag_provider()
            if fallback_rag is not None:
                return fallback_rag
            kb_id = self.default_kb_id

        if not kb_id:
            await initialize_default()
            fallback_rag = fallback_rag_provider()
            if fallback_rag is not None:
                return fallback_rag
            raise RuntimeError("无法获取 RAG 实例：知识库 ID 为空")

        if self.kb_manager is None:
            await self.initialize()

        if self.session_factory is None:
            await initialize_default()
            fallback_rag = fallback_rag_provider()
            if fallback_rag is not None:
                return fallback_rag
            raise RuntimeError("无法获取 RAG 实例：session factory 未初始化")

        async with self.get_session() as session:
            return await self.kb_manager.get_rag_instance(session, kb_id)

    async def resolve_query_backend(self, kb_id: str | None) -> str:
        if self.kb_manager is None:
            await self.initialize()
        if self.session_factory is None:
            return LEGACY_BACKEND

        target_kb_id = kb_id
        if target_kb_id is None:
            target_kb_id = self.default_kb_id or await self.ensure_default_kb()
        if not target_kb_id:
            return LEGACY_BACKEND

        try:
            async with self.get_session() as session:
                kb = await self.kb_manager.get_knowledge_base(session, target_kb_id)
            backend = resolve_retrieval_backend(settings, kb=kb)
        except Exception as exc:
            self.logger.warning(
                "[查询路由] 解析查询后端失败，回退 legacy: kb_id=%s, error=%s",
                target_kb_id,
                exc,
            )
            return LEGACY_BACKEND

        if backend == BLOCKED_BACKEND:
            self.logger.warning(
                "[查询路由] KB=%s 显式要求自研检索但全局开关/白名单未满足，回退 legacy",
                target_kb_id,
            )
            return LEGACY_BACKEND
        return backend

    async def retrieve_self_context(
        self,
        query: str,
        kb_id: str | None,
        *,
        chunk_top_k: int = 10,
    ) -> tuple[str, list[dict[str, Any]]]:
        if self.kb_manager is None:
            await self.initialize()
        if self.session_factory is None:
            raise RuntimeError("无法执行 self 查询：session factory 未初始化")
        target_kb_id = kb_id or self.default_kb_id or await self.ensure_default_kb()
        if not target_kb_id:
            raise RuntimeError("无法执行 self 查询：知识库 ID 为空")

        from backend.rag.retriever_v2 import SelfDirectRetrieverV2

        async with self.get_session() as session:
            kb = await self.kb_manager.get_knowledge_base(session, target_kb_id)
            retriever = SelfDirectRetrieverV2(
                session=session,
                kb_id=target_kb_id,
                kb_code=getattr(kb, "kb_code", None),
                graph_profile=settings.self_graph_retrieval_profile,
            )
            chunks = await retriever.retrieve_chunks(query, top_k=chunk_top_k)
            context = retriever.build_context(chunks)
            return context, [chunk.to_dict() for chunk in chunks]


class DocumentStatusService:
    """Read-only document status use cases for LightRAG doc stores."""

    def __init__(self, *, logger: Any) -> None:
        self.logger = logger

    async def get_all_docs_status(
        self,
        lightrag: Any,
        *,
        limit: int = 1000,
    ) -> dict[str, Any]:
        if lightrag is None:
            return {"status_counts": {}, "docs": []}

        try:
            from lightrag.kg.json_doc_status_impl import DocStatus
        except Exception as exc:
            return {
                "status_counts": {},
                "docs": [],
                "error": f"无法加载 DocStatus: {type(exc).__name__}: {exc}",
            }

        try:
            status_counts: dict[str, int] = {}
            all_docs: list[dict[str, Any]] = []
            result_limit = max(1, int(limit))
            for status in DocStatus:
                docs = lightrag.get_docs_by_status(status)
                if isawaitable(docs):
                    docs = await docs
                if docs:
                    status_counts[status.value] = len(docs)
                    for doc_id, doc_status in docs.items():
                        if len(all_docs) >= result_limit:
                            break
                        file_path = getattr(doc_status, "file_path", None) or "unknown"
                        all_docs.append(
                            {
                                "doc_id": doc_id,
                                "file_path": file_path,
                                "status": status.value,
                                "created_at": getattr(doc_status, "created_at", None),
                                "updated_at": getattr(doc_status, "updated_at", None),
                                "chunks_count": getattr(doc_status, "chunks_count", 0),
                                "error_msg": getattr(doc_status, "error_msg", None),
                            }
                        )

            return {
                "status_counts": status_counts,
                "docs": all_docs,
                "total": sum(status_counts.values()),
                "truncated": sum(status_counts.values()) > len(all_docs),
            }
        except Exception as exc:
            self.logger.exception("获取文档状态失败: %s", exc)
            return {
                "status_counts": {},
                "docs": [],
                "error": f"获取文档状态失败: {type(exc).__name__}: {exc}",
            }

    async def get_processed_docs(self, lightrag: Any) -> list[dict[str, Any]]:
        if lightrag is None:
            return []

        try:
            from lightrag.kg.json_doc_status_impl import DocStatus
        except Exception:
            return []

        try:
            processed_docs = lightrag.get_docs_by_status(DocStatus.PROCESSED)
            if isawaitable(processed_docs):
                processed_docs = await processed_docs

            result = []
            for doc_id, doc_status in processed_docs.items():
                file_path = getattr(doc_status, "file_path", None) or "unknown"
                result.append(
                    {
                        "doc_id": doc_id,
                        "file_path": file_path,
                        "status": DocStatus.PROCESSED.value,
                        "created_at": getattr(doc_status, "created_at", None),
                        "updated_at": getattr(doc_status, "updated_at", None),
                        "chunks_count": getattr(doc_status, "chunks_count", 0),
                    }
                )
            return result
        except Exception as exc:
            self.logger.exception("获取已处理文档失败: %s", exc)
            return []


class LoopLocalServicePool:
    """Thread-local, event-loop-local service cache."""

    def __init__(self, factory: Callable[[], Any]) -> None:
        self.factory = factory
        self.local = threading.local()

    def get(self) -> Any:
        try:
            loop = asyncio.get_running_loop()
        except RuntimeError:
            loop = None

        if loop is None:
            service = getattr(self.local, "sync_service", None)
            if service is None:
                service = self.factory()
                self.local.sync_service = service
            return service

        loop_services = getattr(self.local, "loop_services", None)
        if loop_services is None:
            loop_services = weakref.WeakKeyDictionary()
            self.local.loop_services = loop_services

        service = loop_services.get(loop)
        if service is None:
            service = self.factory()
            loop_services[loop] = service
        return service
