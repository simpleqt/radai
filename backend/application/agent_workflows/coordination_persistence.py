"""Report artifact and parent-thread persistence for coordinated workflows."""

from __future__ import annotations

import asyncio
import logging
import re
from collections.abc import AsyncIterator
from dataclasses import replace
from typing import Any

from langchain_core.messages import AIMessage, HumanMessage

from backend.application.agent_executors.stream_utils import make_process_event
from backend.application.agent_workflows.coordination_types import (
    CoordinationUnitResult,
    ResearchTask,
)
from backend.application.blocking import run_blocking
from backend.application.tools.runtime_invoker import parse_tool_json_result
from backend.domain.agents.events import AgentEvent
from backend.domain.agents.request import AgentRequest
from backend.domain.agents.runtime_context import AgentRuntimeContext

logger = logging.getLogger(__name__)


class CoordinationPersistenceMixin:
    """Persistence behavior shared by coordinated workflow controllers."""

    def _should_generate_report(self, request: AgentRequest) -> bool:
        return self.name == "deep_research" or "deep-research" in (
            request.selected_skills or []
        )

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
        report_timeout_seconds = float(
            self._runtime_budget.deep_research_report_timeout_seconds
        )
        sequence += 1
        yield self._event(
            context,
            sequence,
            "workflow.step",
            {
                "status": "正在生成可下载的深度研究报告",
                "content": "正在生成可下载的深度研究报告",
                "metadata": {
                    "runtime_type": "report_save_started",
                    "report_timeout_seconds": report_timeout_seconds,
                },
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
                        "runtime_type": "report_save_started",
                        "report_timeout_seconds": report_timeout_seconds,
                    },
                )
            },
        )
        try:
            # Resolve through the controller module to preserve the existing
            # runtime/test injection seam while persistence lives separately.
            from backend.application.agent_workflows import (
                coordinated_agent_workflow as controller_runtime,
            )

            async def persist_report() -> tuple[dict[str, Any], dict[str, Any]]:
                invoker_kwargs: dict[str, Any] = {
                    "context": context,
                    "workflow_name": "deep_research",
                    "enforce_skill_load": False,
                }
                resolved_definition = getattr(
                    self,
                    "_resolved_definition",
                    None,
                )
                if resolved_definition is not None:
                    invoker_kwargs["resolved_agent_definition"] = resolved_definition
                report_result = await controller_runtime.RuntimeToolInvoker(
                    **invoker_kwargs
                ).invoke(
                    "save_deep_research_report",
                    {"title": title, "content": content},
                )
                report_payload = parse_tool_json_result(report_result)
                artifact_path = str(report_payload.get("artifact_path") or "")
                artifact_metadata = (
                    await controller_runtime.build_registered_artifact_metadata(
                        internal_thread_id=context.internal_thread_id,
                        artifact_path=artifact_path,
                        title=str(report_payload.get("title") or title),
                        minio_object=(
                            str(report_payload.get("minio_object") or "") or None
                        ),
                    )
                )
                return report_payload, artifact_metadata

            report_payload, artifact_metadata = await asyncio.wait_for(
                persist_report(),
                timeout=report_timeout_seconds,
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
                        metadata={
                            **artifact_metadata,
                            "runtime_type": "report_save_completed",
                            "report_timeout_seconds": report_timeout_seconds,
                        },
                    )
                },
            )
        except Exception as exc:  # noqa: BLE001
            report_timed_out = isinstance(exc, (asyncio.TimeoutError, TimeoutError))
            sequence += 1
            yield self._event(
                context,
                sequence,
                "workflow.step",
                {
                    "event": make_process_event(
                        "status",
                        "报告保存超时" if report_timed_out else "报告归档失败",
                        (
                            f"答案已生成，但 DOCX 报告未能在 {report_timeout_seconds:g} 秒内完成保存。"
                            if report_timed_out
                            else f"答案已生成，但 DOCX 报告保存失败：{exc}"
                        ),
                        level="warning",
                        metadata={
                            "runtime_type": (
                                "report_save_timed_out"
                                if report_timed_out
                                else "report_save_failed"
                            ),
                            "report_timeout_seconds": report_timeout_seconds,
                        },
                    )
                },
            )

    async def _persist_visible_turn(
        self,
        request: AgentRequest,
        context: AgentRuntimeContext,
        answer_text: str,
        *,
        results: list[CoordinationUnitResult] | None = None,
        research_tasks: list[ResearchTask] | None = None,
    ) -> bool:
        """Persist the visible result on the parent, not isolated child, thread."""
        if self._checkpointer is None or not str(answer_text or "").strip():
            logger.warning(
                "[CoordinatedAgentWorkflow] parent-thread history unavailable: thread_id=%s checkpointer=%s",
                context.internal_thread_id,
                bool(self._checkpointer),
            )
            return False

        try:
            parent_request = replace(
                request,
                requested_workflow="full",
                subagent_enabled=False,
            )
            parent_context = replace(
                context,
                workflow_name="full",
                subagent_enabled=False,
            )
            executor = await run_blocking(
                self._delegate.build_executor,
                parent_request,
                parent_context,
            )
            graph = getattr(executor, "graph", None) or getattr(
                executor, "_graph", None
            )
            if graph is None:
                logger.warning(
                    "[CoordinatedAgentWorkflow] executor has no graph; history not persisted: thread_id=%s",
                    context.internal_thread_id,
                )
                return False

            config = {"configurable": {"thread_id": context.internal_thread_id}}
            messages = [
                HumanMessage(content=str(request.question or "").strip()),
                AIMessage(content=str(answer_text or "").strip()),
            ]
            research_workflow = self._build_parent_checkpoint_metadata(
                workflow=self.name,
                results=results or [],
                research_tasks=research_tasks or [],
            )
            values = {
                "messages": messages,
                "research_workflow": research_workflow,
            }
            try:
                await asyncio.wait_for(
                    graph.aupdate_state(config, values, as_node="model"),
                    timeout=10.0,
                )
            except Exception:
                await asyncio.wait_for(
                    graph.aupdate_state(config, values),
                    timeout=10.0,
                )
            return True
        except Exception:
            logger.warning(
                "[CoordinatedAgentWorkflow] parent-thread history persist failed: thread_id=%s",
                context.internal_thread_id,
                exc_info=True,
            )
            return False

    @staticmethod
    def _build_parent_checkpoint_metadata(
        *,
        workflow: str,
        results: list[CoordinationUnitResult],
        research_tasks: list[ResearchTask],
    ) -> dict[str, Any]:
        used_tools: set[str] = set()
        for result in results:
            for event in result.process_events:
                tool_name = event.get("tool")
                if isinstance(tool_name, str) and tool_name:
                    used_tools.add(tool_name)
        return {
            "coordination_workflow": workflow,
            "research_tasks": [
                {
                    "key": task.key,
                    "title": task.title,
                    "question": task.question,
                    "priority": task.priority,
                    "expected_tools": list(task.expected_tools),
                    "risk": task.risk,
                    "dependencies": list(task.dependencies),
                    "budget": task.budget,
                }
                for task in research_tasks
            ],
            "handoffs": [
                {
                    "role": result.role,
                    "answer_preview": result.answer[:800],
                    "error": result.error,
                    "claims": (result.handoff or {}).get("claims", []),
                }
                for result in results
            ],
            "used_tools": sorted(used_tools),
        }
