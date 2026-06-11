import logging
from typing import Any
from fastapi import Request
from langgraph.checkpoint.sqlite.aio import AsyncSqliteSaver

logger = logging.getLogger(__name__)

def get_checkpointer_from_request(request: Request) -> AsyncSqliteSaver:
    """从 FastAPI Request 中获取全局共享的 checkpointer"""
    if not hasattr(request.app.state, "checkpointer"):
        logger.error("Checkpointer not found in app.state")
        raise RuntimeError("Checkpointer not initialized")
    return request.app.state.checkpointer
