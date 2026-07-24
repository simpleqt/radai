"""访客独立 API 路由

提供无需登录的访客流式问答接口：
  - POST /api/v2/guest/query
"""

import asyncio
import hashlib
import hmac
import json
import logging
import re
import uuid
from collections.abc import AsyncIterator

from fastapi import APIRouter, Depends, HTTPException, Request
from fastapi.responses import StreamingResponse
from pydantic import BaseModel, Field, model_validator
from sqlalchemy.ext.asyncio import AsyncSession

from backend.api.dependencies.auth import (
    ApiKeyAuth,
    filter_accessible_kb_ids,
    get_db_session,
    optional_verify_api_key,
)
from backend.application.agent_executors.image_url_registry import (
    StreamingTokenResolver,
    resolve_image_tokens,
)
from backend.application.agents.profile_loader import AgentProfileLoader
from backend.application.agent_service import AgentService
from backend.application.checkpointing.checkpointer import get_checkpointer_from_request
from backend.application.event_stream.thinking_filter import split_thinking_segments
from backend.application.suggestions.follow_up_service import (
    FollowUpContext,
    FollowUpSuggestionService,
)
from backend.config.settings import settings as app_settings
from backend.core.config import build_internal_thread_key
from backend.core.thread_key import validate_external_thread_id
from backend.core.utils.async_tasks import (
    close_async_iterator_safely,
    iterate_until_disconnected,
)
from backend.domain.agents.events import AgentEvent
from backend.domain.agents.request import AgentRequest
from backend.domain.agents.workflows import SUBAGENT_WORKFLOWS, normalize_workflow_name

logger = logging.getLogger(__name__)

router = APIRouter(prefix="/guest", tags=["guest-v2"])

_DEVICE_ID_PATTERNS = (
    re.compile(r"gdev_[0-9a-fA-F]{8}-[0-9a-fA-F]{4}-[1-5][0-9a-fA-F]{3}-[89abAB][0-9a-fA-F]{3}-[0-9a-fA-F]{12}"),
    re.compile(r"gdev_[0-9]{10,16}_[a-zA-Z0-9_-]{16,64}"),
)


def _device_scope_id(raw_device_id: str) -> str:
    """Turn a high-entropy browser token into a non-forgeable internal scope."""
    secret = (app_settings.minio_secret_key or "").encode("utf-8")
    if not secret:
        raise RuntimeError("guest device signing secret is unavailable")
    return hmac.new(secret, raw_device_id.encode("utf-8"), hashlib.sha256).hexdigest()[:32]

def resolve_device_id(request: Request) -> str:
    """Resolve a bounded, high-entropy device token for guest traffic."""
    header_value = (request.headers.get("X-Device-ID") or "").strip()
    if header_value:
        if len(header_value) > 80 or not any(
            pattern.fullmatch(header_value) for pattern in _DEVICE_ID_PATTERNS
        ):
            raise HTTPException(status_code=400, detail="X-Device-ID 格式无效")
        return _device_scope_id(header_value)
    return _device_scope_id(f"anon_{uuid.uuid4().hex}")


class GuestQueryRequest(BaseModel):
    question: str = Field(..., min_length=1, max_length=20000, description="访客问题")
    thread_id: str | None = Field(default=None, min_length=1, max_length=100, description="访客线程 ID")
    agent_code: str = Field(
        default="ne",
        min_length=1,
        max_length=64,
        pattern=r"^[A-Za-z][A-Za-z0-9_-]*$",
        description="已注册的智能体 Profile 代码",
    )
    model: str | None = Field(default=None, max_length=128, description="模型名称")
    use_search: bool = Field(default=True, description="是否启用知识库与联网搜索")
    kb_ids: list[str] | None = Field(default=None, max_length=64, description="检索知识库 ID 列表")
    mode: str | None = Field(default=None, max_length=64, description="前端模式（访客禁用 deep-research）")
    task_type: str | None = Field(default=None, max_length=64, description="任务类型（访客禁用 deep_research）")
    subagent_enabled: bool | None = Field(default=None, description="是否启用子智能体（访客固定禁用）")

    @model_validator(mode="after")
    def validate_bounded_identifiers(self):
        if self.thread_id is not None:
            validate_external_thread_id(self.thread_id)
        if any(len(str(item)) > 128 for item in self.kb_ids or []):
            raise ValueError("kb_ids 包含过长条目")
        return self


