# 任务三：知识库管理器

> **文档状态**：📦 任务归档。现行见 `backend/rag/knowledge_base/manager.py`。

## 任务概述
实现知识库管理器，负责多知识库的创建、切换、删除等生命周期管理，以及 RAG 实例的缓存和协调。

## 前置条件
- 任务一（数据库设计）完成
- 任务二（NebulaGraph 存储）完成

---

## 现有架构分析

### RAG 服务架构
项目使用 `RagService` 单例管理 RAG 实例：

```python
# backend/rag/service.py
class RagService:
    def __init__(self):
        self._initialized = False
        self._rag: RAGAnything | None = None
        self._rag_work_dir: str | None = None
```

### 数据库连接模式
项目使用异步 SQLAlchemy 会话：

```python
# backend/db/connection.py
async def get_async_session() -> AsyncGenerator[AsyncSession, None]:
    factory = get_async_session_factory()
    async with factory() as session:
        try:
            yield session
            await session.commit()
        except Exception:
            await session.rollback()
            raise
```

### CRUD 操作模式
项目使用静态方法 + 单例实例模式：

```python
# backend/db/crud/document_crud.py
class DocumentCRUD:
    @staticmethod
    async def create(...) -> Document: ...
    
    @staticmethod
    async def get_by_id(...) -> Document | None: ...

document_crud = DocumentCRUD()
```

### API 路由模式
项目使用 FastAPI 依赖注入：

```python
# backend/api/routers/rag.py
@router.post("/query", response_model=RagQueryResponse)
async def rag_query(
    request: RagQueryRequest,
    auth: ApiKeyAuth = Depends(verify_api_key),
):
    service = get_rag_service()
    ...
```

---

## 任务详情

### 1. 创建知识库管理器模块

**文件位置**: `backend/rag/knowledge_base/__init__.py` (新建)

```python
"""
知识库管理模块

提供多知识库的创建、切换、删除等生命周期管理
"""

from backend.rag.knowledge_base.manager import KnowledgeBaseManager, get_kb_manager

__all__ = ["KnowledgeBaseManager", "get_kb_manager"]
```

### 2. 实现知识库管理器

**文件位置**: `backend/rag/knowledge_base/manager.py` (新建)

