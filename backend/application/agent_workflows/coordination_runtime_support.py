"""协调工作流运行时支撑:超时/并发常量、任务取消、资源租约与自适应门(拆自 coordinated_agent_workflow.py)。"""

from __future__ import annotations

import logging

import asyncio
import json
import os

from backend.application.agent_workflows.coordination_types import (
    CoordinationUnitResult,
)
from backend.core.agents.resource_lease import (
    InMemoryResourceLeaseManager,
    Lease,
    RedisResourceLeaseManager,
    ResourceLeaseManager,
)
from backend.domain.agents.runtime_budget import AgentRuntimeBudget
from contextlib import suppress
from dataclasses import replace
from typing import Any
from weakref import WeakKeyDictionary


logger = logging.getLogger(__name__)


# Static defaults are retained as compatibility aliases for older tests and
# internal imports.  Runtime configuration is deliberately *not* read here:
# each workflow instance captures its own immutable budget below.
_DEFAULT_RUNTIME_BUDGET = AgentRuntimeBudget()


_MAX_CONCURRENT_RESEARCH = _DEFAULT_RUNTIME_BUDGET.max_concurrent_research


_MAX_CONCURRENT_PER_USER_MODEL = _DEFAULT_RUNTIME_BUDGET.max_concurrent_per_user_model


_PLAN_SOLVE_MAX_CONCURRENT = _DEFAULT_RUNTIME_BUDGET.plan_solve_max_concurrent_subagents


_EVENT_QUEUE_MAXSIZE = _DEFAULT_RUNTIME_BUDGET.event_queue_maxsize


_UNIT_HEARTBEAT_SECONDS = _DEFAULT_RUNTIME_BUDGET.unit_heartbeat_seconds


_UNIT_IDLE_TIMEOUT_SECONDS = _DEFAULT_RUNTIME_BUDGET.unit_idle_timeout_seconds


_UNIT_TOTAL_TIMEOUT_SECONDS = _DEFAULT_RUNTIME_BUDGET.unit_total_timeout_seconds


_KB_SEARCH_TIMEOUT_SECONDS = _DEFAULT_RUNTIME_BUDGET.kb_search_timeout_seconds


_PARTIAL_HANDOFF_GRACE_SECONDS = _DEFAULT_RUNTIME_BUDGET.partial_handoff_grace_seconds


_PARTIAL_WRITER_MIN_BRANCHES = _DEFAULT_RUNTIME_BUDGET.partial_writer_min_branches


_PARTIAL_FINALIZATION_WAIT_SECONDS = (
    _DEFAULT_RUNTIME_BUDGET.partial_finalization_wait_seconds
)


_WEB_SEARCH_TIMEOUT_SECONDS = _DEFAULT_RUNTIME_BUDGET.web_search_timeout_seconds


_WORKFLOW_TOTAL_TIMEOUT_SECONDS = _DEFAULT_RUNTIME_BUDGET.workflow_total_timeout_seconds


_DEEP_RESEARCH_RESEARCH_PHASE_TIMEOUT_SECONDS = (
    _DEFAULT_RUNTIME_BUDGET.deep_research_research_phase_timeout_seconds
)


_DEEP_RESEARCH_WRITER_TIMEOUT_SECONDS = (
    _DEFAULT_RUNTIME_BUDGET.deep_research_writer_timeout_seconds
)


_DEEP_RESEARCH_REPORT_TIMEOUT_SECONDS = (
    _DEFAULT_RUNTIME_BUDGET.deep_research_report_timeout_seconds
)


_DEEP_RESEARCH_BRANCH_WEB_CALLS = 3


_DEEP_RESEARCH_WEB_MAX_RESULTS = 3


_RESEARCH_PHASE_TIMEOUT_ERROR = (
    "研究阶段预算已耗尽；已停止继续检索，并保留已完成证据供最终写作。"
)


_FAST_MODEL_RESEARCH_ROLES = frozenset(
    {"domain_analyst", "evidence_researcher", "risk_reviewer"}
)


_HIGH_RISK_LEVELS = frozenset({"high", "critical", "severe", "高", "高风险"})


