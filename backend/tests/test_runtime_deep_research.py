"""深度研究工作流契约（拆自 test_agent_runtime_policies.py）。"""

import asyncio
import json
import time

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


async def test_deep_research_delegates_web_search_to_specialist_agent(monkeypatch):
    import backend.application.agent_workflows.coordinated_agent_workflow as workflow_module

    class UnexpectedRuntimeToolInvoker:
        def __init__(self, **kwargs):
            raise AssertionError("deep research must not prefetch web_search")

    monkeypatch.setattr(
        workflow_module,
        "RuntimeToolInvoker",
        UnexpectedRuntimeToolInvoker,
    )
    workflow = CoordinatedAgentWorkflow(name="deep_research")
    captured: dict[str, object] = {}

    async def fake_run_unit(**kwargs):
        captured.update(kwargs)
        return CoordinationUnitResult(
            role=kwargs["role"],
            answer="verified evidence",
            process_events=(
                {
                    "kind": "tool_start",
                    "tool": "web_search",
                    "metadata": {"input": {"query": "focused query one"}},
                },
                {
                    "kind": "tool_result",
                    "tool": "web_search",
                    "level": "success",
                    "metadata": {"query": "focused query one"},
                },
            ),
        )

    monkeypatch.setattr(workflow, "_run_unit", fake_run_unit)
    context = replace(
        _context(_profile(), "deep_research"),
        kb_ids=[],
        use_search=True,
    )
    emitted: list[tuple[str, dict]] = []

    async def sink(event):
        emitted.append(event)

    result = await workflow._run_research_task(
        task=ResearchTask(
            key="web-evidence",
            title="External evidence",
            question="Compare several current detector technologies",
            expected_tools=("web_search",),
            agent_type="evidence_researcher",
        ),
        index=1,
        role="evidence_researcher",
        kb_circuit_open=asyncio.Event(),
        request=AgentRequest(
            question="Compare detector technologies",
            agent_code="ne",
            user_id="user-1",
            requested_workflow="deep_research",
        ),
        context=context,
        event_sink=sink,
    )

    assert result.error is None
    allowed_tools = captured["allowed_tools"]
    assert isinstance(allowed_tools, set)
    assert {
        "web_search",
        "multi_kb_search",
        "read_local_skill_file",
        "read_context_artifact",
    }.issubset(allowed_tools)
    assert "自主调用 web_search" in str(captured["question"])
    assert "若结果偏题、过泛或不足，必须改写" in str(captured["question"])
    assert "最多调用 3 次 web_search" in str(captured["question"])
    assert (
        captured["context"].request_metadata["coordination_web_search_max_calls"] == 3
    )
    assert (
        captured["context"].request_metadata["coordination_web_search_max_results"] == 3
    )
    task_end = [
        payload["event"]
        for event_type, payload in emitted
        if event_type == "workflow.step" and payload["event"]["kind"] == "task_end"
    ][-1]
    assert task_end["metadata"]["search_queries"] == ["focused query one"]


