"""Centralized runtime budget for agent execution."""

from __future__ import annotations

import os
from collections.abc import Mapping
from dataclasses import dataclass
from typing import Any


@dataclass(frozen=True)
class AgentRuntimeBudget:
    """Immutable execution-limit snapshot for one coordinated workflow instance."""

    max_concurrent_research: int = 5
    max_concurrent_per_user_model: int = 5
    plan_solve_max_concurrent_subagents: int = 16
    event_queue_maxsize: int = 64
    unit_heartbeat_seconds: int = 10
    unit_idle_timeout_seconds: int = 120
    unit_total_timeout_seconds: int = 300
    kb_search_timeout_seconds: int = 180
    web_search_timeout_seconds: int = 45
    workflow_total_timeout_seconds: int = 420
    deep_research_research_phase_timeout_seconds: int = 900
    deep_research_writer_timeout_seconds: int = 300
    deep_research_report_timeout_seconds: int = 120
    tree_max_tokens: int | None = None
    tree_max_tool_calls: int | None = None
    partial_handoff_grace_seconds: float = 0.25
    partial_writer_min_branches: int = 1
    partial_finalization_wait_seconds: float = 60.0
    deep_research_branch_total_timeout_seconds: float = 600.0
    deep_research_branch_convergence_timeout_seconds: float = 240.0
    deep_research_branch_min_start_seconds: float = 90.0
    deep_research_reflection_rounds: int = 1
    deep_research_followup_tasks: int = 2
    deep_research_structured_output_attempts: int = 2
    deep_research_fast_model_name: str | None = "qwen3.6-35b"
    deep_research_fast_model_max_budget: int = 1
    coordinated_kb_context_max_chars: int = 40000
    coordinated_kb_context_per_branch_chars: int = 8000

    @classmethod
    def from_env(
        cls,
        environ: Mapping[str, str] | None = None,
    ) -> AgentRuntimeBudget:
        """Capture the current environment values into an immutable budget."""

        return cls(
            max_concurrent_research=_env_int(
                "COORDINATED_RESEARCH_MAX_CONCURRENT",
                default=5,
                minimum=1,
                maximum=5,
                environ=environ,
            ),
            max_concurrent_per_user_model=_env_int(
                "COORDINATED_RESEARCH_PER_USER_MODEL_CONCURRENT",
                default=5,
                minimum=1,
                maximum=5,
                environ=environ,
            ),
            plan_solve_max_concurrent_subagents=_env_int(
                "PLAN_SOLVE_MAX_CONCURRENT_SUBAGENTS",
                default=16,
                minimum=1,
                maximum=32,
                environ=environ,
            ),
            event_queue_maxsize=_env_int(
                "DEEP_RESEARCH_EVENT_QUEUE_MAXSIZE",
                default=64,
                minimum=1,
                environ=environ,
            ),
            unit_heartbeat_seconds=_env_int(
                "DEEP_RESEARCH_HEARTBEAT_SECONDS",
                default=10,
                minimum=2,
                environ=environ,
            ),
            unit_idle_timeout_seconds=_env_int(
                "DEEP_RESEARCH_UNIT_IDLE_TIMEOUT_SECONDS",
                default=120,
                minimum=10,
                environ=environ,
            ),
            unit_total_timeout_seconds=_env_int(
                "DEEP_RESEARCH_UNIT_TOTAL_TIMEOUT_SECONDS",
                default=300,
                minimum=30,
                environ=environ,
            ),
            kb_search_timeout_seconds=_env_int(
                "DEEP_RESEARCH_KB_TIMEOUT_SECONDS",
                default=180,
                minimum=10,
                environ=environ,
            ),
            web_search_timeout_seconds=_env_int(
                "DEEP_RESEARCH_WEB_TIMEOUT_SECONDS",
                default=45,
                minimum=10,
                environ=environ,
            ),
            workflow_total_timeout_seconds=_env_int(
                "COORDINATED_WORKFLOW_TOTAL_TIMEOUT_SECONDS",
                default=420,
                minimum=60,
                environ=environ,
            ),
            deep_research_research_phase_timeout_seconds=_env_int(
                "DEEP_RESEARCH_RESEARCH_PHASE_TIMEOUT_SECONDS",
                default=900,
                minimum=60,
                maximum=1800,
                environ=environ,
            ),
            deep_research_writer_timeout_seconds=_env_int(
                "DEEP_RESEARCH_WRITER_TIMEOUT_SECONDS",
                default=300,
                minimum=180,
                maximum=600,
                environ=environ,
            ),
            deep_research_report_timeout_seconds=_env_int(
                "DEEP_RESEARCH_REPORT_TIMEOUT_SECONDS",
                default=120,
                minimum=30,
                maximum=300,
                environ=environ,
            ),
            tree_max_tokens=_env_optional_int(
                "AGENT_TREE_MAX_TOKENS",
                environ=environ,
            ),
            tree_max_tool_calls=_env_optional_int(
                "AGENT_TREE_MAX_TOOL_CALLS",
                environ=environ,
            ),
            partial_handoff_grace_seconds=_env_float(
                "DEEP_RESEARCH_PARTIAL_HANDOFF_GRACE_SECONDS",
                default=0.25,
                minimum=0.0,
                environ=environ,
            ),
            partial_writer_min_branches=_env_int(
                "DEEP_RESEARCH_PARTIAL_WRITER_MIN_BRANCHES",
                default=1,
                minimum=1,
                environ=environ,
            ),
            partial_finalization_wait_seconds=_env_float(
                "DEEP_RESEARCH_PARTIAL_FINALIZATION_WAIT_SECONDS",
                default=60.0,
                minimum=1.0,
                environ=environ,
            ),
            deep_research_branch_total_timeout_seconds=_env_float(
                "DEEP_RESEARCH_BRANCH_TOTAL_TIMEOUT_SECONDS",
                default=600.0,
                minimum=300.0,
                maximum=1200.0,
                environ=environ,
            ),
            deep_research_branch_convergence_timeout_seconds=_env_float(
                "DEEP_RESEARCH_BRANCH_CONVERGENCE_TIMEOUT_SECONDS",
                default=240.0,
                minimum=60.0,
                maximum=600.0,
                environ=environ,
            ),
            deep_research_branch_min_start_seconds=_env_float(
                "DEEP_RESEARCH_BRANCH_MIN_START_SECONDS",
                default=90.0,
                minimum=10.0,
                maximum=300.0,
                environ=environ,
            ),
            deep_research_reflection_rounds=_env_int(
                "DEEP_RESEARCH_REFLECTION_ROUNDS",
                default=1,
                minimum=0,
                maximum=2,
                environ=environ,
            ),
            deep_research_followup_tasks=_env_int(
                "DEEP_RESEARCH_FOLLOWUP_TASKS",
                default=2,
                minimum=1,
                maximum=5,
                environ=environ,
            ),
            deep_research_structured_output_attempts=_env_int(
                "DEEP_RESEARCH_STRUCTURED_OUTPUT_ATTEMPTS",
                default=2,
                minimum=1,
                maximum=3,
                environ=environ,
            ),
            deep_research_fast_model_name=_env_optional_text(
                "DEEP_RESEARCH_FAST_MODEL_NAME",
                default="qwen3.6-35b",
                environ=environ,
            ),
            deep_research_fast_model_max_budget=_env_int(
                "DEEP_RESEARCH_FAST_MODEL_MAX_BUDGET",
                default=1,
                minimum=0,
                maximum=5,
                environ=environ,
            ),
            coordinated_kb_context_max_chars=_env_int(
                "COORDINATED_KB_CONTEXT_MAX_CHARS",
                default=40000,
                minimum=8000,
                maximum=128000,
                environ=environ,
            ),
            coordinated_kb_context_per_branch_chars=_env_int(
                "COORDINATED_KB_CONTEXT_PER_BRANCH_CHARS",
                default=8000,
                minimum=1000,
                maximum=32000,
                environ=environ,
            ),
        )

    @classmethod
    def snapshot(
        cls,
        budget: AgentRuntimeBudget | None = None,
        *,
        environ: Mapping[str, str] | None = None,
    ) -> AgentRuntimeBudget:
        """Resolve an injected budget or capture the environment at call time.

        ``AgentRuntimeBudget`` is frozen, so an injected instance is already a
        safe snapshot and can be reused without copying.  ``environ`` exists as
        a deterministic injection seam for factories and tests.
        """

        if budget is not None:
            if not isinstance(budget, cls):
                raise TypeError("budget must be an AgentRuntimeBudget instance")
            if environ is not None:
                raise ValueError("environ cannot be provided with an explicit budget")
            return budget
        return cls.from_env(environ=environ)