_LEGACY_RUNTIME_BUDGET_ALIASES = {
    "max_concurrent_research": "_MAX_CONCURRENT_RESEARCH",
    "max_concurrent_per_user_model": "_MAX_CONCURRENT_PER_USER_MODEL",
    "plan_solve_max_concurrent_subagents": "_PLAN_SOLVE_MAX_CONCURRENT",
    "event_queue_maxsize": "_EVENT_QUEUE_MAXSIZE",
    "unit_heartbeat_seconds": "_UNIT_HEARTBEAT_SECONDS",
    "unit_idle_timeout_seconds": "_UNIT_IDLE_TIMEOUT_SECONDS",
    "unit_total_timeout_seconds": "_UNIT_TOTAL_TIMEOUT_SECONDS",
    "kb_search_timeout_seconds": "_KB_SEARCH_TIMEOUT_SECONDS",
    "partial_handoff_grace_seconds": "_PARTIAL_HANDOFF_GRACE_SECONDS",
    "partial_writer_min_branches": "_PARTIAL_WRITER_MIN_BRANCHES",
    "partial_finalization_wait_seconds": "_PARTIAL_FINALIZATION_WAIT_SECONDS",
    "web_search_timeout_seconds": "_WEB_SEARCH_TIMEOUT_SECONDS",
    "workflow_total_timeout_seconds": "_WORKFLOW_TOTAL_TIMEOUT_SECONDS",
    "deep_research_research_phase_timeout_seconds": "_DEEP_RESEARCH_RESEARCH_PHASE_TIMEOUT_SECONDS",
    "deep_research_writer_timeout_seconds": "_DEEP_RESEARCH_WRITER_TIMEOUT_SECONDS",
    "deep_research_report_timeout_seconds": "_DEEP_RESEARCH_REPORT_TIMEOUT_SECONDS",
}


_RESOURCE_LEASE_MANAGERS: WeakKeyDictionary[
    asyncio.AbstractEventLoop, ResourceLeaseManager
] = WeakKeyDictionary()


# Compatibility alias for tests and callers that only clear the old cache.
_RESEARCH_LIMITERS = _RESOURCE_LEASE_MANAGERS


_MANIFEST_RESEARCH_ROLES: tuple[str, ...] = (
    "domain_analyst",
    "data_analyst",
    "evidence_researcher",
    "risk_reviewer",
    "solution_architect",
)


def _join_unique_errors(*values: str | None) -> str | None:
    """Join user-facing errors without repeating the same contract failure."""

    unique: list[str] = []
    for value in values:
        text = str(value or "").strip()
        if text and text not in unique:
            unique.append(text)
    return "; ".join(unique) or None


def _tool_result_error(payload: dict[str, Any]) -> str | None:
    error = payload.get("error")
    if not error and str(payload.get("status") or "").lower() != "error":
        return None
    if isinstance(error, dict):
        return str(error.get("message") or error.get("code") or "工具调用失败")
    return str(error or payload.get("summary") or "工具调用失败")


def _compact_web_repair_evidence(
    payload: dict[str, Any],
    raw_result: Any,
    *,
    max_chars: int = 14000,
) -> str:
    """Keep URLs and bounded page excerpts for the no-tool repair pass."""

    results = payload.get("results")
    if isinstance(results, list):
        compact_results: list[dict[str, str]] = []
        for item in results[:_DEEP_RESEARCH_WEB_MAX_RESULTS]:
            if not isinstance(item, dict):
                continue
            compact_results.append(
                {
                    "title": str(item.get("title") or "")[:500],
                    "url": str(
                        item.get("url") or item.get("href") or item.get("link") or ""
                    )[:2000],
                    "content": str(
                        item.get("content")
                        or item.get("markdown")
                        or item.get("description")
                        or ""
                    )[:3000],
                }
            )
        text = json.dumps(
            {
                "query": payload.get("query"),
                "results": compact_results,
            },
            ensure_ascii=False,
            indent=2,
        )
    elif isinstance(raw_result, str):
        text = raw_result
    else:
        text = json.dumps(raw_result, ensure_ascii=False, default=str)
    return text[:max_chars]


class _ManagedCoordinationUnitFailure(RuntimeError):
    """Carry an encoded unit failure through the manager state machine."""

    def __init__(self, result: CoordinationUnitResult):
        super().__init__(result.error or "coordination unit failed")
        self.result = result


class _ManagedCoordinationUnitTimeout(
    _ManagedCoordinationUnitFailure,
    TimeoutError,
):
    """Preserve a partial unit result while marking its runtime as timed out."""


def _capture_runtime_budget(
    runtime_budget: AgentRuntimeBudget | None,
) -> AgentRuntimeBudget:
    """Capture one workflow-scoped budget without import-time environment reads."""

    snapshot = AgentRuntimeBudget.snapshot(runtime_budget)
    if runtime_budget is not None:
        return snapshot

    # Preserve compatibility with older callers that monkeypatch the module
    # constants.  Only values changed away from built-in defaults override the
    # newly captured environment snapshot.
    legacy_overrides = {
        field_name: globals()[alias]
        for field_name, alias in _LEGACY_RUNTIME_BUDGET_ALIASES.items()
        if globals()[alias] != getattr(_DEFAULT_RUNTIME_BUDGET, field_name)
    }
    if legacy_overrides:
        snapshot = replace(snapshot, **legacy_overrides)
    return snapshot