async def test_deep_research_repairs_a_missing_required_web_search(monkeypatch):
    import backend.application.agent_workflows.coordinated_agent_workflow as workflow_module

    invocations: list[tuple[str, dict]] = []

    class FakeRuntimeToolInvoker:
        def __init__(self, **kwargs):
            self.kwargs = kwargs

        async def invoke(self, tool_name, args):
            invocations.append((tool_name, args))
            return json.dumps(
                {
                    "query": "focused fallback query",
                    "engine": "test",
                    "results": [
                        {
                            "title": "Verified detector evidence",
                            "url": "https://example.com/detector-evidence",
                            "content": "Measured detector evidence.",
                        }
                    ],
                }
            )

    monkeypatch.setattr(workflow_module, "RuntimeToolInvoker", FakeRuntimeToolInvoker)
    workflow = CoordinatedAgentWorkflow(name="deep_research")
    unit_calls: list[dict] = []

    async def fake_run_unit(**kwargs):
        unit_calls.append(kwargs)
        if len(unit_calls) == 1:
            return CoordinationUnitResult(
                role=kwargs["role"],
                answer="unsupported first answer",
            )
        return CoordinationUnitResult(
            role=kwargs["role"],
            answer=(
                "Repaired conclusion supported by "
                "https://example.com/detector-evidence"
            ),
        )

    monkeypatch.setattr(workflow, "_run_unit", fake_run_unit)
    monkeypatch.setattr(workflow, "_run_unit_unmanaged", fake_run_unit)
    context = replace(
        _context(_profile(), "deep_research"),
        kb_ids=[],
        use_search=True,
    )
    emitted: list[tuple[str, dict]] = []

    async def sink(event):
        emitted.append(event)

    result = await workflow._run_research_task(
        task=ResearchTask(
            key="web-repair",
            title="Repair missing web evidence",
            question="Verify current detector evidence",
            expected_tools=("web_search",),
            agent_type="evidence_researcher",
        ),
        index=1,
        role="evidence_researcher",
        kb_circuit_open=asyncio.Event(),
        request=AgentRequest(
            question="Compare detector technologies",
            agent_code="ne",
            user_id="user-1",
            requested_workflow="deep_research",
        ),
        context=context,
        event_sink=sink,
    )

    assert result.error is None
    assert result.answer.startswith("Repaired conclusion")
    assert len(unit_calls) == 2
    assert unit_calls[1]["tool_access"] is False
    assert unit_calls[1]["allowed_tools"] == set()
    assert "补充联网证据" in unit_calls[1]["question"]
    assert invocations == [
        (
            "web_search",
            {
                "query": "Verify current detector evidence",
                "max_results": 3,
                "timelimit": None,
            },
        )
    ]
    runtime_types = {
        payload["event"].get("metadata", {}).get("runtime_type")
        for event_type, payload in emitted
        if event_type == "workflow.step"
    }
    assert "contract_tool_repair_started" in runtime_types
    assert "contract_answer_repair_completed" in runtime_types


async def test_deep_research_missing_web_error_is_not_duplicated(monkeypatch):
    import backend.application.agent_workflows.coordinated_agent_workflow as workflow_module

    class FailingRuntimeToolInvoker:
        def __init__(self, **kwargs):
            self.kwargs = kwargs

        async def invoke(self, tool_name, args):
            raise RuntimeError("search unavailable")

    monkeypatch.setattr(
        workflow_module,
        "RuntimeToolInvoker",
        FailingRuntimeToolInvoker,
    )
    workflow = CoordinatedAgentWorkflow(name="deep_research")

    async def fake_run_unit(**kwargs):
        return CoordinationUnitResult(
            role=kwargs["role"],
            answer="answer without required evidence",
        )

    monkeypatch.setattr(workflow, "_run_unit", fake_run_unit)
    context = replace(
        _context(_profile(), "deep_research"),
        kb_ids=[],
        use_search=True,
    )
    result = await workflow._run_research_task(
        task=ResearchTask(
            key="web-failure",
            title="Missing web evidence",
            question="Verify detector evidence",
            expected_tools=("web_search",),
            agent_type="evidence_researcher",
        ),
        index=1,
        role="evidence_researcher",
        kb_circuit_open=asyncio.Event(),
        request=AgentRequest(
            question="Compare detector technologies",
            agent_code="ne",
            user_id="user-1",
            requested_workflow="deep_research",
        ),
        context=context,
    )

    assert result.error is not None
    assert "必需联网检索补做失败: search unavailable" in result.error
    assert result.error.count("必需工具未完成：web_search") == 1


def test_deep_research_request_budget_preserves_per_branch_search_allowance():
    workflow = CoordinatedAgentWorkflow(name="deep_research")
    context = replace(
        _context(_profile(), "deep_research"),
        selected_skills=["nuclear-electronics"],
    )

    budgets = workflow._coordinated_retrieval_budgets(context)

    # Five initial branches plus two possible reflection follow-ups, each with
    # a separately enforced three-call local web-search allowance.
    assert budgets["web_search"] == 21
    assert budgets["multi_kb_search"] == 21


