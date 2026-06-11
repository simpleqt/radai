"""
知识库文档管理 API 路由
"""

import os
import uuid
from io import BytesIO
from collections import defaultdict
from pathlib import Path
from typing import Any, Dict, List, Optional

from fastapi import APIRouter, Body, Depends, File, HTTPException, Query, UploadFile
from pydantic import BaseModel
from sqlalchemy import func, select, update
from sqlalchemy.ext.asyncio import AsyncSession

from backend.api.dependencies.auth import ApiKeyAuth, get_db_session, require_admin
from backend.config import settings
from backend.config.loggers import get_logger
from backend.db.models import KnowledgeBase, KnowledgeBaseDocument, PendingFile
from backend.rag.knowledge_base import get_kb_manager
from backend.rag.service import get_rag_service
from backend.storage import _get_minio_client

router = APIRouter(tags=["知识库文档"])
logger = get_logger(__name__)

class DocumentResponse(BaseModel):
    """文档响应"""

    id: int
    kb_id: str
    doc_id: str
    file_name: str
    file_path: Optional[str] = None
    document_date: Optional[str] = None
    category_path: Optional[str] = None
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


class DocumentUploadResponse(BaseModel):
    """文档上传响应"""

    success: bool
    kb_id: str
    doc_id: Optional[str] = None
    file_name: str
    message: Optional[str] = None


class DocumentDeleteResponse(BaseModel):
    """文档删除响应"""

    success: bool
    kb_id: str
    doc_id: str
    message: Optional[str] = None


class KnowledgeBaseStatsResponse(BaseModel):
    """知识库统计响应"""

    kb_id: str
    kb_code: str
    kb_name: str
    doc_count: int
    entity_count: int
    relation_count: int
    chunk_count: int
    storage_size_mb: float
    last_document_at: Optional[str] = None


class DocumentMoveRequest(BaseModel):
    """文档移动请求"""
    category_path: Optional[str] = None


class BatchMoveRequest(BaseModel):
    """批量移动请求"""
    doc_ids: List[str]
    category_path: Optional[str] = None


class BatchDeleteRequest(BaseModel):
    """批量删除请求"""
    doc_ids: List[str]
    delete_llm_cache: bool = False


class BatchDeleteItemResult(BaseModel):
    """批量删除单项结果"""
    doc_id: str
    success: bool
    message: Optional[str] = None


class BatchDeleteResponse(BaseModel):
    """批量删除响应"""
    success: bool
    kb_id: str
    total: int
    deleted: int
    failed: int
    results: List[BatchDeleteItemResult]


class DirectoryTreeNode(BaseModel):
    """目录树节点"""
    path: str
    name: str
    doc_count: int
    children: List["DirectoryTreeNode"] = []


class DirectoryTreeResponse(BaseModel):
    """目录树响应"""
    tree: List[DirectoryTreeNode]
    total_docs: int


DirectoryTreeNode.model_rebuild()


def doc_to_response(doc: KnowledgeBaseDocument) -> DocumentResponse:
    """将文档模型转换为响应模型"""
    return DocumentResponse(
        id=doc.id,
        kb_id=doc.kb_id,
        doc_id=doc.doc_id,
        file_name=doc.file_name,
        file_path=doc.file_path,
        document_date=doc.document_date.isoformat() if doc.document_date else None,
        category_path=doc.category_path if hasattr(doc, "category_path") else None,
        parse_status=doc.parse_status or 0,
        storage_status=doc.storage_status or 0,
        error_message=doc.error_message,
        chunk_count=doc.chunk_count or 0,
        entity_count=doc.entity_count or 0,
        relation_count=doc.relation_count or 0,
        created_at=doc.created_at.isoformat() if doc.created_at else "",
        updated_at=doc.updated_at.isoformat() if doc.updated_at else None,
    )


def _normalize_category_path(category_path: str) -> str:
    return "/".join(part for part in (category_path or "").split("/") if part).strip()


def _upload_to_minio(content: bytes, minio_path: str, content_type: str = "application/octet-stream") -> None:
    """将上传内容写入 MinIO，失败时抛出异常。"""
    client = _get_minio_client()
    client.put_object(
        settings.minio_bucket_name,
        minio_path,
        BytesIO(content),
        length=len(content),
        content_type=content_type,
    )