def _guest_workflow_name(agent_code: str) -> str:
    """Resolve the guest workflow from the registered profile.

    Guest access intentionally follows the profile default instead of inferring
    behavior from well-known agent codes. Coordinated/subagent defaults are not
    silently downgraded because that would run a different agent than declared.
    """

    profile = AgentProfileLoader().load(agent_code)
    workflow = normalize_workflow_name(profile.default_workflow)
    if not profile.allows_workflow(workflow):
        raise ValueError(
            f"Agent profile {profile.code} does not allow its default workflow: {workflow}"
        )
    if workflow in SUBAGENT_WORKFLOWS:
        raise ValueError(
            f"Agent profile {profile.code} default workflow is unavailable to guests: {workflow}"
        )
    return workflow


async def _iter_guest_edge_events(
    *,
    payload: GuestQueryRequest,
    request: Request,
    user_id: str,
    conversation_id: str,
    internal_tid: str,
    guest_workflow: str,
) -> AsyncIterator[dict]:
    service = AgentService(checkpointer=get_checkpointer_from_request(request))
    agent_request = AgentRequest(
        question=payload.question,
        agent_code=payload.agent_code,
        user_id=user_id,
        conversation_id=conversation_id,
        requested_workflow=guest_workflow,
        selected_kb_ids=list(payload.kb_ids or []),
        selected_skills=[],
        model_name=payload.model,
        use_search=payload.use_search,
        subagent_enabled=False,
        thinking_enabled=False,
        metadata={"source": "guest.query"},
    )
    upstream = service.run_stream(agent_request)
    try:
        async for event in upstream:
            edge_event = _map_standard_event_to_guest_edge(event)
            if edge_event is not None:
                yield edge_event
            if event.type == "done":
                break
    finally:
        await close_async_iterator_safely(upstream)


def _map_standard_event_to_guest_edge(event: AgentEvent) -> dict | None:
    if event.type == "message.delta":
        return {"type": "chunk", "content": str(event.payload.get("content") or "")}
    if event.type == "message.replace":
        return {"type": "message.replace", "content": str(event.payload.get("content") or "")}
    if event.type == "reasoning.delta":
        return {"type": "thinking", "content": str(event.payload.get("content") or "")}
    if event.type == "workflow.step":
        if isinstance(event.payload.get("event"), dict):
            return {"type": "event", "event": event.payload.get("event") or {}}
        content = str(event.payload.get("content") or event.payload.get("status") or "")
        return {"type": "status", "content": content}
    if event.type == "workflow.started":
        return {"type": "status", "content": f"workflow.started:{event.payload.get('workflow', '')}"}
    if event.type == "error":
        return {"type": "error", "message": str(event.payload.get("message") or event.payload.get("error") or "")}
    if event.type == "done":
        return {"type": "done"}
    return None


