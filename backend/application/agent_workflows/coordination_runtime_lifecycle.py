"""Root runtime lifecycle for coordinated agent workflows."""

from __future__ import annotations

import asyncio
import logging
from collections.abc import AsyncIterator
from typing import Any

from backend.application.agent_errors import public_agent_error
from backend.application.agent_executors.stream_utils import make_process_event
from backend.application.agent_trace import AgentTraceRecorder
from backend.application.agent_workflows import coordinated_agent_workflow as _legacy
from backend.application.agent_workflows.coordination_component import (
    CoordinationComponent,
)
from backend.application.agent_workflows.coordination_types import ResearchTask
from backend.core.agents.runtime_manager import (
    RunBudget,
    create_agent_runtime_manager,
    get_agent_runtime_manager,
    reset_agent_runtime_manager,
    set_agent_runtime_manager,
    unregister_agent_runtime_manager,
)
from backend.core.tools.request_limits import (
    reset_request_tool_budget,
    reset_tool_budgets,
    reset_turn_kb_context_budget,
    set_request_tool_budget,
    set_tool_budgets,
)
from backend.core.utils.async_tasks import task_is_cancelling
from backend.domain.agents.events import AgentEvent
from backend.domain.agents.request import AgentRequest
from backend.domain.agents.runtime_context import AgentRuntimeContext

_cancel_task_bounded = _legacy._cancel_task_bounded
logger = logging.getLogger(__name__)


