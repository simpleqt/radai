"""管理员会话管理接口。

提供跨用户的会话列表查看、消息详情获取、会话删除和统计功能。
所有接口均要求管理员权限 (role=admin)。
"""

from datetime import datetime
from typing import Optional

from fastapi import APIRouter, Depends, HTTPException, Query, Request
from pydantic import BaseModel
from sqlalchemy import case, func, select
from sqlalchemy.ext.asyncio import AsyncSession

from backend.api.dependencies.auth import ApiKeyAuth, require_admin
from backend.config.loggers import get_logger
from backend.core.config import build_internal_thread_key, get_paths
from backend.core.thread_key import parse_internal_thread_key
from backend.application.checkpointing.checkpointer import get_checkpointer_from_request
from backend.application.blocking import run_blocking
from backend.application.conversations.message_serializer import serialize_checkpoint_messages
from backend.db.connection import get_async_session
from backend.db.models import Conversation, User
from backend.services.admin_audit import write_admin_audit

logger = get_logger(__name__)

router = APIRouter(prefix="/admin/conversations", tags=["管理员-会话管理"])

# ---------------------------------------------------------------------------
# Response models
# ---------------------------------------------------------------------------


class AdminConversationItem(BaseModel):
    """管理员视角的单条会话信息"""
    id: str
    thread_id: str
    user_id: str
    username: str
    title: str
    metadata: Optional[dict] = None
    is_deleted: bool = False
    created_at: str
    updated_at: str


class AdminConversationListResponse(BaseModel):
    """会话列表响应（含分页信息）"""
    items: list[AdminConversationItem]
    total: int
    limit: int
    offset: int


class AdminMessageItem(BaseModel):
    """单条消息"""
    role: str
    content: str
    metadata: dict = {}
    id: Optional[str] = None


class AdminConversationDetail(BaseModel):
    """会话详情（含消息列表）"""
    conversation: AdminConversationItem
    messages: list[AdminMessageItem]


class ConversationStatsResponse(BaseModel):
    """会话统计信息"""
    total_conversations: int
    active_conversations: int  # 未删除
    deleted_conversations: int
    unique_users: int
    latest_conversation_at: Optional[str] = None


# ---------------------------------------------------------------------------
# Endpoints
# ---------------------------------------------------------------------------


@router.get("/stats", response_model=ConversationStatsResponse)
async def get_conversation_stats(
    session: AsyncSession = Depends(get_async_session),
    _admin: ApiKeyAuth = Depends(require_admin),
):
    """获取会话统计信息"""
    stats = (
        await session.execute(
            select(
                func.count(Conversation.id),
                func.sum(case((Conversation.is_deleted.is_(False), 1), else_=0)),
                func.count(func.distinct(Conversation.user_id)),
                func.max(Conversation.updated_at),
            )
        )
    ).one()
    total = int(stats[0] or 0)
    active = int(stats[1] or 0)
    deleted = total - active
    unique_users = int(stats[2] or 0)
    latest = stats[3]

    return ConversationStatsResponse(
        total_conversations=total,
        active_conversations=active,
        deleted_conversations=deleted,
        unique_users=unique_users,
        latest_conversation_at=latest.isoformat() if latest else None,
    )


@router.get("", response_model=AdminConversationListResponse)
async def list_all_conversations(
    limit: int = Query(20, ge=1, le=200),
    offset: int = Query(0, ge=0),
    user_id: Optional[str] = Query(None, description="按用户 ID 筛选"),
    username: Optional[str] = Query(None, description="按用户名模糊搜索"),
    include_deleted: bool = Query(False, description="是否包含已删除的会话"),
    session: AsyncSession = Depends(get_async_session),
    _admin: ApiKeyAuth = Depends(require_admin),
):
    """获取所有用户的会话列表（管理员）"""
    # 构建基础查询
    stmt = (
        select(Conversation, User.username)
        .join(User, Conversation.user_id == User.user_id)
    )

    # 筛选条件
    if not include_deleted:
        stmt = stmt.where(Conversation.is_deleted == False)
    if user_id:
        stmt = stmt.where(Conversation.user_id == user_id)
    if username:
        stmt = stmt.where(User.username.ilike(f"%{username}%"))

    # 总数
    count_stmt = select(func.count()).select_from(stmt.subquery())
    total = await session.scalar(count_stmt) or 0

    # 分页
    stmt = stmt.order_by(Conversation.updated_at.desc()).offset(offset).limit(limit)
    result = await session.execute(stmt)
    rows = result.all()

    items = [
        AdminConversationItem(
            id=conv.id,
            thread_id=conv.thread_id,
            user_id=conv.user_id,
            username=row_username,
            title=conv.title,
            metadata=conv.metadata_,
            is_deleted=conv.is_deleted,
            created_at=conv.created_at.isoformat() if conv.created_at else "",
            updated_at=conv.updated_at.isoformat() if conv.updated_at else "",
        )
        for conv, row_username in rows
    ]

    return AdminConversationListResponse(
        items=items,
        total=total,
        limit=limit,
        offset=offset,
    )


