"""Workflow registry and resolver."""

from __future__ import annotations

import importlib

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
    ):
        workflow_name = self.resolve_name(request, context)
        if workflow_name not in self._workflow_names:
            raise ValueError(f"Unknown workflow: {workflow_name}")
        if not context.agent_profile.allows_workflow(workflow_name):
            raise ValueError(
                f"Agent {context.agent_profile.code} does not allow workflow {workflow_name}"
            )
        spec = get_workflow_spec(workflow_name)
        if spec is None:
            raise ValueError(f"Workflow is not configured: {workflow_name}")
        provider = self._load_provider(spec.provider)
        return provider(
            name=workflow_name,
            checkpointer=self._checkpointer,
            runnable_config=self._runnable_config,
        )

    def resolve_name(self, request: AgentRequest, context: AgentRuntimeContext) -> str:
        requested = normalize_workflow_name(request.requested_workflow)
        if requested in self._workflow_names:
            return requested
        skill_preferred = self._resolve_skill_preferred_workflow(request.selected_skills)
        if skill_preferred:
            return skill_preferred
        if request.selected_skills:
            return FULL_WORKFLOW
        normalized_default = normalize_workflow_name(context.agent_profile.default_workflow)
        if normalized_default in self._workflow_names:
            return normalized_default
        return LIGHTWEIGHT_WORKFLOW

    def _resolve_skill_preferred_workflow(self, selected_skills: list[str]) -> str | None:
        if not selected_skills:
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
