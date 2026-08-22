"""Request-scoped tool call limits and per-turn retrieval budgets."""

from __future__ import annotations

import asyncio
import json
import os
import re
import threading
from collections import deque
from contextvars import ContextVar
from dataclasses import dataclass, field
from typing import Any, Optional

_TOOL_BUDGETS: ContextVar[dict[str, int] | None] = ContextVar(
    "tool_budgets",
    default=None,
)

_WEB_SEARCH_MAX_RESULTS: ContextVar[int | None] = ContextVar(
    "web_search_max_results",
    default=None,
)

_WEB_SEARCH_NO_PROGRESS_STREAK: ContextVar[int] = ContextVar(
    "web_search_no_progress_streak",
    default=0,
)


@dataclass
class RequestToolBudget:
    """Shared tool allowance for every executor created within one request.

    Executor-local limits prevent a single agent from looping, while this
    object prevents a coordinated workflow from multiplying that allowance by
    its number of branches.  The object is intentionally mutable: copied
    ContextVars in worker tasks must still consume the same request budget.
    """

    remaining: dict[str, int]
    web_search_no_progress_streak: int = 0


_REQUEST_TOOL_BUDGET: ContextVar[RequestToolBudget | None] = ContextVar(
    "request_tool_budget",
    default=None,
)


@dataclass
class SubagentExecutionBudget:
    """Request-scoped queue for delegated subagent executions.

    ``task`` tool calls may be emitted in one model response.  Keeping the
    semaphore in a ContextVar lets LangGraph-created tasks inherit *one*
    request-local queue instead of silently dropping excess calls.
    """

    max_concurrent: int
    semaphore: Any


_SUBAGENT_EXECUTION_BUDGET: ContextVar[SubagentExecutionBudget | None] = ContextVar(
    "subagent_execution_budget",
    default=None,
)


@dataclass
class TurnKbContextBudget:
    used_chars: int = 0
    max_chars: int | None = None
    used_chars_by_scope: dict[str, int] = field(default_factory=dict)


@dataclass(frozen=True)
class TurnKbContextScope:
    key: str
    max_chars: int


@dataclass(eq=False)
class _CapacityWaiter:
    loop: asyncio.AbstractEventLoop
    future: asyncio.Future[None]
    granted: bool = False


class _CrossLoopCapacityLimiter:
    """FIFO async limiter whose capacity can be shared by multiple event loops."""

    def __init__(self, capacity: int) -> None:
        self._capacity = max(1, int(capacity))
        self._available = self._capacity
        self._lock = threading.Lock()
        self._waiters: deque[_CapacityWaiter] = deque()

    async def acquire(self) -> None:
        loop = asyncio.get_running_loop()
        waiter = _CapacityWaiter(loop=loop, future=loop.create_future())
        with self._lock:
            if self._available > 0:
                self._available -= 1
                return
            self._waiters.append(waiter)

        try:
            await waiter.future
        except BaseException:
            release_grant = False
            with self._lock:
                if waiter.granted:
                    release_grant = True
                else:
                    try:
                        self._waiters.remove(waiter)
                    except ValueError:
                        pass
            if release_grant:
                self.release()
            raise

    def release(self) -> None:
        while True:
            waiter: _CapacityWaiter | None = None
            with self._lock:
                while self._waiters:
                    candidate = self._waiters.popleft()
                    if candidate.future.done() or candidate.loop.is_closed():
                        continue
                    candidate.granted = True
                    waiter = candidate
                    break
                if waiter is None:
                    if self._available >= self._capacity:
                        raise ValueError("capacity limiter released too many times")
                    self._available += 1
                    return
            try:
                waiter.loop.call_soon_threadsafe(self._complete_waiter, waiter)
                return
            except RuntimeError:
                with self._lock:
                    waiter.granted = False

    @staticmethod
    def _complete_waiter(waiter: _CapacityWaiter) -> None:
        if not waiter.future.done():
            waiter.future.set_result(None)


