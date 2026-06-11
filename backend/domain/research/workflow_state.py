from __future__ import annotations

from enum import Enum


class ResearchWorkflowState(str, Enum):
    """Unified state machine for deep-research workflow."""

    DRAFT_GENERATED = "draft_generated"
    AWAITING_USER_CONFIRMATION = "awaiting_user_confirmation"
    PLAN_REVISED_BY_USER = "plan_revised_by_user"
    CONFIRMED_READY = "confirmed_ready"
    RUNNING = "running"
    AWAITING_CLARIFICATION = "awaiting_clarification"
    COMPLETED = "completed"
    FALLBACK_ARCHIVED = "fallback_archived"
    CANCELLED = "cancelled"


RESEARCH_WORKFLOW_TERMINAL_STATES: set[str] = {
    ResearchWorkflowState.COMPLETED.value,
    ResearchWorkflowState.FALLBACK_ARCHIVED.value,
    ResearchWorkflowState.CANCELLED.value,
}


def normalize_research_workflow_state(raw_state: str | None) -> str | None:
    text = str(raw_state or "").strip()
    if not text:
        return None
    if text in {state.value for state in ResearchWorkflowState}:
        return text
    return None
