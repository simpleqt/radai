"""Executable plan/solve and coordinated multi-agent workflows."""

from __future__ import annotations

import asyncio
import json
import logging
import os
from collections.abc import AsyncIterator, Awaitable, Callable
from contextlib import suppress
from dataclasses import replace
from typing import TYPE_CHECKING, Any
from weakref import WeakKeyDictionary

from langchain_core.runnables import RunnableConfig
from langgraph.checkpoint.base import BaseCheckpointSaver

from backend.application.agent_trace import AgentTraceRecorder
from backend.application.agent_workflows.coordination_handoff import (
    CoordinationHandoffService,
)
from backend.application.agent_workflows.coordination_persistence import (
    CoordinationPersistenceMixin,
)
from backend.application.agent_workflows.coordination_types import (
    CoordinationUnitResult,
    ResearchTask,
)
from backend.application.agent_workflows.research_planning import (
    build_planner_prompt,
    fallback_research_tasks,
    parse_research_tasks,
)
from backend.application.agent_workflows.research_specialists import (
    normalize_research_specialist,
)
from backend.application.agent_workflows.standard_agent_workflow import (
    StandardAgentWorkflow,
)
from backend.application.agents.runtime_agent_loader import RuntimeAgentLoader
from backend.application.artifacts.artifact_utils import (
    build_registered_artifact_metadata as _default_artifact_metadata_builder,
)
from backend.application.coordination import (
    CoordinationStrategy,
    DefaultCoordinationStrategy,
)
from backend.application.tools.runtime_invoker import RuntimeToolInvoker
from backend.core.agents.context.subagent_adapter import format_handoff_bundle
from backend.core.agents.resource_lease import (
    InMemoryResourceLeaseManager,
    Lease,
    RedisResourceLeaseManager,
    ResourceLeaseManager,
)
from backend.domain.agents.events import AgentEvent
from backend.domain.agents.request import AgentRequest
from backend.domain.agents.retrieval_policy import RetrievalRuntimePolicy
from backend.domain.agents.runtime_agent import RuntimeAgentSpec
from backend.domain.agents.runtime_budget import AgentRuntimeBudget
from backend.domain.agents.runtime_context import AgentRuntimeContext

if TYPE_CHECKING:
    from backend.application.agents.definition import ResolvedAgentDefinition

WorkflowEventSink = Callable[[tuple[str, dict[str, Any]]], Awaitable[None]]

# Persistence resolves this public symbol dynamically so deployments and tests
# can inject an alternate artifact metadata builder through the workflow facade.
build_registered_artifact_metadata = _default_artifact_metadata_builder

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


logger = logging.getLogger(__name__)


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