class CoordinationRuntimeLifecycle(CoordinationComponent):
    async def run(
        self,
        request: AgentRequest,
        context: AgentRuntimeContext,
    ) -> AsyncIterator[AgentEvent]:
        """Run one coordinated tree under a shared runtime manager."""

        runtime_manager = get_agent_runtime_manager()
        if runtime_manager is not None:
            attached_run_status = "interrupted"
            attached_run_error: str | None = None
            attached_run_degraded = False
            try:
                async for event in self._host._run_impl(request, context):
                    if event.type == "coordination.completed" and bool(
                        event.payload.get("degraded")
                    ):
                        attached_run_degraded = True
                    if event.type == "error" and attached_run_status == "interrupted":
                        attached_run_status = "failed"
                        attached_run_error = str(
                            event.payload.get("message") or "agent workflow error"
                        )
                    yield event
                if attached_run_status == "interrupted":
                    attached_run_status = "completed"
            except asyncio.CancelledError:
                attached_run_status = "cancelled"
                attached_run_error = "coordinated workflow cancelled"
                raise
            except GeneratorExit:
                attached_run_status = "interrupted"
                attached_run_error = "coordinated workflow consumer closed"
                raise
            except BaseException as exc:
                attached_run_status = "failed"
                attached_run_error = str(exc) or type(exc).__name__
                raise
            finally:
                self._host._finish_active_trace(
                    request=request,
                    context=context,
                    status=(
                        "degraded"
                        if (
                            attached_run_status == "completed" and attached_run_degraded
                        )
                        else attached_run_status
                    ),
                    error=attached_run_error,
                )
            return

        max_active_agents = max(1, int(self._research_llm_capacity()))
        runtime_manager = create_agent_runtime_manager(
            root_agent_id=context.request_id,
            budget=RunBudget(
                max_spawned_agents=self._max_spawned_agents(),
                max_active_agents=max_active_agents,
                max_depth=1,
                max_tokens=self._runtime_budget.tree_max_tokens,
                max_tool_calls=self._runtime_budget.tree_max_tool_calls,
                # The outer stream runtime owns the request-wide deadline. A
                # second root deadline can expire while deep research is in
                # its bounded final convergence/report phase.
                max_elapsed_seconds=(
                    None
                    if self.name == "deep_research"
                    else float(self._runtime_budget.workflow_total_timeout_seconds)
                ),
            ),
            run_metadata={
                "request_id": context.request_id,
                "user_id": getattr(context, "user_id", ""),
                "thread_id": context.internal_thread_id,
                "external_thread_id": getattr(
                    context,
                    "external_thread_id",
                    "",
                ),
                "workflow": self.name,
            },
        )
        manager_token = set_agent_runtime_manager(runtime_manager)
        run_status = "interrupted"
        run_error: str | None = None
        run_degraded = False
        active_failure: BaseException | None = None
        try:
            async for event in self._host._run_impl(request, context):
                event_type = getattr(event, "type", None)
                event_payload = getattr(event, "payload", {}) or {}
                if event_type == "coordination.completed" and bool(
                    event_payload.get("degraded")
                ):
                    run_degraded = True
                if event_type == "error" and run_status == "interrupted":
                    run_status = "failed"
                    run_error = str(
                        event_payload.get("message") or "agent workflow error"
                    )
                yield event
            if run_status == "interrupted":
                run_status = "completed"
        except asyncio.CancelledError as exc:
            run_status = "cancelled"
            run_error = "coordinated workflow cancelled"
            active_failure = exc
            raise
        except GeneratorExit as exc:
            run_status = "interrupted"
            run_error = "coordinated workflow consumer closed"
            active_failure = exc
            raise
        except BaseException as exc:
            run_status = "failed"
            run_error = str(exc) or type(exc).__name__
            active_failure = exc
            raise
        finally:
            requested_cancelled = bool(runtime_manager.cancellation.cancelled)
            cancellation_reason = runtime_manager.cancellation.reason
            cleanup_error: BaseException | None = None
            finalize_error: BaseException | None = None
            drained = True
            try:
                await runtime_manager.interrupt(
                    runtime_manager.root_agent_id,
                    reason="coordinated workflow closed",
                )
                try:
                    await runtime_manager.wait(timeout=1.5)
                except (asyncio.TimeoutError, TimeoutError):  # noqa: UP041
                    drained = False
                    logger.warning(
                        "[CoordinatedAgentWorkflow] runtime manager drain timed out: request_id=%s",
                        context.request_id,
                    )
            except BaseException as exc:  # cleanup must not hide the run failure
                cleanup_error = exc
                logger.exception(
                    "[CoordinatedAgentWorkflow] runtime manager cleanup failed: request_id=%s",
                    context.request_id,
                )
            finally:
                if run_status == "completed" and requested_cancelled:
                    run_status = "cancelled"
                    run_error = cancellation_reason or "request cancelled"
                elif run_status == "completed" and not drained:
                    run_status = "interrupted"
                    run_error = "runtime manager drain timed out"
                finalize = getattr(runtime_manager, "finalize_run", None)
                if callable(finalize):
                    try:
                        await asyncio.wait_for(
                            finalize(
                                run_status,
                                error=run_error,
                                metadata={
                                    "degraded": run_degraded,
                                    "completion_status": (
                                        "degraded"
                                        if run_status == "completed" and run_degraded
                                        else run_status
                                    ),
                                },
                            ),
                            timeout=15.0,
                        )
                    except TimeoutError:
                        logger.warning(
                            "[CoordinatedAgentWorkflow] runtime outcome persistence timed out: request_id=%s",
                            context.request_id,
                        )
                    except BaseException as exc:
                        finalize_error = exc
                        logger.exception(
                            "[CoordinatedAgentWorkflow] runtime outcome persistence failed: request_id=%s",
                            context.request_id,
                        )
                try:
                    self._host._finish_active_trace(
                        request=request,
                        context=context,
                        status=(
                            "degraded"
                            if run_status == "completed" and run_degraded
                            else run_status
                        ),
                        error=run_error,
                    )
                finally:
                    try:
                        unregister_agent_runtime_manager(
                            runtime_manager.root_agent_id,
                            runtime_manager,
                        )
                    finally:
                        reset_agent_runtime_manager(manager_token)
            if active_failure is None:
                if cleanup_error is not None:
                    raise cleanup_error
                if finalize_error is not None:
                    raise finalize_error

    def _finish_active_trace(
        self,
        *,
        request: AgentRequest,
        context: AgentRuntimeContext,
        status: str,
        error: str | None,
    ) -> None:
        """Commit the trace only after the coordinated stream reaches its barrier."""

        active_trace = self._active_traces.pop(context.request_id, None)
        if active_trace is None:
            return
        error_code = {
            "cancelled": "cancelled",
            "interrupted": "interrupted",
            "failed": "workflow_failed",
        }.get(status)
        try:
            active_trace.record_run_finished(
                request=request,
                context=context,
                status=status,
                error_code=error_code,
            )
        except Exception:
            logger.exception(
                "[CoordinatedAgentWorkflow] trace finalization failed: run_id=%s error=%s",
                context.run_id or context.request_id,
                error,
            )

    async def _run_impl(
        self,
        request: AgentRequest,
        context: AgentRuntimeContext,
    ) -> AsyncIterator[AgentEvent]:
        from backend.application.agent_executors.image_url_registry import (
            get_registry,
            reset_registry,
        )

        reset_registry()
        registry = get_registry()
        if self._runnable_config is None:
            self._host._runnable_config = {}
        else:
            self._host._runnable_config = dict(self._runnable_config)
        configurable = dict(self._runnable_config.get("configurable") or {})
        configurable["image_url_registry"] = registry
        self._runnable_config["configurable"] = configurable
        sequence = 0
        trace = AgentTraceRecorder.from_context(context, workflow_name=self.name)
        self._active_traces[context.request_id] = trace
        trace.record_run_started(request=request, context=context)

        def recorded_event(
            event_sequence: int,
            event_type: str,
            payload: dict[str, Any],
        ) -> AgentEvent:
            event = self._event(context, event_sequence, event_type, payload)
            trace.record_agent_event(event)
            return event

        plan = self._strategy.build_plan(request, context)
        yield recorded_event(
            sequence,
            "workflow.started",
            {
                "workflow": self.name,
                "agent_code": context.agent_profile.code,
                "coordination_mode": self.name,
                "workflow_route": dict(
                    context.request_metadata.get("_workflow_route") or {}
                ),
            },
        )

        sequence += 1
        yield recorded_event(
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
                "metadata": {
                    **dict(plan.metadata),
                    "workflow_route": dict(
                        context.request_metadata.get("_workflow_route") or {}
                    ),
                },
            },
        )

        research_tasks: list[ResearchTask] = []
        streamed_process_events = False
        reset_turn_kb_context_budget(
            self._runtime_budget.coordinated_kb_context_max_chars
            if self.name in {"plan_solve", "deep_research"}
            else None
        )
        tool_budgets = self._coordinated_retrieval_budgets(context)
        budget_token = set_tool_budgets(tool_budgets)
        request_budget_token = set_request_tool_budget(tool_budgets)
        try:
            if self.name in {"plan_solve", "deep_research"}:
                streamed_process_events = True
                event_queue: asyncio.Queue[tuple[str, dict[str, Any]] | None] = (
                    asyncio.Queue(maxsize=self._runtime_budget.event_queue_maxsize)
                )
                outcome: dict[str, Any] = {}

                async def execute_plan_solve() -> None:
                    try:
                        execution = self._run_plan_solve(
                            request,
                            context,
                            event_sink=event_queue.put,
                        )
                        if self.name == "deep_research":
                            # Deep research owns independent research/writer
                            # deadlines internally. A second encompassing timer
                            # would subtract transition overhead from the writer's
                            # reserved time. The outer stream runtime remains the
                            # request-wide emergency deadline.
                            outcome["value"] = await execution
                        else:
                            outcome["value"] = await asyncio.wait_for(
                                execution,
                                timeout=float(
                                    self._runtime_budget.workflow_total_timeout_seconds
                                ),
                            )
                    except asyncio.CancelledError:
                        raise
                    except Exception as exc:  # noqa: BLE001
                        outcome["error"] = exc
                    finally:
                        current = asyncio.current_task()
                        if not task_is_cancelling(current):
                            await event_queue.put(None)

                execution_task = asyncio.create_task(execute_plan_solve())
                try:
                    while True:
                        queued_event = await event_queue.get()
                        if queued_event is None:
                            break
                        event_type, payload = queued_event
                        sequence += 1
                        yield recorded_event(sequence, event_type, payload)
                    await execution_task
                finally:
                    if not execution_task.done():
                        await _cancel_task_bounded(execution_task)
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
            public_message, error_code = public_agent_error(exc)
            sequence += 1
            yield recorded_event(
                sequence,
                "error",
                {
                    "message": public_message,
                    "error": public_message,
                    "error_code": error_code,
                    "workflow": self.name,
                },
            )
            sequence += 1
            yield recorded_event(sequence, "done", {})
            return
        finally:
            reset_tool_budgets(budget_token)
            reset_request_tool_budget(request_budget_token)

        if research_tasks and not streamed_process_events:
            sequence += 1
            yield recorded_event(
                sequence,
                "plan.updated",
                {
                    "workflow": self.name,
                    "tasks": [
                        {
                            "key": task.key,
                            "title": task.title,
                            "question": task.question,
                        }
                        for task in research_tasks
                    ],
                },
            )

        for result in results:
            if not streamed_process_events:
                for process_event in result.process_events:
                    sequence += 1
                    yield recorded_event(
                        sequence,
                        "workflow.step",
                        {"event": process_event},
                    )
            sequence += 1
            event_type = (
                "coordination.unit.failed"
                if result.error
                else "coordination.unit.completed"
            )
            yield recorded_event(
                sequence,
                event_type,
                {
                    "workflow": self.name,
                    "role": result.role,
                    "task_id": result.task_key or result.role,
                    "task_key": result.task_key,
                    "task_title": result.task_title,
                    "error": result.error,
                    "status": (
                        "timed_out"
                        if result.timed_out
                        else "failed" if result.error else "completed"
                    ),
                    "timed_out": result.timed_out,
                    "answer_preview": result.answer[:300],
                },
            )

        terminal_role = {
            "multi_agent_parallel": "synthesizer",
            "multi_agent_alternating": "finalizer",
        }.get(self.name)
        if terminal_role:
            terminal = results[-1] if results else None
            if (
                terminal is None
                or terminal.role != terminal_role
                or terminal.error
                or not terminal.answer.strip()
            ):
                sequence += 1
                message = f"Final coordination unit '{terminal_role}' failed"
                yield recorded_event(
                    sequence,
                    "error",
                    {
                        "message": message,
                        "error": (
                            terminal.error
                            if terminal is not None and terminal.error
                            else message
                        ),
                        "error_code": "coordination_finalization_failed",
                        "workflow": self.name,
                        "role": terminal_role,
                    },
                )
                sequence += 1
                yield recorded_event(sequence, "done", {})
                return

        answered = [result for result in results if result.answer]
        successful = [result for result in answered if not result.error]
        if not answered:
            sequence += 1
            message = "All coordination units failed"
            yield recorded_event(
                sequence,
                "error",
                {"message": message, "error": message, "workflow": self.name},
            )
            sequence += 1
            yield recorded_event(sequence, "done", {})
            return
        else:
            final_answer = results[-1].answer
            from backend.application.agent_executors.image_url_registry import (
                inject_missing_captioned_images,
            )
            from backend.application.image_pipeline import sanitize_answer_image_urls

            final_answer = inject_missing_captioned_images(final_answer)
            final_answer = sanitize_answer_image_urls(final_answer)
            persisted = await self._persist_visible_turn(
                request,
                context,
                final_answer,
                results=results,
                research_tasks=research_tasks,
            )
            sequence += 1
            yield recorded_event(
                sequence,
                "message.replace" if streamed_process_events else "message.delta",
                {"content": final_answer},
            )
            if not persisted:
                sequence += 1
                yield recorded_event(
                    sequence,
                    "workflow.step",
                    {
                        "event": make_process_event(
                            "status",
                            (
                                "会话历史保存失败"
                                if self._checkpointer is not None
                                else "会话历史未启用"
                            ),
                            (
                                "最终答案已发布，报告保存仍将继续；但本轮会话历史未能写入，刷新后可能不可见。"
                                if self._checkpointer is not None
                                else "当前执行入口未配置会话持久化，结果不会出现在刷新后的历史中。"
                            ),
                            level="warning",
                            metadata={
                                "runtime_type": (
                                    "history_persistence_failed"
                                    if self._checkpointer is not None
                                    else "history_persistence_disabled"
                                )
                            },
                        )
                    },
                )
            if self._should_generate_report(request):
                async for report_event in self._save_report(
                    request=request,
                    context=context,
                    content=final_answer,
                    sequence=sequence,
                ):
                    sequence = report_event.sequence
                    trace.record_event(
                        sequence=report_event.sequence,
                        event_type=report_event.type,
                        payload=report_event.payload,
                    )
                    yield report_event
        has_failures = any(result.error for result in results)
        sequence += 1
        yield recorded_event(
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
                "status": "degraded" if has_failures else "completed",
                "degraded": has_failures,
            },
        )
        sequence += 1
        yield recorded_event(sequence, "done", {})
