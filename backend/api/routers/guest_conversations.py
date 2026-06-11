"""访客后端会话列表：仅返回 guest checkpoint 中确有消息的会话。

8083 使用独立的 guest_checkpoints.db，与 8082 的 checkpoints.db 隔离。
PostgreSQL 会话表与主站共用，若不过滤会出现「有标题无消息」的条目。
"""

from fastapi import APIRouter, Depends, Query, Request
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from backend.api.routers.auth import get_current_user
from backend.api.routers.conversations import ConversationResponse
from backend.config.loggers import get_logger
from backend.core.config import build_internal_thread_key
from backend.application.checkpointing.checkpointer import get_checkpointer_from_request
from backend.db.connection import get_async_session
from backend.db.models import Conversation, User

logger = get_logger(__name__)

router = APIRouter(prefix="/conversations", tags=["guest-对话管理"])


async def _has_checkpoint(request: Request, internal_thread_id: str) -> bool:
    try:
        checkpointer = get_checkpointer_from_request(request)
        checkpoint = await checkpointer.aget({"configurable": {"thread_id": internal_thread_id}})
        if not checkpoint:
            return False
        messages = (checkpoint.get("channel_values") or {}).get("messages") or []
        return len(messages) > 0
    except Exception as exc:
        logger.debug("检查 guest checkpoint 失败: thread=%s, error=%s", internal_thread_id, exc)
        return False


@router.get("", response_model=list[ConversationResponse])
async def list_guest_conversations(
    request: Request,
    limit: int = Query(20, ge=1, le=100),
    offset: int = Query(0, ge=0),
    user: User = Depends(get_current_user),
    session: AsyncSession = Depends(get_async_session),
):
    """获取当前用户在访客站点有消息记录的会话列表。"""
    result = await session.execute(
        select(Conversation)
        .where(Conversation.user_id == user.user_id, Conversation.is_deleted == False)
        .order_by(Conversation.updated_at.desc())
        .offset(offset)
        .limit(limit * 3)
    )
    conversations = result.scalars().all()

    visible: list[ConversationResponse] = []
    for conv in conversations:
        internal_tid = build_internal_thread_key(user.user_id, conv.thread_id)
        if not await _has_checkpoint(request, internal_tid):
            continue
        visible.append(
            ConversationResponse(
                id=conv.id,
                thread_id=conv.thread_id,
                title=conv.title,
                metadata=conv.metadata_,
                created_at=conv.created_at.isoformat() if conv.created_at else "",
                updated_at=conv.updated_at.isoformat() if conv.updated_at else "",
            )
        )
        if len(visible) >= limit:
            break

    return visible
