"""Standard application-level agent request."""

from __future__ import annotations

from dataclasses import dataclass, field
from typing import Any


@dataclass(frozen=True)
class AgentRequest:
    question: str
    agent_code: str
    user_id: str
    conversation_id: str | None = None
    requested_workflow: str | None = None
    selected_kb_ids: list[str] = field(default_factory=list)
    selected_skills: list[str] | None = None
    model_name: str | None = None
    use_search: bool = True
    subagent_enabled: bool = False
    thinking_enabled: bool = True
    attachments: list[dict[str, Any]] = field(default_factory=list)
    metadata: dict[str, Any] = field(default_factory=dict)
    # Runtime control fields are deliberately separate from client metadata.
    # Public transports must never let callers forge these values.
    auth_role: str | None = None
    skills_resolved: bool = False
    managed_skills_resolved: bool = False
    skills_requested_for_workflow: bool | None = None
    allow_skill_resolution_fallback: bool = False
