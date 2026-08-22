"""Conversation list persistence for agent runtime sessions."""

from __future__ import annotations

import asyncio
import logging
import os
from datetime import datetime
from typing import Any

from sqlalchemy import select
from sqlalchemy.exc import IntegrityError

from backend.application.conversations.errors import (
    ConversationAgentMismatchError,
    ConversationDeletedError,
    ConversationStateError,
)
from backend.application.conversations.local_catalog import (
    get_local_conversation_catalog,
)
from backend.db.connection import get_async_session_factory
from backend.db.models import Conversation, User

logger = logging.getLogger(__name__)


def _conversation_writes_enabled() -> bool:
    """Return whether runtime conversation metadata may be written at all."""

    return os.getenv("AGENT_CONVERSATION_WRITES_ENABLED", "true").strip().lower() in {
        "1",
        "true",
        "yes",
        "on",
    }


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
        if not _conversation_writes_enabled():
            logger.info(
                "[ConversationStore] 运行时会话目录写入已禁用，"
                "仅保留 Checkpointer: user_id=%s thread_id=%s",
                user_id,
                thread_id,
            )
            return

        local_catalog = get_local_conversation_catalog()
        if local_catalog is not None:
            await local_catalog.ensure_runtime_conversation(
                user_id=user_id,
                thread_id=thread_id,
                title=self._title_from_question(question),
                agent_code=agent_code,
                workflow_name=workflow_name,
                question=question,
                metadata=metadata,
            )
            return

        session_factory = get_async_session_factory()
        async with session_factory() as session:
            try:
                user_result = await asyncio.wait_for(
                    session.execute(
                        select(User.user_id).where(User.user_id == user_id)
                    ),
                    timeout=10.0,
                )
                if user_result.scalar_one_or_none() is None:
                    logger.info(
                        "[ConversationStore] 跳过未注册用户的会话持久化: user_id=%s thread_id=%s",
                        user_id,
                        thread_id,
                    )
                    return

                conversation = await self._lock_conversation(
                    session,
                    user_id=user_id,
                    thread_id=thread_id,
                )
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
                            question=question,
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
                        conversation = await self._lock_conversation(
                            session,
                            user_id=user_id,
                            thread_id=thread_id,
                        )
                        if conversation is None:
                            user_result = await asyncio.wait_for(
                                session.execute(
                                    select(User.user_id).where(User.user_id == user_id)
                                ),
                                timeout=10.0,
                            )
                            if user_result.scalar_one_or_none() is None:
                                logger.info(
                                    "[ConversationStore] 用户已不存在，跳过会话持久化: user_id=%s thread_id=%s",
                                    user_id,
                                    thread_id,
                                )
                                return
                            raise

                if conversation.is_deleted:
                    raise ConversationDeletedError(
                        f"Conversation thread has been deleted: {thread_id}"
                    )

                base_meta = dict(conversation.metadata_ or {})
                runtime_meta = dict(base_meta.get("agent_runtime") or {})
                owner_agent_code = (
                    str(runtime_meta.get("agent_code") or "").strip().lower()
                )
                requested_agent_code = str(agent_code or "").strip().lower()
                if owner_agent_code and owner_agent_code != requested_agent_code:
                    raise ConversationAgentMismatchError(
                        "Conversation thread belongs to a different agent: "
                        f"thread_id={thread_id} owner={owner_agent_code} requested={requested_agent_code}"
                    )

                changed = False
                if not conversation.title or conversation.title == "新对话":
                    conversation.title = title
                    changed = True

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
            except ConversationStateError:
                await session.rollback()
                raise
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
                    logger.warning(
                        "[ConversationStore] 运行时会话回滚失败: user_id=%s thread_id=%s",
                        user_id,
                        thread_id,
                        exc_info=True,
                    )
                # AgentService deliberately fails closed when the runtime
                # conversation cannot be recorded.  Swallowing this exception
                # made the service continue with an unpersisted turn and
                # produced stream/refresh divergence.
                raise

    async def update_conversation_images(
        self,
        *,
        user_id: str,
        thread_id: str,
        registered_images: dict[str, Any],
    ) -> None:
        if not registered_images or not _conversation_writes_enabled():
            return
        local_catalog = get_local_conversation_catalog()
        if local_catalog is not None:
            await local_catalog.merge_registered_images(
                user_id=user_id,
                thread_id=thread_id,
                registered_images=registered_images,
            )
            return
        session_factory = get_async_session_factory()
        async with session_factory() as session:
            try:
                conversation = await self._lock_conversation(
                    session,
                    user_id=user_id,
                    thread_id=thread_id,
                )
                if conversation is None:
                    return

                base_meta = dict(conversation.metadata_ or {})
                existing_images = dict(base_meta.get("registered_images") or {})
                existing_images.update(registered_images)
                base_meta["registered_images"] = existing_images
                conversation.metadata_ = base_meta
                conversation.updated_at = datetime.now()
                await session.commit()
                logger.info(
                    "[ConversationStore] 已更新会话图片注册表: user_id=%s thread_id=%s count=%d",
                    user_id,
                    thread_id,
                    len(existing_images),
                )
            except Exception:
                logger.exception(
                    "[ConversationStore] 更新会话图片注册表失败: user_id=%s thread_id=%s",
                    user_id,
                    thread_id,
                )
                try:
                    await session.rollback()
                except Exception:
                    logger.warning(
                        "[ConversationStore] 图片注册表回滚失败: user_id=%s thread_id=%s",
                        user_id,
                        thread_id,
                        exc_info=True,
                    )
                raise

    async def get_registered_images(
        self,
        *,
        user_id: str,
        thread_id: str,
    ) -> dict[str, Any]:
        """Load durable image identities needed to resolve later turns."""

        local_catalog = get_local_conversation_catalog()
        if local_catalog is not None:
            conversation = await local_catalog.get(
                user_id=user_id,
                thread_id=thread_id,
                include_deleted=False,
            )
            return (
                dict((conversation.metadata_ or {}).get("registered_images") or {})
                if conversation
                else {}
            )

        session_factory = get_async_session_factory()
        async with session_factory() as session:
            result = await session.execute(
                select(Conversation.metadata_).where(
                    Conversation.user_id == user_id,
                    Conversation.thread_id == thread_id,
                    Conversation.is_deleted.is_(False),
                )
            )
            metadata = result.scalar_one_or_none() or {}
            return dict(metadata.get("registered_images") or {})

    async def _lock_conversation(
        self,
        session,
        *,
        user_id: str,
        thread_id: str,
    ) -> Conversation | None:
        """Acquire a row lock for the conversation, waiting if another request holds it.

        Prefer plain ``FOR UPDATE`` over ``SKIP LOCKED``: many MySQL/MariaDB
        deployments in this environment do not support ``SKIP LOCKED`` and reject
        the statement with SQL syntax error 1064, which surfaces as
        ``conversation_persistence_unavailable`` to the client.
        """

        predicate = (
            Conversation.thread_id == thread_id,
            Conversation.user_id == user_id,
        )
        result = await asyncio.wait_for(
            session.execute(select(Conversation).where(*predicate).with_for_update()),
            timeout=10.0,
        )
        return result.scalar_one_or_none()

    def _build_metadata(
        self,
        *,
        agent_code: str,
        workflow_name: str,
        question: str,
        metadata: dict[str, Any] | None,
    ) -> dict[str, Any]:
        return {
            "agent_runtime": {
                "agent_code": agent_code,
                "workflow_name": workflow_name,
                "last_question": str(question or "")[:500],
                "request_metadata": metadata or {},
                "created_at": datetime.now().isoformat(),
            }
        }

    def _title_from_question(self, question: str) -> str:
        text = str(question or "").strip().replace("\n", " ")
        if not text:
            return "新对话"
        return text[:30] + ("..." if len(text) > 30 else "")
