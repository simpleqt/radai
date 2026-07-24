"""FastMCP application assembly from platform MCP config."""

from __future__ import annotations

import secrets
from collections.abc import Sequence
from typing import Any

import mcp.types as mt
from fastmcp import FastMCP
from fastmcp.exceptions import AuthorizationError
from fastmcp.server.middleware import CallNext, MiddlewareContext
from fastmcp.server.middleware.middleware import Middleware as FastMcpMiddleware
from fastmcp.tools.base import Tool, ToolResult
from mcp.server.sse import SseServerTransport
from starlette.applications import Starlette
from starlette.datastructures import Headers
from starlette.middleware import Middleware
from starlette.requests import Request
from starlette.responses import JSONResponse, Response
from starlette.routing import Mount, Route
from starlette.types import ASGIApp, Receive, Scope, Send

from backend.application.auth_service import (
    API_KEY_SCOPE_FULL,
    API_KEY_SCOPE_MCP_ONLY,
    ResolvedAuth,
    auth_service,
)
from backend.application.exceptions import ApplicationError
from backend.application.mcp.config import McpServiceSpec, get_default_mcp_service_spec
from backend.application.mcp.query_service import (
    collect_mcp_visible_answer,
    invoke_mcp_tool,
)
from backend.application.mcp.text_retrieval import (
    NUCLEAR_ELECTRONICS_TEXT_TOOL,
    query_nuclear_electronics_text,
)
from backend.application.mcp.tool_catalog import (
    McpToolCatalogQuery,
    build_mcp_tool_catalog,
)
from backend.config.loggers import get_logger

logger = get_logger(__name__)


def _request_auth(context: MiddlewareContext[Any]) -> ResolvedAuth | None:
    fastmcp_context = context.fastmcp_context
    request_context = fastmcp_context.request_context if fastmcp_context else None
    request = request_context.request if request_context else None
    scope = getattr(request, "scope", None)
    state = scope.get("state", {}) if isinstance(scope, dict) else {}
    auth = state.get("auth") if isinstance(state, dict) else None
    return auth if isinstance(auth, ResolvedAuth) else None


class McpToolAllowlistMiddleware(FastMcpMiddleware):
    """Filter discovery and deny calls using the current API-key allowlist."""

    @staticmethod
    def _allowed_tools(context: MiddlewareContext[Any]) -> set[str] | None:
        auth = _request_auth(context)
        if auth is None:
            return set()
        if auth.mcp_allowed_tools is None:
            return None
        return set(auth.mcp_allowed_tools)

    async def on_list_tools(
        self,
        context: MiddlewareContext[mt.ListToolsRequest],
        call_next: CallNext[mt.ListToolsRequest, Sequence[Tool]],
    ) -> Sequence[Tool]:
        tools = await call_next(context)
        allowed = self._allowed_tools(context)
        if allowed is None:
            return tools
        return [tool for tool in tools if tool.name in allowed]

    async def on_call_tool(
        self,
        context: MiddlewareContext[mt.CallToolRequestParams],
        call_next: CallNext[mt.CallToolRequestParams, ToolResult],
    ) -> ToolResult:
        allowed = self._allowed_tools(context)
        tool_name = context.message.name
        if allowed is not None and tool_name not in allowed:
            raise AuthorizationError(f"API Key 无权调用 MCP 工具: {tool_name}")
        return await call_next(context)


def _extract_mcp_api_key(headers: Headers) -> str:
    x_api_key = (headers.get("X-API-Key") or "").strip()
    if x_api_key:
        return x_api_key

    authorization = (headers.get("Authorization") or "").strip()
    scheme, separator, credential = authorization.partition(" ")
    if separator and scheme.lower() == "bearer":
        return credential.strip()
    return ""


class McpApiKeyAuthMiddleware:
    """Require a valid API key for every MCP SSE transport request."""

    def __init__(self, app: ASGIApp) -> None:
        self.app = app

    async def __call__(self, scope: Scope, receive: Receive, send: Send) -> None:
        if scope["type"] != "http":
            await self.app(scope, receive, send)
            return

        api_key = _extract_mcp_api_key(Headers(scope=scope))
        if not api_key:
            await self._reject(
                scope,
                receive,
                send,
                status_code=401,
                detail="MCP 访问需要 API Key",
                code="missing_api_key",
            )
            return

        state = scope.setdefault("state", {})
        cached_auth = state.get("auth")
        if not (
            isinstance(cached_auth, ResolvedAuth)
            and cached_auth.source == "api_key"
            and secrets.compare_digest(cached_auth.api_key, api_key)
        ):
            try:
                cached_auth = await auth_service.resolve_api_key(api_key)
            except ApplicationError as exc:
                await self._reject(
                    scope,
                    receive,
                    send,
                    status_code=exc.status_code,
                    detail=exc.public_detail,
                    code=exc.error_code,
                )
                return

        if cached_auth is None:
            await self._reject(
                scope,
                receive,
                send,
                status_code=401,
                detail="无效、已禁用或已过期的 API Key",
                code="invalid_api_key",
            )
            return

        if str(cached_auth.access_scope or "").strip().lower() not in {
            API_KEY_SCOPE_FULL,
            API_KEY_SCOPE_MCP_ONLY,
        }:
            await self._reject(
                scope,
                receive,
                send,
                status_code=403,
                detail="此 API Key 的访问范围无效",
                code="api_key_scope_forbidden",
            )
            return

        state["auth"] = cached_auth
        if cached_auth.user_id:
            state["user_id"] = cached_auth.user_id
        await self.app(scope, receive, send)

    @staticmethod
    async def _reject(
        scope: Scope,
        receive: Receive,
        send: Send,
        *,
        status_code: int,
        detail: str,
        code: str,
    ) -> None:
        response = JSONResponse(
            status_code=status_code,
            content={"detail": detail, "code": code},
            headers={
                "Cache-Control": "no-store",
                "WWW-Authenticate": "Bearer",
            },
        )
        await response(scope, receive, send)


