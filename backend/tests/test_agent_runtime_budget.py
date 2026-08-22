from dataclasses import FrozenInstanceError

import pytest

from backend.application.agent_executors.profiled_executor import (
    _run_budget_from_runtime_budget,
)
from backend.domain.agents.runtime_budget import AgentRuntimeBudget


def test_agent_runtime_budget_uses_safe_defaults_without_env(monkeypatch):
    for name in (
        "COORDINATED_RESEARCH_MAX_CONCURRENT",
        "PLAN_SOLVE_MAX_CONCURRENT_SUBAGENTS",
        "DEEP_RESEARCH_UNIT_TOTAL_TIMEOUT_SECONDS",
        "COORDINATED_WORKFLOW_TOTAL_TIMEOUT_SECONDS",
        "DEEP_RESEARCH_RESEARCH_PHASE_TIMEOUT_SECONDS",
        "DEEP_RESEARCH_WRITER_TIMEOUT_SECONDS",
        "DEEP_RESEARCH_REPORT_TIMEOUT_SECONDS",
        "DEEP_RESEARCH_PARTIAL_FINALIZATION_WAIT_SECONDS",
        "DEEP_RESEARCH_BRANCH_TOTAL_TIMEOUT_SECONDS",
        "DEEP_RESEARCH_BRANCH_CONVERGENCE_TIMEOUT_SECONDS",
        "DEEP_RESEARCH_BRANCH_MIN_START_SECONDS",
        "AGENT_TREE_MAX_TOKENS",
        "AGENT_TREE_MAX_TOOL_CALLS",
        "DEEP_RESEARCH_REFLECTION_ROUNDS",
        "DEEP_RESEARCH_FOLLOWUP_TASKS",
        "DEEP_RESEARCH_STRUCTURED_OUTPUT_ATTEMPTS",
        "DEEP_RESEARCH_FAST_MODEL_NAME",
        "DEEP_RESEARCH_FAST_MODEL_MAX_BUDGET",
        "COORDINATED_KB_CONTEXT_MAX_CHARS",
        "COORDINATED_KB_CONTEXT_PER_BRANCH_CHARS",
    ):
        monkeypatch.delenv(name, raising=False)

    budget = AgentRuntimeBudget.from_env()

    assert budget.max_concurrent_research == 5
    assert budget.plan_solve_max_concurrent_subagents == 16
    assert budget.unit_total_timeout_seconds == 300
    assert budget.web_search_timeout_seconds == 45
    assert budget.workflow_total_timeout_seconds == 420
    assert budget.deep_research_research_phase_timeout_seconds == 900
    assert budget.deep_research_writer_timeout_seconds == 300
    assert budget.deep_research_report_timeout_seconds == 120
    assert budget.partial_finalization_wait_seconds == 60
    assert budget.deep_research_branch_total_timeout_seconds == 600
    assert budget.deep_research_branch_convergence_timeout_seconds == 240
    assert budget.deep_research_branch_min_start_seconds == 90
    assert budget.tree_max_tokens is None
    assert budget.tree_max_tool_calls is None
    assert budget.deep_research_reflection_rounds == 1
    assert budget.deep_research_followup_tasks == 2
    assert budget.deep_research_structured_output_attempts == 2
    assert budget.deep_research_fast_model_name == "qwen3.6-35b"
    assert budget.deep_research_fast_model_max_budget == 1
    assert budget.coordinated_kb_context_max_chars == 40000
    assert budget.coordinated_kb_context_per_branch_chars == 8000


