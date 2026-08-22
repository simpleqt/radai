from datetime import datetime
from typing import Any

from fastapi import APIRouter, Depends, HTTPException, Query, Request
from langgraph.types import Overwrite
from pydantic import BaseModel, Field
from sqlalchemy import case, func, select
from sqlalchemy.ext.asyncio import AsyncSession

from backend.api.routers.auth import get_current_user
from backend.application.agent_service import AgentService
from backend.application.blocking import run_blocking
from backend.application.checkpointing.checkpointer import get_checkpointer_from_request
from backend.application.checkpointing.guest_fallback import (
    aget_checkpoint_with_fallback,
)
from backend.application.conversations.local_catalog import (
    get_local_conversation_catalog,
    shared_object_storage_read_only,
)
from backend.application.conversations.message_serializer import (
    serialize_checkpoint_messages,
)
from backend.config.loggers import get_logger
from backend.core.config import build_internal_thread_key, get_paths
from backend.db.connection import get_async_session
from backend.db.models import Conversation, User
from backend.domain.agents.request import AgentRequest

router = APIRouter(prefix="/conversations", tags=["对话管理"])
shared_router = APIRouter(prefix="/conversations", tags=["对话管理"])
logger = get_logger(__name__)


class ConversationResponse(BaseModel):
    id: str
    thread_id: str
    title: str
    metadata: dict | None = None
    created_at: str
    updated_at: str
    pinned: bool = False


class RenameRequest(BaseModel):
    """会话属性更新：重命名与置顶共用一个 PATCH 入口。"""

    title: str | None = None
    pinned: bool | None = None


def _metadata_pinned(metadata: dict | None) -> bool:
    return bool((metadata or {}).get("pinned_at"))


class RollbackRequest(BaseModel):
    keep_user_turns: int = Field(..., ge=0)


@router.get("", response_model=list[ConversationResponse])
async def list_conversations(
    limit: int = Query(20, ge=1, le=100),
    offset: int = Query(0, ge=0),
    include_metadata: bool = Query(
        True,
        description="是否返回会话元数据；侧边栏列表可关闭以减少查询数据量",
    ),
    user: User = Depends(get_current_user),
    session: AsyncSession = Depends(get_async_session),
):
    """获取当前用户的会话列表"""
    local_catalog = get_local_conversation_catalog()
    if local_catalog is not None:
        conversations = await local_catalog.list_for_user(
            user_id=user.user_id,
            limit=limit,
            offset=offset,
        )
        return [
            ConversationResponse(
                id=conversation.id,
                thread_id=conversation.thread_id,
                title=conversation.title,
                metadata=(conversation.metadata_ if include_metadata else None),
                created_at=conversation.created_at.isoformat(),
                updated_at=conversation.updated_at.isoformat(),
                pinned=_metadata_pinned(conversation.metadata_),
            )
            for conversation in conversations
        ]

    # The sidebar only needs summary columns.  When it opts out, avoid loading
    # the metadata JSON: it can contain runtime state and image registrations
    # and made the first history-page request unnecessarily expensive.  The
    # pinned flag rides a tiny JSON_EXTRACT projection instead of the full
    # metadata payload.
    pinned_expr = case(
        (
            func.json_extract(Conversation.metadata_, "$.pinned_at").isnot(None),
            1,
        ),
        else_=0,
    ).label("pinned")
    columns = [
        Conversation.id,
        Conversation.thread_id,
        Conversation.title,
        Conversation.created_at,
        Conversation.updated_at,
        pinned_expr,
    ]
    if include_metadata:
        columns.append(Conversation.metadata_)
    result = await session.execute(
        select(*columns)
        .where(
            Conversation.user_id == user.user_id,
            Conversation.is_deleted.is_(False),
        )
        .order_by(
            pinned_expr.desc(),
            Conversation.updated_at.desc(),
            Conversation.id.desc(),
        )
        .offset(offset)
        .limit(limit)
    )
    conversations = result.all()

    return [
        ConversationResponse(
            id=c.id,
            thread_id=c.thread_id,
            title=c.title,
            metadata=c.metadata_ if include_metadata else None,
            created_at=c.created_at.isoformat() if c.created_at else "",
            updated_at=c.updated_at.isoformat() if c.updated_at else "",
            pinned=bool(getattr(c, "pinned", 0)),
        )
        for c in conversations
    ]


