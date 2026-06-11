"""
关键词索引管理器

管理多个知识库的索引实例，支持 LRU 缓存和自动清理
"""

import asyncio
import os
import threading
import time
import weakref
from datetime import datetime
from typing import Callable, Dict, List, Optional

from backend.config.loggers import get_logger
from backend.config.settings import settings
from backend.rag.keywords.models import EntityData, IndexBuildResult, KeywordIndexStats
from backend.rag.keywords.per_kb_index import PerKBKeywordIndex

logger = get_logger(__name__)

_index_manager_local = threading.local()


def get_keyword_index_manager() -> "KeywordIndexManager":
    """获取关键词索引管理器（事件循环本地实例）"""
    try:
        loop = asyncio.get_running_loop()
    except RuntimeError:
        loop = None

    if loop is None:
        manager = getattr(_index_manager_local, "sync_manager", None)
        if manager is None:
            manager = KeywordIndexManager()
            _index_manager_local.sync_manager = manager
        return manager

    loop_managers = getattr(_index_manager_local, "loop_managers", None)
    if loop_managers is None:
        loop_managers = weakref.WeakKeyDictionary()
        _index_manager_local.loop_managers = loop_managers

    manager = loop_managers.get(loop)
    if manager is None:
        manager = KeywordIndexManager()
        loop_managers[loop] = manager
    return manager


