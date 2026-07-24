"""Runtime agent role contracts.

Runtime agent specs describe executable child-agent roles used inside a
workflow.  They are intentionally separate from AgentProfile, which describes
the user-facing top-level agent.
"""

from __future__ import annotations

from dataclasses import dataclass, field


@dataclass(frozen=True)
class RuntimeAgentSpec:
    name: str
    description: str
    system_prompt: str
    allowed_tools: set[str] | None = None
    required_tools: set[str] = field(default_factory=set)
    disallowed_tools: set[str] = field(default_factory=set)
    max_turns: int = 12
    timeout_seconds: int = 300
    model: str | None = None
    reasoning_effort: str | None = None
    tool_access: bool = True
    output_schema: str | None = None
    can_use_network: bool = True
    can_read_kb: bool = True
    planner_selectable: bool = False
    aliases: tuple[str, ...] = field(default_factory=tuple)
    title: str | None = None
    planning_description: str | None = None