@shared_router.patch("/{thread_id}")
async def rename_conversation(
    thread_id: str,
    request: RenameRequest,
    user: User = Depends(get_current_user),
    session: AsyncSession = Depends(get_async_session),
):
    """更新会话属性（重命名 / 置顶）"""
    if request.title is None and request.pinned is None:
        raise HTTPException(status_code=422, detail="title 与 pinned 至少提供一个")

    local_catalog = get_local_conversation_catalog()
    if local_catalog is not None:
        renamed = False
        if request.title is not None:
            renamed = await local_catalog.rename(
                user_id=user.user_id,
                thread_id=thread_id,
                title=request.title,
            )
        if request.pinned is not None:
            updated = await local_catalog.set_pinned(
                user_id=user.user_id,
                thread_id=thread_id,
                pinned=request.pinned,
            )
            if not updated and not renamed:
                raise HTTPException(status_code=404, detail="会话不存在")
        elif not renamed:
            raise HTTPException(status_code=404, detail="会话不存在")
        return {"success": True}

    result = await session.execute(
        select(Conversation).where(
            Conversation.thread_id == thread_id,
            Conversation.user_id == user.user_id,
            Conversation.is_deleted.is_(False),
        )
    )
    conv = result.scalar_one_or_none()
    if not conv:
        if request.title is None:
            raise HTTPException(status_code=404, detail="会话不存在")
        # If not in DB yet, it might be a fresh thread being renamed
        # Create it
        conv = Conversation(
            user_id=user.user_id,
            thread_id=thread_id,
            title=request.title
        )
        session.add(conv)
    elif request.title is not None:
        conv.title = request.title

    if request.pinned is not None:
        metadata = dict(conv.metadata_ or {})
        if request.pinned:
            metadata["pinned_at"] = datetime.now().isoformat()
        else:
            metadata.pop("pinned_at", None)
        conv.metadata_ = metadata or None

    await session.commit()
    return {"success": True}