# ──────────────────────────────────────────────────────────────────────────────
# 目录树 API（兼容旧接口，推荐使用 /knowledge_bases/{kb_id}/directories）
# ──────────────────────────────────────────────────────────────────────────────

@router.get(
    "/knowledge_bases/{kb_id}/documents/tree",
    response_model=DirectoryTreeResponse,
    deprecated=True,
)
async def get_document_tree(
    kb_id: str,
    auth: ApiKeyAuth = Depends(require_admin),
    session: AsyncSession = Depends(get_db_session),
):
    """
    获取知识库文档目录树（已废弃，请使用 /knowledge_bases/{kb_id}/directories）

    返回按 category_path 聚合的多层目录结构，节点 doc_count 包含所有子节点文档数。
    """
    from backend.services.directory_service import directory_service
    
    manager = get_kb_manager()
    kb = await manager.get_knowledge_base(session, kb_id)
    if not kb:
        raise HTTPException(status_code=404, detail="知识库不存在")

    total_stmt = select(func.count()).where(KnowledgeBaseDocument.kb_id == kb_id)
    total_result = await session.execute(total_stmt)
    total_docs = total_result.scalar() or 0

    tree_data = await directory_service.get_directory_tree(session, kb_id)
    
    def _to_tree_node(node: Dict[str, Any]) -> DirectoryTreeNode:
        return DirectoryTreeNode(
            path=node["path"],
            name=node["name"],
            doc_count=node.get("total_doc_count", node.get("doc_count", 0)),
            children=[_to_tree_node(child) for child in node.get("children", [])],
        )
    
    tree_nodes = [_to_tree_node(node) for node in tree_data]
    
    uncategorized_count = total_docs - sum(n.doc_count for n in tree_nodes)
    if uncategorized_count > 0:
        tree_nodes.insert(0, DirectoryTreeNode(
            path="",
            name="未分类",
            doc_count=uncategorized_count,
            children=[],
        ))

    return DirectoryTreeResponse(
        tree=tree_nodes,
        total_docs=total_docs,
    )


def _flatten_tree_nodes(nodes: List[Dict[str, Any]]) -> List[Dict[str, Any]]:
    """递归将 children 也转换为 DirectoryTreeNode 格式"""
    result = []
    for n in nodes:
        n["children"] = _flatten_tree_nodes(n.get("children", []))
        result.append(n)
    return result


# ──────────────────────────────────────────────────────────────────────────────
# 文档上传 API
# ──────────────────────────────────────────────────────────────────────────────

@router.post(
    "/knowledge_bases/{kb_id}/documents",
    response_model=DocumentUploadResponse,
    status_code=202,
)
async def upload_document_to_kb(
    kb_id: str,
    file: UploadFile = File(..., description="上传的文件"),
    auth: ApiKeyAuth = Depends(require_admin),
    session: AsyncSession = Depends(get_db_session),
):
    """
    上传文档到知识库

    支持的文件格式：pdf, docx, txt, md, png, jpg, jpeg

    文件上传后仅进入待入库队列，需点击“启动任务”后才会开始入库。
    """
    manager = get_kb_manager()
    kb = await manager.get_knowledge_base(session, kb_id)
    if not kb:
        raise HTTPException(status_code=404, detail="知识库不存在")

    if kb.status.value != "active":
        raise HTTPException(status_code=400, detail=f"知识库状态异常: {kb.status}")

    allowed_extensions = {'.pdf', '.docx', '.txt', '.md', '.png', '.jpg', '.jpeg'}
    file_ext = Path(file.filename).suffix.lower() if file.filename else ''

    if file_ext not in allowed_extensions:
        raise HTTPException(
            status_code=400,
            detail=f"不支持的文件类型: {file_ext}，支持: {', '.join(allowed_extensions)}"
        )

    try:
        file_name = file.filename or "unknown"
        content = await file.read()
        file_id = str(uuid.uuid4())
        upload_id = str(uuid.uuid4())
        minio_path = f"pending/{kb_id}/{file_id}/{file_name}"
        content_type = file.content_type or "application/octet-stream"

        _upload_to_minio(content, minio_path, content_type)

        pending_file = PendingFile(
            upload_id=upload_id,
            kb_id=kb_id,
            file_id=file_id,
            file_name=file_name,
            file_size=len(content),
            file_type=content_type,
            minio_path=minio_path,
            status=0,
            created_by=auth.user_id,
        )
        session.add(pending_file)
        await session.commit()

        logger.info(f"文件上传成功并加入待入库队列: {kb_id}/{file_name}, file_id={file_id}, size={len(content)}")

        return DocumentUploadResponse(
            success=True,
            kb_id=kb_id,
            file_name=file_name,
            message="文件上传成功，等待启动任务入库",
        )

    except Exception as e:
        logger.exception(f"文件上传失败: {e}")
        raise HTTPException(status_code=500, detail=f"文件上传失败: {str(e)}")


