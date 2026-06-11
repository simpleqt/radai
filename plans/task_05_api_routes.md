# 任务五：API 路由实现

> **文档状态**：📦 任务归档。现行 API 以 `http://<api_host>:<api_port>/docs` 与 [docs/api_quick_reference.md](../docs/api_quick_reference.md) 为准。

## 任务概述
实现知识库管理和文档操作的 REST API 接口，包括完整的 CRUD 操作、文档管理和查询功能。

## 前置条件
- 任务一（数据库设计）完成
- 任务二（NebulaGraph 存储）完成
- 任务三（知识库管理器）完成
- 任务四（RAG Service 修改）完成

---

## 现有架构分析

### 1. 路由注册模式

**文件**: [backend/api/http_server.py](backend/api/http_server.py)

```python
def create_app() -> FastAPI:
    from backend.api.routers.rag import router as rag_router
    # ... 其他路由
    
    app.include_router(rag_router, prefix="/api/v1/rag", tags=["RAG服务"])
    # ... 其他路由注册
    
    return app
```

### 2. 认证模式

**文件**: [backend/api/dependencies/auth.py](backend/api/dependencies/auth.py)

```python
class ApiKeyAuth(BaseModel):
    api_key: str
    user_id: str

async def verify_api_key(
    x_api_key: str | None = Header(None, alias="X-API-Key"),
    authorization: str | None = Header(None, alias="Authorization"),
) -> ApiKeyAuth:
    # 支持 X-API-Key 和 Bearer Token 两种方式
    # 开发环境支持 <dev_api_key_pattern> 前缀
    # 生产环境从数据库验证
```

### 3. 数据库会话模式

**文件**: [backend/db/connection.py](backend/db/connection.py)

```python
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

### 4. 现有 RAG 路由

**文件**: [backend/api/routers/rag.py](backend/api/routers/rag.py)

| 端点 | 方法 | 功能 |
|-----|------|------|
| `/health` | GET | 健康检查 |
| `/models` | GET | 获取可用模型列表 |
| `/query` | POST | RAG 查询 |
| `/insert` | POST | 插入内容列表 |
| `/insert_docx` | POST | 插入 DOCX 文档 |
| `/process_multimodal` | POST | 处理多模态文档 |
| `/delete_doc` | POST | 删除文档 |
| `/reset` | POST | 重置服务 |
| `/docs_status` | GET | 获取文档状态 |
| `/clear_failed_docs` | P__pycache__OST | 清除失败文档 |
| `/sync_to_mysql` | POST | 同步到 MySQL |

---

## 任务详情

### 1. 创建知识库管理路由

**文件位置**: `backend/api/routers/knowledge_base.py` (新建)

#### 1.1 完整路由实现

```python
"""
知识库管理 API 路由
"""

from typing import Any, Dict, List, Optional

from fastapi import APIRouter, Depends, HTTPException, Query
from pydantic import BaseModel, Field, field_validator
from sqlalchemy.ext.asyncio import AsyncSession

from backend.api.dependencies.auth import ApiKeyAuth, verify_api_key
from backend.config.loggers import get_logger
from backend.db.connection import get_async_session
from backend.db.models import KnowledgeBaseStatus
from backend.rag.knowledge_base import get_kb_manager

router = APIRouter(prefix="/knowledge_bases", tags=["知识库管理"])
logger = get_logger(__name__)


# ============== 请求/响应模型 ==============

class KnowledgeBaseCreate(BaseModel):
    kb_code: str = Field(..., min_length=1, max_length=50, description="知识库编码")
    kb_name: str = Field(..., min_length=1, max_length=100, description="知识库名称")
    subject: Optional[str] = Field(None, max_length=50, description="学科分类")
    description: Optional[str] = Field(None, description="描述")
    owner_id: Optional[str] = Field(None, description="所有者 ID")
    is_default: bool = Field(default=False, description="是否为默认知识库")

    @field_validator('kb_code')
    @classmethod
    def validate_kb_code(cls, v: str) -> str:
        if not v.replace('_', '').replace('-', '').isalnum():
            raise ValueError('kb_code 只能包含字母、数字、下划线和连字符')
        return v.lower()