async def test_deep_research_waits_for_all_branches_before_writer():
    order = []

    class Executor:
        def __init__(self, role):
            self.role = role

        async def aquery_stream(self, question, thread_id, runnable_config=None):
            order.append(f"{self.role}:start")
            if self.role == "planner":
                yield {
                    "type": "chunk",
                    "content": (
                        '{"summary":"plan","tasks":['
                        '{"key":"fast","title":"fast","question":"fast",'
                        '"priority":100,"dependencies":[],"budget":1},'
                        '{"key":"slow","title":"slow","question":"slow",'
                        '"priority":10,"dependencies":[],"budget":1}]}'
                    ),
                }
            elif self.role == "domain_analyst":
                order.append("fast:done")
                yield {"type": "chunk", "content": "结论：高优先级证据已经完成。"}
            elif self.role == "data_analyst":
                await asyncio.sleep(0.03)
                order.append("slow:done")
                yield {"type": "chunk", "content": "slow result"}
            elif self.role == "report_writer":
                order.append("writer:started")
                yield {"type": "chunk", "content": "完整最终答案"}
            else:
                raise AssertionError(f"unexpected role: {self.role}")

    class Delegate:
        def build_executor(self, request, context):
            return Executor(request.metadata["coordination_role"])

    results, _tasks = await CoordinatedAgentWorkflow(
        name="deep_research",
        delegate=Delegate(),
    )._run_plan_solve(
        AgentRequest(
            question="research",
            agent_code="ne",
            user_id="user-1",
            requested_workflow="deep_research",
        ),
        _context(_profile(), "deep_research"),
    )

    assert results[-1].role == "report_writer"
    assert results[-1].answer.endswith("完整最终答案")
    assert "当前研究分支没有形成可核验的证据矩阵" in results[-1].answer
    assert "report_writer_draft:start" not in order
    assert order.index("writer:started") > order.index("slow:done")
    assert all(result.answer for result in results)


async def test_deep_research_ignores_planner_dependencies_and_fans_out_first_wave():
    emitted: list[tuple[str, dict]] = []

    class Executor:
        def __init__(self, role):
            self.role = role

        async def aquery_stream(self, question, thread_id, runnable_config=None):
            if self.role == "planner":
                yield {
                    "type": "chunk",
                    "content": (
                        '{"tasks":['
                        '{"key":"t1","title":"facts","question":"facts",'
                        '"priority":90,"agent_type":"evidence_researcher"},'
                        '{"key":"t2","title":"metrics","question":"metrics",'
                        '"priority":80,"dependencies":["t1"],"agent_type":"data_analyst"},'
                        '{"key":"t3","title":"trends","question":"trends",'
                        '"priority":70,"dependencies":["t1"],"agent_type":"domain_analyst"}'
                        "]}"
                    ),
                }
            else:
                yield {"type": "chunk", "content": f"{self.role} result"}

    class Delegate:
        def build_executor(self, request, context):
            return Executor(request.metadata["coordination_role"])

    async def sink(event: tuple[str, dict]) -> None:
        emitted.append(event)

    results, tasks = await CoordinatedAgentWorkflow(
        name="deep_research",
        delegate=Delegate(),
        runtime_budget=AgentRuntimeBudget(deep_research_reflection_rounds=0),
    )._run_plan_solve(
        AgentRequest(
            question="research",
            agent_code="ne",
            user_id="user-1",
            requested_workflow="deep_research",
        ),
        _context(_profile(), "deep_research"),
        event_sink=sink,
    )

    assert [task.dependencies for task in tasks] == [(), (), ()]
    wave_events = [
        payload["event"]
        for event_type, payload in emitted
        if event_type == "workflow.step"
        and payload["event"].get("metadata", {}).get("runtime_type")
        == "scheduler_wave_started"
    ]
    assert wave_events[0]["metadata"]["task_order"] == ["t1", "t2", "t3"]
    assert len([result for result in results if result.role != "planner"]) == 4


