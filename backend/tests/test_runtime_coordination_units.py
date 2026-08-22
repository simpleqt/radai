"""协调单元执行契约（Fake 执行器基础家族；拆自 test_agent_runtime_policies.py）。"""

import asyncio
import pytest
import time

from backend.application.agent_trace import AgentRunReplay, AgentTraceStore
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


class _FakeExecutor:
    def __init__(self, role: str, calls: list[dict], activity: dict):
        self._role = role
        self._calls = calls
        self._activity = activity

    async def aquery_stream(self, question, thread_id, runnable_config=None):
        self._activity["active"] += 1
        self._activity["max_active"] = max(
            self._activity["max_active"], self._activity["active"]
        )
        self._calls.append(
            {"role": self._role, "question": question, "thread_id": thread_id}
        )
        await asyncio.sleep(0.01)
        self._activity["active"] -= 1
        yield {"type": "chunk", "content": f"{self._role}-answer"}


class _FakeDelegate:
    def __init__(self):
        self.calls: list[dict] = []
        self.activity = {"active": 0, "max_active": 0}

    def build_executor(self, request, context):
        role = request.metadata["coordination_role"]
        return _FakeExecutor(role, self.calls, self.activity)


class _PartiallyFailingDelegate(_FakeDelegate):
    def build_executor(self, request, context):
        role = request.metadata["coordination_role"]
        if role == "risk_reviewer":
            return _FailingExecutor(role, self.calls, self.activity)
        return _FakeExecutor(role, self.calls, self.activity)


class _FailingExecutor(_FakeExecutor):
    async def aquery_stream(self, question, thread_id, runnable_config=None):
        self._calls.append(
            {"role": self._role, "question": question, "thread_id": thread_id}
        )
        raise RuntimeError("branch failed")
        yield  # pragma: no cover


class _PlannedExecutor(_FakeExecutor):
    async def aquery_stream(self, question, thread_id, runnable_config=None):
        self._activity["active"] += 1
        self._activity["max_active"] = max(
            self._activity["max_active"], self._activity["active"]
        )
        self._calls.append(
            {"role": self._role, "question": question, "thread_id": thread_id}
        )
        await asyncio.sleep(0.01)
        self._activity["active"] -= 1
        if self._role == "planner":
            yield {
                "type": "chunk",
                "content": (
                    '{"summary":"plan","tasks":['
                    '{"key":"t1","title":"A","question":"query A"},'
                    '{"key":"t2","title":"B","question":"query B"}]}'
                ),
            }
            return
        yield {"type": "chunk", "content": f"{self._role}-answer"}


class _PlannedDelegate(_FakeDelegate):
    def build_executor(self, request, context):
        role = request.metadata["coordination_role"]
        return _PlannedExecutor(role, self.calls, self.activity)


@pytest.mark.parametrize(
    ("workflow", "expected_roles"),
    [
        ("plan_solve", ["planner", "planner", "domain_analyst", "reviewer"]),
        ("multi_agent_alternating", ["analyst", "critic", "finalizer"]),
    ],
)
async def test_sequential_coordination_runs_isolated_units(workflow, expected_roles):
    delegate = _FakeDelegate()
    profile = _profile()
    request = AgentRequest(
        question="analyze",
        agent_code="ne",
        user_id="user-1",
        requested_workflow=workflow,
    )
    events = [
        event
        async for event in CoordinatedAgentWorkflow(
            name=workflow,
            delegate=delegate,
        ).run(request, _context(profile, workflow))
    ]

    assert [call["role"] for call in delegate.calls] == expected_roles
    assert len({call["thread_id"] for call in delegate.calls}) == len(expected_roles)
    assert events[-2].type == "coordination.completed"
    assert events[-1].type == "done"
    assert any(event.type in {"message.delta", "message.replace"} for event in events)


async def test_coordination_completion_is_degraded_when_any_earlier_unit_failed(
    monkeypatch,
):
    workflow = CoordinatedAgentWorkflow(name="multi_agent_alternating")

    async def run_alternating(_request, _context):
        return [
            CoordinationUnitResult(
                role="analyst",
                answer="partial analysis",
                error="analysis evidence unavailable",
            ),
            CoordinationUnitResult(role="critic", answer="reviewed partial result"),
            CoordinationUnitResult(role="finalizer", answer="bounded final answer"),
        ]

    monkeypatch.setattr(workflow, "_run_alternating", run_alternating)
    request = AgentRequest(
        question="analyze",
        agent_code="ne",
        user_id="user-1",
        requested_workflow="multi_agent_alternating",
    )
    events = [
        event
        async for event in workflow.run(
            request,
            _context(_profile(), "multi_agent_alternating"),
        )
    ]

    completion = next(
        event for event in events if event.type == "coordination.completed"
    )
    assert completion.payload["status"] == "degraded"
    assert completion.payload["degraded"] is True