@shared_router.delete("/{thread_id}")
async def delete_conversation(
    thread_id: str,
    request: Request,
    user: User = Depends(get_current_user),
    session: AsyncSession = Depends(get_async_session),
):
    """逻辑删除会话，并清理 checkpointer 和沙箱目录"""
    local_catalog = get_local_conversation_catalog()
    if local_catalog is not None:
        conv = await local_catalog.get(
            user_id=user.user_id,
            thread_id=thread_id,
        )
    else:
        result = await session.execute(
            select(Conversation).where(
                Conversation.thread_id == thread_id,
                Conversation.user_id == user.user_id
            )
        )
        conv = result.scalar_one_or_none()
    if not conv:
        return {"success": True}

    internal_thread_id = build_internal_thread_key(user.user_id, thread_id)
    if local_catalog is not None:
        await local_catalog.set_deleted(
            user_id=user.user_id,
            thread_id=thread_id,
            deleted=True,
        )
    else:
        conv.is_deleted = True
        await session.commit()

    cleanup_errors: list[str] = []
    try:
        from backend.application.agent_tasks import agent_task_registry

        runtime_metadata = dict((conv.metadata_ or {}).get("agent_runtime") or {})
        agent_code = str(runtime_metadata.get("agent_code") or "ne").strip() or "ne"
        active_task = await agent_task_registry.active_for_thread(
            user_id=user.user_id,
            agent_code=agent_code,
            thread_id=thread_id,
        )
        if active_task is not None:
            await agent_task_registry.cancel(active_task)
    except Exception as exc:
        logger.exception("停止会话后台任务失败: thread=%s", internal_thread_id)
        cleanup_errors.append(f"background_task: {type(exc).__name__}")

    if shared_object_storage_read_only():
        logger.info(
            "共享对象存储为只读，跳过测试会话 MinIO 清理: thread=%s",
            internal_thread_id,
        )
    else:
        try:
            from backend.core.storage.scoped_storage import ScopedStorage

            storage = ScopedStorage(user_id=user.user_id, thread_id=thread_id)
            await run_blocking(storage.delete_all_files, timeout_seconds=30)
        except Exception as exc:
            logger.exception("清理 MinIO 线程附件失败: thread=%s", internal_thread_id)
            cleanup_errors.append(f"storage: {type(exc).__name__}")
        # artifacts/ 之外的线程作用域对象（导出、深研报告、HTML PPT、
        # 会话聊天文件）此前全部遗留；chat-files 里仍被个人文件库引用的
        # 文件除外。
        try:
            from backend.db.models import SessionFile, UserFile
            from backend.application.knowledge.internal_client import (
        internal_staging_delete_prefix as delete_staging_prefix,
    )
            from backend.storage import get_user_minio_bucket_name

            referenced_ids = set(
                (
                    await session.execute(
                        select(SessionFile.file_id)
                        .join(UserFile, UserFile.file_id == SessionFile.file_id)
                        .where(
                            SessionFile.session_id == thread_id,
                            UserFile.status == "active",
                        )
                    )
                ).scalars().all()
            )
            thread_file_ids = set(
                (
                    await session.execute(
                        select(SessionFile.file_id).where(
                            SessionFile.session_id == thread_id
                        )
                    )
                ).scalars().all()
            )
            bucket = get_user_minio_bucket_name()
            base = f"users/{user.user_id}/threads/{thread_id}/"
            for sub in ("artifacts", "exports", "deep-research", "html-ppt"):
                await delete_staging_prefix(f"{base}{sub}/", bucket=bucket)
            for file_id in thread_file_ids - referenced_ids:
                await delete_staging_prefix(
                    f"{base}chat-files/{file_id}/", bucket=bucket
                )
        except Exception as exc:
            logger.exception(
                "清理线程作用域 MinIO 附加前缀失败: thread=%s", internal_thread_id
            )
            cleanup_errors.append(f"storage_extra: {type(exc).__name__}")

    try:
        await run_blocking(
            get_paths().delete_thread_dir,
            internal_thread_id,
            timeout_seconds=10,
        )
    except Exception as exc:
        logger.exception("清理沙箱线程目录失败: thread=%s", internal_thread_id)
        cleanup_errors.append(f"sandbox: {type(exc).__name__}")

    # checkpoint 删除是整条链里唯一不可逆的步骤，放在所有可重试清理
    # （MinIO 附件/沙箱目录）成功之后：否则后续清理失败触发自动恢复时，
    # 会话被恢复成「标题还在、消息历史已永久丢失」的空壳。
    if not cleanup_errors:
        try:
            checkpointer = get_checkpointer_from_request(request)
            await checkpointer.adelete_thread(internal_thread_id)
        except Exception as exc:
            logger.exception("清理 checkpointer 失败: thread=%s", internal_thread_id)
            cleanup_errors.append(f"checkpointer: {type(exc).__name__}")

    if cleanup_errors:
        # Merge the marker instead of writing back the snapshot read before
        # cleanup started: cleanup takes seconds, and concurrent writers (for
        # example the fire-and-forget session-memory save) would have their
        # updates overwritten by the stale copy.
        cleanup_marker = {
            "components": cleanup_errors,
            "updated_at": datetime.now().isoformat(),
        }
        if local_catalog is not None:
            # Restore first: merge_metadata_value only matches a live row.
            await local_catalog.set_deleted(
                user_id=user.user_id,
                thread_id=thread_id,
                deleted=False,
            )
            await local_catalog.merge_metadata_value(
                user_id=user.user_id,
                thread_id=thread_id,
                key="deletion_cleanup_error",
                value=cleanup_marker,
            )
        else:
            await session.refresh(conv)
            metadata = dict(conv.metadata_ or {})
            metadata["deletion_cleanup_error"] = cleanup_marker
            conv.is_deleted = False
            conv.metadata_ = metadata
            await session.commit()
        raise HTTPException(status_code=503, detail="会话清理未完成，请稍后重试")

    if local_catalog is not None:
        await local_catalog.discard_metadata_value(
            user_id=user.user_id,
            thread_id=thread_id,
            key="deletion_cleanup_error",
        )
        await local_catalog.set_deleted(
            user_id=user.user_id,
            thread_id=thread_id,
            deleted=True,
        )
    else:
        await session.refresh(conv)
        metadata = dict(conv.metadata_ or {})
        metadata.pop("deletion_cleanup_error", None)
        conv.metadata_ = metadata
        await session.commit()

    return {"success": True}