# ──────────────────────────────────────────────────────────────────────────────
# 文档列表 API
# ──────────────────────────────────────────────────────────────────────────────

@router.get(
    "/knowledge_bases/{kb_id}/documents",
    response_model=DocumentListResponse,
)
async def list_kb_documents(
    kb_id: str,
    parse_status: Optional[int] = Query(None, ge=0, le=3, description="解析状态过滤"),
    storage_status: Optional[int] = Query(None, ge=0, le=3, description="存储状态过滤"),
    category_path: Optional[str] = Query(None, description="精确分类路径过滤（空字符串=未分类）"),
    category_prefix: Optional[str] = Query(None, description="分类路径前缀过滤（含子目录）"),
    skip: int = Query(0, ge=0, description="跳过数量"),
    limit: int = Query(20, ge=1, le=100, description="返回数量"),
    auth: ApiKeyAuth = Depends(require_admin),
    session: AsyncSession = Depends(get_db_session),
):
    """
    获取知识库文档列表

    支持按分类路径精确过滤（category_path）或前缀过滤（category_prefix，含子目录）。
    两个参数同时存在时 category_path 优先。
    """
    manager = get_kb_manager()
    kb = await manager.get_knowledge_base(session, kb_id)
    if not kb:
        raise HTTPException(status_code=404, detail="知识库不存在")

    # 构建查询
    stmt = select(KnowledgeBaseDocument).where(KnowledgeBaseDocument.kb_id == kb_id)

    # DEBUG: 先查该知识库总文档数
    _base_count_stmt = select(func.count()).where(KnowledgeBaseDocument.kb_id == kb_id)
    _base_total = (await session.execute(_base_count_stmt)).scalar() or 0

    if parse_status is not None:
        stmt = stmt.where(KnowledgeBaseDocument.parse_status == parse_status)
    if storage_status is not None:
        stmt = stmt.where(KnowledgeBaseDocument.storage_status == storage_status)

    if category_path is not None:
        if category_path == "":
            from sqlalchemy import or_
            stmt = stmt.where(
                or_(
                    KnowledgeBaseDocument.category_path.is_(None),
                    KnowledgeBaseDocument.category_path == "",
                )
            )
            # 根目录下仅显示没有日期的文档 (带日期的显示在日期子目录中)
            stmt = stmt.where(KnowledgeBaseDocument.document_date.is_(None))
        else:
            # 检测是否包含日期后缀 (虚拟目录格式 folder/YYYY-MM-DD)
            import re
            from datetime import datetime
            date_pattern = r"(^|/)(\d{4}-\d{2}-\d{2})$"
            match = re.search(date_pattern, category_path)
            
            if match:
                date_str = match.group(2)
                # 获取日期前的目录路径
                pure_path = category_path[:match.start()].rstrip("/")
                
                try:
                    target_date = datetime.strptime(date_str, "%Y-%m-%d").date()
                    from sqlalchemy import or_
                    if pure_path:
                        stmt = stmt.where(
                            KnowledgeBaseDocument.category_path == pure_path,
                            KnowledgeBaseDocument.document_date == target_date
                        )
                    else:
                        # 根目录下的日期文件夹
                        stmt = stmt.where(
                            or_(
                                KnowledgeBaseDocument.category_path.is_(None),
                                KnowledgeBaseDocument.category_path == ""
                            ),
                            KnowledgeBaseDocument.document_date == target_date
                        )
                except ValueError:
                    # 不是有效的日期格式，退回到普通精确匹配
                    stmt = stmt.where(KnowledgeBaseDocument.category_path == category_path)
            else:
                stmt = stmt.where(KnowledgeBaseDocument.category_path == category_path)
                # 目录层级仅显示没有日期的文档 (带日期的文档显示在对应的日期子文件夹中)
                stmt = stmt.where(KnowledgeBaseDocument.document_date.is_(None))
    elif category_prefix:
        from sqlalchemy import or_
        stmt = stmt.where(
            or_(
                KnowledgeBaseDocument.category_path == category_prefix,
                KnowledgeBaseDocument.category_path.like(f"{category_prefix}/%"),
            )
        )

    # 计数
    count_stmt = select(func.count()).select_from(stmt.subquery())
    total_result = await session.execute(count_stmt)
    total = total_result.scalar() or 0

    logger.info(
        f"[文档列表过滤] kb_id={kb_id[:8]}... | 基础总数={_base_total} | "
        f"category_path={category_path!r} | category_prefix={category_prefix!r} | "
        f"parse_status={parse_status} | storage_status={storage_status} | "
        f"过滤后总数={total}"
    )

    # 分页
    stmt = stmt.order_by(KnowledgeBaseDocument.created_at.desc()).offset(skip).limit(limit)
    result = await session.execute(stmt)
    docs = result.scalars().all()

    return DocumentListResponse(
        items=[doc_to_response(doc) for doc in docs],
        total=total,
    )


