"""Application-level MCP service assembly."""

from backend.application.mcp.config import McpServiceSpec, McpServiceRegistry, get_default_mcp_service_spec
from backend.application.mcp.fastmcp_app import create_mcp_sse_app, create_platform_mcp_app

__all__ = [
    "McpServiceSpec",
    "McpServiceRegistry",
    "create_mcp_sse_app",
    "create_platform_mcp_app",
    "get_default_mcp_service_spec",
]