class CoordinatedAgentWorkflow(CoordinationPersistenceMixin):
    """Coordinate isolated agent units with deterministic evidence collection."""

    def __init__(
        self,
        *,
        name: str,
        checkpointer: BaseCheckpointSaver | None = None,
        runnable_config: RunnableConfig | None = None,
        strategy: CoordinationStrategy | None = None,
        delegate: StandardAgentWorkflow | None = None,
        runtime_budget: AgentRuntimeBudget | None = None,
        resource_lease_manager: ResourceLeaseManager | None = None,
        agent_specs: dict[str, RuntimeAgentSpec] | None = None,
        context_policy: Any | None = None,
        resolved_definition: ResolvedAgentDefinition | None = None,
    ):
        self.name = name
        self._resolved_definition = resolved_definition
        self._runtime_budget = _capture_runtime_budget(runtime_budget)
        self._agent_specs = dict(agent_specs or {})
        self._context_policy = context_policy
        self._strategy = strategy or DefaultCoordinationStrategy(name)
        self._delegate = delegate or StandardAgentWorkflow(
            name="full",
            checkpointer=checkpointer,
            runnable_config=runnable_config,
            runtime_budget=self._runtime_budget,
            agent_specs=self._agent_specs,
            context_policy=self._context_policy,
            resolved_definition=resolved_definition,
        )
        self._checkpointer = checkpointer
        self._runnable_config = runnable_config
        self._runtime_agent_loader = RuntimeAgentLoader()
        self._resource_lease_manager = resource_lease_manager
        self._active_traces: dict[str, AgentTraceRecorder] = {}
        # Components are imported lazily because they depend on the host's
        # compatibility hooks while this module remains the public facade.
        from backend.application.agent_workflows.coordination_runtime_lifecycle import (
            CoordinationRuntimeLifecycle,
        )
        from backend.application.agent_workflows.coordination_unit_runner import (
            CoordinationUnitRunner,
        )
        from backend.application.agent_workflows.deep_research_supervisor import (
            DeepResearchSupervisor,
        )
        from backend.application.agent_workflows.report_convergence_builder import (
            ReportConvergenceBuilder,
        )
        from backend.application.agent_workflows.research_branch_runner import (
            ResearchBranchRunner,
        )
        from backend.application.agent_workflows.research_scheduler import (
            ResearchScheduler,
        )

        self._research_scheduler = ResearchScheduler(self)
        self._research_branch_runner = ResearchBranchRunner(self)
        self._unit_runner = CoordinationUnitRunner(self)
        self._runtime_lifecycle = CoordinationRuntimeLifecycle(self)
        self._deep_research_supervisor = DeepResearchSupervisor(self)
        self._report_convergence_builder = ReportConvergenceBuilder(self)

    @property
    def runtime_budget(self) -> AgentRuntimeBudget:
        """Immutable execution limits captured for this workflow instance."""

        return self._runtime_budget

    @staticmethod
    def _invoke_runtime_tool_structured_blocking(
        context: AgentRuntimeContext,
        workflow_name: str,
        tool_name: str,
        args: dict[str, Any],
        invoker_type: type[RuntimeToolInvoker] = RuntimeToolInvoker,
        resolved_definition: ResolvedAgentDefinition | None = None,
    ) -> dict[str, Any]:
        async def invoke() -> dict[str, Any]:
            invoker_kwargs: dict[str, Any] = {
                "context": context,
                "workflow_name": workflow_name,
                "enforce_skill_load": False,
            }
            if resolved_definition is not None:
                invoker_kwargs["resolved_agent_definition"] = resolved_definition
            invoker = invoker_type(
                **invoker_kwargs,
            )
            structured_method = getattr(invoker, "invoke_structured", None)
            if callable(structured_method):
                return await structured_method(tool_name, args)
            value = await invoker.invoke(tool_name, args)
            return {"text": str(value or ""), "evidence_records": [], "stats": {}}

        return asyncio.run(invoke())

    async def run(
        self,
        request: AgentRequest,
        context: AgentRuntimeContext,
    ) -> AsyncIterator[AgentEvent]:
        async for event in self._runtime_lifecycle.run(request, context):
            yield event

    def _finish_active_trace(
        self,
        *,
        request: AgentRequest,
        context: AgentRuntimeContext,
        status: str,
        error: str | None,
    ) -> None:
        self._runtime_lifecycle._finish_active_trace(
            request=request,
            context=context,
            status=status,
            error=error,
        )

    async def _run_impl(
        self,
        request: AgentRequest,
        context: AgentRuntimeContext,
    ) -> AsyncIterator[AgentEvent]:
        async for event in self._runtime_lifecycle._run_impl(request, context):
            yield event

    async def _run_plan_solve(
        self,
        request: AgentRequest,
        context: AgentRuntimeContext,
        event_sink: WorkflowEventSink | None = None,
    ) -> tuple[list[CoordinationUnitResult], list[ResearchTask]]:
        return await self._deep_research_supervisor._run_plan_solve(
            request,
            context,
            event_sink=event_sink,
        )

    async def _run_research_reflection(
        self,
        *,
        request: AgentRequest,
        context: AgentRuntimeContext,
        planner: CoordinationUnitResult,
        tasks: list[ResearchTask],
        branch_results: list[CoordinationUnitResult],
        kb_circuit_open: asyncio.Event,
        event_sink: WorkflowEventSink | None,
        research_deadline: float | None = None,
    ) -> tuple[
        list[ResearchTask],
        list[CoordinationUnitResult],
        list[CoordinationUnitResult],
    ]:
        return await self._deep_research_supervisor._run_research_reflection(
            request=request,
            context=context,
            planner=planner,
            tasks=tasks,
            branch_results=branch_results,
            kb_circuit_open=kb_circuit_open,
            event_sink=event_sink,
            research_deadline=research_deadline,
        )

    @staticmethod
    def _prepare_followup_tasks(
        proposed_tasks: tuple[ResearchTask, ...],
        *,
        existing_tasks: list[ResearchTask],
        context: AgentRuntimeContext,
        round_index: int,
    ) -> list[ResearchTask]:
        from backend.application.agent_workflows.research_scheduler import (
            ResearchScheduler,
        )

        return ResearchScheduler._prepare_followup_tasks(
            proposed_tasks,
            existing_tasks=existing_tasks,
            context=context,
            round_index=round_index,
        )

    @staticmethod
    def _prepare_quality_recovery_tasks(
        *,
        tasks: list[ResearchTask],
        branch_results: list[CoordinationUnitResult],
        context: AgentRuntimeContext,
        round_index: int,
        limit: int,
    ) -> list[ResearchTask]:
        from backend.application.agent_workflows.research_scheduler import (
            ResearchScheduler,
        )

        return ResearchScheduler._prepare_quality_recovery_tasks(
            tasks=tasks,
            branch_results=branch_results,
            context=context,
            round_index=round_index,
            limit=limit,
        )

    @staticmethod
    def _prepare_conflict_resolution_task(
        *,
        existing_tasks: list[ResearchTask],
        branch_results: list[CoordinationUnitResult],
        context: AgentRuntimeContext,
        round_index: int,
    ) -> ResearchTask | None:
        from backend.application.agent_workflows.research_scheduler import (
            ResearchScheduler,
        )

        return ResearchScheduler._prepare_conflict_resolution_task(
            existing_tasks=existing_tasks,
            branch_results=branch_results,
            context=context,
            round_index=round_index,
        )

    def _research_phase_timeout_result(
        self,
        *,
        task: ResearchTask,
        index: int,
        context: AgentRuntimeContext,
        used_fallback: bool,
    ) -> CoordinationUnitResult:
        return self._deep_research_supervisor._research_phase_timeout_result(
            task=task,
            index=index,
            context=context,
            used_fallback=used_fallback,
        )

    async def _run_scheduled_research_tasks(
        self,
        *,
        scheduled_tasks: list[tuple[int, ResearchTask]],
        used_fallback: bool,
        kb_circuit_open: asyncio.Event,
        request: AgentRequest,
        context: AgentRuntimeContext,
        event_sink: WorkflowEventSink | None,
        analysis_gate: _AdaptiveAnalysisGate | None,
        partial_result_limit: int | None = None,
        partial_result_sink: (
            Callable[[list[CoordinationUnitResult]], Awaitable[None]] | None
        ) = None,
        research_deadline: float | None = None,
    ) -> list[CoordinationUnitResult]:
        arguments = dict(locals())
        arguments.pop("self")
        return await self._research_scheduler._run_scheduled_research_tasks(**arguments)

    async def _run_research_task(
        self,
        *,
        task: ResearchTask,
        index: int,
        role: str,
        kb_circuit_open: asyncio.Event,
        request: AgentRequest,
        context: AgentRuntimeContext,
        event_sink: WorkflowEventSink | None = None,
        analysis_gate: _AdaptiveAnalysisGate | None = None,
        research_deadline: float | None = None,
        dependency_results: list[CoordinationUnitResult] | None = None,
    ) -> CoordinationUnitResult:
        arguments = dict(locals())
        arguments.pop("self")
        return await self._research_branch_runner._run_research_task(**arguments)

    async def _run_research_task_unmanaged(
        self,
        *,
        task: ResearchTask,
        index: int,
        role: str,
        kb_circuit_open: asyncio.Event,
        request: AgentRequest,
        context: AgentRuntimeContext,
        event_sink: WorkflowEventSink | None = None,
        analysis_gate: _AdaptiveAnalysisGate | None = None,
        research_deadline: float | None = None,
        dependency_results: list[CoordinationUnitResult] | None = None,
    ) -> CoordinationUnitResult:
        arguments = dict(locals())
        arguments.pop("self")
        return await self._research_branch_runner._run_research_task_unmanaged(
            **arguments
        )

    def _research_role(
        self,
        *,
        task: ResearchTask,
        index: int,
        used_fallback: bool,
        context: AgentRuntimeContext | None,
    ) -> str:
        return normalize_research_specialist(
            task.agent_type,
            agent_specs=self._runtime_agent_specs(context),
            default_index=index,
        )

    def _runtime_agent_specs(
        self,
        context: AgentRuntimeContext | None = None,
    ) -> dict[str, RuntimeAgentSpec]:
        specs: dict[str, RuntimeAgentSpec] = {}
        specs.update(self._agent_specs)
        try:
            specs.update(self._runtime_agent_loader.load_all())
        except Exception:
            logger.debug("runtime agent manifest unavailable", exc_info=True)
        if context is not None:
            specs.update(getattr(context, "agent_specs", None) or {})
        return specs

    def _role_spec(
        self,
        context: AgentRuntimeContext | None,
        role: str,
    ) -> RuntimeAgentSpec | None:
        return self._runtime_agent_specs(context).get(str(role or "").strip())

    def _role_instruction(
        self,
        role: str,
        context: AgentRuntimeContext | None,
    ) -> str:
        spec = self._role_spec(context, role)
        return spec.system_prompt if spec is not None else ""

    def _effective_tool_access(
        self,
        *,
        role: str,
        requested: bool,
        context: AgentRuntimeContext | None,
    ) -> bool:
        spec = self._role_spec(context, role)
        return bool(requested and (spec.tool_access if spec is not None else True))

    def _effective_allowed_tools(
        self,
        *,
        role: str,
        requested_allowed_tools: set[str] | None,
        context: AgentRuntimeContext | None,
    ) -> set[str] | None:
        spec = self._role_spec(context, role)
        if spec is None:
            return (
                set(requested_allowed_tools)
                if requested_allowed_tools is not None
                else None
            )
        if spec.allowed_tools is None:
            allowed = (
                set(requested_allowed_tools)
                if requested_allowed_tools is not None
                else None
            )
        elif requested_allowed_tools is None:
            allowed = set(spec.allowed_tools)
        else:
            allowed = set(requested_allowed_tools) & set(spec.allowed_tools)
        return allowed - set(spec.disallowed_tools) if allowed is not None else None

    def _role_timeout_seconds(
        self,
        role: str,
        context: AgentRuntimeContext | None,
    ) -> float:
        spec = self._role_spec(context, role)
        return float(
            spec.timeout_seconds
            if spec is not None
            else self._runtime_budget.unit_total_timeout_seconds
        )

    def _role_model_name(
        self,
        role: str,
        context: AgentRuntimeContext,
    ) -> str | None:
        context_model_name = getattr(context, "model_name", None)
        spec = self._role_spec(context, role)
        if spec is not None and spec.model:
            if spec.model == "fast":
                if self.name == "deep_research":
                    return (
                        self._runtime_budget.deep_research_fast_model_name
                        or context_model_name
                    )
                return context_model_name
            if spec.model != "default":
                return spec.model
        return context_model_name

    def _research_task_model_name(
        self,
        *,
        task: ResearchTask,
        role: str,
        context: AgentRuntimeContext,
    ) -> str | None:
        """Route only bounded, low-risk research branches to the fast model."""

        role_model = self._role_model_name(role, context)
        fast_model = self._runtime_budget.deep_research_fast_model_name
        if self.name != "deep_research" or not fast_model:
            return role_model
        spec = self._role_spec(context, role)
        if spec is not None and spec.model not in {None, "default", "fast"}:
            return role_model
        if self._runtime_budget.deep_research_fast_model_max_budget <= 0:
            return role_model
        if task.budget > self._runtime_budget.deep_research_fast_model_max_budget:
            return role_model
        if str(task.risk or "").strip().lower() in _HIGH_RISK_LEVELS:
            return role_model
        if role not in _FAST_MODEL_RESEARCH_ROLES:
            return role_model
        return fast_model

    def _model_tier(
        self,
        model_name: str | None,
        context: AgentRuntimeContext,
    ) -> str:
        fast_model = self._runtime_budget.deep_research_fast_model_name
        request_metadata = getattr(context, "request_metadata", None) or {}
        primary_model = request_metadata.get(
            "coordination_primary_model_name",
            getattr(context, "model_name", None),
        )
        if fast_model and model_name == fast_model and model_name != primary_model:
            return "fast"
        return "primary"

    def _role_runnable_config(
        self,
        role: str,
        context: AgentRuntimeContext,
    ) -> RunnableConfig | None:
        spec = self._role_spec(context, role)
        if spec is None:
            return self._runnable_config
        config: dict[str, Any] = dict(self._runnable_config or {})
        metadata = dict(config.get("metadata") or {})
        metadata.update(
            {
                "runtime_agent": spec.name,
                "runtime_agent_output_schema": spec.output_schema,
                "runtime_agent_max_turns": spec.max_turns,
            }
        )
        config["metadata"] = metadata
        # This graph has several middleware nodes per logical model/tool turn,
        # and a model may issue multiple independent searches in one turn.
        # Keep the semantic turn limit in the agent spec while allowing enough
        # graph supersteps to finish its answer. Retrieval/tool budgets still
        # provide the hard loop bound.
        config["recursion_limit"] = min(128, max(32, spec.max_turns * 8 + 8))
        return config

    def _role_can_use_network(
        self,
        role: str,
        context: AgentRuntimeContext | None,
    ) -> bool:
        spec = self._role_spec(context, role)
        return bool(spec.can_use_network if spec is not None else True)

    def _role_can_read_kb(
        self,
        role: str,
        context: AgentRuntimeContext | None,
    ) -> bool:
        spec = self._role_spec(context, role)
        return bool(spec.can_read_kb if spec is not None else True)

    def _role_required_tools(
        self,
        role: str,
        context: AgentRuntimeContext | None,
    ) -> set[str]:
        spec = self._role_spec(context, role)
        return set(spec.required_tools if spec is not None else set())

    @staticmethod
    async def _emit_event(
        event_sink: WorkflowEventSink | None,
        event_type: str,
        payload: dict[str, Any],
    ) -> None:
        if event_sink is not None:
            await event_sink((event_type, payload))

    @classmethod
    async def _emit_process_event(
        cls,
        event_sink: WorkflowEventSink | None,
        process_event: dict[str, Any],
    ) -> None:
        await cls._emit_event(event_sink, "workflow.step", {"event": process_event})

    async def _build_research_report(
        self,
        *,
        request: AgentRequest,
        context: AgentRuntimeContext,
        planner: CoordinationUnitResult,
        tasks: list[ResearchTask],
        branch_results: list[CoordinationUnitResult],
        event_sink: WorkflowEventSink | None,
        research_budget_exhausted: bool,
    ) -> CoordinationUnitResult:
        return await self._report_convergence_builder._build_research_report(
            request=request,
            context=context,
            planner=planner,
            tasks=tasks,
            branch_results=branch_results,
            event_sink=event_sink,
            research_budget_exhausted=research_budget_exhausted,
        )

    @staticmethod
    def _build_fallback_report(
        *,
        request: AgentRequest,
        planner: CoordinationUnitResult,
        tasks: list[ResearchTask],
        branch_results: list[CoordinationUnitResult],
    ) -> str:
        from backend.application.agent_workflows.report_convergence_builder import (
            ReportConvergenceBuilder,
        )

        return ReportConvergenceBuilder._build_fallback_report(
            request=request,
            planner=planner,
            tasks=tasks,
            branch_results=branch_results,
        )

    async def _run_parallel(
        self,
        request: AgentRequest,
        context: AgentRuntimeContext,
    ) -> list[CoordinationUnitResult]:
        first_wave_roles = {
            role: (
                self._role_instruction(role, context),
                self._effective_tool_access(
                    role=role,
                    requested=True,
                    context=context,
                ),
            )
            for role in _MANIFEST_RESEARCH_ROLES
            if role != "solution_architect"
        }
        first_wave_results = list(
            await asyncio.gather(
                *[
                    self._run_unit(
                        role=role,
                        question=f"{instruction}\n\n原始任务：\n{request.question}",
                        request=request,
                        context=context,
                        tool_access=tool_access,
                    )
                    for role, (instruction, tool_access) in first_wave_roles.items()
                ]
            )
        )
        architect_handoffs = format_handoff_bundle(
            [result.handoff for result in first_wave_results if result.handoff],
            max_chars=14000,
        )
        architect = await self._run_unit(
            role="solution_architect",
            question=(
                f"{self._role_instruction('solution_architect', context)}\n\n"
                f"原始任务：\n{request.question}\n\n"
                f"前置分支结构化交接：\n{architect_handoffs}"
            ),
            request=request,
            context=context,
            tool_access=self._effective_tool_access(
                role="solution_architect",
                requested=True,
                context=context,
            ),
        )
        branch_results = [*first_wave_results, architect]
        handoffs = format_handoff_bundle(
            [result.handoff for result in branch_results if result.handoff],
            max_chars=18000,
        )
        synthesizer = await self._run_unit(
            role="synthesizer",
            question=(
                "你是并行结果聚合智能体。比较各分支结论，解决冲突，"
                "保留有证据支持的内容并直接输出最终答案。\n\n"
                f"原始任务：\n{request.question}\n\n结构化分支交接：\n{handoffs}"
            ),
            request=request,
            context=context,
            tool_access=False,
        )
        return [*branch_results, synthesizer]

    async def _run_alternating(
        self,
        request: AgentRequest,
        context: AgentRuntimeContext,
    ) -> list[CoordinationUnitResult]:
        rounds = [
            ("analyst", "提出初步分析、假设和证据需求。", True),
            ("critic", "审查上一轮结果，指出错误、遗漏和未经证实的结论并修正。", False),
            ("finalizer", "吸收前两轮结果并直接输出最终答案。", False),
        ]
        results: list[CoordinationUnitResult] = []
        for role, instruction, tool_access in rounds:
            handoffs = format_handoff_bundle(
                [result.handoff for result in results if result.handoff],
                max_chars=14000,
            )
            result = await self._run_unit(
                role=role,
                question=(
                    f"{instruction}\n\n原始任务：\n{request.question}"
                    + (f"\n\n已有结构化交接：\n{handoffs}" if handoffs else "")
                ),
                request=request,
                context=context,
                tool_access=tool_access,
            )
            results.append(result)
        return results

    async def _run_unit(
        self,
        *,
        role: str,
        question: str,
        request: AgentRequest,
        context: AgentRuntimeContext,
        tool_access: bool = True,
        event_sink: WorkflowEventSink | None = None,
        progress_title: str | None = None,
        task_id: str | None = None,
        handoff_evidence_refs: list[str] | None = None,
        handoff_evidence_records: list[dict[str, Any]] | None = None,
        stream_answer: bool = False,
        allowed_tools: set[str] | None = None,
        timeout_override_seconds: float | None = None,
    ) -> CoordinationUnitResult:
        arguments = dict(locals())
        arguments.pop("self")
        return await self._unit_runner._run_unit(**arguments)

    async def _run_unit_unmanaged(
        self,
        *,
        role: str,
        question: str,
        request: AgentRequest,
        context: AgentRuntimeContext,
        tool_access: bool = True,
        event_sink: WorkflowEventSink | None = None,
        progress_title: str | None = None,
        task_id: str | None = None,
        handoff_evidence_refs: list[str] | None = None,
        handoff_evidence_records: list[dict[str, Any]] | None = None,
        stream_answer: bool = False,
        allowed_tools: set[str] | None = None,
        timeout_override_seconds: float | None = None,
    ) -> CoordinationUnitResult:
        arguments = dict(locals())
        arguments.pop("self")
        return await self._unit_runner._run_unit_unmanaged(**arguments)

    @staticmethod
    def _evidence_record_ref(record: dict[str, Any]) -> str:
        return CoordinationHandoffService.evidence_record_ref(**locals())

    @staticmethod
    def _captured_evidence_ref_map(
        evidence_refs: list[str],
        evidence_records: list[dict[str, Any]],
    ) -> dict[str, str]:
        return CoordinationHandoffService.captured_evidence_ref_map(**locals())

    @staticmethod
    def _resolve_captured_ref(
        raw_ref: str,
        allowed_refs: dict[str, str],
    ) -> str | None:
        return CoordinationHandoffService.resolve_captured_ref(**locals())

    @staticmethod
    def _parse_structured_convergence_payload(
        payload: Any,
    ) -> dict[str, Any] | None:
        return CoordinationHandoffService.parse_structured_payload(**locals())

    @staticmethod
    def _sanitize_structured_convergence_handoff(
        payload: Any,
        *,
        role: str,
        objective: str,
        answer_fallback: str,
        task_id: str,
        evidence_refs: list[str],
        evidence_records: list[dict[str, Any]],
    ) -> dict[str, Any] | None:
        return CoordinationHandoffService.sanitize_structured_handoff(**locals())

    @staticmethod
    def _build_tool_free_convergence_prompt(
        *,
        objective: str,
        partial_answer: str,
        evidence_refs: list[str],
        evidence_records: list[dict[str, Any]],
    ) -> str:
        return CoordinationHandoffService.build_tool_free_convergence_prompt(**locals())

    @staticmethod
    def _collect_dynamic_web_evidence(
        process_events: list[dict[str, Any]],
        *,
        evidence_refs: list[str] | None = None,
        evidence_records: list[dict[str, Any]] | None = None,
    ) -> tuple[list[str], list[dict[str, Any]]]:
        return CoordinationHandoffService.collect_dynamic_web_evidence(**locals())

    @staticmethod
    def _task_scoped_unit_process_event(
        event: AgentEvent,
        *,
        task_id: str | None,
        role: str,
    ) -> dict[str, Any] | None:
        return CoordinationHandoffService.task_scoped_unit_process_event(**locals())

    def _planner_prompt(
        self, request: AgentRequest, context: AgentRuntimeContext
    ) -> str:
        return build_planner_prompt(
            request,
            context,
            max_tasks=self._research_llm_capacity(),
        )

    def _research_llm_capacity(self) -> int:
        if self.name == "plan_solve":
            return self._runtime_budget.plan_solve_max_concurrent_subagents
        return self._runtime_budget.max_concurrent_research

    def _minimum_research_branch_start_seconds(self) -> float:
        """Return the floor below which a new branch cannot make a safe handoff."""

        return max(
            0.0,
            float(self._runtime_budget.deep_research_branch_min_start_seconds),
        )

    def _max_spawned_agents(self) -> int:
        """Size the tree for every bounded attempt the workflow can schedule."""

        if self.name == "deep_research":
            attempts = max(
                1,
                int(self._runtime_budget.deep_research_structured_output_attempts),
            )
            reflection_units = max(
                0,
                int(self._runtime_budget.deep_research_reflection_rounds),
            ) * (
                attempts
                + max(0, int(self._runtime_budget.deep_research_followup_tasks))
            )
            return max(
                16,
                attempts
                + int(self._runtime_budget.max_concurrent_research)
                + reflection_units
                + 1,  # final report writer
            )
        if self.name == "plan_solve":
            return max(
                16,
                int(self._runtime_budget.deep_research_structured_output_attempts)
                + int(self._runtime_budget.plan_solve_max_concurrent_subagents)
                + 1,  # reviewer
            )
        # Parallel mode currently uses five specialists plus one synthesizer;
        # alternating mode uses three sequential roles. Preserve headroom for
        # future bounded additions without sharing deep-research assumptions.
        return 16

    def _coordinated_retrieval_budgets(
        self,
        context: AgentRuntimeContext,
    ) -> dict[str, int]:
        """Bound a request without making parallel branches share one quota."""

        if self.name != "deep_research":
            return {
                "multi_kb_search": 3,
                "web_search": 10,
                "academic_search": 5,
            }

        per_branch = RetrievalRuntimePolicy.for_mode(
            deep_research=True,
            academic_enabled=("academic-search" in set(context.selected_skills or [])),
        ).tool_budgets()
        per_branch["web_search"] = min(
            per_branch.get("web_search", _DEEP_RESEARCH_BRANCH_WEB_CALLS),
            _DEEP_RESEARCH_BRANCH_WEB_CALLS,
        )
        max_branch_executions = self._research_llm_capacity() + (
            self._runtime_budget.deep_research_reflection_rounds
            * self._runtime_budget.deep_research_followup_tasks
        )
        return {
            tool_name: calls * max(1, max_branch_executions)
            for tool_name, calls in per_branch.items()
        }

    def _parse_research_tasks(
        self,
        planner_answer: str,
        original_question: str,
        context: AgentRuntimeContext | None = None,
    ) -> list[ResearchTask]:
        return parse_research_tasks(
            planner_answer,
            original_question,
            context,
            max_tasks=self._research_llm_capacity(),
        )

    def _fallback_research_tasks(
        self,
        question: str,
        context: AgentRuntimeContext | None = None,
    ) -> list[ResearchTask]:
        return fallback_research_tasks(self.name, question, context)

    @staticmethod
    def _writer_safe_branch_results(
        branch_results: list[CoordinationUnitResult],
    ) -> list[CoordinationUnitResult]:
        return CoordinationHandoffService.writer_safe_branch_results(**locals())

    @staticmethod
    def _format_evidence_matrix(
        branch_results: list[CoordinationUnitResult],
        *,
        max_chars: int = 24000,
    ) -> str:
        from backend.application.agent_workflows.report_convergence_builder import (
            ReportConvergenceBuilder,
        )

        return ReportConvergenceBuilder._format_evidence_matrix(
            branch_results,
            max_chars=max_chars,
        )

    @staticmethod
    def _claim_fingerprint(text: str) -> set[str]:
        from backend.application.agent_workflows.report_convergence_builder import (
            ReportConvergenceBuilder,
        )

        return ReportConvergenceBuilder._claim_fingerprint(text)

    @staticmethod
    def _answer_claim_candidates(answer: str) -> list[str]:
        from backend.application.agent_workflows.report_convergence_builder import (
            ReportConvergenceBuilder,
        )

        return ReportConvergenceBuilder._answer_claim_candidates(answer)

    @staticmethod
    def _verify_answer_against_matrix(
        answer: str,
        branch_results: list[CoordinationUnitResult],
    ) -> str:
        from backend.application.agent_workflows.report_convergence_builder import (
            ReportConvergenceBuilder,
        )

        return ReportConvergenceBuilder._verify_answer_against_matrix(
            answer,
            branch_results,
        )

    @staticmethod
    def _normalize_event(raw_event: Any, context: AgentRuntimeContext) -> AgentEvent:
        if isinstance(raw_event, AgentEvent):
            return raw_event
        if isinstance(raw_event, dict):
            return AgentEvent.from_runtime_payload(
                raw_event,
                request_id=context.request_id,
                thread_id=context.external_thread_id,
                sequence=0,
            )
        return AgentEvent(
            type="message.delta",
            request_id=context.request_id,
            thread_id=context.external_thread_id,
            sequence=0,
            payload={"content": str(raw_event)},
        )

    @staticmethod
    def _event(
        context: AgentRuntimeContext,
        sequence: int,
        event_type: str,
        payload: dict[str, Any],
    ) -> AgentEvent:
        return AgentEvent(
            type=event_type,
            request_id=context.request_id,
            thread_id=context.external_thread_id,
            sequence=sequence,
            payload=payload,
        )

    def _unit_thread_id(
        self,
        context: AgentRuntimeContext,
        *,
        unit_key: str,
    ) -> str:
        """Return the stable checkpointer thread for one child execution."""

        return (
            f"{context.internal_thread_id}:coord:{self.name}:"
            f"{context.request_id}:{unit_key}"
        )

    def build_executor(self, request: AgentRequest, context: AgentRuntimeContext):
        return self._delegate.build_executor(request, context)