```python
"""
知识库管理器

负责：
- 知识库的创建、删除、切换
- RAG 实例的缓存和管理
- 存储资源的协调
"""

import asyncio
import os
from dataclasses import dataclass, field
from datetime import datetime
from pathlib import Path
from typing import Any, Dict, List, Optional, Tuple

from sqlalchemy.ext.asyncio import AsyncSession

from backend.config.loggers import get_logger
from backend.config.settings import settings
from backend.db.models import KnowledgeBase, KnowledgeBaseDocument, KnowledgeBaseStatus
from backend.db.crud.knowledge_base_crud import KnowledgeBaseCRUD, KnowledgeBaseDocumentCRUD
from backend.rag.raganything.config import RAGAnythingConfig
from backend.rag.raganything.raganything import RAGAnything

logger = get_logger(__name__)

_kb_manager: Optional["KnowledgeBaseManager"] = None


def get_kb_manager() -> "KnowledgeBaseManager":
    """获取知识库管理器单例"""
    global _kb_manager
    if _kb_manager is None:
        _kb_manager = KnowledgeBaseManager()
    return _kb_manager


@dataclass
class RAGInstance:
    """RAG 实例包装"""
    kb_id: str
    kb_code: str
    rag: RAGAnything
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
        self._current_kb_id: Optional[str] = None
        self._lock = asyncio.Lock()
        self._max_cached_instances = int(os.getenv("KB_MAX_CACHED_INSTANCES", "10"))
    
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
        4. 初始化 Milvus Collection（延迟创建）
        5. 初始化 Redis 前缀
        6. 更新 MySQL 记录（状态：active）
        
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
        
        nebula_storage = NebulaGraphStorage(
            namespace=kb.kb_code,
            workspace=kb.kb_code,
        )
        try:
            await nebula_storage.initialize()
            logger.info(f"NebulaGraph Space 创建成功: {kb.kb_code}")
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
        
        nebula_storage = NebulaGraphStorage(
            namespace=kb.kb_code,
            workspace=kb.kb_code,
        )
        try:
            await nebula_storage.initialize()
            await nebula_storage.drop()
            logger.info(f"NebulaGraph Space 删除成功: {kb.kb_code}")
        except Exception as e:
            logger.warning(f"NebulaGraph Space 删除警告: {e}")
        finally:
            await nebula_storage.finalize()
        
        await self._clear_redis_data(kb.kb_code)
        await self._drop_milvus_collections(kb.kb_code)
        
        logger.info(f"存储资源清理完成: {kb.kb_code}")
    
    async def _clear_redis_data(self, kb_code: str) -> None:
        """清理 Redis 数据"""
        try:
            import redis
            redis_uri = os.getenv("REDIS_URI") or os.getenv("REDIS_URL")
            if not redis_uri:
                return
            
            client = redis.from_url(redis_uri)
            pattern = f"{kb_code}:*"
            keys = client.keys(pattern)
            if keys:
                client.delete(*keys)
                logger.info(f"Redis 数据清理完成: {kb_code}, 删除 {len(keys)} 个键")
        except Exception as e:
            logger.warning(f"Redis 数据清理失败: {e}")
    
    async def _drop_milvus_collections(self, kb_code: str) -> None:
        """删除 Milvus Collection"""
        try:
            from pymilvus import connections, utility
            
            milvus_uri = os.getenv("MILVUS_URI")
            if not milvus_uri:
                return
            
            connections.connect(uri=milvus_uri)
            
            for suffix in ["entities", "relationships", "chunks"]:
                collection_name = f"{kb_code}_{suffix}"
                if utility.has_collection(collection_name):
                    utility.drop_collection(collection_name)
                    logger.info(f"Milvus Collection 删除: {collection_name}")
            
            connections.disconnect("default")
            logger.info(f"Milvus Collection 清理完成: {kb_code}")
        except Exception as e:
            logger.warning(f"Milvus Collection 清理失败: {e}")
    
    async def get_knowledge_base(
        self,
        session: AsyncSession,
        kb_id: str,
    ) -> Optional[KnowledgeBase]:
        """获取知识库信息"""
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
    ) -> RAGAnything:
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
        async with self._lock:
            if kb_id in self._rag_instances:
                instance = self._rag_instances[kb_id]
                instance.last_used_at = datetime.now()
                return instance.rag
            
            kb = await self.get_knowledge_base(session, kb_id)
            if not kb:
                raise ValueError(f"知识库不存在: {kb_id}")
            
            if kb.status != KnowledgeBaseStatus.ACTIVE:
                raise ValueError(f"知识库未激活: {kb_id}, 状态: {kb.status}")
            
            if len(self._rag_instances) >= self._max_cached_instances:
                await self._evict_lru_instance()
            
            rag = await self._create_rag_instance(kb)
            
            self._rag_instances[kb_id] = RAGInstance(
                kb_id=kb_id,
                kb_code=kb.kb_code,
                rag=rag,
            )
            
            logger.info(f"RAG 实例创建并缓存: {kb_id} ({kb.kb_code})")
            return rag
    
    async def _create_rag_instance(self, kb: KnowledgeBase) -> RAGAnything:
        """
        创建 RAG 实例
        
        Args:
            kb: 知识库对象
            
        Returns:
            RAG 实例
        """
        from lightrag.utils import EmbeddingFunc
        
        rag_work_dir = os.getenv("RAG_WORK_DIR", "./rag_storage")
        
        embedding_config = settings.embedding_config
        embedding_api_base = embedding_config.get("api_base") or os.getenv("EMBEDDING_API_BASE")
        embedding_api_key = embedding_config.get("api_key") or os.getenv("EMBEDDING_API_KEY")
        embedding_model = embedding_config.get("model") or os.getenv("EMBEDDING_MODEL")
        embedding_dim = embedding_config.get("dim") or int(os.getenv("EMBEDDING_DIM", "1024"))
        
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
        
        config = RAGAnythingConfig(
            working_dir=rag_work_dir,
        )
        
        lightrag_kwargs = {
            "workspace": kb.kb_code,
        }
        
        redis_uri = os.getenv("REDIS_URI") or os.getenv("REDIS_URL")
        if redis_uri:
            lightrag_kwargs.update({
                "kv_storage": "RedisKVStorage",
                "doc_status_storage": "RedisDocStatusStorage",
            })
        
        if os.getenv("MILVUS_URI"):
            lightrag_kwargs["vector_storage"] = "MilvusVectorDBStorage"
        
        lightrag_kwargs["graph_storage"] = "NebulaGraphStorage"
        
        rag = RAGAnything(
            config=config,
            llm_model_func=self._build_llm_model_func(),
            embedding_func=embedding_func,
            lightrag_kwargs=lightrag_kwargs,
        )
        
        await rag._ensure_lightrag_initialized()
        
        return rag
    
    def _build_embedding_func(self, api_base: str, api_key: str, model: str):
        """构建嵌入函数"""
        import httpx
        
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
        llm_config = settings.active_llm_config
        api_base = llm_config.get("base_url", "")
        api_key = llm_config.get("api_key", "")
        model = llm_config.get("model", "")
        
        async def llm_func(prompt: str, **kwargs) -> str:
            import httpx
            
            async with httpx.AsyncClient(timeout=300.0) as client:
                response = await client.post(
                    f"{api_base}/chat/completions",
                    headers={"Authorization": f"Bearer {api_key}"},
                    json={
                        "model": model,
                        "messages": [{"role": "user", "content": prompt}],
                        **kwargs,
                    },
                )
                response.raise_for_status()
                return response.json()["choices"][0]["message"]["content"]
        
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
        
        kb = await crud.update_stats(
            session=session,
            kb_id=kb_id,
            doc_count=doc_count,
            entity_count=total_entities,
        )
        
        logger.info(f"知识库统计更新: {kb_id}, docs={doc_count}, entities={total_entities}")
        return kb
    
    async def add_document(
        self,
        session: AsyncSession,
        kb_id: str,
        doc_id: str,
        file_name: str,
        file_path: Optional[str] = None,
        file_size: Optional[int] = None,
    ) -> KnowledgeBaseDocument:
        """
        添加文档到知识库
        
        Args:
            session: 数据库会话
            kb_id: 知识库 ID
            doc_id: 文档 ID (LightRAG doc_id)
            file_name: 文件名
            file_path: 文件路径
            file_size: 文件大小
            
        Returns:
            创建的文档记录
        """
        crud = KnowledgeBaseDocumentCRUD()
        
        existing = await crud.get_by_doc_id(session, kb_id, doc_id)
        if existing:
            logger.warning(f"文档已存在: {kb_id}/{doc_id}")
            return existing
        
        doc = await crud.create(
            session=session,
            kb_id=kb_id,
            doc_id=doc_id,
            file_name=file_name,
            file_path=file_path,
            file_size=file_size,
        )
        
        logger.info(f"文档添加到知识库: {kb_id}/{doc_id}")
        return doc
    
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
        crud = KnowledgeBaseDocumentCRUD()
        return await crud.update_status(
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
        """
        从知识库移除文档
        
        Args:
            session: 数据库会话
            kb_id: 知识库 ID
            doc_id: 文档 ID
            
        Returns:
            是否移除成功
        """
        crud = KnowledgeBaseDocumentCRUD()
        return await crud.delete(session, kb_id, doc_id)
    
    async def list_documents(
        self,
        session: AsyncSession,
        kb_id: str,
        parse_status: Optional[int] = None,
        storage_status: Optional[int] = None,
        skip: int = 0,
        limit: int = 20,
    ) -> Tuple[List[KnowledgeBaseDocument], int]:
        """列出知识库的文档"""
        crud = KnowledgeBaseDocumentCRUD()
        return await crud.list_by_kb(
            session=session,
            kb_id=kb_id,
            parse_status=parse_status,
            storage_status=storage_status,
            skip=skip,
            limit=limit,
        )
    
    async def close(self) -> None:
        """关闭管理器，清理所有资源"""
        async with self._lock:
            for kb_id, instance in list(self._rag_instances.items()):
                try:
                    await instance.rag.finalize_storages()
                    logger.info(f"RAG 实例关闭: {kb_id}")
                except Exception as e:
                    logger.warning(f"RAG 实例关闭失败: {e}")
            
            self._rag_instances.clear()
            self._current_kb_id = None
            logger.info("知识库管理器已关闭")
```

