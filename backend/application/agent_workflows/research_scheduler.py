"""Dependency-aware scheduling for coordinated research branches."""

from __future__ import annotations

import asyncio
import re
import time
from collections.abc import Awaitable, Callable
from dataclasses import replace

from backend.application.agent_executors.stream_utils import make_process_event
from backend.application.agent_workflows import coordinated_agent_workflow as _legacy
from backend.application.agent_workflows.adaptive_scheduler import select_next_wave
from backend.application.agent_workflows.coordination_component import (
    CoordinationComponent,
)
from backend.application.agent_workflows.coordination_types import (
    CoordinationUnitResult,
    ResearchTask,
)
from backend.application.agent_workflows.evidence_verifier import normalize_confidence
from backend.application.agent_workflows.evidence_quality import (
    build_claim_source_graph,
)
from backend.domain.agents.request import AgentRequest
from backend.domain.agents.runtime_context import AgentRuntimeContext

WorkflowEventSink = _legacy.WorkflowEventSink
_AdaptiveAnalysisGate = _legacy._AdaptiveAnalysisGate
_cancel_child_on_parent_exit = _legacy._cancel_child_on_parent_exit
_cancel_task_bounded = _legacy._cancel_task_bounded


def _query_features(value: str) -> frozenset[str]:
    """Build a language-agnostic signature for semantic-near duplicate tasks."""

    normalized = re.sub(r"\s+", "", str(value or "").casefold())
    cjk = "".join(re.findall(r"[\u3400-\u9fff]", normalized))
    cjk = cjk.replace("对应", "")
    cjk = re.sub(r"[的了和与及请]", "", cjk)
    cjk_bigrams = {cjk[index : index + 2] for index in range(max(0, len(cjk) - 1))}
    words = set(re.findall(r"[a-z0-9][a-z0-9_-]+", normalized))
    return frozenset({*cjk_bigrams, *words})


def _queries_overlap(left: str, right: str) -> bool:
    left_features = _query_features(left)
    right_features = _query_features(right)
    if not left_features or not right_features:
        return "".join(left.casefold().split()) == "".join(right.casefold().split())
    intersection = len(left_features & right_features)
    containment = intersection / max(1, min(len(left_features), len(right_features)))
    return containment >= 0.82


