from __future__ import annotations

import asyncio

import pytest

from backend.api.routers.agent_runtime import _uses_deep_research_background
from backend.application.agent_executors.writer_stage import (
    EvidenceWriterStage,
    WriterStageConfig,
)
from backend.application.agent_tasks import AgentTaskConflictError, AgentTaskRegistry
from backend.application.agent_trace import AgentTraceStore
from backend.application.agent_workflows.coordinated_agent_workflow import (
    CoordinatedAgentWorkflow,
)
from backend.application.event_stream.sse_encoder import SSEEncoder
from backend.domain.agents.events import AgentEvent
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


async def test_background_agent_task_survives_stream_disconnect_and_reconnects():
    registry = AgentTaskRegistry()
    release = asyncio.Event()

    async def events():
        yield AgentEvent("message.delta", "r", "thread-1", 1, {"content": "first"})
        await release.wait()
        yield AgentEvent("done", "r", "thread-1", 2, {})

    record = await registry.start(
        user_id="user-1",
        agent_code="ne",
        thread_id="thread-1",
        question="q",
        stream_factory=events,
    )
    subscription = registry.subscribe(record, replay=True)
    assert (await anext(subscription)).payload["event"]["kind"] == "background_task_started"
    first = await anext(subscription)
    assert first.payload["content"] == "first"
    assert first.payload["_task_event_id"] == 1
    await subscription.aclose()
    assert record.runner is not None and not record.runner.done()

    release.set()
    await record.runner
    assert record.status == "completed"
    reconnect = registry.subscribe(record, replay=False)
    assert (await anext(reconnect)).type == "done"

    resumed = registry.subscribe(record, replay=True, after_event_id=1)
    resumed_event = await anext(resumed)
    assert resumed_event.type == "done"
    assert resumed_event.payload["_task_event_id"] == 2


async def test_background_task_startup_ack_does_not_wait_for_task_completion():
    registry = AgentTaskRegistry()
    release = asyncio.Event()

    async def events():
        await release.wait()
        yield AgentEvent("done", "r", "thread-1", 1, {})

    record = await registry.start(
        user_id="user-1",
        agent_code="ne",
        thread_id="thread-1",
        question="q",
        stream_factory=events,
    )

    ack = [event async for event in registry.startup_ack(record)]

    assert [event.type for event in ack] == ["workflow.step", "done"]
    assert ack[0].payload["event"]["metadata"]["task_id"] == record.task_id
    assert record.runner is not None and not record.runner.done()
    release.set()
    await record.runner


async def test_background_task_requires_terminal_commit_event():
    registry = AgentTaskRegistry()

    async def events():
        yield AgentEvent(
            "message.delta",
            "r",
            "thread-1",
            1,
            {"content": "uncommitted result"},
        )

    record = await registry.start(
        user_id="user-1",
        agent_code="ne",
        thread_id="thread-1",
        question="q",
        stream_factory=events,
    )
    await record.runner

    assert record.status == "failed"
    assert record.error_message == "智能体事件流未提交终态，任务结果不可确认"
    assert [event.type for event in record.events[-2:]] == ["error", "done"]
    assert record.events[-2].payload["error_code"] == "agent_terminal_event_missing"


async def test_background_task_publishes_done_after_upstream_cleanup():
    registry = AgentTaskRegistry()
    cleanup_started = asyncio.Event()
    release_cleanup = asyncio.Event()

    async def events():
        yield AgentEvent("done", "r", "thread-1", 1, {})
        cleanup_started.set()
        await release_cleanup.wait()

    record = await registry.start(
        user_id="user-1",
        agent_code="ne",
        thread_id="thread-1",
        question="q",
        stream_factory=events,
    )
    await cleanup_started.wait()

    assert record.status == "running"
    assert all(event.type != "done" for event in record.events)

    release_cleanup.set()
    await record.runner
    assert record.status == "completed"
    assert record.events[-1].type == "done"


async def test_coordinated_trace_finishes_after_done_barrier():
    profile = AgentProfile(code="ne", name="NE", domain="nuclear-electronics")
    context = _context(profile)
    request = AgentRequest(
        question="q",
        agent_code="ne",
        user_id="user-1",
    )
    terminal_statuses: list[str] = []

    class Trace:
        def record_run_finished(self, *, status, **kwargs):
            del kwargs
            terminal_statuses.append(status)

    class BarrierWorkflow(CoordinatedAgentWorkflow):
        async def _run_impl(self, request, context):
            del request
            self._active_traces[context.request_id] = Trace()
            yield AgentEvent(
                "coordination.completed",
                context.request_id,
                context.external_thread_id,
                1,
                {},
            )
            yield AgentEvent(
                "done",
                context.request_id,
                context.external_thread_id,
                2,
                {},
            )

    stream = BarrierWorkflow(name="deep_research").run(request, context)
    assert (await anext(stream)).type == "coordination.completed"
    assert terminal_statuses == []
    assert (await anext(stream)).type == "done"
    assert terminal_statuses == []
    with pytest.raises(StopAsyncIteration):
        await anext(stream)
    assert terminal_statuses == ["completed"]


