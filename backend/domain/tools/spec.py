"""Tool metadata contract."""

from __future__ import annotations

from dataclasses import dataclass, field
from typing import Literal

from backend.domain.agents.workflows import resolve_workflow_capabilities

ToolRiskLevel = Literal["safe", "network", "write", "sandbox"]


@dataclass(frozen=True)
class ToolSpec:
    name: str
    provider: str
    description: str = ""
    domains: set[str] = field(default_factory=set)
    workflows: set[str] = field(default_factory=set)
    required_skills: set[str] = field(default_factory=set)
    risk_level: ToolRiskLevel = "safe"
    enabled_by_default: bool = False

    def supports(self, *, domain: str, workflow: str, selected_skills: set[str]) -> bool:
        if self.domains and domain not in self.domains:
            return False
        if self.workflows and self.workflows.isdisjoint(resolve_workflow_capabilities(workflow)):
            return False
        if self.required_skills and self.required_skills.isdisjoint(selected_skills):
            return False
        return True