def _env_int(
    name: str,
    *,
    default: int,
    minimum: int | None = None,
    maximum: int | None = None,
    environ: Mapping[str, str] | None = None,
) -> int:
    return int(
        _bounded(
            _parse_number(_env_value(name, environ), default),
            minimum,
            maximum,
        )
    )


def _env_float(
    name: str,
    *,
    default: float,
    minimum: float | None = None,
    maximum: float | None = None,
    environ: Mapping[str, str] | None = None,
) -> float:
    return float(
        _bounded(
            _parse_number(_env_value(name, environ), default),
            minimum,
            maximum,
        )
    )


def _env_optional_int(
    name: str,
    *,
    environ: Mapping[str, str] | None = None,
) -> int | None:
    """Read an optional non-negative integer without inventing a default cap."""

    value = _env_value(name, environ)
    if value is None or value == "":
        return None
    try:
        parsed = int(value)
    except (TypeError, ValueError):
        return None
    return max(0, parsed)


def _env_optional_text(
    name: str,
    *,
    default: str | None,
    environ: Mapping[str, str] | None = None,
) -> str | None:
    value = _env_value(name, environ)
    if value is None:
        return default
    normalized = str(value).strip()
    return normalized or None


def _env_value(name: str, environ: Mapping[str, str] | None) -> str | None:
    if environ is None:
        return os.getenv(name)
    return environ.get(name)


def _parse_number(value: Any, default: int | float) -> int | float:
    if value is None or value == "":
        return default
    try:
        if isinstance(default, int):
            return int(value)
        return float(value)
    except (TypeError, ValueError):
        return default


def _bounded(
    value: int | float,
    minimum: int | float | None,
    maximum: int | float | None,
) -> int | float:
    if minimum is not None:
        value = max(minimum, value)
    if maximum is not None:
        value = min(maximum, value)
    return value
