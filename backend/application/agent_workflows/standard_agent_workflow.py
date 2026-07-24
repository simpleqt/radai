"""Standard lightweight/full agent workflow implementation."""

from __future__ import annotations

import asyncio
import logging
from collections.abc import AsyncIterator
from typing import TYPE_CHECKING, Any

from langchain_core.runnables import RunnableConfig
from langgraph.checkpoint.base import BaseCheckpointSaver

from backend.application.agent_errors import public_agent_error
from backend.application.agent_executors import AgentExecutorRegistry
from backend.application.agent_trace import AgentTraceRecorder
from backend.application.blocking import run_blocking
from backend.application.suggestions.follow_up_service import (
    FollowUpContext,
    FollowUpSuggestionService,
    persist_corrected_answer_to_checkpoint,
    persist_follow_up_to_checkpoint,
    suggestions_to_metadata_items,
)
from backend.config.settings import settings
from backend.core.skills.scoped_skills_provider import ScopedSkillsProvider
from backend.domain.agents.events import AgentEvent
from backend.domain.agents.request import AgentRequest
from backend.domain.agents.runtime_agent import RuntimeAgentSpec
from backend.domain.agents.runtime_budget import AgentRuntimeBudget
from backend.domain.agents.runtime_context import AgentRuntimeContext
from backend.domain.agents.workflows import get_workflow_spec
from backend.domain.tools.runtime_context import ToolRuntimeContext

if TYPE_CHECKING:
    from backend.application.agents.definition import ResolvedAgentDefinition

logger = logging.getLogger(__name__)