# ──────────────────────────────────────────────────────────────────────────────
# 文档详情 API
# ──────────────────────────────────────────────────────────────────────────────

@router.get(
    "/knowledge_bases/{kb_id}/documents/{doc_id}",
    response_model=DocumentResponse,
)
async def get_kb_document(
    kb_id: str,
    doc_id: str,
    auth: ApiKeyAuth = Depends(require_admin),
    session: AsyncSession = Depends(get_db_session),
):
    """获取文档详情"""
    from backend.db.crud.knowledge_base_crud import KnowledgeBaseDocumentCRUD

    crud = KnowledgeBaseDocumentCRUD()
    doc = await crud.get_by_doc_id(session, kb_id, doc_id)

    if not doc:
        raise HTTPException(status_code=404, detail="文档不存在")

    return doc_to_response(doc)


# ──────────────────────────────────────────────────────────────────────────────
# 文档目录修改（单条）API
# ──────────────────────────────────────────────────────────────────────────────

@router.patch(
    "/knowledge_bases/{kb_id}/documents/{doc_id}",
    response_model=DocumentResponse,
)
async def update_document_category(
    kb_id: str,
    doc_id: str,
    body: DocumentMoveRequest,
    auth: ApiKeyAuth = Depends(require_admin),
    session: AsyncSession = Depends(get_db_session),
):
    """
    修改文档分类路径（移动文档到新目录）

    传入 category_path=null 或 "" 可将文档移回"未分类"。
    """
    result = await session.execute(
        select(KnowledgeBaseDocument).where(
            KnowledgeBaseDocument.kb_id == kb_id,
            KnowledgeBaseDocument.doc_id == doc_id,
        )
    )
    doc = result.scalar_one_or_none()

    if not doc:
        raise HTTPException(status_code=404, detail="文档不存在")

    old_path = doc.category_path
    new_path = body.category_path or None
    
    await session.execute(
        update(KnowledgeBaseDocument)
        .where(
            KnowledgeBaseDocument.kb_id == kb_id,
            KnowledgeBaseDocument.doc_id == doc_id,
        )
        .values(category_path=new_path)
    )
    await session.commit()
    await session.refresh(doc)

    from backend.services.directory_service import directory_service
    
    if old_path:
        await directory_service.update_doc_counts_for_path(session, kb_id, old_path)
    if new_path and new_path != old_path:
        await directory_service.update_doc_counts_for_path(session, kb_id, new_path)

    logger.info(f"文档分类路径已更新: {doc_id} → '{new_path}'")
    return doc_to_response(doc)


# ──────────────────────────────────────────────────────────────────────────────
# 批量移动文档 API
# ──────────────────────────────────────────────────────────────────────────────