class KnowledgeBaseUpdate(BaseModel):
    kb_name: Optional[str] = Field(None, max_length=100, description="知识库名称")
    subject: Optional[str] = Field(None, max_length=50, description="学科分类")
    description: Optional[str] = Field(None, description="描述")


class KnowledgeBaseResponse(BaseModel):
    kb_id: str
    kb_code: str
    kb_name: str
    subject: Optional[str] = None
    description: Optional[str] = None
    owner_id: Optional[str] = None
    status: str
    is_default: bool
    doc_count: int = 0
    entity_count: int = 0
    relation_count: int = 0
    created_at: str
    updated_at: str


class KnowledgeBaseListResponse(BaseModel):
    items: List[KnowledgeBaseResponse]
    total: int


class KnowledgeBaseStatsResponse(BaseModel):
    kb_id: str
    kb_code: str
    kb_name: str
    doc_count: int
    entity_count: int
    relation_count: int
    chunk_count: int
    storage_size_mb: float
    last_document_at: Optional[str] = None


# ============== 辅助函数 ==============

def kb_to_response(kb) -> KnowledgeBaseResponse:
    """将知识库模型转换为响应模型"""
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


# ============== API 端点 ==============

@router.post("", response_model=KnowledgeBaseResponse, status_code=201)
async def create_knowledge_base(
    request: KnowledgeBaseCreate,
    auth: ApiKeyAuth = Depends(verify_api_key),
    session: AsyncSession = Depends(get_async_session),
):
    """
    创建知识库
    
    创建一个新的知识库，系统会自动：
    1. 在 MySQL 中创建知识库记录
    2. 在 NebulaGraph 中创建对应的 Space
    3. 初始化 Milvus Collection（延迟创建）
    4. 初始化 Redis 前缀
    
    Args:
        request: 知识库创建请求
        
    Returns:
        创建的知识库信息
        
    Raises:
        400: kb_code 已存在或格式错误
        500: 创建存储资源失败
    """
    manager = get_kb_manager()
    try:
        kb = await manager.create_knowledge_base(
            session=session,
            kb_code=request.kb_code,
            kb_name=request.kb_name,
            subject=request.subject,
            description=request.description,
            owner_id=request.owner_id or auth.user_id,
            is_default=request.is_default,
        )
        logger.info(f"知识库创建成功: {kb.kb_id} ({kb.kb_code}) by user {auth.user_id}")
        return kb_to_response(kb)
    except ValueError as e:
        logger.warning(f"知识库创建失败(参数错误): {e}")
        raise HTTPException(status_code=400, detail=str(e))
    except RuntimeError as e:
        logger.error(f"知识库创建失败(运行时错误): {e}")
        raise HTTPException(status_code=500, detail=str(e))


@router.get("", response_model=KnowledgeBaseListResponse)
async def list_knowledge_bases(
    status: Optional[str] = Query(None, description="状态过滤 (active/inactive/deleting)"),
    owner_id: Optional[str] = Query(None, description="所有者过滤"),
    subject: Optional[str] = Query(None, description="学科分类过滤"),
    skip: int = Query(0, ge=0, description="跳过数量"),
    limit: int = Query(20, ge=1, le=100, description="返回数量"),
    auth: ApiKeyAuth = Depends(verify_api_key),
    session: AsyncSession = Depends(get_async_session),
):
    """
    列出知识库
    
    支持按状态、所有者、学科分类过滤，支持分页。
    
    Args:
        status: 状态过滤
        owner_id: 所有者过滤
        subject: 学科分类过滤
        skip: 分页偏移
        limit: 每页数量
        
    Returns:
        知识库列表和总数
    """
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
    """
    获取默认知识库
    
    返回系统默认知识库的信息。如果不存在，会自动创建。
    """
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
    """
    获取知识库详情
    
    Args:
        kb_id: 知识库 ID
        
    Returns:
        知识库详细信息
    """
    manager = get_kb_manager()
    kb = await manager.get_knowledge_base(session, kb_id)
    if not kb:
        raise HTTPException(status_code=404, detail="知识库不存在")
    return kb_to_response(kb)


