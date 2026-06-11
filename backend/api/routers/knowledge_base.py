"""知识库管理 API 路由"""

from typing import List, Optional

from fastapi import APIRouter, Depends, HTTPException, Query
from pydantic import BaseModel, Field
from sqlalchemy.ext.asyncio import AsyncSession

from backend.api.dependencies.auth import ApiKeyAuth, get_db_session, require_admin
from backend.config.loggers import get_logger
from backend.db.models import KnowledgeBase, KnowledgeBaseDocument, KnowledgeBaseStatus
from backend.rag.knowledge_base import get_kb_manager

router = APIRouter(prefix="/knowledge_bases", tags=["知识库管理"])
logger = get_logger(__name__)


class KnowledgeBaseCreateRequest(BaseModel):
    """创建知识库请求"""

    kb_code: str = Field(..., description="知识库编码", min_length=1, max_length=50)
    kb_name: str = Field(..., description="知识库名称", min_length=1, max_length=100)
    subject: Optional[str] = Field(None, description="学科分类", max_length=50)
    description: Optional[str] = Field(None, description="描述")
    owner_id: Optional[str] = Field(None, description="所有者 ID")
    is_default: bool = Field(False, description="是否为默认知识库")


class KnowledgeBaseResponse(BaseModel):
    """知识库响应"""

    kb_id: str
    kb_code: str
    kb_name: str
    subject: Optional[str] = None
    description: Optional[str] = None
    owner_id: Optional[str] = None
    nebula_space: Optional[str] = None
    milvus_collection_prefix: Optional[str] = None
    redis_key_prefix: Optional[str] = None
    doc_count: int = 0
    entity_count: int = 0
    relation_count: int = 0
    chunk_count: int = 0
    status: str
    is_default: bool = False
    created_at: str
    updated_at: str


class KnowledgeBaseListResponse(BaseModel):
    """知识库列表响应"""

    items: List[KnowledgeBaseResponse]
    total: int


class DocumentResponse(BaseModel):
    """文档响应"""

    id: int
    kb_id: str
    doc_id: str
    file_name: str
    file_path: Optional[str] = None
    document_date: Optional[str] = None
    parse_status: int = 0
    storage_status: int = 0
    error_message: Optional[str] = None
    chunk_count: int = 0
    entity_count: int = 0
    relation_count: int = 0
    created_at: str
    updated_at: Optional[str] = None


class DocumentListResponse(BaseModel):
    """文档列表响应"""

    items: List[DocumentResponse]
    total: int


def kb_to_response(kb: KnowledgeBase) -> KnowledgeBaseResponse:
    """将知识库对象转换为响应模型"""
    return KnowledgeBaseResponse(
        kb_id=kb.kb_id,
        kb_code=kb.kb_code,
        kb_name=kb.kb_name,
        subject=kb.subject,
        description=kb.description,
        owner_id=kb.owner_id,
        nebula_space=kb.nebula_space,
        milvus_collection_prefix=kb.milvus_collection_prefix,
        redis_key_prefix=kb.redis_key_prefix,
        doc_count=kb.doc_count,
        entity_count=kb.entity_count,
        relation_count=kb.relation_count,
        chunk_count=kb.chunk_count,
        status=kb.status.value if kb.status else "creating",
        is_default=kb.is_default,
        created_at=kb.created_at.isoformat() if kb.created_at else "",
        updated_at=kb.updated_at.isoformat() if kb.updated_at else "",
    )


def doc_to_response(doc: KnowledgeBaseDocument) -> DocumentResponse:
    """将文档对象转换为响应模型"""
    return DocumentResponse(
        id=doc.id,
        kb_id=doc.kb_id,
        doc_id=doc.doc_id,
        file_name=doc.file_name,
        file_path=doc.file_path,
        document_date=doc.document_date.isoformat() if doc.document_date else None,
        parse_status=doc.parse_status,
        storage_status=doc.storage_status,
        error_message=doc.error_message,
        chunk_count=doc.chunk_count or 0,
        entity_count=doc.entity_count or 0,
        relation_count=doc.relation_count or 0,
        created_at=doc.created_at.isoformat() if doc.created_at else "",
        updated_at=doc.updated_at.isoformat() if doc.updated_at else "",
    )