async def test_coordinated_workflow_writes_trace_when_enabled(tmp_path):
    delegate = _FakeDelegate()
    profile = _profile()
    context = _context(profile, "plan_solve")
    context = AgentRuntimeContext(
        **{
            **context.__dict__,
            "run_id": "trace-run-1",
            "trace_enabled": True,
            "request_metadata": {"agent_trace_root": str(tmp_path)},
        }
    )
    request = AgentRequest(
        question="analyze",
        agent_code="ne",
        user_id="user-1",
        requested_workflow="plan_solve",
    )

    events = [
        event
        async for event in CoordinatedAgentWorkflow(
            name="plan_solve",
            delegate=delegate,
        ).run(request, context)
    ]

    store = AgentTraceStore(tmp_path)
    records = store.read_records("trace-run-1")
    summary = AgentRunReplay(store).dry_run("trace-run-1")

    assert events[-1].type == "done"
    assert summary.status == "completed"
    assert summary.events >= 3
    assert summary.units >= 3
    assert summary.handoffs >= 3
    assert any(record.kind == "run" for record in records)
    assert any(
        record.kind == "event" and record.payload["event_type"] == "workflow.started"
        for record in records
    )
    assert any(
        record.kind == "handoff"
        and record.payload["payload"]["schema_version"] == "2.0"
        for record in records
    )


async def test_research_units_with_same_role_use_distinct_task_threads():
    delegate = _FakeDelegate()
    workflow = CoordinatedAgentWorkflow(name="plan_solve", delegate=delegate)
    request = AgentRequest(
        question="analyze",
        agent_code="ne",
        user_id="user-1",
        requested_workflow="plan_solve",
    )
    context = _context(_profile(), "plan_solve")

    await workflow._run_unit(
        role="evidence_researcher",
        question="first",
        request=request,
        context=context,
        task_id="req-1:research:1",
    )
    await workflow._run_unit(
        role="evidence_researcher",
        question="second",
        request=request,
        context=context,
        task_id="req-1:research:2",
    )

    assert len({call["thread_id"] for call in delegate.calls}) == 2
    assert delegate.calls[0]["thread_id"].endswith("req-1:research:1")
    assert delegate.calls[1]["thread_id"].endswith("req-1:research:2")


async def test_parallel_coordination_runs_branches_concurrently_then_synthesizes():
    import backend.application.agent_workflows.coordinated_agent_workflow as workflow_module

    delegate = _FakeDelegate()
    profile = _profile()
    request = AgentRequest(
        question="analyze",
        agent_code="ne",
        user_id="user-1",
        requested_workflow="multi_agent_parallel",
    )
    events = [
        event
        async for event in CoordinatedAgentWorkflow(
            name="multi_agent_parallel",
            delegate=delegate,
        ).run(request, _context(profile, "multi_agent_parallel"))
    ]

    assert [call["role"] for call in delegate.calls[-1:]] == ["synthesizer"]
    assert {call["role"] for call in delegate.calls[:4]} == {
        "domain_analyst",
        "data_analyst",
        "evidence_researcher",
        "risk_reviewer",
    }
    assert delegate.calls[4]["role"] == "solution_architect"
    configured_capacity = min(
        5,
        workflow_module._MAX_CONCURRENT_RESEARCH,
        workflow_module._MAX_CONCURRENT_PER_USER_MODEL,
    )
    # Capacity is an upper bound, not a promise that every 10ms fake operation
    # overlaps on a loaded CI runner.
    assert 2 <= delegate.activity["max_active"] <= configured_capacity
    assert events[-2].payload["successful_units"] == 6
    assert "前置分支结构化交接" in delegate.calls[4]["question"]
    assert '<agent_handoff role="risk_reviewer">' in delegate.calls[4]["question"]
    assert '<agent_handoff role="domain_analyst">' in delegate.calls[-1]["question"]
    assert "结构化分支交接" in delegate.calls[-1]["question"]


