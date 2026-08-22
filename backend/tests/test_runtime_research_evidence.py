"""研究证据收敛/调度/写作阶段契约（拆自 test_agent_runtime_policies.py）。"""

import asyncio
import pytest
import time

from backend.application.agent_executors.stream_handler import (
    _coordinated_tool_evidence_records,
)
from backend.application.agent_workflows.coordinated_agent_workflow import (
    CoordinatedAgentWorkflow,
    CoordinationUnitResult,
    ResearchTask,
)
from backend.core.thread_key import build_internal_thread_key
from backend.domain.agents.profile import AgentProfile
from backend.domain.agents.request import AgentRequest
from backend.domain.agents.runtime_budget import AgentRuntimeBudget
from backend.domain.agents.runtime_context import AgentRuntimeContext
from dataclasses import replace


def _profile(**kwargs) -> AgentProfile:
    values = {
        "code": "ne",
        "name": "NE",
        "domain": "nuclear-electronics",
        "default_kb_ids": ["stale-yaml-kb"],
        "kb_policy": "bound_or_selected",
        "guardrail_policy": "ne_domain_scope",
    }
    values.update(kwargs)
    return AgentProfile(**values)


def _context(profile: AgentProfile, workflow: str) -> AgentRuntimeContext:
    return AgentRuntimeContext(
        request_id="req-1",
        user_id="user-1",
        external_thread_id="thread-1",
        internal_thread_id=build_internal_thread_key("user-1", "thread-1"),
        agent_profile=profile,
        workflow_name=workflow,
        model_name=None,
        permissions={
            "knowledge_base": {"allowed_ids": ["bound-kb"]},
            "domain_context": {"authorized": True},
        },
        use_search=False,
    )


@pytest.mark.asyncio
async def test_writer_verification_failure_falls_back_to_evidence_only_report(
    monkeypatch,
):
    workflow = CoordinatedAgentWorkflow(name="deep_research")
    source = "https://example.edu/neutron-study"
    responses = iter(
        (
            CoordinationUnitResult(
                role="report_writer",
                answer=(
                    "研究表明该设备具备未经证据支持的突破性能。"
                    "https://invented.example/report"
                ),
            ),
            CoordinationUnitResult(
                role="report_writer",
                answer="修订稿仍声称探测效率达到 99%。",
            ),
        )
    )

    async def fake_run_unit(**_kwargs):
        return next(responses)

    monkeypatch.setattr(workflow, "_run_unit", fake_run_unit)
    events: list[tuple[str, dict]] = []

    async def sink(event):
        events.append(event)

    result = await workflow._build_research_report(
        request=AgentRequest(
            question="分析中子探测器最新技术",
            agent_code="ne",
            user_id="user-1",
            requested_workflow="deep_research",
        ),
        context=_context(_profile(), "deep_research"),
        planner=CoordinationUnitResult(role="planner", answer='{"tasks": []}'),
        tasks=[ResearchTask("materials", "新型探测材料", "核验材料性能")],
        branch_results=[
            CoordinationUnitResult(
                role="evidence_researcher",
                answer="",
                task_key="materials",
                task_title="新型探测材料",
                handoff={
                    "claims": [
                        {
                            "claim": "实验结果显示该探测器的热中子效率达到 31.3%",
                            "supporting_refs": [source],
                            "confidence": 0.9,
                        }
                    ],
                    "evidence_refs": [source],
                },
            )
        ],
        event_sink=sink,
        research_budget_exhausted=False,
    )

    assert "31.3%" in result.answer
    assert source in result.answer
    assert "99%" not in result.answer
    assert "未经证据支持" not in result.answer
    assert "待核验" not in result.answer
    completed = [
        payload["event"]
        for event_type, payload in events
        if event_type == "workflow.step"
        and payload["event"].get("metadata", {}).get("runtime_type")
        == "writer_completed"
    ]
    assert completed[-1]["level"] == "warning"
    assert completed[-1]["metadata"]["repair_attempted"] is True
    assert completed[-1]["metadata"]["evidence_only_fallback_used"] is True
    assert completed[-1]["metadata"]["repair_answer_verification"] is not None
    assert "repair_unsupported_claims" in completed[-1]["metadata"]["fallback_reasons"]
    assert completed[-1]["metadata"]["answer_verification"]["unsupported_claims"] == 0


def test_coordinated_evidence_capture_ignores_tool_input_fields():
    assert (
        _coordinated_tool_evidence_records(
            "query_project_tables",
            {"query": "SELECT secret_assumption", "input": {"table": "monitoring"}},
            tool_run_id="tool-1",
        )
        == []
    )

    records = _coordinated_tool_evidence_records(
        "query_project_tables",
        {
            "query": "SELECT secret_assumption",
            "result": {"rows": [{"well": "A", "concentration": 10}]},
        },
        tool_run_id="tool-2",
    )

    assert len(records) == 1
    assert '"concentration": 10' in records[0]["content"]
    assert "secret_assumption" not in records[0]["content"]