### 3. 创建 API 路由

**文件位置**: `backend/api/routers/knowledge_base.py` (新建)

```python
"""
知识库管理 API 路由
"""

from typing import Any, Dict, List, Optional

from fastapi import APIRouter, Depends, HTTPException, Query
from pydantic import BaseModel, Field
from sqlalchemy.ext.asyncio import AsyncSession

from backend.api.dependencies.auth import ApiKeyAuth, verify_api_key
from backend.config.loggers import get_logger
from backend.db.connection import get_async_session
from backend.db.models import KnowledgeBaseStatus
from backend.rag.knowledge_base import get_kb_manager

router = APIRouter(prefix="/knowledge_bases", tags=["Knowledge Base"])
logger = get_logger(__name__)


class KnowledgeBaseCreate(BaseModel):
    kb_code: str = Field(..., min_length=1, max_length=50, description="知识库编码")
    kb_name: str = Field(..., min_length=1, max_length=100, description="知识库名称")
    subject: Optional[str] = Field(None, max_length=50, description="学科分类")
    description: Optional[str] = Field(None, description="描述")
    owner_id: Optional[str] = Field(None, description="所有者 ID")
    is_default: bool = Field(default=False, description="是否为默认知识库")


class KnowledgeBaseResponse(BaseModel):
    kb_id: str
    kb_code: str
    kb_name: str
    subject: Optional[str]
    description: Optional[str]
    owner_id: Optional[str]
    status: str
    is_default: bool
    doc_count: int
    entity_count: int
    relation_count: int
    created_at: str
    updated_at: str


class KnowledgeBaseListResponse(BaseModel):
    items: List[KnowledgeBaseResponse]
    total: int


class DocumentResponse(BaseModel):
    id: int
    kb_id: str
    doc_id: str
    file_name: str
    file_path: Optional[str]
    file_size: Optional[int]
    parse_status: int
    storage_status: int
    error_message: Optional[str]
    chunk_count: int
    entity_count: int
    created_at: str


class DocumentListResponse(BaseModel):
    items: List[DocumentResponse]
    total: int


def kb_to_response(kb) -> KnowledgeBaseResponse:
    return KnowledgeBaseResponse(
        kb_id=kb.kb_id,
        kb_code=kb.kb_code,
        kb_name=kb.kb_name,
        subject=kb.subject,
        description=kb.description,
        owner_id=kb.owner_id,
        status=kb.status.value if hasattr(kb.status, 'value') else str(kb.status),
        is_default=kb.is_default,
        doc_count=kb.doc_count or 0,
        entity_count=kb.entity_count or 0,
        relation_count=kb.relation_count or 0,
        created_at=kb.created_at.isoformat() if kb.created_at else "",
        updated_at=kb.updated_at.isoformat() if kb.updated_at else "",
    )


def doc_to_response(doc) -> DocumentResponse:
    return DocumentResponse(
        id=doc.id,
        kb_id=doc.kb_id,
        doc_id=doc.doc_id,
        file_name=doc.file_name,
        file_path=doc.file_path,
        file_size=doc.file_size,
        parse_status=doc.parse_status or 0,
        storage_status=doc.storage_status or 0,
        error_message=doc.error_message,
        chunk_count=doc.chunk_count or 0,
        entity_count=doc.entity_count or 0,
        created_at=doc.created_at.isoformat() if doc.created_at else "",
    )


@router.post("", response_model=KnowledgeBaseResponse)
async def create_knowledge_base(
    request: KnowledgeBaseCreate,
    auth: ApiKeyAuth = Depends(verify_api_key),
    session: AsyncSession = Depends(get_async_session),
):
    """创建知识库"""
    manager = get_kb_manager()
    try:
        kb = await manager.create_knowledge_base(
            session=session,
            kb_code=request.kb_code,
            kb_name=request.kb_name,
            subject=request.subject,
            description=request.description,
            owner_id=request.owner_id,
            is_default=request.is_default,
        )
        return kb_to_response(kb)
    except ValueError as e:
        raise HTTPException(status_code=400, detail=str(e))
    except RuntimeError as e:
        raise HTTPException(status_code=500, detail=str(e))


@router.get("", response_model=KnowledgeBaseListResponse)
async def list_knowledge_bases(
    status: Optional[str] = Query(None, description="状态过滤"),
    owner_id: Optional[str] = Query(None, description="所有者过滤"),
    skip: int = Query(0, ge=0),
    limit: int = Query(20, ge=1, le=100),
    auth: ApiKeyAuth = Depends(verify_api_key),
    session: AsyncSession = Depends(get_async_session),
):
    """列出知识库"""
    manager = get_kb_manager()
    
    status_enum = None
    if status:
        try:
            status_enum = KnowledgeBaseStatus(status)
        except ValueError:
            raise HTTPException(status_code=400, detail=f"无效的状态: {status}")
    
    kbs, total = await manager.list_knowledge_bases(
        session=session,
        status=status_enum,
        owner_id=owner_id,
        skip=skip,
        limit=limit,
    )
    
    return KnowledgeBaseListResponse(
        items=[kb_to_response(kb) for kb in kbs],
        total=total,
    )


@router.get("/default", response_model=KnowledgeBaseResponse)
async def get_default_knowledge_base(
    auth: ApiKeyAuth = Depends(verify_api_key),
    session: AsyncSession = Depends(get_async_session),
):
    """获取默认知识库"""
    manager = get_kb_manager()
    kb = await manager.get_default_knowledge_base(session)
    if not kb:
        raise HTTPException(status_code=404, detail="默认知识库不存在")
    return kb_to_response(kb)


@router.get("/{kb_id}", response_model=KnowledgeBaseResponse)
async def get_knowledge_base(
    kb_id: str,
    auth: ApiKeyAuth = Depends(verify_api_key),
    session: AsyncSession = Depends(get_async_session),
):
    """获取知识库详情"""
    manager = get_kb_manager()
    kb = await manager.get_knowledge_base(session, kb_id)
    if not kb:
        raise HTTPException(status_code=404, detail="知识库不存在")
    return kb_to_response(kb)


@router.delete("/{kb_id}")
async def delete_knowledge_base(
    kb_id: str,
    auth: ApiKeyAuth = Depends(verify_api_key),
    session: AsyncSession = Depends(get_async_session),
):
    """删除知识库"""
    manager = get_kb_manager()
    try:
        success = await manager.delete_knowledge_base(session, kb_id)
        if not success:
            raise HTTPException(status_code=404, detail="知识库不存在")
        return {"success": True, "kb_id": kb_id}
    except ValueError as e:
        raise HTTPException(status_code=400, detail=str(e))


@router.post("/{kb_id}/switch")
async def switch_knowledge_base(
    kb_id: str,
    auth: ApiKeyAuth = Depends(verify_api_key),
    session: AsyncSession = Depends(get_async_session),
):
    """切换当前知识库"""
    manager = get_kb_manager()
    try:
        kb = await manager.switch_knowledge_base(session, kb_id)
        return {"success": True, "current_kb_id": kb.kb_id, "kb_code": kb.kb_code}
    except ValueError as e:
        raise HTTPException(status_code=400, detail=str(e))


@router.get("/{kb_id}/documents", response_model=DocumentListResponse)
async def list_documents(
    kb_id: str,
    parse_status: Optional[int] = Query(None),
    storage_status: Optional[int] = Query(None),
    skip: int = Query(0, ge=0),
    limit: int = Query(20, ge=1, le=100),
    auth: ApiKeyAuth = Depends(verify_api_key),
    session: AsyncSession = Depends(get_async_session),
):
    """列出知识库文档"""
    manager = get_kb_manager()
    docs, total = await manager.list_documents(
        session=session,
        kb_id=kb_id,
        parse_status=parse_status,
        storage_status=storage_status,
        skip=skip,
        limit=limit,
    )
    return DocumentListResponse(
        items=[doc_to_response(doc) for doc in docs],
        total=total,
    )


@router.post("/{kb_id}/stats")
async def update_knowledge_base_stats(
    kb_id: str,
    auth: ApiKeyAuth = Depends(verify_api_key),
    session: AsyncSession = Depends(get_async_session),
):
    """更新知识库统计信息"""
    manager = get_kb_manager()
    kb = await manager.update_knowledge_base_stats(session, kb_id)
    if not kb:
        raise HTTPException(status_code=404, detail="知识库不存在")
    return kb_to_response(kb)
```