@router.put("/{kb_id}", response_model=KnowledgeBaseResponse)
async def update_knowledge_base(
    kb_id: str,
    request: KnowledgeBaseUpdate,
    auth: ApiKeyAuth = Depends(verify_api_key),
    session: AsyncSession = Depends(get_async_session),
):
    """
    更新知识库信息
    
    只能更新知识库的名称、学科分类和描述，不能更改 kb_code。
    
    Args:
        kb_id: 知识库 ID
        request: 更新请求
        
    Returns:
        更新后的知识库信息
    """
    from backend.db.crud.knowledge_base_crud import KnowledgeBaseCRUD
    
    crud = KnowledgeBaseCRUD()
    kb = await crud.get_by_id(session, kb_id)
    if not kb:
        raise HTTPException(status_code=404, detail="知识库不存在")
    
    update_data = request.model_dump(exclude_unset=True)
    if not update_data:
        return kb_to_response(kb)
    
    updated_kb = await crud.update(session, kb_id, **update_data)
    logger.info(f"知识库更新成功: {kb_id} by user {auth.user_id}")
    return kb_to_response(updated_kb)


@router.delete("/{kb_id}")
async def delete_knowledge_base(
    kb_id: str,
    auth: ApiKeyAuth = Depends(verify_api_key),
    session: AsyncSession = Depends(get_async_session),
):
    """
    删除知识库
    
    危险操作！删除知识库将：
    1. 删除 NebulaGraph 中的 Space 及所有数据
    2. 删除 Milvus 中的 Collection
    3. 清理 Redis 中的所有数据
    4. 删除 MySQL 中的记录
    
    此操作不可恢复！
    
    Args:
        kb_id: 知识库 ID
        
    Returns:
        删除结果
    """
    manager = get_kb_manager()
    try:
        success = await manager.delete_knowledge_base(session, kb_id)
        if not success:
            raise HTTPException(status_code=404, detail="知识库不存在")
        logger.info(f"知识库删除成功: {kb_id} by user {auth.user_id}")
        return {"success": True, "kb_id": kb_id}
    except ValueError as e:
        raise HTTPException(status_code=400, detail=str(e))


@router.post("/{kb_id}/switch")
async def switch_knowledge_base(
    kb_id: str,
    auth: ApiKeyAuth = Depends(verify_api_key),
    session: AsyncSession = Depends(get_async_session),
):
    """
    切换当前知识库
    
    将当前会话的知识库切换到指定的知识库。
    后续操作将默认使用此知识库。
    
    Args:
        kb_id: 目标知识库 ID
        
    Returns:
        切换结果
    """
    manager = get_kb_manager()
    try:
        kb = await manager.switch_knowledge_base(session, kb_id)
        logger.info(f"知识库切换: {kb_id} by user {auth.user_id}")
        return {
            "success": True, 
            "current_kb_id": kb.kb_id, 
            "kb_code": kb.kb_code,
            "kb_name": kb.kb_name,
        }
    except ValueError as e:
        raise HTTPException(status_code=400, detail=str(e))


@router.get("/{kb_id}/stats", response_model=KnowledgeBaseStatsResponse)
async def get_knowledge_base_stats(
    kb_id: str,
    auth: ApiKeyAuth = Depends(verify_api_key),
    session: AsyncSession = Depends(get_async_session),
):
    """
    获取知识库统计信息
    
    返回知识库的详细统计数据，包括：
    - 文档数量
    - 实体数量
    - 关系数量
    - Chunk 数量
    - 存储大小
    
    Args:
        kb_id: 知识库 ID
        
    Returns:
        统计信息
    """
    manager = get_kb_manager()
    kb = await manager.get_knowledge_base(session, kb_id)
    if not kb:
        raise HTTPException(status_code=404, detail="知识库不存在")
    
    # 更新统计信息
    updated_kb = await manager.update_knowledge_base_stats(session, kb_id)
    
    return KnowledgeBaseStatsResponse(
        kb_id=updated_kb.kb_id,
        kb_code=updated_kb.kb_code,
        kb_name=updated_kb.kb_name,
        doc_count=updated_kb.doc_count or 0,
        entity_count=updated_kb.entity_count or 0,
        relation_count=updated_kb.relation_count or 0,
        chunk_count=updated_kb.chunk_count or 0,
        storage_size_mb=0.0,  # TODO: 计算实际存储大小
        last_document_at=updated_kb.updated_at.isoformat() if updated_kb.updated_at else None,
    )
