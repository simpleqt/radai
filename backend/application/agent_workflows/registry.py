"""Workflow registry and resolver."""

from __future__ import annotations

import importlib
import inspect
from typing import TYPE_CHECKING

from langchain_core.runnables import RunnableConfig
from langgraph.checkpoint.base import BaseCheckpointSaver

from backend.application.skills.manifest_loader import SkillManifestLoader
from backend.domain.agents.request import AgentRequest
from backend.domain.agents.runtime_context import AgentRuntimeContext
from backend.domain.agents.workflows import (
    FULL_WORKFLOW,
    LIGHTWEIGHT_WORKFLOW,
    SUPPORTED_WORKFLOWS,
    get_workflow_spec,
    normalize_workflow_name,
)

if TYPE_CHECKING:
    from backend.application.agents.definition import ResolvedAgentDefinition


class WorkflowRegistry:
    def __init__(
        self,
        *,
        checkpointer: BaseCheckpointSaver | None = None,
        runnable_config: RunnableConfig | None = None,
    ):
        self._checkpointer = checkpointer
        self._runnable_config = runnable_config
        self._workflow_names = set(SUPPORTED_WORKFLOWS)

    def resolve(
        self,
        request: AgentRequest,
        context: AgentRuntimeContext,
        *,
        definition: ResolvedAgentDefinition | None = None,
    ):
        workflow_name = self.resolve_name(request, context, definition=definition)
        workflow_names = self._allowed_workflow_names(definition)
        if workflow_name not in workflow_names:
            raise ValueError(f"Unknown workflow: {workflow_name}")
        if not context.agent_profile.allows_workflow(workflow_name):
            raise ValueError(
                f"Agent {context.agent_profile.code} does not allow workflow {workflow_name}"
            )
        provider = self._resolve_provider(workflow_name, definition=definition)
        kwargs = {
            "name": workflow_name,
            "checkpointer": self._checkpointer,
            "runnable_config": self._runnable_config,
            "runtime_budget": context.runtime_budget,
            "agent_specs": dict(context.agent_specs),
            "context_policy": context.context_policy,
        }
        if definition is not None and self._accepts_resolved_definition(provider):
            kwargs["resolved_definition"] = definition
        return provider(**kwargs)

    def resolve_name(
        self,
        request: AgentRequest,
        context: AgentRuntimeContext,
        *,
        definition: ResolvedAgentDefinition | None = None,
    ) -> str:
        workflow_names = self._allowed_workflow_names(definition)
        raw_requested = str(request.requested_workflow or "").strip()
        requested = self._normalize_workflow_name(
            request.requested_workflow,
            definition=definition,
        )
        if raw_requested:
            if requested in workflow_names:
                return requested
            raise ValueError(f"Unknown requested workflow: {raw_requested}")
        skills_control_workflow = (
            request.skills_requested_for_workflow
            if request.skills_requested_for_workflow is not None
            else bool(
                request.metadata.get(
                    "_skills_requested_for_workflow",
                    bool(request.selected_skills),
                )
            )
        )
        if skills_control_workflow:
            skill_preferred = self._resolve_skill_preferred_workflow(
                request.selected_skills,
                definition=definition,
            )
            normalized_preferred = self._normalize_workflow_name(
                skill_preferred,
                definition=definition,
            )
            if normalized_preferred in workflow_names:
                return normalized_preferred
            if FULL_WORKFLOW in workflow_names:
                return FULL_WORKFLOW
            if skill_preferred:
                raise ValueError(
                    f"Selected Skill prefers unknown workflow: {skill_preferred}"
                )
            raise ValueError(
                f"Agent {context.agent_profile.code} has no full-capability workflow"
            )
        normalized_default = self._normalize_workflow_name(
            context.agent_profile.default_workflow,
            definition=definition,
        )
        if normalized_default in workflow_names:
            return normalized_default
        if LIGHTWEIGHT_WORKFLOW in workflow_names:
            return LIGHTWEIGHT_WORKFLOW
        raise ValueError(
            f"Agent {context.agent_profile.code} has no resolvable default workflow"
        )

    def _resolve_skill_preferred_workflow(
        self,
        selected_skills: list[str] | None,
        *,
        definition: ResolvedAgentDefinition | None = None,
    ) -> str | None:
        if not selected_skills:
            return None
        if definition is not None:
            manifests = {manifest.name: manifest for manifest in definition.skills}
            for skill_name in selected_skills:
                manifest = manifests.get(skill_name)
                if manifest is None and definition.runtime_scope is not None:
                    try:
                        manifest = definition.runtime_scope.skill_loader.load(
                            skill_name
                        )
                    except ValueError:
                        continue
                if manifest is not None and manifest.preferred_workflow:
                    return manifest.preferred_workflow
            return None
        loader = SkillManifestLoader()
        for skill_name in selected_skills:
            try:
                manifest = loader.load(skill_name)
            except ValueError:
                continue
            if manifest.preferred_workflow:
                return manifest.preferred_workflow
        return None

    def resolve_skill_preferred_workflow(
        self,
        selected_skills: list[str] | None,
        *,
        definition: ResolvedAgentDefinition | None = None,
    ) -> str | None:
        """Resolve a selected Skill's workflow before managed binding filtering."""
        return self._resolve_skill_preferred_workflow(
            selected_skills,
            definition=definition,
        )

    def _allowed_workflow_names(
        self,
        definition: ResolvedAgentDefinition | None,
    ) -> set[str]:
        if definition is None:
            return set(self._workflow_names)
        return {spec.name for spec in definition.workflows}

    @staticmethod
    def _normalize_workflow_name(
        workflow_name: str | None,
        *,
        definition: ResolvedAgentDefinition | None,
    ) -> str:
        if definition is None:
            return normalize_workflow_name(workflow_name)
        normalized = str(workflow_name or "").strip().lower()
        aliases = {
            alias.strip().lower(): spec.name
            for spec in definition.workflows
            for alias in (set(spec.aliases) | {spec.name})
            if alias.strip()
        }
        return aliases.get(normalized, normalized)

    def _resolve_provider(
        self,
        workflow_name: str,
        *,
        definition: ResolvedAgentDefinition | None,
    ):
        if definition is not None:
            provider = dict(definition.workflow_providers).get(workflow_name)
            if provider is not None:
                return provider
        spec = get_workflow_spec(workflow_name)
        if spec is None:
            raise ValueError(f"Workflow is not configured: {workflow_name}")
        return self._load_provider(spec.provider)

    @staticmethod
    def _accepts_resolved_definition(provider) -> bool:
        try:
            parameters = inspect.signature(provider).parameters.values()
        except (TypeError, ValueError):
            return False
        return any(
            parameter.name == "resolved_definition"
            or parameter.kind is inspect.Parameter.VAR_KEYWORD
            for parameter in parameters
        )

    def _load_provider(self, provider_path: str):
        module_name, sep, attr_name = provider_path.partition(":")
        if not sep or not module_name or not attr_name:
            raise ValueError(f"Invalid workflow provider: {provider_path}")
        module = importlib.import_module(module_name)
        provider = getattr(module, attr_name)
        if not callable(provider):
            raise ValueError(f"Workflow provider is not callable: {provider_path}")
        return provider


def get_workflow_registry(
    *,
    checkpointer: BaseCheckpointSaver | None = None,
    runnable_config: RunnableConfig | None = None,
) -> WorkflowRegistry:
    return WorkflowRegistry(checkpointer=checkpointer, runnable_config=runnable_config)