async def test_planner_tool_hints_do_not_narrow_runtime_role_capabilities(monkeypatch):
    workflow = CoordinatedAgentWorkflow(name="plan_solve")
    captured: dict[str, object] = {}

    async def fake_run_unit(**kwargs):
        captured["allowed_tools"] = kwargs["allowed_tools"]
        return CoordinationUnitResult(role=kwargs["role"], answer="evidence")

    monkeypatch.setattr(workflow, "_run_unit", fake_run_unit)

    await workflow._run_research_task(
        task=ResearchTask(
            key="t1",
            title="Open research",
            question="analyze",
            expected_tools=("multi_kb_search",),
            agent_type="evidence_researcher",
        ),
        index=1,
        role="evidence_researcher",
        kb_circuit_open=asyncio.Event(),
        request=AgentRequest(
            question="analyze",
            agent_code="ne",
            user_id="user-1",
            requested_workflow="plan_solve",
        ),
        context=_context(_profile(), "plan_solve"),
    )

    assert "multi_kb_search" in captured["allowed_tools"]
    assert "read_local_skill_file" in captured["allowed_tools"]
    assert "web_search" in captured["allowed_tools"]


async def test_no_tool_unit_uses_standard_executor_and_result_only_context():
    captured = {}

    class CapturingExecutor:
        async def aquery_stream(self, question, thread_id, runnable_config=None):
            captured["question"] = question
            captured["thread_id"] = thread_id
            captured["runnable_config"] = runnable_config
            yield {"type": "chunk", "content": "standard answer"}

    class CapturingDelegate:
        def build_executor(self, request, context):
            captured["request_metadata"] = dict(request.metadata)
            captured["context"] = context
            return CapturingExecutor()

    workflow = CoordinatedAgentWorkflow(
        name="plan_solve",
        delegate=CapturingDelegate(),
    )
    result = await workflow._run_unit(
        role="reviewer",
        question="write final answer",
        request=AgentRequest(
            question="research",
            agent_code="ne",
            user_id="user-1",
            requested_workflow="plan_solve",
        ),
        context=_context(_profile(), "plan_solve"),
        tool_access=False,
    )

    assert result.answer == "standard answer"
    assert result.error is None
    assert result.handoff["subagent_type"] == "reviewer"
    assert captured["request_metadata"]["tool_access"] is False
    assert "direct_no_tool_unit" not in captured["request_metadata"]


async def test_research_convergence_is_not_charged_to_search_timeout(monkeypatch):
    emitted: list[tuple[str, dict]] = []
    invocations: list[dict] = []

    class Executor:
        def __init__(self, tool_access):
            self.tool_access = tool_access

        async def aquery_stream(self, question, thread_id, runnable_config=None):
            if not self.tool_access:
                await asyncio.sleep(0.01)
                yield {"type": "chunk", "content": "final converged answer"}
                return
            yield {
                "type": "event",
                "event": {
                    "id": "web-result-1",
                    "kind": "tool_result",
                    "title": "联网检索完成",
                    "detail": "已返回网页证据",
                    "tool": "web_search",
                    "level": "success",
                    "metadata": {"query": "focused query"},
                },
            }
            await asyncio.sleep(1)

    class Delegate:
        def build_executor(self, request, context):
            invocations.append(dict(request.metadata))
            return Executor(request.metadata["tool_access"])

    async def sink(event):
        emitted.append(event)

    workflow = CoordinatedAgentWorkflow(
        name="deep_research",
        delegate=Delegate(),
    )
    monkeypatch.setattr(
        workflow,
        "_role_timeout_seconds",
        lambda role, context: 0.2,
    )
    context = replace(
        _context(_profile(), "deep_research"),
        request_metadata={
            "coordination_web_search_max_calls": 1,
            "coordination_web_search_max_results": 3,
        },
    )

    result = await workflow._run_unit(
        role="risk_reviewer",
        question="review evidence",
        request=AgentRequest(
            question="research",
            agent_code="ne",
            user_id="user-1",
            requested_workflow="deep_research",
        ),
        context=context,
        task_id="task-risk",
        event_sink=sink,
    )

    assert result.error is None
    assert result.answer == "final converged answer"
    assert [item["tool_access"] for item in invocations] == [True, False]
    assert invocations[1]["coordination_allowed_tools"] == []
    assert any(
        payload["event"].get("metadata", {}).get("search_timeout_suspended") is True
        for event_type, payload in emitted
        if event_type == "workflow.step"
    )