async def test_sse_encoder_writes_trace_when_enabled(tmp_path):
    async def events():
        yield AgentEvent("message.delta", "sse-run-1", "thread-1", 1, {"content": "hello"})
        yield AgentEvent("done", "sse-run-1", "thread-1", 2, {})

    chunks = [
        chunk
        async for chunk in SSEEncoder(
            trace_enabled=True,
            trace_root=str(tmp_path),
        ).encode_stream(events())
    ]
    records = AgentTraceStore(tmp_path).read_records("sse-run-1")

    assert any('"type": "message.delta"' in chunk for chunk in chunks)
    assert chunks[-1] == "data: [DONE]\n\n"
    assert [
        record.payload["event_type"]
        for record in records
        if record.kind == "event"
    ] == ["sse.message.delta", "sse.done"]


async def test_writer_stage_writes_input_output_trace(tmp_path, monkeypatch):
    from langchain_core.messages import AIMessageChunk

    import backend.core.models as core_models

    class FakeGraph:
        async def aupdate_state(self, config, values, **kwargs):
            return None

    class FakeModel:
        async def astream(self, messages):
            yield AIMessageChunk(content="最终答案")

    class FakeHandler:
        project_evidence_text = "项目证据"
        retrieved_evidence_text = "标准证据"
        answer_fragments: list[str]

        def __init__(self):
            self.answer_fragments = []

        async def handle(self, event):
            if event.get("event") == "on_chat_model_stream":
                chunk = event["data"]["chunk"]
                content = str(getattr(chunk, "content", "") or "")
                self.answer_fragments.append(content)
                yield {"type": "chunk", "content": content}

    monkeypatch.setattr(core_models, "create_chat_model", lambda **kwargs: FakeModel())
    writer_stage = EvidenceWriterStage(
        WriterStageConfig(
            label="writer-test",
            graph=FakeGraph(),
            model_name="fake-model",
            request_metadata={
                "_trace_enabled": True,
                "_trace_run_id": "writer-run-1",
                "_request_id": "writer-run-1",
                "_workflow_name": "full",
                "agent_trace_root": str(tmp_path),
            },
        )
    )

    events = [
        event
        async for event in writer_stage.stream(
            question="问题",
            effective_config={"configurable": {"thread_id": "thread-1"}},
            handler=FakeHandler(),
        )
    ]
    records = AgentTraceStore(tmp_path).read_records("writer-run-1")

    assert any(event["type"] == "chunk" and event["content"] == "最终答案" for event in events)
    assert [
        record.payload["event_type"]
        for record in records
        if record.kind == "event"
    ] == ["writer.input", "writer.output"]
    output_payload = records[-1].payload["payload"]
    assert output_payload["status"] == "completed"
    assert output_payload["visible_chars"] == len("最终答案")


async def test_background_agent_task_rejects_second_question_on_active_thread():
    registry = AgentTaskRegistry()
    release = asyncio.Event()

    async def events():
        await release.wait()
        yield AgentEvent("done", "r", "thread-1", 1, {})

    first = await registry.start(
        user_id="user-1",
        agent_code="ne",
        thread_id="thread-1",
        question="first",
        stream_factory=events,
    )
    with pytest.raises(AgentTaskConflictError) as exc_info:
        await registry.start(
            user_id="user-1",
            agent_code="ne",
            thread_id="thread-1",
            question="second",
            stream_factory=events,
        )
    assert exc_info.value.record is first
    release.set()
    await first.runner


async def test_background_agent_task_status_and_events_survive_registry_restart(tmp_path):
    db_path = tmp_path / "agent-tasks.db"
    registry = AgentTaskRegistry(db_path)

    async def events():
        yield AgentEvent("message.delta", "r", "thread-1", 1, {"content": "saved"})
        yield AgentEvent("done", "r", "thread-1", 2, {})

    record = await registry.start(
        user_id="user-1",
        agent_code="ne",
        thread_id="thread-1",
        question="q",
        stream_factory=events,
    )
    await record.runner

    restored_registry = AgentTaskRegistry(db_path)
    restored = await restored_registry.get(
        record.task_id, user_id="user-1", agent_code="ne"
    )
    assert restored is not None
    assert restored.status == "completed"
    replay = [event async for event in restored_registry.subscribe(restored, replay=True)]
    assert [event.type for event in replay] == [
        "workflow.step",
        "message.delta",
        "done",
    ]


def test_deep_research_skill_uses_reconnectable_background_task():
    profile = AgentProfile(code="ne", name="NE", domain="nuclear-electronics")
    request = AgentRequest(
        question="调研",
        agent_code="ne",
        user_id="user-1",
        selected_skills=["deep-research"],
        skills_requested_for_workflow=True,
    )
    assert _uses_deep_research_background(request, profile)


def test_plan_solve_uses_reconnectable_background_task():
    profile = AgentProfile(
        code="groundwater",
        name="Groundwater",
        domain="groundwater-project-analysis",
    )
    request = AgentRequest(
        question="制定监测点位布设方案",
        agent_code="groundwater",
        user_id="user-1",
        requested_workflow="plan_solve",
    )
    assert _uses_deep_research_background(request, profile)