def _last_question_fallback_message(conv: Conversation, thread_id: str) -> list[dict[str, Any]]:
    runtime_metadata = dict((conv.metadata_ or {}).get("agent_runtime") or {})
    last_question = str(runtime_metadata.get("last_question") or "").strip()
    if not last_question:
        return []
    return [
        {
            "role": "human",
            "content": last_question,
            "metadata": {"source": "conversation_metadata"},
            "id": f"conversation:{thread_id}:last-question",
        }
    ]


@shared_router.get("/{thread_id}/messages")
async def get_conversation_messages(
    thread_id: str,
    request: Request,
    offset: int = Query(0, ge=0, description="从最新消息向前跳过的数量"),
    limit: int = Query(100, ge=1, le=500, description="最多返回的消息数"),
    user: User = Depends(get_current_user),
    session: AsyncSession = Depends(get_async_session),
):
    """获取指定会话的消息历史"""
    local_catalog = get_local_conversation_catalog()
    if local_catalog is not None:
        conv = await local_catalog.get(
            user_id=user.user_id,
            thread_id=thread_id,
            include_deleted=False,
        )
    else:
        result = await session.execute(
            select(Conversation).where(
                Conversation.thread_id == thread_id,
                Conversation.user_id == user.user_id,
                Conversation.is_deleted.is_(False),
            )
        )
        conv = result.scalar_one_or_none()
    if not conv:
        return []

    internal_thread_id = build_internal_thread_key(user.user_id, thread_id)

    try:
        checkpoint = await aget_checkpoint_with_fallback(request, internal_thread_id)
        if not checkpoint:
            return _last_question_fallback_message(conv, thread_id)

        channel_values = checkpoint.get("channel_values", {})
        messages = channel_values.get("messages", [])
        end = max(0, len(messages) - offset)
        start = max(0, end - limit)
        messages = messages[start:end]

        registered_images = (conv.metadata_ or {}).get("registered_images") or {}
        serialized_messages = serialize_checkpoint_messages(
            messages,
            registered_images=registered_images,
        )

        # 工具调用在首个模型轮次异常中断时，checkpoint 可能只保留工具消息。
        # 会话表在请求开始时已可靠保存用户问题，恢复时用它补齐，避免刷新后
        # 出现历史标题存在但对话正文为空的情况。
        has_user_message = any(
            str(item.get("role") or "").lower() in {"user", "human"}
            and bool(str(item.get("content") or "").strip())
            for item in serialized_messages
        )
        if not has_user_message:
            serialized_messages[:0] = _last_question_fallback_message(conv, thread_id)

        return serialized_messages
    except Exception as e:
        logger.exception("Failed to fetch messages for %s", internal_thread_id)
        raise HTTPException(status_code=503, detail="会话历史暂不可用") from e


def _is_user_message(message: Any) -> bool:
    role = ""
    if hasattr(message, "type"):
        role = str(getattr(message, "type", "")).lower()
    elif isinstance(message, dict):
        role = str(message.get("type", message.get("role", ""))).lower()
    return role in {"user", "human"}


def _trim_messages_by_user_turns(messages: list[Any], keep_user_turns: int) -> list[Any]:
    if keep_user_turns <= 0:
        return []

    trimmed: list[Any] = []
    seen_user_turns = 0

    for msg in messages:
        if _is_user_message(msg):
            if seen_user_turns >= keep_user_turns:
                break
            seen_user_turns += 1
            trimmed.append(msg)
            continue

        if seen_user_turns == 0:
            continue
        trimmed.append(msg)

    return trimmed