async def test_deep_research_report_save_has_independent_timeout(monkeypatch):
    import backend.application.agent_workflows.coordinated_agent_workflow as workflow_module

    class StalledRuntimeToolInvoker:
        def __init__(self, **kwargs):
            pass

        async def invoke(self, tool_name, args):
            assert tool_name == "save_deep_research_report"
            await asyncio.sleep(1)
            return {"artifact_path": "too-late.docx"}

    monkeypatch.setattr(
        workflow_module,
        "RuntimeToolInvoker",
        StalledRuntimeToolInvoker,
    )
    workflow = CoordinatedAgentWorkflow(
        name="deep_research",
        runtime_budget=AgentRuntimeBudget(
            deep_research_report_timeout_seconds=0.03,
        ),
    )

    started_at = time.monotonic()
    events = [
        event
        async for event in workflow._save_report(
            request=AgentRequest(
                question="research",
                agent_code="ne",
                user_id="user-1",
                requested_workflow="deep_research",
            ),
            context=_context(_profile(), "deep_research"),
            content="# completed answer",
            sequence=0,
        )
    ]

    assert time.monotonic() - started_at < 0.2
    terminal_event = events[-1].payload["event"]
    assert terminal_event["title"] == "报告保存超时"
    assert terminal_event["metadata"]["runtime_type"] == "report_save_timed_out"


async def test_deep_research_worker_does_not_reload_workflow_skill():
    captured = {}

    class CapturingExecutor:
        async def aquery_stream(self, question, thread_id, runnable_config=None):
            yield {"type": "chunk", "content": "evidence answer"}

    class CapturingDelegate:
        def build_executor(self, request, context):
            captured["context"] = context
            return CapturingExecutor()

    workflow = CoordinatedAgentWorkflow(
        name="deep_research",
        delegate=CapturingDelegate(),
    )
    context = replace(
        _context(_profile(), "deep_research"),
        selected_skills=["deep-research", "nuclear-electronics"],
    )

    result = await workflow._run_unit(
        role="evidence_researcher",
        question="collect evidence",
        request=AgentRequest(
            question="research",
            agent_code="ne",
            user_id="user-1",
            requested_workflow="deep_research",
        ),
        context=context,
        tool_access=True,
    )

    assert result.answer == "evidence answer"
    assert captured["context"].selected_skills == ["nuclear-electronics"]


async def test_deep_research_ignores_legacy_encompassing_workflow_timeout(monkeypatch):
    workflow = CoordinatedAgentWorkflow(
        name="deep_research",
        runtime_budget=AgentRuntimeBudget(
            workflow_total_timeout_seconds=0.01,
            deep_research_research_phase_timeout_seconds=0.03,
            deep_research_writer_timeout_seconds=0.03,
        ),
    )

    async def stalled_plan_solve(*args, **kwargs):
        await asyncio.sleep(0.05)
        return [], []

    monkeypatch.setattr(workflow, "_run_plan_solve", stalled_plan_solve)
    request = AgentRequest(
        question="research",
        agent_code="ne",
        user_id="user-1",
        requested_workflow="deep_research",
    )

    started_at = time.monotonic()
    events = [
        event
        async for event in workflow.run(
            request,
            _context(_profile(), "deep_research"),
        )
    ]

    assert 0.05 <= time.monotonic() - started_at < 0.3
    assert events[-1].type == "done"
    assert any(event.type == "error" for event in events)
    assert not any(
        event.type == "error" and event.payload.get("error_code") == "agent_timeout"
        for event in events
    )
    assert not any(event.type == "coordination.completed" for event in events)


