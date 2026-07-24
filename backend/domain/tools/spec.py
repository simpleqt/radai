"""Tool metadata contract."""

from __future__ import annotations

from dataclasses import dataclass, field
from typing import Literal

from backend.domain.agents.workflows import resolve_workflow_capabilities

ToolRiskLevel = Literal["safe", "network", "write"]


@dataclass(frozen=True)
class ToolSpec:
    name: str
    provider: str
    description: str = ""
    domains: set[str] = field(default_factory=set)
    workflows: set[str] = field(default_factory=set)
    required_skills: set[str] = field(default_factory=set)
    required_skill_mode: Literal["any", "all"] = "any"
    risk_level: ToolRiskLevel = "safe"
    enabled_by_default: bool = False
    instructions: str = ""
    timeout_seconds: float | None = None
    side_effect: str = "none"
    policy_source: str = "registry"
    idempotent: bool = True
    parallel_safe: bool = True
    max_concurrency: int | None = None
    max_attempts: int = 1
    retryable_errors: set[str] = field(default_factory=set)
    max_output_chars: int | None = None
    cache_enabled: bool = False
    cache_ttl_seconds: int | None = None

    def supports(
        self, *, domain: str, workflow: str, selected_skills: set[str]
    ) -> bool:
        # ``*`` is an explicit platform-wide declaration.  It is not an
        # unknown-domain fallback: callers must still resolve a registered
        # profile and tool policy before reaching this capability check.
        if self.domains and "*" not in self.domains and domain not in self.domains:
            return False
        if self.workflows and self.workflows.isdisjoint(
            resolve_workflow_capabilities(workflow)
        ):
            return False
        if self.required_skills:
            if (
                self.required_skill_mode == "all"
                and not self.required_skills <= selected_skills
            ):
                return False
            if self.required_skill_mode == "any" and self.required_skills.isdisjoint(
                selected_skills
            ):
                return False
        return True
