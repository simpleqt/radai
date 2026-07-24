"""
知识库级别的关键词索引

每个知识库独立的索引实例，支持向量检索和 BM25 检索
"""

import asyncio
import hashlib
import re
import time
from dataclasses import dataclass, field
from datetime import datetime
from typing import Callable, Dict, List, Optional, Tuple

import numpy as np
import redis
from pymilvus import Collection, connections, utility

from backend.config.loggers import get_logger
from backend.config.settings import settings
from backend.core.utils.env import positive_float_from_env
from backend.rag.keywords.bm25_index import BM25Index
from backend.rag.keywords.models import EntityData, KeywordIndexStats

logger = get_logger(__name__)


_MILVUS_KEYWORD_LOAD_TIMEOUT_SECONDS = positive_float_from_env(
    "MILVUS_KEYWORD_LOAD_TIMEOUT_SECONDS", 15.0
)
_MILVUS_KEYWORD_SEARCH_TIMEOUT_SECONDS = positive_float_from_env(
    "MILVUS_KEYWORD_SEARCH_TIMEOUT_SECONDS", 15.0
)


def _sanitize_collection_component(value: str, fallback_prefix: str) -> str:
    """将任意 kb_code 规范化为 Milvus 允许的集合名片段（仅字母数字下划线）。"""
    raw = str(value or "").strip()
    cleaned = re.sub(r"[^0-9A-Za-z_]", "_", raw)
    cleaned = re.sub(r"_+", "_", cleaned).strip("_")

    # 若清洗后为空（如纯中文），使用 hash 保证稳定且可区分。
    if not cleaned:
        digest = hashlib.md5(raw.encode("utf-8")).hexdigest()[:8]
        cleaned = f"{fallback_prefix}_{digest}"

    # Milvus 集合名需以字母或下划线开头，数字开头时补前缀。
    if cleaned[0].isdigit():
        cleaned = f"{fallback_prefix}_{cleaned}"

    return cleaned


def get_keyword_collection_name(kb_code: str) -> str:
    """获取关键词索引 Collection 名称"""
    base = _sanitize_collection_component(kb_code, fallback_prefix="kb")
    if base.startswith("rag_"):
        return f"{base}_keyword_index"
    return f"rag_{base}_keyword_index"


def get_entity_collection_name(kb_code: str) -> str:
    """获取实体 Collection 名称"""
    base = _sanitize_collection_component(kb_code, fallback_prefix="kb")
    return f"{base}_entities"


def get_self_ingestion_entity_collection_name(kb_code: str) -> str:
    """获取 self-ingestion v2 实体 Collection 名称"""
    base = _sanitize_collection_component(kb_code, fallback_prefix="kb")
    return f"{base}_entities_v2"


def get_redis_key_prefix(kb_code: str) -> str:
    """获取 Redis Key 前缀"""
    return f"{kb_code}:keyword_index"


