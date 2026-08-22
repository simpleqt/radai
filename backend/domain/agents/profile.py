"""Agent profile contracts.

Profiles describe what an agent is. They intentionally do not describe a
single request execution; that belongs in AgentRuntimeContext.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from typing import Any

from backend.domain.agents.workflows import SUPPORTED_WORKFLOWS


@dataclass(frozen=True)
class AgentProfile:
    code: str
    name: str
    domain: str
    description: str = ""
    default_model: str | None = None
    default_kb_ids: list[str] = field(default_factory=list)
    default_skills: list[str] = field(default_factory=list)
    default_workflow: str = "lightweight"
    allowed_workflows: list[str] = field(default_factory=lambda: list(SUPPORTED_WORKFLOWS))
    prompt_profile: str = "default"
    tool_policy: str = "default"
    kb_policy: str = "bound_or_selected"
    guardrail_policy: str = "default"
    ui_profile: str | None = None
    domain_context_provider: str | None = None
    stream_timeout_policy: dict[str, Any] | None = None

    def allows_workflow(self, workflow_name: str) -> bool:
        return workflow_name in set(self.allowed_workflows)
