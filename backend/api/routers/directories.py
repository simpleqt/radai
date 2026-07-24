"""知识库目录管理 API 路由"""

from typing import Any, Dict, List, Optional
from urllib.parse import unquote

from fastapi import APIRouter, Depends, HTTPException, Query
from pydantic import BaseModel, Field
from sqlalchemy.ext.asyncio import AsyncSession

from backend.api.dependencies.auth import ApiKeyAuth, get_db_session, require_admin
from backend.config.loggers import get_logger
from backend.rag.knowledge_base import get_kb_manager
from backend.services.directory_service import directory_service

router = APIRouter(tags=["知识库目录"])
logger = get_logger(__name__)


class DirectoryCreateRequest(BaseModel):
    """创建目录请求"""

    category_path: str = Field(..., description="目录路径，如 '期刊/2024'", min_length=1, max_length=500)
    description: Optional[str] = Field(None, description="目录描述", max_length=1000)
    sort_order: int = Field(0, description="排序序号")
    icon: Optional[str] = Field(None, description="目录图标", max_length=50)
    color: Optional[str] = Field(None, description="目录颜色", max_length=20)


class DirectoryUpdateRequest(BaseModel):
    """更新目录请求"""

    description: Optional[str] = Field(None, description="目录描述", max_length=1000)
    sort_order: Optional[int] = Field(None, description="排序序号")
    icon: Optional[str] = Field(None, description="目录图标", max_length=50)
    color: Optional[str] = Field(None, description="目录颜色", max_length=20)


class DirectoryRenameRequest(BaseModel):
    """重命名目录请求"""

    # ``old_path`` is used by the body-based endpoint below.  It remains
    # optional so the legacy path-based endpoint can keep accepting its
    # original payload.
    old_path: Optional[str] = Field(None, description="原目录路径")
    new_name: str = Field(..., description="新目录名称", min_length=1, max_length=100)


class DirectoryMoveRequest(BaseModel):
    """移动目录请求"""

    # ``source_path`` is used by the body-based endpoint below.  It remains
    # optional for compatibility with the legacy path-based endpoint.
    source_path: Optional[str] = Field(None, description="源目录路径")
    target_parent_path: Optional[str] = Field(None, description="目标父目录路径，None 表示移动到根目录")


class DirectoryDeleteRequest(BaseModel):
    """删除目录请求"""

    delete_documents: bool = Field(False, description="是否同时删除目录下的文档")


class DirectoryReorderRequest(BaseModel):
    """同级目录重排请求"""

    parent_path: Optional[str] = Field(None, description="父目录路径，空表示根目录")
    paths: List[str] = Field(..., min_length=1, description="同级目录路径，按新顺序排列")


class DirectoryResponse(BaseModel):
    """目录响应"""

    id: int
    kb_id: str
    category_path: str
    parent_path: Optional[str] = None
    depth: int = 1
    name: str
    description: Optional[str] = None
    sort_order: int = 0
    icon: Optional[str] = None
    color: Optional[str] = None
    doc_count: int = 0
    total_doc_count: int = 0
    is_system: bool = False
    created_at: str
    updated_at: Optional[str] = None


class DirectoryTreeNode(BaseModel):
    """目录树节点"""

    id: Optional[int | str] = None
    path: str
    name: str
    description: Optional[str] = None
    icon: Optional[str] = None
    color: Optional[str] = None
    sort_order: int = 0
    doc_count: int = 0
    total_doc_count: int = 0
    is_virtual: bool = False
    children: List["DirectoryTreeNode"] = []


class DirectoryTreeResponse(BaseModel):
    """目录树响应"""

    tree: List[DirectoryTreeNode]
    total_directories: int
    total_docs: int = 0


DirectoryTreeNode.model_rebuild()


def _normalize_path(path: str) -> str:
    """规范化路径"""
    # Path parameters may be percent-encoded by the browser.  Decode once
    # here so paths containing Chinese characters or nested segments resolve
    # to the same database value as paths sent in a JSON body.
    decoded = unquote(path or "")
    return "/".join(part for part in decoded.split("/") if part).strip()