@router.get("/{user_id}/{thread_id}/messages", response_model=AdminConversationDetail)
async def get_user_conversation_messages(
    user_id: str,
    thread_id: str,
    request: Request,
    session: AsyncSession = Depends(get_async_session),
    _admin: ApiKeyAuth = Depends(require_admin),
):
    """获取任意用户的会话详情及消息历史"""
    # 查询会话元数据
    result = await session.execute(
        select(Conversation, User.username)
        .join(User, Conversation.user_id == User.user_id)
        .where(
            Conversation.thread_id == thread_id,
            Conversation.user_id == user_id,
        )
    )
    row = result.first()
    if not row:
        raise HTTPException(status_code=404, detail="会话不存在")

    conv, username = row
    conv_item = AdminConversationItem(
        id=conv.id,
        thread_id=conv.thread_id,
        user_id=conv.user_id,
        username=username,
        title=conv.title,
        metadata=conv.metadata_,
        is_deleted=conv.is_deleted,
        created_at=conv.created_at.isoformat() if conv.created_at else "",
        updated_at=conv.updated_at.isoformat() if conv.updated_at else "",
    )

    # 从 checkpointer 读取消息
    internal_thread_id = build_internal_thread_key(user_id, thread_id)
    messages: list[AdminMessageItem] = []
    try:
        checkpointer = get_checkpointer_from_request(request)
        config = {"configurable": {"thread_id": internal_thread_id}}
        checkpoint = await checkpointer.aget(config)

        if checkpoint:
            channel_values = checkpoint.get("channel_values", {})
            raw_messages = channel_values.get("messages", [])
            registered_images = (conv.metadata_ or {}).get("registered_images") or {}
            messages = [
                AdminMessageItem(**item)
                for item in serialize_checkpoint_messages(
                    raw_messages,
                    registered_images=registered_images,
                    coerce_content_to_str=True,
                )
            ]
    except Exception as e:
        logger.error(f"[Admin] 读取 checkpointer 失败: thread={internal_thread_id}, error={e}")

    return AdminConversationDetail(conversation=conv_item, messages=messages)