### 4. 注册路由

**文件位置**: `backend/api/http_server.py` (修改)

在路由注册部分添加：

```python
from backend.api.routers.knowledge_base import router as kb_router

app.include_router(kb_router, prefix="/api/v1")
```

### 5. 更新 CRUD 导出

**文件位置**: `backend/db/crud/__init__.py` (修改)

```python
"""CRUD 模块"""

from backend.db.crud.document_crud import DocumentCRUD, document_crud
from backend.db.crud.extraction_crud import ExtractionCRUD, extraction_crud
from backend.db.crud.task_crud import TaskCRUD, task_crud
from backend.db.crud.file_info_crud import FileInfoCRUD, get_file_info_crud
from backend.db.crud.knowledge_base_crud import (
    KnowledgeBaseCRUD,
    KnowledgeBaseDocumentCRUD,
    knowledge_base_crud,
    kb_document_crud,
)

__all__ = [
    "document_crud",
    "DocumentCRUD",
    "task_crud",
    "TaskCRUD",
    "extraction_crud",
    "ExtractionCRUD",
    "FileInfoCRUD",
    "get_file_info_crud",
    "KnowledgeBaseCRUD",
    "KnowledgeBaseDocumentCRUD",
    "knowledge_base_crud",
    "kb_document_crud",
]
```