async def test_research_scheduler_runs_five_independent_branches_in_one_wave(
    monkeypatch,
):
    import backend.application.agent_workflows.coordination_runtime_support as budget_module

    monkeypatch.setattr(budget_module, "_MAX_CONCURRENT_RESEARCH", 5)
    monkeypatch.setattr(budget_module, "_MAX_CONCURRENT_PER_USER_MODEL", 5)
    budget_module._RESEARCH_LIMITERS.clear()
    delegate = _FakeDelegate()
    workflow = CoordinatedAgentWorkflow(name="plan_solve", delegate=delegate)
    tasks = [
        ResearchTask(
            key=f"t{index}",
            title=f"task {index}",
            question=f"question {index}",
            priority=100 - index,
            budget=5,
            agent_type="general_analyst",
        )
        for index in range(1, 6)
    ]

    results = await workflow._run_scheduled_research_tasks(
        scheduled_tasks=list(enumerate(tasks, start=1)),
        used_fallback=False,
        kb_circuit_open=asyncio.Event(),
        request=AgentRequest(
            question="analyze",
            agent_code="ne",
            user_id="user-1",
            requested_workflow="plan_solve",
        ),
        context=_context(_profile(), "plan_solve"),
        event_sink=None,
        analysis_gate=None,
    )

    assert len(results) == 5
    # All five are admitted in the same scheduler wave. Exact 10ms overlap is
    # not guaranteed because executor construction crosses the bounded
    # blocking pool before the async stream starts.
    assert 2 <= delegate.activity["max_active"] <= 5


async def test_parallel_coordination_isolates_branch_failure_and_still_synthesizes():
    delegate = _PartiallyFailingDelegate()
    profile = _profile()
    request = AgentRequest(
        question="analyze",
        agent_code="ne",
        user_id="user-1",
        requested_workflow="multi_agent_parallel",
    )
    events = [
        event
        async for event in CoordinatedAgentWorkflow(
            name="multi_agent_parallel",
            delegate=delegate,
        ).run(request, _context(profile, "multi_agent_parallel"))
    ]

    assert any(
        event.type == "coordination.unit.failed"
        and event.payload["role"] == "risk_reviewer"
        for event in events
    )
    assert delegate.calls[-1]["role"] == "synthesizer"
    assert events[-2].payload["successful_units"] == 5


async def test_parallel_first_wave_isolates_exceptions_escaping_the_unit_runner(
    monkeypatch,
):
    """从 _run_unit 逃逸的异常(spawn/预算/基础设施类)不得中止整波。

    单元运行器内部的执行失败已被归一化为 error 结果(上一个测试覆盖);
    这里守护的是 gather 层的分支隔离:失败分支降级为 error 结果,兄弟
    分支证据保留,架构师与聚合器照常运行。
    """

    workflow = CoordinatedAgentWorkflow(
        name="multi_agent_parallel",
        delegate=_FakeDelegate(),
    )
    context = _context(_profile(), "multi_agent_parallel")
    request = AgentRequest(
        question="analyze",
        agent_code="ne",
        user_id="user-1",
        requested_workflow="multi_agent_parallel",
    )

    async def fake_run_unit(*, role, **_kwargs):
        if role == "risk_reviewer":
            raise RuntimeError("spawn rejected")
        return CoordinationUnitResult(role=role, answer=f"{role}-answer")

    monkeypatch.setattr(workflow, "_run_unit", fake_run_unit)

    results = await workflow._run_parallel(request, context)

    by_role = {result.role: result for result in results}
    assert by_role["risk_reviewer"].error == "spawn rejected"
    assert by_role["risk_reviewer"].answer == ""
    assert by_role["domain_analyst"].answer == "domain_analyst-answer"
    assert by_role["synthesizer"].answer == "synthesizer-answer"


async def test_failed_terminal_unit_does_not_publish_an_intermediate_answer():
    class Delegate(_FakeDelegate):
        def build_executor(self, request, context):
            role = request.metadata["coordination_role"]
            if role == "finalizer":
                return _FailingExecutor(role, self.calls, self.activity)
            return _FakeExecutor(role, self.calls, self.activity)

    request = AgentRequest(
        question="analyze",
        agent_code="ne",
        user_id="user-1",
        requested_workflow="multi_agent_alternating",
    )
    events = [
        event
        async for event in CoordinatedAgentWorkflow(
            name="multi_agent_alternating",
            delegate=Delegate(),
        ).run(request, _context(_profile(), "multi_agent_alternating"))
    ]

    assert events[-1].type == "done"
    assert not any(event.type == "coordination.completed" for event in events)
    assert not any(
        event.type in {"message.delta", "message.replace"} for event in events
    )
    assert any(
        event.type == "error"
        and event.payload.get("error_code") == "coordination_finalization_failed"
        for event in events
    )