def test_agent_runtime_budget_clamps_invalid_or_out_of_range_env(monkeypatch):
    monkeypatch.setenv("COORDINATED_RESEARCH_MAX_CONCURRENT", "99")
    monkeypatch.setenv("PLAN_SOLVE_MAX_CONCURRENT_SUBAGENTS", "0")
    monkeypatch.setenv("DEEP_RESEARCH_UNIT_TOTAL_TIMEOUT_SECONDS", "not-a-number")
    monkeypatch.setenv("COORDINATED_WORKFLOW_TOTAL_TIMEOUT_SECONDS", "10")
    monkeypatch.setenv("DEEP_RESEARCH_RESEARCH_PHASE_TIMEOUT_SECONDS", "10")
    monkeypatch.setenv("DEEP_RESEARCH_WRITER_TIMEOUT_SECONDS", "10")
    monkeypatch.setenv("DEEP_RESEARCH_REPORT_TIMEOUT_SECONDS", "10")
    monkeypatch.setenv("DEEP_RESEARCH_BRANCH_TOTAL_TIMEOUT_SECONDS", "10")
    monkeypatch.setenv("DEEP_RESEARCH_BRANCH_CONVERGENCE_TIMEOUT_SECONDS", "10")
    monkeypatch.setenv("DEEP_RESEARCH_BRANCH_MIN_START_SECONDS", "1")
    monkeypatch.setenv("DEEP_RESEARCH_REFLECTION_ROUNDS", "99")
    monkeypatch.setenv("DEEP_RESEARCH_FOLLOWUP_TASKS", "0")
    monkeypatch.setenv("DEEP_RESEARCH_STRUCTURED_OUTPUT_ATTEMPTS", "99")
    monkeypatch.setenv("DEEP_RESEARCH_FAST_MODEL_MAX_BUDGET", "99")
    monkeypatch.setenv("COORDINATED_KB_CONTEXT_MAX_CHARS", "1")
    monkeypatch.setenv("COORDINATED_KB_CONTEXT_PER_BRANCH_CHARS", "999999")

    budget = AgentRuntimeBudget.from_env()

    assert budget.max_concurrent_research == 5
    assert budget.plan_solve_max_concurrent_subagents == 1
    assert budget.unit_total_timeout_seconds == 300
    assert budget.workflow_total_timeout_seconds == 60
    assert budget.deep_research_research_phase_timeout_seconds == 60
    assert budget.deep_research_writer_timeout_seconds == 180
    assert budget.deep_research_report_timeout_seconds == 30
    assert budget.deep_research_branch_total_timeout_seconds == 300
    assert budget.deep_research_branch_convergence_timeout_seconds == 60
    assert budget.deep_research_branch_min_start_seconds == 10
    assert budget.deep_research_reflection_rounds == 2
    assert budget.deep_research_followup_tasks == 1
    assert budget.deep_research_structured_output_attempts == 3
    assert budget.deep_research_fast_model_max_budget == 5
    assert budget.coordinated_kb_context_max_chars == 8000
    assert budget.coordinated_kb_context_per_branch_chars == 32000


def test_agent_runtime_budget_can_disable_fast_deep_research_model(monkeypatch):
    monkeypatch.setenv("DEEP_RESEARCH_FAST_MODEL_NAME", "  ")
    monkeypatch.setenv("DEEP_RESEARCH_FAST_MODEL_MAX_BUDGET", "0")

    budget = AgentRuntimeBudget.from_env()

    assert budget.deep_research_fast_model_name is None
    assert budget.deep_research_fast_model_max_budget == 0


def test_agent_runtime_budget_is_immutable_runtime_contract():
    budget = AgentRuntimeBudget()

    with pytest.raises(FrozenInstanceError):
        budget.max_concurrent_research = 1


def test_agent_runtime_budget_reads_optional_tree_limits(monkeypatch):
    monkeypatch.setenv("AGENT_TREE_MAX_TOKENS", "12000")
    monkeypatch.setenv("AGENT_TREE_MAX_TOOL_CALLS", "25")

    budget = AgentRuntimeBudget.from_env()

    assert budget.tree_max_tokens == 12000
    assert budget.tree_max_tool_calls == 25


def test_deep_research_stream_owns_request_wide_deadline():
    budget = _run_budget_from_runtime_budget(
        AgentRuntimeBudget(workflow_total_timeout_seconds=420),
        max_active_agents=5,
        request_deadline_owned_by_stream=True,
    )

    assert budget.max_elapsed_seconds is None