@router.post("/query")
async def query_guest(
    payload: GuestQueryRequest,
    request: Request,
    auth: ApiKeyAuth = Depends(optional_verify_api_key),
    session: AsyncSession = Depends(get_db_session),
):
    """访客流式问答（SSE）。登录态携带 Bearer Token 时写入用户会话。"""
    try:
        try:
            guest_workflow = await asyncio.to_thread(
                _guest_workflow_name,
                payload.agent_code,
            )
        except ValueError as exc:
            detail = str(exc)
            status_code = (
                404 if detail.startswith("Unknown agent profile config:") else 400
            )
            raise HTTPException(status_code=status_code, detail=detail) from exc

        mode = str(payload.mode or "").strip().lower()
        task_type = str(payload.task_type or "").strip().lower()
        if (
            mode == "deep-research"
            or task_type == "deep_research"
            or payload.subagent_enabled
        ):
            raise HTTPException(status_code=400, detail="访客模式不支持深度研究")
        requested_workflow = normalize_workflow_name(task_type or mode)
        if requested_workflow and requested_workflow != guest_workflow:
            raise HTTPException(
                status_code=400,
                detail=f"访客工作流必须与智能体 Profile 默认值一致: {guest_workflow}",
            )

        requested_kb_ids = list(
            dict.fromkeys(
                str(value or "").strip()
                for value in payload.kb_ids or []
                if str(value or "").strip()
            )
        )
        if requested_kb_ids:
            allowed_kb_ids = await filter_accessible_kb_ids(
                session,
                requested_kb_ids,
                auth,
            )
            denied_kb_ids = [
                kb_id for kb_id in requested_kb_ids if kb_id not in set(allowed_kb_ids)
            ]
            if denied_kb_ids:
                raise HTTPException(status_code=403, detail="无权访问所选知识库")
            payload.kb_ids = allowed_kb_ids

        external_tid = (payload.thread_id or str(uuid.uuid4())).strip()
        validate_external_thread_id(external_tid)

        authenticated_user_id = (
            auth.user_id
            if auth.user_id and auth.user_id not in {"", "guest"}
            else None
        )
        if authenticated_user_id:
            user_id = authenticated_user_id
            conversation_id = external_tid
            internal_tid = build_internal_thread_key(authenticated_user_id, external_tid)
        else:
            device_id = resolve_device_id(request)
            user_id = f"guest_{device_id}"
            internal_tid = f"g_{device_id}__t_{external_tid}"
            conversation_id = internal_tid

        logger.info(
            "[Guest V2] 开始处理查询, authenticated=%s, internal_thread_id=%s",
            bool(authenticated_user_id),
            internal_tid,
        )

        async def stream_generator():
            chunk_count = 0
            in_thought = False
            think_carry = ""
            token_resolver = StreamingTokenResolver()
            accumulated_answer = ""
            follow_up_service = FollowUpSuggestionService()
            follow_up_stream = None
            if app_settings.follow_up_suggestions_enabled:
                context_parts = [
                    f"模式: guest/{guest_workflow}",
                    f"联网/检索: {'开启' if payload.use_search else '关闭'}",
                ]
                if payload.kb_ids:
                    context_parts.append(f"已选知识库数量: {len(payload.kb_ids)}")
                follow_up_stream = follow_up_service.start_stream_task(
                    FollowUpContext(
                        user_question=payload.question,
                        context_summary="\n".join(context_parts),
                        agent_mode="guest",
                        selected_skills=[],
                        model_name=payload.model,
                        use_llm=app_settings.follow_up_guest_enabled,
                    )
                )

            async def emit_follow_up_if_ready(*, force: bool = False) -> str | None:
                if follow_up_stream is None:
                    return None
                ready = await follow_up_stream.take_ready(force=force)
                if not ready:
                    return None
                suggestions, sug_meta = ready
                return f"data: {json.dumps({'type': 'follow_up_suggestions', 'suggestions': suggestions, 'metadata': sug_meta}, ensure_ascii=False)}\n\n"

            edge_events = _iter_guest_edge_events(
                payload=payload,
                request=request,
                user_id=user_id,
                conversation_id=conversation_id,
                internal_tid=internal_tid,
                guest_workflow=guest_workflow,
            )
            try:
                async for event in iterate_until_disconnected(edge_events, request):
                    if event["type"] == "chunk":
                        chunk_count += 1
                        visible, thinking, in_thought, think_carry = split_thinking_segments(event.get("content", ""), in_thought, think_carry)
                        if thinking:
                            thinking = resolve_image_tokens(thinking)
                            yield f"data: {json.dumps({'type': 'thinking', 'content': thinking}, ensure_ascii=False)}\n\n"
                        if visible:
                            visible = token_resolver.feed(visible)
                        if visible:
                            accumulated_answer += visible
                            if follow_up_stream is not None:
                                follow_up_stream.note_visible(visible)
                            yield f"data: {json.dumps({'type': 'chunk', 'content': visible}, ensure_ascii=False)}\n\n"
                            if follow_up_event := await emit_follow_up_if_ready():
                                yield follow_up_event

                    elif event["type"] == "message.replace":
                        content = resolve_image_tokens(str(event.get("content") or ""))
                        accumulated_answer = content
                        if follow_up_stream is not None:
                            follow_up_stream.note_visible(content)
                        yield f"data: {json.dumps({'type': 'message.replace', 'content': content}, ensure_ascii=False)}\n\n"
                        if follow_up_event := await emit_follow_up_if_ready():
                            yield follow_up_event

                    elif event["type"] == "status":
                        status = resolve_image_tokens(str(event.get("content", "")))
                        yield f"data: {json.dumps({'type': 'status', 'status': status}, ensure_ascii=False)}\n\n"
                        if follow_up_event := await emit_follow_up_if_ready():
                            yield follow_up_event

                    elif event["type"] == "thinking":
                        thinking = resolve_image_tokens(str(event.get("content", "")))
                        yield f"data: {json.dumps({'type': 'thinking', 'content': thinking}, ensure_ascii=False)}\n\n"
                        if follow_up_event := await emit_follow_up_if_ready():
                            yield follow_up_event

                    elif event["type"] == "event":
                        event_payload = event.get("event", {}) or {}
                        event_payload_str = json.dumps(event_payload, ensure_ascii=False)
                        event_payload_str = resolve_image_tokens(event_payload_str)
                        event_payload = json.loads(event_payload_str)
                        follow_up_service.note_process_event(event_payload)
                        yield f"data: {json.dumps({'type': 'event', 'event': event_payload}, ensure_ascii=False)}\n\n"
                        if follow_up_event := await emit_follow_up_if_ready():
                            yield follow_up_event

                    elif event["type"] == "error":
                        yield f"data: {json.dumps({'error': event.get('message', event.get('content', ''))}, ensure_ascii=False)}\n\n"

                    elif event["type"] == "done":
                        break

                if await request.is_disconnected():
                    logger.info(
                        "[Guest V2] 客户端已断开，停止后处理: thread_id=%s",
                        internal_tid,
                    )
                    return

                remaining = token_resolver.flush()
                if remaining:
                    accumulated_answer += remaining
                    if follow_up_stream is not None:
                        follow_up_stream.note_visible(remaining)
                    yield f"data: {json.dumps({'type': 'chunk', 'content': remaining}, ensure_ascii=False)}\n\n"
                    if follow_up_event := await emit_follow_up_if_ready():
                        yield follow_up_event

                logger.info("[Guest V2] 流式生成完成, 共发送 %s 个 chunk", chunk_count)
                if follow_up_event := await emit_follow_up_if_ready(force=True):
                    yield follow_up_event
                if follow_up_stream is not None:
                    follow_up_stream.cancel()
                yield "data: [DONE]\n\n"
            except asyncio.CancelledError:
                if follow_up_stream is not None:
                    follow_up_stream.cancel()
                logger.warning("[Guest V2] 流式连接被取消: thread_id=%s", internal_tid)
                raise
            except Exception as e:
                logger.exception("[Guest V2] 流式生成失败: %s", e)
                yield f"data: {json.dumps({'error': '访客智能体暂时不可用'}, ensure_ascii=False)}\n\n"
                yield "data: [DONE]\n\n"
            finally:
                if follow_up_stream is not None:
                    follow_up_stream.cancel()
                await close_async_iterator_safely(edge_events)

        return StreamingResponse(
            stream_generator(),
            media_type="text/event-stream",
            headers={
                "Cache-Control": "no-cache",
                "Connection": "keep-alive",
                "X-Accel-Buffering": "no",
            },
        )
    except HTTPException:
        raise
    except Exception as e:
        logger.exception("[Guest V2] 创建智能体失败: %s", e)
        raise HTTPException(status_code=500, detail="创建访客智能体失败") from e
