"""Coordination strategy contracts."""

from __future__ import annotations

from dataclasses import dataclass, field
from typing import Protocol

from backend.domain.agents.request import AgentRequest
from backend.domain.agents.runtime_context import AgentRuntimeContext


@dataclass(frozen=True)
class CoordinationStage:
    key: str
    title: str
    description: str


@dataclass(frozen=True)
class CoordinationPlan:
    workflow: str
    summary: str
    stages: list[CoordinationStage] = field(default_factory=list)
    metadata: dict[str, str] = field(default_factory=dict)


class CoordinationStrategy(Protocol):
    workflow_name: str

    def build_plan(
        self,
        request: AgentRequest,
        context: AgentRuntimeContext,
    ) -> CoordinationPlan:
        ...