class _AlreadyHandledResponse(Response):
    """Response shim for endpoints that already wrote directly to ASGI send."""

    async def __call__(self, scope, receive, send) -> None:  # type: ignore[override]
        return None


def create_platform_mcp_app(spec: McpServiceSpec | None = None) -> FastMCP:
    spec = spec or get_default_mcp_service_spec()
    mcp_app = FastMCP(name=spec.name, instructions=spec.instructions)

    @mcp_app.tool(name=spec.tool_name, description=spec.tool_description)
    async def query_knowledge_base(
        query: str,
        knowledge_bases: str | list[str] | None = None,
        thread_id: str | None = None,
    ) -> str:
        """
        查询配置绑定智能体的知识库并返回回答。

        Args:
            query: 用户问题，支持中英文查询。
            knowledge_bases: 可选知识库名称或名称列表；不传或传空列表时
                查询当前智能体绑定的全部可访问知识库。
            thread_id: 可选 MCP 会话 ID，用于连续对话。

        Returns:
            智能体的可见回答内容。
        """
        return await collect_mcp_visible_answer(
            query,
            spec=spec,
            source="mcp.fastmcp_tool",
            knowledge_bases=knowledge_bases,
            thread_id=thread_id,
        )

    @mcp_app.tool(
        name="list_available_tools",
        description=("列出当前 MCP 服务绑定智能体的 policy tool 目录和治理注解"),
    )
    async def list_available_tools(
        workflow: str | None = None,
        selected_skills: list[str] | None = None,
    ) -> dict:
        """
        列出当前 MCP 服务可见的工具描述。

        Args:
            workflow: 可选工作流名称；为空时使用 MCP 服务配置的
                workflow。
            selected_skills: 可选 Skill 名称列表，用于展开按 Skill
                启用的工具。

        Returns:
            MCP-like 工具目录，包含工具名、描述、输入 schema
            和治理注解。
        """
        catalog = build_mcp_tool_catalog(
            McpToolCatalogQuery(
                agent_code=spec.agent_code,
                workflow=workflow or spec.workflow or "full",
                selected_skills=set(selected_skills or []),
            )
        )
        return catalog.to_dict()

    @mcp_app.tool(
        name="call_tool",
        description="调用当前 MCP 服务目录中已授权的单个工具",
    )
    async def call_tool(
        tool_name: str,
        arguments: dict | None = None,
        thread_id: str | None = None,
    ) -> dict:
        value = await invoke_mcp_tool(
            tool_name,
            arguments or {},
            user_id=spec.user_id,
            role="user",
            spec=spec,
            thread_id=thread_id,
        )
        return {"tool_name": tool_name, "result": value}

    @mcp_app.tool(
        name=NUCLEAR_ELECTRONICS_TEXT_TOOL,
        description=(
            "仅检索核电子学期刊知识库并返回原始文字片段；"
            "不生成答案，不返回图片、图片链接或图片内容"
        ),
    )
    async def query_nuclear_electronics_text_tool(
        query: str,
        limit: int = 10,
    ) -> dict:
        return await query_nuclear_electronics_text(query, limit=limit)

    return mcp_app


def create_mcp_sse_app(
    spec: McpServiceSpec | None = None,
    *,
    debug: bool = False,
) -> Starlette:
    platform_mcp_app = create_platform_mcp_app(spec)
    platform_mcp_app.add_middleware(McpToolAllowlistMiddleware())
    mcp_server = platform_mcp_app._mcp_server
    sse = SseServerTransport("/messages/")

    async def handle_sse(request: Request) -> Response:
        response_started = False

        async def send(message):
            nonlocal response_started
            if message.get("type") == "http.response.start":
                response_started = True
            await request._send(message)

        try:
            async with sse.connect_sse(
                request.scope,
                request.receive,
                send,
            ) as (read_stream, write_stream):
                await mcp_server.run(
                    read_stream,
                    write_stream,
                    mcp_server.create_initialization_options(),
                )
        except Exception as exc:
            logger.error("MCP SSE连接错误: %s", exc)
            if response_started:
                return _AlreadyHandledResponse()
            return Response("Internal server error", status_code=500)
        return _AlreadyHandledResponse()

    return Starlette(
        debug=debug,
        middleware=[Middleware(McpApiKeyAuthMiddleware)],
        routes=[
            Route("/", endpoint=handle_sse),
            Route("/sse", endpoint=handle_sse),
            Mount("/messages/", app=sse.handle_post_message),
        ],
    )
