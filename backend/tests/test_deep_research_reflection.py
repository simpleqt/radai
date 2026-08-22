import asyncio
import json

import pytest

from backend.application.agent_workflows.coordinated_agent_workflow import (
    CoordinatedAgentWorkflow,
)
from backend.application.agent_workflows.coordination_types import (
    CoordinationUnitResult,
    ResearchTask,
)
from backend.application.agent_workflows.research_planning import (
    parse_research_reflection,
)
from backend.application.agent_workflows.research_evidence_ledger import (
    ResearchBrief,
    parse_research_brief,
)
from backend.domain.agents.profile import AgentProfile
from backend.domain.agents.request import AgentRequest
from backend.domain.agents.runtime_budget import AgentRuntimeBudget
from backend.domain.agents.runtime_context import AgentRuntimeContext


def _request_and_context() -> tuple[AgentRequest, AgentRuntimeContext]:
    profile = AgentProfile(
        code="ne",
        name="NE",
        domain="nuclear-electronics",
    )
    request = AgentRequest(
        question="Compare the current evidence for two detector approaches",
        agent_code="ne",
        user_id="user-1",
        conversation_id="thread-1",
        requested_workflow="deep_research",
        use_search=True,
    )
    context = AgentRuntimeContext(
        request_id="req-1",
        user_id="user-1",
        external_thread_id="thread-1",
        internal_thread_id="user-1:thread-1",
        agent_profile=profile,
        workflow_name="deep_research",
        model_name=None,
        kb_ids=["kb-1"],
        use_search=True,
    )
    return request, context


def test_research_reflection_parser_requires_explicit_boolean_verdict() -> None:
    assert parse_research_reflection('{"complete":"false","tasks":[]}', "q") is None

    complete = parse_research_reflection(
        '{"complete":true,"reason":"covered","tasks":[{"question":"ignored"}]}',
        "q",
    )

    assert complete is not None
    assert complete.complete is True
    assert complete.tasks == ()


def test_research_reflection_parser_bounds_followup_tasks() -> None:
    answer = json.dumps(
        {
            "complete": False,
            "reason": "two material gaps",
            "tasks": [
                {"key": f"g{index}", "question": f"gap {index}"}
                for index in range(1, 5)
            ],
        }
    )

    reflection = parse_research_reflection(answer, "q", max_tasks=2)

    assert reflection is not None
    assert reflection.complete is False
    assert [task.question for task in reflection.tasks] == ["gap 1", "gap 2"]


def test_research_reflection_parser_preserves_bounded_coverage_decision() -> None:
    reflection = parse_research_reflection(
        json.dumps(
            {
                "complete": False,
                "reason": "one material gap",
                "coverage_score": 1.5,
                "unresolved_dimensions": ["适用边界"],
                "source_diversity_score": 0.65,
                "contradiction_count": 2,
                "expected_information_gain": 0.4,
                "stop_reason": "",
                "tasks": [],
            }
        ),
        "q",
    )

    assert reflection is not None
    assert reflection.coverage_score == 1.0
    assert reflection.unresolved_dimensions == ("适用边界",)
    assert reflection.source_diversity_score == 0.65
    assert reflection.contradiction_count == 2
    assert reflection.expected_information_gain == 0.4


def test_planner_research_brief_is_optional_and_bounded() -> None:
    fallback = ResearchBrief(objective="fallback", source_policy=("kb",))
    brief = parse_research_brief(
        json.dumps(
            {
                "research_brief": {
                    "objective": "compare detectors",
                    "dimensions": [f"d{index}" for index in range(20)],
                    "source_policy": ["primary sources"],
                }
            }
        ),
        fallback=fallback,
    )

    assert brief.objective == "compare detectors"
    assert len(brief.dimensions) == 12
    assert brief.source_policy == ("primary sources",)
    assert parse_research_brief("not-json", fallback=fallback) == fallback


def test_followup_scheduler_deduplicates_near_identical_chinese_queries() -> None:
    _request, context = _request_and_context()
    prepared = CoordinatedAgentWorkflow._prepare_followup_tasks(
        (
            ResearchTask(
                key="gap1",
                title="重复任务",
                question="核验高纯锗探测器的能量分辨率与测量条件",
            ),
        ),
        existing_tasks=[
            ResearchTask(
                key="t1",
                title="已有任务",
                question="请核验高纯锗探测器能量分辨率和对应测量条件",
            )
        ],
        context=context,
        round_index=1,
    )

    assert prepared == []


