"""Application service for standardized agent execution."""

from __future__ import annotations

import logging
import uuid
from datetime import datetime
from typing import Any, AsyncIterator

from langchain_core.runnables import RunnableConfig
from langgraph.checkpoint.base import BaseCheckpointSaver

from backend.application.agents.profile_loader import AgentProfileLoader
from backend.application.agent_workflows.registry import WorkflowRegistry
from backend.application.blocking import run_blocking
from backend.application.conversations import ConversationStore
from backend.application.domain_context import DomainContextResolver
from backend.application.policies import ProfileRuntimePolicy
from backend.core.agents.thread_key import build_internal_thread_key
from backend.domain.agents.events import AgentEvent
from backend.domain.agents.request import AgentRequest
from backend.domain.agents.runtime_context import AgentRuntimeContext
from backend.domain.agents.workflows import SUBAGENT_WORKFLOWS

logger = logging.getLogger(__name__)


class AgentService:
    def __init__(
        self,
        *,
        profile_loader: AgentProfileLoader | None = None,
        checkpointer: BaseCheckpointSaver | None = None,
        runnable_config: RunnableConfig | None = None,
        conversation_store: ConversationStore | None = None,
        domain_context_resolver: DomainContextResolver | None = None,
        runtime_policy: ProfileRuntimePolicy | None = None,
    ):
        self._profile_loader = profile_loader or AgentProfileLoader()
        self._conversation_store = conversation_store or ConversationStore()
        self._domain_context_resolver = domain_context_resolver or DomainContextResolver()
        self._runtime_policy = runtime_policy or ProfileRuntimePolicy()
        self._workflow_registry = WorkflowRegistry(
            checkpointer=checkpointer,
            runnable_config=runnable_config,
        )

    async def run_stream(self, request: AgentRequest) -> AsyncIterator[AgentEvent]:
        workflow, context = await run_blocking(self._resolve_workflow, request)
        try:
            await self._conversation_store.ensure_runtime_conversation(
                user_id=context.user_id,
                thread_id=context.external_thread_id,
                question=request.question,
                agent_code=context.agent_profile.code,
                workflow_name=context.workflow_name,
                metadata=request.metadata,
            )
        except Exception:
            logger.exception(
                "[AgentService] 会话记录保存失败，继续执行流式响应: user_id=%s thread_id=%s agent=%s workflow=%s",
                context.user_id,
                context.external_thread_id,
                context.agent_profile.code,
                context.workflow_name,
            )
        async for event in workflow.run(request, context):
            yield event

    def build_executor(self, request: AgentRequest):
        """Build the workflow executor for internal state operations."""
        workflow, context = self._resolve_workflow(request)
        build_executor = getattr(workflow, "build_executor", None)
        if not callable(build_executor):
            raise ValueError(f"Workflow {context.workflow_name} does not expose an executor")
        return build_executor(request, context)

    def _resolve_workflow(self, request: AgentRequest):
        profile = self._profile_loader.load(request.agent_code)
        request_id = str(uuid.uuid4())
        external_thread_id = request.conversation_id or str(uuid.uuid4())
        domain_context = self._domain_context_resolver.resolve(
            profile=profile,
            request=request,
        )
        policy_result = self._runtime_policy.evaluate(
            user_id=request.user_id,
            requested_kb_ids=list(request.selected_kb_ids),
            profile=profile,
            domain_context=domain_context,
        )
        initial_context = self._build_context(
            request,
            profile=profile,
            workflow_name=request.requested_workflow or profile.default_workflow,
            request_id=request_id,
            external_thread_id=external_thread_id,
            domain_context=domain_context,
            kb_ids=policy_result.kb_ids,
            permissions=policy_result.permissions,
            guardrail_instructions=policy_result.guardrail_instructions,
        )
        workflow_name = self._workflow_registry.resolve_name(request, initial_context)
        context = self._build_context(
            request,
            profile=profile,
            workflow_name=workflow_name,
            request_id=request_id,
            external_thread_id=external_thread_id,
            domain_context=domain_context,
            kb_ids=policy_result.kb_ids,
            permissions=policy_result.permissions,
            guardrail_instructions=policy_result.guardrail_instructions,
        )
        workflow = self._workflow_registry.resolve(request, context)
        return workflow, context

    def _build_context(
        self,
        request: AgentRequest,
        *,
        profile,
        workflow_name: str,
        request_id: str,
        external_thread_id: str,
        domain_context: dict[str, Any] | None = None,
        kb_ids: list[str] | None = None,
        permissions: dict[str, Any] | None = None,
        guardrail_instructions: str = "",
    ) -> AgentRuntimeContext:
        effective_kb_ids = list(
            kb_ids if kb_ids is not None else request.selected_kb_ids or profile.default_kb_ids
        )
        selected_skills = list(request.selected_skills or profile.default_skills)
        model_name = request.model_name or profile.default_model
        return AgentRuntimeContext(
            request_id=request_id,
            user_id=request.user_id,
            external_thread_id=external_thread_id,
            internal_thread_id=build_internal_thread_key(request.user_id, external_thread_id),
            agent_profile=profile,
            workflow_name=workflow_name,
            model_name=model_name,
            kb_ids=effective_kb_ids,
            selected_skills=selected_skills,
            use_search=request.use_search,
            subagent_enabled=request.subagent_enabled or workflow_name in SUBAGENT_WORKFLOWS,
            thinking_enabled=request.thinking_enabled,
            permissions=dict(permissions or {}),
            guardrail_instructions=guardrail_instructions,
            time_context=self._build_runtime_time_context(),
            request_metadata=dict(request.metadata or {}),
            domain_context=dict(domain_context or {}),
        )

    def _build_runtime_time_context(self) -> str:
        now = datetime.now().astimezone()
        return (
            "<runtime_current_time>\n"
            f"- now_iso: {now.isoformat()}\n"
            f"- now_local: {now.strftime('%Y年%m月%d日 %H:%M:%S %Z')}\n"
            f"- current_year: {now.year}\n"
            "- rule: 涉及最新、今年、近期的问题必须以当前时间为准。\n"
            "</runtime_current_time>"
        )
