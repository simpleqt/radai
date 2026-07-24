"""MCP 路由 - 封装查询API供外部调用"""

import asyncio
import json
from collections.abc import AsyncIterator

from fastapi import APIRouter, Depends, HTTPException, Request
from fastapi.responses import StreamingResponse
from pydantic import BaseModel, Field, model_validator

from backend.api.dependencies.auth import ApiKeyAuth, verify_api_key
from backend.application.agent_executors.image_url_registry import (
    StreamingTokenResolver,
    resolve_image_tokens,
)
from backend.application.agent_service import AgentService
from backend.application.event_stream.thinking_filter import split_thinking_segments
from backend.application.mcp.query_service import (
    build_mcp_agent_request,
    invoke_mcp_tool,
    validate_mcp_arguments,
)
from backend.application.mcp.tool_catalog import (
    McpToolCatalogQuery,
    build_mcp_tool_catalog,
)
from backend.config.loggers import get_logger
from backend.core.thread_key import validate_external_thread_id
from backend.core.utils.async_tasks import (
    close_async_iterator_safely,
    iterate_until_disconnected,
)
from backend.domain.agents.events import AgentEvent

router = APIRouter()
logger = get_logger(__name__)


class McpQueryRequest(BaseModel):
    query: str = Field(..., description="用户查询")
    thread_id: str | None = Field(default=None, min_length=1, max_length=100)
    conversation_id: str | None = Field(default=None, min_length=1, max_length=100)

    @model_validator(mode="after")
    def validate_thread_aliases(self):
        for value in (self.thread_id, self.conversation_id):
            if value:
                validate_external_thread_id(value)
        if self.thread_id and self.conversation_id and self.thread_id != self.conversation_id:
            raise ValueError("thread_id 与 conversation_id 必须一致")
        return self


class McpToolCallRequest(BaseModel):
    tool_name: str = Field(..., min_length=1, max_length=128)
    arguments: dict = Field(default_factory=dict)
    agent_code: str | None = None
    workflow: str | None = None
    selected_skills: list[str] | None = None
    thread_id: str | None = Field(default=None, min_length=1, max_length=100)
    conversation_id: str | None = Field(default=None, min_length=1, max_length=100)

    @model_validator(mode="after")
    def validate_thread_aliases(self):
        validate_mcp_arguments(self.arguments)
        for value in (self.thread_id, self.conversation_id):
            if value:
                validate_external_thread_id(value)
        if self.thread_id and self.conversation_id and self.thread_id != self.conversation_id:
            raise ValueError("thread_id 与 conversation_id 必须一致")
        return self


class McpConfigResponse(BaseModel):
    type: str = Field(..., description="传输类型")
    url: str = Field(..., description="SSE服务地址")
    headers: dict = Field(..., description="请求头")


class McpToolCatalogResponse(BaseModel):
    agent_code: str | None = Field(None, description="智能体代码")
    policy_name: str = Field(..., description="工具策略名称")
    domain: str = Field(..., description="智能体领域")
    workflow: str = Field(..., description="工作流")
    selected_skills: list[str] = Field(
        default_factory=list,
        description="已选 Skills",
    )
    tools: list[dict] = Field(default_factory=list, description="MCP-like 工具描述")


@router.get("/config", response_model=McpConfigResponse)
async def get_mcp_config(
    auth: ApiKeyAuth = Depends(verify_api_key),
):
    from backend.config.settings import settings

    server_host = settings.server_host or "YOUR_SERVER_HOST"
    if server_host in {"127.0.0.1", "localhost"}:
        server_host = "0.0.0.0"

    return McpConfigResponse(
        type="sse",
        url=f"http://{server_host}:{settings.server_port}/api/v1/mcp/query",
        headers={
            "Authorization": f"Bearer {auth.api_key}",
            "Content-Type": "application/json",
        },
    )