@dataclass
class RetrievalRequestCache:
    completed: dict[str, Any] = field(default_factory=dict)
    in_flight: dict[str, tuple[int, Any]] = field(default_factory=dict)
    lock: threading.RLock = field(default_factory=threading.RLock)
    provider_cancelled: threading.Event = field(default_factory=threading.Event)

    def get_exact(self, key: str) -> Any | None:
        with self.lock:
            return self.completed.get(key)

    def set_exact(self, key: str, value: Any) -> None:
        with self.lock:
            self.completed[key] = value

    def get_similar(self, key: str, *, threshold: float = 0.72) -> Any | None:
        query, parameters = _cache_key_parts(key)
        key_terms = _cache_terms(query)
        if not key_terms:
            return None
        with self.lock:
            completed = list(self.completed.items())
        best_value = None
        best_overlap = threshold
        for existing_key, value in completed:
            existing_query, existing_parameters = _cache_key_parts(existing_key)
            if existing_parameters != parameters:
                continue
            if not _queries_have_compatible_polarity(query, existing_query):
                continue
            existing_terms = _cache_terms(existing_query)
            if not existing_terms:
                continue
            overlap = len(key_terms & existing_terms) / max(
                1, len(key_terms | existing_terms)
            )
            if overlap >= best_overlap:
                best_overlap = overlap
                best_value = value
        return best_value

    def get_in_flight(self, key: str, *, loop_id: int) -> Any | None:
        with self.lock:
            existing = self.in_flight.get(key)
            if existing is None:
                return None
            existing_loop_id, task = existing
            if existing_loop_id != loop_id:
                return None
            return task

    def set_in_flight(self, key: str, *, loop_id: int, task: Any) -> None:
        with self.lock:
            self.in_flight[key] = (loop_id, task)

    def clear_in_flight(self, key: str, task: Any) -> None:
        with self.lock:
            current = self.in_flight.get(key)
            if current is not None and current[1] is task:
                self.in_flight.pop(key, None)

    async def acquire_provider_slot(self) -> None:
        await _KB_PROVIDER_LIMITER.acquire()

    def release_provider_slot(self) -> None:
        _KB_PROVIDER_LIMITER.release()

    def cancel_provider_requests(self) -> None:
        self.provider_cancelled.set()

    def provider_requests_cancelled(self) -> bool:
        return self.provider_cancelled.is_set()


_TURN_KB_CONTEXT_BUDGET: ContextVar[TurnKbContextBudget | None] = ContextVar(
    "turn_kb_context_budget",
    default=None,
)
_TURN_KB_CONTEXT_SCOPE: ContextVar[TurnKbContextScope | None] = ContextVar(
    "turn_kb_context_scope",
    default=None,
)

_RETRIEVAL_REQUEST_CACHE: ContextVar[RetrievalRequestCache | None] = ContextVar(
    "retrieval_request_cache",
    default=None,
)

TURN_MAX_EVIDENCE_CHARS = max(
    1000, int(os.getenv("MULTI_KB_SEARCH_TURN_MAX_EVIDENCE_CHARS", "14000"))
)
_REQUEST_LIMITS_LOCK = threading.RLock()
try:
    _KB_PROVIDER_CAPACITY = max(
        1,
        int(os.getenv("MULTI_KB_PROVIDER_CAPACITY", "8") or "8"),
    )
except (TypeError, ValueError):
    _KB_PROVIDER_CAPACITY = 8
_KB_PROVIDER_LIMITER = _CrossLoopCapacityLimiter(_KB_PROVIDER_CAPACITY)


def set_tool_budgets(budgets: dict[str, int]):
    return _TOOL_BUDGETS.set(dict(budgets))


def set_web_search_max_results(max_results: int | None):
    """Set an executor-local ceiling for pages returned by one web search."""

    resolved = None if max_results is None else max(1, int(max_results))
    return _WEB_SEARCH_MAX_RESULTS.set(resolved)


def get_web_search_max_results() -> int | None:
    return _WEB_SEARCH_MAX_RESULTS.get()


def reset_web_search_max_results(token) -> None:
    if getattr(token, "var", _WEB_SEARCH_MAX_RESULTS) is not _WEB_SEARCH_MAX_RESULTS:
        raise ValueError(
            "web search result limit token belongs to a different ContextVar"
        )
    try:
        _WEB_SEARCH_MAX_RESULTS.reset(token)
    except ValueError as exc:
        if "different Context" not in str(exc):
            raise
        _WEB_SEARCH_MAX_RESULTS.set(None)


def get_web_search_no_progress_streak() -> int:
    """Return consecutive web searches that produced no relevant results."""

    request_budget = _REQUEST_TOOL_BUDGET.get()
    if request_budget is not None:
        return max(0, int(request_budget.web_search_no_progress_streak or 0))
    return max(0, int(_WEB_SEARCH_NO_PROGRESS_STREAK.get() or 0))


def increment_web_search_no_progress_streak() -> int:
    """Count one more consecutive low-quality web search and return the total."""

    request_budget = _REQUEST_TOOL_BUDGET.get()
    if request_budget is not None:
        request_budget.web_search_no_progress_streak += 1
        streak = request_budget.web_search_no_progress_streak
    else:
        streak = get_web_search_no_progress_streak() + 1
        _WEB_SEARCH_NO_PROGRESS_STREAK.set(streak)
    return streak


def reset_web_search_no_progress_streak() -> None:
    """Reset the low-quality streak (a relevant result or a new request)."""

    request_budget = _REQUEST_TOOL_BUDGET.get()
    if request_budget is not None:
        request_budget.web_search_no_progress_streak = 0
    _WEB_SEARCH_NO_PROGRESS_STREAK.set(0)