@router.post("", response_model=KnowledgeBaseResponse)
async def create_knowledge_base(
    request: KnowledgeBaseCreateRequest,
    auth: ApiKeyAuth = Depends(require_admin),
    session: AsyncSession = Depends(get_db_session),
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
    owner_id: Optional[str] = Query(None, description="所有者 ID"),
    skip: int = Query(0, ge=0, description="跳过数量"),
    limit: int = Query(20, ge=1, le=100, description="返回数量"),
    auth: ApiKeyAuth = Depends(require_admin),
    session: AsyncSession = Depends(get_db_session),
):
    """列出知识库"""
    manager = get_kb_manager()
    
    status_enum = None
    if status:
        try:
            status_enum = KnowledgeBaseStatus(status)
        except ValueError:
            raise HTTPException(status_code=400, detail=f"无效的状态值: {status}")
    
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
    auth: ApiKeyAuth = Depends(require_admin),
    session: AsyncSession = Depends(get_db_session),
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
    auth: ApiKeyAuth = Depends(require_admin),
    session: AsyncSession = Depends(get_db_session),
):
    """获取知识库详情"""
    manager = get_kb_manager()
    kb = await manager.get_knowledge_base(session, kb_id)
    if not kb:
        raise HTTPException(status_code=404, detail="知识库不存在")
    return kb_to_response(kb)


class KnowledgeBaseUpdateRequest(BaseModel):
    """更新知识库请求"""

    kb_name: Optional[str] = Field(None, description="知识库名称", max_length=100)
    subject: Optional[str] = Field(None, description="学科分类", max_length=50)
    description: Optional[str] = Field(None, description="描述")


@router.put("/{kb_id}", response_model=KnowledgeBaseResponse)
async def update_knowledge_base(
    kb_id: str,
    request: KnowledgeBaseUpdateRequest,
    auth: ApiKeyAuth = Depends(require_admin),
    session: AsyncSession = Depends(get_db_session),
):
    """更新知识库信息"""
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
    auth: ApiKeyAuth = Depends(require_admin),
    session: AsyncSession = Depends(get_db_session),
):
    """删除知识库"""
    manager = get_kb_manager()
    try:
        success = await manager.delete_knowledge_base(session, kb_id)
        if not success:
            raise HTTPException(status_code=404, detail="知识库不存在")
        return {"success": True, "message": f"知识库 {kb_id} 已删除"}
    except ValueError as e:
        raise HTTPException(status_code=400, detail=str(e))


@router.post("/{kb_id}/switch", response_model=KnowledgeBaseResponse)
async def switch_knowledge_base(
    kb_id: str,
    auth: ApiKeyAuth = Depends(require_admin),
    session: AsyncSession = Depends(get_db_session),
):
    """切换当前知识库"""
    manager = get_kb_manager()
    try:
        kb = await manager.switch_knowledge_base(session, kb_id)
        return kb_to_response(kb)
    except ValueError as e:
        raise HTTPException(status_code=400, detail=str(e))


@router.get("/{kb_id}/documents", response_model=DocumentListResponse)
async def list_documents(
    kb_id: str,
    parse_status: Optional[int] = Query(None, description="解析状态过滤"),
    storage_status: Optional[int] = Query(None, description="入库状态过滤"),
    skip: int = Query(0, ge=0, description="跳过数量"),
    limit: int = Query(20, ge=1, le=100, description="返回数量"),
    auth: ApiKeyAuth = Depends(require_admin),
    session: AsyncSession = Depends(get_db_session),
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


@router.post("/{kb_id}/stats", response_model=KnowledgeBaseResponse)
async def update_knowledge_base_stats(
    kb_id: str,
    auth: ApiKeyAuth = Depends(require_admin),
    session: AsyncSession = Depends(get_db_session),
):
    """更新知识库统计信息"""
    manager = get_kb_manager()
    kb = await manager.update_knowledge_base_stats(session, kb_id)
    if not kb:
        raise HTTPException(status_code=404, detail="知识库不存在")
    return kb_to_response(kb)