@router.get("/tools", response_model=McpToolCatalogResponse)
async def list_mcp_tools(
    agent_code: str | None = None,
    policy_name: str | None = None,
    domain: str | None = None,
    workflow: str | None = None,
    selected_skills: str | None = None,
    auth: ApiKeyAuth = Depends(verify_api_key),
):
    """列出 policy 管理的 MCP-like 工具目录。"""
    del auth
    try:
        catalog = build_mcp_tool_catalog(
            McpToolCatalogQuery(
                agent_code=agent_code,
                policy_name=policy_name,
                domain=domain,
                workflow=workflow,
                selected_skills=_parse_selected_skills(selected_skills),
            )
        )
    except ValueError as exc:
        raise HTTPException(status_code=400, detail=str(exc)) from exc
    return McpToolCatalogResponse(**catalog.to_dict())


@router.post("/tools/call")
async def call_mcp_tool(
    body: McpToolCallRequest,
    http_request: Request,
    auth: ApiKeyAuth = Depends(verify_api_key),
):
    """调用目录中的单个工具，并复用 Agent Runtime 权限/超时治理。"""
    from backend.application.mcp.config import McpServiceRegistry

    service_spec = McpServiceRegistry().get()
    if body.agent_code and body.agent_code != service_spec.agent_code:
        raise HTTPException(status_code=403, detail="MCP 服务不允许切换绑定智能体")
    try:
        from backend.application.checkpointing.checkpointer import get_checkpointer_from_request

        value = await invoke_mcp_tool(
            body.tool_name,
            body.arguments,
            user_id=auth.user_id,
            role=auth.role,
            spec=service_spec,
            workflow=body.workflow,
            selected_skills=body.selected_skills,
            thread_id=body.thread_id,
            conversation_id=body.conversation_id,
            checkpointer=get_checkpointer_from_request(http_request),
        )
        return {"tool_name": body.tool_name, "result": value}
    except PermissionError as exc:
        raise HTTPException(status_code=403, detail=str(exc)) from exc
    except (TimeoutError, ValueError, RuntimeError) as exc:
        raise HTTPException(status_code=400, detail=str(exc)) from exc


def _parse_selected_skills(value: str | None) -> set[str]:
    if not value:
        return set()
    return {item.strip() for item in value.split(",") if item.strip()}


async def stream_generator(
    question: str,
    *,
    user_id: str,
    thread_id: str | None = None,
    conversation_id: str | None = None,
    checkpointer=None,
    request: Request | None = None,
):
    """生成 SSE 流式响应 - 返回与前端 v2 接口相同格式的事件

    事件格式与 /api/v2/agent-runtime/query 一致：
    - type: "chunk"  → {"type": "chunk", "content": "..."}
    - type: "status" → {"type": "status", "status": "..."}
    - type: "event"  → {"type": "event", "event": {...}}
    - type: "error"  → {"type": "error", "message": "..."}
    图片统一使用后端图片管线处理，禁止向客户端输出原始 MinIO 地址。
    """
    edge_events = _iter_mcp_edge_events(
        question=question,
        user_id=user_id,
        thread_id=thread_id,
        conversation_id=conversation_id,
        checkpointer=checkpointer,
    )
    try:
        token_resolver = StreamingTokenResolver()
        in_thought = False
        think_carry = ""

        event_stream = (
            iterate_until_disconnected(edge_events, request)
            if request is not None
            else edge_events
        )
        async for event in event_stream:
            event_type = event.get("type")
            if event_type == "chunk":
                content = event.get("content", "")
                visible, _, in_thought, think_carry = split_thinking_segments(content, in_thought, think_carry)
                if visible:
                    visible = token_resolver.feed(visible)
                    if visible:
                        yield f"data: {json.dumps({'type': 'chunk', 'content': visible}, ensure_ascii=False)}\n\n"
            elif event_type == "message.replace":
                # Coordinated workflows may publish a draft then a corrected
                # final answer.  Preserve replacement semantics for MCP
                # clients instead of silently discarding the final result.
                content = resolve_image_tokens(str(event.get("content") or ""))
                yield f"data: {json.dumps({'type': 'message.replace', 'content': content}, ensure_ascii=False)}\n\n"
            elif event_type == "status":
                yield f"data: {json.dumps({'type': 'status', 'status': event.get('content', '')}, ensure_ascii=False)}\n\n"
            elif event_type == "event":
                event_payload = event.get("event", {})
                event_payload_str = json.dumps(event_payload, ensure_ascii=False)
                event_payload_str = resolve_image_tokens(event_payload_str)
                event_payload = json.loads(event_payload_str)
                yield f"data: {json.dumps({'type': 'event', 'event': event_payload}, ensure_ascii=False)}\n\n"
            elif event_type == "error":
                error_msg = event.get("message", event.get("content", ""))
                yield f"data: {json.dumps({'type': 'error', 'message': error_msg}, ensure_ascii=False)}\n\n"
            elif event_type == "done":
                break

        if request is not None and await request.is_disconnected():
            logger.info("MCP 客户端已断开，停止流式后处理")
            return

        remaining = token_resolver.flush()
        if remaining:
            yield f"data: {json.dumps({'type': 'chunk', 'content': remaining}, ensure_ascii=False)}\n\n"

        yield "data: [DONE]\n\n"
    except asyncio.CancelledError:
        logger.warning("MCP 流式连接被取消")
        raise
    except Exception as e:
        logger.exception(f"MCP 流式查询失败: {e}")
        yield f"data: {json.dumps({'type': 'error', 'message': str(e)}, ensure_ascii=False)}\n\n"
        yield "data: [DONE]\n\n"
    finally:
        await close_async_iterator_safely(edge_events)


