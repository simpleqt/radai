"""Planning and bounded supervision for deep research workflows."""

from __future__ import annotations

import asyncio
import time
from dataclasses import replace

from backend.application.agent_executors.stream_utils import make_process_event
from backend.application.agent_workflows import coordinated_agent_workflow as _legacy
from backend.application.agent_workflows.coordination_component import (
    CoordinationComponent,
)
from backend.application.agent_workflows.coordination_types import (
    CoordinationUnitResult,
    ResearchTask,
)
from backend.application.agent_workflows.evidence_verifier import CitationVerifier
from backend.application.agent_workflows.research_evidence_ledger import (
    build_coverage_ledger,
    build_default_research_brief,
    collect_source_ids,
    parse_research_brief,
)
from backend.application.agent_workflows.research_planning import (
    build_research_reflection_prompt,
    parse_research_reflection,
)
from backend.core.tools.request_limits import get_retrieval_request_cache
from backend.domain.agents.request import AgentRequest
from backend.domain.agents.runtime_context import AgentRuntimeContext

WorkflowEventSink = _legacy.WorkflowEventSink
_AdaptiveAnalysisGate = _legacy._AdaptiveAnalysisGate
_RESEARCH_PHASE_TIMEOUT_ERROR = _legacy._RESEARCH_PHASE_TIMEOUT_ERROR
_cancel_child_on_parent_exit = _legacy._cancel_child_on_parent_exit
_cancel_task_bounded = _legacy._cancel_task_bounded