```

### 2. 创建文档管理路由

**文件位置**: `backend/api/routers/knowledge_base_documents.py` (新建)

```python
"""
知识库文档管理 API 路由
"""

from pathlib import Path
from typing import Any, Dict, List, Optional

from fastapi import APIRouter, BackgroundTasks, Depends, File, HTTPException, Query, UploadFile
from pydantic import BaseModel, Field
from sqlalchemy.ext.asyncio import AsyncSession

from backend.api.dependencies.auth import ApiKeyAuth, verify_api_key
from backend.config.loggers import get_logger
from backend.db.connection import get_async_session
from backend.rag.knowledge_base import get_kb_manager
from backend.rag.service import get_rag_service

router = APIRouter(tags=["知识库文档"])
logger = get_logger(__name__)


# ============== 请求/响应模型 ==============

class DocumentResponse(BaseModel):
    id: int
    kb_id: str
    doc_id: str
    file_name: str
    file_path: Optional[str] = None
    file_size: Optional[int] = None
    parse_status: int = 0
    storage_status: int = 0
    error_message: Optional[str] = None
    chunk_count: int = 0
    entity_count: int = 0
    created_at: str
    updated_at: Optional[str] = None


class DocumentListResponse(BaseModel):
    items: List[DocumentResponse]
    total: int


class DocumentUploadResponse(BaseModel):
    success: bool
    kb_id: str
    doc_id: Optional[str] = None
    file_name: str
    message: Optional[str] = None


class DocumentDeleteResponse(BaseModel):
    success: bool
    kb_id: str
    doc_id: str
    message: Optional[str] = None


# ============== 辅助函数 ==============

def doc_to_response(doc) -> DocumentResponse:
    """将文档模型转换为响应模型"""
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
        updated_at=doc.updated_at.isoformat() if doc.updated_at else None,
    )


# ============== 后台任务 ==============

async def process_document_task(
    kb_id: str,
    file_path: str,
    file_name: str,
):
    """
    后台文档处理任务
    
    Args:
        kb_id: 知识库 ID
        file_path: 文件路径
        file_name: 文件名
    """
    try:
        service = get_rag_service()
        
        # 根据文件类型选择处理方法
        suffix = Path(file_path).suffix.lower()
        
        if suffix == '.docx':
            result = await service.insert_docx(
                file_path=file_path,
                kb_id=kb_id,
            )
        else:
            # 其他文件类型使用 process_multimodal
            result = await service.process_multimodal(
                file_path=file_path,
                kb_id=kb_id,
            )
        
        logger.info(f"文档处理完成: {kb_id}/{file_name}, result={result.get('success')}")
        
    except Exception as e:
        logger.exception(f"文档处理失败: {kb_id}/{file_name}, error={e}")


# ============== API 端点 ==============