---

## 文件清单

| 操作 | 文件路径 | 说明 |
|-----|---------|------|
| 新建 | `backend/rag/knowledge_base/__init__.py` | 模块初始化 |
| 新建 | `backend/rag/knowledge_base/manager.py` | 知识库管理器 |
| 新建 | `backend/api/routers/knowledge_base.py` | API 路由 |
| 修改 | `backend/api/http_server.py` | 注册路由 |
| 修改 | `backend/db/crud/__init__.py` | 更新导出 |

---

## API 接口

| 方法 | 路径 | 说明 |
|-----|------|------|
| POST | `/api/v1/knowledge_bases` | 创建知识库 |
| GET | `/api/v1/knowledge_bases` | 列出知识库 |
| GET | `/api/v1/knowledge_bases/default` | 获取默认知识库 |
| GET | `/api/v1/knowledge_bases/{kb_id}` | 获取知识库详情 |
| DELETE | `/api/v1/knowledge_bases/{kb_id}` | 删除知识库 |
| POST | `/api/v1/knowledge_bases/{kb_id}/switch` | 切换知识库 |
| GET | `/api/v1/knowledge_bases/{kb_id}/documents` | 列出文档 |
| POST | `/api/v1/knowledge_bases/{kb_id}/stats` | 更新统计 |

---

## 验收标准

### 功能验收
- [ ] 知识库创建流程完整（MySQL + NebulaGraph）
- [ ] 知识库删除流程完整（含数据清理）
- [ ] RAG 实例缓存机制正常
- [ ] LRU 淘汰机制正常
- [ ] API 接口响应正确

### 性能验收
- [ ] RAG 实例复用正常
- [ ] 并发访问安全（异步锁）
- [ ] 无内存泄漏

### 单元测试
- [ ] 知识库 CRUD 测试通过
- [ ] RAG 实例管理测试通过
- [ ] API 接口测试通过
- [ ] 测试覆盖率 > 80%

---

## 执行步骤

```bash
# 1. 创建模块目录
mkdir -p backend/rag/knowledge_base

# 2. 运行数据库初始化（任务一）
cd backend
python -m backend.db.init_knowledge_base

# 3. 运行单元测试
pytest tests/test_knowledge_base_manager.py -v

# 4. 启动服务测试 API
uvicorn backend.api.http_server:app --reload --port 8000
```

---

## 依赖关系
- 依赖任务一（数据库设计）
- 依赖任务二（NebulaGraph 存储）

## 预计工期
2 天
