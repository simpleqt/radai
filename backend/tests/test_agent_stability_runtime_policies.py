"""运行时策略稳定性:工作流附件/trace、规划器依赖、工具调用超时/trace、回滚与特征派生。(拆自 test_agent_stability_regressions.py)"""

from __future__ import annotations

import asyncio
from types import SimpleNamespace

import pytest
from pydantic import ValidationError

from backend.api.routers import conversations as conversations_router
from backend.api.routers.agent_runtime import AgentRuntimeQueryRequest
from backend.application.agent_trace import AgentRunReplay, AgentTraceStore
from backend.application.agent_workflows.coordinated_agent_workflow import (
    CoordinatedAgentWorkflow,
)
from backend.application.agent_workflows.coordination_types import (
    CoordinationUnitResult,
    ResearchTask,
)
from backend.application.agent_workflows.standard_agent_workflow import (
    StandardAgentWorkflow,
)
from backend.application.conversations import ConversationAgentMismatchError
from backend.config.handlers import KafkaLogHandler
from backend.domain.agents.profile import AgentProfile
from backend.domain.agents.request import AgentRequest
from backend.domain.agents.runtime_context import AgentRuntimeContext


def _context(profile: AgentProfile) -> AgentRuntimeContext:
    return AgentRuntimeContext(
        request_id="request-1",
        user_id="user-1",
        external_thread_id="thread-1",
        internal_thread_id="user-1:thread-1",
        agent_profile=profile,
        workflow_name="full",
        model_name=None,
        kb_ids=[],
        selected_skills=[],
        use_search=False,
        subagent_enabled=False,
        thinking_enabled=False,
    )


async def test_standard_workflow_forwards_attachments_only_when_present():
    calls: list[dict] = []

    class FakeExecutor:
        async def aquery_stream(self, question, thread_id, runnable_config=None, **kwargs):
            calls.append(kwargs)
            yield {"type": "chunk", "content": "ok"}

    class Registry:
        def build(self, profile, **kwargs):
            return FakeExecutor()

    profile = AgentProfile(code="ne", name="NE", domain="nuclear-electronics")
    workflow = StandardAgentWorkflow(name="full", executor_registry=Registry())
    request = AgentRequest(
        question="看图",
        agent_code="ne",
        user_id="user-1",
        attachments=[{"base64": "aGVsbG8=", "mime_type": "image/png"}],
    )
    _ = [event async for event in workflow.run(request, _context(profile))]
    assert calls == [{"image_contents": request.attachments}]


async def test_standard_workflow_writes_trace_when_enabled(tmp_path, monkeypatch):
    import backend.application.agent_workflows.standard_agent_workflow as workflow_module

    monkeypatch.setattr(workflow_module.settings, "follow_up_suggestions_enabled", False)

    class FakeExecutor:
        async def aquery_stream(self, question, thread_id, runnable_config=None, **kwargs):
            yield {"type": "chunk", "content": "standard answer"}

    class Registry:
        def build(self, profile, **kwargs):
            return FakeExecutor()

    profile = AgentProfile(code="ne", name="NE", domain="nuclear-electronics")
    context = _context(profile)
    context = AgentRuntimeContext(
        **{
            **context.__dict__,
            "run_id": "standard-trace-1",
            "trace_enabled": True,
            "request_metadata": {"agent_trace_root": str(tmp_path)},
        }
    )
    workflow = StandardAgentWorkflow(name="full", executor_registry=Registry())

    events = [
        event
        async for event in workflow.run(
            AgentRequest(question="hello", agent_code="ne", user_id="user-1"),
            context,
        )
    ]

    store = AgentTraceStore(tmp_path)
    records = store.read_records("standard-trace-1")
    summary = AgentRunReplay(store).dry_run("standard-trace-1")

    assert [event.type for event in events] == [
        "workflow.started",
        "message.delta",
        "done",
    ]
    assert summary.status == "completed"
    assert summary.events == 3
    assert any(
        record.kind == "event"
        and record.payload["event_type"] == "workflow.started"
        for record in records
    )
    assert any(
        record.kind == "event" and record.payload["event_type"] == "done"
        for record in records
    )


def test_agent_runtime_request_rejects_oversized_inputs():
    with pytest.raises(ValidationError):
        AgentRuntimeQueryRequest(question="x" * 50_001)
    with pytest.raises(ValidationError):
        AgentRuntimeQueryRequest(question="q", kb_ids=["kb"] * 65)
    with pytest.raises(ValidationError):
        AgentRuntimeQueryRequest(question="q", metadata={"payload": "x" * 70_000})
    with pytest.raises(ValidationError):
        AgentRuntimeQueryRequest(question="q", thread_id="invalid:thread")
    with pytest.raises(ValidationError):
        AgentRuntimeQueryRequest(
            question="q",
            attachments=[{"image_url": "file:///etc/passwd", "mime_type": "image/png"}],
        )
    with pytest.raises(ValidationError):
        AgentRuntimeQueryRequest(
            question="q",
            attachments=[{"base64": "not-base64", "mime_type": "image/png"}],
        )

    payload = AgentRuntimeQueryRequest(
        question="q",
        thread_id="valid-thread_1",
        attachments=[{"image_url": "https://example.com/a.png", "mime_type": "image/png"}],
    )
    assert payload.attachments[0].image_url == "https://example.com/a.png"