@router.get(
    "/knowledge_bases/{kb_id}/directories",
    response_model=DirectoryTreeResponse,
)
async def get_directory_tree(
    kb_id: str,
    auth: ApiKeyAuth = Depends(require_admin),
    session: AsyncSession = Depends(get_db_session),
):
    """
    获取知识库目录树

    返回完整的目录树结构，每个节点包含文档统计信息。
    """
    from sqlalchemy import func, select
    from backend.db.models import KnowledgeBaseDocument
    
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
            id=node.get("id"),
            path=node["path"],
            name=node["name"],
            description=node.get("description"),
            icon=node.get("icon"),
            color=node.get("color"),
            sort_order=node.get("sort_order", 0),
            doc_count=node.get("doc_count", 0),
            total_doc_count=node.get("total_doc_count", 0),
            is_virtual=node.get("is_virtual", False),
            children=[_to_tree_node(child) for child in node.get("children", [])],
        )

    tree_nodes = [_to_tree_node(node) for node in tree_data]

    return DirectoryTreeResponse(
        tree=tree_nodes,
        total_directories=sum(1 for _ in _flatten_tree(tree_nodes)),
        total_docs=total_docs,
    )


def _flatten_tree(nodes: List[DirectoryTreeNode]):
    """扁平化目录树"""
    for node in nodes:
        yield node
        yield from _flatten_tree(node.children)


@router.post(
    "/knowledge_bases/{kb_id}/directories",
    response_model=DirectoryResponse,
    status_code=201,
)
async def create_directory(
    kb_id: str,
    request: DirectoryCreateRequest,
    auth: ApiKeyAuth = Depends(require_admin),
    session: AsyncSession = Depends(get_db_session),
):
    """
    创建目录

    自动创建父目录（如果不存在）。
    """
    manager = get_kb_manager()
    kb = await manager.get_knowledge_base(session, kb_id)
    if not kb:
        raise HTTPException(status_code=404, detail="知识库不存在")

    category_path = _normalize_path(request.category_path)
    if not category_path:
        raise HTTPException(status_code=400, detail="目录路径不能为空")

    directory = await directory_service.create_directory(
        session=session,
        kb_id=kb_id,
        category_path=category_path,
        description=request.description,
        sort_order=request.sort_order,
        icon=request.icon,
        color=request.color,
        created_by=auth.user_id,
    )

    return DirectoryResponse(
        id=directory.id,
        kb_id=directory.kb_id,
        category_path=directory.category_path,
        parent_path=directory.parent_path,
        depth=directory.depth,
        name=directory.name,
        description=directory.description,
        sort_order=directory.sort_order,
        icon=directory.icon,
        color=directory.color,
        doc_count=directory.doc_count,
        total_doc_count=directory.total_doc_count,
        is_system=directory.is_system,
        created_at=directory.created_at.isoformat() if directory.created_at else "",
        updated_at=directory.updated_at.isoformat() if directory.updated_at else None,
    )


@router.get(
    "/knowledge_bases/{kb_id}/directories/{category_path:path}",
    response_model=DirectoryResponse,
)
async def get_directory(
    kb_id: str,
    category_path: str,
    auth: ApiKeyAuth = Depends(require_admin),
    session: AsyncSession = Depends(get_db_session),
):
    """获取目录详情"""
    manager = get_kb_manager()
    kb = await manager.get_knowledge_base(session, kb_id)
    if not kb:
        raise HTTPException(status_code=404, detail="知识库不存在")

    directory = await directory_service.crud.get_by_path(session, kb_id, category_path)
    if not directory:
        raise HTTPException(status_code=404, detail="目录不存在")

    return DirectoryResponse(
        id=directory.id,
        kb_id=directory.kb_id,
        category_path=directory.category_path,
        parent_path=directory.parent_path,
        depth=directory.depth,
        name=directory.name,
        description=directory.description,
        sort_order=directory.sort_order,
        icon=directory.icon,
        color=directory.color,
        doc_count=directory.doc_count,
        total_doc_count=directory.total_doc_count,
        is_system=directory.is_system,
        created_at=directory.created_at.isoformat() if directory.created_at else "",
        updated_at=directory.updated_at.isoformat() if directory.updated_at else None,
    )


