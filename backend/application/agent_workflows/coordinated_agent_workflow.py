"""Executable plan/solve and coordinated multi-agent workflows."""

from __future__ import annotations

import asyncio
import json
import re
from collections.abc import AsyncIterator, Awaitable, Callable
from contextlib import suppress
from dataclasses import dataclass, field, replace
from typing import Any

from langchain_core.runnables import RunnableConfig
from langgraph.checkpoint.base import BaseCheckpointSaver

from backend.application.agent_executors.stream_utils import (
    extract_web_search_metadata,
    make_process_event,
)
from backend.application.agent_workflows.standard_agent_workflow import (
    StandardAgentWorkflow,
)
from backend.application.artifacts.artifact_utils import (
    build_registered_artifact_metadata,
)
from backend.application.blocking import run_blocking
from backend.application.coordination import (
    CoordinationStrategy,
    DefaultCoordinationStrategy,
)
from backend.application.tools.providers.async_web_search_tool import execute_web_search
from backend.application.tools.providers.deep_research_report_tool import (
    save_deep_research_report_direct,
)
from backend.application.tools.providers.multi_kb_search_tool import (
    execute_multi_kb_search,
)
from backend.domain.agents.events import AgentEvent
from backend.domain.agents.request import AgentRequest
from backend.domain.agents.runtime_context import AgentRuntimeContext


@dataclass(frozen=True)
class ResearchTask:
    key: str
    title: str
    question: str


@dataclass(frozen=True)
class CoordinationUnitResult:
    role: str
    answer: str
    error: str | None = None
    process_events: tuple[dict[str, Any], ...] = field(default_factory=tuple)


WorkflowEventSink = Callable[[tuple[str, dict[str, Any]]], Awaitable[None]]


class _AsyncTaskBarrier:
    def __init__(self, parties: int):
        self._parties = max(1, parties)
        self._arrived = 0
        self._lock = asyncio.Lock()
        self._ready = asyncio.Event()

    async def wait(self) -> None:
        async with self._lock:
            self._arrived += 1
            if self._arrived >= self._parties:
                self._ready.set()
        await self._ready.wait()


