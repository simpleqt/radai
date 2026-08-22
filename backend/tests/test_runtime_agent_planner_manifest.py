from __future__ import annotations

import asyncio
from pathlib import Path

import pytest

from backend.application.agent_workflows.coordinated_agent_workflow import (
    CoordinatedAgentWorkflow,
)
from backend.application.agent_workflows.coordination_types import (
    CoordinationUnitResult,
)
from backend.application.agent_workflows.research_planning import (
    build_planner_prompt,
    parse_research_tasks,
)
from backend.application.agents.runtime_agent_loader import RuntimeAgentLoader
from backend.domain.agents.profile import AgentProfile
from backend.domain.agents.request import AgentRequest
from backend.domain.agents.runtime_budget import AgentRuntimeBudget
from backend.domain.agents.runtime_context import AgentRuntimeContext


def _load_custom_manifest(path: Path):
    path.write_text(
        """
agents:
  planner:
    description: Plans work
    system_prompt: Use the injected role catalog.
    tool_access: false
  field_forensic:
    description: Examines field evidence
    system_prompt: Preserve the field evidence chain.
    planner_selectable: true
    aliases:
      - forensic
    title: Field Forensic
    planning_description: Inspect physical observations and preserve provenance.
    allowed_tools: []
""",
        encoding="utf-8",
    )
    return RuntimeAgentLoader(path).load_all()


def _context(agent_specs):
    return AgentRuntimeContext(
        request_id="request-1",
        user_id="user-1",
        external_thread_id="thread-1",
        internal_thread_id="user-1:thread-1",
        agent_profile=AgentProfile(
            code="virtual-fourth-domain",
            name="Virtual Fourth Domain",
            domain="virtual-fourth-domain",
        ),
        workflow_name="plan_solve",
        model_name=None,
        use_search=False,
        agent_specs=agent_specs,
    )


@pytest.mark.asyncio
async def test_new_manifest_role_is_prompted_parsed_and_dispatched(
    tmp_path: Path,
    monkeypatch,
):
    specs = _load_custom_manifest(tmp_path / "runtime_agents.yaml")
    context = _context(specs)
    request = AgentRequest(
        question="Inspect the fourth-domain sample",
        agent_code="virtual-fourth-domain",
        user_id="user-1",
        requested_workflow="plan_solve",
    )

    prompt = build_planner_prompt(request, context)
    assert "field_forensic（Field Forensic）" in prompt
    assert "Inspect physical observations and preserve provenance." in prompt
    assert "domain_analyst" not in prompt

    tasks = parse_research_tasks(
        '{"tasks":[{"key":"sample","title":"Inspect sample",'
        '"question":"Inspect it","agent_type":"forensic"}]}',
        request.question,
        context,
    )
    assert [task.agent_type for task in tasks] == ["field_forensic"]

    dispatched_roles: list[str] = []
    workflow = CoordinatedAgentWorkflow(name="plan_solve", agent_specs=specs)

    async def fake_run_research_task(**kwargs):
        dispatched_roles.append(kwargs["role"])
        return CoordinationUnitResult(role=kwargs["role"], answer="verified")

    monkeypatch.setattr(workflow, "_run_research_task", fake_run_research_task)
    results = await workflow._run_scheduled_research_tasks(
        scheduled_tasks=list(enumerate(tasks, start=1)),
        used_fallback=False,
        kb_circuit_open=asyncio.Event(),
        request=request,
        context=context,
        event_sink=None,
        analysis_gate=None,
    )

    assert dispatched_roles == ["field_forensic"]
    assert [result.role for result in results] == ["field_forensic"]


def test_unknown_planner_role_fails_instead_of_rotating_to_another_role(
    tmp_path: Path,
):
    context = _context(_load_custom_manifest(tmp_path / "runtime_agents.yaml"))

    with pytest.raises(
        ValueError,
        match="Unknown planner-selectable runtime agent 'invented_role'",
    ):
        parse_research_tasks(
            '{"tasks":[{"key":"sample","question":"Inspect it",'
            '"agent_type":"invented_role"}]}',
            "Inspect the sample",
            context,
        )


def test_loader_rejects_ambiguous_planner_aliases(tmp_path: Path):
    path = tmp_path / "runtime_agents.yaml"
    path.write_text(
        """
agents:
  first:
    description: First role
    system_prompt: First prompt
    planner_selectable: true
    aliases: [shared]
    title: First
    planning_description: First planning role
  second:
    description: Second role
    system_prompt: Second prompt
    planner_selectable: true
    aliases: [shared]
    title: Second
    planning_description: Second planning role
""",
        encoding="utf-8",
    )

    with pytest.raises(ValueError, match="alias shared is declared by both"):
        RuntimeAgentLoader(path).load_all()


@pytest.mark.asyncio
async def test_manifest_role_executes_after_two_invalid_planner_attempts(
    tmp_path: Path,
    monkeypatch,
):
    specs = _load_custom_manifest(tmp_path / "runtime_agents.yaml")
    context = _context(specs)
    request = AgentRequest(
        question="Inspect the fourth-domain sample",
        agent_code="virtual-fourth-domain",
        user_id="user-1",
        requested_workflow="plan_solve",
    )
    workflow = CoordinatedAgentWorkflow(
        name="plan_solve",
        agent_specs=specs,
        runtime_budget=AgentRuntimeBudget(
            deep_research_structured_output_attempts=2,
        ),
    )
    planner_calls: list[str] = []
    dispatched_roles: list[str] = []

    async def invalid_planner(**kwargs):
        planner_calls.append(kwargs["question"])
        return CoordinationUnitResult(role="planner", answer="not valid JSON")

    async def fake_run_research_task(**kwargs):
        dispatched_roles.append(kwargs["role"])
        return CoordinationUnitResult(
            role=kwargs["role"],
            answer="verified fallback evidence",
            task_key=kwargs["task"].key,
            task_title=kwargs["task"].title,
        )

    async def fake_build_research_report(**_kwargs):
        return CoordinationUnitResult(role="writer", answer="fallback report")

    monkeypatch.setattr(workflow, "_run_unit", invalid_planner)
    monkeypatch.setattr(workflow, "_run_research_task", fake_run_research_task)
    monkeypatch.setattr(
        workflow,
        "_build_research_report",
        fake_build_research_report,
    )

    results, tasks = await workflow._run_plan_solve(request, context)

    assert len(planner_calls) == 2
    assert "上一次响应无法解析" in planner_calls[1]
    assert [task.agent_type for task in tasks] == ["field_forensic"]
    assert dispatched_roles == ["field_forensic"]
    assert results[-1].answer == "fallback report"
