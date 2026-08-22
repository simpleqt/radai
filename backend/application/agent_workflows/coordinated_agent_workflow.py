"""Executable plan/solve and coordinated multi-agent workflows."""

from __future__ import annotations

import asyncio
import logging
from collections.abc import AsyncIterator, Awaitable, Callable
from typing import TYPE_CHECKING, Any

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
from backend.core.agents.resource_lease import ResourceLeaseManager
from backend.domain.agents.events import AgentEvent
from backend.domain.agents.request import AgentRequest
from backend.domain.agents.runtime_agent import RuntimeAgentSpec
from backend.domain.agents.runtime_budget import AgentRuntimeBudget
from backend.domain.agents.runtime_context import AgentRuntimeContext

if TYPE_CHECKING:
    from backend.application.agents.definition import ResolvedAgentDefinition

from backend.application.agent_workflows.coordination_runtime_support import (  # noqa: F401  (含测试与 _legacy 属性访问兼容 re-export)
    _DEEP_RESEARCH_BRANCH_WEB_CALLS,
    _DEEP_RESEARCH_WEB_MAX_RESULTS,
    _MAX_CONCURRENT_PER_USER_MODEL,
    _MAX_CONCURRENT_RESEARCH,
    _RESEARCH_LIMITERS,
    _RESEARCH_PHASE_TIMEOUT_ERROR,
    _AdaptiveAnalysisGate,
    _ManagedCoordinationUnitFailure,
    _ManagedCoordinationUnitTimeout,
    _cancel_child_on_parent_exit,
    _cancel_task_bounded,
    _capture_runtime_budget,
    _compact_web_repair_evidence,
    _get_resource_lease_manager,
    _join_unique_errors,
    _renew_resource_lease,
    _resource_lease_ttl_seconds,
    _tool_result_error,
)
from backend.application.agent_workflows.coordination_role_config import (
    _research_role as _impl_research_role,
    _runtime_agent_specs as _impl_runtime_agent_specs,
    _role_spec as _impl_role_spec,
    _role_instruction as _impl_role_instruction,
    _effective_tool_access as _impl_effective_tool_access,
    _effective_allowed_tools as _impl_effective_allowed_tools,
    _role_timeout_seconds as _impl_role_timeout_seconds,
    _role_model_name as _impl_role_model_name,
    _research_task_model_name as _impl_research_task_model_name,
    _model_tier as _impl_model_tier,
    _role_runnable_config as _impl_role_runnable_config,
    _role_can_use_network as _impl_role_can_use_network,
    _role_can_read_kb as _impl_role_can_read_kb,
    _role_required_tools as _impl_role_required_tools,
    _planner_prompt as _impl_planner_prompt,
    _research_llm_capacity as _impl_research_llm_capacity,
    _minimum_research_branch_start_seconds as _impl_minimum_research_branch_start_seconds,
    _max_spawned_agents as _impl_max_spawned_agents,
    _coordinated_retrieval_budgets as _impl_coordinated_retrieval_budgets,
    _parse_research_tasks as _impl_parse_research_tasks,
    _fallback_research_tasks as _impl_fallback_research_tasks,
)
from backend.application.agent_workflows.coordination_execution import (
    _run_plan_solve as _impl_run_plan_solve,
    _run_research_reflection as _impl_run_research_reflection,
    _research_phase_timeout_result as _impl_research_phase_timeout_result,
    _run_scheduled_research_tasks as _impl_run_scheduled_research_tasks,
    _run_research_task as _impl_run_research_task,
    _run_research_task_unmanaged as _impl_run_research_task_unmanaged,
    _build_research_report as _impl_build_research_report,
    _run_parallel as _impl_run_parallel,
    _run_alternating as _impl_run_alternating,
    _run_unit as _impl_run_unit,
    _run_unit_unmanaged as _impl_run_unit_unmanaged,
    _unit_thread_id as _impl_unit_thread_id,
)

logger = logging.getLogger(__name__)

WorkflowEventSink = Callable[[tuple[str, dict[str, Any]]], Awaitable[None]]