async def test_deep_research_research_timeout_preserves_writer_budget(monkeypatch):
    emitted: list[tuple[str, dict]] = []
    writer_timeout_overrides: list[float | None] = []
    workflow = CoordinatedAgentWorkflow(
        name="deep_research",
        runtime_budget=AgentRuntimeBudget(
            # Leave enough headroom for loaded-suite scheduler jitter while the
            # one-second branch still deterministically exceeds the phase.
            deep_research_research_phase_timeout_seconds=0.4,
            deep_research_writer_timeout_seconds=0.12,
            deep_research_reflection_rounds=0,
            deep_research_branch_min_start_seconds=0.001,
        ),
    )

    async def fake_run_unit(*, role, timeout_override_seconds=None, **kwargs):
        if role == "planner":
            return CoordinationUnitResult(
                role=role,
                answer=(
                    '{"tasks":[{"key":"t1","title":"slow evidence",'
                    '"question":"collect evidence","priority":100,'
                    '"agent_type":"evidence_researcher"},'
                    '{"key":"t2","title":"fast evidence",'
                    '"question":"collect fast evidence","priority":90,'
                    '"agent_type":"data_analyst"}]}'
                ),
            )
        assert role == "report_writer"
        writer_timeout_overrides.append(timeout_override_seconds)
        await asyncio.sleep(0.05)
        return CoordinationUnitResult(
            role=role, answer="final report from partial evidence"
        )

    async def stalled_research_task(**kwargs):
        if kwargs["task"].key == "t2":
            return CoordinationUnitResult(
                role=kwargs["role"],
                answer="completed evidence before research deadline",
            )
        await asyncio.sleep(1)
        return CoordinationUnitResult(role=kwargs["role"], answer="late evidence")

    async def sink(event):
        emitted.append(event)

    monkeypatch.setattr(workflow, "_run_unit", fake_run_unit)
    monkeypatch.setattr(workflow, "_run_research_task", stalled_research_task)

    started_at = time.monotonic()
    results, _tasks = await workflow._run_plan_solve(
        AgentRequest(
            question="research",
            agent_code="ne",
            user_id="user-1",
            requested_workflow="deep_research",
        ),
        _context(_profile(), "deep_research"),
        event_sink=sink,
    )

    assert time.monotonic() - started_at >= 0.25
    assert results[-1].role == "report_writer"
    assert results[-1].answer.endswith("final report from partial evidence")
    assert any(
        result.answer == "completed evidence before research deadline"
        for result in results
    )
    assert any(result.timed_out for result in results)
    assert writer_timeout_overrides == [0.12]
    runtime_types = [
        payload["event"].get("metadata", {}).get("runtime_type")
        for event_type, payload in emitted
        if event_type == "workflow.step"
    ]
    assert "research_phase_timed_out" in runtime_types
    assert "writer_started" in runtime_types
    assert "writer_completed" in runtime_types


async def test_deep_research_report_falls_back_to_python_docx_and_local_storage(
    monkeypatch,
    tmp_path,
):
    from docx import Document

    import backend.application.tools.providers.deep_research_report_tool as report_tool
    from backend.core.config.paths import Paths

    def no_pandoc():
        raise RuntimeError("pandoc unavailable")

    async def failed_upload(*args, **kwargs):
        raise RuntimeError("minio unavailable")

    monkeypatch.setattr(report_tool, "_resolve_pandoc_executable", no_pandoc)
    monkeypatch.setattr(report_tool, "upload_bytes_to_minio", failed_upload)
    monkeypatch.setattr(report_tool, "get_paths", lambda: Paths(tmp_path))

    payload = await report_tool.save_deep_research_report_direct(
        thread_id=build_internal_thread_key("user-1", "thread-1"),
        title="本地深度研究报告",
        content="# 本地深度研究报告\n\n## 结论\n\n- 可在无 Pandoc 和 MinIO 时生成。",
    )

    report_path = (
        tmp_path
        / "users"
        / "user-1"
        / "threads"
        / "thread-1"
        / "files"
        / "artifacts"
        / str(payload["artifact_path"])
    )
    assert payload["minio_object"] is None
    assert report_path.exists()
    document = Document(report_path)
    assert any(
        "本地深度研究报告" in paragraph.text for paragraph in document.paragraphs
    )
