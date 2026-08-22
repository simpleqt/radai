"""Application service for standardized agent execution."""

from __future__ import annotations

import logging
import os
import uuid
from collections.abc import AsyncIterator
from dataclasses import replace
from datetime import datetime
from typing import Any

from langchain_core.runnables import RunnableConfig
from langgraph.checkpoint.base import BaseCheckpointSaver

from backend.application.agent_errors import public_agent_error
from backend.application.agent_workflows.registry import WorkflowRegistry
from backend.application.agents.definition import (
    AgentDefinitionValidator,
    ResolvedAgentDefinition,
)
from backend.application.agents.profile_loader import AgentProfileLoader
from backend.application.agents.runtime_agent_loader import RuntimeAgentLoader
from backend.application.blocking import run_blocking
from backend.application.conversations import ConversationStateError, ConversationStore
from backend.application.domain_context import DomainContextResolver
from backend.application.policies import ProfileRuntimePolicy
from backend.application.skills.selection_closure import (
    UnmanagedSkillClosureResolver,
)
from backend.core.agents.context import ContextPolicy
from backend.core.thread_key import build_internal_thread_key
from backend.domain.agents.events import AgentEvent
from backend.domain.agents.profile import AgentProfile
from backend.domain.agents.request import AgentRequest
from backend.domain.agents.runtime_bindings import AgentRuntimeBindings
from backend.domain.agents.runtime_budget import AgentRuntimeBudget
from backend.domain.agents.runtime_context import AgentRuntimeContext
from backend.domain.agents.workflows import SUBAGENT_WORKFLOWS

logger = logging.getLogger(__name__)


def _allow_skill_resolution_fallback(request: AgentRequest) -> bool:
    if request.allow_skill_resolution_fallback:
        return True
    if request.auth_role is None and request.selected_skills is None:
        return True
    raw = os.getenv("AGENT_SKILL_RESOLUTION_FALLBACK", "").strip().lower()
    return raw in {"1", "true", "yes", "on"}


def _env_flag(name: str) -> bool:
    return os.getenv(name, "").strip().lower() in {"1", "true", "yes", "on"}


def _request_trace_enabled(request: AgentRequest) -> bool:
    if _env_flag("AGENT_TRACE_ENABLED"):
        return True
    # Private metadata is used by in-process callers. Public HTTP transports
    # strip underscore-prefixed keys before constructing AgentRequest.
    value = dict(request.metadata or {}).get("_trace_enabled")
    if isinstance(value, bool):
        return value
    if isinstance(value, str):
        return value.strip().lower() in {"1", "true", "yes", "on"}
    return False