# Persistence resolves this public symbol dynamically so deployments and tests
# can inject an alternate artifact metadata builder through the workflow facade.
build_registered_artifact_metadata = _default_artifact_metadata_builder


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
    async def _invoke_runtime_tool_structured(
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

        # 原生 async:在调用方事件循环上执行,复用主循环的 keep-alive 客户端与
        # 按循环的进程级并发帽(旧的「阻塞线程 + run_in_fresh_loop」桥每轮重建
        # MySQL 引擎/HTTP/Redis 客户端,还让一次性循环绕过了按循环的检索信号量)。
        # 子任务自带 contextvars 拷贝,与旧桥 copy_context 的隔离语义一致——
        # 工具侧的上下文写入(如分支本地图片注册表)不回渗调用方。
        # 父任务(wait_for 包装)退出/超时时联动取消子任务:旧线程桥超时后线程
        # 仍会跑完并占住阻塞池槽位,现在取消会真正终止底层调用。
        task = asyncio.create_task(invoke())
        _cancel_child_on_parent_exit(task)
        return await task

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

    # 实现在 coordination_execution.py(模块级函数,self 经 descriptor 绑定)
    _run_plan_solve = _impl_run_plan_solve

    # 实现在 coordination_execution.py(模块级函数,self 经 descriptor 绑定)
    _run_research_reflection = _impl_run_research_reflection

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

    # 实现在 coordination_execution.py(模块级函数,self 经 descriptor 绑定)
    _research_phase_timeout_result = _impl_research_phase_timeout_result

    # 实现在 coordination_execution.py(模块级函数,self 经 descriptor 绑定)
    _run_scheduled_research_tasks = _impl_run_scheduled_research_tasks

    # 实现在 coordination_execution.py(模块级函数,self 经 descriptor 绑定)
    _run_research_task = _impl_run_research_task

    # 实现在 coordination_execution.py(模块级函数,self 经 descriptor 绑定)
    _run_research_task_unmanaged = _impl_run_research_task_unmanaged

    # 实现在 coordination_role_config.py(模块级函数,self 经 descriptor 绑定)
    _research_role = _impl_research_role

    # 实现在 coordination_role_config.py(模块级函数,self 经 descriptor 绑定)
    _runtime_agent_specs = _impl_runtime_agent_specs

    # 实现在 coordination_role_config.py(模块级函数,self 经 descriptor 绑定)
    _role_spec = _impl_role_spec

    # 实现在 coordination_role_config.py(模块级函数,self 经 descriptor 绑定)
    _role_instruction = _impl_role_instruction

    # 实现在 coordination_role_config.py(模块级函数,self 经 descriptor 绑定)
    _effective_tool_access = _impl_effective_tool_access

    # 实现在 coordination_role_config.py(模块级函数,self 经 descriptor 绑定)
    _effective_allowed_tools = _impl_effective_allowed_tools

    # 实现在 coordination_role_config.py(模块级函数,self 经 descriptor 绑定)
    _role_timeout_seconds = _impl_role_timeout_seconds

    # 实现在 coordination_role_config.py(模块级函数,self 经 descriptor 绑定)
    _role_model_name = _impl_role_model_name

    # 实现在 coordination_role_config.py(模块级函数,self 经 descriptor 绑定)
    _research_task_model_name = _impl_research_task_model_name

    # 实现在 coordination_role_config.py(模块级函数,self 经 descriptor 绑定)
    _model_tier = _impl_model_tier

    # 实现在 coordination_role_config.py(模块级函数,self 经 descriptor 绑定)
    _role_runnable_config = _impl_role_runnable_config

    # 实现在 coordination_role_config.py(模块级函数,self 经 descriptor 绑定)
    _role_can_use_network = _impl_role_can_use_network

    # 实现在 coordination_role_config.py(模块级函数,self 经 descriptor 绑定)
    _role_can_read_kb = _impl_role_can_read_kb

    # 实现在 coordination_role_config.py(模块级函数,self 经 descriptor 绑定)
    _role_required_tools = _impl_role_required_tools

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

    # 实现在 coordination_execution.py(模块级函数,self 经 descriptor 绑定)
    _build_research_report = _impl_build_research_report

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

    # 实现在 coordination_execution.py(模块级函数,self 经 descriptor 绑定)
    _run_parallel = _impl_run_parallel

    # 实现在 coordination_execution.py(模块级函数,self 经 descriptor 绑定)
    _run_alternating = _impl_run_alternating

    # 实现在 coordination_execution.py(模块级函数,self 经 descriptor 绑定)
    _run_unit = _impl_run_unit

    # 实现在 coordination_execution.py(模块级函数,self 经 descriptor 绑定)
    _run_unit_unmanaged = _impl_run_unit_unmanaged

    @staticmethod
    def _evidence_record_ref(record: dict[str, Any]) -> str:
        return CoordinationHandoffService.evidence_record_ref(record)

    @staticmethod
    def _captured_evidence_ref_map(
        evidence_refs: list[str],
        evidence_records: list[dict[str, Any]],
    ) -> dict[str, str]:
        return CoordinationHandoffService.captured_evidence_ref_map(
            evidence_refs,
            evidence_records,
        )

    @staticmethod
    def _resolve_captured_ref(
        raw_ref: str,
        allowed_refs: dict[str, str],
    ) -> str | None:
        return CoordinationHandoffService.resolve_captured_ref(raw_ref, allowed_refs)

    @staticmethod
    def _parse_structured_convergence_payload(
        payload: Any,
    ) -> dict[str, Any] | None:
        return CoordinationHandoffService.parse_structured_payload(payload)

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
        return CoordinationHandoffService.sanitize_structured_handoff(
            payload,
            role=role,
            objective=objective,
            answer_fallback=answer_fallback,
            task_id=task_id,
            evidence_refs=evidence_refs,
            evidence_records=evidence_records,
        )

    @staticmethod
    def _build_tool_free_convergence_prompt(
        *,
        objective: str,
        partial_answer: str,
        evidence_refs: list[str],
        evidence_records: list[dict[str, Any]],
    ) -> str:
        return CoordinationHandoffService.build_tool_free_convergence_prompt(
            objective=objective,
            partial_answer=partial_answer,
            evidence_refs=evidence_refs,
            evidence_records=evidence_records,
        )

    @staticmethod
    def _collect_dynamic_web_evidence(
        process_events: list[dict[str, Any]],
        *,
        evidence_refs: list[str] | None = None,
        evidence_records: list[dict[str, Any]] | None = None,
    ) -> tuple[list[str], list[dict[str, Any]]]:
        return CoordinationHandoffService.collect_dynamic_web_evidence(
            process_events,
            evidence_refs=evidence_refs,
            evidence_records=evidence_records,
        )

    @staticmethod
    def _task_scoped_unit_process_event(
        event: AgentEvent,
        *,
        task_id: str | None,
        role: str,
    ) -> dict[str, Any] | None:
        return CoordinationHandoffService.task_scoped_unit_process_event(
            event,
            task_id=task_id,
            role=role,
        )

    # 实现在 coordination_role_config.py(模块级函数,self 经 descriptor 绑定)
    _planner_prompt = _impl_planner_prompt

    # 实现在 coordination_role_config.py(模块级函数,self 经 descriptor 绑定)
    _research_llm_capacity = _impl_research_llm_capacity

    # 实现在 coordination_role_config.py(模块级函数,self 经 descriptor 绑定)
    _minimum_research_branch_start_seconds = _impl_minimum_research_branch_start_seconds

    # 实现在 coordination_role_config.py(模块级函数,self 经 descriptor 绑定)
    _max_spawned_agents = _impl_max_spawned_agents

    # 实现在 coordination_role_config.py(模块级函数,self 经 descriptor 绑定)
    _coordinated_retrieval_budgets = _impl_coordinated_retrieval_budgets

    # 实现在 coordination_role_config.py(模块级函数,self 经 descriptor 绑定)
    _parse_research_tasks = _impl_parse_research_tasks

    # 实现在 coordination_role_config.py(模块级函数,self 经 descriptor 绑定)
    _fallback_research_tasks = _impl_fallback_research_tasks

    @staticmethod
    def _writer_safe_branch_results(
        branch_results: list[CoordinationUnitResult],
    ) -> list[CoordinationUnitResult]:
        return CoordinationHandoffService.writer_safe_branch_results(branch_results)

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

    # 实现在 coordination_execution.py(模块级函数,self 经 descriptor 绑定)
    _unit_thread_id = _impl_unit_thread_id

    def build_executor(self, request: AgentRequest, context: AgentRuntimeContext):
        return self._delegate.build_executor(request, context)
