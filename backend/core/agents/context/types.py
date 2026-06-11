"""Structured context types for unified context governance."""

from __future__ import annotations

import time
import uuid
from typing import Literal, NotRequired, TypedDict

ContextKind = Literal[
    "skill_rule",
    "retrieval_evidence",
    "analysis_result",
    "artifact_ref",
    "subagent_result",
    "generic",
]

ContextScope = Literal["turn", "thread"]


class ContextItem(TypedDict):
    """Structured context entry stored in thread state."""

    id: str
    kind: ContextKind
    source_tool: str
    scope: ContextScope
    summary: str
    keywords: list[str]
    importance: float
    raw_ref: NotRequired[str | None]
    created_at: float
    turn_id: NotRequired[str | None]
    skill: NotRequired[str | None]
    tags: list[str]


class WorkingMemory(TypedDict, total=False):
    """Per-turn executable state."""

    turn_id: str | None
    active_skills: list[str]
    tools_called: list[str]
    tool_budget_used: dict[str, int]
    interim_summary: str | None


class ContextBudgetState(TypedDict, total=False):
    """Tracks budget consumption for the current assembly cycle."""

    jit_char_budget: int
    jit_chars_used: int
    items_injected: int
    items_total: int
    last_assembled_at: float


class SubagentResult(TypedDict, total=False):
    """Structured subagent return consumed by the main agent."""

    summary: str
    evidence_refs: list[str]
    risks: list[str]
    suggested_next_actions: list[str]
    raw_artifact_ref: str | None
    subagent_type: str
    description: str


def new_context_item_id() -> str:
    return f"ctx_{uuid.uuid4().hex[:12]}"


def new_turn_id() -> str:
    return f"t_{uuid.uuid4().hex[:8]}"


def make_context_item(
    *,
    kind: ContextKind,
    source_tool: str,
    summary: str,
    keywords: list[str] | None = None,
    importance: float = 0.5,
    raw_ref: str | None = None,
    scope: ContextScope = "thread",
    turn_id: str | None = None,
    skill: str | None = None,
    tags: list[str] | None = None,
) -> ContextItem:
    return ContextItem(
        id=new_context_item_id(),
        kind=kind,
        source_tool=source_tool,
        scope=scope,
        summary=summary,
        keywords=keywords or [],
        importance=importance,
        raw_ref=raw_ref,
        created_at=time.time(),
        turn_id=turn_id,
        skill=skill,
        tags=tags or [],
    )