def test_scheduler_creates_one_bounded_conflict_resolution_lane() -> None:
    _request, context = _request_and_context()
    results = [
        CoordinationUnitResult(
            role="data_analyst",
            answer="one",
            task_key="t1",
            handoff={
                "claims": [
                    {
                        "claim": "该探测器的能量分辨率为 1.0%",
                        "supporting_refs": ["https://a.example/report"],
                        "confidence": 0.9,
                    }
                ]
            },
        ),
        CoordinationUnitResult(
            role="evidence_researcher",
            answer="two",
            task_key="t2",
            handoff={
                "claims": [
                    {
                        "claim": "该探测器的能量分辨率为 2.0%",
                        "supporting_refs": ["https://b.example/report"],
                        "confidence": 0.9,
                    }
                ]
            },
        ),
    ]

    task = CoordinatedAgentWorkflow._prepare_conflict_resolution_task(
        existing_tasks=[],
        branch_results=results,
        context=context,
        round_index=1,
    )

    assert task is not None
    assert task.key == "conflict_1"
    assert task.agent_type == "risk_reviewer"
    assert task.risk == "high"
    assert task.expected_tools == ("multi_kb_search", "web_search")
    assert "1.0%" in task.question and "2.0%" in task.question
    assert (
        CoordinatedAgentWorkflow._prepare_conflict_resolution_task(
            existing_tasks=[task],
            branch_results=results,
            context=context,
            round_index=2,
        )
        is None
    )


def test_quality_gate_builds_bounded_recovery_for_failed_and_uncited_branches() -> None:
    _request, context = _request_and_context()
    tasks = [
        ResearchTask(
            key="failed",
            title="Failed evidence",
            question="Recover failed evidence",
            expected_tools=("web_search",),
            agent_type="evidence_researcher",
        ),
        ResearchTask(
            key="uncited",
            title="Uncited evidence",
            question="Recover citations",
            expected_tools=("web_search",),
            agent_type="evidence_researcher",
        ),
    ]
    results = [
        CoordinationUnitResult(
            role="evidence_researcher",
            answer="timed out",
            task_key="failed",
            task_title="Failed evidence",
            error="execution timed out after 180s",
        ),
        CoordinationUnitResult(
            role="evidence_researcher",
            answer="claim without citation",
            task_key="uncited",
            task_title="Uncited evidence",
            handoff={
                "claims": [{"claim": "claim without citation", "confidence": 0.8}]
            },
        ),
    ]

    prepared = CoordinatedAgentWorkflow._prepare_quality_recovery_tasks(
        tasks=tasks,
        branch_results=results,
        context=context,
        round_index=1,
        limit=1,
    )

    assert len(prepared) == 1
    assert prepared[0].key == "quality_1_1"
    assert prepared[0].title == "证据恢复：Failed evidence"
    assert prepared[0].budget == 2
    assert prepared[0].expected_tools == ("web_search",)


