import asyncio
from datetime import datetime
from typing import Any, Dict, List, Optional

from backend.config.loggers import get_logger

logger = get_logger(__name__)


class RAGIndexSyncManager:
    """
    RAG 索引同步管理器
    
    职责：
    1. 入库后异步增量同步
    2. 提供查询接口
    3. 状态管理
    """
    
    _instance: Optional["RAGIndexSyncManager"] = None
    
    def __new__(cls):
        if cls._instance is None:
            cls._instance = super().__new__(cls)
            cls._instance._initialized = False
        return cls._instance
    
    def __init__(self):
        if self._initialized:
            return
        
        self._initialized = True
        self._sync_tasks: Dict[str, asyncio.Task] = {}
        self._sync_status: Dict[str, Dict[str, Any]] = {}
        self._lock = asyncio.Lock()
        self._enabled = True
        self._sync_on_insert = True
        self._batch_size = 100
        self._async_initialized = False
        self._async_init_lock = asyncio.Lock()
        
        logger.info("[RAGIndexSyncManager] 初始化完成")

    async def initialize(self) -> None:
        """异步初始化入口，确保并发场景只初始化一次。"""
        if self._async_initialized:
            return

        async with self._async_init_lock:
            if self._async_initialized:
                return

            self._async_initialized = True
            logger.info("[RAGIndexSyncManager] 异步初始化完成")
    
    def set_config(
        self,
        enabled: bool = True,
        sync_on_insert: bool = True,
        batch_size: int = 100,
    ) -> None:
        """设置同步配置"""
        self._enabled = enabled
        self._sync_on_insert = sync_on_insert
        self._batch_size = batch_size
        logger.info(
            f"[RAGIndexSyncManager] 配置更新: enabled={enabled}, "
            f"sync_on_insert={sync_on_insert}, batch_size={batch_size}"
        )
    
    async def sync_after_insert(
        self,
        kb_code: str,
        kb_id: str,
        doc_ids: List[str],
    ) -> bool:
        """
        入库后触发异步增量同步
        
        Args:
            kb_code: 知识库编码
            kb_id: 知识库 ID
            doc_ids: 新入库的文档 ID 列表
            
        Returns:
            是否成功触发同步
        """
        await self.initialize()

        if not self._enabled or not self._sync_on_insert:
            logger.debug("[RAGIndexSyncManager] 同步已禁用，跳过")
            return False
        
        if not doc_ids:
            logger.debug("[RAGIndexSyncManager] 无文档 ID，跳过")
            return False
        
        async with self._lock:
            if kb_code in self._sync_tasks:
                if not self._sync_tasks[kb_code].done():
                    logger.debug(f"[RAGIndexSyncManager] 同步任务进行中，跳过: kb={kb_code}")
                    return False
            
            task = asyncio.create_task(
                self._do_sync(kb_code, kb_id, doc_ids)
            )
            self._sync_tasks[kb_code] = task
        
        logger.info(f"[RAGIndexSyncManager] 触发增量同步: kb={kb_code}, docs={len(doc_ids)}")
        return True
    
    async def _do_sync(
        self,
        kb_code: str,
        kb_id: str,
        doc_ids: List[str],
    ) -> None:
        """执行增量同步"""
        try:
            self._sync_status[kb_code] = {
                "status": "running",
                "started_at": datetime.now().isoformat(),
                "doc_ids": doc_ids,
            }
            
            new_entities = await self._fetch_new_entities(kb_code, doc_ids)
            
            if not new_entities:
                logger.info(f"[RAGIndexSyncManager] 无新增实体: kb={kb_code}")
                self._sync_status[kb_code]["status"] = "completed"
                self._sync_status[kb_code]["completed_at"] = datetime.now().isoformat()
                self._sync_status[kb_code]["entity_count"] = 0
                return
            
            await self._batch_add_entities(kb_code, kb_id, new_entities)
            
            self._sync_status[kb_code]["status"] = "completed"
            self._sync_status[kb_code]["completed_at"] = datetime.now().isoformat()
            self._sync_status[kb_code]["entity_count"] = len(new_entities)
            
            logger.info(
                f"[RAGIndexSyncManager] 同步完成: kb={kb_code}, "
                f"entities={len(new_entities)}"
            )
            
        except Exception as e:
            logger.error(f"[RAGIndexSyncManager] 同步失败: kb={kb_code}, error={e}")
            self._sync_status[kb_code] = {
                "status": "failed",
                "error": str(e),
            }
        finally:
            async with self._lock:
                if kb_code in self._sync_tasks:
                    del self._sync_tasks[kb_code]
    
    async def _fetch_new_entities(
        self,
        kb_code: str,
        doc_ids: List[str],
    ) -> List[Dict[str, str]]:
        """从图数据库获取新增实体
        
        注意：实体的 source_id 存储的是 chunk_id，而不是 doc_id。
        所以需要先从 Milvus chunks collection 获取文档的所有 chunk_id，
        然后用这些 chunk_id 去查询实体。
        """
        storage = None
        try:
            chunk_ids = await self._get_chunk_ids_for_docs(kb_code, doc_ids)
            
            if not chunk_ids:
                logger.info(f"[RAGIndexSyncManager] 未找到文档的 chunk_id: kb={kb_code}, doc_ids={doc_ids}")
                return []
            
            from backend.rag.storages.nebula_storage import NebulaGraphStorage
            storage = NebulaGraphStorage(workspace=kb_code)
            await storage.initialize()
            
            all_entities = []
            for chunk_id in chunk_ids:
                try:
                    entities = await storage.get_nodes_by_source_id(chunk_id)
                    all_entities.extend(entities)
                except Exception as e:
                    logger.warning(
                        f"[RAGIndexSyncManager] 获取实体失败: chunk_id={chunk_id}, error={e}"
                    )
            
            seen = set()
            unique_entities = []
            for e in all_entities:
                eid = e.get("entity_id")
                if eid and eid not in seen:
                    seen.add(eid)
                    unique_entities.append(e)
            
            return unique_entities
        except Exception as e:
            logger.error(f"[RAGIndexSyncManager] 获取新增实体失败: {e}")
            return []
        finally:
            if storage:
                try:
                    await storage.finalize()
                except Exception:
                    pass
    
    async def _get_chunk_ids_for_docs(
        self,
        kb_code: str,
        doc_ids: List[str],
    ) -> List[str]:
        """从 Milvus chunks collection 获取文档的所有 chunk_id"""
        try:
            from pymilvus import connections, Collection, utility
            from backend.config.settings import settings
            
            milvus_uri = settings.milvus_connection_uri
            milvus_user = settings.milvus_user or "root"
            milvus_password = settings.milvus_password or ""
            milvus_db_name = settings.milvus_connection_db_name
            
            conn_alias = f"chunk_sync_{kb_code}"
            
            if not connections.has_connection(conn_alias):
                connect_kwargs = {
                    "alias": conn_alias,
                    "uri": milvus_uri,
                    "user": milvus_user,
                    "password": milvus_password,
                }
                if milvus_db_name:
                    connect_kwargs["db_name"] = milvus_db_name
                connections.connect(**connect_kwargs)
            
            collection_name = f"{kb_code}_chunks"
            
            if not utility.has_collection(collection_name, using=conn_alias):
                logger.info(f"[RAGIndexSyncManager] Milvus chunks collection 不存在: {collection_name}")
                return []
            
            collection = Collection(collection_name, using=conn_alias)
            collection.load()
            
            chunk_ids = []
            for doc_id in doc_ids:
                try:
                    results = collection.query(
                        expr=f'full_doc_id == "{doc_id}"',
                        output_fields=["id"],
                        limit=10000,
                    )
                    for item in results:
                        chunk_id = item.get("id", "")
                        if chunk_id:
                            chunk_ids.append(chunk_id)
                except Exception as e:
                    logger.warning(f"[RAGIndexSyncManager] 查询 chunk_id 失败: doc_id={doc_id}, error={e}")
            
            logger.info(f"[RAGIndexSyncManager] 获取 chunk_id: kb={kb_code}, doc_ids={doc_ids}, chunk_count={len(chunk_ids)}")
            return chunk_ids
            
        except Exception as e:
            logger.error(f"[RAGIndexSyncManager] 获取 chunk_id 失败: {e}")
            return []
    
    async def _batch_add_entities(
        self,
        kb_code: str,
        kb_id: str,
        entities: List[Dict[str, str]],
    ) -> None:
        """批量添加实体到索引"""
        try:
            from backend.rag.keywords.index_manager import get_keyword_index_manager
            from backend.rag.keywords.per_kb_index import EntityData

            index_manager = get_keyword_index_manager()

            if not index_manager._embedding_func:
                from lightrag.utils import EmbeddingFunc
                from backend.config.settings import settings

                embedding_config = settings.embedding_config
                embedding_api_base = embedding_config.get("api_base") or ""
                embedding_api_key = embedding_config.get("api_key") or ""
                embedding_model = embedding_config.get("model") or ""
                embedding_dim = embedding_config.get("dim") or 4096

                if embedding_api_base and embedding_model:
                    async def embed_func(texts: list[str]):
                        import httpx
                        import numpy as np
                        async with httpx.AsyncClient(timeout=60.0) as client:
                            response = await client.post(
                                f"{embedding_api_base}/embeddings",
                                headers={"Authorization": f"Bearer {embedding_api_key}"},
                                json={"input": texts, "model": embedding_model},
                            )
                            response.raise_for_status()
                            data = response.json()
                            embeddings = [item["embedding"] for item in data["data"]]
                            return np.array(embeddings)

                    embedding_func = EmbeddingFunc(
                        embedding_dim=embedding_dim,
                        max_token_size=8192,
                        func=embed_func,
                    )
                    index_manager.set_embedding_func(embedding_func)
            
            for i in range(0, len(entities), self._batch_size):
                batch = entities[i:i + self._batch_size]
                
                entity_data_list = [
                    EntityData(
                        entity_name=e["entity_id"],
                        entity_type=e.get("entity_type", "unknown"),
                        description=e.get("description", ""),
                        source_id=e.get("source_id", ""),
                        file_path=e.get("file_path", ""),
                    )
                    for e in batch
                    if e.get("entity_id")
                ]
                
                if entity_data_list:
                    is_last_batch = (i + self._batch_size) >= len(entities)
                    await index_manager.add_entities(
                        kb_code, kb_id, entity_data_list, 
                        save_immediately=is_last_batch
                    )
                    
                    logger.debug(
                        f"[RAGIndexSyncManager] 批次完成: kb={kb_code}, "
                        f"batch={i//self._batch_size + 1}, count={len(batch)}"
                    )
                    
        except Exception as e:
            logger.error(f"[RAGIndexSyncManager] 批量添加实体失败: {e}")
            raise
    
    async def get_sync_status(self, kb_code: str) -> Optional[Dict[str, Any]]:
        """获取同步状态"""
        await self.initialize()
        return self._sync_status.get(kb_code)
    
    async def force_sync(
        self,
        kb_code: str,
        kb_id: str,
    ) -> bool:
        """
        强制全量同步
        
        从 NebulaGraph 或 Milvus 获取实体数据，同步到关键词索引
        
        Args:
            kb_code: 知识库编码
            kb_id: 知识库 ID
            
        Returns:
            是否成功触发
        """
        await self.initialize()

        storage = None
        try:
            all_entities = []
            
            from backend.rag.storages.nebula_storage import NebulaGraphStorage
            storage = NebulaGraphStorage(workspace=kb_code)
            await storage.initialize()
            all_entities = await storage.get_all_nodes()
            
            if not all_entities:
                logger.info(f"[RAGIndexSyncManager] NebulaGraph 无数据，尝试从 Milvus 获取: kb={kb_code}")
                all_entities = await self._fetch_entities_from_milvus(kb_code)
            
            existing_entities = set()
            has_vector_index = False
            try:
                from backend.rag.keywords.index_manager import get_keyword_index_manager
                index_manager = get_keyword_index_manager()
                index = await index_manager.get_index(kb_code, kb_id)
                if index:
                    existing_entities = set(index.entity_names)
                    has_vector_index = await index.has_vector_index_async()
            except Exception as e:
                logger.warning(f"[RAGIndexSyncManager] 获取索引失败: {e}")
            
            new_entities = [
                e for e in all_entities
                if e.get("entity_id") and e["entity_id"] not in existing_entities
            ]
            
            need_rebuild_vector = not has_vector_index and all_entities
            
            if new_entities:
                await self._batch_add_entities(kb_code, kb_id, new_entities)
                logger.info(f"[RAGIndexSyncManager] 全量同步完成: kb={kb_code}, 新增={len(new_entities)}")
            elif need_rebuild_vector:
                logger.info(f"[RAGIndexSyncManager] 向量索引不存在，重建向量索引: kb={kb_code}")
                await self._rebuild_vector_index(kb_code, kb_id, all_entities)
            else:
                logger.info(f"[RAGIndexSyncManager] 全量同步: 无新增实体: kb={kb_code}")
            
            return True
            
        except Exception as e:
            logger.error(f"[RAGIndexSyncManager] 全量同步失败: kb={kb_code}, error={e}")
            return False
        finally:
            if storage:
                try:
                    await storage.finalize()
                except Exception:
                    pass
    
    async def _fetch_entities_from_milvus(self, kb_code: str) -> List[Dict[str, str]]:
        """从 Milvus 的 entities collection 获取实体数据"""
        try:
            from pymilvus import connections, Collection, utility
            from backend.config.settings import settings
            
            milvus_uri = settings.milvus_connection_uri
            milvus_user = settings.milvus_user or "root"
            milvus_password = settings.milvus_password or ""
            milvus_db_name = settings.milvus_connection_db_name
            
            conn_alias = f"sync_{kb_code}"
            
            if not connections.has_connection(conn_alias):
                connect_kwargs = {
                    "alias": conn_alias,
                    "uri": milvus_uri,
                    "user": milvus_user,
                    "password": milvus_password,
                }
                if milvus_db_name:
                    connect_kwargs["db_name"] = milvus_db_name
                connections.connect(**connect_kwargs)
            
            collection_name = f"{kb_code}_entities"
            
            if not utility.has_collection(collection_name, using=conn_alias):
                logger.info(f"[RAGIndexSyncManager] Milvus collection 不存在: {collection_name}")
                return []
            
            collection = Collection(collection_name, using=conn_alias)
            collection.load()
            
            entities = []
            iterator = collection.query_iterator(
                expr="",
                output_fields=["entity_name", "entity_type", "description", "source_id"],
                batch_size=1000,
            )
            
            while True:
                results = iterator.next()
                if not results:
                    break
                
                for item in results:
                    entity_name = item.get("entity_name", "")
                    if entity_name:
                        entities.append({
                            "entity_id": entity_name,
                            "entity_type": item.get("entity_type", "unknown"),
                            "description": item.get("description", ""),
                            "source_id": item.get("source_id", ""),
                        })
            
            iterator.close()
            logger.info(f"[RAGIndexSyncManager] 从 Milvus 获取实体: kb={kb_code}, count={len(entities)}")
            
            return entities
            
        except Exception as e:
            logger.error(f"[RAGIndexSyncManager] 从 Milvus 获取实体失败: {e}")
            return []
    
    async def _rebuild_vector_index(
        self,
        kb_code: str,
        kb_id: str,
        entities: List[Dict[str, str]],
    ) -> None:
        """重建向量索引"""
        try:
            from backend.rag.keywords.index_manager import get_keyword_index_manager
            from backend.rag.keywords.per_kb_index import EntityData
            
            index_manager = get_keyword_index_manager()
            
            if not index_manager._embedding_func:
                from lightrag.utils import EmbeddingFunc
                from backend.config.settings import settings
                import httpx
                import numpy as np
                
                embedding_config = settings.embedding_config
                embedding_api_base = embedding_config.get("api_base") or ""
                embedding_api_key = embedding_config.get("api_key") or ""
                embedding_model = embedding_config.get("model") or ""
                embedding_dim = embedding_config.get("dim") or 4096
                
                if embedding_api_base and embedding_model:
                    async def embed_func(texts: list[str]):
                        async with httpx.AsyncClient(timeout=60.0) as client:
                            response = await client.post(
                                f"{embedding_api_base}/embeddings",
                                headers={"Authorization": f"Bearer {embedding_api_key}"},
                                json={"input": texts, "model": embedding_model},
                            )
                            response.raise_for_status()
                            data = response.json()
                            embeddings = [item["embedding"] for item in data["data"]]
                            return np.array(embeddings)
                    
                    embedding_func = EmbeddingFunc(
                        embedding_dim=embedding_dim,
                        max_token_size=8192,
                        func=embed_func,
                    )
                    index_manager.set_embedding_func(embedding_func)
            
            entity_data_list = [
                EntityData(
                    entity_name=e["entity_id"],
                    entity_type=e.get("entity_type", "unknown"),
                    description=e.get("description", ""),
                    source_id=e.get("source_id", ""),
                    file_path=e.get("file_path", ""),
                )
                for e in entities
                if e.get("entity_id")
            ]
            
            if entity_data_list and index_manager._embedding_func:
                index = await index_manager.get_index(kb_code, kb_id)
                await index._save_to_milvus(entity_data_list, index_manager._embedding_func)
                logger.info(f"[RAGIndexSyncManager] 向量索引重建完成: kb={kb_code}, count={len(entity_data_list)}")
            else:
                logger.warning(f"[RAGIndexSyncManager] 无法重建向量索引: kb={kb_code}, embedding_func={index_manager._embedding_func is not None}")
                
        except Exception as e:
            logger.error(f"[RAGIndexSyncManager] 重建向量索引失败: kb={kb_code}, error={e}")
    
    async def disable(self) -> None:
        """禁用同步"""
        await self.initialize()
        self._enabled = False
        logger.info("[RAGIndexSyncManager] 同步已禁用")
    
    async def enable(self) -> None:
        """启用同步"""
        await self.initialize()
        self._enabled = True
        logger.info("[RAGIndexSyncManager] 同步已启用")


def get_rag_index_sync_manager() -> RAGIndexSyncManager:
    """获取 RAG 索引同步管理器单例"""
    return RAGIndexSyncManager()
