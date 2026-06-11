from datetime import datetime
from typing import Any, Optional
from fastapi import APIRouter, Depends, HTTPException, Query, Request
from pydantic import BaseModel, Field
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession
from backend.db.connection import get_async_session
from backend.db.models import Conversation, User
from backend.api.routers.auth import get_current_user
from backend.application.agent_service import AgentService
from backend.config.loggers import get_logger
from backend.application.checkpointing.checkpointer import get_checkpointer_from_request
from backend.core.config import build_internal_thread_key, get_paths
from backend.domain.agents.request import AgentRequest
from langgraph.types import Overwrite

router = APIRouter(prefix="/conversations", tags=["对话管理"])
logger = get_logger(__name__)


class ConversationResponse(BaseModel):
    id: str
    thread_id: str
    title: str
    metadata: Optional[dict] = None
    created_at: str
    updated_at: str


class RenameRequest(BaseModel):
    title: str


class RollbackRequest(BaseModel):
    keep_user_turns: int = Field(..., ge=0)


@router.get("", response_model=list[ConversationResponse])
async def list_conversations(
    limit: int = Query(20, ge=1, le=100),
    offset: int = Query(0, ge=0),
    user: User = Depends(get_current_user),
    session: AsyncSession = Depends(get_async_session),
):
    """获取当前用户的会话列表"""
    result = await session.execute(
        select(Conversation)
        .where(Conversation.user_id == user.user_id, Conversation.is_deleted == False)
        .order_by(Conversation.updated_at.desc())
        .offset(offset)
        .limit(limit)
    )
    conversations = result.scalars().all()
    
    return [
        ConversationResponse(
            id=c.id,
            thread_id=c.thread_id,
            title=c.title,
            metadata=c.metadata_,
            created_at=c.created_at.isoformat() if c.created_at else "",
            updated_at=c.updated_at.isoformat() if c.updated_at else "",
        )
        for c in conversations
    ]


@router.patch("/{thread_id}")
async def rename_conversation(
    thread_id: str,
    request: RenameRequest,
    user: User = Depends(get_current_user),
    session: AsyncSession = Depends(get_async_session),
):
    """重命名会话"""
    result = await session.execute(
        select(Conversation).where(
            Conversation.thread_id == thread_id,
            Conversation.user_id == user.user_id,
            Conversation.is_deleted == False
        )
    )
    conv = result.scalar_one_or_none()
    if not conv:
        # If not in DB yet, it might be a fresh thread being renamed
        # Create it
        conv = Conversation(
            user_id=user.user_id,
            thread_id=thread_id,
            title=request.title
        )
        session.add(conv)
    else:
        conv.title = request.title
        
    await session.commit()
    return {"success": True}


@router.delete("/{thread_id}")
async def delete_conversation(
    thread_id: str,
    request: Request,
    user: User = Depends(get_current_user),
    session: AsyncSession = Depends(get_async_session),
):
    """逻辑删除会话，并清理 checkpointer 和沙箱目录"""
    result = await session.execute(
        select(Conversation).where(
            Conversation.thread_id == thread_id,
            Conversation.user_id == user.user_id
        )
    )
    conv = result.scalar_one_or_none()
    if not conv:
        return {"success": True}

    conv.is_deleted = True
    await session.commit()

    internal_thread_id = build_internal_thread_key(user.user_id, thread_id)

    try:
        checkpointer = get_checkpointer_from_request(request)
        if hasattr(checkpointer, "conn"):
            existing_tables: set[str] = set()
            cursor = await checkpointer.conn.execute(
                "SELECT name FROM sqlite_master WHERE type='table'"
            )
            rows = await cursor.fetchall()
            existing_tables = {str(r[0]) for r in rows if r and r[0]}

            for table_name in ("checkpoints", "checkpoint_blobs", "checkpoint_writes"):
                if table_name not in existing_tables:
                    logger.debug("checkpointer 表不存在，跳过清理: %s", table_name)
                    continue
                await checkpointer.conn.execute(
                    f"DELETE FROM {table_name} WHERE thread_id = ?",
                    (internal_thread_id,),
                )

            await checkpointer.conn.commit()
    except Exception as e:
        logger.warning(f"清理 checkpointer 失败: {e}")

    try:
        from backend.core.storage.scoped_storage import ScopedStorage
        storage = ScopedStorage(user_id=user.user_id, thread_id=thread_id)
        storage.delete_all_files()
    except Exception as e:
        logger.warning(f"清理 MinIO 线程附件失败: {e}")

    try:
        get_paths().delete_thread_dir(internal_thread_id)
    except Exception as e:
        logger.warning(f"清理沙箱线程目录失败: {e}")

    return {"success": True}


