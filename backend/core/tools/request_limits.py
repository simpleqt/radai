"""Request-scoped tool call limits and per-turn retrieval budgets."""

from __future__ import annotations

import asyncio
import os
from contextvars import ContextVar
from typing import Optional

_TOOL_BUDGETS: ContextVar[dict[str, int] | None] = ContextVar(
    "tool_budgets",
    default=None,
)

_TURN_KB_CHARS_USED: ContextVar[int] = ContextVar("turn_kb_chars_used", default=0)

_MULTI_KB_REQUEST_LOCK: ContextVar[asyncio.Lock | None] = ContextVar(
    "multi_kb_request_lock",
    default=None,
)

TURN_MAX_EVIDENCE_CHARS = max(
    1000, int(os.getenv("MULTI_KB_SEARCH_TURN_MAX_EVIDENCE_CHARS", "8000"))
)


def set_tool_budgets(budgets: dict[str, int]):
    return _TOOL_BUDGETS.set(dict(budgets))


def reset_tool_budgets(token) -> None:
    _TOOL_BUDGETS.reset(token)


def set_tool_budget(tool_name: str, remaining: int) -> None:
    budgets = _TOOL_BUDGETS.get()
    if budgets is None:
        return
    budgets[tool_name] = max(0, int(remaining))


def consume_tool_budget(tool_name: str) -> tuple[bool, Optional[int]]:
    budgets = _TOOL_BUDGETS.get()
    if budgets is None:
        return True, None

    remaining = budgets.get(tool_name)
    if remaining is None:
        return True, None

    if remaining <= 0:
        return False, 0

    budgets[tool_name] = remaining - 1
    return True, budgets[tool_name]


def reset_turn_kb_context_budget() -> None:
    _TURN_KB_CHARS_USED.set(0)
    _MULTI_KB_REQUEST_LOCK.set(asyncio.Lock())


def get_turn_kb_chars_remaining(turn_max: int | None = None) -> int:
    limit = turn_max if turn_max is not None else TURN_MAX_EVIDENCE_CHARS
    return max(0, limit - _TURN_KB_CHARS_USED.get())


def record_turn_kb_chars(char_count: int) -> int:
    used = _TURN_KB_CHARS_USED.get() + max(0, int(char_count))
    _TURN_KB_CHARS_USED.set(used)
    return used


def get_multi_kb_request_lock() -> asyncio.Lock:
    lock = _MULTI_KB_REQUEST_LOCK.get()
    if lock is None:
        lock = asyncio.Lock()
        _MULTI_KB_REQUEST_LOCK.set(lock)
    return lock