class ResearchScheduler(CoordinationComponent):
    @staticmethod
    def _prepare_followup_tasks(
        proposed_tasks: tuple[ResearchTask, ...],
        *,
        existing_tasks: list[ResearchTask],
        context: AgentRuntimeContext,
        round_index: int,
    ) -> list[ResearchTask]:
        """Normalize, deduplicate and source-scope supervisor follow-up tasks."""

        seen = [task.question or task.title for task in existing_tasks]
        prepared: list[ResearchTask] = []
        for task in proposed_tasks:
            candidate = task.question or task.title
            if not candidate or any(_queries_overlap(candidate, item) for item in seen):
                continue
            seen.append(candidate)
            expected_tools = tuple(dict.fromkeys(task.expected_tools))
            if not expected_tools:
                expected_tools = tuple(
                    tool_name
                    for enabled, tool_name in (
                        (bool(context.kb_ids), "multi_kb_search"),
                        (context.use_search, "web_search"),
                    )
                    if enabled
                )
            prepared.append(
                replace(
                    task,
                    key=f"followup_{round_index}_{len(prepared) + 1}",
                    dependencies=(),
                    expected_tools=expected_tools,
                )
            )
        return prepared

    @staticmethod
    def _prepare_quality_recovery_tasks(
        *,
        tasks: list[ResearchTask],
        branch_results: list[CoordinationUnitResult],
        context: AgentRuntimeContext,
        round_index: int,
        limit: int,
    ) -> list[ResearchTask]:
        """Create bounded recovery work for failed or uncited research lanes."""

        if limit <= 0:
            return []
        result_by_key = {
            result.task_key: result for result in branch_results if result.task_key
        }
        existing_titles = {task.title for task in tasks}
        prepared: list[ResearchTask] = []
        for task in tasks:
            if task.title.startswith("证据恢复："):
                continue
            result = result_by_key.get(task.key)
            if result is None:
                issue = "原分支没有返回结果"
            elif result.error:
                issue = f"原分支执行异常：{result.error}"
            else:
                handoff = result.handoff or {}
                claims = handoff.get("claims") or []
                cited = sum(1 for claim in claims if claim.get("supporting_refs"))
                uncited = sum(1 for claim in claims if not claim.get("supporting_refs"))
                low_confidence = sum(
                    1
                    for claim in claims
                    if normalize_confidence(
                        claim.get("confidence") or handoff.get("confidence")
                    )
                    < 0.5
                )
                captured_refs = len(
                    {
                        str(ref).strip()
                        for ref in (
                            result.captured_evidence_refs
                            or tuple(handoff.get("evidence_refs") or [])
                        )
                        if str(ref).strip()
                    }
                )
                missing_blob = " ".join(
                    str(item) for item in handoff.get("missing") or []
                )
                dropped_hint = "未绑定到实际捕获来源" in missing_blob
                if not claims:
                    issue = "原分支没有形成可核验结论"
                elif uncited:
                    issue = f"原分支有 {uncited} 条结论缺少来源"
                elif dropped_hint and cited < 3:
                    issue = (
                        f"原分支有候选结论因来源绑定失败被排除，"
                        f"当前仅剩 {cited} 条可引用结论"
                    )
                elif cited <= 1 and captured_refs >= 3:
                    issue = (
                        f"原分支捕获了 {captured_refs} 个来源但仅形成 "
                        f"{cited} 条绑定结论，覆盖不足"
                    )
                elif low_confidence:
                    issue = f"原分支有 {low_confidence} 条低置信度结论"
                else:
                    continue
            recovery_title = f"证据恢复：{task.title}"
            if recovery_title in existing_titles:
                continue
            expected_tools = tuple(dict.fromkeys(task.expected_tools)) or tuple(
                tool_name
                for enabled, tool_name in (
                    (bool(context.kb_ids), "multi_kb_search"),
                    (context.use_search, "web_search"),
                )
                if enabled
            )
            prepared.append(
                ResearchTask(
                    key=f"quality_{round_index}_{len(prepared) + 1}",
                    title=recovery_title,
                    question=(
                        f"针对子问题“{task.question}”进行一次定向证据恢复。{issue}。"
                        "只补充原分支缺失的关键事实和可追溯来源；优先使用权威、直接相关来源，"
                        "形成带 supporting_refs 的简洁结论，避免重复背景材料。"
                    ),
                    priority=max(90, task.priority),
                    expected_tools=expected_tools,
                    risk=task.risk,
                    dependencies=(),
                    budget=max(
                        task.budget,
                        (
                            2
                            if task.agent_type
                            in {"evidence_researcher", "risk_reviewer"}
                            else 1
                        ),
                    ),
                    agent_type=task.agent_type,
                )
            )
            existing_titles.add(recovery_title)
            if len(prepared) >= limit:
                break
        return prepared

    @staticmethod
    def _prepare_conflict_resolution_task(
        *,
        existing_tasks: list[ResearchTask],
        branch_results: list[CoordinationUnitResult],
        context: AgentRuntimeContext,
        round_index: int,
    ) -> ResearchTask | None:
        """Create one bounded adjudication lane for deterministic conflicts."""

        title = "冲突核验与口径统一"
        if any(task.title == title for task in existing_tasks):
            return None
        quality = build_claim_source_graph(branch_results)
        if not quality.conflicts:
            return None
        claims_by_id = {claim.claim_id: claim.claim for claim in quality.claims}
        conflict_lines = [
            (
                f"{claims_by_id.get(conflict.left_claim_id, conflict.left_claim_id)}"
                f" <-> {claims_by_id.get(conflict.right_claim_id, conflict.right_claim_id)}"
                f"（{conflict.reason}）"
            )
            for conflict in quality.conflicts[:3]
        ]
        expected_tools = tuple(
            tool_name
            for enabled, tool_name in (
                (bool(context.kb_ids), "multi_kb_search"),
                (context.use_search, "web_search"),
            )
            if enabled
        )
        return ResearchTask(
            key=f"conflict_{round_index}",
            title=title,
            question=(
                "以下已捕获结论存在数值或否定关系冲突，请核对原始来源、版本、时间、"
                "测量条件和适用范围，判断它们是真实矛盾还是口径差异。优先补充权威、"
                "直接相关的第三方来源；不能解决时必须并列保留并明确不确定性。\n- "
                + "\n- ".join(conflict_lines)
            ),
            priority=100,
            expected_tools=expected_tools,
            risk="high",
            dependencies=(),
            budget=2,
            agent_type="risk_reviewer",
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
        known_keys = {task.key for _index, task in scheduled_tasks}
        terminal_keys: set[str] = set()
        remaining = list(scheduled_tasks)
        branch_results: list[CoordinationUnitResult] = []
        partial_notified = False

        while remaining:
            remaining_phase_seconds = (
                research_deadline - time.monotonic()
                if research_deadline is not None
                else None
            )
            if research_deadline is not None and (
                remaining_phase_seconds is None
                or remaining_phase_seconds
                < self._minimum_research_branch_start_seconds()
            ):
                branch_results.extend(
                    self._research_phase_timeout_result(
                        index=index,
                        task=task,
                        used_fallback=used_fallback,
                        context=context,
                    )
                    for index, task in remaining
                )
                await self._emit_process_event(
                    event_sink,
                    make_process_event(
                        "status",
                        "研究阶段剩余时间不足以启动新分支",
                        "为避免在截止前启动必然无法完成检索和证据交接的任务，"
                        "调度器已停止开新分支并保留现有证据供 writer 使用。",
                        level="warning",
                        metadata={
                            "runtime_type": "research_phase_start_window_exhausted",
                            "remaining_seconds": max(
                                0.0, remaining_phase_seconds or 0.0
                            ),
                            "minimum_start_seconds": self._minimum_research_branch_start_seconds(),
                            "skipped_task_count": len(remaining),
                        },
                    ),
                )
                break
            if partial_result_limit is not None and not partial_notified:
                wave_size = max(
                    1,
                    min(
                        self._research_llm_capacity(),
                        partial_result_limit - len(branch_results),
                    ),
                )
                effective_known_keys = known_keys
                effective_terminal_keys = terminal_keys
            elif partial_notified and partial_result_sink is not None:
                # Reserve one global LLM slot so the partial writer cannot be
                # starved by a newly scheduled research wave.
                wave_size = max(
                    1,
                    self._research_llm_capacity() - 1,
                )
                effective_known_keys = known_keys
                effective_terminal_keys = terminal_keys
            else:
                wave_size = self._research_llm_capacity()
                effective_known_keys = known_keys
                effective_terminal_keys = terminal_keys
            decision = select_next_wave(
                remaining,
                known_keys=effective_known_keys,
                terminal_keys=effective_terminal_keys,
                capacity=wave_size,
            )
            wave = [(index, task) for index, task in decision.wave]
            if decision.dependency_fallback:
                skipped_results = [
                    CoordinationUnitResult(
                        role=self._research_role(
                            task=task,
                            index=index,
                            used_fallback=used_fallback,
                            context=context,
                        ),
                        answer="",
                        error="研究任务依赖图无法推进，已跳过该分支；请检查任务间是否存在循环依赖。",
                        task_key=task.key,
                        task_title=task.title,
                    )
                    for index, task in remaining
                ]
                branch_results.extend(skipped_results)
                await self._emit_process_event(
                    event_sink,
                    make_process_event(
                        "status",
                        "研究任务依赖图无法推进",
                        "剩余任务之间无法继续拓扑调度，可能存在循环依赖；上游分支失败本身不会再阻止后续汇总任务运行。",
                        level="warning",
                        metadata={
                            "runtime_type": "scheduler_dependency_fallback",
                            "scheduler_state": "dependency_fallback",
                            "task_order": [task.key for _index, task in remaining],
                        },
                    ),
                )
                break
            await self._emit_process_event(
                event_sink,
                make_process_event(
                    "status",
                    "研究任务开始执行",
                    (
                        "Plan-Solve 正在启动当前依赖已满足的专业研究分支。"
                        if self.name == "plan_solve"
                        else "深度研究正在同时启动所有独立证据分支。"
                    ),
                    level="active",
                    metadata={
                        "runtime_type": "scheduler_wave_started",
                        "scheduler_state": "running",
                        "task_order": [task.key for _index, task in wave],
                        "resource": {
                            "kb_capacity": 1,
                            "llm_capacity": self._research_llm_capacity(),
                        },
                    },
                ),
            )
            wave_futures: list[
                tuple[int, ResearchTask, asyncio.Task[CoordinationUnitResult]]
            ] = [
                (
                    index,
                    task,
                    asyncio.create_task(
                        self._run_research_task(
                            task=task,
                            index=index,
                            role=self._research_role(
                                task=task,
                                index=index,
                                used_fallback=used_fallback,
                                context=context,
                            ),
                            kb_circuit_open=kb_circuit_open,
                            request=request,
                            context=context,
                            event_sink=event_sink,
                            analysis_gate=analysis_gate,
                            research_deadline=research_deadline,
                            dependency_results=[
                                result
                                for result in branch_results
                                if result.task_key in task.dependencies
                            ],
                        )
                    ),
                )
                for index, task in wave
            ]
            for _index, _task, future in wave_futures:
                _cancel_child_on_parent_exit(future)
            if research_deadline is None:
                # Isolate per branch: one branch raising an unexpected error
                # (or being cancelled) must not abort the whole wave and
                # discard already-completed siblings' evidence. This mirrors
                # the deadline path below; managed failures/timeouts are
                # already normalized into results by the branch runner.
                gathered = await asyncio.gather(
                    *(future for _index, _task, future in wave_futures),
                    return_exceptions=True,
                )
                raw_results = []
                for (index, task, _future), outcome in zip(
                    wave_futures, gathered, strict=True
                ):
                    if isinstance(outcome, asyncio.CancelledError):
                        raw_results.append(
                            self._research_phase_timeout_result(
                                index=index,
                                task=task,
                                used_fallback=used_fallback,
                                context=context,
                            )
                        )
                    elif isinstance(outcome, BaseException):
                        raw_results.append(
                            CoordinationUnitResult(
                                role=self._research_role(
                                    task=task,
                                    index=index,
                                    used_fallback=used_fallback,
                                    context=context,
                                ),
                                answer="",
                                error=str(outcome) or type(outcome).__name__,
                                task_key=task.key,
                                task_title=task.title,
                            )
                        )
                    else:
                        raw_results.append(outcome)
            else:
                remaining_research_seconds = max(
                    0.0,
                    research_deadline - time.monotonic(),
                )
                done_futures, pending_futures = await asyncio.wait(
                    [future for _index, _task, future in wave_futures],
                    timeout=remaining_research_seconds,
                )
                if pending_futures:
                    await asyncio.gather(
                        *(_cancel_task_bounded(future) for future in pending_futures)
                    )
                    await self._emit_process_event(
                        event_sink,
                        make_process_event(
                            "status",
                            "研究阶段预算已耗尽",
                            "已取消仍在运行的研究分支，并保留此前完成的证据。",
                            level="warning",
                            metadata={
                                "runtime_type": "research_phase_wave_timed_out",
                                "completed_in_wave": len(done_futures),
                                "cancelled_in_wave": len(pending_futures),
                            },
                        ),
                    )
                raw_results = []
                for index, task, future in wave_futures:
                    if future in pending_futures or future.cancelled():
                        raw_results.append(
                            self._research_phase_timeout_result(
                                index=index,
                                task=task,
                                used_fallback=used_fallback,
                                context=context,
                            )
                        )
                        continue
                    try:
                        raw_results.append(future.result())
                    except asyncio.CancelledError:
                        raw_results.append(
                            self._research_phase_timeout_result(
                                index=index,
                                task=task,
                                used_fallback=used_fallback,
                                context=context,
                            )
                        )
                    except Exception as exc:  # noqa: BLE001
                        raw_results.append(
                            CoordinationUnitResult(
                                role=self._research_role(
                                    task=task,
                                    index=index,
                                    used_fallback=used_fallback,
                                    context=context,
                                ),
                                answer="",
                                error=str(exc) or type(exc).__name__,
                                task_key=task.key,
                                task_title=task.title,
                            )
                        )
            results = [
                replace(
                    result,
                    task_key=result.task_key or task.key,
                    task_title=result.task_title or task.title,
                )
                for (_index, task, _future), result in zip(
                    wave_futures,
                    raw_results,
                    strict=True,
                )
            ]
            branch_results.extend(results)
            completed_wave_ids = {id(task) for _index, task in wave}
            # A dependency controls ordering, not truth.  Once an upstream
            # task reaches any terminal state, dependent synthesis must run
            # with the successful subset and an explicit list of evidence
            # gaps instead of being skipped wholesale.
            terminal_keys.update(task.key for _index, task in wave)
            wave_ids = completed_wave_ids
            remaining = [
                (index, task) for index, task in remaining if id(task) not in wave_ids
            ]
            if (
                partial_result_limit is not None
                and len(branch_results) >= partial_result_limit
                and remaining
                and not partial_notified
            ):
                partial_notified = True
                await self._emit_process_event(
                    event_sink,
                    make_process_event(
                        "status",
                        "高优研究分支已完成",
                        "阶段性结论已经可用；调度器将继续执行剩余分支，避免最终报告遗漏证据。",
                        level="success",
                        metadata={
                            "runtime_type": "scheduler_partial_ready",
                            "scheduler_state": "partial_ready",
                            "partial_handoff": True,
                            "completed_branches": len(branch_results),
                            "remaining_branches": len(remaining),
                        },
                    ),
                )
                if partial_result_sink is not None:
                    await partial_result_sink(list(branch_results))

        return branch_results
