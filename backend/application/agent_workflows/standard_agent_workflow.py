"""Standard lightweight/full agent workflow implementation."""

from __future__ import annotations

import logging
from typing import Any, AsyncIterator

from langchain_core.runnables import RunnableConfig
from langgraph.checkpoint.base import BaseCheckpointSaver

from backend.application.agent_executors import AgentExecutorRegistry
from backend.application.blocking import run_blocking
from backend.application.suggestions.follow_up_service import (
    FollowUpContext,
    FollowUpSuggestionService,
    persist_follow_up_to_checkpoint,
    suggestions_to_metadata_items,
)
from backend.config.settings import settings
from backend.domain.agents.events import AgentEvent
from backend.domain.agents.request import AgentRequest
from backend.domain.agents.runtime_context import AgentRuntimeContext
from backend.domain.tools.runtime_context import ToolRuntimeContext

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
    ):
        self.name = name
        self._checkpointer = checkpointer
        self._runnable_config = runnable_config
        self._executor_registry = executor_registry or AgentExecutorRegistry()

    async def run(
        self,
        request: AgentRequest,
        context: AgentRuntimeContext,
    ) -> AsyncIterator[AgentEvent]:
        follow_up_service = FollowUpSuggestionService()
        follow_up_stream = None
        follow_up_suggestions: list[str] = []
        executor = None
        failed = False
        sequence = 0
        yield AgentEvent(
            type="workflow.started",
            request_id=context.request_id,
            thread_id=context.external_thread_id,
            sequence=sequence,
            payload={
                "workflow": self.name,
                "agent_code": context.agent_profile.code,
            },
        )
        try:
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
            async for raw_event in executor.aquery_stream(
                request.question,
                context.internal_thread_id,
                self._runnable_config,
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
                    yield event
                    failed = failed or event.type == "error"
                    follow_up_service.note_process_event(
                        event.payload.get("event", {})
                        if isinstance(event.payload.get("event"), dict)
                        else {}
                    )
                    if event.type == "message.delta" and follow_up_stream is not None:
                        follow_up_stream.note_visible(str(event.payload.get("content") or ""))
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
                        yield follow_up_event
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
                    yield event
                    failed = failed or event.type == "error"
                    if raw_event.get("type") == "event":
                        follow_up_service.note_process_event(
                            raw_event.get("event", {})
                            if isinstance(raw_event.get("event"), dict)
                            else {}
                        )
                    if raw_event.get("type") == "chunk" and follow_up_stream is not None:
                        follow_up_stream.note_visible(str(raw_event.get("content") or ""))
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
                        yield follow_up_event
                    continue
                sequence += 1
                yield AgentEvent(
                    type="message.delta",
                    request_id=context.request_id,
                    thread_id=context.external_thread_id,
                    sequence=sequence,
                    payload={"content": str(raw_event)},
                )
        except Exception as exc:  # noqa: BLE001
            failed = True
            logger.exception(
                "[StandardAgentWorkflow] stream failed: agent=%s workflow=%s thread_id=%s",
                context.agent_profile.code,
                self.name,
                context.external_thread_id,
            )
            sequence += 1
            yield AgentEvent(
                type="error",
                request_id=context.request_id,
                thread_id=context.external_thread_id,
                sequence=sequence,
                payload={"message": str(exc), "error": str(exc)},
            )
        finally:
            if follow_up_stream is not None:
                follow_up_stream.cancel()

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
                yield follow_up_event
        if follow_up_suggestions and executor is not None:
            await persist_follow_up_to_checkpoint(
                checkpointer=self._checkpointer,
                graph=getattr(executor, "graph", None),
                internal_thread_id=context.internal_thread_id,
                suggestions=suggestions_to_metadata_items(follow_up_suggestions),
            )

        yield AgentEvent(
            type="done",
            request_id=context.request_id,
            thread_id=context.external_thread_id,
            sequence=sequence + 1,
            payload={},
        )

    def build_executor(self, request: AgentRequest, context: AgentRuntimeContext):
        kwargs = self._build_agent_kwargs(request, context)
        return self._executor_registry.build(context.agent_profile, **kwargs)

    def _build_agent_kwargs(
        self,
        request: AgentRequest,
        context: AgentRuntimeContext,
    ) -> dict[str, Any]:
        selected_skills = [] if self.name == "lightweight" else context.selected_skills
        return {
            "model_name": context.model_name,
            "subagent_enabled": context.subagent_enabled,
            "use_search": context.use_search,
            "kb_ids": context.kb_ids,
            "selected_skills": selected_skills,
            "lightweight_mode": self.name == "lightweight",
            "checkpointer": self._checkpointer,
            "thread_id": context.internal_thread_id,
            "thinking_enabled": context.thinking_enabled,
            "domain": context.agent_profile.domain,
            "prompt_profile": context.agent_profile.prompt_profile,
            "workflow_name": context.workflow_name,
            "tool_policy": context.agent_profile.tool_policy,
            "guardrail_policy": context.agent_profile.guardrail_policy,
            "runtime_permissions": dict(context.permissions),
            "guardrail_instructions": context.guardrail_instructions,
            "time_context": context.time_context,
            "user_id": context.user_id,
            "tool_runtime_context": ToolRuntimeContext(
                request_id=context.request_id,
                user_id=context.user_id,
                agent_code=context.agent_profile.code,
                domain=context.agent_profile.domain,
                thread_id=context.internal_thread_id,
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
