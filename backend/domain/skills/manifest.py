"""Platform-level skill manifest contract."""

from __future__ import annotations

from dataclasses import dataclass, field
from pathlib import Path


@dataclass(frozen=True)
class SkillManifest:
    name: str
    description: str
    domain: str
    skill_file: Path
    category: str
    version: str = "0.0.0"
    tools: list[str] = field(default_factory=list)
    allowed_workflows: list[str] = field(default_factory=list)
    preferred_workflow: str | None = None
    kb_required: bool = False
    kb_binding_mode: str = "agent_or_user_selected"
    guardrails: list[str] = field(default_factory=list)
    dependencies: list[str] = field(default_factory=list)
    conflicts: list[str] = field(default_factory=list)
    capabilities: list[str] = field(default_factory=list)
    resource_budget: dict[str, int | float] = field(default_factory=dict)