def exhaust_tool_budget(tool_name: str) -> None:
    """Force a tool's effective remaining budget to zero in every scope."""

    with _REQUEST_LIMITS_LOCK:
        budgets = _TOOL_BUDGETS.get()
        if budgets is not None:
            budgets[tool_name] = 0
        request_budget = _REQUEST_TOOL_BUDGET.get()
        if request_budget is not None:
            request_budget.remaining[tool_name] = 0


def set_request_tool_budget(budgets: dict[str, int]):
    return _REQUEST_TOOL_BUDGET.set(
        RequestToolBudget(
            remaining={name: max(0, int(value)) for name, value in budgets.items()}
        )
    )


def reset_request_tool_budget(token) -> None:
    if getattr(token, "var", _REQUEST_TOOL_BUDGET) is not _REQUEST_TOOL_BUDGET:
        raise ValueError("request tool budget token belongs to a different ContextVar")
    try:
        _REQUEST_TOOL_BUDGET.reset(token)
    except ValueError as exc:
        if "different Context" not in str(exc):
            raise
        _REQUEST_TOOL_BUDGET.set(None)


def reset_tool_budgets(token) -> None:
    if getattr(token, "var", _TOOL_BUDGETS) is not _TOOL_BUDGETS:
        raise ValueError("tool budget token belongs to a different ContextVar")
    try:
        _TOOL_BUDGETS.reset(token)
    except ValueError as exc:
        if "different Context" not in str(exc):
            raise
        _TOOL_BUDGETS.set(None)


def set_subagent_execution_limit(max_concurrent: int):
    import asyncio

    limit = max(1, min(5, int(max_concurrent)))
    return _SUBAGENT_EXECUTION_BUDGET.set(
        SubagentExecutionBudget(
            max_concurrent=limit,
            semaphore=asyncio.Semaphore(limit),
        )
    )


def reset_subagent_execution_limit(token) -> None:
    try:
        _SUBAGENT_EXECUTION_BUDGET.reset(token)
    except ValueError as exc:
        if "different Context" not in str(exc):
            raise
        _SUBAGENT_EXECUTION_BUDGET.set(None)


def get_subagent_execution_budget() -> SubagentExecutionBudget | None:
    return _SUBAGENT_EXECUTION_BUDGET.get()


def consume_tool_budget(tool_name: str) -> tuple[bool, Optional[int]]:
    budgets = _TOOL_BUDGETS.get()
    request_budget = _REQUEST_TOOL_BUDGET.get()
    with _REQUEST_LIMITS_LOCK:
        local_remaining = budgets.get(tool_name) if budgets is not None else None
        request_remaining = (
            request_budget.remaining.get(tool_name)
            if request_budget is not None
            else None
        )
        if local_remaining is not None and local_remaining <= 0:
            return False, 0
        if request_remaining is not None and request_remaining <= 0:
            return False, 0
        if local_remaining is not None:
            budgets[tool_name] = local_remaining - 1
        if request_remaining is not None and request_budget is not None:
            request_budget.remaining[tool_name] = request_remaining - 1
        remaining_values = [
            value
            for value in (
                budgets.get(tool_name) if budgets is not None else None,
                (
                    request_budget.remaining.get(tool_name)
                    if request_budget is not None
                    else None
                ),
            )
            if value is not None
        ]
        return True, min(remaining_values) if remaining_values else None


def get_tool_budget_remaining(tool_name: str) -> Optional[int]:
    """Return the effective request-local allowance without consuming it."""

    budgets = _TOOL_BUDGETS.get()
    request_budget = _REQUEST_TOOL_BUDGET.get()
    with _REQUEST_LIMITS_LOCK:
        remaining_values = [
            value
            for value in (
                budgets.get(tool_name) if budgets is not None else None,
                (
                    request_budget.remaining.get(tool_name)
                    if request_budget is not None
                    else None
                ),
            )
            if value is not None
        ]
    return min(remaining_values) if remaining_values else None


def reset_turn_kb_context_budget(max_chars: int | None = None) -> None:
    """Start a fresh request budget, optionally overriding the global ceiling."""

    resolved_max = None if max_chars is None else max(1, int(max_chars))
    _TURN_KB_CONTEXT_BUDGET.set(TurnKbContextBudget(max_chars=resolved_max))
    _TURN_KB_CONTEXT_SCOPE.set(None)
    _RETRIEVAL_REQUEST_CACHE.set(RetrievalRequestCache())