class DeepResearchSupervisor(CoordinationComponent):
    async def _run_plan_solve(
        self,
        request: AgentRequest,
        context: AgentRuntimeContext,
        event_sink: WorkflowEventSink | None = None,
    ) -> tuple[list[CoordinationUnitResult], list[ResearchTask]]:
        """Compatibility entry point for the shared coordination host."""

        return await self._run_research_workflow(request, context, event_sink)

    async def _run_research_workflow(
        self,
        request: AgentRequest,
        context: AgentRuntimeContext,
        event_sink: WorkflowEventSink | None = None,
    ) -> tuple[list[CoordinationUnitResult], list[ResearchTask]]:
        # Child tasks and blocking executor contexts must share one request
        # cache for exact/in-flight dedupe and timeout cancellation.
        get_retrieval_request_cache()
        research_deadline = (
            time.monotonic()
            + float(self._runtime_budget.deep_research_research_phase_timeout_seconds)
            if self.name == "deep_research"
            else None
        )
        research_budget_exhausted = False
        await self._emit_process_event(
            event_sink,
            make_process_event(
                "status",
                "正在规划研究任务",
                "规划智能体正在拆解问题并确定并行研究方向。",
                level="active",
                metadata={"runtime_type": "planner_started"},
            ),
        )
        planner_prompt = self._planner_prompt(request, context)
        planner = CoordinationUnitResult(role="planner", answer="")
        tasks: list[ResearchTask] = []
        attempt_limit = self._runtime_budget.deep_research_structured_output_attempts
        for attempt in range(1, attempt_limit + 1):
            planner_timeout_override: float | None = None
            if research_deadline is not None:
                remaining_research_seconds = research_deadline - time.monotonic()
                if remaining_research_seconds <= 0:
                    research_budget_exhausted = True
                    break
                planner_timeout_override = min(
                    self._role_timeout_seconds("planner", context),
                    remaining_research_seconds,
                )
            planner = await self._run_unit(
                role="planner",
                question=planner_prompt,
                request=request,
                context=context,
                tool_access=False,
                event_sink=event_sink,
                progress_title="规划智能体仍在分析",
                task_id=(
                    f"{context.request_id}:research-planner:{attempt}"
                    if attempt > 1
                    else None
                ),
                timeout_override_seconds=planner_timeout_override,
            )
            planner_contract_error = ""
            try:
                tasks = self._parse_research_tasks(
                    planner.answer,
                    request.question,
                    context,
                )
            except ValueError as exc:
                # Unknown/ambiguous roles are an invalid structured plan. Give
                # the planner one bounded correction opportunity instead of
                # silently dispatching the task to an unrelated role.
                tasks = []
                planner_contract_error = str(exc)
            if tasks:
                break
            if attempt < attempt_limit:
                await self._emit_process_event(
                    event_sink,
                    make_process_event(
                        "status",
                        "正在校正研究计划格式",
                        "规划智能体未返回有效任务 JSON，正在进行一次有界格式重试。",
                        level="active",
                        metadata={
                            "runtime_type": "planner_structured_output_retry",
                            "attempt": attempt + 1,
                        },
                    ),
                )
                planner_prompt += (
                    "\n\n上一次响应无法解析为有效任务计划。只返回一个 JSON 对象；"
                    "tasks 必须是非空数组，每项必须包含非空 key、title、question 和合法 agent_type，"
                    "禁止 Markdown 或解释文字。"
                    + (
                        f" 上一次角色契约错误：{planner_contract_error}。"
                        if planner_contract_error
                        else ""
                    )
                )
        used_fallback = not tasks
        if used_fallback:
            tasks = self._fallback_research_tasks(request.question, context)
        research_brief = parse_research_brief(
            planner.answer,
            fallback=build_default_research_brief(request, context),
        )
        if self.name == "deep_research":
            # Evidence-gathering lanes fan out independently. The solution
            # role keeps its normalized dependencies so it cannot design a
            # plan before facts, data, evidence and risk handoffs are ready.
            tasks = [
                (
                    replace(task, dependencies=())
                    if task.agent_type != "solution_architect"
                    else task
                )
                for task in tasks
            ]
        # Preloading and specialization are bypassed for non-groundwater domains.
        await self._emit_event(
            event_sink,
            "plan.updated",
            {
                "workflow": self.name,
                "research_brief": research_brief.to_dict(),
                "tasks": [
                    {
                        "key": task.key,
                        "title": task.title,
                        "question": task.question,
                        "priority": task.priority,
                        "expected_tools": list(task.expected_tools),
                        "risk": task.risk,
                        "dependencies": list(task.dependencies),
                        "budget": task.budget,
                        "agent_type": task.agent_type,
                    }
                    for task in tasks
                ],
            },
        )
        await self._emit_process_event(
            event_sink,
            make_process_event(
                "status",
                "研究计划已生成",
                f"已拆分为 {len(tasks)} 个并行研究子任务。",
                level="success",
                metadata={
                    "runtime_type": "planner_completed",
                    "task_count": len(tasks),
                    "used_fallback": used_fallback,
                    "research_brief": research_brief.to_dict(),
                    "agent_types": [task.agent_type for task in tasks],
                    "workflow_route": dict(
                        context.request_metadata.get("_workflow_route") or {}
                    ),
                },
            ),
        )
        kb_circuit_open = asyncio.Event()
        scheduled_tasks = sorted(
            enumerate(tasks, start=1),
            key=lambda pair: (-pair[1].priority, pair[0]),
        )
        analysis_gate = _AdaptiveAnalysisGate(
            min_ready=min(2, len(scheduled_tasks)),
            timeout_seconds=self._runtime_budget.partial_handoff_grace_seconds,
        )
        await self._emit_process_event(
            event_sink,
            make_process_event(
                "status",
                "研究调度队列已建立",
                (
                    "Plan-Solve 将并发启动无依赖的专业研究分支，并按任务依赖逐波推进。"
                    if self.name == "plan_solve"
                    else "深度研究将同时启动所有证据分支，并在全部分支完成后统一汇总。"
                ),
                level="active",
                metadata={
                    "runtime_type": "scheduler_started",
                    "scheduler_state": "queued",
                    "task_order": [task.key for _index, task in scheduled_tasks],
                    "resource": {
                        "kb_capacity": 1,
                        "llm_capacity": self._research_llm_capacity(),
                    },
                },
            ),
        )

        branch_results = await self._run_scheduled_research_tasks(
            scheduled_tasks=scheduled_tasks,
            used_fallback=used_fallback,
            kb_circuit_open=kb_circuit_open,
            request=request,
            context=context,
            event_sink=event_sink,
            analysis_gate=analysis_gate,
            research_deadline=research_deadline,
        )
        research_budget_exhausted = research_budget_exhausted or any(
            result.timed_out and result.error == _RESEARCH_PHASE_TIMEOUT_ERROR
            for result in branch_results
        )
        reflection_results: list[CoordinationUnitResult] = []
        if self.name == "deep_research":
            tasks, branch_results, reflection_results = (
                await self._run_research_reflection(
                    request=request,
                    context=context,
                    planner=planner,
                    tasks=tasks,
                    branch_results=branch_results,
                    kb_circuit_open=kb_circuit_open,
                    event_sink=event_sink,
                    research_deadline=research_deadline,
                )
            )
            research_budget_exhausted = research_budget_exhausted or any(
                result.timed_out and result.error == _RESEARCH_PHASE_TIMEOUT_ERROR
                for result in reflection_results
            )
        research_budget_exhausted = research_budget_exhausted or bool(
            research_deadline is not None and time.monotonic() >= research_deadline
        )
        if research_budget_exhausted:
            await self._emit_process_event(
                event_sink,
                make_process_event(
                    "status",
                    "研究阶段时限已到，正在强制汇总",
                    "已停止新的检索和补研；writer 将使用当前证据矩阵生成最终报告。",
                    level="warning",
                    metadata={
                        "runtime_type": "research_phase_timed_out",
                        "research_timeout_seconds": (
                            self._runtime_budget.deep_research_research_phase_timeout_seconds
                        ),
                        "writer_reserved_seconds": (
                            self._runtime_budget.deep_research_writer_timeout_seconds
                        ),
                        "completed_branches": sum(
                            1 for result in branch_results if result.answer.strip()
                        ),
                        "planned_branches": len(tasks),
                    },
                ),
            )
        writer = await self._host._build_research_report(
            request=request,
            context=context,
            planner=planner,
            tasks=tasks,
            branch_results=branch_results,
            event_sink=event_sink,
            research_budget_exhausted=research_budget_exhausted,
        )
        return [planner, *reflection_results, *branch_results, writer], tasks

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
        """Run bounded supervisor reviews and research only material evidence gaps."""

        reflection_results: list[CoordinationUnitResult] = []
        rounds = self._runtime_budget.deep_research_reflection_rounds
        followup_limit = self._runtime_budget.deep_research_followup_tasks
        research_brief = parse_research_brief(
            planner.answer,
            fallback=build_default_research_brief(request, context),
        )
        previous_source_ids: frozenset[str] = frozenset()
        for round_index in range(1, rounds + 1):
            reflection_remaining_seconds = (
                research_deadline - time.monotonic()
                if research_deadline is not None
                else None
            )
            if research_deadline is not None and (
                reflection_remaining_seconds is None
                or reflection_remaining_seconds
                < self._minimum_research_branch_start_seconds() * 2
            ):
                await self._emit_process_event(
                    event_sink,
                    make_process_event(
                        "status",
                        "剩余研究时间不足以启动证据复核",
                        "已停止启动新的监督与补研单元；现有证据将立即交给独立预算内的 writer。",
                        level="warning",
                        metadata={
                            "runtime_type": "research_reflection_start_window_exhausted",
                            "round": round_index,
                            "remaining_seconds": max(
                                0.0, reflection_remaining_seconds or 0.0
                            ),
                            "minimum_start_seconds": self._minimum_research_branch_start_seconds()
                            * 2,
                        },
                    ),
                )
                break
            evidence_matrix = self._format_evidence_matrix(
                branch_results, max_chars=24000
            )
            citation_report = CitationVerifier().verify_handoffs(branch_results)
            coverage_ledger = build_coverage_ledger(
                tasks,
                branch_results,
                citation_report,
                previous_source_ids=previous_source_ids,
            )
            current_source_ids = collect_source_ids(branch_results)
            await self._emit_process_event(
                event_sink,
                make_process_event(
                    "status",
                    "正在检查研究证据缺口",
                    "监督智能体正在核对问题覆盖、来源相关性、冲突和未解决证据缺口。",
                    level="active",
                    metadata={
                        "runtime_type": "research_reflection_started",
                        "round": round_index,
                        "completed_branches": len(branch_results),
                        "coverage_ledger": coverage_ledger.to_dict(),
                    },
                ),
            )
            reflection_prompt = build_research_reflection_prompt(
                request,
                context,
                plan=planner.answer,
                evidence_matrix=evidence_matrix,
                citation_report=citation_report.to_dict(),
                research_brief=research_brief.to_dict(),
                coverage_ledger=coverage_ledger.to_dict(),
                max_tasks=followup_limit,
            )
            reflection = None
            reflection_result: CoordinationUnitResult | None = None
            attempt_limit = (
                self._runtime_budget.deep_research_structured_output_attempts
            )
            for attempt in range(1, attempt_limit + 1):
                reflection_timeout_override: float | None = None
                if research_deadline is not None:
                    remaining_research_seconds = research_deadline - time.monotonic()
                    supervisor_seconds = (
                        remaining_research_seconds
                        - self._minimum_research_branch_start_seconds()
                    )
                    if supervisor_seconds <= 0:
                        break
                    reflection_timeout_override = min(
                        self._role_timeout_seconds("planner", context),
                        supervisor_seconds,
                    )
                reflection_result = await self._run_unit(
                    role="planner",
                    question=reflection_prompt,
                    request=request,
                    context=context,
                    tool_access=False,
                    event_sink=event_sink,
                    progress_title="监督智能体仍在检查证据缺口",
                    task_id=(
                        f"{context.request_id}:research-reflection:"
                        f"{round_index}:{attempt}"
                    ),
                    timeout_override_seconds=reflection_timeout_override,
                )
                reflection_contract_error = ""
                try:
                    reflection = parse_research_reflection(
                        reflection_result.answer,
                        request.question,
                        context,
                        max_tasks=followup_limit,
                    )
                except ValueError as exc:
                    reflection = None
                    reflection_contract_error = str(exc)
                if reflection is not None:
                    break
                if attempt < attempt_limit:
                    await self._emit_process_event(
                        event_sink,
                        make_process_event(
                            "status",
                            "正在校正监督结果格式",
                            "监督智能体未返回可解析 JSON，正在进行一次有界格式重试。",
                            level="active",
                            metadata={
                                "runtime_type": "research_reflection_retry",
                                "round": round_index,
                                "attempt": attempt + 1,
                            },
                        ),
                    )
                    reflection_prompt += (
                        "\n\n上一次响应无法解析。只返回一个 JSON 对象；"
                        "complete 必须是布尔值，tasks 必须是数组，禁止 Markdown。"
                        + (
                            f" 上一次角色契约错误：{reflection_contract_error}。"
                            if reflection_contract_error
                            else ""
                        )
                    )
            if reflection_result is not None:
                # Structured retries are attempts of one supervisor unit, not
                # separate research branches. Surface only the final attempt
                # so a recovered format/runtime error cannot falsely degrade
                # an otherwise successful workflow.
                reflection_results.append(
                    replace(reflection_result, role="research_supervisor")
                )
            saturated = bool(
                round_index > 1
                and coverage_ledger.novel_source_ratio <= 0.1
                and reflection is not None
                and reflection.expected_information_gain < 0.35
            )
            # Evidence extraction collapse is more urgent than discretionary
            # breadth expansion. Reserve the bounded follow-up capacity for
            # branches that captured sources but failed to turn them into
            # source-bound claims before accepting supervisor-proposed work.
            quality_followups = self._prepare_quality_recovery_tasks(
                tasks=tasks,
                branch_results=branch_results,
                context=context,
                round_index=round_index,
                limit=0 if saturated else followup_limit,
            )
            remaining_followup_slots = max(0, followup_limit - len(quality_followups))
            proposed_followups = self._prepare_followup_tasks(
                reflection.tasks if reflection is not None and not saturated else (),
                existing_tasks=[*tasks, *quality_followups],
                context=context,
                round_index=round_index,
            )
            followup_tasks = [
                *quality_followups,
                *proposed_followups[:remaining_followup_slots],
            ]
            conflict_task = (
                self._prepare_conflict_resolution_task(
                    existing_tasks=tasks,
                    branch_results=branch_results,
                    context=context,
                    round_index=round_index,
                )
                if not saturated and len(followup_tasks) < followup_limit
                else None
            )
            if conflict_task is not None:
                followup_tasks.append(conflict_task)
            is_complete = bool(
                reflection is not None and reflection.complete and not followup_tasks
            )
            reason = (
                reflection.reason
                if reflection is not None
                else "监督结果不是有效结构化 JSON，不能据此判定证据完整。"
            )
            if saturated:
                reason = (
                    reason + "；" if reason else ""
                ) + "连续研究未带来足够新来源且预期信息增益较低，已停止继续扩展。"
            if quality_followups:
                reason = (
                    reason + "；" if reason else ""
                ) + f"质量门发现 {len(quality_followups)} 个失败或证据不足分支，已安排定向恢复。"
            await self._emit_process_event(
                event_sink,
                make_process_event(
                    "status",
                    (
                        "研究证据检查完成"
                        if is_complete or followup_tasks
                        else "研究证据检查异常"
                    ),
                    reason
                    or (
                        "现有证据足以进入写作。"
                        if is_complete
                        else "发现需要补研的证据缺口。"
                    ),
                    level=(
                        "success"
                        if is_complete
                        else "active" if followup_tasks else "warning"
                    ),
                    metadata={
                        "runtime_type": "research_reflection_completed",
                        "round": round_index,
                        "complete": is_complete,
                        "followup_task_count": len(followup_tasks),
                        "reason": reason,
                        "structured_output_valid": reflection is not None,
                        "citation_verification": citation_report.to_dict(),
                        "coverage_ledger": coverage_ledger.to_dict(),
                        "supervisor_decision": (
                            {
                                "coverage_score": reflection.coverage_score,
                                "unresolved_dimensions": list(
                                    reflection.unresolved_dimensions
                                ),
                                "source_diversity_score": (
                                    reflection.source_diversity_score
                                ),
                                "contradiction_count": (reflection.contradiction_count),
                                "expected_information_gain": (
                                    reflection.expected_information_gain
                                ),
                                "stop_reason": reflection.stop_reason,
                            }
                            if reflection is not None
                            else None
                        ),
                        "saturated": saturated,
                    },
                ),
            )
            if is_complete or not followup_tasks:
                break
            previous_source_ids = current_source_ids

            followup_remaining_seconds = (
                research_deadline - time.monotonic()
                if research_deadline is not None
                else None
            )
            if research_deadline is not None and (
                followup_remaining_seconds is None
                or followup_remaining_seconds
                < self._minimum_research_branch_start_seconds()
            ):
                followup_results = [
                    self._research_phase_timeout_result(
                        index=len(tasks) + offset,
                        task=task,
                        used_fallback=False,
                        context=context,
                    )
                    for offset, task in enumerate(followup_tasks, start=1)
                ]
                tasks.extend(followup_tasks)
                branch_results.extend(followup_results)
                await self._emit_process_event(
                    event_sink,
                    make_process_event(
                        "status",
                        "剩余研究时间不足以启动定向补研",
                        "已保留现有证据并停止开新分支，writer 将在独立预算内明确剩余证据缺口。",
                        level="warning",
                        metadata={
                            "runtime_type": "research_followup_start_window_exhausted",
                            "round": round_index,
                            "remaining_seconds": max(
                                0.0, followup_remaining_seconds or 0.0
                            ),
                            "minimum_start_seconds": self._minimum_research_branch_start_seconds(),
                            "skipped_task_count": len(followup_tasks),
                        },
                    ),
                )
                break

            await self._emit_event(
                event_sink,
                "plan.updated",
                {
                    "workflow": self.name,
                    "phase": "evidence_gap_followup",
                    "tasks": [
                        {
                            "key": task.key,
                            "title": task.title,
                            "question": task.question,
                            "priority": task.priority,
                            "expected_tools": list(task.expected_tools),
                            "risk": task.risk,
                            "dependencies": list(task.dependencies),
                            "budget": task.budget,
                            "agent_type": task.agent_type,
                        }
                        for task in followup_tasks
                    ],
                },
            )
            await self._emit_process_event(
                event_sink,
                make_process_event(
                    "status",
                    "正在补充关键证据",
                    f"已根据证据缺口启动 {len(followup_tasks)} 个定向补研分支。",
                    level="active",
                    metadata={
                        "runtime_type": "research_followup_started",
                        "round": round_index,
                        "task_order": [task.key for task in followup_tasks],
                    },
                ),
            )
            prior_results = list(branch_results)
            followup_futures = [
                asyncio.create_task(
                    self._run_research_task(
                        task=task,
                        index=len(tasks) + offset,
                        role=self._research_role(
                            task=task,
                            index=len(tasks) + offset,
                            used_fallback=False,
                            context=context,
                        ),
                        kb_circuit_open=kb_circuit_open,
                        request=request,
                        context=context,
                        event_sink=event_sink,
                        research_deadline=research_deadline,
                        dependency_results=prior_results,
                    )
                )
                for offset, task in enumerate(followup_tasks, start=1)
            ]
            for future in followup_futures:
                _cancel_child_on_parent_exit(future)
            followup_phase_timed_out = False
            if research_deadline is None:
                # 生产路径反思仅在 deep_research 下运行且必有截止时间;此分支
                # 服务直接调用门面的调用方。与下方 deadline 分支同一纪律:
                # 单个补研分支逃逸的异常/取消不得中止其余分支并丢弃其证据。
                gathered_followups = await asyncio.gather(
                    *followup_futures,
                    return_exceptions=True,
                )
                followup_results = []
                for offset, (task, outcome) in enumerate(
                    zip(followup_tasks, gathered_followups, strict=True),
                    start=1,
                ):
                    if isinstance(outcome, asyncio.CancelledError):
                        followup_results.append(
                            self._research_phase_timeout_result(
                                index=len(tasks) + offset,
                                task=task,
                                used_fallback=False,
                                context=context,
                            )
                        )
                    elif isinstance(outcome, BaseException):
                        followup_results.append(
                            CoordinationUnitResult(
                                role=self._research_role(
                                    task=task,
                                    index=len(tasks) + offset,
                                    used_fallback=False,
                                    context=context,
                                ),
                                answer="",
                                error=str(outcome) or type(outcome).__name__,
                                task_key=task.key,
                                task_title=task.title,
                            )
                        )
                    else:
                        followup_results.append(outcome)
            else:
                remaining_research_seconds = max(
                    0.0,
                    research_deadline - time.monotonic(),
                )
                _done_futures, pending_futures = await asyncio.wait(
                    followup_futures,
                    timeout=remaining_research_seconds,
                )
                followup_phase_timed_out = bool(pending_futures)
                if pending_futures:
                    await asyncio.gather(
                        *(_cancel_task_bounded(future) for future in pending_futures)
                    )
                followup_results = []
                for offset, (task, future) in enumerate(
                    zip(followup_tasks, followup_futures, strict=True),
                    start=1,
                ):
                    if future in pending_futures or future.cancelled():
                        followup_results.append(
                            self._research_phase_timeout_result(
                                index=len(tasks) + offset,
                                task=task,
                                used_fallback=False,
                                context=context,
                            )
                        )
                        continue
                    try:
                        followup_results.append(future.result())
                    except asyncio.CancelledError:
                        followup_results.append(
                            self._research_phase_timeout_result(
                                index=len(tasks) + offset,
                                task=task,
                                used_fallback=False,
                                context=context,
                            )
                        )
                    except Exception as exc:  # noqa: BLE001
                        followup_results.append(
                            CoordinationUnitResult(
                                role=self._research_role(
                                    task=task,
                                    index=len(tasks) + offset,
                                    used_fallback=False,
                                    context=context,
                                ),
                                answer="",
                                error=str(exc) or type(exc).__name__,
                                task_key=task.key,
                                task_title=task.title,
                            )
                        )
            tasks.extend(followup_tasks)
            branch_results.extend(followup_results)
            await self._emit_process_event(
                event_sink,
                make_process_event(
                    "status",
                    (
                        "定向补研达到研究时限"
                        if followup_phase_timed_out
                        else "定向补研已完成"
                    ),
                    (
                        "未完成分支已停止；现有补研结果已并入证据矩阵，将立即进入最终写作。"
                        if followup_phase_timed_out
                        else "补研结果已并入证据矩阵，将进入下一轮检查或最终写作。"
                    ),
                    level="warning" if followup_phase_timed_out else "success",
                    metadata={
                        "runtime_type": "research_followup_completed",
                        "round": round_index,
                        "completed_branches": len(followup_results),
                        "research_phase_timed_out": followup_phase_timed_out,
                    },
                ),
            )
            if followup_phase_timed_out:
                break
        return tasks, branch_results, reflection_results

    def _research_phase_timeout_result(
        self,
        *,
        index: int,
        task: ResearchTask,
        used_fallback: bool,
        context: AgentRuntimeContext,
    ) -> CoordinationUnitResult:
        return CoordinationUnitResult(
            role=self._research_role(
                task=task,
                index=index,
                used_fallback=used_fallback,
                context=context,
            ),
            answer="",
            error=_RESEARCH_PHASE_TIMEOUT_ERROR,
            task_key=task.key,
            task_title=task.title,
            timed_out=True,
        )