@router.delete("/{user_id}/{thread_id}")
async def delete_user_conversation(
    user_id: str,
    thread_id: str,
    request: Request,
    session: AsyncSession = Depends(get_async_session),
    admin: ApiKeyAuth = Depends(require_admin),
):
    """删除任意用户的会话（逻辑删除 + 清理 checkpointer + 清理沙箱目录）"""
    result = await session.execute(
        select(Conversation).where(
            Conversation.thread_id == thread_id,
            Conversation.user_id == user_id,
        )
    )
    conv = result.scalar_one_or_none()
    if not conv:
        raise HTTPException(status_code=404, detail="会话不存在")

    conv.is_deleted = True
    await write_admin_audit(
        session,
        request,
        admin,
        action="conversation.delete",
        target_type="conversation",
        target_id=thread_id,
        detail={"user_id": user_id, "title": conv.title},
    )
    await session.commit()

    internal_thread_id = build_internal_thread_key(user_id, thread_id)
    cleanup_errors: list[str] = []
    try:
        from backend.application.agent_tasks import agent_task_registry

        runtime_metadata = dict((conv.metadata_ or {}).get("agent_runtime") or {})
        agent_code = str(runtime_metadata.get("agent_code") or "ne").strip() or "ne"
        active_task = await agent_task_registry.active_for_thread(
            user_id=user_id,
            agent_code=agent_code,
            thread_id=thread_id,
        )
        if active_task is not None:
            await agent_task_registry.cancel(active_task)
    except Exception as exc:
        logger.exception("[Admin] 停止会话后台任务失败: thread=%s", internal_thread_id)
        cleanup_errors.append(f"background_task: {type(exc).__name__}")

    try:
        checkpointer = get_checkpointer_from_request(request)
        await checkpointer.adelete_thread(internal_thread_id)
    except Exception as exc:
        logger.exception("[Admin] 清理 checkpointer 失败: thread=%s", internal_thread_id)
        cleanup_errors.append(f"checkpointer: {type(exc).__name__}")

    try:
        from backend.core.storage.scoped_storage import ScopedStorage

        storage = ScopedStorage(user_id=user_id, thread_id=thread_id)
        await run_blocking(storage.delete_all_files, timeout_seconds=30)
    except Exception as exc:
        logger.exception("[Admin] 清理 MinIO 线程附件失败: thread=%s", internal_thread_id)
        cleanup_errors.append(f"storage: {type(exc).__name__}")

    try:
        await run_blocking(
            get_paths().delete_thread_dir,
            internal_thread_id,
            timeout_seconds=10,
        )
    except Exception as exc:
        logger.exception("[Admin] 清理沙箱线程目录失败: thread=%s", internal_thread_id)
        cleanup_errors.append(f"sandbox: {type(exc).__name__}")

    if cleanup_errors:
        conv.is_deleted = False
        metadata = dict(conv.metadata_ or {})
        metadata["deletion_cleanup_error"] = {
            "components": cleanup_errors,
            "updated_at": datetime.now().isoformat(),
        }
        conv.metadata_ = metadata
        await session.commit()
        raise HTTPException(status_code=503, detail="会话清理未完成，请稍后重试")

    metadata = dict(conv.metadata_ or {})
    metadata.pop("deletion_cleanup_error", None)
    conv.metadata_ = metadata
    await session.commit()

    return {
        "success": True,
        "message": f"已删除用户 {user_id} 的会话 {thread_id}",
    }


@router.get("/checkpointer/threads")
async def list_checkpointer_threads(
    request: Request,
    limit: int = Query(50, ge=1, le=500),
    offset: int = Query(0, ge=0),
    _admin: ApiKeyAuth = Depends(require_admin),
):
    """直接列出 checkpointer SQLite 中的所有 thread_id。

    此接口绕过 Conversation 表，直接查询 SQLite checkpointer 数据库，
    可发现未记录在 Conversation 表中的孤立会话。
    """
    try:
        checkpointer = get_checkpointer_from_request(request)
        if not hasattr(checkpointer, "conn"):
            return {"items": [], "total": 0, "message": "checkpointer 不支持直接 SQL 查询"}

        # 检查表是否存在
        cursor = await checkpointer.conn.execute(
            "SELECT name FROM sqlite_master WHERE type='table' AND name='checkpoints'"
        )
        if not await cursor.fetchone():
            return {"items": [], "total": 0, "message": "checkpoints 表不存在"}

        # 获取去重的 thread_id 列表
        count_cursor = await checkpointer.conn.execute(
            "SELECT COUNT(DISTINCT thread_id) FROM checkpoints"
        )
        total_row = await count_cursor.fetchone()
        total = total_row[0] if total_row else 0

        cursor = await checkpointer.conn.execute(
            "SELECT DISTINCT thread_id FROM checkpoints ORDER BY thread_id LIMIT ? OFFSET ?",
            (limit, offset),
        )
        rows = await cursor.fetchall()

        items = []
        for row in rows:
            raw_tid = str(row[0])
            parsed_user_id, parsed_thread_id = parse_internal_thread_key(raw_tid)
            items.append({
                "internal_thread_id": raw_tid,
                "user_id": parsed_user_id,
                "thread_id": parsed_thread_id,
            })

        return {"items": items, "total": total, "limit": limit, "offset": offset}
    except Exception as e:
        logger.error(f"[Admin] 查询 checkpointer threads 失败: {e}")
        raise HTTPException(status_code=500, detail="查询 checkpointer 失败")