async def test_plan_solve_forces_selected_kb_and_web_search_per_subagent(monkeypatch):
    import backend.application.agent_workflows.coordinated_agent_workflow as workflow_module

    kb_calls = []
    web_calls = []
    search_activity = {"kb_active": 0, "kb_max": 0, "web_active": 0, "web_max": 0}

    class FakeRuntimeToolInvoker:
        def __init__(self, *, context, **kwargs):
            self.context = context

        async def invoke(self, tool_name, args):
            if tool_name == "multi_kb_search":
                search_activity["kb_active"] += 1
                search_activity["kb_max"] = max(
                    search_activity["kb_max"],
                    search_activity["kb_active"],
                )
                kb_calls.append({"kb_ids": list(self.context.kb_ids), **args})
                await asyncio.sleep(0.01)
                search_activity["kb_active"] -= 1
                return "kb evidence"
            if tool_name == "web_search":
                search_activity["web_active"] += 1
                search_activity["web_max"] = max(
                    search_activity["web_max"],
                    search_activity["web_active"],
                )
                web_calls.append(args)
                await asyncio.sleep(0.01)
                search_activity["web_active"] -= 1
                return '{"query":"q","results":[]}'
            raise AssertionError(f"unexpected tool: {tool_name}")

    monkeypatch.setattr(workflow_module, "RuntimeToolInvoker", FakeRuntimeToolInvoker)

    delegate = _PlannedDelegate()
    profile = _profile()
    context = _context(profile, "plan_solve")
    context = AgentRuntimeContext(
        **{
            **context.__dict__,
            "kb_ids": ["kb-a", "kb-b"],
            "use_search": True,
        }
    )
    workflow = CoordinatedAgentWorkflow(name="plan_solve", delegate=delegate)

    results, tasks = await workflow._run_plan_solve(
        AgentRequest(
            question="analyze",
            agent_code="ne",
            user_id="user-1",
            requested_workflow="plan_solve",
        ),
        context,
    )

    assert [task.question for task in tasks] == ["query A", "query B"]
    assert len(kb_calls) == 2
    assert all(call["kb_ids"] == ["kb-a", "kb-b"] for call in kb_calls)
    assert [call["query"] for call in web_calls] == ["query A", "query B"]
    assert search_activity["kb_max"] == 2
    assert search_activity["web_max"] == 2
    assert {result.role for result in results} >= {
        "planner",
        "domain_analyst",
        "data_analyst",
        "reviewer",
    }
    assert delegate.activity["max_active"] == 2
    reviewer_call = next(call for call in delegate.calls if call["role"] == "reviewer")
    assert '<agent_handoff role="domain_analyst">' in reviewer_call["question"]
    assert "不含工具执行轨迹" in reviewer_call["question"]


async def test_alternating_coordination_passes_bounded_result_only_handoffs():
    delegate = _FakeDelegate()
    workflow = CoordinatedAgentWorkflow(
        name="multi_agent_alternating",
        delegate=delegate,
    )

    results = await workflow._run_alternating(
        AgentRequest(
            question="analyze",
            agent_code="ne",
            user_id="user-1",
            requested_workflow="multi_agent_alternating",
        ),
        _context(_profile(), "multi_agent_alternating"),
    )

    assert [result.role for result in results] == ["analyst", "critic", "finalizer"]
    critic_call = next(call for call in delegate.calls if call["role"] == "critic")
    finalizer_call = next(
        call for call in delegate.calls if call["role"] == "finalizer"
    )
    assert '<agent_handoff role="analyst">' in critic_call["question"]
    assert '<agent_handoff role="critic">' in finalizer_call["question"]
    assert "已有结构化交接" in finalizer_call["question"]


