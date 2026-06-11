"""访客独立 API 路由

提供无需登录的访客流式问答接口：
  - POST /api/v2/guest/query
"""

import asyncio
import json
import logging
import re
import uuid
from typing import AsyncIterator

from fastapi import APIRouter, HTTPException, Request
from fastapi.responses import StreamingResponse
from pydantic import BaseModel, Field

from backend.application.checkpointing.checkpointer import get_checkpointer_from_request
from backend.application.agent_service import AgentService
from backend.application.event_stream.thinking_filter import split_thinking_segments
from backend.application.suggestions.follow_up_service import FollowUpContext, get_follow_up_service
from backend.config.settings import settings as app_settings
from backend.domain.agents.events import AgentEvent
from backend.domain.agents.request import AgentRequest

logger = logging.getLogger(__name__)

router = APIRouter(prefix="/guest", tags=["guest-v2"])

def resolve_device_id(request: Request) -> str:
    """Resolve or generate device id for guest traffic."""
    header_value = (request.headers.get("X-Device-ID") or "").strip()
    if header_value:
        safe = re.sub(r"[^a-zA-Z0-9_-]", "", header_value)
        if safe:
            return safe[:64]
    return f"anon_{uuid.uuid4().hex[:16]}"


class GuestQueryRequest(BaseModel):
    question: str = Field(..., min_length=1, description="访客问题")
    thread_id: str | None = Field(default=None, description="访客线程 ID")
    model: str | None = Field(default=None, description="模型名称")
    use_search: bool = Field(default=True, description="是否启用知识库与联网搜索")
    kb_ids: list[str] | None = Field(default=None, description="检索知识库 ID 列表")
    mode: str | None = Field(default=None, description="前端模式（访客禁用 deep-research）")
    task_type: str | None = Field(default=None, description="任务类型（访客禁用 deep_research）")
    subagent_enabled: bool | None = Field(default=None, description="是否启用子智能体（访客固定禁用）")


async def _iter_guest_edge_events(
    *,
    payload: GuestQueryRequest,
    device_id: str,
    internal_tid: str,
    request: Request,
) -> AsyncIterator[dict]:
    service = AgentService(checkpointer=get_checkpointer_from_request(request))
    agent_request = AgentRequest(
        question=payload.question,
        agent_code="ne",
        user_id=f"guest:{device_id}",
        conversation_id=internal_tid,
        requested_workflow="lightweight",
        selected_kb_ids=list(payload.kb_ids or []),
        selected_skills=[],
        model_name=payload.model,
        use_search=payload.use_search,
        subagent_enabled=False,
        thinking_enabled=False,
        metadata={"source": "guest.query"},
    )
    async for event in service.run_stream(agent_request):
        edge_event = _map_standard_event_to_guest_edge(event)
        if edge_event is not None:
            yield edge_event
        if event.type == "done":
            break


def _map_standard_event_to_guest_edge(event: AgentEvent) -> dict | None:
    if event.type == "message.delta":
        return {"type": "chunk", "content": str(event.payload.get("content") or "")}
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
async def query_guest(payload: GuestQueryRequest, request: Request):
    """访客流式问答（SSE）。"""
    try:
        mode = str(payload.mode or "").strip().lower()
        task_type = str(payload.task_type or "").strip().lower()
        if mode == "deep-research" or task_type == "deep_research" or payload.subagent_enabled:
            raise HTTPException(status_code=400, detail="访客模式不支持深度研究")

        external_tid = (payload.thread_id or str(uuid.uuid4())).strip()
        device_id = resolve_device_id(request)
        internal_tid = f"g_{device_id}__t_{external_tid}"
        logger.info("[Guest V2] 开始处理查询, internal_thread_id=%s", internal_tid)

        async def stream_generator():
            chunk_count = 0
            in_thought = False
            think_carry = ""
            accumulated_answer = ""
            follow_up_service = get_follow_up_service()
            follow_up_service.reset_tool_summary()
            follow_up_stream = None
            if app_settings.follow_up_suggestions_enabled:
                context_parts = [
                    "模式: guest/lightweight",
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

            try:
                async for event in _iter_guest_edge_events(
                    payload=payload,
                    device_id=device_id,
                    internal_tid=internal_tid,
                    request=request,
                ):
                    if event["type"] == "chunk":
                        chunk_count += 1
                        visible, thinking, in_thought, think_carry = split_thinking_segments(event.get("content", ""), in_thought, think_carry)
                        if thinking:
                            yield f"data: {json.dumps({'type': 'thinking', 'content': thinking}, ensure_ascii=False)}\n\n"
                        if visible:
                            accumulated_answer += visible
                            if follow_up_stream is not None:
                                follow_up_stream.note_visible(visible)
                            yield f"data: {json.dumps({'type': 'chunk', 'content': visible}, ensure_ascii=False)}\n\n"
                            if follow_up_event := await emit_follow_up_if_ready():
                                yield follow_up_event

                    elif event["type"] == "status":
                        yield f"data: {json.dumps({'type': 'status', 'status': event.get('content', '')}, ensure_ascii=False)}\n\n"
                        if follow_up_event := await emit_follow_up_if_ready():
                            yield follow_up_event

                    elif event["type"] == "thinking":
                        yield f"data: {json.dumps({'type': 'thinking', 'content': event.get('content', '')}, ensure_ascii=False)}\n\n"
                        if follow_up_event := await emit_follow_up_if_ready():
                            yield follow_up_event

                    elif event["type"] == "event":
                        follow_up_service.note_process_event(event.get("event", {}) or {})
                        yield f"data: {json.dumps({'type': 'event', 'event': event.get('event', {})}, ensure_ascii=False)}\n\n"
                        if follow_up_event := await emit_follow_up_if_ready():
                            yield follow_up_event

                    elif event["type"] == "error":
                        yield f"data: {json.dumps({'error': event.get('message', event.get('content', ''))}, ensure_ascii=False)}\n\n"

                    elif event["type"] == "done":
                        break

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
                yield f"data: {json.dumps({'error': '连接已取消'}, ensure_ascii=False)}\n\n"
                yield "data: [DONE]\n\n"
            except Exception as e:
                logger.exception("[Guest V2] 流式生成失败: %s", e)
                yield f"data: {json.dumps({'error': str(e)}, ensure_ascii=False)}\n\n"
                yield "data: [DONE]\n\n"

        return StreamingResponse(
            stream_generator(),
            media_type="text/event-stream",
            headers={
                "Cache-Control": "no-cache",
                "Connection": "keep-alive",
                "X-Accel-Buffering": "no",
            },
        )
    except Exception as e:
        logger.exception("[Guest V2] 创建智能体失败: %s", e)
        raise HTTPException(status_code=500, detail=f"创建访客智能体失败: {str(e)}")
