"""Research domain models."""

from backend.domain.research.workflow_state import (
    RESEARCH_WORKFLOW_TERMINAL_STATES,
    ResearchWorkflowState,
    normalize_research_workflow_state,
)

__all__ = [
    "RESEARCH_WORKFLOW_TERMINAL_STATES",
    "ResearchWorkflowState",
    "normalize_research_workflow_state",
]