async def test_plan_solve_respects_research_task_dependencies(monkeypatch):
    profile = AgentProfile(
        code="groundwater",
        name="Groundwater",
        domain="groundwater-project-analysis",
    )
    workflow = CoordinatedAgentWorkflow(name="plan_solve")
    started: list[str] = []
    started_events = {
        "facts": asyncio.Event(),
        "analysis": asyncio.Event(),
        "solution": asyncio.Event(),
    }
    releases = {
        "facts": asyncio.Event(),
        "analysis": asyncio.Event(),
        "solution": asyncio.Event(),
    }

    async def fake_run_research_task(**kwargs):
        task = kwargs["task"]
        started.append(task.key)
        started_events[task.key].set()
        await releases[task.key].wait()
        return CoordinationUnitResult(
            role=kwargs["role"],
            answer=f"answer {task.key}",
            task_key=task.key,
            task_title=task.title,
        )

    monkeypatch.setattr(workflow, "_run_research_task", fake_run_research_task)
    scheduled_tasks = [
        (1, ResearchTask(key="facts", title="Facts", question="q1", priority=10)),
        (
            2,
            ResearchTask(
                key="analysis",
                title="Analysis",
                question="q2",
                priority=9,
                dependencies=("facts",),
            ),
        ),
        (
            3,
            ResearchTask(
                key="solution",
                title="Solution",
                question="q3",
                priority=8,
                dependencies=("analysis",),
            ),
        ),
    ]
    runner = asyncio.create_task(
        workflow._run_scheduled_research_tasks(
            scheduled_tasks=scheduled_tasks,
            used_fallback=False,
            kb_circuit_open=asyncio.Event(),
            request=AgentRequest(
                question="q",
                agent_code="groundwater",
                user_id="user-1",
            ),
            context=_context(profile),
            event_sink=None,
            analysis_gate=None,
        )
    )
    try:
        await asyncio.wait_for(started_events["facts"].wait(), timeout=1)
        assert started == ["facts"]
        releases["facts"].set()
        await asyncio.wait_for(started_events["analysis"].wait(), timeout=1)
        assert started == ["facts", "analysis"]
        releases["analysis"].set()
        await asyncio.wait_for(started_events["solution"].wait(), timeout=1)
        assert started == ["facts", "analysis", "solution"]
    finally:
        for release in releases.values():
            release.set()
        results = await runner
    assert [result.task_key for result in results] == [
        "facts",
        "analysis",
        "solution",
    ]


async def test_project_data_contract_failure_marks_branch_failed(monkeypatch):
    profile = AgentProfile(
        code="groundwater",
        name="Groundwater",
        domain="groundwater-project-analysis",
    )
    workflow = CoordinatedAgentWorkflow(name="plan_solve")
    events: list[tuple[str, dict]] = []

    async def fake_run_unit(**kwargs):
        return CoordinationUnitResult(
            role=kwargs["role"],
            answer="根据项目数据分析得到结论",
            process_events=(),
        )

    async def sink(event):
        events.append(event)

    monkeypatch.setattr(workflow, "_run_unit", fake_run_unit)
    result = await workflow._run_research_task(
        task=ResearchTask(
            key="project-data",
            title="Project Data",
            question="分析项目数据",
            expected_tools=("query_project_tables",),
            agent_type="data_analyst",
        ),
        index=1,
        role="data_analyst",
        kb_circuit_open=asyncio.Event(),
        request=AgentRequest(
            question="q",
            agent_code="groundwater",
            user_id="user-1",
        ),
        context=_context(profile),
        event_sink=sink,
        analysis_gate=None,
    )

    assert result.error is not None
    assert "query_project_tables" in result.error
    task_end = [
        payload["event"]
        for event_type, payload in events
        if event_type == "workflow.step" and payload["event"]["kind"] == "task_end"
    ][-1]
    assert task_end["level"] == "warning"
    assert task_end["metadata"]["runtime_type"] == "task_failed"


