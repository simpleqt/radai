"""协调工作流研究调度与单元执行(首参 self 为 CoordinatedAgentWorkflow)。"""

from __future__ import annotations

import logging

import asyncio

from backend.application.agent_workflows.coordination_types import (
    CoordinationUnitResult,
    ResearchTask,
)
from backend.core.agents.context.subagent_adapter import format_handoff_bundle
from backend.domain.agents.request import AgentRequest
from backend.domain.agents.runtime_context import AgentRuntimeContext
from collections.abc import Awaitable, Callable
from typing import Any
from backend.application.agent_workflows.coordination_runtime_support import (
    _AdaptiveAnalysisGate,
    _MANIFEST_RESEARCH_ROLES,
)


logger = logging.getLogger(__name__)


WorkflowEventSink = Callable[[tuple[str, dict[str, Any]]], Awaitable[None]]


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
    first_wave_items = list(first_wave_roles.items())
    gathered = await asyncio.gather(
        *[
            self._run_unit(
                role=role,
                question=f"{instruction}\n\n原始任务：\n{request.question}",
                request=request,
                context=context,
                tool_access=tool_access,
            )
            for role, (instruction, tool_access) in first_wave_items
        ],
        # 与 research_scheduler 波次同一纪律:单元运行器内部失败已被归一化
        # 为 error 结果,但从 _run_unit 逃逸的异常(spawn/预算/基础设施类)
        # 不得中止整波并丢弃已完成兄弟分支的证据——失败分支降级为 error
        # 结果,架构师与聚合器继续消费可用交接。父任务取消仍照常向外传播。
        return_exceptions=True,
    )
    first_wave_results: list[CoordinationUnitResult] = []
    for (role, _spec), outcome in zip(first_wave_items, gathered, strict=True):
        if isinstance(outcome, BaseException):
            logger.warning(
                "parallel first-wave unit escaped the unit runner: role=%s error=%s",
                role,
                outcome,
            )
            first_wave_results.append(
                CoordinationUnitResult(
                    role=role,
                    answer="",
                    error=str(outcome) or type(outcome).__name__,
                )
            )
        else:
            first_wave_results.append(outcome)
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
