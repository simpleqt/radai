"""Conversation list persistence for agent runtime sessions."""

from __future__ import annotations

import logging
from datetime import datetime
from typing import Any

from sqlalchemy import select
from sqlalchemy.exc import IntegrityError

from backend.db.connection import get_async_session_factory
from backend.db.models import Conversation, User

logger = logging.getLogger(__name__)


class ConversationStore:
    """Persist the lightweight conversation record used by conversation lists."""

    async def ensure_runtime_conversation(
        self,
        *,
        user_id: str,
        thread_id: str,
        question: str,
        agent_code: str,
        workflow_name: str,
        metadata: dict[str, Any] | None = None,
    ) -> None:
        session_factory = get_async_session_factory()
        async with session_factory() as session:
            try:
                user_result = await session.execute(
                    select(User.user_id).where(User.user_id == user_id)
                )
                if user_result.scalar_one_or_none() is None:
                    logger.info(
                        "[ConversationStore] 跳过未注册用户的会话持久化: user_id=%s thread_id=%s",
                        user_id,
                        thread_id,
                    )
                    return

                result = await session.execute(
                    select(Conversation).where(
                        Conversation.thread_id == thread_id,
                        Conversation.user_id == user_id,
                    )
                )
                conversation = result.scalar_one_or_none()
                title = self._title_from_question(question)
                now = datetime.now()

                if conversation is None:
                    conversation = Conversation(
                        user_id=user_id,
                        thread_id=thread_id,
                        title=title,
                        metadata_=self._build_metadata(
                            agent_code=agent_code,
                            workflow_name=workflow_name,
                            metadata=metadata,
                        ),
                    )
                    session.add(conversation)
                    try:
                        await session.commit()
                        logger.info(
                            "[ConversationStore] 已创建运行时会话记录: user_id=%s thread_id=%s agent=%s workflow=%s",
                            user_id,
                            thread_id,
                            agent_code,
                            workflow_name,
                        )
                        return
                    except IntegrityError:
                        await session.rollback()
                        result = await session.execute(
                            select(Conversation).where(
                                Conversation.thread_id == thread_id,
                                Conversation.user_id == user_id,
                            )
                        )
                        conversation = result.scalar_one_or_none()
                        if conversation is None:
                            user_result = await session.execute(
                                select(User.user_id).where(User.user_id == user_id)
                            )
                            if user_result.scalar_one_or_none() is None:
                                logger.info(
                                    "[ConversationStore] 用户已不存在，跳过会话持久化: user_id=%s thread_id=%s",
                                    user_id,
                                    thread_id,
                                )
                                return
                            raise

                changed = False
                if conversation.is_deleted:
                    conversation.is_deleted = False
                    conversation.title = title
                    changed = True
                if not conversation.title or conversation.title == "新对话":
                    conversation.title = title
                    changed = True

                base_meta = dict(conversation.metadata_ or {})
                runtime_meta = dict(base_meta.get("agent_runtime") or {})
                runtime_meta.update(
                    {
                        "agent_code": agent_code,
                        "workflow_name": workflow_name,
                        "last_question": str(question or "")[:500],
                        "updated_at": now.isoformat(),
                    }
                )
                if metadata:
                    runtime_meta["request_metadata"] = metadata
                base_meta["agent_runtime"] = runtime_meta
                conversation.metadata_ = base_meta
                conversation.updated_at = now
                changed = True

                if changed:
                    await session.commit()
                    logger.info(
                        "[ConversationStore] 已更新运行时会话记录: user_id=%s thread_id=%s agent=%s workflow=%s",
                        user_id,
                        thread_id,
                        agent_code,
                        workflow_name,
                    )
            except Exception:
                logger.exception(
                    "[ConversationStore] 运行时会话记录保存失败: user_id=%s thread_id=%s agent=%s workflow=%s",
                    user_id,
                    thread_id,
                    agent_code,
                    workflow_name,
                )
                try:
                    await session.rollback()
                except Exception:
                    pass

    def _build_metadata(
        self,
        *,
        agent_code: str,
        workflow_name: str,
        metadata: dict[str, Any] | None,
    ) -> dict[str, Any]:
        return {
            "agent_runtime": {
                "agent_code": agent_code,
                "workflow_name": workflow_name,
                "request_metadata": metadata or {},
                "created_at": datetime.now().isoformat(),
            }
        }

    def _title_from_question(self, question: str) -> str:
        text = str(question or "").strip().replace("\n", " ")
        if not text:
            return "新对话"
        return text[:30] + ("..." if len(text) > 30 else "")