class AgentService:
    @staticmethod
    def _derive_auth_role_for_request(*, user_id: str | None, auth_role: str | None) -> str:
        """运行时身份归一：入口漏传/传错时兜底。

        访客主体（guest_* 用户键）无论 auth_role 传什么一律归一为
        guest——写工具守卫（tool_write_requires_authenticated_user）
        按此身份拒绝 risk_level=write 工具。
        """
        derived = str(auth_role or "user").strip().lower() or "user"
        if str(user_id or "").startswith("guest_") and derived not in {
            "guest",
            "anonymous",
        }:
            derived = "guest"
        return derived

    def __init__(
        self,
        *,
        profile_loader: AgentProfileLoader | None = None,
        checkpointer: BaseCheckpointSaver | None = None,
        runnable_config: RunnableConfig | None = None,
        conversation_store: ConversationStore | None = None,
        domain_context_resolver: DomainContextResolver | None = None,
        runtime_policy: ProfileRuntimePolicy | None = None,
        runtime_agent_loader: RuntimeAgentLoader | None = None,
        definition_validator: AgentDefinitionValidator | None = None,
    ):
        self._profile_loader = profile_loader or AgentProfileLoader()
        # Production callers resolve the full cross-registry contract. Tests
        # and extension hosts that deliberately inject a profile loader retain
        # their isolated contract unless they also inject a validator.
        self._definition_validator = definition_validator or (
            AgentDefinitionValidator(profile_loader=self._profile_loader)
            if profile_loader is None
            else None
        )
        self._conversation_store = conversation_store or ConversationStore()
        self._domain_context_resolver = (
            domain_context_resolver or DomainContextResolver()
        )
        self._runtime_policy = runtime_policy or ProfileRuntimePolicy()
        self._runtime_agent_loader = runtime_agent_loader or RuntimeAgentLoader()
        self._workflow_registry = WorkflowRegistry(
            checkpointer=checkpointer,
            runnable_config=runnable_config,
        )

    async def run_stream(self, request: AgentRequest) -> AsyncIterator[AgentEvent]:
        try:
            request = await self.resolve_request_skills(request)
            workflow, context = await self.resolve_workflow(request)
        except Exception as exc:  # noqa: BLE001
            logger.exception("[AgentService] request preparation failed")
            public_message, error_code = public_agent_error(exc)
            request_id = str(uuid.uuid4())
            thread_id = request.conversation_id or str(uuid.uuid4())
            yield AgentEvent(
                type="error",
                request_id=request_id,
                thread_id=thread_id,
                sequence=0,
                payload={
                    "message": public_message,
                    "error": public_message,
                    "error_code": error_code,
                },
            )
            yield AgentEvent(
                type="done",
                request_id=request_id,
                thread_id=thread_id,
                sequence=1,
                payload={},
            )
            return

        try:
            await self._conversation_store.ensure_runtime_conversation(
                user_id=context.user_id,
                thread_id=context.external_thread_id,
                question=request.question,
                agent_code=context.agent_profile.code,
                workflow_name=context.workflow_name,
                metadata={
                    key: value
                    for key, value in dict(request.metadata or {}).items()
                    # `_` 前缀为 in-process 私有键（图片上下文、上传文件清单等），
                    # 只供本轮 prompt 使用，不进持久化会话 metadata。
                    if not str(key).startswith("_")
                },
            )
        except ConversationStateError as exc:
            logger.warning(
                "[AgentService] 拒绝复用不安全会话: user_id=%s thread_id=%s agent=%s reason=%s",
                context.user_id,
                context.external_thread_id,
                context.agent_profile.code,
                exc,
            )
            yield AgentEvent(
                type="error",
                request_id=context.request_id,
                thread_id=context.external_thread_id,
                sequence=0,
                payload={
                    "message": "该会话已删除或属于其他智能体，请新建会话后重试",
                    "error": "该会话已删除或属于其他智能体，请新建会话后重试",
                    "error_code": "conversation_state_conflict",
                },
            )
            yield AgentEvent(
                type="done",
                request_id=context.request_id,
                thread_id=context.external_thread_id,
                sequence=1,
                payload={},
            )
            return
        except Exception:
            logger.exception(
                "[AgentService] 会话记录保存失败，拒绝执行工作流: user_id=%s thread_id=%s agent=%s workflow=%s",
                context.user_id,
                context.external_thread_id,
                context.agent_profile.code,
                context.workflow_name,
            )
            yield AgentEvent(
                type="error",
                request_id=context.request_id,
                thread_id=context.external_thread_id,
                sequence=0,
                payload={
                    "message": "会话服务暂不可用，请稍后重试",
                    "error": "会话服务暂不可用，请稍后重试",
                    "error_code": "conversation_persistence_unavailable",
                },
            )
            yield AgentEvent(
                type="done",
                request_id=context.request_id,
                thread_id=context.external_thread_id,
                sequence=1,
                payload={},
            )
            return
        from backend.application.agent_executors.image_url_registry import (
            get_registry,
        )

        # Keep the exact mutable registry used by this run. Looking the
        # ContextVar up again in finally can yield a different registry when
        # execution crossed an async task/context boundary.
        image_registry = get_registry()
        try:
            load_images = getattr(
                self._conversation_store, "get_registered_images", None
            )
            registered_images = (
                await load_images(
                    user_id=context.user_id,
                    thread_id=context.external_thread_id,
                )
                if callable(load_images)
                else {}
            )
            image_registry.replace_persisted(registered_images)
        except Exception:
            # The conversation itself was already validated above.  A legacy
            # catalog without image metadata must not block ordinary text QA.
            logger.warning(
                "[AgentService] Failed to restore conversation image registry: "
                "user_id=%s thread_id=%s",
                context.user_id,
                context.external_thread_id,
                exc_info=True,
            )
            image_registry.replace_persisted({})
        try:
            async for event in workflow.run(request, context):
                yield event
        finally:
            try:
                if not image_registry.is_empty():
                    images_dict = {
                        token: entry.to_metadata()
                        for token, entry in image_registry.iter_entries()
                    }
                    await self._conversation_store.update_conversation_images(
                        user_id=context.user_id,
                        thread_id=context.external_thread_id,
                        registered_images=images_dict,
                    )
            except Exception:
                logger.exception(
                    "[AgentService] Failed to persist registered images to conversation metadata"
                )

    async def resolve_request_skills(self, request: AgentRequest) -> AgentRequest:
        """Resolve database-managed Skill bindings before any workflow is built."""
        if request.skills_resolved:
            return request
        definition = (
            self._definition_validator.resolve(request.agent_code)
            if self._definition_validator is not None
            else None
        )
        profile = (
            definition.profile
            if definition is not None
            else self._profile_loader.load(request.agent_code)
        )
        profile_default_skills = {
            str(skill).strip()
            for skill in (profile.default_skills or [])
            if str(skill or "").strip()
        }
        explicitly_selected_skills = {
            str(skill).strip()
            for skill in (request.selected_skills or [])
            if str(skill or "").strip()
        }
        # UIs commonly echo the profile's default Skill. That must not be
        # treated as a user request to lock the workflow to ``full``.
        skills_requested_for_workflow = bool(
            explicitly_selected_skills - profile_default_skills
        )
        workflow = request.requested_workflow
        if not workflow and request.selected_skills:
            workflow = (
                self._workflow_registry.resolve_skill_preferred_workflow(
                    request.selected_skills,
                    definition=definition,
                )
                or "full"
            )
        workflow = workflow or profile.default_workflow
        workflow = self._canonical_workflow_name(workflow, definition=definition)
        selected = (
            list(profile.default_skills)
            if request.selected_skills is None
            else list(request.selected_skills)
        )
        resolution = None
        try:
            from backend.application.skills.runtime_resolver import SkillRuntimeResolver
            from backend.db.connection import get_async_session_factory

            factory = get_async_session_factory()
            async with factory() as session:
                resolution = await SkillRuntimeResolver().resolve(
                    session,
                    user_id=request.user_id,
                    role=request.auth_role,
                    agent_code=profile.code,
                    agent_domain=profile.domain,
                    workflow=workflow,
                    requested_skills=request.selected_skills,
                    fallback_skills=list(profile.default_skills),
                )
        except Exception as exc:
            from backend.application.skills.runtime_resolver import SkillRuntimeError

            if isinstance(exc, SkillRuntimeError):
                raise
            if not _allow_skill_resolution_fallback(request):
                logger.exception(
                    "[AgentService] Skill catalog resolution failed; refusing compatibility fallback"
                )
                raise RuntimeError(
                    "Skill catalog resolution failed; refusing unsafe profile fallback"
                ) from exc
            logger.warning(
                "[AgentService] Skill catalog unavailable, using profile compatibility mode: %s",
                exc,
            )
        effective_selected = resolution.selected_skills if resolution else selected
        managed_skills = bool(resolution and resolution.managed)
        required_skills = (
            {item.name for item in resolution.available_skills if item.required}
            if managed_skills and resolution is not None
            else set(profile_default_skills)
        )
        if definition is not None and not managed_skills:
            effective_selected = self._resolve_unmanaged_skill_closure(
                definition,
                selected_skills=effective_selected,
                workflow_name=workflow,
            )
        effective_skill_set = {
            str(skill).strip()
            for skill in (effective_selected or [])
            if str(skill or "").strip()
        }
        default_skill_closure = (
            {manifest.name for manifest in definition.skills}
            if definition is not None
            else profile_default_skills
        )
        resolved_skills_control_workflow = skills_requested_for_workflow or bool(
            effective_skill_set - default_skill_closure
        )
        metadata = dict(request.metadata)
        metadata["_skills_resolved"] = True
        metadata["_managed_skills_resolved"] = managed_skills
        metadata["_skills_requested_for_workflow"] = resolved_skills_control_workflow
        metadata["_required_skills"] = sorted(required_skills)
        return replace(
            request,
            selected_skills=effective_selected,
            metadata=metadata,
            skills_resolved=True,
            managed_skills_resolved=managed_skills,
            skills_requested_for_workflow=resolved_skills_control_workflow,
        )

    def build_executor(self, request: AgentRequest):
        """Build the workflow executor for internal state operations."""
        workflow, context = self._resolve_workflow(request)
        build_executor = getattr(workflow, "build_executor", None)
        if not callable(build_executor):
            raise ValueError(
                f"Workflow {context.workflow_name} does not expose an executor"
            )
        return build_executor(request, context)

    async def resolve_workflow(self, request: AgentRequest):
        """Resolve policy and workflow without blocking the event-loop thread."""
        return await run_blocking(self._resolve_workflow, request)

    async def build_executor_async(self, request: AgentRequest):
        """Async boundary for callers that need a workflow executor."""
        workflow, context = await self.resolve_workflow(request)
        build_executor = getattr(workflow, "build_executor", None)
        if not callable(build_executor):
            raise ValueError(
                f"Workflow {context.workflow_name} does not expose an executor"
            )
        # Executor construction compiles the graph, assembles tools, and reads
        # prompt/skill files; keep that blocking work off the event loop, the
        # same way the streaming workflow path does.
        return await run_blocking(build_executor, request, context)

    def _resolve_workflow(self, request: AgentRequest):
        definition = (
            self._definition_validator.resolve(request.agent_code)
            if self._definition_validator is not None
            else None
        )
        if definition is not None:
            profile, agent_bindings = self._project_runtime_bindings(definition)
        else:
            profile, agent_bindings = (
                self._profile_loader.load(request.agent_code),
                None,
            )
        request_id = str(uuid.uuid4())
        external_thread_id = request.conversation_id or str(uuid.uuid4())
        runtime_budget = AgentRuntimeBudget.snapshot()
        agent_specs = self._runtime_agent_loader.load_all()
        effective_model_name = request.model_name or profile.default_model
        if not effective_model_name:
            from backend.core.agents.config_compat import get_default_model_name

            effective_model_name = get_default_model_name()
        context_policy = ContextPolicy.from_env(effective_model_name)
        domain_context_kwargs: dict[str, Any] = {
            "profile": profile,
            "request": request,
        }
        if definition is not None and definition.domain_context_provider is not None:
            domain_context_kwargs["provider"] = definition.domain_context_provider
        domain_context = self._domain_context_resolver.resolve(
            **domain_context_kwargs,
        )
        policy_result = self._runtime_policy.evaluate(
            user_id=request.user_id,
            requested_kb_ids=list(request.selected_kb_ids),
            profile=profile,
            domain_context=domain_context,
        )
        permissions = dict(policy_result.permissions)
        # Carry the authenticated role as server-side runtime state.  This is
        # deliberately not taken from request metadata so tools can enforce
        # role-aware ACLs without trusting model-controlled arguments.
        permissions["auth_role"] = self._derive_auth_role_for_request(
            user_id=request.user_id, auth_role=request.auth_role
        )
        permissions["managed_skills_resolved"] = request.managed_skills_resolved
        preliminary_workflow_name = (
            request.requested_workflow or profile.default_workflow
        )
        initial_context = self._build_context(
            request,
            profile=profile,
            workflow_name=preliminary_workflow_name,
            request_id=request_id,
            external_thread_id=external_thread_id,
            domain_context=domain_context,
            kb_ids=policy_result.kb_ids,
            permissions=permissions,
            guardrail_instructions=policy_result.guardrail_instructions,
            runtime_budget=runtime_budget,
            agent_specs=agent_specs,
            context_policy=context_policy,
            agent_bindings=agent_bindings,
        )
        workflow_name = self._workflow_registry.resolve_name(
            request,
            initial_context,
            definition=definition,
        )
        resolved_selected_skills: list[str] | None = None
        if definition is not None and not request.managed_skills_resolved:
            unresolved_selection = (
                list(profile.default_skills)
                if request.selected_skills is None
                else list(request.selected_skills)
            )
            resolved_selected_skills = self._resolve_unmanaged_skill_closure(
                definition,
                selected_skills=unresolved_selection,
                workflow_name=workflow_name,
            )
        if workflow_name == preliminary_workflow_name and resolved_selected_skills is None:
            # 常见路径:名称解析结果与预解析一致且没有技能闭包重解析,
            # 第一次构建的上下文即最终上下文,避免整套派生逻辑跑第二遍。
            context = initial_context
        else:
            context = self._build_context(
                request,
                profile=profile,
                workflow_name=workflow_name,
                request_id=request_id,
                external_thread_id=external_thread_id,
                domain_context=domain_context,
                kb_ids=policy_result.kb_ids,
                permissions=permissions,
                guardrail_instructions=policy_result.guardrail_instructions,
                runtime_budget=runtime_budget,
                agent_specs=agent_specs,
                context_policy=context_policy,
                agent_bindings=agent_bindings,
                resolved_selected_skills=resolved_selected_skills,
            )
        workflow = self._workflow_registry.resolve(
            request,
            context,
            definition=definition,
        )
        return workflow, context

    def _load_profile(self, agent_code: str) -> AgentProfile:
        profile, _ = self._load_profile_and_bindings(agent_code)
        return profile

    def _load_profile_and_bindings(
        self,
        agent_code: str,
    ) -> tuple[AgentProfile, AgentRuntimeBindings | None]:
        if self._definition_validator is not None:
            definition = self._definition_validator.resolve(agent_code)
            return self._project_runtime_bindings(definition)
        return self._profile_loader.load(agent_code), None

    @staticmethod
    def _project_runtime_bindings(
        definition: ResolvedAgentDefinition,
    ) -> tuple[AgentProfile, AgentRuntimeBindings]:
        profile = definition.profile
        return profile, AgentRuntimeBindings(
            agent_code=definition.profile.code,
            domain=definition.profile.domain,
            prompt_profile=definition.prompt_spec.name,
            tool_policy=definition.tool_policy.name,
            guardrail_policy=definition.profile.guardrail_policy,
            default_workflow=definition.profile.default_workflow,
            workflow_names=tuple(spec.name for spec in definition.workflows),
            tool_names=tuple(spec.name for spec in definition.tools),
            skill_names=tuple(manifest.name for manifest in definition.skills),
            skill_dependencies=tuple(
                (manifest.name, tuple(sorted(set(manifest.dependencies))))
                for manifest in definition.skills
            ),
            domain_context_provider=definition.profile.domain_context_provider,
        )

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
        runtime_budget: AgentRuntimeBudget | None = None,
        agent_specs: dict[str, Any] | None = None,
        context_policy: ContextPolicy | None = None,
        agent_bindings: AgentRuntimeBindings | None = None,
        resolved_selected_skills: list[str] | None = None,
    ) -> AgentRuntimeContext:
        effective_kb_ids = list(
            kb_ids
            if kb_ids is not None
            else request.selected_kb_ids or profile.default_kb_ids
        )
        requested_skills = (
            list(resolved_selected_skills)
            if resolved_selected_skills is not None
            else list(
                profile.default_skills
                if request.selected_skills is None
                else request.selected_skills
            )
        )
        if resolved_selected_skills is not None or bool(
            (permissions or {}).get("managed_skills_resolved")
        ):
            selected_skills = self._dedupe_skill_names(requested_skills)
        else:
            # Preserve isolated extension-host compatibility. Production
            # definitions always pass a closure resolved from runtime_scope.
            selected_skills = self._expand_selected_skill_dependencies(
                requested_skills,
                agent_bindings=agent_bindings,
            )
        model_name = request.model_name or profile.default_model
        if not model_name:
            from backend.core.agents.config_compat import get_default_model_name

            model_name = get_default_model_name()
        project_delegation_enabled = (
            profile.domain == "groundwater-project-analysis" and workflow_name == "full"
        )
        return AgentRuntimeContext(
            request_id=request_id,
            user_id=request.user_id,
            external_thread_id=external_thread_id,
            internal_thread_id=build_internal_thread_key(
                request.user_id, external_thread_id
            ),
            agent_profile=profile,
            workflow_name=workflow_name,
            model_name=model_name,
            kb_ids=effective_kb_ids,
            selected_skills=selected_skills,
            use_search=request.use_search,
            subagent_enabled=(
                request.subagent_enabled
                or workflow_name in SUBAGENT_WORKFLOWS
                or project_delegation_enabled
            ),
            thinking_enabled=request.thinking_enabled,
            deep_retrieval=request.deep_retrieval,
            permissions=dict(permissions or {}),
            guardrail_instructions=guardrail_instructions,
            time_context=self._build_runtime_time_context(),
            request_metadata=dict(request.metadata or {}),
            domain_context=dict(domain_context or {}),
            runtime_budget=runtime_budget,
            run_id=request_id,
            trace_enabled=_request_trace_enabled(request),
            agent_specs=dict(agent_specs or {}),
            context_policy=context_policy,
            agent_bindings=agent_bindings,
        )

    @staticmethod
    def _canonical_workflow_name(
        workflow_name: str,
        *,
        definition: ResolvedAgentDefinition | None,
    ) -> str:
        normalized = str(workflow_name or "").strip().lower()
        if definition is None:
            return normalized
        aliases = {
            str(alias or "").strip().lower(): spec.name
            for spec in definition.workflows
            for alias in (set(spec.aliases) | {spec.name})
            if str(alias or "").strip()
        }
        return aliases.get(normalized, normalized)

    @staticmethod
    def _resolve_unmanaged_skill_closure(
        definition: ResolvedAgentDefinition,
        *,
        selected_skills: list[str],
        workflow_name: str,
    ) -> list[str]:
        scope = definition.runtime_scope
        if scope is None:
            raise RuntimeError(
                f"Resolved agent definition has no runtime registry scope: "
                f"{definition.profile.code}"
            )
        return UnmanagedSkillClosureResolver(
            skill_loader=scope.skill_loader,
            tool_registry=scope.tool_registry,
        ).resolve(
            profile=definition.profile,
            tool_policy=definition.tool_policy,
            workflow=str(workflow_name or "").strip().lower(),
            selected_skills=selected_skills,
            trusted_manifests=definition.skills,
        )

    @staticmethod
    def _dedupe_skill_names(selected_skills: list[str]) -> list[str]:
        deduped: list[str] = []
        seen: set[str] = set()
        for raw_name in selected_skills:
            name = str(raw_name or "").strip()
            if name and name not in seen:
                seen.add(name)
                deduped.append(name)
        return deduped

    @staticmethod
    def _expand_selected_skill_dependencies(
        selected_skills: list[str],
        *,
        agent_bindings: AgentRuntimeBindings | None,
    ) -> list[str]:
        """Expand resolved Skill dependencies without widening authorization.

        A definition binding contains only its validated Skill closure. Explicit
        request selections keep their existing compatibility semantics, while
        automatically added dependencies are constrained to that closure.
        """
        expanded: list[str] = []
        selected: set[str] = set()
        for raw_name in selected_skills:
            name = str(raw_name or "").strip()
            if name and name not in selected:
                selected.add(name)
                expanded.append(name)

        # In particular, an explicit [] must not be replaced with profile
        # defaults or with every Skill known by the resolved definition.
        if not expanded or agent_bindings is None:
            return expanded

        definition_skills = set(agent_bindings.skill_names)
        dependency_map = {
            skill_name: tuple(dependencies)
            for skill_name, dependencies in agent_bindings.skill_dependencies
            if skill_name in definition_skills
        }

        def add_dependencies(skill_name: str) -> None:
            for dependency in dependency_map.get(skill_name, ()):
                # Bindings are a trust boundary. Even a manually constructed or
                # stale binding cannot silently grant a Skill outside the
                # validator-resolved definition.
                if dependency not in definition_skills or dependency in selected:
                    continue
                selected.add(dependency)
                expanded.append(dependency)
                add_dependencies(dependency)

        for skill_name in tuple(expanded):
            if skill_name in definition_skills:
                add_dependencies(skill_name)
        return expanded

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
