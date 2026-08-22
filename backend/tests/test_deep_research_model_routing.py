from dataclasses import replace

from backend.application.agent_workflows.coordinated_agent_workflow import (
    CoordinatedAgentWorkflow,
)
from backend.application.agent_workflows.coordination_types import ResearchTask
from backend.application.agent_workflows.research_planning import (
    fallback_research_tasks,
    parse_research_tasks,
)
from backend.domain.agents.profile import AgentProfile
from backend.domain.agents.runtime_budget import AgentRuntimeBudget
from backend.domain.agents.runtime_context import AgentRuntimeContext


def _context(*, model_name: str = "primary-model") -> AgentRuntimeContext:
    return AgentRuntimeContext(
        request_id="req-model-routing",
        user_id="user-model-routing",
        external_thread_id="external-thread",
        internal_thread_id="internal-thread",
        agent_profile=AgentProfile(
            code="test-agent",
            name="Test agent",
            domain="test",
        ),
        workflow_name="deep_research",
        model_name=model_name,
    )


def _task(
    *,
    budget: int = 1,
    risk: str = "medium",
    agent_type: str = "evidence_researcher",
) -> ResearchTask:
    return ResearchTask(
        key="t1",
        title="test task",
        question="research one bounded question",
        budget=budget,
        risk=risk,
        agent_type=agent_type,
    )


def test_deep_research_routes_planner_to_fast_symbolic_model():
    workflow = CoordinatedAgentWorkflow(name="deep_research")
    context = _context()

    assert workflow._role_model_name("planner", context) == "qwen3.6-35b"
    assert workflow._model_tier("qwen3.6-35b", context) == "fast"


def test_deep_research_routes_only_low_budget_safe_roles_to_fast_model():
    workflow = CoordinatedAgentWorkflow(name="deep_research")
    context = _context()

    assert (
        workflow._research_task_model_name(
            task=_task(), role="evidence_researcher", context=context
        )
        == "qwen3.6-35b"
    )
    assert (
        workflow._research_task_model_name(
            task=_task(budget=2), role="evidence_researcher", context=context
        )
        == "primary-model"
    )


def test_verification_roles_default_to_primary_model_budget():
    context = _context()
    tasks = parse_research_tasks(
        '{"tasks":['
        '{"key":"facts","question":"verify current facts",'
        '"agent_type":"evidence_researcher"},'
        '{"key":"risk","question":"challenge the evidence",'
        '"agent_type":"risk_reviewer"},'
        '{"key":"background","question":"summarize background",'
        '"agent_type":"domain_analyst"}'
        "]}",
        "research",
        context,
    )

    assert [task.budget for task in tasks] == [2, 2, 1]
    workflow = CoordinatedAgentWorkflow(name="deep_research")
    assert all(
        workflow._research_task_model_name(
            task=task,
            role=task.agent_type,
            context=context,
        )
        == "primary-model"
        for task in tasks[:2]
    )

    fallback = fallback_research_tasks("deep_research", "research", context)
    assert [task.budget for task in fallback] == [1, 2, 2]
    assert (
        workflow._research_task_model_name(
            task=_task(risk="high"), role="evidence_researcher", context=context
        )
        == "primary-model"
    )
    assert (
        workflow._research_task_model_name(
            task=_task(agent_type="data_analyst"),
            role="data_analyst",
            context=context,
        )
        == "primary-model"
    )
    assert (
        workflow._research_task_model_name(
            task=_task(agent_type="solution_architect"),
            role="solution_architect",
            context=context,
        )
        == "primary-model"
    )


def test_plan_solve_keeps_primary_model_and_fast_routing_can_be_disabled():
    context = _context()
    plan_solve = CoordinatedAgentWorkflow(name="plan_solve")
    disabled = CoordinatedAgentWorkflow(
        name="deep_research",
        runtime_budget=AgentRuntimeBudget(
            deep_research_fast_model_name=None,
            deep_research_fast_model_max_budget=0,
        ),
    )

    assert plan_solve._role_model_name("planner", context) == "primary-model"
    assert (
        plan_solve._research_task_model_name(
            task=_task(), role="evidence_researcher", context=context
        )
        == "primary-model"
    )
    assert disabled._role_model_name("planner", context) == "primary-model"
    assert (
        disabled._research_task_model_name(
            task=_task(), role="evidence_researcher", context=context
        )
        == "primary-model"
    )


def test_fast_tier_survives_task_scoped_context_replacement():
    workflow = CoordinatedAgentWorkflow(name="deep_research")
    scoped = replace(
        _context(),
        model_name="qwen3.6-35b",
        request_metadata={"coordination_primary_model_name": "primary-model"},
    )

    assert workflow._model_tier("qwen3.6-35b", scoped) == "fast"
