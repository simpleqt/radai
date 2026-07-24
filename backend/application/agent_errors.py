"""Stable public error messages for agent transports."""

from __future__ import annotations

import asyncio


def public_agent_error(exc: BaseException) -> tuple[str, str]:
    """Return a client-safe message and a stable machine-readable code."""
    if isinstance(exc, (asyncio.TimeoutError, TimeoutError)):
        return "智能体执行超时，请稍后重试", "agent_timeout"
    if isinstance(exc, PermissionError):
        return "当前用户无权执行该操作", "agent_permission_denied"
    return "智能体执行失败，请稍后重试", "agent_execution_failed"