def _cancel_child_on_parent_exit(child: asyncio.Task[Any]) -> None:
    """Ensure independently-created child tasks cannot outlive their coroutine."""
    parent = asyncio.current_task()
    if parent is None:
        return

    def cancel_child(_completed_parent: asyncio.Task[Any]) -> None:
        if not child.done():
            child.cancel()

    parent.add_done_callback(cancel_child)


def _consume_background_task_result(task: asyncio.Task[Any]) -> None:
    try:
        task.result()
    except (asyncio.CancelledError, StopAsyncIteration):
        pass
    except Exception:
        logger.warning(
            "[CoordinatedAgentWorkflow] background task failed",
            exc_info=True,
        )


async def _cancel_task_bounded(
    task: asyncio.Task[Any] | None,
    *,
    timeout: float = 1.0,
) -> bool:
    """Signal cancellation without allowing cleanup to wait forever."""

    if task is None:
        return True
    if not task.done():
        task.cancel()
        done, _ = await asyncio.wait({task}, timeout=max(0.0, timeout))
        if not done:
            logger.warning(
                "[CoordinatedAgentWorkflow] task cancellation exceeded %.2fs: task=%s",
                max(0.0, timeout),
                task.get_name(),
            )
            task.add_done_callback(_consume_background_task_result)
            return False
    _consume_background_task_result(task)
    return True


def _get_resource_lease_manager() -> ResourceLeaseManager:
    loop = asyncio.get_running_loop()
    manager = _RESOURCE_LEASE_MANAGERS.get(loop)
    if manager is not None:
        return manager

    backend = os.getenv("AGENT_RESOURCE_LEASE_BACKEND", "memory").strip().lower()
    if backend == "redis":
        # ``get_redis`` constructs a lazy redis-py client; no network I/O is
        # performed until the first atomic Lua acquire.
        from backend.db.redis import get_redis

        manager = RedisResourceLeaseManager(
            get_redis(),
            namespace=os.getenv(
                "AGENT_RESOURCE_LEASE_NAMESPACE",
                "agent-runtime-lease",
            ),
        )
    elif backend == "memory":
        manager = InMemoryResourceLeaseManager()
    else:
        raise ValueError("AGENT_RESOURCE_LEASE_BACKEND must be 'memory' or 'redis'")
    _RESOURCE_LEASE_MANAGERS[loop] = manager
    return manager


def _resource_lease_ttl_seconds() -> float:
    try:
        return max(
            3.0,
            float(os.getenv("AGENT_RESOURCE_LEASE_TTL_SECONDS", "30")),
        )
    except (TypeError, ValueError):
        return 30.0


async def _renew_resource_lease(
    lease: Lease,
    *,
    ttl_seconds: float,
    owner_task: asyncio.Task[Any] | None,
) -> None:
    """Keep a lease alive and fail closed if distributed ownership is lost."""

    interval = max(1.0, ttl_seconds / 3.0)
    try:
        while True:
            await asyncio.sleep(interval)
            if owner_task is not None and owner_task.done():
                # 兜底:单元的 finally 清理链被二次取消打断时,续租任务不会被
                # 取消。没有这个自检,孤儿续租会每周期成功续租、容量槽位永久
                # 占用直到进程重启;有了它,孤儿最多存活一个周期,租约随 TTL
                # 自然过期回收。正常路径仍由清理链先取消续租再显式 release。
                logger.warning(
                    "resource lease renewal orphaned (owner finished); "
                    "stopping so the lease expires by TTL: resource=%s owner=%s token=%s",
                    lease.resource,
                    lease.owner_id,
                    lease.fencing_token,
                )
                return
            await asyncio.wait_for(
                lease.renew(ttl_seconds=ttl_seconds),
                timeout=max(1.0, ttl_seconds / 2.0),
            )
    except asyncio.CancelledError:
        raise
    except Exception:
        logger.exception(
            "resource lease renewal failed; cancelling owner: resource=%s owner=%s token=%s",
            lease.resource,
            lease.owner_id,
            lease.fencing_token,
        )
        if owner_task is not None and not owner_task.done():
            owner_task.cancel()


class _AdaptiveAnalysisGate:
    """Small, timeout-bounded rendezvous so fast branches are not held by slow ones."""

    def __init__(self, *, min_ready: int, timeout_seconds: float):
        self._min_ready = max(1, min_ready)
        self._timeout_seconds = max(0.0, timeout_seconds)
        self._ready_count = 0
        self._lock = asyncio.Lock()
        self._event = asyncio.Event()

    async def mark_ready_and_wait(self) -> None:
        async with self._lock:
            self._ready_count += 1
            if self._ready_count >= self._min_ready:
                self._event.set()
        if self._timeout_seconds <= 0:
            return
        with suppress(asyncio.TimeoutError):
            await asyncio.wait_for(self._event.wait(), timeout=self._timeout_seconds)