@router.post(
    "/knowledge_bases/{kb_id}/documents", 
    response_model=DocumentUploadResponse,
    status_code=202,
)
async def upload_document_to_kb(
    kb_id: str,
    background_tasks: BackgroundTasks,
    file: UploadFile = File(..., description="上传的文件"),
    auth: ApiKeyAuth = Depends(verify_api_key),
    session: AsyncSession = Depends(get_async_session),
):
    """
    上传文档到知识库
    
    支持的文件格式：pdf, docx, txt, md, png, jpg, jpeg
    
    文件上传后会异步处理入库，处理状态可通过文档列表接口查询。
    
    Args:
        kb_id: 知识库 ID
        file: 上传的文件
        
    Returns:
        上传结果（处理在后台进行）
    """
    # 验证知识库存在
    manager = get_kb_manager()
    kb = await manager.get_knowledge_base(session, kb_id)
    if not kb:
        raise HTTPException(status_code=404, detail="知识库不存在")
    
    if kb.status.value != "active":
        raise HTTPException(status_code=400, detail=f"知识库状态异常: {kb.status}")
    
    # 验证文件类型
    allowed_extensions = {'.pdf', '.docx', '.txt', '.md', '.png', '.jpg', '.jpeg'}
    file_ext = Path(file.filename).suffix.lower() if file.filename else ''
    
    if file_ext not in allowed_extensions:
        raise HTTPException(
            status_code=400, 
            detail=f"不支持的文件类型: {file_ext}，支持: {', '.join(allowed_extensions)}"
        )
    
    # 保存文件
    import os
    import tempfile
    
    upload_dir = Path(os.getenv("UPLOAD_DIR", "/tmp/uploads")) / kb_id
    upload_dir.mkdir(parents=True, exist_ok=True)
    
    file_path = upload_dir / file.filename
    
    try:
        content = await file.read()
        file_path.write_bytes(content)
        
        logger.info(f"文件上传成功: {kb_id}/{file.filename}, size={len(content)}")
        
        # 添加后台处理任务
        background_tasks.add_task(
            process_document_task,
            kb_id=kb_id,
            file_path=str(file_path),
            file_name=file.filename or "unknown",
        )
        
        return DocumentUploadResponse(
            success=True,
            kb_id=kb_id,
            file_name=file.filename or "unknown",
            message="文件上传成功，正在后台处理入库",
        )
        
    except Exception as e:
        logger.exception(f"文件上传失败: {e}")
        raise HTTPException(status_code=500, detail=f"文件上传失败: {str(e)}")


@router.get(
    "/knowledge_bases/{kb_id}/documents", 
    response_model=DocumentListResponse,
)
async def list_kb_documents(
    kb_id: str,
    parse_status: Optional[int] = Query(None, ge=0, le=3, description="解析状态过滤"),
    storage_status: Optional[int] = Query(None, ge=0, le=3, description="存储状态过滤"),
    skip: int = Query(0, ge=0, description="跳过数量"),
    limit: int = Query(20, ge=1, le=100, description="返回数量"),
    auth: ApiKeyAuth = Depends(verify_api_key),
    session: AsyncSession = Depends(get_async_session),
):
    """
    获取知识库文档列表
    
    Args:
        kb_id: 知识库 ID
        parse_status: 解析状态过滤 (0=待处理, 1=处理中, 2=成功, 3=失败)
        storage_status: 存储状态过滤
        skip: 分页偏移
        limit: 每页数量
        
    Returns:
        文档列表和总数
    """
    manager = get_kb_manager()
    
    # 验证知识库存在
    kb = await manager.get_knowledge_base(session, kb_id)
    if not kb:
        raise HTTPException(status_code=404, detail="知识库不存在")
    
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


@router.get(
    "/knowledge_bases/{kb_id}/documents/{doc_id}", 
    response_model=DocumentResponse,
)
async def get_kb_document(
    kb_id: str,
    doc_id: str,
    auth: ApiKeyAuth = Depends(verify_api_key),
    session: AsyncSession = Depends(get_async_session),
):
    """
    获取文档详情
    
    Args:
        kb_id: 知识库 ID
        doc_id: 文档 ID
        
    Returns:
        文档详细信息
    """
    from backend.db.crud.knowledge_base_crud import KnowledgeBaseDocumentCRUD
    
    crud = KnowledgeBaseDocumentCRUD()
    doc = await crud.get_by_doc_id(session, kb_id, doc_id)
    
    if not doc:
        raise HTTPException(status_code=404, detail="文档不存在")
    
    return doc_to_response(doc)