class StandardAgentWorkflow:
    """Run lightweight/full workflows without the legacy router indirection."""

    def __init__(
        self,
        *,
        name: str,
        checkpointer: BaseCheckpointSaver | None = None,
        runnable_config: RunnableConfig | None = None,
        executor_registry: AgentExecutorRegistry | None = None,
        runtime_budget: AgentRuntimeBudget | None = None,
        agent_specs: dict[str, RuntimeAgentSpec] | None = None,
        context_policy: Any | None = None,
        execution_strategy: str | None = None,
        resolved_definition: ResolvedAgentDefinition | None = None,
    ):
        self.name = name
        self._resolved_definition = resolved_definition
        workflow_spec = (
            next(
                (spec for spec in resolved_definition.workflows if spec.name == name),
                None,
            )
            if resolved_definition is not None
            else get_workflow_spec(name)
        )
        self._execution_strategy = (
            str(
                execution_strategy
                or getattr(workflow_spec, "execution_strategy", "graph")
                or "graph"
            )
            .strip()
            .lower()
        )
        if self._execution_strategy not in {"graph", "lightweight"}:
            raise ValueError(
                f"Standard workflow {name} cannot use execution strategy "
                f"{self._execution_strategy}"
            )
        self._checkpointer = checkpointer
        self._runnable_config = runnable_config
        self._executor_registry = executor_registry or AgentExecutorRegistry()
        self._runtime_budget = runtime_budget
        self._agent_specs = dict(agent_specs or {})
        self._context_policy = context_policy

    async def run(
        self,
        request: AgentRequest,
        context: AgentRuntimeContext,
    ) -> AsyncIterator[AgentEvent]:
        from backend.application.agent_executors.image_url_registry import (
            inject_missing_captioned_images,
            reset_registry,
        )
        from backend.application.image_pipeline import sanitize_answer_image_urls

        reset_registry()
        from backend.application.agent_executors.image_url_registry import get_registry

        registry = get_registry()
        logger.info("[standard_agent_workflow.run] Start registry id: %s", id(registry))
        if self._runnable_config is None:
            self._runnable_config = {}
        else:
            self._runnable_config = dict(self._runnable_config)
        configurable = dict(self._runnable_config.get("configurable") or {})
        configurable["image_url_registry"] = registry
        self._runnable_config["configurable"] = configurable
        follow_up_service = FollowUpSuggestionService()
        follow_up_stream = None
        follow_up_suggestions: list[str] = []
        visible_answer_parts: list[str] = []
        executor = None
        failed = False
        run_error_code: str | None = None
        terminal_status = "completed"
        terminal_error: str | None = None
        terminal_exception = False
        trace_finished = False
        sequence = 0
        trace = AgentTraceRecorder.from_context(context, workflow_name=self.name)
        trace.record_run_started(request=request, context=context)

        def emit(event: AgentEvent) -> AgentEvent:
            trace.record_agent_event(event)
            return event

        def finish_trace() -> None:
            nonlocal trace_finished
            if trace_finished:
                return
            trace_finished = True
            try:
                trace.record_run_finished(
                    request=request,
                    context=context,
                    status=terminal_status,
                    error_code=run_error_code,
                )
            except Exception:
                # Trace persistence must never hide the request's terminal
                # state or turn a client disconnect into a second failure.
                logger.exception(
                    "[StandardAgentWorkflow] trace finalization failed: run_id=%s",
                    context.run_id or context.request_id,
                )

        def mark_event_failure(event: AgentEvent) -> None:
            nonlocal failed, terminal_status, terminal_error, run_error_code
            if event.type != "error":
                return
            failed = True
            terminal_status = "failed"
            terminal_error = str(event.payload.get("message") or "agent stream error")
            run_error_code = str(
                event.payload.get("error_code") or "agent_stream_error"
            )

        try:
            yield emit(
                AgentEvent(
                    type="workflow.started",
                    request_id=context.request_id,
                    thread_id=context.external_thread_id,
                    sequence=sequence,
                    payload={
                        "workflow": self.name,
                        "agent_code": context.agent_profile.code,
                        "workflow_route": dict(
                            context.request_metadata.get("_workflow_route") or {}
                        ),
                    },
                )
            )
            executor = await run_blocking(self.build_executor, request, context)
            if (
                settings.follow_up_suggestions_enabled
                and context.workflow_name != "deep_research"
            ):
                follow_up_stream = follow_up_service.start_stream_task(
                    FollowUpContext(
                        user_question=request.question,
                        context_summary=self._build_follow_up_context(request, context),
                        agent_mode=context.workflow_name,
                        selected_skills=context.selected_skills,
                        model_name=context.model_name,
                        use_llm=True,
                    )
                )
            stream_kwargs: dict[str, Any] = {}
            if request.attachments:
                stream_kwargs["image_contents"] = list(request.attachments)
            async for raw_event in executor.aquery_stream(
                request.question,
                context.internal_thread_id,
                self._runnable_config,
                **stream_kwargs,
            ):
                if isinstance(raw_event, AgentEvent):
                    if raw_event.type == "done":
                        break
                    sequence += 1
                    event = AgentEvent(
                        type=raw_event.type,
                        request_id=context.request_id,
                        thread_id=context.external_thread_id,
                        sequence=sequence,
                        payload=dict(raw_event.payload),
                        created_at=raw_event.created_at,
                    )
                    self._track_visible_answer(event, visible_answer_parts)
                    yield emit(event)
                    mark_event_failure(event)
                    follow_up_service.note_process_event(
                        event.payload.get("event", {})
                        if isinstance(event.payload.get("event"), dict)
                        else {}
                    )
                    if event.type == "message.delta" and follow_up_stream is not None:
                        follow_up_stream.note_visible(
                            str(event.payload.get("content") or "")
                        )
                    follow_up_event = await self._take_follow_up_event(
                        None if failed else follow_up_stream,
                        context=context,
                        sequence=sequence + 1,
                    )
                    if follow_up_event is not None:
                        follow_up_suggestions = list(
                            follow_up_event.payload.get("suggestions") or []
                        )
                        sequence += 1
                        yield emit(follow_up_event)
                    continue
                if isinstance(raw_event, dict):
                    if raw_event.get("type") == "done":
                        break
                    sequence += 1
                    event = AgentEvent.from_runtime_payload(
                        raw_event,
                        request_id=context.request_id,
                        thread_id=context.external_thread_id,
                        sequence=sequence,
                    )
                    self._track_visible_answer(event, visible_answer_parts)
                    yield emit(event)
                    mark_event_failure(event)
                    if raw_event.get("type") == "event":
                        follow_up_service.note_process_event(
                            raw_event.get("event", {})
                            if isinstance(raw_event.get("event"), dict)
                            else {}
                        )
                    if (
                        raw_event.get("type") == "chunk"
                        and follow_up_stream is not None
                    ):
                        follow_up_stream.note_visible(
                            str(raw_event.get("content") or "")
                        )
                    follow_up_event = await self._take_follow_up_event(
                        None if failed else follow_up_stream,
                        context=context,
                        sequence=sequence + 1,
                    )
                    if follow_up_event is not None:
                        follow_up_suggestions = list(
                            follow_up_event.payload.get("suggestions") or []
                        )
                        sequence += 1
                        yield emit(follow_up_event)
                    continue
                sequence += 1
                yield emit(
                    AgentEvent(
                        type="message.delta",
                        request_id=context.request_id,
                        thread_id=context.external_thread_id,
                        sequence=sequence,
                        payload={"content": str(raw_event)},
                    )
                )
                visible_answer_parts.append(str(raw_event))
            if not failed:
                current_answer = "".join(visible_answer_parts)
                from backend.application.agent_executors.image_url_registry import (
                    get_registry,
                )

                logger.info(
                    "[standard_agent_workflow.run] End registry id: %s, entries: %s",
                    id(get_registry()),
                    get_registry()._entries,
                )
                corrected_answer = inject_missing_captioned_images(current_answer)
                corrected_answer = sanitize_answer_image_urls(corrected_answer)
                if corrected_answer != current_answer:
                    visible_answer_parts[:] = [corrected_answer]
                    sequence += 1
                    yield emit(
                        AgentEvent(
                            type="message.replace",
                            request_id=context.request_id,
                            thread_id=context.external_thread_id,
                            sequence=sequence,
                            payload={"content": corrected_answer},
                        )
                    )
                    if executor is not None:
                        await persist_corrected_answer_to_checkpoint(
                            checkpointer=self._checkpointer,
                            graph=getattr(executor, "graph", None),
                            internal_thread_id=context.internal_thread_id,
                            corrected_answer=corrected_answer,
                        )

            if not failed:
                follow_up_event = await self._take_follow_up_event(
                    follow_up_stream,
                    context=context,
                    sequence=sequence + 1,
                )
                if follow_up_event is not None:
                    follow_up_suggestions = list(
                        follow_up_event.payload.get("suggestions") or []
                    )
                    sequence += 1
                    yield emit(follow_up_event)
            if follow_up_suggestions and executor is not None:
                await persist_follow_up_to_checkpoint(
                    checkpointer=self._checkpointer,
                    graph=getattr(executor, "graph", None),
                    internal_thread_id=context.internal_thread_id,
                    suggestions=suggestions_to_metadata_items(follow_up_suggestions),
                )
        except asyncio.CancelledError:
            failed = True
            terminal_status = "cancelled"
            terminal_error = "standard workflow cancelled"
            run_error_code = "cancelled"
            terminal_exception = True
            finish_trace()
            raise
        except GeneratorExit:
            failed = True
            terminal_status = "interrupted"
            terminal_error = "standard workflow consumer closed"
            run_error_code = "interrupted"
            terminal_exception = True
            finish_trace()
            raise
        except Exception as exc:  # noqa: BLE001
            failed = True
            terminal_status = "failed"
            terminal_error = str(exc) or type(exc).__name__
            logger.exception(
                "[StandardAgentWorkflow] stream failed: agent=%s workflow=%s thread_id=%s",
                context.agent_profile.code,
                self.name,
                context.external_thread_id,
            )
            public_message, error_code = public_agent_error(exc)
            run_error_code = error_code
            sequence += 1
            yield emit(
                AgentEvent(
                    type="error",
                    request_id=context.request_id,
                    thread_id=context.external_thread_id,
                    sequence=sequence,
                    payload={
                        "message": public_message,
                        "error": public_message,
                        "error_code": error_code,
                    },
                )
            )
        finally:
            if follow_up_stream is not None:
                try:
                    follow_up_stream.cancel()
                except Exception:
                    logger.debug(
                        "[StandardAgentWorkflow] follow-up stream cancellation failed",
                        exc_info=True,
                    )

        if terminal_exception:
            finish_trace()
            return

        final_event = emit(
            AgentEvent(
                type="done",
                request_id=context.request_id,
                thread_id=context.external_thread_id,
                sequence=sequence + 1,
                payload={},
            )
        )
        try:
            # ``done`` is the lifecycle commit barrier.  Keep the trace in a
            # running state until the terminal event has been handed to the
            # owning stream/task and the generator is resumed or closed.
            yield final_event
        finally:
            finish_trace()

    @staticmethod
    def _track_visible_answer(
        event: AgentEvent, visible_answer_parts: list[str]
    ) -> None:
        if event.type == "message.delta":
            visible_answer_parts.append(str(event.payload.get("content") or ""))
        elif event.type == "message.replace":
            visible_answer_parts[:] = [str(event.payload.get("content") or "")]

    def build_executor(self, request: AgentRequest, context: AgentRuntimeContext):
        kwargs = self._build_agent_kwargs(request, context)
        return self._executor_registry.build(context.agent_profile, **kwargs)

    def _build_agent_kwargs(
        self,
        request: AgentRequest,
        context: AgentRuntimeContext,
    ) -> dict[str, Any]:
        managed_skills = bool(context.permissions.get("managed_skills_resolved"))
        bindings = context.agent_bindings
        if bindings is not None and bindings.agent_code != context.agent_profile.code:
            raise ValueError(
                "Runtime binding agent_code does not match context profile: "
                f"{bindings.agent_code} != {context.agent_profile.code}"
            )
        prompt_profile = (
            bindings.prompt_profile
            if bindings is not None
            else context.agent_profile.prompt_profile
        )
        tool_policy = (
            bindings.tool_policy
            if bindings is not None
            else context.agent_profile.tool_policy
        )
        skills_base_path = (
            self._resolved_definition.runtime_scope.skills_base_path
            if self._resolved_definition is not None
            and self._resolved_definition.runtime_scope is not None
            else None
        )
        definition_scoped_skills = (
            self._resolved_definition is not None
            and self._resolved_definition.runtime_scope is not None
        )
        return {
            "model_name": context.model_name,
            "subagent_enabled": context.subagent_enabled,
            "use_search": context.use_search,
            "kb_ids": context.kb_ids,
            "selected_skills": context.selected_skills,
            "managed_skills": managed_skills,
            "skills_provider": ScopedSkillsProvider(
                skills_base_path=skills_base_path,
                user_id=context.user_id,
                authorized_skill_names=(
                    set(context.selected_skills)
                    if managed_skills or definition_scoped_skills
                    else None
                ),
            ),
            "resolved_agent_definition": self._resolved_definition,
            # Lightweight is a manifest-selected policy of the same profiled
            # executor, not an agent-code branch or a separate registry entry.
            "lightweight_mode": self._execution_strategy == "lightweight",
            "checkpointer": self._checkpointer,
            "thread_id": context.internal_thread_id,
            "external_thread_id": context.external_thread_id,
            "thinking_enabled": context.thinking_enabled,
            "domain": context.agent_profile.domain,
            "prompt_profile": prompt_profile,
            "workflow_name": context.workflow_name,
            "tool_policy": tool_policy,
            "guardrail_policy": context.agent_profile.guardrail_policy,
            "agent_bindings": bindings,
            "runtime_permissions": dict(context.permissions),
            "runtime_budget": context.runtime_budget or self._runtime_budget,
            "agent_specs": dict(context.agent_specs or self._agent_specs),
            "context_policy": context.context_policy or self._context_policy,
            "guardrail_instructions": context.guardrail_instructions,
            "time_context": context.time_context,
            "request_metadata": {
                **dict(context.request_metadata),
                "_request_id": context.request_id,
                "_agent_code": context.agent_profile.code,
                "_domain": context.agent_profile.domain,
                "_tool_policy": tool_policy,
                "_workflow_name": context.workflow_name,
                "_domain_context": dict(context.domain_context),
                "_trace_enabled": context.trace_enabled,
                "_trace_run_id": context.run_id or context.request_id,
            },
            "user_id": context.user_id,
            "tool_runtime_context": ToolRuntimeContext(
                request_id=context.request_id,
                user_id=context.user_id,
                agent_code=context.agent_profile.code,
                domain=context.agent_profile.domain,
                thread_id=context.internal_thread_id,
                role=str(context.permissions.get("auth_role") or "user").strip().lower()
                or "user",
                permissions=dict(context.permissions),
                time_context=context.time_context,
                request_metadata=dict(context.request_metadata),
                domain_context=dict(context.domain_context),
            ),
        }

    @staticmethod
    def _build_follow_up_context(
        request: AgentRequest,
        context: AgentRuntimeContext,
    ) -> str:
        parts = [
            f"工作流: {context.workflow_name}",
            f"联网/检索: {'开启' if context.use_search else '关闭'}",
        ]
        if context.kb_ids:
            parts.append(f"已选知识库数量: {len(context.kb_ids)}")
        if context.selected_skills:
            parts.append("已选技能: " + ", ".join(context.selected_skills[:6]))
        if request.attachments:
            parts.append(f"附件数量: {len(request.attachments)}")
        parts.append("用户问题:\n" + request.question[:1000])
        return "\n".join(parts)

    @staticmethod
    async def _take_follow_up_event(
        follow_up_stream,
        *,
        context: AgentRuntimeContext,
        sequence: int,
        force: bool = False,
    ) -> AgentEvent | None:
        if follow_up_stream is None:
            return None
        ready = await follow_up_stream.take_ready(force=force)
        if not ready:
            return None
        suggestions, metadata = ready
        return AgentEvent(
            type="follow_up.created",
            request_id=context.request_id,
            thread_id=context.external_thread_id,
            sequence=sequence,
            payload={
                "suggestions": suggestions,
                "metadata": metadata,
            },
        )