@dataclass
class PerKBKeywordIndex:
    """
    单个知识库的关键词索引
    
    包含：
    - 向量索引（Milvus）
    - BM25 文本索引
    - 图结构索引（可选）
    """
    kb_code: str
    kb_id: str
    
    entity_names: List[str] = field(default_factory=list)
    entity_metadata: Dict[str, Dict] = field(default_factory=dict)
    bm25_index: Optional[BM25Index] = None
    
    last_updated: Optional[datetime] = None
    last_accessed: Optional[datetime] = None
    entity_count: int = 0
    
    _milvus_conn_alias: str = "default"
    _redis_client: Optional[redis.Redis] = None
    _keyword_collection: Optional[Collection] = None
    _embedding_func: Optional[Callable] = None
    _embedding_dim: int = 4096
    _is_initialized: bool = False
    
    def __post_init__(self):
        pass
    
    def _get_redis_client(self) -> redis.Redis:
        """获取 Redis 客户端"""
        if self._redis_client is None:
            redis_host = settings.redis_host or "localhost"
            redis_port = settings.redis_port or 6379
            redis_password = settings.redis_password
            redis_db = settings.redis_db or 0
            
            self._redis_client = redis.Redis(
                host=redis_host,
                port=redis_port,
                password=redis_password if redis_password else None,
                db=redis_db,
                decode_responses=False,
            )
        return self._redis_client
    
    def _connect_milvus(self) -> None:
        """连接 Milvus"""
        if not connections.has_connection(self._milvus_conn_alias):
            milvus_uri = settings.milvus_connection_uri
            milvus_user = settings.milvus_user or "root"
            milvus_password = settings.milvus_password or ""
            milvus_db_name = settings.milvus_connection_db_name
            
            if not milvus_uri:
                raise ValueError("Milvus URI not configured in settings")
            
            connect_kwargs = {
                "alias": self._milvus_conn_alias,
                "uri": milvus_uri,
                "user": milvus_user,
                "password": milvus_password,
            }
            if milvus_db_name:
                connect_kwargs["db_name"] = milvus_db_name

            connections.connect(**connect_kwargs)
    
    async def initialize(self) -> None:
        """初始化索引（从 Redis 和 Milvus 加载）"""
        if self._is_initialized:
            return
        
        start_time = time.time()
        logger.info(f"[关键词索引] 开始初始化知识库 {self.kb_code}...")
        
        try:
            await self._load_from_redis()
            
            if not self.entity_names:
                await self._load_from_milvus()
            
            if not self.entity_names:
                await self._load_from_self_ingestion_entities_v2()
            
            if not self.entity_names:
                await self._load_from_nebula()
            
            self._is_initialized = True
            elapsed = (time.time() - start_time) * 1000
            logger.info(f"[关键词索引] 初始化完成: kb={self.kb_code}, 实体数={len(self.entity_names)}, 耗时={elapsed:.1f}ms")
            
        except Exception as e:
            logger.error(f"[关键词索引] 初始化失败: kb={self.kb_code}, error={e}")
            self._is_initialized = False
    
    async def _load_from_redis(self) -> None:
        """从 Redis 加载索引数据"""
        try:
            def _load_sync() -> tuple[List[str], Dict[str, Dict], Optional[BM25Index], Optional[datetime], bool]:
                redis_client = self._get_redis_client()
                prefix = get_redis_key_prefix(self.kb_code)

                entity_names_bytes = redis_client.lrange(f"{prefix}:entity_names", 0, -1)
                entity_names = [name.decode() for name in entity_names_bytes]
                if not entity_names:
                    return [], {}, None, None, False

                entity_metadata: Dict[str, Dict] = {}
                pipe = redis_client.pipeline()
                for entity_name in entity_names:
                    key = f"{prefix}:entity_metadata:{entity_name}"
                    pipe.hgetall(key)

                results = pipe.execute()
                for entity_name, metadata in zip(entity_names, results):
                    if metadata:
                        entity_metadata[entity_name] = {
                            "entity_type": metadata.get(b"entity_type", b"").decode(),
                            "description": metadata.get(b"description", b"").decode(),
                            "source_id": metadata.get(b"source_id", b"").decode(),
                            "file_path": metadata.get(b"file_path", b"").decode(),
                        }

                bm25_index: Optional[BM25Index] = None
                bm25_rebuilt = False
                bm25_data = redis_client.get(f"{prefix}:bm25_corpus")
                if bm25_data:
                    try:
                        bm25_index = BM25Index.deserialize(bm25_data)
                    except Exception:
                        bm25_index = None

                if not bm25_index and entity_names:
                    bm25_index = BM25Index()
                    bm25_index.build(entity_names)
                    redis_client.set(f"{prefix}:bm25_corpus", bm25_index.serialize())
                    bm25_rebuilt = True

                last_updated: Optional[datetime] = None
                last_updated_str = redis_client.get(f"{prefix}:last_updated")
                if last_updated_str:
                    try:
                        last_updated = datetime.fromtimestamp(int(last_updated_str))
                    except Exception:
                        last_updated = None

                return entity_names, entity_metadata, bm25_index, last_updated, bm25_rebuilt

            entity_names, entity_metadata, bm25_index, last_updated, bm25_rebuilt = await asyncio.to_thread(_load_sync)
            self.entity_names = entity_names
            self.entity_metadata = entity_metadata
            self.bm25_index = bm25_index
            self.last_updated = last_updated
            self.entity_count = len(self.entity_names)
            self.last_accessed = datetime.now()

            if bm25_rebuilt:
                logger.info(f"[Redis] BM25索引构建完成: {len(self.entity_names)} 个文档")

            if not self.entity_names:
                return

            logger.info(f"[Redis] 加载知识库 {self.kb_code}: {len(self.entity_names)} 个实体, {len(self.entity_metadata)} 个元数据")
            
        except Exception as e:
            logger.warning(f"[Redis] 加载失败: kb={self.kb_code}, error={e}")
    
    def get_entity_metadata(self, entity_name: str) -> Optional[Dict]:
        """获取单个实体的元数据（延迟加载）"""
        if entity_name in self.entity_metadata:
            return self.entity_metadata[entity_name]
        
        try:
            redis_client = self._get_redis_client()
            prefix = get_redis_key_prefix(self.kb_code)
            key = f"{prefix}:entity_metadata:{entity_name}"
            metadata = redis_client.hgetall(key)
            if metadata:
                result = {
                    "entity_type": metadata.get(b"entity_type", b"").decode(),
                    "description": metadata.get(b"description", b"").decode(),
                    "source_id": metadata.get(b"source_id", b"").decode(),
                    "file_path": metadata.get(b"file_path", b"").decode(),
                }
                self.entity_metadata[entity_name] = result
                return result
        except Exception as e:
            logger.warning(f"[Redis] 获取实体元数据失败: {entity_name}, error={e}")
        
        return None
    
    async def _load_from_milvus(self) -> None:
        """从 Milvus 加载实体数据"""
        try:
            def _load_sync() -> tuple[List[str], Dict[str, Dict]]:
                self._connect_milvus()

                collection_name = get_keyword_collection_name(self.kb_code)
                if not utility.has_collection(collection_name, using=self._milvus_conn_alias):
                    return [], {}

                collection = Collection(collection_name, using=self._milvus_conn_alias)
                collection.load()

                names: List[str] = []
                metadata: Dict[str, Dict] = {}
                seen_names: set[str] = set()
                existing_fields = {field.name for field in collection.schema.fields}
                output_fields = [
                    field
                    for field in ("entity_name", "entity_type", "description", "source_id", "file_path")
                    if field in existing_fields
                ]
                if "entity_name" not in output_fields:
                    logger.warning(
                        "[Milvus] Collection %s 缺少 entity_name 字段，跳过加载",
                        collection_name,
                    )
                    return [], {}

                iterator = collection.query_iterator(
                    expr="",
                    output_fields=output_fields,
                    batch_size=10000,
                )

                try:
                    while True:
                        results = iterator.next()
                        if not results:
                            break

                        for item in results:
                            entity_name = item.get("entity_name", "")
                            if entity_name and entity_name not in seen_names:
                                seen_names.add(entity_name)
                                names.append(entity_name)
                                metadata[entity_name] = {
                                    "entity_type": item.get("entity_type", ""),
                                    "description": item.get("description", ""),
                                    "source_id": item.get("source_id", ""),
                                    "file_path": item.get("file_path", ""),
                                }
                finally:
                    iterator.close()

                return names, metadata

            names, metadata = await asyncio.to_thread(_load_sync)
            if not names:
                logger.info(f"[Milvus] Collection {get_keyword_collection_name(self.kb_code)} 不存在或无数据")
                return

            seen = set(self.entity_names)
            for entity_name in names:
                if entity_name not in seen:
                    self.entity_names.append(entity_name)
                    self.entity_metadata[entity_name] = metadata.get(entity_name, {})
                    seen.add(entity_name)

            self.entity_count = len(self.entity_names)
            self.last_accessed = datetime.now()
            logger.info(f"[Milvus] 加载知识库 {self.kb_code}: {len(self.entity_names)} 个实体")
            
            if self.entity_names and not self.bm25_index:
                logger.info(f"[Milvus] BM25索引不存在，开始构建...")
                self.bm25_index = BM25Index()
                self.bm25_index.build(self.entity_names)
                logger.info(f"[Milvus] BM25索引构建完成: {len(self.entity_names)} 个文档")
            
            if self.entity_names:
                await self._save_to_redis()
                logger.info("[Redis] 已保存从 Milvus 加载的数据")
            
        except Exception as e:
            logger.warning(f"[Milvus] 加载失败: kb={self.kb_code}, error={e}")

    async def _load_from_self_ingestion_entities_v2(self) -> None:
        """从 self-ingestion v2 的实体向量库加载关键词实体。"""
        try:
            def _load_sync() -> tuple[List[str], Dict[str, Dict]]:
                self._connect_milvus()

                collection_name = get_self_ingestion_entity_collection_name(self.kb_code)
                if not utility.has_collection(collection_name, using=self._milvus_conn_alias):
                    return [], {}

                collection = Collection(collection_name, using=self._milvus_conn_alias)
                collection.load()

                existing_fields = {field.name for field in collection.schema.fields}
                output_fields = [
                    field
                    for field in (
                        "entity_name",
                        "entity_type",
                        "description",
                        "source_id",
                        "file_path",
                        "doc_ids",
                        "chunk_ids",
                    )
                    if field in existing_fields
                ]
                if "entity_name" not in output_fields:
                    logger.warning(
                        "[Milvus v2] Collection %s 缺少 entity_name 字段，跳过加载",
                        collection_name,
                    )
                    return [], {}

                expr_parts = []
                if "kb_id" in existing_fields and self.kb_id:
                    expr_parts.append(f'kb_id == "{self.kb_id}"')
                if "is_visible" in existing_fields:
                    expr_parts.append("is_visible == true")
                if "status" in existing_fields:
                    expr_parts.append('status != "deleted"')
                expr = " && ".join(expr_parts)

                names: List[str] = []
                metadata: Dict[str, Dict] = {}
                seen_names: set[str] = set()
                iterator = collection.query_iterator(
                    expr=expr,
                    output_fields=output_fields,
                    batch_size=10000,
                )

                try:
                    while True:
                        results = iterator.next()
                        if not results:
                            break

                        for item in results:
                            entity_name = item.get("entity_name", "")
                            if entity_name and entity_name not in seen_names:
                                seen_names.add(entity_name)
                                names.append(entity_name)
                                metadata[entity_name] = {
                                    "entity_type": item.get("entity_type", "term") or "term",
                                    "description": item.get("description", "") or "",
                                    "source_id": item.get("source_id", "") or item.get("chunk_ids", ""),
                                    "file_path": item.get("file_path", "") or item.get("doc_ids", ""),
                                }
                finally:
                    iterator.close()

                return names, metadata

            names, metadata = await asyncio.to_thread(_load_sync)
            if not names:
                logger.info(
                    "[Milvus v2] Collection %s 不存在或无数据",
                    get_self_ingestion_entity_collection_name(self.kb_code),
                )
                return

            seen = set(self.entity_names)
            for entity_name in names:
                if entity_name not in seen:
                    self.entity_names.append(entity_name)
                    self.entity_metadata[entity_name] = metadata.get(entity_name, {})
                    seen.add(entity_name)

            self.entity_count = len(self.entity_names)
            self.last_accessed = datetime.now()
            logger.info(f"[Milvus v2] 加载知识库 {self.kb_code}: {len(names)} 个实体")

            if self.entity_names and not self.bm25_index:
                self.bm25_index = BM25Index()
                self.bm25_index.build(self.entity_names)

            if self.entity_names:
                await self._save_to_redis()
                logger.info("[Redis] 已保存从 Milvus v2 加载的数据")

        except Exception as e:
            logger.warning(f"[Milvus v2] 加载失败: kb={self.kb_code}, error={e}")
    
    async def _load_from_nebula(self) -> None:
        """从 NebulaGraph 加载实体数据"""
        try:
            def _load_sync() -> tuple[List[str], Dict[str, Dict]]:
                from nebula3.gclient.net import ConnectionPool
                from nebula3.Config import Config as NebulaConfig

                def get_prop(props, key):
                    if key in props:
                        prop_val = props[key]
                        if hasattr(prop_val, "is_string") and prop_val.is_string():
                            return prop_val.as_string()
                        if hasattr(prop_val, "is_int") and prop_val.is_int():
                            return str(prop_val.as_int())
                        if hasattr(prop_val, "is_double") and prop_val.is_double():
                            return str(prop_val.as_double())
                    return ""

                nebula_host = settings.nebula_host
                nebula_port = settings.nebula_graph_port
                nebula_user = settings.nebula_user
                nebula_password = settings.nebula_password

                config = NebulaConfig()
                config.max_connection_pool_size = 10

                pool = ConnectionPool()
                pool.init([(nebula_host, nebula_port)], config)
                session = pool.get_session(nebula_user, nebula_password)

                names: List[str] = []
                metadata: Dict[str, Dict] = {}
                seen: set[str] = set()
                try:
                    result = session.execute(f"USE `{self.kb_code}`")
                    if not result.is_succeeded():
                        return [], {}

                    offset = 0
                    batch_size = 10000
                    total = 0

                    while True:
                        nGQL = f"MATCH (v:entity) RETURN v SKIP {offset} LIMIT {batch_size}"
                        result = session.execute(nGQL)
                        if not result.is_succeeded():
                            break

                        row_count = result.row_size()
                        if row_count == 0:
                            break

                        for i in range(row_count):
                            row = result.row_values(i)
                            val = row[0]
                            if not val.is_vertex():
                                continue

                            vertex = val.as_node()
                            props = vertex.properties()
                            entity_id = get_prop(props, "entity_id")
                            if entity_id and entity_id not in seen:
                                seen.add(entity_id)
                                names.append(entity_id)
                                metadata[entity_id] = {
                                    "entity_type": get_prop(props, "entity_type") or "unknown",
                                    "description": (get_prop(props, "description") or "")[:500],
                                    "source_id": get_prop(props, "source_id"),
                                    "file_path": get_prop(props, "file_path"),
                                }

                        total += row_count
                        offset += batch_size
                        if row_count < batch_size:
                            break

                        if total % 50000 == 0:
                            logger.info(f"[NebulaGraph] 已加载 {total} 个实体...")
                finally:
                    session.release()
                    pool.close()

                return names, metadata

            names, metadata = await asyncio.to_thread(_load_sync)
            if not names:
                logger.warning(f"[NebulaGraph] Space 不存在或无数据: {self.kb_code}")
                return

            seen = set(self.entity_names)
            for entity_id in names:
                if entity_id not in seen:
                    self.entity_names.append(entity_id)
                    self.entity_metadata[entity_id] = metadata.get(entity_id, {})
                    seen.add(entity_id)

            self.entity_count = len(self.entity_names)
            self.last_accessed = datetime.now()
            logger.info(f"[NebulaGraph] 加载知识库 {self.kb_code}: {len(self.entity_names)} 个实体")

            if self.entity_names and not self.bm25_index:
                self.bm25_index = BM25Index()
                self.bm25_index.build(self.entity_names)
                logger.info("[NebulaGraph] 构建 BM25 索引完成")

            await self._save_to_redis()
            logger.info("[Redis] 已保存从 NebulaGraph 加载的数据")
                
        except Exception as e:
            logger.warning(f"[NebulaGraph] 加载失败: kb={self.kb_code}, error={e}")
    
    async def search_vector(
        self,
        query_embedding: np.ndarray,
        top_k: int = 15,
    ) -> List[Tuple[str, float]]:
        """
        向量搜索
        
        返回: [(entity_name, score), ...]
        """
        try:
            collection_name = get_keyword_collection_name(self.kb_code)
            query_vector = query_embedding.tolist()

            def _search_sync() -> List[Tuple[str, float]]:
                self._connect_milvus()

                if not utility.has_collection(collection_name, using=self._milvus_conn_alias):
                    return []

                collection = Collection(collection_name, using=self._milvus_conn_alias)
                try:
                    collection.load(timeout=_MILVUS_KEYWORD_LOAD_TIMEOUT_SECONDS)
                except Exception as load_error:
                    error_msg = str(load_error)
                    if "can't change the index" in error_msg or "invalid parameter" in error_msg:
                        logger.warning(f"[向量搜索] Collection schema 不兼容，删除重建: {collection_name}")
                        utility.drop_collection(collection_name, using=self._milvus_conn_alias)
                        return []
                    raise

                search_params = {"metric_type": "COSINE", "params": {"ef": 64}}
                results = collection.search(
                    data=[query_vector],
                    anns_field="vector",
                    param=search_params,
                    limit=top_k,
                    output_fields=["entity_name"],
                    timeout=_MILVUS_KEYWORD_SEARCH_TIMEOUT_SECONDS,
                )

                entities: List[Tuple[str, float]] = []
                for hits in results:
                    for hit in hits:
                        entity_name = hit.entity.get("entity_name", "")
                        if entity_name:
                            entities.append((entity_name, hit.score))
                return entities

            entities = await asyncio.to_thread(_search_sync)
            if not entities:
                logger.warning(f"[向量搜索] Collection不存在或无结果: {collection_name}")
                return []

            logger.info(f"[向量搜索] kb={self.kb_code}, collection={collection_name}, 返回{len(entities)}个结果")
            
            return entities
            
        except Exception as e:
            logger.warning(f"[向量搜索] 失败: kb={self.kb_code}, error={e}", exc_info=True)
            return []
    
    async def search_bm25(
        self,
        query: str,
        top_k: int = 15,
    ) -> List[Tuple[str, float]]:
        """
        BM25 搜索
        
        返回: [(entity_name, score), ...]
        """
        if self.bm25_index is None:
            return []
        
        try:
            results = await asyncio.to_thread(
                self.bm25_index.search,
                query,
                top_k=top_k,
            )
            
            entities = []
            for doc_idx, score in results:
                if doc_idx < len(self.entity_names):
                    entity_name = self.entity_names[doc_idx]
                    entities.append((entity_name, score))
            
            return entities
            
        except Exception as e:
            logger.warning(f"[BM25搜索] 失败: kb={self.kb_code}, error={e}")
            return []
    
    def get_stats(self) -> KeywordIndexStats:
        """获取索引统计信息"""
        return KeywordIndexStats(
            kb_id=self.kb_id,
            kb_code=self.kb_code,
            entity_count=len(self.entity_names),
            vector_count=len(self.entity_names),
            bm25_doc_count=self.bm25_index.doc_count if self.bm25_index else 0,
            bm25_term_count=len(self.bm25_index.term_doc_freq) if self.bm25_index else 0,
            last_updated=self.last_updated,
            status="ready" if self._is_initialized else "not_initialized",
        )
    
    def has_vector_index(self) -> bool:
        """检查 Milvus 向量索引是否存在且有数据"""
        try:
            self._connect_milvus()
            collection_name = get_keyword_collection_name(self.kb_code)
            if not utility.has_collection(collection_name, using=self._milvus_conn_alias):
                return False
            
            collection = Collection(collection_name, using=self._milvus_conn_alias)
            
            try:
                collection.load()
            except Exception as load_error:
                error_msg = str(load_error)
                if "can't change the index" in error_msg or "invalid parameter" in error_msg:
                    logger.warning(f"[向量索引检查] Collection schema 不兼容，删除重建: {collection_name}")
                    utility.drop_collection(collection_name, using=self._milvus_conn_alias)
                    return False
                raise
            
            count = collection.num_entities
            logger.info(f"[向量索引检查] Collection={collection_name}, 数据量={count}")
            return count > 0
        except Exception as e:
            logger.warning(f"[向量索引检查] 失败: {e}")
            return False

    async def has_vector_index_async(self) -> bool:
        """异步检查向量索引，避免阻塞事件循环。"""
        return await asyncio.to_thread(self.has_vector_index)
    
    async def add_entities(
        self,
        entities: List[EntityData],
        embedding_func: Optional[Callable] = None,
        save_immediately: bool = True,
    ) -> int:
        """
        增量添加实体
        
        Args:
            entities: 实体列表
            embedding_func: 嵌入函数
            save_immediately: 是否立即保存到 Redis（批量操作时设为 False）
        
        Returns: 
            添加的实体数量
        """
        if not entities:
            return 0
        
        added_count = 0
        newly_added_entities: List[EntityData] = []
        
        for entity in entities:
            if entity.entity_name not in self.entity_names:
                self.entity_names.append(entity.entity_name)
                self.entity_metadata[entity.entity_name] = {
                    "entity_type": entity.entity_type,
                    "description": entity.description,
                    "source_id": entity.source_id,
                    "file_path": entity.file_path,
                }
                added_count += 1
                newly_added_entities.append(entity)
        
        if added_count > 0:
            if self.bm25_index is None:
                self.bm25_index = BM25Index()
                self.bm25_index.build(self.entity_names)
            else:
                for entity in newly_added_entities:
                    if entity.entity_name:
                        self.bm25_index.add_document(entity.entity_name)
            
            if save_immediately:
                await self._save_to_redis()
            
            if embedding_func:
                await self._save_to_milvus(newly_added_entities, embedding_func)
        
        self.entity_count = len(self.entity_names)
        self.last_updated = datetime.now()
        self.last_accessed = datetime.now()
        
        return added_count
    
    async def _save_to_redis(self) -> None:
        """保存索引到 Redis"""
        try:
            entity_names = list(self.entity_names)
            entity_metadata = dict(self.entity_metadata)
            bm25_index = self.bm25_index

            def _save_sync() -> None:
                redis_client = self._get_redis_client()
                prefix = get_redis_key_prefix(self.kb_code)

                redis_client.delete(f"{prefix}:entity_names")
                if entity_names:
                    redis_client.rpush(f"{prefix}:entity_names", *[name.encode() for name in entity_names])

                for entity_name, metadata in entity_metadata.items():
                    key = f"{prefix}:entity_metadata:{entity_name}"
                    redis_client.hset(key, mapping={
                        "entity_type": metadata.get("entity_type", ""),
                        "description": metadata.get("description", ""),
                        "source_id": metadata.get("source_id", ""),
                        "file_path": metadata.get("file_path", ""),
                    })

                if bm25_index:
                    redis_client.set(f"{prefix}:bm25_corpus", bm25_index.serialize())

                redis_client.set(f"{prefix}:last_updated", str(int(time.time())))

            await asyncio.to_thread(_save_sync)
            
            logger.info(f"[Redis] 保存知识库 {self.kb_code}: {len(self.entity_names)} 个实体")
            
        except Exception as e:
            logger.warning(f"[Redis] 保存失败: kb={self.kb_code}, error={e}")
    
    async def _save_to_milvus(
        self,
        entities: List[EntityData],
        embedding_func: Callable,
    ) -> None:
        """保存向量到 Milvus（并发处理，只存储关键词和向量）"""
        import httpx
        
        try:
            await asyncio.to_thread(self._connect_milvus)
            
            collection_name = get_keyword_collection_name(self.kb_code)
            
            has_collection = await asyncio.to_thread(
                utility.has_collection,
                collection_name,
                using=self._milvus_conn_alias,
            )
            if not has_collection:
                await self._create_milvus_collection()
            
            collection = Collection(collection_name, using=self._milvus_conn_alias)
            
            batch_size = max(1, settings.keyword_index_insert_batch_size)
            max_concurrent = max(1, settings.keyword_index_insert_max_concurrency)
            total_entities = len(entities)
            inserted_count = 0
            
            embedding_api_base = settings.embedding_config.get("api_base") or ""
            embedding_api_key = settings.embedding_config.get("api_key") or ""
            embedding_model = settings.embedding_config.get("model") or "text-embedding-v3"
            
            logger.info(f"[Milvus] 开始并发插入向量, 总实体数: {total_entities}, 批大小: {batch_size}, 并发数: {max_concurrent}")
            
            semaphore = asyncio.Semaphore(max_concurrent)
            insert_lock = asyncio.Lock()
            
            async def process_batch(batch_entities: List[EntityData], batch_idx: int) -> int:
                nonlocal inserted_count
                async with semaphore:
                    entity_names = [e.entity_name for e in batch_entities]
                    
                    try:
                        if embedding_api_base and embedding_api_key:
                            async with httpx.AsyncClient(trust_env=False, timeout=120.0) as client:
                                response = await client.post(
                                    f"{embedding_api_base}/embeddings",
                                    headers={"Authorization": f"Bearer {embedding_api_key}"},
                                    json={"input": entity_names, "model": embedding_model},
                                )
                                response.raise_for_status()
                                data = response.json()
                                embeddings = [item["embedding"] for item in data["data"]]
                        else:
                            if asyncio.iscoroutinefunction(embedding_func):
                                embeddings = await embedding_func(entity_names)
                            else:
                                embeddings = await asyncio.to_thread(embedding_func, entity_names)
                            if isinstance(embeddings, np.ndarray):
                                embeddings = embeddings.tolist()
                        
                        ids = [hashlib.md5(name.encode()).hexdigest() for name in entity_names]
                        batch_data = [ids, entity_names, embeddings]
                        async with insert_lock:
                            await asyncio.to_thread(collection.insert, batch_data)
                        
                        count = len(ids)
                        inserted_count += count
                        
                        if inserted_count % 10000 == 0:
                            logger.info(f"[Milvus] 已插入 {inserted_count}/{total_entities} 条记录")
                        
                        return count
                    except Exception as e:
                        logger.warning(f"[Milvus] 批次 {batch_idx} embedding 失败: {e}")
                        return 0
            
            tasks = []
            for i in range(0, total_entities, batch_size):
                batch_entities = entities[i:i + batch_size]
                task = asyncio.create_task(process_batch(batch_entities, i // batch_size + 1))
                tasks.append(task)
            
            await asyncio.gather(*tasks)
            
            await asyncio.to_thread(collection.flush)
            
            logger.info(f"[Milvus] 插入完成: {inserted_count} 条记录到 {collection_name}")
            
        except Exception as e:
            logger.warning(f"[Milvus] 保存失败: kb={self.kb_code}, error={e}", exc_info=True)
    
    async def _create_milvus_collection(self) -> Collection:
        """创建 Milvus Collection（只存储关键词和向量）"""
        from pymilvus import CollectionSchema, DataType, FieldSchema

        await asyncio.to_thread(self._connect_milvus)

        collection_name = get_keyword_collection_name(self.kb_code)

        def _create_sync() -> None:
            if utility.has_collection(collection_name, using=self._milvus_conn_alias):
                old_collection = Collection(collection_name, using=self._milvus_conn_alias)
                old_fields = [f.name for f in old_collection.schema.fields]
                expected_fields = ["id", "entity_name", "vector"]
                if set(old_fields) != set(expected_fields):
                    logger.info(f"[Milvus] Collection schema 变更，删除旧 Collection: {collection_name}")
                    utility.drop_collection(collection_name, using=self._milvus_conn_alias)

            if not utility.has_collection(collection_name, using=self._milvus_conn_alias):
                fields = [
                    FieldSchema(name="id", dtype=DataType.VARCHAR, max_length=64, is_primary=True),
                    FieldSchema(name="entity_name", dtype=DataType.VARCHAR, max_length=512),
                    FieldSchema(name="vector", dtype=DataType.FLOAT_VECTOR, dim=self._embedding_dim),
                ]

                schema = CollectionSchema(
                    fields=fields,
                    description=f"关键词索引 - {self.kb_code}",
                )

                collection = Collection(
                    name=collection_name,
                    schema=schema,
                    using=self._milvus_conn_alias,
                )

                from backend.config.settings import settings
                index_type = settings.milvus_index_type.upper()
                if index_type == "HNSW":
                    index_params = {
                        "metric_type": "COSINE",
                        "index_type": "HNSW",
                        "params": {"M": settings.milvus_hnsw_m, "efConstruction": settings.milvus_hnsw_ef_construction},
                    }
                elif index_type == "DISKANN":
                    index_params = {
                        "metric_type": "COSINE",
                        "index_type": "DISKANN",
                        "params": {},
                    }
                elif index_type == "IVF_FLAT":
                    index_params = {
                        "metric_type": "COSINE",
                        "index_type": "IVF_FLAT",
                        "params": {"nlist": settings.milvus_ivf_nlist},
                    }
                elif index_type == "IVF_PQ":
                    index_params = {
                        "metric_type": "COSINE",
                        "index_type": "IVF_PQ",
                        "params": {
                            "nlist": settings.milvus_ivf_nlist,
                            "m": settings.milvus_pq_m,
                            "nbits": settings.milvus_pq_nbits,
                        },
                    }
                elif index_type == "IVF_SQ8":
                    index_params = {
                        "metric_type": "COSINE",
                        "index_type": "IVF_SQ8",
                        "params": {"nlist": settings.milvus_ivf_nlist},
                    }
                else:
                    index_params = {
                        "metric_type": "COSINE",
                        "index_type": "HNSW",
                        "params": {"M": 16, "efConstruction": 256},
                    }

                collection.create_index(field_name="vector", index_params=index_params)

                logger.info(f"[Milvus] Collection {collection_name} 创建成功，索引类型: {index_type}")

        await asyncio.to_thread(_create_sync)
        return await asyncio.to_thread(Collection, collection_name, using=self._milvus_conn_alias)
    
    async def clear(self) -> None:
        """清空索引"""
        self.entity_names = []
        self.entity_metadata = {}
        self.bm25_index = None
        self.entity_count = 0
        self.last_updated = None
        self._is_initialized = False
        
        try:
            prefix = get_redis_key_prefix(self.kb_code)

            def _clear_sync() -> None:
                redis_client = self._get_redis_client()
                keys = redis_client.keys(f"{prefix}:*")
                if keys:
                    redis_client.delete(*keys)

            await asyncio.to_thread(_clear_sync)
        except Exception as exc:
            logger.warning(
                "[关键词索引] 清空 Redis 键失败（已继续重置内存状态）: kb=%s error=%s",
                self.kb_code,
                exc,
            )

        logger.info(f"[关键词索引] 已清空: kb={self.kb_code}")