@router.delete(
    "/knowledge_bases/{kb_id}/documents/{doc_id}",
    response_model=DocumentDeleteResponse,
)
async def delete_document_from_kb(
    kb_id: str,
    doc_id: str,
    delete_llm_cache: bool = Query(False, description="是否删除 LLM 缓存"),
    auth: ApiKeyAuth = Depends(verify_api_key),
    session: AsyncSession = Depends(get_async_session),
):
    """
    从知识库删除文档
    
    删除文档将：
    1. 从 LightRAG 存储中删除相关数据
    2. 从 MySQL 记录中删除
    
    Args:
        kb_id: 知识库 ID
        doc_id: 文档 ID
        delete_llm_cache: 是否同时删除 LLM 缓存
        
    Returns:
        删除结果
    """
    manager = get_kb_manager()
    
    # 验证知识库存在
    kb = await manager.get_knowledge_base(session, kb_id)
    if not kb:
        raise HTTPException(status_code=404, detail="知识库不存在")
    
    try:
        # 从 RAG 服务删除文档
        service = get_rag_service()
        result = await service.delete_doc_by_id(
            doc_id=doc_id,
            delete_llm_cache=delete_llm_cache,
            kb_id=kb_id,
        )
        
        # 从知识库记录中移除
        await manager.remove_document(session, kb_id, doc_id)
        
        logger.info(f"文档删除成功: {kb_id}/{doc_id} by user {auth.user_id}")
        
        return DocumentDeleteResponse(
            success=True,
            kb_id=kb_id,
            doc_id=doc_id,
            message="文档删除成功",
        )
        
    except Exception as e:
        logger.exception(f"文档删除失败: {kb_id}/{doc_id}")
        raise HTTPException(status_code=500, detail=f"文档删除失败: {str(e)}")
```

### 3. 修改现有 RAG 路由

**文件位置**: `backend/api/routers/rag.py` (修改)

#### 3.1 添加 kb_id 参数

```python
class RagQueryRequest(BaseModel):
    query: str = Field(..., description="用户查询")
    mode: str = Field(default="mix", description="查询模式")
    kb_id: str | None = Field(default=None, description="知识库 ID")
    kwargs: Dict[str, Any] = Field(default_factory=dict, description="额外参数")


class RagInsertRequest(BaseModel):
    content_list: List[Dict[str, Any]] = Field(..., description="OCR content_list")
    file_path: str = Field(default="unknown_document", description="来源文件路径")
    kb_id: str | None = Field(default=None, description="知识库 ID")
    kwargs: Dict[str, Any] = Field(default_factory=dict, description="额外参数")


class RagInsertDocxRequest(BaseModel):
    file_path: str = Field(..., description="DOCX 文件路径")
    kb_id: str | None = Field(default=None, description="知识库 ID")
    kwargs: Dict[str, Any] = Field(default_factory=dict, description="额外参数")


class RagProcessMultimodalRequest(BaseModel):
    file_path: str = Field(..., description="文件路径")
    kb_id: str | None = Field(default=None, description="知识库 ID")
    kwargs: Dict[str, Any] = Field(default_factory=dict, description="额外参数")


class RagDeleteDocRequest(BaseModel):
    doc_id: str = Field(..., description="需要删除的 doc_id")
    delete_llm_cache: bool = Field(default=False, description="是否删除 LLM 缓存")
    kb_id: str | None = Field(default=None, description="知识库 ID")


class RagResetRequest(BaseModel):
    new_work_dir: str | None = Field(default=None, description="新的 RAG_WORK_DIR")
    clear_old_work_dir: bool = Field(default=True, description="是否清空旧工作目录")
    kb_id: str | None = Field(default=None, description="知识库 ID")
```

#### 3.2 新增跨知识库查询接口

```python
class RagMultiQueryRequest(BaseModel):
    query: str = Field(..., description="用户查询")
    kb_ids: List[str] = Field(..., description="知识库 ID 列表")
    mode: str = Field(default="mix", description="查询模式")
    kwargs: Dict[str, Any] = Field(default_factory=dict, description="额外参数")


class RagMultiQueryResponse(BaseModel):
    results: Dict[str, Dict[str, Any]] = Field(
        default_factory=dict, 
        description="各知识库的查询结果"
    )