def set_turn_kb_context_scope(scope_key: str, *, max_chars: int):
    """Apply a per-branch KB injection ceiling inside the shared request cap."""

    return _TURN_KB_CONTEXT_SCOPE.set(
        TurnKbContextScope(
            key=str(scope_key or "default"),
            max_chars=max(1, int(max_chars)),
        )
    )


def reset_turn_kb_context_scope(token) -> None:
    _TURN_KB_CONTEXT_SCOPE.reset(token)


def _get_turn_kb_context_budget() -> TurnKbContextBudget:
    budget = _TURN_KB_CONTEXT_BUDGET.get()
    if budget is None:
        budget = TurnKbContextBudget()
        _TURN_KB_CONTEXT_BUDGET.set(budget)
    return budget


def get_turn_kb_chars_remaining(turn_max: int | None = None) -> int:
    budget = _get_turn_kb_context_budget()
    limit = budget.max_chars or (
        turn_max if turn_max is not None else TURN_MAX_EVIDENCE_CHARS
    )
    with _REQUEST_LIMITS_LOCK:
        request_remaining = max(0, limit - budget.used_chars)
        scope = _TURN_KB_CONTEXT_SCOPE.get()
        if scope is None:
            return request_remaining
        scope_remaining = max(
            0,
            scope.max_chars - budget.used_chars_by_scope.get(scope.key, 0),
        )
        return min(request_remaining, scope_remaining)


def reserve_turn_kb_chars(
    char_count: int,
    *,
    turn_max: int | None = None,
    minimum_chars: int = 0,
) -> int:
    """Atomically reserve a bounded slice of the per-turn KB context budget.

    Retrieval branches run concurrently and inherit the same mutable budget.
    Keeping the remaining-budget check and the increment under one lock avoids
    every branch observing the same allowance and overcommitting it.
    """

    requested = max(0, int(char_count))
    if requested <= 0:
        return 0
    minimum = min(requested, max(0, int(minimum_chars)))
    budget = _get_turn_kb_context_budget()
    limit = budget.max_chars or (
        turn_max if turn_max is not None else TURN_MAX_EVIDENCE_CHARS
    )
    with _REQUEST_LIMITS_LOCK:
        remaining = max(0, limit - budget.used_chars)
        scope = _TURN_KB_CONTEXT_SCOPE.get()
        if scope is not None:
            remaining = min(
                remaining,
                max(
                    0,
                    scope.max_chars - budget.used_chars_by_scope.get(scope.key, 0),
                ),
            )
        if remaining < minimum:
            return 0
        reserved = min(requested, remaining)
        budget.used_chars += reserved
        if scope is not None:
            budget.used_chars_by_scope[scope.key] = (
                budget.used_chars_by_scope.get(scope.key, 0) + reserved
            )
        return reserved


def record_turn_kb_chars(char_count: int) -> int:
    budget = _get_turn_kb_context_budget()
    with _REQUEST_LIMITS_LOCK:
        budget.used_chars += max(0, int(char_count))
        return budget.used_chars


def get_retrieval_request_cache() -> RetrievalRequestCache:
    cache = _RETRIEVAL_REQUEST_CACHE.get()
    if cache is None:
        cache = RetrievalRequestCache()
        _RETRIEVAL_REQUEST_CACHE.set(cache)
    return cache


def _cache_terms(key: str) -> set[str]:
    text = str(key or "").lower()
    terms = {term for term in re.findall(r"[a-z0-9_]{2,}", text)}
    for cjk_run in re.findall(r"[\u4e00-\u9fff]+", text):
        if len(cjk_run) == 1:
            terms.add(cjk_run)
        else:
            terms.update(
                cjk_run[index : index + 2] for index in range(len(cjk_run) - 1)
            )
    return terms


_NEGATION_RE = re.compile(
    r"\b(?:not|no|never|without|cannot|can't|won't|exclude|except)\b|"
    r"(?:不会|不能|不应|不得|不需要|未曾|尚未|没有|无法|无需|禁止|排除|避免)",
    re.IGNORECASE,
)


def _queries_have_compatible_polarity(left: str, right: str) -> bool:
    """Do not reuse evidence between affirmative and explicitly negated queries."""

    return bool(_NEGATION_RE.search(str(left or ""))) == bool(
        _NEGATION_RE.search(str(right or ""))
    )


def _cache_key_parts(key: str) -> tuple[str, str]:
    """Return query and an exact-match parameter signature."""

    raw = str(key or "")
    try:
        payload = json.loads(raw)
    except (TypeError, json.JSONDecodeError):
        query, separator, parameters = raw.partition("|")
        return query, parameters if separator else ""
    if not isinstance(payload, dict):
        return raw, ""
    query = str(payload.get("query") or "")
    parameters = json.dumps(
        payload.get("parameters") or {},
        ensure_ascii=False,
        sort_keys=True,
        separators=(",", ":"),
    )
    return query, parameters
