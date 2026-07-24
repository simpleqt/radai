"""Shared value objects for coordinated agent workflows."""

from __future__ import annotations

from dataclasses import dataclass, field
from typing import Any

from backend.core.agents.context.types import SubagentResult


@dataclass(frozen=True)
class ResearchTask:
    """One independently schedulable research branch."""

    key: str
    title: str
    question: str
    priority: int = 50
    expected_tools: tuple[str, ...] = field(default_factory=tuple)
    risk: str = "medium"
    dependencies: tuple[str, ...] = field(default_factory=tuple)
    budget: int = 1
    agent_type: str = "general_analyst"


@dataclass(frozen=True)
class CoordinationUnitResult:
    """Bounded result returned by one isolated coordination unit."""

    role: str
    answer: str
    error: str | None = None
    process_events: tuple[dict[str, Any], ...] = field(default_factory=tuple)
    handoff: SubagentResult | None = None
    task_key: str | None = None
    task_title: str | None = None
    timed_out: bool = False
    # Internal retrieval telemetry.  This intentionally lives outside the
    # strict SubagentResult contract: evidence_refs contains only sources that
    # survived claim binding, while the scheduler also needs to know how many
    # sources were actually captured in order to detect extraction collapse.
    captured_evidence_refs: tuple[str, ...] = field(default_factory=tuple)