@router.post("/query_multiple", response_model=RagMultiQueryResponse)
async def rag_query_multiple(
    request: RagMultiQueryRequest,
    auth: ApiKeyAuth = Depends(verify_api_key),
):
    """
    跨知识库查询
    
    在多个知识库中执行相同查询，返回各知识库的结果。
    结果以 kb_id 为键，包含每个知识库的查询结果。
    
    Args:
        request: 跨知识库查询请求
        
    Returns:
        Dict[kb_id, query_result]
    """
    service = get_rag_service()
    try:
        results = await service.query_multiple_kbs(
            query_text=request.query,
            kb_ids=request.kb_ids,
            mode=request.mode,
            **request.kwargs,
        )
        return RagMultiQueryResponse(results=results)
    except Exception as e:
        logger.exception(f"跨知识库查询失败: {e}")
        raise HTTPException(status_code=500, detail=f"跨知识库查询失败: {str(e)}")
```

### 4. 注册路由

**文件位置**: `backend/api/http_server.py` (修改)

```python
def create_app() -> FastAPI:
    from backend.api.routers.document_ingestion import router as document_ingestion_router
    from backend.api.routers.document_processing import router as document_processing_router
    from backend.api.routers.extraction import router as extraction_router
    from backend.api.routers.knowledge_base import router as knowledge_base_router
    from backend.api.routers.knowledge_base_documents import router as kb_documents_router
    from backend.api.routers.mcp import router as mcp_router
    from backend.api.routers.rag import router as rag_router

    # 现有路由
    app.include_router(
        document_processing_router, prefix="/api/v1/documents", tags=["文档处理"]
    )
    app.include_router(
        extraction_router, prefix="/api/v1/extraction", tags=["内容抽取"]
    )
    app.include_router(rag_router, prefix="/api/v1/rag", tags=["RAG服务"])
    app.include_router(
    )
    app.include_router(
        document_ingestion_router, prefix="/api/v1/ingestion", tags=["文档入库"]
    )
    app.include_router(mcp_router, prefix="/api/v1/mcp", tags=["MCP服务"])
    
    # 新增：知识库管理路由
    app.include_router(
        knowledge_base_router, 
        prefix="/api/v1", 
        tags=["知识库管理"]
    )
    app.include_router(
        kb_documents_router, 
        prefix="/api/v1", 
        tags=["知识库文档"]
    )

    # ... 其余代码保持不变
```

---

## API 端点汇总

### 知识库管理 API

| 方法 | 路径 | 说明 |
|-----|------|------|
| POST | `/api/v1/knowledge_bases` | 创建知识库 |
| GET | `/api/v1/knowledge_bases` | 列出知识库 |
| GET | `/api/v1/knowledge_bases/default` | 获取默认知识库 |
| GET | `/api/v1/knowledge_bases/{kb_id}` | 获取知识库详情 |
| PUT | `/api/v1/knowledge_bases/{kb_id}` | 更新知识库 |
| DELETE | `/api/v1/knowledge_bases/{kb_id}` | 删除知识库 |
| POST | `/api/v1/knowledge_bases/{kb_id}/switch` | 切换知识库 |
| POST | `/api/v1/knowledge_bases/{kb_id}/stats` | 获取统计信息 |

### 文档管理 API

| 方法 | 路径 | 说明 |
|-----|------|------|
| POST | `/api/v1/knowledge_bases/{kb_id}/documents` | 上传文档 |
| GET | `/api/v1/knowledge_bases/{kb_id}/documents` | 列出文档 |
| GET | `/api/v1/knowledge_bases/{kb_id}/documents/{doc_id}` | 获取文档详情 |
| DELETE | `/api/v1/knowledge_bases/{kb_id}/documents/{doc_id}` | 删除文档 |

### RAG 查询 API (修改)

| 方法 | 路径 | 说明 |
|-----|------|------|
| POST | `/api/v1/rag/query` | 单知识库查询（支持 kb_id） |
| POST | `/api/v1/rag/query_multiple` | 跨知识库查询（新增） |
| POST | `/api/v1/rag/insert` | 插入内容（支持 kb_id） |
| POST | `/api/v1/rag/insert_docx` | 插入 DOCX（支持 kb_id） |
| POST | `/api/v1/rag/process_multimodal` | 处理多模态（支持 kb_id） |
| POST | `/api/v1/rag/delete_doc` | 删除文档（支持 kb_id） |
| POST | `/api/v1/rag/reset` | 重置服务（支持 kb_id） |

---

## 需要创建/修改的文件

| 文件 | 操作 | 说明 |
|-----|------|------|
| `backend/api/routers/knowledge_base.py` | 新建 | 知识库管理路由 |
| `backend/api/routers/knowledge_base_documents.py` | 新建 | 文档管理路由 |
| `backend/api/routers/rag.py` | 修改 | 添加 kb_id 参数和跨知识库查询 |
| `backend/api/http_server.py` | 修改 | 注册新路由 |

---

## 验收标准

- [ ] 所有 API 端点实现完成
- [ ] API 文档自动生成（Swagger UI: `/docs`）
- [ ] 请求参数验证正确
- [ ] 错误处理完善（400/401/404/500）
- [ ] 权限控制正确（API Key 验证）
- [ ] 后台任务正常执行
- [ ] API 测试通过

---

## 依赖关系

- 依赖任务一（数据库设计）
- 依赖任务二（NebulaGraph 存储）
- 依赖任务三（知识库管理器）
- 依赖任务四（RAG Service 修改）

---

## 预计工期

2 天

---

## 测试策略

### 1. 单元测试

```python
import pytest
from httpx import AsyncClient
from backend.api.http_server import app

