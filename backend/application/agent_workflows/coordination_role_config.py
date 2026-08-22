"""协调工作流角色/模型/预算解析(首参 self 为 CoordinatedAgentWorkflow)。"""

from __future__ import annotations

import logging

from backend.application.agent_workflows.coordination_types import ResearchTask
from backend.application.agent_workflows.research_planning import (
    build_planner_prompt,
    fallback_research_tasks,
    parse_research_tasks,
)
from backend.application.agent_workflows.research_specialists import (
    normalize_research_specialist,
)
from backend.domain.agents.request import AgentRequest
from backend.domain.agents.retrieval_policy import RetrievalRuntimePolicy
from backend.domain.agents.runtime_agent import RuntimeAgentSpec
from backend.domain.agents.runtime_context import AgentRuntimeContext
from langchain_core.runnables import RunnableConfig
from typing import Any
from backend.application.agent_workflows.coordination_runtime_support import (
    _DEEP_RESEARCH_BRANCH_WEB_CALLS,
    _FAST_MODEL_RESEARCH_ROLES,
    _HIGH_RISK_LEVELS,
)


logger = logging.getLogger(__name__)


def _research_role(
    self,
    *,
    task: ResearchTask,
    index: int,
    used_fallback: bool,
    context: AgentRuntimeContext | None,
) -> str:
    return normalize_research_specialist(
        task.agent_type,
        agent_specs=self._runtime_agent_specs(context),
        default_index=index,
    )


def _runtime_agent_specs(
    self,
    context: AgentRuntimeContext | None = None,
) -> dict[str, RuntimeAgentSpec]:
    specs: dict[str, RuntimeAgentSpec] = {}
    specs.update(self._agent_specs)
    try:
        specs.update(self._runtime_agent_loader.load_all())
    except Exception:
        logger.debug("runtime agent manifest unavailable", exc_info=True)
    if context is not None:
        specs.update(getattr(context, "agent_specs", None) or {})
    return specs


def _role_spec(
    self,
    context: AgentRuntimeContext | None,
    role: str,
) -> RuntimeAgentSpec | None:
    return self._runtime_agent_specs(context).get(str(role or "").strip())


def _role_instruction(
    self,
    role: str,
    context: AgentRuntimeContext | None,
) -> str:
    spec = self._role_spec(context, role)
    return spec.system_prompt if spec is not None else ""


def _effective_tool_access(
    self,
    *,
    role: str,
    requested: bool,
    context: AgentRuntimeContext | None,
) -> bool:
    spec = self._role_spec(context, role)
    return bool(requested and (spec.tool_access if spec is not None else True))


def _effective_allowed_tools(
    self,
    *,
    role: str,
    requested_allowed_tools: set[str] | None,
    context: AgentRuntimeContext | None,
) -> set[str] | None:
    spec = self._role_spec(context, role)
    if spec is None:
        return (
            set(requested_allowed_tools)
            if requested_allowed_tools is not None
            else None
        )
    if spec.allowed_tools is None:
        allowed = (
            set(requested_allowed_tools)
            if requested_allowed_tools is not None
            else None
        )
    elif requested_allowed_tools is None:
        allowed = set(spec.allowed_tools)
    else:
        allowed = set(requested_allowed_tools) & set(spec.allowed_tools)
    return allowed - set(spec.disallowed_tools) if allowed is not None else None


def _role_timeout_seconds(
    self,
    role: str,
    context: AgentRuntimeContext | None,
) -> float:
    spec = self._role_spec(context, role)
    return float(
        spec.timeout_seconds
        if spec is not None
        else self._runtime_budget.unit_total_timeout_seconds
    )


def _role_model_name(
    self,
    role: str,
    context: AgentRuntimeContext,
) -> str | None:
    context_model_name = getattr(context, "model_name", None)
    spec = self._role_spec(context, role)
    if spec is not None and spec.model:
        if spec.model == "fast":
            if self.name == "deep_research":
                return (
                    self._runtime_budget.deep_research_fast_model_name
                    or context_model_name
                )
            return context_model_name
        if spec.model != "default":
            return spec.model
    return context_model_name


def _research_task_model_name(
    self,
    *,
    task: ResearchTask,
    role: str,
    context: AgentRuntimeContext,
) -> str | None:
    """Route only bounded, low-risk research branches to the fast model."""

    role_model = self._role_model_name(role, context)
    fast_model = self._runtime_budget.deep_research_fast_model_name
    if self.name != "deep_research" or not fast_model:
        return role_model
    spec = self._role_spec(context, role)
    if spec is not None and spec.model not in {None, "default", "fast"}:
        return role_model
    if self._runtime_budget.deep_research_fast_model_max_budget <= 0:
        return role_model
    if task.budget > self._runtime_budget.deep_research_fast_model_max_budget:
        return role_model
    if str(task.risk or "").strip().lower() in _HIGH_RISK_LEVELS:
        return role_model
    if role not in _FAST_MODEL_RESEARCH_ROLES:
        return role_model
    return fast_model