class CoordinatedAgentWorkflow:
    """Coordinate isolated agent units with deterministic evidence collection."""

    def __init__(
        self,
        *,
        name: str,
        checkpointer: BaseCheckpointSaver | None = None,
        runnable_config: RunnableConfig | None = None,
        strategy: CoordinationStrategy | None = None,
        delegate: StandardAgentWorkflow | None = None,
    ):
        self.name = name
        self._strategy = strategy or DefaultCoordinationStrategy(name)
        self._delegate = delegate or StandardAgentWorkflow(
            name="full",
            checkpointer=checkpointer,
            runnable_config=runnable_config,
        )
        self._runnable_config = runnable_config
        # The RAG provider rejects overlapping multi_kb_search calls in one request.
        # Queue only that I/O step while keeping web search and agent analysis concurrent.
        self._kb_search_lock = asyncio.Lock()

    async def run(
        self,
        request: AgentRequest,
        context: AgentRuntimeContext,
    ) -> AsyncIterator[AgentEvent]:
        sequence = 0
        plan = self._strategy.build_plan(request, context)
        yield self._event(
            context,
            sequence,
            "workflow.started",
            {
                "workflow": self.name,
                "agent_code": context.agent_profile.code,
                "coordination_mode": self.name,
            },
        )

        sequence += 1
        yield self._event(
            context,
            sequence,
            "plan.created",
            {
                "workflow": self.name,
                "summary": plan.summary,
                "stages": [
                    {
                        "key": stage.key,
                        "title": stage.title,
                        "description": stage.description,
                    }
                    for stage in plan.stages
                ],
                "metadata": dict(plan.metadata),
            },
        )

        research_tasks: list[ResearchTask] = []
        streamed_process_events = False
        try:
            if self.name in {"plan_solve", "deep_research"}:
                streamed_process_events = True
                event_queue: asyncio.Queue[tuple[str, dict[str, Any]] | None] = asyncio.Queue()
                outcome: dict[str, Any] = {}

                async def execute_plan_solve() -> None:
                    try:
                        outcome["value"] = await self._run_plan_solve(
                            request,
                            context,
                            event_sink=event_queue.put,
                        )
                    except BaseException as exc:  # noqa: BLE001
                        outcome["error"] = exc
                    finally:
                        await event_queue.put(None)

                execution_task = asyncio.create_task(execute_plan_solve())
                try:
                    while True:
                        queued_event = await event_queue.get()
                        if queued_event is None:
                            break
                        event_type, payload = queued_event
                        sequence += 1
                        yield self._event(context, sequence, event_type, payload)
                    await execution_task
                finally:
                    if not execution_task.done():
                        execution_task.cancel()
                        with suppress(asyncio.CancelledError):
                            await execution_task
                if "error" in outcome:
                    raise outcome["error"]
                results, research_tasks = outcome["value"]
            elif self.name == "multi_agent_parallel":
                results = await self._run_parallel(request, context)
            elif self.name == "multi_agent_alternating":
                results = await self._run_alternating(request, context)
            else:
                raise ValueError(f"Unsupported coordinated workflow: {self.name}")
        except Exception as exc:  # noqa: BLE001
            sequence += 1
            yield self._event(
                context,
                sequence,
                "error",
                {"message": str(exc), "error": str(exc), "workflow": self.name},
            )
            sequence += 1
            yield self._event(context, sequence, "done", {})
            return

        if research_tasks and not streamed_process_events:
            sequence += 1
            yield self._event(
                context,
                sequence,
                "plan.updated",
                {
                    "workflow": self.name,
                    "tasks": [
                        {"key": task.key, "title": task.title, "question": task.question}
                        for task in research_tasks
                    ],
                },
            )

        for result in results:
            if not streamed_process_events:
                for process_event in result.process_events:
                    sequence += 1
                    yield self._event(
                        context,
                        sequence,
                        "workflow.step",
                        {"event": process_event},
                    )
            sequence += 1
            event_type = "coordination.unit.failed" if result.error else "coordination.unit.completed"
            yield self._event(
                context,
                sequence,
                event_type,
                {
                    "workflow": self.name,
                    "role": result.role,
                    "error": result.error,
                    "answer_preview": result.answer[:300],
                },
            )

        answered = [result for result in results if result.answer]
        successful = [result for result in answered if not result.error]
        if not answered:
            sequence += 1
            message = "All coordination units failed"
            yield self._event(
                context,
                sequence,
                "error",
                {"message": message, "error": message, "workflow": self.name},
            )
        else:
            final_answer = results[-1].answer or answered[-1].answer
            sequence += 1
            yield self._event(context, sequence, "message.delta", {"content": final_answer})

            if self._should_generate_report(request):
                async for report_event in self._save_report(
                    request=request,
                    context=context,
                    content=final_answer,
                    sequence=sequence,
                ):
                    sequence = report_event.sequence
                    yield report_event

        sequence += 1
        yield self._event(
            context,
            sequence,
            "coordination.completed",
            {
                "workflow": self.name,
                "mode": self.name,
                "units": len(results),
                "successful_units": len(successful),
                "research_tasks": len(research_tasks),
                "kb_search_enabled": bool(context.kb_ids),
                "web_search_enabled": context.use_search,
            },
        )
        sequence += 1
        yield self._event(context, sequence, "done", {})

    async def _run_plan_solve(
        self,
        request: AgentRequest,
        context: AgentRuntimeContext,
        event_sink: WorkflowEventSink | None = None,
    ) -> tuple[list[CoordinationUnitResult], list[ResearchTask]]:
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
        planner = await self._run_unit(
            role="planner",
            question=self._planner_prompt(request, context),
            request=request,
            context=context,
            tool_access=False,
        )
        tasks = self._parse_research_tasks(planner.answer, request.question)
        used_fallback = not tasks
        if used_fallback:
            tasks = self._fallback_research_tasks(request.question)
        await self._emit_event(
            event_sink,
            "plan.updated",
            {
                "workflow": self.name,
                "tasks": [
                    {"key": task.key, "title": task.title, "question": task.question}
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
                },
            ),
        )
        research_barrier = _AsyncTaskBarrier(len(tasks))

        branch_results = list(
            await asyncio.gather(
                *[
                    self._run_research_task(
                        task=task,
                        index=index,
                        role=(
                            "solver"
                            if used_fallback and self.name == "plan_solve" and index == 1
                            else f"researcher_{index}"
                        ),
                        research_barrier=research_barrier,
                        request=request,
                        context=context,
                        event_sink=event_sink,
                    )
                    for index, task in enumerate(tasks, start=1)
                ]
            )
        )
        branch_evidence = "\n\n".join(
            f"## {task.title}\n{result.answer or result.error or '该分支未返回结果'}"
            for task, result in zip(tasks, branch_results, strict=True)
        )
        writer_role = "report_writer" if self.name == "deep_research" else "reviewer"
        await self._emit_process_event(
            event_sink,
            make_process_event(
                "status",
                "正在汇总研究结论",
                "答案输出智能体正在核对各分支证据、处理冲突并编写最终报告。",
                level="active",
                metadata={"runtime_type": "writer_started"},
            ),
        )
        writer = await self._run_unit(
            role=writer_role,
            question=(
                "你是答案输出智能体。基于规划和所有子智能体结果，解决冲突、标明不确定性，"
                "输出结构完整的最终 Markdown 报告。不得声称使用了未出现在证据中的来源；"
                "优先保留知识库文献名、网页标题和 URL 等可核验引用。\n\n"
                f"原始任务：\n{request.question}\n\n"
                f"规划结果：\n{planner.answer}\n\n"
                f"子智能体结果：\n{branch_evidence}"
            ),
            request=request,
            context=context,
            tool_access=False,
        )
        writer_fallback_used = not writer.answer
        if writer_fallback_used:
            writer = replace(
                writer,
                answer=self._build_fallback_report(
                    request=request,
                    planner=planner,
                    tasks=tasks,
                    branch_results=branch_results,
                ),
            )
        await self._emit_process_event(
            event_sink,
            make_process_event(
                "answer",
                "研究报告内容已完成",
                "已完成多分支证据汇总，正在生成可下载报告。",
                level="warning" if writer.error else "success",
                metadata={
                    "runtime_type": "writer_completed",
                    "fallback_used": writer_fallback_used,
                },
            ),
        )
        return [planner, *branch_results, writer], tasks

    async def _run_research_task(
        self,
        *,
        task: ResearchTask,
        index: int,
        role: str,
        research_barrier: _AsyncTaskBarrier,
        request: AgentRequest,
        context: AgentRuntimeContext,
        event_sink: WorkflowEventSink | None = None,
    ) -> CoordinationUnitResult:
        task_id = f"{context.request_id}:research:{index}"
        process_events: list[dict[str, Any]] = []

        async def record(process_event: dict[str, Any]) -> None:
            process_events.append(process_event)
            await self._emit_process_event(event_sink, process_event)

        await record(
            make_process_event(
                "task_start",
                task.title,
                task.question,
                tool="task",
                level="active",
                metadata={
                    "task_id": task_id,
                    "description": task.title,
                    "subagent_type": "researcher",
                    "runtime_type": "task_started",
                },
            )
        )
        evidence_parts: list[str] = []
        errors: list[str] = []
        web_task: asyncio.Task[str] | None = None

        if context.use_search:
            await record(
                make_process_event(
                    "tool_start",
                    "联网检索",
                    task.question,
                    tool="web_search",
                    level="active",
                    metadata={"task_id": task_id, "query": task.question},
                )
            )
            web_task = asyncio.create_task(execute_web_search(task.question, max_results=5))

        if context.kb_ids:
            await record(
                make_process_event(
                    "tool_start",
                    "检索已选知识库",
                    task.question,
                    tool="multi_kb_search",
                    level="active",
                    metadata={
                        "task_id": task_id,
                        "query": task.question,
                        "kb_ids": list(context.kb_ids),
                    },
                )
            )
            try:
                async with self._kb_search_lock:
                    kb_result = await execute_multi_kb_search(
                        query=task.question,
                        kb_ids=list(context.kb_ids),
                        top_k=8,
                        chunk_top_k=5,
                        merge_strategy="rrf",
                        max_result_chars=6500,
                    )
                evidence_parts.append("### 知识库证据\n" + kb_result)
                await record(
                    make_process_event(
                        "tool_result",
                        "知识库检索完成",
                        "已将选中知识库的召回结果交给子智能体。",
                        tool="multi_kb_search",
                        level="success",
                        metadata={
                            "task_id": task_id,
                            "query": task.question,
                            "kb_ids": list(context.kb_ids),
                        },
                    )
                )
            except Exception as exc:  # noqa: BLE001
                errors.append(f"知识库检索失败: {exc}")
                await record(
                    make_process_event(
                        "tool_result",
                        "知识库检索失败",
                        str(exc),
                        tool="multi_kb_search",
                        level="warning",
                        metadata={"task_id": task_id, "query": task.question},
                    )
                )

        if web_task is not None:
            try:
                web_result = await web_task
                evidence_parts.append("### 联网证据\n" + web_result)
                await record(
                    make_process_event(
                        "tool_result",
                        "联网检索完成",
                        "已将网页检索结果交给子智能体。",
                        tool="web_search",
                        level="success",
                        metadata={
                            "task_id": task_id,
                            **extract_web_search_metadata(web_result),
                        },
                    )
                )
            except Exception as exc:  # noqa: BLE001
                errors.append(f"联网检索失败: {exc}")
                await record(
                    make_process_event(
                        "tool_result",
                        "联网检索失败",
                        str(exc),
                        tool="web_search",
                        level="warning",
                        metadata={"task_id": task_id, "query": task.question},
                    )
                )

        seed_evidence = "\n\n".join(evidence_parts) or "当前未配置可用的知识库或联网检索。"
        # Start all answer subagents together after their planned evidence is ready.
        await research_barrier.wait()
        await record(
            make_process_event(
                "task_progress",
                "子智能体正在分析证据",
                "检索证据已准备完成，研究子智能体正在提取事实、出处和争议。",
                tool="task",
                level="active",
                metadata={
                    "task_id": task_id,
                    "description": task.title,
                    "subagent_type": "researcher",
                    "runtime_type": "task_running",
                },
            )
        )
        result = await self._run_unit(
            role=role,
            question=(
                "你是独立研究子智能体。请围绕分配的子问题分析下方检索证据，提取事实、"
                "数据、出处、冲突和证据缺口，形成可供最终报告直接引用的研究结论。"
                "若证据不足，可使用当前允许的工具继续核验，但不要编造引用。\n\n"
                f"原始任务：\n{request.question}\n\n"
                f"子问题：\n{task.question}\n\n"
                f"编排层预检索证据：\n{seed_evidence}"
            ),
            request=request,
            context=context,
            tool_access=True,
        )
        combined_error = "; ".join([*errors, *([result.error] if result.error else [])]) or None
        await record(
            make_process_event(
                "task_end",
                task.title,
                result.answer[:500] if result.answer else (combined_error or "子任务未返回内容"),
                tool="task",
                level="warning" if combined_error and not result.answer else "success",
                metadata={
                    "task_id": task_id,
                    "description": task.title,
                    "subagent_type": "researcher",
                    "runtime_type": "task_completed" if result.answer else "task_failed",
                    "search_queries": [task.question],
                },
            )
        )
        return replace(
            result,
            error=combined_error if not result.answer else result.error,
            process_events=tuple(process_events),
        )

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

    @staticmethod
    def _build_fallback_report(
        *,
        request: AgentRequest,
        planner: CoordinationUnitResult,
        tasks: list[ResearchTask],
        branch_results: list[CoordinationUnitResult],
    ) -> str:
        sections = [
            f"# 深度研究报告：{request.question.strip()}",
            "",
            "## 研究说明",
            "最终写作智能体未返回完整内容，以下报告由规划结果和各研究分支的有效证据自动汇编。",
        ]
        if planner.answer:
            sections.extend(["", "## 研究计划", planner.answer])
        for task, result in zip(tasks, branch_results, strict=True):
            sections.extend(
                [
                    "",
                    f"## {task.title}",
                    result.answer or f"该研究分支未返回有效内容。{result.error or ''}".strip(),
                ]
            )
        sections.extend(
            [
                "",
                "## 结论与证据缺口",
                "请结合上述分支结论交叉核验引用；未被检索证据支持的内容不应视为确定事实。",
            ]
        )
        return "\n".join(sections).strip()

    async def _run_parallel(
        self,
        request: AgentRequest,
        context: AgentRuntimeContext,
    ) -> list[CoordinationUnitResult]:
        roles = {
            "domain_analyst": "从领域知识、关键概念和技术约束角度独立分析。",
            "evidence_researcher": "从知识库、工具证据和事实核验角度独立分析。",
            "risk_reviewer": "从遗漏、风险、反例和授权边界角度独立分析。",
        }
        branch_results = list(
            await asyncio.gather(
                *[
                    self._run_unit(
                        role=role,
                        question=f"{instruction}\n\n原始任务：\n{request.question}",
                        request=request,
                        context=context,
                    )
                    for role, instruction in roles.items()
                ]
            )
        )
        evidence = "\n\n".join(
            f"[{item.role}]\n{item.answer or item.error or '无结果'}" for item in branch_results
        )
        synthesizer = await self._run_unit(
            role="synthesizer",
            question=(
                "你是并行结果聚合智能体。比较各分支结论，解决冲突，"
                "保留有证据支持的内容并直接输出最终答案。\n\n"
                f"原始任务：\n{request.question}\n\n分支结果：\n{evidence}"
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
            ("analyst", "提出初步分析、假设和证据需求。"),
            ("critic", "审查上一轮结果，指出错误、遗漏和未经证实的结论并修正。"),
            ("finalizer", "吸收前两轮结果，必要时补充工具验证，直接输出最终答案。"),
        ]
        results: list[CoordinationUnitResult] = []
        shared = ""
        for role, instruction in rounds:
            result = await self._run_unit(
                role=role,
                question=(
                    f"{instruction}\n\n原始任务：\n{request.question}"
                    + (f"\n\n已有协作结果：\n{shared}" if shared else "")
                ),
                request=request,
                context=context,
            )
            results.append(result)
            shared += f"\n\n[{role}]\n{result.answer or result.error or '无结果'}"
        return results

    async def _run_unit(
        self,
        *,
        role: str,
        question: str,
        request: AgentRequest,
        context: AgentRuntimeContext,
        tool_access: bool = True,
    ) -> CoordinationUnitResult:
        unit_request = replace(
            request,
            question=question,
            requested_workflow="full",
            subagent_enabled=False,
            metadata={
                **dict(request.metadata or {}),
                "coordination_workflow": self.name,
                "coordination_role": role,
            },
        )
        unit_context = replace(
            context,
            workflow_name="full",
            internal_thread_id=f"{context.internal_thread_id}:coord:{self.name}:{context.request_id}:{role}",
            subagent_enabled=False,
            use_search=context.use_search if tool_access else False,
            kb_ids=list(context.kb_ids) if tool_access else [],
            selected_skills=list(context.selected_skills) if tool_access else [],
        )
        answer_parts: list[str] = []
        errors: list[str] = []
        try:
            executor = await run_blocking(self._delegate.build_executor, unit_request, unit_context)
            async for raw_event in executor.aquery_stream(
                question,
                unit_context.internal_thread_id,
                self._runnable_config,
            ):
                event = self._normalize_event(raw_event, context)
                if event.type == "message.delta":
                    answer_parts.append(str(event.payload.get("content") or ""))
                elif event.type == "error":
                    errors.append(str(event.payload.get("message") or event.payload.get("error") or ""))
        except Exception as exc:  # noqa: BLE001
            errors.append(str(exc))
        return CoordinationUnitResult(
            role=role,
            answer="".join(answer_parts).strip(),
            error="; ".join(filter(None, errors)) or None,
        )

    def _planner_prompt(self, request: AgentRequest, context: AgentRuntimeContext) -> str:
        source_requirements = []
        if context.kb_ids:
            source_requirements.append(f"必须检索用户选中的 {len(context.kb_ids)} 个知识库")
        if context.use_search:
            source_requirements.append("必须进行联网检索以补充和核验时效性信息")
        if not source_requirements:
            source_requirements.append("当前没有外部检索源，只能规划基于模型知识的分析")
        return (
            "你是研究规划智能体。把任务拆成 2-4 个可并行、边界清晰、合并后能完整回答"
            "原问题的研究子任务。只输出 JSON，不要 Markdown。格式："
            '{"summary":"...","tasks":[{"key":"t1","title":"...","question":"..."}]}。'
            "每个 question 必须是可直接用于检索的完整查询。\n"
            f"检索约束：{'；'.join(source_requirements)}。\n\n原始任务：\n{request.question}"
        )

    def _parse_research_tasks(self, planner_answer: str, original_question: str) -> list[ResearchTask]:
        text = str(planner_answer or "").strip()
        if not text:
            return []
        fenced = re.search(r"```(?:json)?\s*([\s\S]*?)```", text, flags=re.IGNORECASE)
        candidate = fenced.group(1).strip() if fenced else text
        start = min([pos for pos in (candidate.find("{"), candidate.find("[")) if pos >= 0], default=-1)
        if start > 0:
            candidate = candidate[start:]
        try:
            parsed, _ = json.JSONDecoder().raw_decode(candidate)
        except Exception:
            return []
        raw_tasks = parsed.get("tasks") if isinstance(parsed, dict) else parsed
        if not isinstance(raw_tasks, list):
            return []
        tasks: list[ResearchTask] = []
        for index, item in enumerate(raw_tasks[:4], start=1):
            if not isinstance(item, dict):
                continue
            question = str(item.get("question") or item.get("query") or "").strip()
            if not question:
                continue
            tasks.append(
                ResearchTask(
                    key=str(item.get("key") or f"t{index}").strip() or f"t{index}",
                    title=str(item.get("title") or f"研究子任务 {index}").strip(),
                    question=question,
                )
            )
        return tasks

    def _fallback_research_tasks(self, question: str) -> list[ResearchTask]:
        if self.name == "plan_solve":
            return [ResearchTask("t1", "执行计划", question)]
        return [
            ResearchTask("t1", "核心事实与技术依据", f"{question} 核心事实 技术依据"),
            ResearchTask("t2", "现状、案例与最新进展", f"{question} 现状 案例 最新进展"),
            ResearchTask("t3", "局限、风险与争议", f"{question} 局限 风险 争议"),
        ]

    def _should_generate_report(self, request: AgentRequest) -> bool:
        return self.name == "deep_research" or "deep-research" in request.selected_skills

    async def _save_report(
        self,
        *,
        request: AgentRequest,
        context: AgentRuntimeContext,
        content: str,
        sequence: int,
    ) -> AsyncIterator[AgentEvent]:
        title_seed = re.sub(r"\s+", " ", request.question).strip()[:48]
        title = f"深度研究报告：{title_seed}" if title_seed else "深度研究报告"
        sequence += 1
        yield self._event(
            context,
            sequence,
            "workflow.step",
            {
                "status": "正在生成可下载的深度研究报告",
                "content": "正在生成可下载的深度研究报告",
            },
        )
        sequence += 1
        yield self._event(
            context,
            sequence,
            "workflow.step",
            {
                "event": make_process_event(
                    "artifact_update",
                    "深度研究报告（预览）",
                    "最终 Markdown 报告已生成，DOCX 文件正在归档。",
                    tool="save_deep_research_report",
                    level="active",
                    metadata={
                        "id": f"deep-research-preview:{context.request_id}",
                        "file_id": f"deep-research-preview:{context.request_id}",
                        "title": title,
                        "type": "markdown",
                        "content": content,
                        "previewable": True,
                        "virtual_artifact": True,
                        "is_streaming": False,
                    },
                )
            },
        )
        try:
            report_payload = await save_deep_research_report_direct(
                thread_id=context.internal_thread_id,
                title=title,
                content=content,
            )
            artifact_path = str(report_payload.get("artifact_path") or "")
            artifact_metadata = await build_registered_artifact_metadata(
                internal_thread_id=context.internal_thread_id,
                artifact_path=artifact_path,
                title=str(report_payload.get("title") or title),
                minio_object=str(report_payload.get("minio_object") or "") or None,
            )
            sequence += 1
            yield self._event(
                context,
                sequence,
                "workflow.step",
                {
                    "event": make_process_event(
                        "artifact",
                        str(report_payload.get("title") or title),
                        "研究报告已生成，可下载 DOCX 文件。",
                        tool="save_deep_research_report",
                        level="success",
                        metadata=artifact_metadata,
                    )
                },
            )
        except Exception as exc:  # noqa: BLE001
            sequence += 1
            yield self._event(
                context,
                sequence,
                "workflow.step",
                {
                    "event": make_process_event(
                        "status",
                        "报告归档失败",
                        f"答案已生成，但 DOCX 报告保存失败：{exc}",
                        level="warning",
                    )
                },
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

    def build_executor(self, request: AgentRequest, context: AgentRuntimeContext):
        return self._delegate.build_executor(request, context)