class KeywordIndexManager:
    """
    关键词索引管理器
    
    职责：
    - 管理多个知识库的索引实例
    - LRU 缓存机制
    - 索引生命周期管理
    """
    
    def __init__(self, max_cached_kb: int = 5):
        self._indices: Dict[str, PerKBKeywordIndex] = {}
        self._init_tasks: Dict[str, asyncio.Task[PerKBKeywordIndex]] = {}
        self._init_task_started_at: Dict[str, float] = {}
        self._max_cached = max_cached_kb
        self._lock = asyncio.Lock()
        self._embedding_func: Optional[Callable] = None
        self._embedding_dim: int = settings.embedding_dim or 4096
        self._init_timeout_seconds = int(os.getenv("KEYWORD_INDEX_INIT_TIMEOUT_SECONDS", "180"))
    
    def set_embedding_func(self, func: Callable, dim: int = 4096) -> None:
        """设置嵌入函数"""
        self._embedding_func = func
        self._embedding_dim = dim
    
    async def get_index(
        self,
        kb_code: str,
        kb_id: str = "",
    ) -> PerKBKeywordIndex:
        """
        获取指定知识库的索引（带缓存）
        
        如果索引不存在，创建新的索引实例并初始化。
        如果缓存已满，清理最久未使用的索引。
        """
        while True:
            async with self._lock:
                cached = self._indices.get(kb_code)
                if cached is not None:
                    cached.last_accessed = datetime.now()
                    return cached

                task = self._init_tasks.get(kb_code)
                if task is not None:
                    if task.done():
                        self._init_tasks.pop(kb_code, None)
                        self._init_task_started_at.pop(kb_code, None)
                        task = None
                    else:
                        started_at = self._init_task_started_at.get(kb_code, 0.0)
                        if started_at and (time.monotonic() - started_at) > self._init_timeout_seconds:
                            logger.warning(
                                f"[索引管理器] 初始化任务超时，取消并重建: kb={kb_code}, timeout={self._init_timeout_seconds}s"
                            )
                            task.cancel()
                            self._init_tasks.pop(kb_code, None)
                            self._init_task_started_at.pop(kb_code, None)
                            task = None

                if task is None:
                    if len(self._indices) >= self._max_cached:
                        self._evict_lru_index_locked(exclude={kb_code})

                    task = asyncio.create_task(self._create_and_initialize_index(kb_code, kb_id))
                    self._init_tasks[kb_code] = task
                    self._init_task_started_at[kb_code] = time.monotonic()

            try:
                index = await asyncio.wait_for(
                    asyncio.shield(task),
                    timeout=self._init_timeout_seconds,
                )
            except asyncio.TimeoutError:
                async with self._lock:
                    current = self._init_tasks.get(kb_code)
                    if current is task:
                        current.cancel()
                        self._init_tasks.pop(kb_code, None)
                        self._init_task_started_at.pop(kb_code, None)
                logger.warning(
                    f"[索引管理器] 获取索引超时，已清理初始化任务: kb={kb_code}, timeout={self._init_timeout_seconds}s"
                )
                continue
            except Exception:
                async with self._lock:
                    current = self._init_tasks.get(kb_code)
                    if current is task:
                        self._init_tasks.pop(kb_code, None)
                        self._init_task_started_at.pop(kb_code, None)
                raise

            async with self._lock:
                cached = self._indices.get(kb_code)
                if cached is not None:
                    cached.last_accessed = datetime.now()
                    self._init_tasks.pop(kb_code, None)
                    self._init_task_started_at.pop(kb_code, None)
                    return cached

                self._indices[kb_code] = index
                index.last_accessed = datetime.now()
                self._init_tasks.pop(kb_code, None)
                self._init_task_started_at.pop(kb_code, None)

                if len(self._indices) > self._max_cached:
                    self._evict_lru_index_locked(exclude={kb_code})

                logger.info(f"[索引管理器] 创建索引实例: kb={kb_code}, 当前缓存数={len(self._indices)}")
                return index

    async def _create_and_initialize_index(self, kb_code: str, kb_id: str) -> PerKBKeywordIndex:
        """创建并初始化单个知识库索引（锁外执行）。"""
        index = PerKBKeywordIndex(
            kb_code=kb_code,
            kb_id=kb_id,
            _embedding_dim=self._embedding_dim,
        )
        await index.initialize()
        return index

    def _evict_lru_index_locked(self, exclude: set[str] | None = None) -> None:
        """清理最久未使用的索引"""
        if not self._indices:
            return

        candidates = [kb for kb in self._indices.keys() if exclude is None or kb not in exclude]
        if not candidates:
            return

        lru_kb_code = min(
            candidates,
            key=lambda k: self._indices[k].last_accessed or self._indices[k].last_updated or datetime.min,
        )

        del self._indices[lru_kb_code]

        logger.info(f"[索引管理器] LRU 清理索引: kb={lru_kb_code}")
    
    async def build_index(
        self,
        kb_code: str,
        kb_id: str,
        entities: List[EntityData],
    ) -> IndexBuildResult:
        """
        为指定知识库构建索引
        
        Args:
            kb_code: 知识库编码
            kb_id: 知识库 ID
            entities: 实体数据列表
            
        Returns:
            构建结果
        """
        import time
        
        start_time = time.time()
        result = IndexBuildResult()
        
        try:
            async with self._lock:
                self._indices.pop(kb_code, None)

            index = PerKBKeywordIndex(
                kb_code=kb_code,
                kb_id=kb_id,
                _embedding_dim=self._embedding_dim,
            )

            index.entity_names = [e.entity_name for e in entities]
            for e in entities:
                index.entity_metadata[e.entity_name] = {
                    "entity_type": e.entity_type,
                    "description": e.description,
                    "source_id": e.source_id,
                    "file_path": e.file_path,
                }

            from backend.rag.keywords.bm25_index import BM25Index
            index.bm25_index = BM25Index()
            index.bm25_index.build(index.entity_names)

            index.entity_count = len(entities)
            index.last_updated = datetime.now()
            index.last_accessed = datetime.now()

            await index._save_to_redis()

            logger.info(f"[索引管理器] embedding_func 已设置: {self._embedding_func is not None}")

            if self._embedding_func:
                logger.info(f"[索引管理器] 开始保存向量到 Milvus, 实体数: {len(entities)}")
                await index._save_to_milvus(entities, self._embedding_func)
                result.vector_count = len(entities)
                logger.info(f"[索引管理器] 向量保存完成: {result.vector_count}")
            else:
                logger.warning("[索引管理器] embedding_func 未设置，跳过向量索引构建")

            index._is_initialized = True

            async with self._lock:
                self._indices[kb_code] = index
                if len(self._indices) > self._max_cached:
                    self._evict_lru_index_locked(exclude={kb_code})

            result.entity_count = len(entities)
            result.bm25_index_size = len(index.bm25_index.serialize()) if index.bm25_index else 0
            result.status = "success"
            result.build_time_ms = (time.time() - start_time) * 1000

            logger.info(f"[索引管理器] 构建索引完成: kb={kb_code}, 实体数={result.entity_count}, 耗时={result.build_time_ms:.1f}ms")
                
        except Exception as e:
            result.status = "failed"
            result.error = str(e)
            result.build_time_ms = (time.time() - start_time) * 1000
            logger.error(f"[索引管理器] 构建索引失败: kb={kb_code}, error={e}")
        
        return result
    
    async def add_entities(
        self,
        kb_code: str,
        kb_id: str,
        entities: List[EntityData],
        save_immediately: bool = True,
    ) -> int:
        """
        向指定知识库增量添加实体
        
        Args:
            kb_code: 知识库编码
            kb_id: 知识库 ID
            entities: 实体列表
            save_immediately: 是否立即保存到 Redis（批量操作时设为 False）
        
        Returns:
            添加的实体数量
        """
        index = await self.get_index(kb_code, kb_id)
        return await index.add_entities(entities, self._embedding_func, save_immediately)
    
    async def invalidate_index(self, kb_code: str) -> None:
        """使指定知识库的索引失效"""
        async with self._lock:
            index = self._indices.pop(kb_code, None)
            init_task = self._init_tasks.pop(kb_code, None)
            self._init_task_started_at.pop(kb_code, None)

        if init_task is not None and not init_task.done():
            init_task.cancel()

        if index is not None:
            await index.clear()
            logger.info(f"[索引管理器] 索引失效: kb={kb_code}")
    
    async def delete_index(self, kb_code: str) -> bool:
        """
        删除指定知识库的索引
        
        包括：
        - 内存中的索引实例
        - Redis 中的索引数据
        - Milvus 中的向量索引
        """
        try:
            async with self._lock:
                index = self._indices.pop(kb_code, None)
                init_task = self._init_tasks.pop(kb_code, None)
                self._init_task_started_at.pop(kb_code, None)

            if init_task is not None and not init_task.done():
                init_task.cancel()

            if index is not None:
                await index.clear()

            from backend.rag.keywords.per_kb_index import get_keyword_collection_name
            from pymilvus import connections, utility

            try:
                conn_alias = f"kb_keyword_delete_{kb_code}"
                milvus_uri = settings.milvus_connection_uri
                milvus_user = settings.milvus_user or "root"
                milvus_password = settings.milvus_password or ""

                if not milvus_uri:
                    logger.warning("[索引管理器] Milvus URI 未配置，跳过删除 Collection")
                elif not connections.has_connection(conn_alias):
                    connections.connect(
                        alias=conn_alias,
                        uri=milvus_uri,
                        user=milvus_user,
                        password=milvus_password,
                    )

                collection_name = get_keyword_collection_name(kb_code)
                if utility.has_collection(collection_name, using=conn_alias):
                    utility.drop_collection(collection_name, using=conn_alias)
                    logger.info(f"[索引管理器] 删除 Milvus Collection: {collection_name}")
            except Exception as e:
                logger.warning(f"[索引管理器] 删除 Milvus Collection 失败: {e}")

            logger.info(f"[索引管理器] 删除索引完成: kb={kb_code}")
            return True
                
        except Exception as e:
            logger.error(f"[索引管理器] 删除索引失败: kb={kb_code}, error={e}")
            return False
    
    async def get_stats(self, kb_code: str, kb_id: str = "") -> KeywordIndexStats:
        """获取指定知识库的索引统计信息"""
        index = await self.get_index(kb_code, kb_id)
        return index.get_stats()
    
    async def list_cached_kb(self) -> List[str]:
        """列出已缓存的知识库"""
        return list(self._indices.keys())
    
    async def clear_all(self) -> None:
        """清空所有缓存的索引"""
        async with self._lock:
            indices = list(self._indices.items())
            self._indices.clear()
            init_tasks = list(self._init_tasks.values())
            self._init_tasks.clear()
            self._init_task_started_at.clear()

        for task in init_tasks:
            if not task.done():
                task.cancel()

        for _kb_code, index in indices:
            try:
                await index.clear()
            except Exception:
                pass

        logger.info("[索引管理器] 已清空所有缓存索引")
