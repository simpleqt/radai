"""回答反馈路由：收集用户对单条回答的赞/踩与原因。

挂载在 ``/api/v2/conversations/{thread_id}/feedback``。同一
(user, thread, message_key) 幂等 upsert；再次提交覆盖，DELETE 撤回。
只写 MySQL 业务表，不触碰会话目录与 checkpoint。
"""

from __future__ import annotations

from datetime import datetime

from fastapi import APIRouter, Depends, HTTPException
from pydantic import BaseModel, Field, field_validator
from sqlalchemy import select
from sqlalchemy.exc import IntegrityError
from sqlalchemy.ext.asyncio import AsyncSession

from backend.api.routers.auth import get_current_user
from backend.config.loggers import get_logger
from backend.db.connection import get_async_session
from backend.db.models import MessageFeedback, User

router = APIRouter(prefix="/conversations", tags=["回答反馈"])
logger = get_logger(__name__)

_ALLOWED_RATINGS = {"up", "down"}
_MAX_REASONS = 5
_MAX_REASON_CHARS = 50
_MAX_COMMENT_CHARS = 2000


class FeedbackRequest(BaseModel):
    message_key: str = Field(..., min_length=1, max_length=128)
    rating: str
    reasons: list[str] | None = None
    comment: str | None = None
    agent_code: str | None = Field(default=None, max_length=32)
    model_name: str | None = Field(default=None, max_length=128)
    question_excerpt: str | None = None
    answer_excerpt: str | None = None

    @field_validator("rating")
    @classmethod
    def _validate_rating(cls, value: str) -> str:
        normalized = str(value or "").strip().lower()
        if normalized not in _ALLOWED_RATINGS:
            raise ValueError("rating 必须是 up 或 down")
        return normalized

    @field_validator("reasons")
    @classmethod
    def _validate_reasons(cls, value: list[str] | None) -> list[str] | None:
        if value is None:
            return None
        cleaned = [
            str(item or "").strip()[:_MAX_REASON_CHARS]
            for item in value
            if str(item or "").strip()
        ]
        return cleaned[:_MAX_REASONS] or None

    @field_validator("comment")
    @classmethod
    def _validate_comment(cls, value: str | None) -> str | None:
        text = str(value or "").strip()
        return text[:_MAX_COMMENT_CHARS] or None


class FeedbackItem(BaseModel):
    message_key: str
    rating: str
    reasons: list[str] | None = None
    comment: str | None = None


def _truncate(value: str | None, limit: int) -> str | None:
    text = str(value or "").strip()
    return text[:limit] or None


@router.get("/{thread_id}/feedback")
async def list_thread_feedback(
    thread_id: str,
    user: User = Depends(get_current_user),
    session: AsyncSession = Depends(get_async_session),
) -> dict:
    """当前用户在该会话下的全部反馈（用于刷新后恢复按钮状态）。"""
    result = await session.execute(
        select(MessageFeedback).where(
            MessageFeedback.user_id == user.user_id,
            MessageFeedback.thread_id == thread_id,
        )
    )
    items = [
        FeedbackItem(
            message_key=row.message_key,
            rating=row.rating,
            reasons=list(row.reasons) if isinstance(row.reasons, list) else None,
            comment=row.comment,
        )
        for row in result.scalars().all()
    ]
    return {"items": [item.model_dump() for item in items]}


@router.post("/{thread_id}/feedback")
async def submit_feedback(
    thread_id: str,
    request: FeedbackRequest,
    user: User = Depends(get_current_user),
    session: AsyncSession = Depends(get_async_session),
) -> dict:
    """提交或覆盖一条反馈。"""
    result = await session.execute(
        select(MessageFeedback).where(
            MessageFeedback.user_id == user.user_id,
            MessageFeedback.thread_id == thread_id,
            MessageFeedback.message_key == request.message_key,
        )
    )
    record = result.scalar_one_or_none()
    if record is None:
        record = MessageFeedback(
            user_id=user.user_id,
            thread_id=thread_id,
            message_key=request.message_key,
        )
        session.add(record)

    record.rating = request.rating
    record.reasons = request.reasons
    record.comment = request.comment
    record.agent_code = (request.agent_code or "ne").strip() or "ne"
    record.model_name = _truncate(request.model_name, 128)
    record.question_excerpt = _truncate(request.question_excerpt, 500)
    record.answer_excerpt = _truncate(request.answer_excerpt, 1000)
    record.updated_at = datetime.now()

    try:
        await session.commit()
    except IntegrityError:
        # 并发提交同一条消息的反馈：以后写为准，回读再覆盖一次。
        await session.rollback()
        retry = await session.execute(
            select(MessageFeedback).where(
                MessageFeedback.user_id == user.user_id,
                MessageFeedback.thread_id == thread_id,
                MessageFeedback.message_key == request.message_key,
            )
        )
        record = retry.scalar_one_or_none()
        if record is None:
            raise HTTPException(status_code=500, detail="反馈写入失败，请重试")
        record.rating = request.rating
        record.reasons = request.reasons
        record.comment = request.comment
        await session.commit()

    return {"success": True, "feedback_id": record.id, "rating": record.rating}


@router.delete("/{thread_id}/feedback/{message_key}")
async def delete_feedback(
    thread_id: str,
    message_key: str,
    user: User = Depends(get_current_user),
    session: AsyncSession = Depends(get_async_session),
) -> dict:
    """撤回反馈（再次点击同一评价按钮）。"""
    result = await session.execute(
        select(MessageFeedback).where(
            MessageFeedback.user_id == user.user_id,
            MessageFeedback.thread_id == thread_id,
            MessageFeedback.message_key == message_key,
        )
    )
    record = result.scalar_one_or_none()
    if record is not None:
        await session.delete(record)
        await session.commit()
    return {"success": True}