async def test_plan_solve_stops_followup_kb_searches_after_timeout(monkeypatch):
    import backend.application.agent_workflows.coordinated_agent_workflow as workflow_module

    kb_calls = []

    class StalledRuntimeToolInvoker:
        def __init__(self, *, context, **kwargs):
            self.context = context

        async def invoke(self, tool_name, args):
            if tool_name != "multi_kb_search":
                raise AssertionError(f"unexpected tool: {tool_name}")
            kb_calls.append({"kb_ids": list(self.context.kb_ids), **args})
            await asyncio.sleep(0.15)
            return "late evidence"

    monkeypatch.setattr(
        workflow_module, "RuntimeToolInvoker", StalledRuntimeToolInvoker
    )
    import backend.application.agent_workflows.coordination_runtime_support as budget_module

    monkeypatch.setattr(budget_module, "_KB_SEARCH_TIMEOUT_SECONDS", 0.03)

    context = _context(_profile(), "plan_solve")
    context = AgentRuntimeContext(
        **{
            **context.__dict__,
            "kb_ids": ["kb-a"],
        }
    )
    results, tasks = await CoordinatedAgentWorkflow(
        name="plan_solve",
        delegate=_PlannedDelegate(),
    )._run_plan_solve(
        AgentRequest(
            question="analyze",
            agent_code="ne",
            user_id="user-1",
            requested_workflow="plan_solve",
        ),
        context,
    )

    assert len(tasks) == 2
    assert len(kb_calls) == 2
    from backend.core.tools.request_limits import get_retrieval_request_cache

    assert get_retrieval_request_cache().provider_requests_cancelled() is True


async def test_deep_research_generates_downloadable_report_event(monkeypatch):
    import backend.application.agent_workflows.coordinated_agent_workflow as workflow_module

    class FakeRuntimeToolInvoker:
        def __init__(self, **kwargs):
            pass

        async def invoke(self, tool_name, args):
            if tool_name != "save_deep_research_report":
                raise AssertionError(f"unexpected tool: {tool_name}")
            return {
                "title": "深度研究报告",
                "artifact_path": "report.docx",
                "minio_object": "users/u/report.docx",
            }

    async def fake_artifact_metadata(**kwargs):
        return {"id": "file-1", "title": "深度研究报告", "url": "/download/report"}

    monkeypatch.setattr(workflow_module, "RuntimeToolInvoker", FakeRuntimeToolInvoker)
    monkeypatch.setattr(
        workflow_module, "build_registered_artifact_metadata", fake_artifact_metadata
    )

    delegate = _FakeDelegate()
    profile = _profile()
    context = _context(profile, "deep_research")
    request = AgentRequest(
        question="research topic",
        agent_code="ne",
        user_id="user-1",
        requested_workflow="deep_research",
        use_search=False,
    )

    events = [
        event
        async for event in CoordinatedAgentWorkflow(
            name="deep_research",
            delegate=delegate,
            runtime_budget=AgentRuntimeBudget(deep_research_reflection_rounds=0),
        ).run(request, context)
    ]

    task_starts = [
        event.payload["event"]
        for event in events
        if event.type == "workflow.step"
        and isinstance(event.payload.get("event"), dict)
        and event.payload["event"].get("kind") == "task_start"
    ]
    artifact_events = [
        event.payload["event"]
        for event in events
        if event.type == "workflow.step"
        and isinstance(event.payload.get("event"), dict)
        and event.payload["event"].get("kind") == "artifact"
    ]

    assert len(task_starts) == 3
    assert any(
        event.type == "workflow.step"
        and isinstance(event.payload.get("event"), dict)
        and event.payload["event"].get("metadata", {}).get("partial_handoff") is True
        for event in events
    )
    assert artifact_events[0]["metadata"]["url"] == "/download/report"
    assert not any(
        event.type == "message.delta"
        and event.payload.get("metadata", {}).get("streaming_final_answer") is True
        for event in events
    )
    replace_index = next(
        index for index, event in enumerate(events) if event.type == "message.replace"
    )
    assert "report_writer-answer" in events[replace_index].payload["content"]
    assert "待核验" in events[replace_index].payload["content"]
    first_task_index = next(
        index
        for index, event in enumerate(events)
        if event.type == "workflow.step"
        and isinstance(event.payload.get("event"), dict)
        and event.payload["event"].get("kind") == "task_start"
    )
    answer_index = replace_index
    assert first_task_index < answer_index
    assert any(
        event.type == "workflow.step"
        and isinstance(event.payload.get("event"), dict)
        and event.payload["event"].get("kind") == "artifact_update"
        for event in events
    )


