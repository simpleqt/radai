"""FastMCP application assembly from platform MCP config."""

from __future__ import annotations

from fastmcp import FastMCP
from mcp.server.sse import SseServerTransport
from starlette.applications import Starlette
from starlette.requests import Request
from starlette.responses import Response
from starlette.routing import Mount, Route

from backend.application.mcp.config import McpServiceSpec, get_default_mcp_service_spec
from backend.application.mcp.query_service import collect_mcp_visible_answer
from backend.config.loggers import get_logger

logger = get_logger(__name__)


def create_platform_mcp_app(spec: McpServiceSpec | None = None) -> FastMCP:
    spec = spec or get_default_mcp_service_spec()
    mcp_app = FastMCP(name=spec.name, instructions=spec.instructions)

    @mcp_app.tool(name=spec.tool_name, description=spec.tool_description)
    async def query_knowledge_base(query: str) -> str:
        """
        查询配置绑定智能体的知识库并返回回答。

        Args:
            query: 用户问题，支持中英文查询。

        Returns:
            智能体的可见回答内容。
        """
        return await collect_mcp_visible_answer(
            query,
            spec=spec,
            source="mcp.fastmcp_tool",
        )

    return mcp_app


def create_mcp_sse_app(spec: McpServiceSpec | None = None, *, debug: bool = True) -> Starlette:
    mcp_server = create_platform_mcp_app(spec)._mcp_server
    sse = SseServerTransport("/messages/")

    async def handle_sse(request: Request) -> Response:
        try:
            async with sse.connect_sse(
                request.scope,
                request.receive,
                request._send,
            ) as (read_stream, write_stream):
                await mcp_server.run(
                    read_stream,
                    write_stream,
                    mcp_server.create_initialization_options(),
                )
        except Exception as exc:
            logger.error("MCP SSE连接错误: %s", exc)
            return Response(f"Error: {str(exc)}", status_code=500)
        return Response("Connection established", status_code=200)

    return Starlette(
        debug=debug,
        routes=[
            Route("/", endpoint=handle_sse),
            Route("/sse", endpoint=handle_sse),
            Mount("/messages/", app=sse.handle_post_message),
        ],
    )
