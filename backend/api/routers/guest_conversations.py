"""访客后端会话列表：返回 guest 或主站回退库中确有消息的会话。

8083 使用独立的 guest_checkpoints.db，与 8082 的 checkpoints.db 隔离。
会话表与主站共用，因此优先检查 guest 库，无消息时回退检查主站库。
"""

import asyncio
import time

from fastapi import APIRouter, Depends, Query, Request
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from backend.api.routers.auth import get_current_user
from backend.api.routers.conversations import ConversationResponse
from backend.application.checkpointing.guest_fallback import (
    get_thread_ids_with_checkpoint_messages,
    has_checkpoint_messages,
)
from backend.config.loggers import get_logger
from backend.core.config import build_internal_thread_key
from backend.db.connection import get_async_session
from backend.db.models import Conversation, User

logger = get_logger(__name__)

router = APIRouter(prefix="/conversations", tags=["guest-对话管理"])


async def _has_checkpoint(request: Request, internal_thread_id: str) -> bool:
    try:
        return await has_checkpoint_messages(request, internal_thread_id)
    except Exception as exc:
        logger.debug(
            "检查 guest/main checkpoint 失败: thread=%s, error=%s",
            internal_thread_id,
            exc,
        )
        return False


@router.get("", response_model=list[ConversationResponse])
async def list_guest_conversations(
    request: Request,
    limit: int = Query(20, ge=1, le=100),
    offset: int = Query(0, ge=0),
    include_metadata: bool = Query(
        True,
        description="是否返回会话元数据；侧边栏列表可关闭以减少查询数据量",
    ),
    user: User = Depends(get_current_user),
    session: AsyncSession = Depends(get_async_session),
):
    """获取当前用户在访客库或主站回退库中有消息记录的会话列表。"""
    columns = [
        Conversation.id,
        Conversation.thread_id,
        Conversation.title,
        Conversation.created_at,
        Conversation.updated_at,
    ]
    if include_metadata:
        columns.append(Conversation.metadata_)

    # ``offset`` is defined over visible conversations, not raw DB candidates.
    # Check candidate batches concurrently; the previous sequential N+1 loop
    # made the first sidebar page wait for every checkpoint lookup in turn.
    wanted_visible = offset + limit
    scan_offset = 0
    scan_batch_size = max(limit, min(60, wanted_visible))
    visible: list[ConversationResponse] = []
    started_at = time.perf_counter()
    checkpoint_seconds = 0.0

    while len(visible) < wanted_visible:
        result = await session.execute(
            select(*columns)
            .where(
                Conversation.user_id == user.user_id,
                Conversation.is_deleted == False,
            )
            .order_by(Conversation.updated_at.desc(), Conversation.id.desc())
            .offset(scan_offset)
            .limit(scan_batch_size)
        )
        conversations = result.all()
        if not conversations:
            break
        scan_offset += len(conversations)

        internal_thread_ids = [
            build_internal_thread_key(user.user_id, conv.thread_id)
            for conv in conversations
        ]
        checkpoint_started_at = time.perf_counter()
        try:
            message_thread_ids = await get_thread_ids_with_checkpoint_messages(
                request,
                internal_thread_ids,
            )
        except Exception as exc:
            logger.warning("批量检查 checkpoint 失败，回退逐条查询: %s", exc)
            checks = await asyncio.gather(
                *(
                    _has_checkpoint(request, internal_thread_id)
                    for internal_thread_id in internal_thread_ids
                )
            )
            message_thread_ids = {
                thread_id
                for thread_id, has_messages in zip(internal_thread_ids, checks)
                if has_messages
            }
        checkpoint_seconds += time.perf_counter() - checkpoint_started_at
        for conv, internal_thread_id in zip(conversations, internal_thread_ids):
            if internal_thread_id not in message_thread_ids:
                continue
            visible.append(
                ConversationResponse(
                    id=conv.id,
                    thread_id=conv.thread_id,
                    title=conv.title,
                    metadata=conv.metadata_ if include_metadata else None,
                    created_at=conv.created_at.isoformat() if conv.created_at else "",
                    updated_at=conv.updated_at.isoformat() if conv.updated_at else "",
                )
            )

        if len(conversations) < scan_batch_size:
            break

    elapsed_seconds = time.perf_counter() - started_at
    logger.info(
        "历史记录分页完成: user=%s offset=%d limit=%d visible=%d total_ms=%.1f checkpoint_ms=%.1f",
        user.user_id,
        offset,
        limit,
        len(visible[offset:wanted_visible]),
        elapsed_seconds * 1000,
        checkpoint_seconds * 1000,
    )
    return visible[offset:wanted_visible]
