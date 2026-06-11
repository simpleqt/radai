"""Standalone MCP server entrypoint."""

from __future__ import annotations

import uvicorn

from backend.application.mcp import create_mcp_sse_app


def run_server(host: str = "0.0.0.0", port: int | None = None) -> None:
    import os
    port = port or int(os.getenv("MCP_SERVER_PORT", "8083"))
    uvicorn.run(create_mcp_sse_app(debug=True), host=host, port=port)


if __name__ == "__main__":
    run_server()