def test_planner_normalizes_duplicate_and_invalid_dependencies():
    workflow = CoordinatedAgentWorkflow(name="deep_research")
    tasks = workflow._parse_research_tasks(
        '{"tasks":['
        '{"key":"t1","question":"a","dependencies":["missing","t1"]},'
        '{"key":"t1","question":"b","dependencies":["t1"]}'
        "]}",
        "q",
    )
    assert [task.key for task in tasks] == ["t1", "t1_2"]
    assert [task.agent_type for task in tasks] == ["domain_analyst", "data_analyst"]
    assert tasks[0].dependencies == ()
    assert tasks[1].dependencies == ("t1",)


def test_planner_assigns_capability_roles_and_orders_solution_after_evidence():
    workflow = CoordinatedAgentWorkflow(name="deep_research")
    tasks = workflow._parse_research_tasks(
        '{"tasks":['
        '{"key":"facts","question":"facts","agent_type":"evidence_researcher"},'
        '{"key":"data","question":"data","agent_type":"data_analyst"},'
        '{"key":"risk","question":"risk","agent_type":"risk_reviewer"},'
        '{"key":"plan","question":"plan","agent_type":"solution_architect"}'
        "]}",
        "q",
    )

    assert [task.agent_type for task in tasks] == [
        "evidence_researcher",
        "data_analyst",
        "risk_reviewer",
        "solution_architect",
    ]
    assert tasks[-1].dependencies == ("facts", "data", "risk")


def test_planner_preserves_explicit_solution_dependency_subset():
    workflow = CoordinatedAgentWorkflow(name="deep_research")
    tasks = workflow._parse_research_tasks(
        '{"tasks":['
        '{"key":"facts","question":"facts","agent_type":"evidence_researcher"},'
        '{"key":"risk","question":"risk","agent_type":"risk_reviewer"},'
        '{"key":"plan","question":"plan","agent_type":"solution_architect",'
        '"dependencies":["facts"]}'
        "]}",
        "q",
    )

    assert tasks[-1].dependencies == ("facts",)


def test_kafka_handler_does_not_connect_in_constructor(monkeypatch):
    calls: list[bool] = []
    monkeypatch.setattr(KafkaLogHandler, "_init_producer", lambda self: calls.append(True))
    handler = KafkaLogHandler("127.0.0.1:1", flush_timeout=0.01)
    try:
        assert calls == []
    finally:
        handler.close()


async def test_agent_service_returns_conflict_for_cross_agent_thread(monkeypatch):
    from backend.application.agent_service import AgentService

    profile = AgentProfile(code="csh", name="CSH", domain="csh-hydrogeology")
    context = _context(profile)

    class Store:
        async def ensure_runtime_conversation(self, **kwargs):
            raise ConversationAgentMismatchError("owned by ne")

    class Workflow:
        async def run(self, request, runtime_context):
            raise AssertionError("workflow must not run")
            yield

    service = AgentService(conversation_store=Store())
    monkeypatch.setattr(service, "_resolve_workflow", lambda request: (Workflow(), context))
    events = [
        event
        async for event in service.run_stream(
            AgentRequest(question="q", agent_code="csh", user_id="user-1")
        )
    ]

    assert [event.type for event in events] == ["error", "done"]
    assert events[0].payload["error_code"] == "conversation_state_conflict"


async def test_conversation_rollback_uses_persisted_agent_runtime(monkeypatch):
    captured: list[AgentRequest] = []
    conversation = SimpleNamespace(
        metadata_={
            "agent_runtime": {
                "agent_code": "csh",
                "workflow_name": "full",
                "request_metadata": {"domain": "hydro"},
            }
        }
    )

    class Result:
        def scalar_one_or_none(self):
            return conversation

    class Session:
        async def execute(self, statement):
            return Result()

    class Graph:
        async def aget_state(self, config):
            return None

    class Service:
        def __init__(self, *, checkpointer):
            pass

        async def build_executor_async(self, request):
            captured.append(request)
            return SimpleNamespace(graph=Graph())

    monkeypatch.setattr(conversations_router, "AgentService", Service)
    request = SimpleNamespace(
        app=SimpleNamespace(state=SimpleNamespace(checkpointer=object()))
    )
    user = SimpleNamespace(user_id="user-1")

    result = await conversations_router.rollback_conversation(
        "thread-1",
        conversations_router.RollbackRequest(keep_user_turns=1),
        request,
        user,
        Session(),
    )

    assert result["success"] is True
    assert captured[0].agent_code == "csh"
    assert captured[0].requested_workflow == "full"
    assert captured[0].metadata == {
        "domain": "hydro",
        "source": "conversation.rollback",
    }


async def test_runtime_tool_invoker_applies_timeout(monkeypatch):
    from backend.application.tools.runtime_invoker import RuntimeToolInvoker

    monkeypatch.setenv("AGENT_TOOL_TIMEOUT_SECONDS", "0.01")
    invoker = object.__new__(RuntimeToolInvoker)

    with pytest.raises(TimeoutError, match="slow_tool"):
        await invoker._await_tool("slow_tool", asyncio.sleep(1))