@shared_router.post("/{thread_id}/rollback")
async def rollback_conversation(
    thread_id: str,
    payload: RollbackRequest,
    request: Request,
    user: User = Depends(get_current_user),
    session: AsyncSession = Depends(get_async_session),
):
    """将会话回退到前 N 个用户轮次。"""
    local_catalog = get_local_conversation_catalog()
    if local_catalog is not None:
        conv = await local_catalog.get(
            user_id=user.user_id,
            thread_id=thread_id,
            include_deleted=False,
        )
    else:
        result = await session.execute(
            select(Conversation).where(
                Conversation.thread_id == thread_id,
                Conversation.user_id == user.user_id,
                Conversation.is_deleted.is_(False),
            )
        )
        conv = result.scalar_one_or_none()
    if not conv:
        raise HTTPException(status_code=404, detail="Conversation not found")

    internal_thread_id = build_internal_thread_key(user.user_id, thread_id)
    conversation_metadata = dict(conv.metadata_ or {})
    runtime_metadata = dict(conversation_metadata.get("agent_runtime") or {})
    agent_code = str(runtime_metadata.get("agent_code") or "ne").strip() or "ne"
    workflow_name = str(runtime_metadata.get("workflow_name") or "full").strip() or "full"
    request_metadata = runtime_metadata.get("request_metadata")
    if not isinstance(request_metadata, dict):
        request_metadata = {}

    try:
        checkpointer = get_checkpointer_from_request(request)
        agent = await AgentService(checkpointer=checkpointer).build_executor_async(
            AgentRequest(
                question="",
                agent_code=agent_code,
                user_id=user.user_id,
                conversation_id=thread_id,
                requested_workflow=workflow_name,
                metadata={
                    **request_metadata,
                    "source": "conversation.rollback",
                },
            )
        )
        config = {"configurable": {"thread_id": internal_thread_id}}
        state = await agent.graph.aget_state(config)
        if not state:
            return {"success": True, "kept_user_turns": payload.keep_user_turns, "remaining_messages": 0}

        current_messages = list(state.values.get("messages", []) or [])
        trimmed_messages = _trim_messages_by_user_turns(current_messages, payload.keep_user_turns)

        # LangGraph 要求 aupdate_state 指定 as_node，否则多节点图会抛
        # InvalidUpdateError: Ambiguous update, specify as_node
        # 取当前状态最后活跃的节点作为 as_node；若无则回退到图的入口节点
        as_node = state.next[0] if state.next else None
        if not as_node:
            # 尝试从 checkpoint 元数据获取上一个执行的节点
            as_node = state.created_by if hasattr(state, "created_by") and state.created_by else None
        if not as_node:
            # 最终回退：取图的第一个节点名
            try:
                node_names = list(agent.graph.nodes.keys())
                # 跳过 __start__ 等虚拟节点
                as_node = next((n for n in node_names if not n.startswith("__")), node_names[0] if node_names else None)
            except Exception:
                logger.warning(
                    "回滚会话时读取图节点名称失败: conversation_id=%s",
                    thread_id,
                    exc_info=True,
                )
                as_node = None

        update_kwargs: dict = {
            "values": {
                "messages": Overwrite(value=trimmed_messages),
                "artifacts": Overwrite(value=[]),
            },
        }
        if as_node:
            update_kwargs["as_node"] = as_node

        await agent.graph.aupdate_state(
            config,
            **update_kwargs,
        )

        if local_catalog is not None:
            await local_catalog.touch(user_id=user.user_id, thread_id=thread_id)
        else:
            conv.updated_at = datetime.now()
            await session.commit()

        return {
            "success": True,
            "kept_user_turns": payload.keep_user_turns,
            "remaining_messages": len(trimmed_messages),
        }
    except Exception as e:
        logger.exception("回退会话失败: %s", e)
        raise HTTPException(status_code=500, detail="回退会话失败") from e