@pytest.mark.asyncio
async def test_create_knowledge_base():
    """测试创建知识库"""
    async with AsyncClient(app=app, base_url="http://test") as client:
        response = await client.post(
            "/api/v1/knowledge_bases",
            json={
                "kb_code": "test_kb",
                "kb_name": "测试知识库",
                "description": "测试用知识库",
            },
            headers={"X-API-Key": "<test_api_key>"},
        )
        assert response.status_code == 201
        data = response.json()
        assert data["kb_code"] == "test_kb"
        assert data["kb_name"] == "测试知识库"


@pytest.mark.asyncio
async def test_list_knowledge_bases():
    """测试列出知识库"""
    async with AsyncClient(app=app, base_url="http://test") as client:
        response = await client.get(
            "/api/v1/knowledge_bases",
            headers={"X-API-Key": "<test_api_key>"},
        )
        assert response.status_code == 200
        data = response.json()
        assert "items" in data
        assert "total" in data


@pytest.mark.asyncio
async def test_query_with_kb_id():
    """测试指定知识库查询"""
    async with AsyncClient(app=app, base_url="http://test") as client:
        response = await client.post(
            "/api/v1/rag/query",
            json={
                "query": "测试查询",
                "mode": "mix",
                "kb_id": "kb_123",
            },
            headers={"X-API-Key": "<test_api_key>"},
        )
        # 注意：实际测试需要 mock RAG 服务
        assert response.status_code in [200, 500]
```

### 2. 集成测试

```python
@pytest.mark.asyncio
async def test_full_kb_workflow():
    """测试完整知识库工作流"""
    async with AsyncClient(app=app, base_url="http://test") as client:
        headers = {"X-API-Key": "<test_api_key>"}
        
        # 1. 创建知识库
        create_resp = await client.post(
            "/api/v1/knowledge_bases",
            json={
                "kb_code": "workflow_test",
                "kb_name": "工作流测试知识库",
            },
            headers=headers,
        )
        assert create_resp.status_code == 201
        kb_id = create_resp.json()["kb_id"]
        
        # 2. 获取知识库详情
        get_resp = await client.get(
            f"/api/v1/knowledge_bases/{kb_id}",
            headers=headers,
        )
        assert get_resp.status_code == 200
        
        # 3. 列出知识库
        list_resp = await client.get(
            "/api/v1/knowledge_bases",
            headers=headers,
        )
        assert list_resp.status_code == 200
        assert any(kb["kb_id"] == kb_id for kb in list_resp.json()["items"])
        
        # 4. 删除知识库
        delete_resp = await client.delete(
            f"/api/v1/knowledge_bases/{kb_id}",
            headers=headers,
        )
        assert delete_resp.status_code == 200
```

### 3. API 文档测试

访问 `/docs` 查看 Swagger UI 文档，确保：
- 所有端点正确显示
- 请求/响应模型正确
- 参数描述清晰
- 示例值合理