async def test_unit_runtime_role_manifest_controls_result_only_context():
    captured = {}

    class CapturingExecutor:
        async def aquery_stream(self, question, thread_id, runnable_config=None):
            captured["question"] = question
            captured["thread_id"] = thread_id
            captured["runnable_config"] = runnable_config
            yield {"type": "chunk", "content": "data answer"}

    class CapturingDelegate:
        def build_executor(self, request, context):
            captured["request_metadata"] = dict(request.metadata)
            captured["context"] = context
            return CapturingExecutor()

    workflow = CoordinatedAgentWorkflow(
        name="plan_solve",
        delegate=CapturingDelegate(),
    )
    base_context = _context(_profile(), "plan_solve")
    context = replace(
        base_context,
        use_search=True,
        kb_ids=["kb-1"],
        model_name="parent-model",
    )
    result = await workflow._run_unit(
        role="reviewer",
        question="review final answer",
        request=AgentRequest(
            question="research",
            agent_code="ne",
            user_id="user-1",
            requested_workflow="plan_solve",
        ),
        context=context,
        tool_access=True,
        allowed_tools={"web_search", "multi_kb_search"},
    )

    unit_context = captured["context"]
    metadata = captured["request_metadata"]
    assert result.answer == "data answer"
    assert unit_context.use_search is False
    assert unit_context.kb_ids == []
    assert metadata["runtime_agent"] == "reviewer"
    assert metadata["requested_tool_access"] is True
    assert metadata["tool_access"] is False
    assert metadata["runtime_agent_can_use_network"] is False
    assert metadata["runtime_agent_can_read_kb"] is False
    assert metadata["coordination_allowed_tools"] == []
    assert unit_context.agent_specs["reviewer"].system_prompt
    assert captured["runnable_config"]["recursion_limit"] == 48


async def test_unit_metadata_only_advertises_tools_available_in_context():
    captured = {}

    class CapturingExecutor:
        async def aquery_stream(self, question, thread_id, runnable_config=None):
            yield {"type": "chunk", "content": "evidence answer"}

    class CapturingDelegate:
        def build_executor(self, request, context):
            captured["metadata"] = dict(request.metadata)
            captured["context"] = context
            return CapturingExecutor()

    workflow = CoordinatedAgentWorkflow(
        name="deep_research",
        delegate=CapturingDelegate(),
    )
    context = replace(
        _context(_profile(), "deep_research"),
        use_search=True,
        kb_ids=[],
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
        allowed_tools={"web_search", "multi_kb_search"},
    )

    assert result.error is None
    assert captured["metadata"]["coordination_allowed_tools"] == ["web_search"]
    assert captured["context"].kb_ids == []


async def test_kb_search_does_not_block_api_event_loop(monkeypatch):
    import backend.application.agent_workflows.coordinated_agent_workflow as workflow_module

    captured: dict[str, object] = {}

    class LoopCapturingRuntimeToolInvoker:
        """新契约:分支检索原生跑在调用方事件循环上(旧桥是一次性循环)。

        检索栈的 loop-safety 由检索实现自身保证(标准工作流的工具节点本就
        在主循环上调用同一实现);本测试锁定 (1) 不再切换事件循环,
        (2) 异步检索在飞行中时 API 循环保持响应。
        """

        def __init__(self, **kwargs):
            pass

        async def invoke(self, tool_name, args):
            if tool_name != "multi_kb_search":
                raise AssertionError(f"unexpected tool: {tool_name}")
            captured["loop"] = asyncio.get_running_loop()
            await asyncio.sleep(0.15)
            return "kb evidence"

    monkeypatch.setattr(
        workflow_module,
        "RuntimeToolInvoker",
        LoopCapturingRuntimeToolInvoker,
    )
    workflow = CoordinatedAgentWorkflow(
        name="deep_research",
        delegate=_FakeDelegate(),
    )
    context = _context(_profile(), "deep_research")
    context = AgentRuntimeContext(
        **{
            **context.__dict__,
            "kb_ids": ["kb-a"],
        }
    )
    started_at = time.monotonic()
    research_task = asyncio.create_task(
        workflow._run_research_task(
            task=workflow_module.ResearchTask("t1", "T", "query"),
            index=1,
            role="researcher_1",
            kb_circuit_open=asyncio.Event(),
            request=AgentRequest(
                question="research",
                agent_code="ne",
                user_id="user-1",
                requested_workflow="deep_research",
            ),
            context=context,
        )
    )
    await asyncio.sleep(0.02)
    event_loop_delay = time.monotonic() - started_at
    result = await research_task

    assert event_loop_delay < 0.1
    assert captured["loop"] is asyncio.get_running_loop()
    assert "researcher_1-answer" in result.answer