@pytest.mark.asyncio
async def test_deep_research_runs_agent_selected_evidence_followup(monkeypatch) -> None:
    request, context = _request_and_context()
    workflow = CoordinatedAgentWorkflow(
        name="deep_research",
        runtime_budget=AgentRuntimeBudget(
            deep_research_reflection_rounds=1,
            deep_research_followup_tasks=2,
        ),
    )
    initial_task = ResearchTask(
        key="t1",
        title="Initial evidence",
        question="Collect initial evidence",
        expected_tools=("web_search",),
        agent_type="evidence_researcher",
    )
    planner = CoordinationUnitResult(role="planner", answer='{"tasks":[]}')
    initial_result = CoordinationUnitResult(
        role="evidence_researcher",
        answer="Initial answer",
        task_key="t1",
        task_title="Initial evidence",
        handoff={
            "evidence_refs": ["https://example.com/initial"],
            "claims": [
                {
                    "claim": "Initial answer",
                    "supporting_refs": ["https://example.com/initial"],
                    "confidence": 0.8,
                }
            ],
        },
    )
    emitted = []
    followup_calls = []

    async def fake_emit_process(_sink, event):
        emitted.append(event)

    async def fake_emit(_sink, event_type, payload):
        emitted.append({"event_type": event_type, **payload})

    async def fake_run_unit(**kwargs):
        assert kwargs["role"] == "planner"
        return CoordinationUnitResult(
            role="planner",
            answer=json.dumps(
                {
                    "complete": False,
                    "reason": "latest quantitative evidence is missing",
                    "tasks": [
                        {
                            "key": "gap1",
                            "title": "Fill quantitative gap",
                            "question": "Find current quantitative evidence for the missing comparison",
                            "expected_tools": [],
                            "agent_type": "evidence_researcher",
                        }
                    ],
                }
            ),
        )

    async def fake_run_research_task(**kwargs):
        followup_calls.append(kwargs)
        return CoordinationUnitResult(
            role=kwargs["role"],
            answer="Follow-up evidence",
            task_key=kwargs["task"].key,
            task_title=kwargs["task"].title,
        )

    monkeypatch.setattr(workflow, "_emit_process_event", fake_emit_process)
    monkeypatch.setattr(workflow, "_emit_event", fake_emit)
    monkeypatch.setattr(workflow, "_run_unit", fake_run_unit)
    monkeypatch.setattr(workflow, "_run_research_task", fake_run_research_task)

    tasks, results, reflections = await workflow._run_research_reflection(
        request=request,
        context=context,
        planner=planner,
        tasks=[initial_task],
        branch_results=[initial_result],
        kb_circuit_open=asyncio.Event(),
        event_sink=None,
    )

    assert len(reflections) == 1
    assert [task.key for task in tasks] == ["t1", "followup_1_1"]
    assert tasks[-1].expected_tools == ("multi_kb_search", "web_search")
    assert len(results) == 2
    assert followup_calls[0]["dependency_results"] == [initial_result]
    assert any(
        event.get("metadata", {}).get("runtime_type") == "research_followup_completed"
        for event in emitted
    )


@pytest.mark.asyncio
async def test_invalid_supervisor_output_retries_and_never_marks_complete(
    monkeypatch,
) -> None:
    request, context = _request_and_context()
    workflow = CoordinatedAgentWorkflow(
        name="deep_research",
        runtime_budget=AgentRuntimeBudget(
            deep_research_reflection_rounds=1,
            deep_research_followup_tasks=1,
            deep_research_structured_output_attempts=2,
        ),
    )
    task = ResearchTask(
        key="t1",
        title="Initial evidence",
        question="Collect initial evidence",
        expected_tools=("web_search",),
        agent_type="evidence_researcher",
    )
    initial_result = CoordinationUnitResult(
        role="evidence_researcher",
        answer="supported answer",
        task_key="t1",
        task_title=task.title,
        handoff={
            "evidence_refs": ["https://example.com/source"],
            "claims": [
                {
                    "claim": "supported answer",
                    "supporting_refs": ["https://example.com/source"],
                    "confidence": 0.8,
                }
            ],
        },
    )
    emitted = []
    run_calls = []

    async def fake_emit_process(_sink, event):
        emitted.append(event)

    async def fake_run_unit(**kwargs):
        run_calls.append(kwargs)
        return CoordinationUnitResult(role="planner", answer="not-json")

    monkeypatch.setattr(workflow, "_emit_process_event", fake_emit_process)
    monkeypatch.setattr(workflow, "_run_unit", fake_run_unit)

    tasks, results, reflections = await workflow._run_research_reflection(
        request=request,
        context=context,
        planner=CoordinationUnitResult(role="planner", answer='{"tasks":[]}'),
        tasks=[task],
        branch_results=[initial_result],
        kb_circuit_open=asyncio.Event(),
        event_sink=None,
    )

    assert tasks == [task]
    assert results == [initial_result]
    assert len(reflections) == 1
    assert reflections[0].role == "research_supervisor"
    assert reflections[0].answer == "not-json"
    assert len(run_calls) == 2
    completed = [
        event
        for event in emitted
        if event.get("metadata", {}).get("runtime_type")
        == "research_reflection_completed"
    ][-1]
    assert completed["level"] == "warning"
    assert completed["metadata"]["complete"] is False
    assert completed["metadata"]["structured_output_valid"] is False
