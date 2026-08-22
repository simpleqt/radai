"""Request-scoped runtime context for agent execution."""

from __future__ import annotations

from dataclasses import dataclass, field
from typing import Any

from backend.domain.agents.profile import AgentProfile
from backend.domain.agents.runtime_agent import RuntimeAgentSpec
from backend.domain.agents.runtime_bindings import AgentRuntimeBindings
from backend.domain.agents.runtime_budget import AgentRuntimeBudget


@dataclass(frozen=True)
class AgentRuntimeContext:
    request_id: str
    user_id: str
    external_thread_id: str
    internal_thread_id: str
    agent_profile: AgentProfile
    workflow_name: str
    model_name: str | None
    kb_ids: list[str] = field(default_factory=list)
    selected_skills: list[str] = field(default_factory=list)
    use_search: bool = True
    subagent_enabled: bool = False
    thinking_enabled: bool = True
    deep_retrieval: bool = False
    permissions: dict = field(default_factory=dict)
    guardrail_instructions: str = ""
    time_context: str = ""
    request_metadata: dict[str, Any] = field(default_factory=dict)
    domain_context: dict[str, Any] = field(default_factory=dict)
    runtime_budget: AgentRuntimeBudget | None = None
    run_id: str = ""
    trace_enabled: bool = False
    agent_specs: dict[str, RuntimeAgentSpec] = field(default_factory=dict)
    context_policy: Any | None = None
    agent_bindings: AgentRuntimeBindings | None = None