async def test_research_branch_reserves_time_before_phase_deadline(monkeypatch):
    captured: dict[str, float | None] = {}
    workflow = CoordinatedAgentWorkflow(name="deep_research")

    async def fake_run_unit(**kwargs):
        captured["timeout"] = kwargs["timeout_override_seconds"]
        return CoordinationUnitResult(role=kwargs["role"], answer="bounded evidence")

    monkeypatch.setattr(workflow, "_run_unit", fake_run_unit)
    deadline = time.monotonic() + 0.08

    result = await workflow._run_research_task(
        task=ResearchTask(
            key="bounded",
            title="Bounded branch",
            question="collect evidence before the phase deadline",
            agent_type="general_analyst",
        ),
        index=1,
        role="general_analyst",
        kb_circuit_open=asyncio.Event(),
        request=AgentRequest(
            question="research",
            agent_code="ne",
            user_id="user-1",
            requested_workflow="deep_research",
        ),
        context=_context(_profile(), "deep_research"),
        research_deadline=deadline,
    )

    assert result.answer == "bounded evidence"
    assert captured["timeout"] is not None
    assert 0 < captured["timeout"] <= 0.08


async def test_research_convergence_has_an_absolute_hard_timeout(monkeypatch):
    class Executor:
        async def aquery_stream(self, question, thread_id, runnable_config=None):
            yield {
                "type": "event",
                "event": {
                    "id": "web-result-hard-timeout",
                    "kind": "tool_result",
                    "title": "联网检索完成",
                    "detail": "已返回网页证据",
                    "tool": "web_search",
                    "level": "success",
                },
            }
            while True:
                await asyncio.sleep(0.005)
                yield {"type": "chunk", "content": "still converging"}

    class Delegate:
        def build_executor(self, request, context):
            return Executor()

    workflow = CoordinatedAgentWorkflow(
        name="deep_research",
        delegate=Delegate(),
        runtime_budget=AgentRuntimeBudget(
            partial_finalization_wait_seconds=0.02,
            unit_idle_timeout_seconds=1,
            unit_heartbeat_seconds=1,
        ),
    )
    monkeypatch.setattr(workflow, "_role_timeout_seconds", lambda role, context: 0.03)
    context = replace(
        _context(_profile(), "deep_research"),
        request_metadata={"coordination_web_search_max_calls": 1},
    )

    started_at = time.monotonic()
    result = await workflow._run_unit(
        role="risk_reviewer",
        question="review evidence",
        request=AgentRequest(
            question="research",
            agent_code="ne",
            user_id="user-1",
            requested_workflow="deep_research",
        ),
        context=context,
        task_id="task-risk-hard-timeout",
    )

    assert time.monotonic() - started_at < 0.2
    assert "hard timeout" in (result.error or "")


async def test_research_convergence_timeout_keeps_cited_partial_answer(monkeypatch):
    class Executor:
        def __init__(self, tool_access):
            self.tool_access = tool_access

        async def aquery_stream(self, question, thread_id, runnable_config=None):
            if self.tool_access:
                yield {
                    "type": "event",
                    "event": {
                        "id": "web-result-cited-timeout",
                        "kind": "tool_result",
                        "title": "联网检索完成",
                        "detail": "已返回网页证据",
                        "tool": "web_search",
                        "level": "success",
                        "metadata": {
                            "query": "SiC neutron detector",
                            "web_results": [
                                {
                                    "title": "SiC detector study",
                                    "url": "https://example.org/sic-study",
                                    "content_excerpt": "碳化硅中子探测器具有耐辐照和高温工作的材料优势。",
                                }
                            ],
                        },
                    },
                }
                await asyncio.sleep(1)
                return
            yield {
                "type": "chunk",
                "content": "研究表明，碳化硅中子探测器具有耐辐照和高温工作的材料优势。",
            }
            await asyncio.sleep(1)

    class Delegate:
        def build_executor(self, request, context):
            return Executor(request.metadata["tool_access"])

    workflow = CoordinatedAgentWorkflow(
        name="deep_research",
        delegate=Delegate(),
        runtime_budget=AgentRuntimeBudget(
            deep_research_branch_convergence_timeout_seconds=0.03,
            unit_idle_timeout_seconds=1,
            unit_heartbeat_seconds=1,
        ),
    )
    monkeypatch.setattr(workflow, "_role_timeout_seconds", lambda role, context: 0.12)
    context = replace(
        _context(_profile(), "deep_research"),
        request_metadata={"coordination_web_search_max_calls": 1},
    )

    result = await workflow._run_unit(
        role="evidence_researcher",
        question="核验碳化硅中子探测器材料优势",
        request=AgentRequest(
            question="research",
            agent_code="ne",
            user_id="user-1",
            requested_workflow="deep_research",
        ),
        context=context,
        task_id="task-cited-convergence-timeout",
    )

    assert result.error is None
    assert result.timed_out is False
    assert any(
        claim.get("supporting_refs") == ["https://example.org/sic-study"]
        for claim in (result.handoff or {}).get("claims") or []
    )
    assert any(
        event.get("metadata", {}).get("runtime_type")
        in {"task_convergence_recovered", "task_partial_evidence_recovered"}
        for event in result.process_events
    )