@router.put(
    "/knowledge_bases/{kb_id}/directories/{category_path:path}",
    response_model=DirectoryResponse,
)
async def update_directory(
    kb_id: str,
    category_path: str,
    request: DirectoryUpdateRequest,
    auth: ApiKeyAuth = Depends(require_admin),
    session: AsyncSession = Depends(get_db_session),
):
    """更新目录属性"""
    manager = get_kb_manager()
    kb = await manager.get_knowledge_base(session, kb_id)
    if not kb:
        raise HTTPException(status_code=404, detail="知识库不存在")

    directory = await directory_service.update_directory(
        session=session,
        kb_id=kb_id,
        category_path=category_path,
        description=request.description,
        sort_order=request.sort_order,
        icon=request.icon,
        color=request.color,
    )

    if not directory:
        raise HTTPException(status_code=404, detail="目录不存在")

    return DirectoryResponse(
        id=directory.id,
        kb_id=directory.kb_id,
        category_path=directory.category_path,
        parent_path=directory.parent_path,
        depth=directory.depth,
        name=directory.name,
        description=directory.description,
        sort_order=directory.sort_order,
        icon=directory.icon,
        color=directory.color,
        doc_count=directory.doc_count,
        total_doc_count=directory.total_doc_count,
        is_system=directory.is_system,
        created_at=directory.created_at.isoformat() if directory.created_at else "",
        updated_at=directory.updated_at.isoformat() if directory.updated_at else None,
    )


@router.post("/knowledge_bases/{kb_id}/directories/rename")
async def rename_directory_by_body(
    kb_id: str,
    request: DirectoryRenameRequest,
    auth: ApiKeyAuth = Depends(require_admin),
    session: AsyncSession = Depends(get_db_session),
):
    """通过请求体重命名目录，避免将完整路径放入 URL 路径参数。"""
    manager = get_kb_manager()
    kb = await manager.get_knowledge_base(session, kb_id)
    if not kb:
        raise HTTPException(status_code=404, detail="知识库不存在")

    old_path = _normalize_path(request.old_path or "")
    if not old_path:
        raise HTTPException(status_code=400, detail="原目录路径不能为空")

    success, affected, new_path = await directory_service.rename_directory(
        session=session,
        kb_id=kb_id,
        old_path=old_path,
        new_name=request.new_name,
    )

    if not success:
        raise HTTPException(
            status_code=400,
            detail=f"重命名失败，目标路径可能已存在: {new_path}",
        )

    return {
        "success": True,
        "old_path": old_path,
        "new_path": new_path,
        "affected_directories": affected,
    }


@router.post(
    "/knowledge_bases/{kb_id}/directories/{category_path:path}/rename",
)
async def rename_directory(
    kb_id: str,
    category_path: str,
    request: DirectoryRenameRequest,
    auth: ApiKeyAuth = Depends(require_admin),
    session: AsyncSession = Depends(get_db_session),
):
    """
    重命名目录

    同时更新所有子目录和文档的路径。
    """
    manager = get_kb_manager()
    kb = await manager.get_knowledge_base(session, kb_id)
    if not kb:
        raise HTTPException(status_code=404, detail="知识库不存在")

    category_path = _normalize_path(category_path)
    success, affected, new_path = await directory_service.rename_directory(
        session=session,
        kb_id=kb_id,
        old_path=category_path,
        new_name=request.new_name,
    )

    if not success:
        raise HTTPException(
            status_code=400,
            detail=f"重命名失败，目标路径可能已存在: {new_path}",
        )

    return {
        "success": True,
        "old_path": category_path,
        "new_path": new_path,
        "affected_directories": affected,
    }


@router.post(
    "/knowledge_bases/{kb_id}/directories/{category_path:path}/move",
)
async def move_directory(
    kb_id: str,
    category_path: str,
    request: DirectoryMoveRequest,
    auth: ApiKeyAuth = Depends(require_admin),
    session: AsyncSession = Depends(get_db_session),
):
    """
    移动目录到新的父目录下

    同时移动所有子目录和文档。
    """
    manager = get_kb_manager()
    kb = await manager.get_knowledge_base(session, kb_id)
    if not kb:
        raise HTTPException(status_code=404, detail="知识库不存在")

    category_path = _normalize_path(category_path)
    target_parent_path = _normalize_path(request.target_parent_path or "") or None
    success, affected, new_path = await directory_service.move_directory(
        session=session,
        kb_id=kb_id,
        source_path=category_path,
        target_parent_path=target_parent_path,
    )

    if not success:
        raise HTTPException(
            status_code=400,
            detail=f"移动失败，目标路径可能已存在或不能移动到子目录: {new_path}",
        )

    return {
        "success": True,
        "old_path": category_path,
        "new_path": new_path,
        "affected_directories": affected,
    }