@router.post(
    "/knowledge_bases/{kb_id}/documents/batch-move",
)
async def batch_move_documents(
    kb_id: str,
    body: BatchMoveRequest,
    auth: ApiKeyAuth = Depends(require_admin),
    session: AsyncSession = Depends(get_db_session),
):
    """
    批量移动文档到目标分类路径

    - 传入 category_path=null 或 "" 可将文档移回"未分类"
    - doc_ids 为要移动的文档 doc_id 列表
    """
    if not body.doc_ids:
        raise HTTPException(status_code=400, detail="doc_ids 不能为空")

    result = await session.execute(
        select(KnowledgeBaseDocument.category_path).where(
            KnowledgeBaseDocument.kb_id == kb_id,
            KnowledgeBaseDocument.doc_id.in_(body.doc_ids),
        )
    )
    old_paths = set(row[0] for row in result.fetchall())
    
    new_path = _normalize_category_path(body.category_path or "") or None
    result = await session.execute(
        update(KnowledgeBaseDocument)
        .where(
            KnowledgeBaseDocument.kb_id == kb_id,
            KnowledgeBaseDocument.doc_id.in_(body.doc_ids),
        )
        .values(category_path=new_path)
    )
    await session.commit()

    updated = result.rowcount
    
    from backend.services.directory_service import directory_service
    
    for old_path in old_paths:
        if old_path:
            await directory_service.update_doc_counts_for_path(session, kb_id, old_path)
    
    if new_path:
        await directory_service.update_doc_counts_for_path(session, kb_id, new_path)
    
    logger.info(f"批量移动文档: {updated} 条 → '{new_path}'")
    return {"success": True, "updated": updated, "category_path": new_path}


# ──────────────────────────────────────────────────────────────────────────────
# 批量删除文档 API
# ──────────────────────────────────────────────────────────────────────────────

@router.post(
    "/knowledge_bases/{kb_id}/documents/batch-delete",
    response_model=BatchDeleteResponse,
)
async def batch_delete_documents_from_kb(
    kb_id: str,
    body: BatchDeleteRequest,
    auth: ApiKeyAuth = Depends(require_admin),
    session: AsyncSession = Depends(get_db_session),
):
    """批量删除知识库文档"""
    if not body.doc_ids:
        raise HTTPException(status_code=400, detail="doc_ids 不能为空")

    manager = get_kb_manager()
    kb = await manager.get_knowledge_base(session, kb_id)
    if not kb:
        raise HTTPException(status_code=404, detail="知识库不存在")

    service = get_rag_service()
    results: List[BatchDeleteItemResult] = []
    deleted_count = 0

    unique_doc_ids = list(dict.fromkeys(body.doc_ids))
    for doc_id in unique_doc_ids:
        try:
            await service.delete_doc_by_id(
                doc_id=doc_id,
                delete_llm_cache=body.delete_llm_cache,
                kb_id=kb_id,
            )
            await manager.remove_document(session, kb_id, doc_id)
            deleted_count += 1
            results.append(BatchDeleteItemResult(doc_id=doc_id, success=True, message="删除成功"))
        except Exception as e:
            logger.exception(f"批量删除文档失败: {kb_id}/{doc_id}")
            results.append(BatchDeleteItemResult(doc_id=doc_id, success=False, message=str(e)))

    failed_count = len(unique_doc_ids) - deleted_count
    logger.info(
        f"批量删除文档完成: kb_id={kb_id}, total={len(unique_doc_ids)}, "
        f"deleted={deleted_count}, failed={failed_count}, user={auth.user_id}"
    )

    return BatchDeleteResponse(
        success=failed_count == 0,
        kb_id=kb_id,
        total=len(unique_doc_ids),
        deleted=deleted_count,
        failed=failed_count,
        results=results,
    )


# ──────────────────────────────────────────────────────────────────────────────
# 批量重命名目录（更新路径前缀）API
# ──────────────────────────────────────────────────────────────────────────────

