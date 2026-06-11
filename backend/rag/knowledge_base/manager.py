"""
知识库管理器

负责：
- 知识库的创建、删除、切换
- RAG 实例的缓存和管理
- 存储资源的协调
"""

import asyncio
import threading
import weakref
from backend.rag.keywords.models import IndexBuildResult, QueryType
from datetime import date
import inspect
import os
from types import SimpleNamespace
from dataclasses import dataclass, field
from datetime import datetime
from typing import Any, Callable, Dict, List, Optional, Tuple

from sqlalchemy.ext.asyncio import AsyncSession

from backend.config.loggers import get_logger
from backend.config.settings import settings
from backend.db.crud.knowledge_base_crud import KnowledgeBaseCRUD, KnowledgeBaseDocumentCRUD
from backend.db.models import KnowledgeBase, KnowledgeBaseDocument, KnowledgeBaseStatus
from backend.rag.keywords.index_manager import KeywordIndexManager, get_keyword_index_manager
from backend.rag.keywords.extractor import PerKBKeywordExtractor
from backend.rag.keywords.bm25_index import BM25Index
from backend.rag.content_filter import (
    ContentFilterManager,
    FilterMode,
    FilterResult,
    RawRetrievalData,
    get_content_filter_manager,
)

logger = get_logger(__name__)

_KNOWN_SUBAGENT_KB_CODES: Dict[str, str] = {
    # 通过环境变量或数据库配置加载，不再硬编码 UUID
    **dict(
        item.split("=", 1)
        for item in os.getenv("SUBAGENT_KB_CODE_MAP", "").split(",")
        if "=" in item
    ),
}
_KNOWN_SUBAGENT_NEBULA_SPACES: Dict[str, str] = {
    **dict(
        item.split("=", 1)
        for item in os.getenv("SUBAGENT_NEBULA_SPACE_MAP", "").split(",")
        if "=" in item
    ),
}

_kb_manager_local = threading.local()


def get_kb_manager() -> "KnowledgeBaseManager":
    """获取知识库管理器（事件循环本地实例）"""
    try:
        loop = asyncio.get_running_loop()
    except RuntimeError:
        loop = None

    if loop is None:
        manager = getattr(_kb_manager_local, "sync_manager", None)
        if manager is None:
            manager = KnowledgeBaseManager()
            _kb_manager_local.sync_manager = manager
        return manager

    loop_managers = getattr(_kb_manager_local, "loop_managers", None)
    if loop_managers is None:
        loop_managers = weakref.WeakKeyDictionary()
        _kb_manager_local.loop_managers = loop_managers

    manager = loop_managers.get(loop)
    if manager is None:
        manager = KnowledgeBaseManager()
        loop_managers[loop] = manager
    return manager


@dataclass
class RAGInstance:
    """RAG 实例包装"""
    kb_id: str
    kb_code: str
    rag: Any
    created_at: datetime = field(default_factory=datetime.now)
    last_used_at: datetime = field(default_factory=datetime.now)