@router.post("/knowledge_bases/{kb_id}/directories/move")
async def move_directory_by_body(
    kb_id: str,
    request: DirectoryMoveRequest,
    auth: ApiKeyAuth = Depends(require_admin),
    session: AsyncSession = Depends(get_db_session),
):
    """通过请求体移动目录，支持将目录拖入另一个目录。"""
    manager = get_kb_manager()
    kb = await manager.get_knowledge_base(session, kb_id)
    if not kb:
        raise HTTPException(status_code=404, detail="知识库不存在")

    source_path = _normalize_path(request.source_path or "")
    target_parent_path = _normalize_path(request.target_parent_path or "") or None
    if not source_path:
        raise HTTPException(status_code=400, detail="源目录路径不能为空")

    success, affected, new_path = await directory_service.move_directory(
        session=session,
        kb_id=kb_id,
        source_path=source_path,
        target_parent_path=target_parent_path,
    )

    if not success:
        raise HTTPException(
            status_code=400,
            detail=f"移动失败，目标路径可能已存在或不能移动到子目录: {new_path}",
        )

    return {
        "success": True,
        "old_path": source_path,
        "new_path": new_path,
        "affected_directories": affected,
    }


@router.delete(
    "/knowledge_bases/{kb_id}/directories/{category_path:path}",
)
async def delete_directory(
    kb_id: str,
    category_path: str,
    delete_documents: bool = Query(False, description="是否同时删除目录下的文档"),
    auth: ApiKeyAuth = Depends(require_admin),
    session: AsyncSession = Depends(get_db_session),
):
    """
    删除目录

    只能删除空目录（无子目录）。如果目录下有文档，需要设置 delete_documents=true。
    """
    manager = get_kb_manager()
    kb = await manager.get_knowledge_base(session, kb_id)
    if not kb:
        raise HTTPException(status_code=404, detail="知识库不存在")

    category_path = _normalize_path(category_path)
    success, deleted_dirs, deleted_docs = await directory_service.delete_directory(
        session=session,
        kb_id=kb_id,
        category_path=category_path,
        delete_documents=delete_documents,
    )

    if not success:
        raise HTTPException(
            status_code=400,
            detail="删除失败，目录可能不存在、是系统目录、或有子目录",
        )

    return {
        "success": True,
        "deleted_directories": deleted_dirs,
        "deleted_documents": deleted_docs,
    }


@router.post("/knowledge_bases/{kb_id}/directories/reorder")
async def reorder_directories(
    kb_id: str,
    request: DirectoryReorderRequest,
    auth: ApiKeyAuth = Depends(require_admin),
    session: AsyncSession = Depends(get_db_session),
):
    """批量更新同级目录的展示顺序。"""
    manager = get_kb_manager()
    kb = await manager.get_knowledge_base(session, kb_id)
    if not kb:
        raise HTTPException(status_code=404, detail="知识库不存在")

    try:
        updated = await directory_service.reorder_directories(
            session=session,
            kb_id=kb_id,
            parent_path=request.parent_path,
            paths=request.paths,
        )
        await session.commit()
    except ValueError as exc:
        raise HTTPException(status_code=400, detail=str(exc)) from exc

    return {"success": True, "updated": updated}


@router.post(
    "/knowledge_bases/{kb_id}/directories/sync",
)
async def sync_directories(
    kb_id: str,
    auth: ApiKeyAuth = Depends(require_admin),
    session: AsyncSession = Depends(get_db_session),
):
    """
    从文档同步目录结构

    扫描所有文档的 category_path，创建缺失的目录记录。
    """
    manager = get_kb_manager()
    kb = await manager.get_knowledge_base(session, kb_id)
    if not kb:
        raise HTTPException(status_code=404, detail="知识库不存在")

    created_count = await directory_service.sync_from_documents(session, kb_id)

    return {
        "success": True,
        "created_directories": created_count,
    }


@router.post(
    "/knowledge_bases/{kb_id}/directories/recalculate",
)
async def recalculate_doc_counts(
    kb_id: str,
    auth: ApiKeyAuth = Depends(require_admin),
    session: AsyncSession = Depends(get_db_session),
):
    """
    重新计算所有目录的文档计数
    """
    manager = get_kb_manager()
    kb = await manager.get_knowledge_base(session, kb_id)
    if not kb:
        raise HTTPException(status_code=404, detail="知识库不存在")

    updated_count = await directory_service.recalculate_doc_counts(session, kb_id)

    return {
        "success": True,
        "updated_directories": updated_count,
    }