@router.post(
    "/knowledge_bases/{kb_id}/documents/rename-category",
)
async def rename_category(
    kb_id: str,
    old_path: str = Body(..., embed=True, description="旧分类路径"),
    new_path: str = Body(..., embed=True, description="新分类路径"),
    auth: ApiKeyAuth = Depends(require_admin),
    session: AsyncSession = Depends(get_db_session),
):
    """
    重命名目录（将所有匹配旧路径前缀的文档更新为新路径前缀）

    例如：old_path="物理评论", new_path="PRL" 会将:
      "物理评论/2024" → "PRL/2024"
      "物理评论"       → "PRL"
      
    注意：推荐使用 /knowledge_bases/{kb_id}/directories/{path}/rename 接口，
    该接口会同时更新目录表和文档表。
    """
    if not old_path.strip():
        raise HTTPException(status_code=400, detail="未分类不能重命名")

    if not new_path.strip():
        raise HTTPException(status_code=400, detail="新分类路径不能为空")

    old_path = _normalize_category_path(old_path)
    new_path = _normalize_category_path(new_path)

    from backend.services.directory_service import directory_service
    
    success, affected, final_path = await directory_service.rename_directory(
        session=session,
        kb_id=kb_id,
        old_path=old_path,
        new_name=new_path.split("/")[-1],
    )
    
    if not success:
        raise HTTPException(
            status_code=400,
            detail=f"重命名失败，目标路径可能已存在: {final_path}",
        )

    logger.info(f"目录重命名: '{old_path}' → '{final_path}', 影响 {affected} 个目录")
    return {"success": True, "updated": affected, "old_path": old_path, "new_path": final_path}


# ──────────────────────────────────────────────────────────────────────────────
# 删除文档 API
# ──────────────────────────────────────────────────────────────────────────────

@router.delete(
    "/knowledge_bases/{kb_id}/documents/{doc_id}",
    response_model=DocumentDeleteResponse,
)
async def delete_document_from_kb(
    kb_id: str,
    doc_id: str,
    delete_llm_cache: bool = Query(False, description="是否删除 LLM 缓存"),
    auth: ApiKeyAuth = Depends(require_admin),
    session: AsyncSession = Depends(get_db_session),
):
    """从知识库删除文档"""
    manager = get_kb_manager()

    kb = await manager.get_knowledge_base(session, kb_id)
    if not kb:
        raise HTTPException(status_code=404, detail="知识库不存在")

    try:
        service = get_rag_service()
        await service.delete_doc_by_id(
            doc_id=doc_id,
            delete_llm_cache=delete_llm_cache,
            kb_id=kb_id,
        )

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

# ──────────────────────────────────────────────────────────────────────────────
# 全局最近文档 API
# ──────────────────────────────────────────────────────────────────────────────

@router.get(
    "/documents/recent",
    response_model=List[Dict[str, Any]],
)
async def list_recent_documents(
    limit: int = Query(10, ge=1, le=50, description="返回数量"),
    auth: ApiKeyAuth = Depends(require_admin),
    session: AsyncSession = Depends(get_db_session),
):
    """获取所有知识库中最近上传的文档"""
    # 使用 join 获取知识库名称
    stmt = (
        select(
            KnowledgeBaseDocument,
            KnowledgeBase.kb_name
        )
        .join(KnowledgeBase, KnowledgeBaseDocument.kb_id == KnowledgeBase.kb_id)
        .order_by(KnowledgeBaseDocument.created_at.desc())
        .limit(limit)
    )
    
    result = await session.execute(stmt)
    rows = result.all()
    
    items = []
    for doc, kb_name in rows:
        item = doc_to_response(doc).model_dump()
        item["kb_name"] = kb_name
        items.append(item)
        
    return items

# ──────────────────────────────────────────────────────────────────────────────
# 知识库统计 API
# ──────────────────────────────────────────────────────────────────────────────

@router.get(
    "/knowledge_bases/{kb_id}/stats",
    response_model=KnowledgeBaseStatsResponse,
)
async def get_knowledge_base_stats(
    kb_id: str,
    auth: ApiKeyAuth = Depends(require_admin),
    session: AsyncSession = Depends(get_db_session),
):
    """获取知识库统计信息"""
    manager = get_kb_manager()
    kb = await manager.get_knowledge_base(session, kb_id)
    if not kb:
        raise HTTPException(status_code=404, detail="知识库不存在")

    updated_kb = await manager.update_knowledge_base_stats(session, kb_id)

    return KnowledgeBaseStatsResponse(
        kb_id=updated_kb.kb_id,
        kb_code=updated_kb.kb_code,
        kb_name=updated_kb.kb_name,
        doc_count=updated_kb.doc_count or 0,
        entity_count=updated_kb.entity_count or 0,
        relation_count=updated_kb.relation_count or 0,
        chunk_count=updated_kb.chunk_count or 0,
        storage_size_mb=0.0,
        last_document_at=updated_kb.updated_at.isoformat() if updated_kb.updated_at else None,
    )