async def test_runtime_tool_invoker_writes_tool_call_trace(tmp_path, monkeypatch):
    import backend.application.tools.runtime_invoker as runtime_invoker_module
    from backend.application.tools.runtime_invoker import RuntimeToolInvoker

    class FakeTool:
        name = "echo"

        async def ainvoke(self, args):
            return {"echo": args}

    class FakePipeline:
        def __init__(self, **kwargs):
            pass

        def assemble(self):
            return [FakeTool()]

    class FakeGuardrail:
        def __init__(self, **kwargs):
            pass

        async def aevaluate(self, request):
            return SimpleNamespace(allow=True, reasons=[])

    monkeypatch.setattr(runtime_invoker_module, "ToolAssemblyPipeline", FakePipeline)
    monkeypatch.setattr(
        runtime_invoker_module,
        "RuntimePolicyGuardrailProvider",
        FakeGuardrail,
    )

    profile = AgentProfile(
        code="ne",
        name="NE",
        domain="nuclear-electronics",
        tool_policy="ne_default",
    )
    context = _context(profile)
    context = AgentRuntimeContext(
        **{
            **context.__dict__,
            "run_id": "tool-trace-1",
            "trace_enabled": True,
            "request_metadata": {"agent_trace_root": str(tmp_path)},
        }
    )
    invoker = RuntimeToolInvoker(context=context, workflow_name="full")

    result = await invoker.invoke("echo", {"query": "hello"})

    store = AgentTraceStore(tmp_path)
    records = store.read_records("tool-trace-1")
    summary = AgentRunReplay(store).dry_run("tool-trace-1")

    assert result == {"echo": {"query": "hello"}}
    assert summary.tool_calls == 1
    tool_call = next(record for record in records if record.kind == "tool_call")
    assert tool_call.payload["tool_name"] == "echo"
    assert tool_call.payload["status"] == "completed"
    assert tool_call.payload["args_preview"] == {"query": "hello"}
    assert '"echo"' in tool_call.payload["result_preview"]


async def test_model_change_subscriber_retries_after_disconnect(monkeypatch):
    import backend.core.models.model_cache as model_cache

    calls = 0

    async def listen_once():
        nonlocal calls
        calls += 1
        if calls == 1:
            raise ConnectionError("redis unavailable")
        raise asyncio.CancelledError

    async def no_delay(_seconds):
        return None

    monkeypatch.setattr(model_cache, "_subscribe_model_changes_once", listen_once)
    monkeypatch.setattr(model_cache.asyncio, "sleep", no_delay)
    with pytest.raises(asyncio.CancelledError):
        await model_cache.subscribe_model_changes()
    assert calls == 2


def test_llm_performance_callback_accepts_missing_metadata():
    from backend.core.models.factory import LLMPerformanceCallback

    callback = LLMPerformanceCallback()
    callback.on_chat_model_start({}, [[]], run_id="run-1")
    assert "run-1" in callback._run_start_times


def test_llm_performance_callback_logs_cancelled_as_info(monkeypatch):
    import backend.core.models.factory as model_factory

    class FakeLogger:
        def __init__(self):
            self.info_calls = []
            self.error_calls = []

        def info(self, *args, **kwargs):
            self.info_calls.append((args, kwargs))

        def error(self, *args, **kwargs):
            self.error_calls.append((args, kwargs))

    fake_logger = FakeLogger()
    monkeypatch.setattr(model_factory, "logger", fake_logger)

    callback = model_factory.LLMPerformanceCallback()
    callback.on_chat_model_start({}, [[]], run_id="run-1")
    callback.on_llm_error(asyncio.CancelledError(), run_id="run-1")

    assert fake_logger.error_calls == []
    assert len(fake_logger.info_calls) == 2
    assert "LLM调用已取消" in fake_logger.info_calls[-1][0][0]
    assert "run-1" not in callback._run_start_times


def test_agent_features_are_derived_from_profile_capabilities():
    from backend.application.agents.info_service import AgentInfoService

    service = AgentInfoService()
    groundwater = AgentProfile(
        code="groundwater",
        name="Groundwater",
        domain="groundwater-project-analysis",
        default_workflow="full",
        allowed_workflows=["full", "plan_solve"],
        tool_policy="groundwater_default",
    )
    features = service._features(
        profile=groundwater,
        kb_ids=[],
        available_models=[{"supports_vision": False}],
        use_search=service._supports_tool(groundwater, "web_search"),
        supports_delegation=True,
    )

    assert "web_search" in features
    assert "task_delegation" in features
    assert "deep_research_report" not in features
    assert "file_presentation" not in features
    assert "vision" not in features
