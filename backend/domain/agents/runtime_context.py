"""Request-scoped runtime context for agent execution."""

from __future__ import annotations

from dataclasses import dataclass, field
from typing import Any

from backend.domain.agents.profile import AgentProfile


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
    permissions: dict = field(default_factory=dict)
    guardrail_instructions: str = ""
    time_context: str = ""
    request_metadata: dict[str, Any] = field(default_factory=dict)
    domain_context: dict[str, Any] = field(default_factory=dict)