class KnowledgeBaseManager:
    """
    知识库管理器
    
    负责：
    - 知识库的创建、删除、切换
    - RAG 实例的缓存和管理
    - 存储资源的协调
    
    Attributes:
        _rag_instances: RAG 实例缓存 {kb_id: RAGInstance}
        _current_kb_id: 当前知识库 ID
        _lock: 异步锁，保证并发安全
    """
    
    def __init__(self):
        self._rag_instances: Dict[str, RAGInstance] = {}
        self._rag_init_tasks: Dict[
            str,
            asyncio.Task[tuple[KnowledgeBase, Any]],
        ] = {}
        self._current_kb_id: Optional[str] = None
        self._lock = asyncio.Lock()
        self._keyword_extractor_lock = asyncio.Lock()
        self._max_cached_instances = int(os.getenv("KB_MAX_CACHED_INSTANCES", "10"))
        self._keyword_index_manager: KeywordIndexManager = get_keyword_index_manager()
        self._content_filter_manager: ContentFilterManager = get_content_filter_manager()
        self._keyword_extractors: Dict[str, PerKBKeywordExtractor] = {}
        self._building_kb_ids: set = set()
    
    async def create_knowledge_base(
        self,
        session: AsyncSession,
        kb_code: str,
        kb_name: str,
        subject: Optional[str] = None,
        description: Optional[str] = None,
        owner_id: Optional[str] = None,
        is_default: bool = False,
    ) -> KnowledgeBase:
        """
        创建知识库
        
        流程：
        1. 验证 kb_code 唯一性
        2. 创建 MySQL 记录（状态：creating）
        3. 创建 NebulaGraph Space
        4. 更新 MySQL 记录（状态：active）
        
        Args:
            session: 数据库会话
            kb_code: 知识库编码（唯一标识）
            kb_name: 知识库名称
            subject: 学科分类
            description: 描述
            owner_id: 所有者 ID
            is_default: 是否为默认知识库
            
        Returns:
            创建的知识库对象
            
        Raises:
            ValueError: kb_code 已存在
            RuntimeError: 创建存储资源失败
        """
        async with self._lock:
            crud = KnowledgeBaseCRUD()
            
            existing = await crud.get_by_code(session, kb_code)
            if existing:
                raise ValueError(f"知识库编码已存在: {kb_code}")
            
            kb = await crud.create(
                session=session,
                kb_code=kb_code,
                kb_name=kb_name,
                subject=subject,
                description=description,
                owner_id=owner_id,
                is_default=is_default,
            )
            
            try:
                await self._create_storage_resources(kb)
                await crud.update_status(session, kb.kb_id, KnowledgeBaseStatus.ACTIVE)
                kb.status = KnowledgeBaseStatus.ACTIVE
                
                logger.info(f"知识库创建成功: {kb.kb_id} ({kb.kb_code})")
                return kb
                
            except Exception as e:
                await crud.update_status(session, kb.kb_id, KnowledgeBaseStatus.INACTIVE)
                logger.error(f"知识库创建失败: {kb.kb_id}, {e}")
                raise RuntimeError(f"创建存储资源失败: {e}")
    
    async def _create_storage_resources(self, kb: KnowledgeBase) -> None:
        """
        创建存储资源
        
        Args:
            kb: 知识库对象
        """
        from backend.rag.storages import NebulaGraphStorage
        
        graph_workspace = self._resolve_graph_workspace(kb)
        nebula_storage = NebulaGraphStorage(
            namespace=kb.kb_code,
            workspace=graph_workspace,
        )
        try:
            await nebula_storage.initialize()
            logger.info(f"NebulaGraph Space 创建成功: {graph_workspace}")
        except Exception as e:
            logger.warning(f"NebulaGraph Space 创建警告: {e}")
        finally:
            await nebula_storage.finalize()
        
        logger.info(f"存储资源初始化完成: {kb.kb_code}")
    
    async def delete_knowledge_base(
        self,
        session: AsyncSession,
        kb_id: str,
    ) -> bool:
        """
        删除知识库及其所有数据
        
        流程：
        1. 更新 MySQL 记录（状态：deleting）
        2. 清理 Redis 数据
        3. 删除 Milvus Collection
        4. 删除 NebulaGraph Space
        5. 删除 MySQL 记录
        6. 清理缓存的 RAG 实例
        
        Args:
            session: 数据库会话
            kb_id: 知识库 ID
            
        Returns:
            是否删除成功
        """
        async with self._lock:
            crud = KnowledgeBaseCRUD()
            kb = await crud.get_by_id(session, kb_id)
            
            if not kb:
                logger.warning(f"知识库不存在: {kb_id}")
                return False
            
            if kb.is_default:
                raise ValueError("不能删除默认知识库")
            
            await crud.update_status(session, kb_id, KnowledgeBaseStatus.DELETING)
            
            try:
                await self._delete_storage_resources(kb)
                
                if kb_id in self._rag_instances:
                    rag_instance = self._rag_instances[kb_id]
                    try:
                        if hasattr(rag_instance.rag, 'finalize_storages'):
                            await rag_instance.rag.finalize_storages()
                    except Exception as e:
                        logger.warning(f"关闭 RAG 实例失败: {e}")
                    del self._rag_instances[kb_id]
                
                await crud.delete(session, kb_id)
                
                logger.info(f"知识库删除成功: {kb_id} ({kb.kb_code})")
                return True
                
            except Exception as e:
                logger.error(f"知识库删除失败: {kb_id}, {e}")
                await crud.update_status(session, kb_id, KnowledgeBaseStatus.INACTIVE)
                raise
    
    async def _delete_storage_resources(self, kb: KnowledgeBase) -> None:
        """
        删除存储资源
        
        Args:
            kb: 知识库对象
        """
        from backend.rag.storages import NebulaGraphStorage
        
        graph_workspace = self._resolve_graph_workspace(kb)
        nebula_storage = NebulaGraphStorage(
            namespace=kb.kb_code,
            workspace=graph_workspace,
        )
        try:
            await nebula_storage.initialize()
            await nebula_storage.drop()
            logger.info(f"NebulaGraph Space 删除成功: {graph_workspace}")
        except Exception as e:
            logger.warning(f"NebulaGraph Space 删除警告: {e}")
        finally:
            await nebula_storage.finalize()
        
        await self._clear_redis_data(kb.kb_code)
        await self._drop_milvus_collections(kb.kb_code)
        
        await self._keyword_index_manager.delete_index(kb.kb_code)
        logger.info(f"关键词索引删除完成: {kb.kb_code}")
        
        logger.info(f"存储资源清理完成: {kb.kb_code}")
    
    async def _clear_redis_data(self, kb_code: str) -> None:
        """清理 Redis 数据"""
        try:
            import redis
            if not settings.redis_host:
                return

            redis_url = f"redis://:{settings.redis_password}@{settings.redis_host}:{settings.redis_port}/{settings.redis_db}"

            def _clear_sync() -> int:
                client = redis.from_url(redis_url)
                patterns = [f"{kb_code}:*", f"{kb_code}_*"]
                deleted = 0
                for pattern in patterns:
                    keys = client.keys(pattern)
                    if keys:
                        client.delete(*keys)
                        deleted += len(keys)
                return deleted

            total_deleted = await asyncio.to_thread(_clear_sync)

            if total_deleted > 0:
                logger.info(f"Redis 数据清理完成: {kb_code}, 删除 {total_deleted} 个键")
        except Exception as e:
            logger.warning(f"Redis 数据清理失败: {e}")
    
    async def _drop_milvus_collections(self, kb_code: str) -> None:
        """删除 Milvus Collection"""
        try:
            from pymilvus import connections, utility

            milvus_uri = settings.milvus_connection_uri
            if not milvus_uri:
                return

            def _drop_sync() -> list[str]:
                dropped: list[str] = []
                connections.connect(uri=milvus_uri)
                try:
                    for suffix in ["entities", "relationships", "chunks"]:
                        collection_name = f"{kb_code}_{suffix}"
                        if utility.has_collection(collection_name):
                            utility.drop_collection(collection_name)
                            dropped.append(collection_name)
                finally:
                    connections.disconnect("default")
                return dropped

            dropped_collections = await asyncio.to_thread(_drop_sync)
            for name in dropped_collections:
                logger.info(f"Milvus Collection 删除: {name}")

            logger.info(f"Milvus Collection 清理完成: {kb_code}")
        except Exception as e:
            logger.warning(f"Milvus Collection 清理失败: {e}")
    
    async def get_knowledge_base(
        self,
        session: AsyncSession,
        kb_id: str,
    ) -> Optional[KnowledgeBase]:
        crud = KnowledgeBaseCRUD()
        return await crud.get_by_id(session, kb_id)
    
    async def get_knowledge_base_by_code(
        self,
        session: AsyncSession,
        kb_code: str,
    ) -> Optional[KnowledgeBase]:
        """根据编码获取知识库"""
        crud = KnowledgeBaseCRUD()
        return await crud.get_by_code(session, kb_code)
    
    async def get_default_knowledge_base(
        self,
        session: AsyncSession,
    ) -> Optional[KnowledgeBase]:
        """获取默认知识库"""
        crud = KnowledgeBaseCRUD()
        return await crud.get_default(session)
    
    async def list_knowledge_bases(
        self,
        session: AsyncSession,
        status: Optional[KnowledgeBaseStatus] = None,
        owner_id: Optional[str] = None,
        skip: int = 0,
        limit: int = 20,
    ) -> Tuple[List[KnowledgeBase], int]:
        """列出知识库"""
        crud = KnowledgeBaseCRUD()
        return await crud.list(
            session=session,
            status=status,
            owner_id=owner_id,
            skip=skip,
            limit=limit,
        )
    
    async def get_rag_instance(
        self,
        session: AsyncSession,
        kb_id: str,
    ) -> Any:
        """
        获取指定知识库的 RAG 实例
        
        使用缓存机制，避免重复创建。
        如果缓存已满，会清理最久未使用的实例。
        
        Args:
            session: 数据库会话
            kb_id: 知识库 ID
            
        Returns:
            RAG 实例
            
        Raises:
            ValueError: 知识库不存在或未激活
        """
        init_task: asyncio.Task[tuple[KnowledgeBase, Any]] | None = None
        # 使用 kb_id 作为唯一缓存键，让不同线程共享同一个 RAG 实例
        # RAG 实例的查询操作是线程安全的，不需要按线程隔离
        init_task_key = kb_id
        async with self._lock:
            if kb_id in self._rag_instances:
                instance = self._rag_instances[kb_id]
                instance.last_used_at = datetime.now()
                return instance.rag

            init_task = self._rag_init_tasks.get(init_task_key)
            if init_task is None:
                init_task = asyncio.create_task(self._build_rag_instance(session, kb_id))
                self._rag_init_tasks[init_task_key] = init_task

        try:
            kb, rag = await init_task
        except Exception:
            async with self._lock:
                if self._rag_init_tasks.get(init_task_key) is init_task:
                    self._rag_init_tasks.pop(init_task_key, None)
            raise

        async with self._lock:
            cached = self._rag_instances.get(kb_id)
            if cached is not None:
                cached.last_used_at = datetime.now()
                if self._rag_init_tasks.get(init_task_key) is init_task:
                    self._rag_init_tasks.pop(init_task_key, None)
                return cached.rag

            if len(self._rag_instances) >= self._max_cached_instances:
                await self._evict_lru_instance()

            self._rag_instances[kb_id] = RAGInstance(
                kb_id=kb_id,
                kb_code=kb.kb_code,
                rag=rag,
            )
            if self._rag_init_tasks.get(init_task_key) is init_task:
                self._rag_init_tasks.pop(init_task_key, None)

            logger.info(f"RAG 实例创建并缓存: {kb_id} ({kb.kb_code})")
            return rag

    async def _build_rag_instance(
        self,
        session: AsyncSession,
        kb_id: str,
    ) -> tuple[KnowledgeBase, Any]:
        current_thread_name = threading.current_thread().name
        if current_thread_name.startswith("subagent-exec-") and kb_id in _KNOWN_SUBAGENT_KB_CODES:
            # Subagent worker threads may run on event loops different from API startup.
            # Avoid async DB access here to prevent cross-loop aiomysql/session issues.
            kb = SimpleNamespace(
                kb_id=kb_id,
                kb_code=_KNOWN_SUBAGENT_KB_CODES[kb_id],
                nebula_space=_KNOWN_SUBAGENT_NEBULA_SPACES.get(
                    kb_id,
                    _KNOWN_SUBAGENT_KB_CODES[kb_id],
                ),
                status=KnowledgeBaseStatus.ACTIVE,
            )
            rag = await self._create_rag_instance(kb)
            return kb, rag

        kb = await self.get_knowledge_base(session, kb_id)
        if not kb:
            raise ValueError(f"知识库不存在: {kb_id}")

        if kb.status != KnowledgeBaseStatus.ACTIVE:
            raise ValueError(f"知识库未激活: {kb_id}, 状态: {kb.status}")

        rag = await self._create_rag_instance(kb)
        return kb, rag
    
    async def _create_rag_instance(self, kb: KnowledgeBase) -> Any:
        """
        创建 RAG 实例
        
        Args:
            kb: 知识库对象
            
        Returns:
            RAG 实例
        """
        from lightrag.utils import EmbeddingFunc
        from backend.rag.raganything.config import RAGAnythingConfig
        from backend.rag.raganything.raganything import RAGAnything
        from backend.rag.service import (
            _patch_lightrag_async_limiters_loop_local,
            _patch_lightrag_shared_storage_loop_locks,
            _patch_lightrag_milvus_delete,
            _patch_lightrag_milvus_index_compat,
            _patch_lightrag_milvus_query_for_diskann,
            _patch_lightrag_milvus_upsert_limits,
            _register_nebula_storage,
        )
        
        _register_nebula_storage()
        _patch_lightrag_async_limiters_loop_local()
        _patch_lightrag_shared_storage_loop_locks()
        
        from backend.rag.utils.lightrag_chunk_patch import apply_chunk_query_patch
        from backend.rag.utils.lightrag_no_truncation_patch import apply_no_truncation_patch
        apply_chunk_query_patch()
        apply_no_truncation_patch()
        
        rag_work_dir = os.getenv("RAG_WORK_DIR", "./rag_storage")
        
        embedding_config = settings.embedding_config
        embedding_api_base = embedding_config.get("api_base") or os.getenv("EMBEDDING_API_BASE")
        embedding_api_key = embedding_config.get("api_key") or os.getenv("EMBEDDING_API_KEY")
        embedding_model = embedding_config.get("model") or os.getenv("EMBEDDING_MODEL")
        embedding_dim = embedding_config.get("dim") or int(os.getenv("EMBEDDING_DIM", "4096"))
        
        embedding_func = None
        if embedding_api_base and embedding_model:
            embedding_func = EmbeddingFunc(
                embedding_dim=embedding_dim,
                max_token_size=8192,
                func=self._build_embedding_func(
                    api_base=embedding_api_base,
                    api_key=embedding_api_key,
                    model=embedding_model,
                ),
            )
        
        rag_ingestion = settings.rag_ingestion_config
        concurrency_config = rag_ingestion.get("concurrency", {})
        chunking_config = rag_ingestion.get("chunking", {})
        multimodal_config = rag_ingestion.get("multimodal", {})

        config = RAGAnythingConfig(
            working_dir=rag_work_dir,
            enable_image_processing=multimodal_config.get("enable_image_processing", True),
            enable_table_processing=multimodal_config.get("enable_table_processing", True),
            enable_equation_processing=multimodal_config.get("enable_equation_processing", False),
            max_concurrent_files=concurrency_config.get("max_concurrent_files", 5),
            max_concurrent_entity_batches=concurrency_config.get("max_concurrent_entity_batches", 5),
            max_concurrent_entity_nodes=concurrency_config.get("max_concurrent_entity_nodes", 5),
            max_concurrent_text_processing=concurrency_config.get("max_concurrent_text_processing", 20),
            max_concurrent_vision_processing=concurrency_config.get("max_concurrent_vision_processing", 40),
            entity_extraction_batch_size=concurrency_config.get("entity_extraction_batch_size", 5),
            llm_model_max_async=concurrency_config.get("llm_model_max_async", 15),
            use_semantic_chunking=chunking_config.get("use_semantic_chunking", True),
            semantic_chunking_threshold=chunking_config.get("semantic_chunking_threshold", 95),
            embedding_batch_size=chunking_config.get("embedding_batch_size", 200),
        )
        
        graph_workspace = self._resolve_graph_workspace(kb)
        lightrag_kwargs = {
            "workspace": kb.kb_code,
            "addon_params": self._build_lightrag_addon_params(graph_workspace),
            "kg_chunk_pick_method": "WEIGHT",  # 跳过向量相似度计算，由外部 PPR+Rerank 保证精度
        }
        logger.info(
            "RAG 存储映射: kb_code=%s, graph_workspace=%s",
            kb.kb_code,
            graph_workspace,
        )
        
        if settings.redis_host:
            redis_password = settings.redis_password or ""
            auth = f":{redis_password}@" if redis_password else ""
            redis_uri = f"redis://{auth}{settings.redis_host}:{settings.redis_port or 6379}/{settings.redis_db or 0}"
            os.environ["REDIS_URI"] = redis_uri
            lightrag_kwargs.update({
                "kv_storage": "RedisKVStorage",
                "doc_status_storage": "RedisDocStatusStorage",
            })
        
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
            lightrag_kwargs["vector_storage"] = "MilvusVectorDBStorage"
        
        lightrag_kwargs["graph_storage"] = "NebulaGraphStorage"
        
        llm_model_func = self._build_llm_model_func()
        
        rag = RAGAnything(
            config=config,
            llm_model_func=llm_model_func,
            embedding_func=embedding_func,
            lightrag_kwargs=lightrag_kwargs,
        )
        
        init_result = await rag.initialize()
        if not init_result.get("success"):
            error_msg = init_result.get("error", "Unknown error")
            logger.error(f"RAGAnything 初始化失败: {error_msg}")
            raise RuntimeError(f"RAGAnything 初始化失败: {error_msg}")
        
        return rag

    @staticmethod
    def _resolve_graph_workspace(kb: KnowledgeBase) -> str:
        """Use the configured Nebula space without changing Redis/Milvus workspace."""
        return str(getattr(kb, "nebula_space", None) or kb.kb_code).strip()

    @staticmethod
    def _build_lightrag_addon_params(graph_workspace: str) -> Dict[str, Any]:
        """Preserve LightRAG extraction settings while adding the graph override."""
        from lightrag.constants import DEFAULT_ENTITY_TYPES, DEFAULT_SUMMARY_LANGUAGE
        from lightrag.utils import get_env_value

        return {
            "language": get_env_value(
                "SUMMARY_LANGUAGE",
                DEFAULT_SUMMARY_LANGUAGE,
                str,
            ),
            "entity_types": get_env_value(
                "ENTITY_TYPES",
                DEFAULT_ENTITY_TYPES,
                list,
            ),
            "graph_workspace": graph_workspace,
        }
    
    def _build_embedding_func(self, api_base: str, api_key: str, model: str):
        """构建嵌入函数"""
        import httpx
        import numpy as np
        
        async def embed(texts: list[str]) -> np.ndarray:
            async with httpx.AsyncClient(timeout=60.0) as client:
                response = await client.post(
                    f"{api_base}/embeddings",
                    headers={"Authorization": f"Bearer {api_key}"},
                    json={"input": texts, "model": model},
                )
                response.raise_for_status()
                data = response.json()
                embeddings = [item["embedding"] for item in data["data"]]
                return np.array(embeddings)
        
        return embed
    
    def _build_llm_model_func(self):
        """构建 LLM 模型函数"""
        llm_config = settings.ingestion_llm_config
        rag_ingestion = settings.rag_ingestion_config
        api_base = llm_config.get("base_url", "")
        api_key = llm_config.get("api_key", "")
        model = llm_config.get("model", "")
        llm_timeout = float((rag_ingestion.get("timeout", {}) or {}).get("llm_timeout", 900))
        max_retries = int(os.getenv("RAG_INGESTION_LLM_MAX_RETRIES", "3") or "3")
        
        async def llm_func(prompt: str, **kwargs) -> str:
            import httpx
            
            system_prompt = kwargs.get("system_prompt", "")
            
            def is_json_serializable(v):
                try:
                    import json
                    json.dumps(v)
                    return True
                except (TypeError, ValueError):
                    return False
            
            filtered_kwargs = {
                k: v for k, v in kwargs.items()
                if is_json_serializable(v)
                and k not in ("llm_response_cache", "cache_type", "cache_keys_collector", "system_prompt")
                and not k.endswith("_collector")
                and not k.startswith("llm_")
            }
            
            messages = []
            if system_prompt:
                messages.append({"role": "system", "content": system_prompt})
            messages.append({"role": "user", "content": prompt})

            timeout = httpx.Timeout(llm_timeout)
            last_exception: Exception | None = None

            for attempt in range(max_retries):
                try:
                    async with httpx.AsyncClient(timeout=timeout) as client:
                        response = await client.post(
                            f"{api_base}/chat/completions",
                            headers={"Authorization": f"Bearer {api_key}"},
                            json={
                                "model": model,
                                "messages": messages,
                                **filtered_kwargs,
                            },
                        )
                        response.raise_for_status()
                        return response.json()["choices"][0]["message"]["content"]
                except httpx.ReadTimeout as e:
                    last_exception = e
                    if attempt < max_retries - 1:
                        wait_time = 2 ** attempt
                        logger.warning(
                            "[RAG Ingestion] LLM 调用超时，%ss 后重试 (%s/%s): %s",
                            wait_time,
                            attempt + 1,
                            max_retries,
                            e,
                        )
                        await asyncio.sleep(wait_time)
                        continue
                    logger.error("[RAG Ingestion] LLM 调用超时，已达到最大重试次数: %s", e)
                    raise
                except (httpx.ConnectTimeout, httpx.ConnectError, httpx.RemoteProtocolError) as e:
                    last_exception = e
                    if attempt < max_retries - 1:
                        wait_time = 2 ** attempt
                        logger.warning(
                            "[RAG Ingestion] LLM 网络异常，%ss 后重试 (%s/%s): %s",
                            wait_time,
                            attempt + 1,
                            max_retries,
                            e,
                        )
                        await asyncio.sleep(wait_time)
                        continue
                    logger.error("[RAG Ingestion] LLM 网络异常，已达到最大重试次数: %s", e)
                    raise

            raise last_exception or RuntimeError("RAG 入库 LLM 调用失败")
        
        return llm_func
    
    async def _evict_lru_instance(self) -> None:
        """清理最久未使用的 RAG 实例"""
        if not self._rag_instances:
            return
        
        lru_kb_id = min(
            self._rag_instances.keys(),
            key=lambda k: self._rag_instances[k].last_used_at,
        )
        
        instance = self._rag_instances[lru_kb_id]
        try:
            if hasattr(instance.rag, 'finalize_storages'):
                await instance.rag.finalize_storages()
            logger.info(f"RAG 实例清理: {lru_kb_id}")
        except Exception as e:
            logger.warning(f"RAG 实例清理失败: {e}")
        
        del self._rag_instances[lru_kb_id]
    
    async def switch_knowledge_base(
        self,
        session: AsyncSession,
        kb_id: str,
    ) -> KnowledgeBase:
        """
        切换当前知识库
        
        Args:
            session: 数据库会话
            kb_id: 目标知识库 ID
            
        Returns:
            切换后的知识库对象
        """
        kb = await self.get_knowledge_base(session, kb_id)
        if not kb:
            raise ValueError(f"知识库不存在: {kb_id}")
        
        if kb.status != KnowledgeBaseStatus.ACTIVE:
            raise ValueError(f"知识库未激活: {kb_id}")
        
        self._current_kb_id = kb_id
        logger.info(f"切换知识库: {kb_id} ({kb.kb_code})")
        return kb
    
    def get_current_knowledge_base_id(self) -> Optional[str]:
        """获取当前知识库 ID"""
        return self._current_kb_id
    
    async def update_knowledge_base_stats(
        self,
        session: AsyncSession,
        kb_id: str,
    ) -> KnowledgeBase:
        """
        更新知识库统计信息
        
        Args:
            session: 数据库会话
            kb_id: 知识库 ID
            
        Returns:
            更新后的知识库对象
        """
        crud = KnowledgeBaseCRUD()
        doc_crud = KnowledgeBaseDocumentCRUD()
        
        docs, doc_count = await doc_crud.list_by_kb(session, kb_id, limit=10000)
        
        total_entities = sum(d.entity_count for d in docs)
        total_relations = sum(d.relation_count for d in docs)
        total_chunks = sum(d.chunk_count for d in docs)
        
        kb = await crud.update_stats(
            session=session,
            kb_id=kb_id,
            doc_count=doc_count,
            entity_count=total_entities,
            relation_count=total_relations,
            chunk_count=total_chunks,
        )
        
        return kb
    
    async def add_document(
        self,
        session: AsyncSession,
        kb_id: str,
        doc_id: str,
        file_name: str,
        file_path: Optional[str] = None,
        file_size: Optional[int] = None,
        document_date: Optional[date] = None,
        category_path: Optional[str] = None,
        chunk_count: int = 0,
        entity_count: int = 0,
        relation_count: int = 0,
    ) -> KnowledgeBaseDocument:
        """添加文档到知识库（如果已存在则更新），并自动同步目录结构"""
        # 自动创建目录记录 (如果路径提供且不为空)
        if category_path:
            from backend.services.directory_service import directory_service
            try:
                await directory_service.create_directory(
                    session=session,
                    kb_id=kb_id,
                    category_path=category_path
                )
            except Exception as e:
                logger.warning(f"自动创建目录失败: {category_path}, error={e}")

        doc_crud = KnowledgeBaseDocumentCRUD()
        return await doc_crud.upsert(
            session=session,
            kb_id=kb_id,
            doc_id=doc_id,
            file_name=file_name,
            file_path=file_path,
            file_size=file_size,
            document_date=document_date,
            category_path=category_path,
            chunk_count=chunk_count,
            entity_count=entity_count,
            relation_count=relation_count,
        )
    
    async def list_documents(
        self,
        session: AsyncSession,
        kb_id: str,
        parse_status: Optional[int] = None,
        storage_status: Optional[int] = None,
        skip: int = 0,
        limit: int = 20,
    ) -> Tuple[List[KnowledgeBaseDocument], int]:
        """列出知识库文档"""
        doc_crud = KnowledgeBaseDocumentCRUD()
        return await doc_crud.list_by_kb(
            session=session,
            kb_id=kb_id,
            parse_status=parse_status,
            storage_status=storage_status,
            skip=skip,
            limit=limit,
        )
    
    async def update_document_status(
        self,
        session: AsyncSession,
        kb_id: str,
        doc_id: str,
        parse_status: Optional[int] = None,
        storage_status: Optional[int] = None,
        error_message: Optional[str] = None,
    ) -> Optional[KnowledgeBaseDocument]:
        """更新文档状态"""
        doc_crud = KnowledgeBaseDocumentCRUD()
        return await doc_crud.update_status(
            session=session,
            kb_id=kb_id,
            doc_id=doc_id,
            parse_status=parse_status,
            storage_status=storage_status,
            error_message=error_message,
        )
    
    async def remove_document(
        self,
        session: AsyncSession,
        kb_id: str,
        doc_id: str,
    ) -> bool:
        """从知识库移除文档"""
        doc_crud = KnowledgeBaseDocumentCRUD()
        return await doc_crud.delete(session, kb_id, doc_id)
    
    async def get_keyword_extractor(
        self,
        session: AsyncSession,
        kb_id: str,
        embedding_func: Optional[Callable] = None,
    ) -> PerKBKeywordExtractor:
        """
        获取指定知识库的关键词提取器（带缓存）
        
        Args:
            session: 数据库会话
            kb_id: 知识库 ID
            embedding_func: 嵌入函数（可选）
            
        Returns:
            关键词提取器实例
        """
        def _hydrate_extractor_embedding(extractor: PerKBKeywordExtractor) -> None:
            """在缓存命中时补齐 embedding_func，避免预热阶段创建的实例长期缺失向量能力。"""
            if embedding_func is None:
                return

            if extractor.embedding_func is None:
                extractor.embedding_func = embedding_func
                self._keyword_index_manager.set_embedding_func(embedding_func)
                logger.info(f"[关键词提取器] 已为缓存实例补齐 embedding_func: kb_id={kb_id}")

        if kb_id in self._keyword_extractors:
            logger.debug(f"[关键词提取器] 缓存命中: kb_id={kb_id}")
            extractor = self._keyword_extractors[kb_id]
            _hydrate_extractor_embedding(extractor)
            return extractor

        # 并发保护：避免同一 kb 在高并发下重复创建提取器，导致后续重复建图
        async with self._keyword_extractor_lock:
            if kb_id in self._keyword_extractors:
                logger.debug(f"[关键词提取器] 缓存命中(锁内): kb_id={kb_id}")
                extractor = self._keyword_extractors[kb_id]
                _hydrate_extractor_embedding(extractor)
                return extractor
        
            kb = await self.get_knowledge_base(session, kb_id)
            if not kb:
                raise ValueError(f"知识库不存在: {kb_id}")

            if kb.status != KnowledgeBaseStatus.ACTIVE:
                raise ValueError(f"知识库未激活: {kb_id}")

            if embedding_func:
                self._keyword_index_manager.set_embedding_func(embedding_func)

            index = await self._keyword_index_manager.get_index(kb.kb_code, kb.kb_id)

            need_build = False
            if not index.entity_names:
                logger.info(f"[关键词索引] 索引未初始化: {kb.kb_code}")
                need_build = True
            elif embedding_func and not await index.has_vector_index_async():
                if kb_id in self._building_kb_ids:
                    logger.info(f"[关键词索引] 索引正在构建中，跳过: {kb.kb_code}")
                else:
                    logger.info(f"[关键词索引] 向量索引不存在，异步后台构建: {kb.kb_code}")
                    asyncio.create_task(self._build_keyword_index_async(kb_id, embedding_func))

            if need_build:
                if kb_id in self._building_kb_ids:
                    logger.info(f"[关键词索引] 索引正在构建中，返回临时提取器: {kb.kb_code}")
                else:
                    logger.info(f"[关键词索引] 索引未初始化，异步后台构建: {kb.kb_code}")
                    asyncio.create_task(self._build_keyword_index_async(kb_id, embedding_func))

            extractor = PerKBKeywordExtractor(
                kb_code=kb.kb_code,
                kb_id=kb.kb_id,
                index_manager=self._keyword_index_manager,
                embedding_func=embedding_func,
            )

            self._keyword_extractors[kb_id] = extractor
            logger.info(f"[关键词提取器] 缓存创建: kb_id={kb_id}, 当前缓存数={len(self._keyword_extractors)}")

            return extractor
    
    async def get_keyword_index_manager(self) -> KeywordIndexManager:
        """获取关键词索引管理器"""
        return self._keyword_index_manager
    
    async def get_content_filter(
        self,
        session: AsyncSession,
        kb_id: str,
        embedding_func: Optional[Callable] = None,
    ):
        """
        获取指定知识库的内容过滤器
        
        Args:
            session: 数据库会话
            kb_id: 知识库 ID
            embedding_func: 嵌入函数（可选）
            
        Returns:
            内容过滤器实例
        """
        kb = await self.get_knowledge_base(session, kb_id)
        if not kb:
            raise ValueError(f"知识库不存在: {kb_id}")
        
        if kb.status != KnowledgeBaseStatus.ACTIVE:
            raise ValueError(f"知识库未激活: {kb_id}")
        
        return await self._content_filter_manager.get_filter(
            kb_code=kb.kb_code,
            kb_id=kb.kb_id,
            embedding_func=embedding_func,
        )
    
    async def filter_content(
        self,
        session: AsyncSession,
        kb_id: str,
        raw_data: RawRetrievalData,
        hl_keywords: List[str],
        query: str,
        mode: FilterMode = FilterMode.FULL,
        query_type: Optional[QueryType] = None,
        seed_weights: Optional[Dict[str, float]] = None,
    ) -> FilterResult:
        """
        过滤内容
        
        Args:
            session: 数据库会话
            kb_id: 知识库 ID
            raw_data: 原始检索数据
            hl_keywords: 高级关键词
            query: 查询文本
            mode: 过滤模式
            
        Returns:
            过滤结果
        """
        content_filter = await self.get_content_filter(session, kb_id)
        return await content_filter.filter_content(
            raw_data=raw_data,
            hl_keywords=hl_keywords,
            query=query,
            mode=mode,
            query_type=query_type,
            seed_weights=seed_weights,
        )
    
    async def build_keyword_index(
        self,
        session: AsyncSession,
        kb_id: str,
        embedding_func: Optional[Callable] = None,
    ) -> bool:
        """
        构建指定知识库的关键词索引（流水线模式：获取和嵌入同时进行）
        
        Args:
            session: 数据库会话
            kb_id: 知识库 ID
            embedding_func: 嵌入函数（可选，用于向量索引）
            
        Returns:
            是否构建成功
        """
        
        kb = await self.get_knowledge_base(session, kb_id)
        if not kb:
            raise ValueError(f"知识库不存在: {kb_id}")
        
        if embedding_func:
            self._keyword_index_manager.set_embedding_func(embedding_func)
        
        try:
            result = await self._build_index_pipeline(kb, embedding_func)
            logger.info(f"[关键词索引] 构建结果: status={result.status}, entity_count={result.entity_count}, vector_count={result.vector_count}")
            return result.status == "success"
            
        except Exception as e:
            logger.error(f"构建关键词索引失败: {kb_id}, {e}")
            return False
    
    async def _build_index_pipeline(
        self,
        kb: KnowledgeBase,
        embedding_func: Optional[Callable] = None,
    ) -> "IndexBuildResult":
        """流水线构建索引：获取实体和嵌入插入同时进行"""
        import time
        import httpx
        import hashlib
        import asyncio
        from backend.rag.keywords.models import IndexBuildResult
        from backend.rag.keywords.per_kb_index import PerKBKeywordIndex, get_keyword_collection_name
        from backend.rag.keywords.bm25_index import BM25Index
        
        start_time = time.time()
        result = IndexBuildResult()
        
        entity_queue = asyncio.Queue(maxsize=100)
        all_entities = []
        inserted_count = 0
        total_count = 0
        stop_signal = object()
        
        embedding_api_base = settings.embedding_config.get("api_base") or ""
        embedding_api_key = settings.embedding_config.get("api_key") or ""
        embedding_model = settings.embedding_config.get("model") or "text-embedding-v3"
        
        index = PerKBKeywordIndex(
            kb_code=kb.kb_code,
            kb_id=kb.kb_id,
            _embedding_dim=settings.embedding_dim or 4096,
        )
        
        async def fetch_producer():
            nonlocal total_count
            try:
                if kb.nebula_space:
                    await self._fetch_entities_streaming(kb, entity_queue, stop_signal)
                else:
                    entities = await self._fetch_entities_from_milvus(kb)
                    for entity in entities:
                        await entity_queue.put(entity)
                    total_count = len(entities)
                    await entity_queue.put(stop_signal)
            except Exception as e:
                logger.error(f"[流水线] 获取实体失败: {e}")
                await entity_queue.put(stop_signal)
        
        async def embed_consumer():
            
            index._connect_milvus()
            get_keyword_collection_name(kb.kb_code)
            
            collection = await index._create_milvus_collection()
            
            batch_size = max(1, settings.keyword_index_insert_batch_size)
            max_concurrent = max(1, settings.keyword_index_insert_max_concurrency)
            semaphore = asyncio.Semaphore(max_concurrent)
            batch_entities = []
            pending_tasks = []
            
            async def process_batch(entities_batch):
                nonlocal inserted_count
                async with semaphore:
                    entity_names = [e.entity_name for e in entities_batch]
                    try:
                        if inserted_count == 0 and len(entity_names) > 0:
                            logger.info(f"[流水线] 首批实体名称样本: {entity_names[:3]}")
                        
                        async with httpx.AsyncClient(timeout=120.0) as client:
                            if embedding_api_base and embedding_api_key:
                                response = await client.post(
                                    f"{embedding_api_base}/embeddings",
                                    headers={"Authorization": f"Bearer {embedding_api_key}"},
                                    json={"input": entity_names, "model": embedding_model},
                                )
                                response.raise_for_status()
                                data = response.json()
                                embeddings = [item["embedding"] for item in data["data"]]
                            else:
                                return 0
                        
                        ids = [hashlib.md5(name.encode()).hexdigest() for name in entity_names]
                        batch_data = [ids, entity_names, embeddings]
                        await asyncio.to_thread(collection.insert, batch_data)
                        
                        inserted_count += len(ids)
                        if inserted_count % 10000 == 0:
                            logger.info(f"[流水线] 已插入 {inserted_count} 条向量记录")
                        return len(ids)
                    except httpx.HTTPStatusError as e:
                        logger.warning(f"[流水线] 批次处理失败 HTTP {e.response.status_code}: {e.response.text[:200]}")
                        return 0
                    except Exception as e:
                        logger.warning(f"[流水线] 批次处理失败: {type(e).__name__}: {e}")
                        return 0
            
            while True:
                item = await entity_queue.get()
                
                if item is stop_signal:
                    if batch_entities:
                        task = asyncio.create_task(process_batch(batch_entities))
                        pending_tasks.append(task)
                    break
                
                all_entities.append(item)
                batch_entities.append(item)
                
                if len(batch_entities) >= batch_size:
                    task = asyncio.create_task(process_batch(batch_entities))
                    pending_tasks.append(task)
                    batch_entities = []
                    
                    if len(all_entities) % 50000 == 0:
                        logger.info(f"[流水线] 已获取 {len(all_entities)} 个实体, 已插入 {inserted_count} 条向量")
            
            if pending_tasks:
                await asyncio.gather(*pending_tasks)
            
            await asyncio.to_thread(collection.flush)
            logger.info(f"[流水线] 向量插入完成: {inserted_count} 条")
        
        await asyncio.gather(fetch_producer(), embed_consumer())
        
        if all_entities:
            index.entity_names = [e.entity_name for e in all_entities]
            for e in all_entities:
                index.entity_metadata[e.entity_name] = {
                    "entity_type": e.entity_type,
                    "description": e.description,
                    "source_id": e.source_id,
                    "file_path": e.file_path,
                }
            
            index.bm25_index = BM25Index()
            index.bm25_index.build(index.entity_names)
            index.entity_count = len(all_entities)
            index.last_updated = datetime.now()
            index._is_initialized = True
            
            await index._save_to_redis()
            
            self._keyword_index_manager._indices[kb.kb_code] = index
        
        result.entity_count = len(all_entities)
        result.vector_count = inserted_count
        result.status = "success"
        result.build_time_ms = (time.time() - start_time) * 1000
        
        logger.info(f"[流水线] 构建完成: 实体数={result.entity_count}, 向量数={result.vector_count}, 耗时={result.build_time_ms:.1f}ms")
        
        return result
    
    async def _fetch_entities_streaming(
        self,
        kb: KnowledgeBase,
        queue: asyncio.Queue,
        stop_signal: object,
    ) -> None:
        """流式获取实体（生产者）- 在线程池中运行同步 NebulaGraph 操作"""
        from backend.rag.keywords.models import EntityData
        from nebula3.gclient.net import ConnectionPool
        from nebula3.Config import Config
        
        def get_prop(props, key):
            if key in props:
                prop_val = props[key]
                if hasattr(prop_val, 'is_string') and prop_val.is_string():
                    return prop_val.as_string()
                elif hasattr(prop_val, 'is_int') and prop_val.is_int():
                    return str(prop_val.as_int())
                elif hasattr(prop_val, 'is_double') and prop_val.is_double():
                    return str(prop_val.as_double())
            return ""
        
        def fetch_sync():
            entities_batch = []
            total_fetched = 0
            
            try:
                config = Config()
                config.max_connection_pool_size = 10
                
                pool = ConnectionPool()
                host = os.getenv("NEBULA_HOST", "")
                port = int(os.getenv("NEBULA_PORT", "9669"))
                user = os.getenv("NEBULA_USER", "")
                password = os.getenv("NEBULA_PASSWORD", "")
                
                pool.init([(host, port)], config)
                
                session = pool.get_session(user, password)
                try:
                    result = session.execute(f"USE `{kb.nebula_space}`")
                    if not result.is_succeeded():
                        logger.warning(f"NebulaGraph Space 不存在: {kb.nebula_space}")
                        return entities_batch
                    
                    offset = 0
                    batch_size = 10000
                    
                    while True:
                        nGQL = f"MATCH (v:entity) RETURN v SKIP {offset} LIMIT {batch_size}"
                        result = session.execute(nGQL)
                        
                        if not result.is_succeeded():
                            logger.warning(f"查询失败: {result.error_msg()}")
                            break
                        
                        row_count = result.row_size()
                        if row_count == 0:
                            break
                        
                        for i in range(row_count):
                            row = result.row_values(i)
                            val = row[0]
                            
                            if val.is_vertex():
                                vertex = val.as_node()
                                props = vertex.properties()
                                
                                entity_id = get_prop(props, "entity_id")
                                entity_type = get_prop(props, "entity_type")
                                description = get_prop(props, "description")
                                source_id = get_prop(props, "source_id")
                                file_path = get_prop(props, "file_path")
                                
                                if entity_id:
                                    entity = EntityData(
                                        entity_name=entity_id,
                                        entity_type=entity_type or "unknown",
                                        description=description[:500] if description else "",
                                        source_id=source_id,
                                        file_path=file_path,
                                    )
                                    entities_batch.append(entity)
                        
                        total_fetched += row_count
                        offset += batch_size
                        
                        if row_count < batch_size:
                            break
                        
                        if total_fetched % 50000 == 0:
                            logger.info(f"[流水线] 已获取 {total_fetched} 个实体...")
                    
                    logger.info(f"[流水线] 获取完成: {total_fetched} 个实体")
                    
                finally:
                    session.release()
                    pool.close()
                    
            except Exception as e:
                logger.warning(f"[流水线] 获取实体失败: {kb.kb_code}, {e}")
            
            return entities_batch
        
        loop = asyncio.get_event_loop()
        entities = await loop.run_in_executor(None, fetch_sync)
        
        for entity in entities:
            await queue.put(entity)
        
        await queue.put(stop_signal)
    
    async def _build_keyword_index_async(
        self,
        kb_id: str,
        embedding_func: Optional[Callable] = None,
    ) -> None:
        """
        异步后台构建关键词索引（不阻塞主请求）
        
        使用独立线程运行，避免阻塞事件循环
        """
        import threading
        
        if kb_id in self._building_kb_ids:
            logger.info(f"[关键词索引] 已在构建中，跳过: kb_id={kb_id}")
            return
        
        self._building_kb_ids.add(kb_id)
        
        def run_in_thread():
            try:
                new_loop = asyncio.new_event_loop()
                asyncio.set_event_loop(new_loop)
                try:
                    new_loop.run_until_complete(self._build_keyword_index_task(kb_id, embedding_func))
                    logger.info(f"[关键词索引] 后台构建完成: kb_id={kb_id}")
                    if kb_id in self._keyword_extractors:
                        del self._keyword_extractors[kb_id]
                        logger.info(f"[关键词索引] 已清除提取器缓存，下次查询将使用新索引: kb_id={kb_id}")
                finally:
                    new_loop.run_until_complete(new_loop.shutdown_asyncgens())
                    new_loop.close()
            except Exception as e:
                logger.error(f"[关键词索引] 后台构建线程失败: kb_id={kb_id}, error={e}")
            finally:
                self._building_kb_ids.discard(kb_id)
        
        logger.info(f"[关键词索引] 开始后台构建: kb_id={kb_id}")
        
        thread = threading.Thread(target=run_in_thread, daemon=True)
        thread.start()
    
    async def _build_keyword_index_task(
        self,
        kb_id: str,
        embedding_func: Optional[Callable] = None,
    ) -> None:
        """后台构建任务（使用独立数据库连接）"""
        from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker, create_async_engine
        from backend.config.settings import settings
        
        engine = create_async_engine(
            settings.async_mysql_url,
            pool_size=5,
            pool_pre_ping=True,
            echo=False,
        )
        async_session_factory = async_sessionmaker(
            bind=engine,
            class_=AsyncSession,
            expire_on_commit=False,
        )
        
        try:
            async with async_session_factory() as session:
                await self.build_keyword_index(session, kb_id, embedding_func)
        finally:
            await engine.dispose()
    
    async def _fetch_entities_from_kb(self, kb: KnowledgeBase) -> List[Any]:
        """从知识库存储中获取实体数据"""
        
        entities = []
        
        try:
            if kb.nebula_space:
                entities = await self._fetch_entities_from_nebula(kb)
            else:
                entities = await self._fetch_entities_from_milvus(kb)
        except Exception as e:
            logger.warning(f"获取实体失败: {kb.kb_code}, {e}")
        
        return entities
    
    async def _fetch_entities_from_nebula(self, kb: KnowledgeBase) -> List[Any]:
        """从 NebulaGraph 获取实体数据"""
        from backend.rag.keywords.models import EntityData
        from nebula3.gclient.net import ConnectionPool
        from nebula3.Config import Config
        
        entities = []
        
        def get_prop(props, key):
            if key in props:
                prop_val = props[key]
                if hasattr(prop_val, 'is_string') and prop_val.is_string():
                    return prop_val.as_string()
                elif hasattr(prop_val, 'is_int') and prop_val.is_int():
                    return str(prop_val.as_int())
                elif hasattr(prop_val, 'is_double') and prop_val.is_double():
                    return str(prop_val.as_double())
            return ""
        
        try:
            config = Config()
            config.max_connection_pool_size = 10
            
            pool = ConnectionPool()
            host = os.getenv("NEBULA_HOST", "")
            port = int(os.getenv("NEBULA_PORT", "9669"))
            user = os.getenv("NEBULA_USER", "")
            password = os.getenv("NEBULA_PASSWORD", "")
            
            pool.init([(host, port)], config)
            
            session = pool.get_session(user, password)
            try:
                result = session.execute(f"USE `{kb.nebula_space}`")
                if not result.is_succeeded():
                    logger.warning(f"NebulaGraph Space 不存在: {kb.nebula_space}")
                    return entities
                
                offset = 0
                batch_size = 10000
                total_fetched = 0
                
                while True:
                    nGQL = f"MATCH (v:entity) RETURN v SKIP {offset} LIMIT {batch_size}"
                    result = session.execute(nGQL)
                    
                    if not result.is_succeeded():
                        logger.warning(f"查询失败: {result.error_msg()}")
                        break
                    
                    row_count = result.row_size()
                    if row_count == 0:
                        break
                    
                    for i in range(row_count):
                        row = result.row_values(i)
                        val = row[0]
                        
                        if val.is_vertex():
                            vertex = val.as_node()
                            props = vertex.properties()
                            
                            entity_id = get_prop(props, "entity_id")
                            entity_type = get_prop(props, "entity_type")
                            description = get_prop(props, "description")
                            source_id = get_prop(props, "source_id")
                            file_path = get_prop(props, "file_path")
                            
                            if entity_id:
                                entity = EntityData(
                                    entity_name=entity_id,
                                    entity_type=entity_type or "unknown",
                                    description=description[:500] if description else "",
                                    source_id=source_id,
                                    file_path=file_path,
                                )
                                entities.append(entity)
                    
                    total_fetched += row_count
                    offset += batch_size
                    
                    if row_count < batch_size:
                        break
                    
                    if total_fetched % 50000 == 0:
                        logger.info(f"已获取 {total_fetched} 个实体...")
                
                logger.info(f"从 NebulaGraph 获取 {len(entities)} 个实体: {kb.kb_code}")
                
            finally:
                session.release()
                pool.close()
                
        except Exception as e:
            logger.warning(f"从 NebulaGraph 获取实体失败: {kb.kb_code}, {e}")
        
        return entities
    
    async def _fetch_entities_from_milvus(self, kb: KnowledgeBase) -> List[Any]:
        """从 Milvus 获取实体数据"""
        from backend.rag.keywords.models import EntityData
        
        entities = []
        
        try:
            from pymilvus import Collection, connections, utility
            
            milvus_uri = settings.milvus_connection_uri
            if not milvus_uri:
                return entities
            
            conn_alias = f"kb_entity_{kb.kb_code}"
            if not connections.has_connection(conn_alias):
                connections.connect(uri=milvus_uri, alias=conn_alias)
            
            collection_name = f"{kb.kb_code}_entities"
            if not utility.has_collection(collection_name, using=conn_alias):
                return entities
            
            collection = Collection(collection_name, using=conn_alias)
            collection.load()
            
            iterator = collection.query_iterator(
                expr="",
                output_fields=["entity_name", "entity_type", "description", "source_id", "file_path"],
                batch_size=10000,
            )
            
            while True:
                results = iterator.next()
                if not results:
                    break
                
                for item in results:
                    entity = EntityData(
                        entity_name=item.get("entity_name", ""),
                        entity_type=item.get("entity_type", ""),
                        description=item.get("description", "")[:500],
                        source_id=item.get("source_id", ""),
                        file_path=item.get("file_path", ""),
                    )
                    if entity.entity_name:
                        entities.append(entity)
            
            iterator.close()
            logger.info(f"从 Milvus 获取 {len(entities)} 个实体: {kb.kb_code}")
            
        except Exception as e:
            logger.warning(f"从 Milvus 获取实体失败: {kb.kb_code}, {e}")
        
        return entities

    async def initialize_keyword_components(
        self,
        session: AsyncSession,
        kb_id: str,
        force_rebuild: bool = True,
        init_bm25: bool = True,
        warmup_low_level: bool = True,
        progress_callback: Optional[Callable[[str, str, Dict[str, Any]], Any]] = None,
    ) -> Dict[str, Any]:
        """初始化关键词能力（仅 BM25 + 低级关键词图，不进行高级向量构建）。"""

        async def _notify(phase: str, message: str, extra: Optional[Dict[str, Any]] = None) -> None:
            if progress_callback is None:
                return
            payload = extra or {}
            ret = progress_callback(phase, message, payload)
            if inspect.isawaitable(ret):
                await ret

        kb = await self.get_knowledge_base(session, kb_id)
        if not kb:
            raise ValueError(f"知识库不存在: {kb_id}")

        await _notify("clearing", "开始清理旧关键词缓存", {"kb_id": kb_id, "kb_code": kb.kb_code})

        # 避免使用旧 extractor 和旧图缓存。
        self._keyword_extractors.pop(kb_id, None)
        PerKBKeywordExtractor._global_graph_cache.pop(kb_id, None)

        index = await self._keyword_index_manager.get_index(kb.kb_code, kb.kb_id)
        if force_rebuild:
            await index.clear()

        await _notify("loading_entities", "开始加载实体数据", {})
        entities = await self._fetch_entities_from_kb(kb)

        unique_entities = []
        seen_names = set()
        for entity in entities:
            if not entity.entity_name or entity.entity_name in seen_names:
                continue
            seen_names.add(entity.entity_name)
            unique_entities.append(entity)

        index.entity_names = [e.entity_name for e in unique_entities]
        index.entity_metadata = {
            e.entity_name: {
                "entity_type": e.entity_type,
                "description": e.description,
                "source_id": e.source_id,
                "file_path": e.file_path,
            }
            for e in unique_entities
        }
        index.entity_count = len(index.entity_names)
        index.last_updated = datetime.now()
        index._is_initialized = True

        bm25_doc_count = 0
        bm25_term_count = 0
        if init_bm25:
            await _notify("building_bm25", "开始重建 BM25 索引", {"entity_count": index.entity_count})
            index.bm25_index = BM25Index()
            index.bm25_index.build(index.entity_names)
            bm25_doc_count = index.bm25_index.doc_count
            bm25_term_count = len(index.bm25_index.term_doc_freq)

        await index._save_to_redis()

        graph_nodes = 0
        graph_edges = 0
        if warmup_low_level:
            await _notify("warming_low_level_graph", "开始预热低级关键词图", {})
            extractor = PerKBKeywordExtractor(
                kb_code=kb.kb_code,
                kb_id=kb.kb_id,
                index_manager=self._keyword_index_manager,
                embedding_func=None,
            )
            self._keyword_extractors[kb_id] = extractor
            await extractor._build_full_graph()
            if extractor._graph is not None:
                graph_nodes = extractor._graph.number_of_nodes()
                graph_edges = extractor._graph.number_of_edges()

        result = {
            "kb_id": kb.kb_id,
            "kb_code": kb.kb_code,
            "entity_count": index.entity_count,
            "bm25_doc_count": bm25_doc_count,
            "bm25_term_count": bm25_term_count,
            "graph_nodes": graph_nodes,
            "graph_edges": graph_edges,
            "force_rebuild": force_rebuild,
            "init_bm25": init_bm25,
            "warmup_low_level": warmup_low_level,
        }

        await _notify("done", "关键词能力初始化完成", result)
        return result