def _model_tier(
    self,
    model_name: str | None,
    context: AgentRuntimeContext,
) -> str:
    fast_model = self._runtime_budget.deep_research_fast_model_name
    request_metadata = getattr(context, "request_metadata", None) or {}
    primary_model = request_metadata.get(
        "coordination_primary_model_name",
        getattr(context, "model_name", None),
    )
    if fast_model and model_name == fast_model and model_name != primary_model:
        return "fast"
    return "primary"


def _role_runnable_config(
    self,
    role: str,
    context: AgentRuntimeContext,
) -> RunnableConfig | None:
    spec = self._role_spec(context, role)
    if spec is None:
        return self._runnable_config
    config: dict[str, Any] = dict(self._runnable_config or {})
    metadata = dict(config.get("metadata") or {})
    metadata.update(
        {
            "runtime_agent": spec.name,
            "runtime_agent_output_schema": spec.output_schema,
            "runtime_agent_max_turns": spec.max_turns,
        }
    )
    config["metadata"] = metadata
    # This graph has several middleware nodes per logical model/tool turn,
    # and a model may issue multiple independent searches in one turn.
    # Keep the semantic turn limit in the agent spec while allowing enough
    # graph supersteps to finish its answer. Retrieval/tool budgets still
    # provide the hard loop bound.
    config["recursion_limit"] = min(128, max(32, spec.max_turns * 8 + 8))
    return config


def _role_can_use_network(
    self,
    role: str,
    context: AgentRuntimeContext | None,
) -> bool:
    spec = self._role_spec(context, role)
    return bool(spec.can_use_network if spec is not None else True)


def _role_can_read_kb(
    self,
    role: str,
    context: AgentRuntimeContext | None,
) -> bool:
    spec = self._role_spec(context, role)
    return bool(spec.can_read_kb if spec is not None else True)


def _role_required_tools(
    self,
    role: str,
    context: AgentRuntimeContext | None,
) -> set[str]:
    spec = self._role_spec(context, role)
    return set(spec.required_tools if spec is not None else set())


def _planner_prompt(
    self, request: AgentRequest, context: AgentRuntimeContext
) -> str:
    return build_planner_prompt(
        request,
        context,
        max_tasks=self._research_llm_capacity(),
    )


def _research_llm_capacity(self) -> int:
    if self.name == "plan_solve":
        return self._runtime_budget.plan_solve_max_concurrent_subagents
    return self._runtime_budget.max_concurrent_research


def _minimum_research_branch_start_seconds(self) -> float:
    """Return the floor below which a new branch cannot make a safe handoff."""

    return max(
        0.0,
        float(self._runtime_budget.deep_research_branch_min_start_seconds),
    )


def _max_spawned_agents(self) -> int:
    """Size the tree for every bounded attempt the workflow can schedule."""

    if self.name == "deep_research":
        attempts = max(
            1,
            int(self._runtime_budget.deep_research_structured_output_attempts),
        )
        reflection_units = max(
            0,
            int(self._runtime_budget.deep_research_reflection_rounds),
        ) * (
            attempts
            + max(0, int(self._runtime_budget.deep_research_followup_tasks))
        )
        return max(
            16,
            attempts
            + int(self._runtime_budget.max_concurrent_research)
            + reflection_units
            + 1,  # final report writer
        )
    if self.name == "plan_solve":
        return max(
            16,
            int(self._runtime_budget.deep_research_structured_output_attempts)
            + int(self._runtime_budget.plan_solve_max_concurrent_subagents)
            + 1,  # reviewer
        )
    # Parallel mode currently uses five specialists plus one synthesizer;
    # alternating mode uses three sequential roles. Preserve headroom for
    # future bounded additions without sharing deep-research assumptions.
    return 16


def _coordinated_retrieval_budgets(
    self,
    context: AgentRuntimeContext,
) -> dict[str, int]:
    """Bound a request without making parallel branches share one quota."""

    if self.name != "deep_research":
        return {
            "multi_kb_search": 3,
            "web_search": 5,
            "academic_search": 5,
        }

    per_branch = RetrievalRuntimePolicy.for_mode(
        deep_research=True,
        academic_enabled=("academic-search" in set(context.selected_skills or [])),
    ).tool_budgets()
    per_branch["web_search"] = min(
        per_branch.get("web_search", _DEEP_RESEARCH_BRANCH_WEB_CALLS),
        _DEEP_RESEARCH_BRANCH_WEB_CALLS,
    )
    max_branch_executions = self._research_llm_capacity() + (
        self._runtime_budget.deep_research_reflection_rounds
        * self._runtime_budget.deep_research_followup_tasks
    )
    return {
        tool_name: calls * max(1, max_branch_executions)
        for tool_name, calls in per_branch.items()
    }


def _parse_research_tasks(
    self,
    planner_answer: str,
    original_question: str,
    context: AgentRuntimeContext | None = None,
) -> list[ResearchTask]:
    return parse_research_tasks(
        planner_answer,
        original_question,
        context,
        max_tasks=self._research_llm_capacity(),
    )


def _fallback_research_tasks(
    self,
    question: str,
    context: AgentRuntimeContext | None = None,
) -> list[ResearchTask]:
    return fallback_research_tasks(self.name, question, context)