@router.get("/{thread_id}/messages")
async def get_conversation_messages(
    thread_id: str,
    request: Request,
    user: User = Depends(get_current_user),
    session: AsyncSession = Depends(get_async_session),
):
    """获取指定会话的消息历史"""
    result = await session.execute(
        select(Conversation).where(
            Conversation.thread_id == thread_id,
            Conversation.user_id == user.user_id,
            Conversation.is_deleted == False,
        )
    )
    conv = result.scalar_one_or_none()
    if not conv:
        return []

    internal_thread_id = build_internal_thread_key(user.user_id, thread_id)

    try:
        checkpointer = get_checkpointer_from_request(request)
        config = {"configurable": {"thread_id": internal_thread_id}}
        checkpoint = await checkpointer.aget(config)
        if not checkpoint and internal_thread_id != thread_id:
            legacy_config = {"configurable": {"thread_id": thread_id}}
            checkpoint = await checkpointer.aget(legacy_config)
            if checkpoint:
                logger.info(
                    "使用旧外部 thread_id 读取会话历史: user_id=%s thread_id=%s",
                    user.user_id,
                    thread_id,
                )
        
        if not checkpoint:
            return []
        
        channel_values = checkpoint.get("channel_values", {})
        messages = channel_values.get("messages", [])
        
        # Convert LangChain messages to a JSON-serializable format
        import re as _re
        _IMAGE_TOKEN_RE = _re.compile(r"\[IMAGE:\d+\]")

        serialized_messages = []
        for m in messages:
            # Basic mapping
            role = "user"
            if hasattr(m, "type"):
                role = m.type
            elif isinstance(m, dict):
                role = m.get("type", "user")

            content = ""
            if hasattr(m, "content"):
                content = m.content
            elif isinstance(m, dict):
                content = m.get("content", "")

            # 清理 AI 消息中未被替换的 [IMAGE:xxx] token，防止前端渲染为
            # 损坏的代理 URL（这些 token 是因为 _resolve_history_image_tokens
            # 超时/失败后残留在 checkpoint 里的）
            if isinstance(content, str) and role in ("ai", "assistant") and _IMAGE_TOKEN_RE.search(content):
                content = _IMAGE_TOKEN_RE.sub("", content)

            metadata = {}
            if hasattr(m, "additional_kwargs"):
                metadata = m.additional_kwargs
            elif isinstance(m, dict) and "additional_kwargs" in m:
                metadata = m["additional_kwargs"]
            elif isinstance(m, dict) and "metadata" in m:
                metadata = m["metadata"]

            # 过滤自动摘要生成的 JIT 历史压缩消息，避免向前端输出
            if metadata and metadata.get("lc_source") == "summarization":
                continue

            if isinstance(content, str) and (
                "Here is a summary of the conversation to date:" in content or
                content.startswith("[State Elevation:") or
                content.startswith("[Context Isolation:") or
                content.startswith("[System Notification:")
            ):
                continue

            serialized_messages.append({
                "role": role,
                "content": content,
                "metadata": metadata,
                "id": getattr(m, "id", None)
            })
        
        return serialized_messages
    except Exception as e:
        logger.error(f"Failed to fetch messages for {internal_thread_id}: {e}")
        return []


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


@router.post("/{thread_id}/rollback")
async def rollback_conversation(
    thread_id: str,
    payload: RollbackRequest,
    request: Request,
    user: User = Depends(get_current_user),
    session: AsyncSession = Depends(get_async_session),
):
    """将会话回退到前 N 个用户轮次。"""
    result = await session.execute(
        select(Conversation).where(
            Conversation.thread_id == thread_id,
            Conversation.user_id == user.user_id,
            Conversation.is_deleted == False,
        )
    )
    conv = result.scalar_one_or_none()
    if not conv:
        raise HTTPException(status_code=404, detail="Conversation not found")

    internal_thread_id = build_internal_thread_key(user.user_id, thread_id)

    try:
        checkpointer = get_checkpointer_from_request(request)
        agent = AgentService(checkpointer=checkpointer).build_executor(
            AgentRequest(
                question="",
                agent_code="ne",
                user_id=user.user_id,
                conversation_id=thread_id,
                requested_workflow="full",
                metadata={"source": "conversation.rollback"},
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

        conv.updated_at = datetime.now()
        await session.commit()

        return {
            "success": True,
            "kept_user_turns": payload.keep_user_turns,
            "remaining_messages": len(trimmed_messages),
        }
    except Exception as e:
        logger.exception("回退会话失败: %s", e)
        raise HTTPException(status_code=500, detail=f"回退会话失败: {str(e)}")