async def _iter_mcp_edge_events(
    *,
    question: str,
    user_id: str,
    thread_id: str | None = None,
    conversation_id: str | None = None,
    checkpointer=None,
) -> AsyncIterator[dict]:
    service = AgentService(checkpointer=checkpointer)
    agent_request = build_mcp_agent_request(
        question=question,
        user_id=user_id,
        source="mcp.query",
        thread_id=thread_id,
        conversation_id=conversation_id,
    )
    upstream = service.run_stream(agent_request)
    try:
        async for event in upstream:
            edge_event = _map_standard_event_to_mcp_edge(event)
            if edge_event is not None:
                yield edge_event
            if event.type == "done":
                break
    finally:
        await close_async_iterator_safely(upstream)


def _map_standard_event_to_mcp_edge(event: AgentEvent) -> dict | None:
    if event.type == "message.delta":
        return {"type": "chunk", "content": str(event.payload.get("content") or "")}
    if event.type == "message.replace":
        return {"type": "message.replace", "content": str(event.payload.get("content") or "")}
    if event.type == "workflow.step":
        if isinstance(event.payload.get("event"), dict):
            return {"type": "event", "event": event.payload.get("event") or {}}
        return {"type": "status", "content": str(event.payload.get("content") or event.payload.get("status") or "")}
    if event.type == "reasoning.delta":
        return None
    if event.type == "workflow.started":
        return {"type": "status", "content": f"workflow.started:{event.payload.get('workflow', '')}"}
    if event.type == "error":
        return {"type": "error", "message": str(event.payload.get("message") or event.payload.get("error") or "")}
    if event.type == "done":
        return {"type": "done"}
    return None


@router.post("/query")
async def mcp_query_stream(
    body: McpQueryRequest,
    http_request: Request,
    auth: ApiKeyAuth = Depends(verify_api_key),
):
    """MCP 流式查询接口"""
    try:
        from backend.application.checkpointing.checkpointer import get_checkpointer_from_request

        return StreamingResponse(
            stream_generator(
                body.query,
                user_id=auth.user_id,
                thread_id=body.thread_id,
                conversation_id=body.conversation_id,
                checkpointer=get_checkpointer_from_request(http_request),
                request=http_request,
            ),
            media_type="text/event-stream",
            headers={
                "Cache-Control": "no-cache",
                "Connection": "keep-alive",
                "X-Accel-Buffering": "no",
            },
        )
    except Exception as e:
        logger.exception(f"MCP 流式查询初始化失败: {e}")
        raise HTTPException(status_code=500, detail="MCP 流式查询初始化失败")
