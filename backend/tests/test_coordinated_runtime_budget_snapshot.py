from backend.application.agent_workflows.coordinated_agent_workflow import (
    CoordinatedAgentWorkflow,
)
from backend.domain.agents.runtime_budget import AgentRuntimeBudget


def test_workflow_captures_environment_budget_when_instance_is_created(monkeypatch):
    key = "PLAN_SOLVE_MAX_CONCURRENT_SUBAGENTS"
    monkeypatch.setenv(key, "7")

    first = CoordinatedAgentWorkflow(name="plan_solve")

    monkeypatch.setenv(key, "9")
    second = CoordinatedAgentWorkflow(name="plan_solve")

    assert first.runtime_budget.plan_solve_max_concurrent_subagents == 7
    assert first._research_llm_capacity() == 7
    assert second.runtime_budget.plan_solve_max_concurrent_subagents == 9
    assert second._research_llm_capacity() == 9


def test_explicit_workflow_budget_is_stable_and_takes_precedence(monkeypatch):
    monkeypatch.setenv("PLAN_SOLVE_MAX_CONCURRENT_SUBAGENTS", "12")
    injected = AgentRuntimeBudget(
        plan_solve_max_concurrent_subagents=3,
        unit_total_timeout_seconds=45,
    )

    workflow = CoordinatedAgentWorkflow(
        name="plan_solve",
        runtime_budget=injected,
    )
    monkeypatch.setenv("PLAN_SOLVE_MAX_CONCURRENT_SUBAGENTS", "15")

    assert workflow.runtime_budget is injected
    assert workflow._research_llm_capacity() == 3
    assert workflow._role_timeout_seconds("unconfigured-role", None) == 45


def test_budget_snapshot_can_use_an_isolated_environment_mapping(monkeypatch):
    monkeypatch.setenv("PLAN_SOLVE_MAX_CONCURRENT_SUBAGENTS", "20")

    budget = AgentRuntimeBudget.snapshot(
        environ={"PLAN_SOLVE_MAX_CONCURRENT_SUBAGENTS": "4"}
    )

    assert budget.plan_solve_max_concurrent_subagents == 4
    assert budget.workflow_total_timeout_seconds == 420
    assert budget.deep_research_research_phase_timeout_seconds == 900
    assert budget.deep_research_writer_timeout_seconds == 300
    assert budget.deep_research_report_timeout_seconds == 120


def test_plan_solve_limit_controls_prompt_parser_and_real_wave_capacity():
    workflow = CoordinatedAgentWorkflow(
        name="plan_solve",
        runtime_budget=AgentRuntimeBudget(
            plan_solve_max_concurrent_subagents=7,
        ),
    )
    planner_json = '{"tasks":[' + ",".join(
        f'{{"key":"t{index}","question":"q{index}"}}'
        for index in range(1, 8)
    ) + "]}"

    tasks = workflow._parse_research_tasks(planner_json, "question")

    assert len(tasks) == 7
    assert workflow._research_llm_capacity() == 7
