from __future__ import annotations

import asyncio
import time
from dataclasses import replace

import pytest

from backend.application.agent_executors.evidence_convergence import (
    EvidenceConvergencePolicy,
)
from backend.application.agent_executors.streaming_runtime import (
    AgentStreamRuntime,
    StreamRuntimeConfig,
    cancel_pending_stream_event,
    close_stream_event_iterator,
    drain_agent_control,
)
from backend.core.agents.control import AgentControl, AgentStatus, get_agent_control
from backend.core.agents.runtime_manager import (
    reset_agent_runtime_manager,
    set_agent_runtime_manager,
)
from backend.core.agents.runtime_store import SQLiteAgentRuntimeStore


def _unused_async_iterator(**_kwargs):
    async def iterator():
        for item in ():
            yield item

    return iterator()


async def _unused_history_resolver(_config):
    return None


def _runtime_config() -> StreamRuntimeConfig:
    return StreamRuntimeConfig(
        label="test",
        graph=object(),
        model_name="test-model",
        user_id="user-1",
        kb_ids=[],
        use_search=False,
        subagent_enabled=True,
        max_concurrent_subagents=2,
        selected_skills=None,
        task_type=None,
        lightweight_mode=True,
        lightweight_thread_id="thread-1",
        lightweight_external_thread_id="external-thread-1",
        thinking_enabled=False,
        prompt_domain="test",
        guardrail_policy="default",
        runtime_permissions={},
        guardrail_instructions="",
        time_context="",
        lightweight_allowed_tools=[],
        runtime_tools={},
        normal_max_output_chars=1000,
        multi_kb_search_max_calls=1,
        deep_research_task_type="deep_research",
        deep_research_stream_timeout_seconds=60,
        deep_research_idle_timeout_seconds=30,
        deep_research_finalize_idle_timeout_seconds=30,
        deep_research_task_stall_grace_seconds=5,
        deep_research_finalize_grace_seconds=5,
        emit_graph_artifacts=_unused_async_iterator,
        finalize_deep_research_report=_unused_async_iterator,
        resolve_history_image_tokens=_unused_history_resolver,
        request_metadata={"_request_id": "request-1"},
    )


async def test_stream_runtime_scopes_and_closes_one_agent_control(monkeypatch):
    captured = {}

    class FakeLightweightAgent:
        def __init__(self, **_kwargs):
            pass

        async def aquery_stream(self, **_kwargs):
            control = get_agent_control()
            assert control is not None
            captured["control"] = control
            assert control.root_agent_id == "request-1"
            assert control.limits.max_active_agents == 2
            await control.register("orphaned-child")
            yield {"type": "chunk", "content": "one"}
            assert get_agent_control() is control
            yield {"type": "chunk", "content": "two"}

    monkeypatch.setattr(
        "backend.application.agent_executors.lightweight_qa_agent.LightweightQAAgent",
        FakeLightweightAgent,
    )

    runtime = AgentStreamRuntime(_runtime_config())
    stream = runtime.aquery_stream(
        question="hello",
        thread_id="thread-1",
    ).__aiter__()
    first = await asyncio.create_task(stream.__anext__())
    second = await asyncio.create_task(stream.__anext__())
    with pytest.raises(StopAsyncIteration):
        await asyncio.create_task(stream.__anext__())

    assert [first, second] == [
        {"type": "chunk", "content": "one"},
        {"type": "chunk", "content": "two"},
    ]
    assert get_agent_control() is None
    orphan = await captured["control"].get("orphaned-child")
    assert orphan.status is AgentStatus.INTERRUPTED
    assert orphan.error == "root request stream closed"


async def test_nested_stream_reuses_and_does_not_close_parent_manager(monkeypatch):
    parent = AgentControl("parent-root")

    class FakeLightweightAgent:
        def __init__(self, **_kwargs):
            pass

        async def aquery_stream(self, **_kwargs):
            assert get_agent_control() is parent
            yield {"type": "chunk", "content": "nested"}

    monkeypatch.setattr(
        "backend.application.agent_executors.lightweight_qa_agent.LightweightQAAgent",
        FakeLightweightAgent,
    )
    token = set_agent_runtime_manager(parent)
    try:
        events = [
            event
            async for event in AgentStreamRuntime(_runtime_config()).aquery_stream(
                question="hello",
                thread_id="thread-1",
            )
        ]
        assert get_agent_control() is parent
    finally:
        reset_agent_runtime_manager(token)

    assert events == [{"type": "chunk", "content": "nested"}]
    assert parent.closed is False
    assert parent.cancellation.cancelled is False


async def test_full_runtime_preserves_coordinator_recursion_limit():
    captured = {}

    class FakeGraph:
        async def astream_events(self, graph_input, *, config, **_kwargs):
            from backend.core.tools.request_limits import (
                consume_tool_budget,
                get_web_search_max_results,
            )

            captured["config"] = config
            captured["web_budget"] = [
                consume_tool_budget("web_search") for _ in range(4)
            ]
            captured["web_max_results"] = get_web_search_max_results()
            for event in ():
                yield event

    config = replace(
        _runtime_config(),
        graph=FakeGraph(),
        lightweight_mode=False,
        subagent_enabled=False,
        use_search=True,
        request_metadata={
            "_request_id": "request-1",
            "coordination_workflow": "deep_research",
            "coordination_web_search_max_calls": 3,
            "coordination_web_search_max_results": 3,
        },
    )

    _ = [
        event
        async for event in AgentStreamRuntime(config).aquery_stream(
            question="bounded coordinated branch",
            thread_id="thread-1",
            config={"recursion_limit": 20},
        )
    ]

    assert captured["config"]["recursion_limit"] == 20
    assert captured["web_budget"] == [
        (True, 2),
        (True, 1),
        (True, 0),
        (False, 0),
    ]
    assert captured["web_max_results"] == 3


async def test_stream_runtime_persists_owned_tree_outcome(tmp_path, monkeypatch):
    path = tmp_path / "stream-runtime.sqlite3"
    monkeypatch.setenv("AGENT_RUNTIME_STORE_BACKEND", "sqlite")
    monkeypatch.setenv("AGENT_RUNTIME_STORE_PATH", str(path))

    class FakeLightweightAgent:
        def __init__(self, **_kwargs):
            pass

        async def aquery_stream(self, **_kwargs):
            control = get_agent_control()
            assert control is not None
            async with control.spawn("persisted-child"):
                yield {"type": "chunk", "content": "done"}

    monkeypatch.setattr(
        "backend.application.agent_executors.lightweight_qa_agent.LightweightQAAgent",
        FakeLightweightAgent,
    )

    events = [
        event
        async for event in AgentStreamRuntime(_runtime_config()).aquery_stream(
            question="hello",
            thread_id="thread-1",
        )
    ]

    store = SQLiteAgentRuntimeStore(path)
    run = await store.load_run("request-1")
    agents = await store.load_tree_agents("request-1")
    assert events == [{"type": "chunk", "content": "done"}]
    assert run is not None
    assert run.status == "completed"
    assert [(agent.agent_id, agent.status) for agent in agents] == [
        ("persisted-child", "completed")
    ]


async def test_evidence_convergence_hands_off_to_writer_stage(monkeypatch):
    class FakeGraph:
        async def astream_events(self, *_args, **_kwargs):
            yield {
                "event": "on_tool_end",
                "name": "query_project_tables",
                "data": {
                    "output": '{"rows": [{"point_code": "GW18"}]}'
                },
            }

    writer_calls = []

    class FakeWriterStage:
        def __init__(self, config):
            self.config = config

        async def stream(self, *, question, effective_config, handler):
            writer_calls.append(
                {
                    "question": question,
                    "config": effective_config,
                    "evidence": handler.project_evidence_text,
                }
            )
            handler.answer_fragments.append("writer answer")
            yield {"type": "chunk", "content": "writer answer"}

    monkeypatch.setattr(
        "backend.application.agent_executors.streaming_runtime_impl.EvidenceWriterStage",
        FakeWriterStage,
    )
    config = replace(
        _runtime_config(),
        graph=FakeGraph(),
        lightweight_mode=False,
        prompt_domain="general",
        evidence_convergence_policy=EvidenceConvergencePolicy(
            enabled=True,
            max_queries=1,
        ),
    )

    events = [
        event
        async for event in AgentStreamRuntime(config).aquery_stream(
            question="如何评价 GW18？",
            thread_id="thread-1",
        )
    ]

    assert any(
        event == {
            "type": "status",
            "content": "已完成多轮项目数据查询，正在直接生成阶段性分析结论...",
        }
        for event in events
    )
    assert {"type": "chunk", "content": "writer answer"} in events
    assert len(writer_calls) == 1
    assert writer_calls[0]["question"] == "如何评价 GW18？"
    assert "GW18" in writer_calls[0]["evidence"]


async def test_completed_tool_only_graph_recovers_with_writer_stage(monkeypatch):
    class FakeGraph:
        async def astream_events(self, *_args, **_kwargs):
            yield {
                "event": "on_tool_end",
                "name": "multi_kb_search",
                "data": {"output": "检索证据：教材《污染水文地质学》包含相关章节。"},
            }

    writer_calls = []

    class FakeWriterStage:
        def __init__(self, config):
            self.config = config

        async def stream(self, *, question, effective_config, handler):
            writer_calls.append(
                {
                    "question": question,
                    "evidence": handler.retrieved_evidence_text,
                }
            )
            handler.answer_fragments.append("根据检索证据生成的最终回答")
            handler.chunk_count += 1
            yield {"type": "chunk", "content": "根据检索证据生成的最终回答"}

    monkeypatch.setattr(
        "backend.application.agent_executors.streaming_runtime_impl.EvidenceWriterStage",
        FakeWriterStage,
    )
    config = replace(
        _runtime_config(),
        graph=FakeGraph(),
        lightweight_mode=False,
        subagent_enabled=False,
    )

    events = [
        event
        async for event in AgentStreamRuntime(config).aquery_stream(
            question="请查找相关教材目录",
            thread_id="thread-1",
        )
    ]

    assert {"type": "chunk", "content": "根据检索证据生成的最终回答"} in events
    assert len(writer_calls) == 1
    assert writer_calls[0]["question"] == "请查找相关教材目录"
    assert "污染水文地质学" in writer_calls[0]["evidence"]
    assert not any(
        event.get("type") == "chunk"
        and "没有生成可展示的最终答案" in str(event.get("content") or "")
        for event in events
    )


async def test_stream_runtime_propagates_producer_cancellation_without_hanging():
    class CancelledRuntime(AgentStreamRuntime):
        async def _aquery_stream_impl(self, **_kwargs):
            for event in ():
                yield event
            raise asyncio.CancelledError

    stream = CancelledRuntime(_runtime_config()).aquery_stream(
        question="hello",
        thread_id="thread-1",
    )

    with pytest.raises(asyncio.CancelledError):
        await asyncio.wait_for(stream.__anext__(), timeout=0.5)


async def test_stream_runtime_delivers_buffered_tail_before_producer_cancellation():
    class TailThenCancelledRuntime(AgentStreamRuntime):
        async def _aquery_stream_impl(self, **_kwargs):
            yield {"type": "chunk", "content": "tail"}
            raise asyncio.CancelledError

    stream = TailThenCancelledRuntime(_runtime_config()).aquery_stream(
        question="hello",
        thread_id="thread-1",
    )

    assert await stream.__anext__() == {"type": "chunk", "content": "tail"}
    with pytest.raises(asyncio.CancelledError):
        await asyncio.wait_for(stream.__anext__(), timeout=0.5)


async def test_stream_runtime_double_cancel_still_signals_producer(monkeypatch):
    producer_started = asyncio.Event()
    producer_cancelled = asyncio.Event()
    cleanup_entered = asyncio.Event()
    hold_cleanup = asyncio.Event()

    class BlockingRuntime(AgentStreamRuntime):
        async def _aquery_stream_impl(self, **_kwargs):
            for event in ():
                yield event
            producer_started.set()
            try:
                await asyncio.Event().wait()
            except asyncio.CancelledError:
                producer_cancelled.set()
                raise
    cleanup_calls = 0

    async def block_first_cleanup(_task):
        nonlocal cleanup_calls
        cleanup_calls += 1
        if cleanup_calls == 1:
            cleanup_entered.set()
            await hold_cleanup.wait()
        return True

    monkeypatch.setattr(
        "backend.application.agent_executors.streaming_runtime.cancel_pending_stream_event",
        block_first_cleanup,
    )
    stream = BlockingRuntime(_runtime_config()).aquery_stream(
        question="hello",
        thread_id="thread-1",
    )
    relay_task = asyncio.create_task(stream.__anext__())
    await producer_started.wait()

    relay_task.cancel()
    await cleanup_entered.wait()
    relay_task.cancel()

    with pytest.raises(asyncio.CancelledError):
        await relay_task
    await asyncio.wait_for(producer_cancelled.wait(), timeout=0.5)


async def test_pending_stream_cancellation_is_bounded(monkeypatch):
    cancellation_seen = asyncio.Event()
    release = asyncio.Event()

    async def ignore_first_cancel():
        try:
            await asyncio.Event().wait()
        except asyncio.CancelledError:
            cancellation_seen.set()
            await release.wait()

    task = asyncio.create_task(ignore_first_cancel())
    await asyncio.sleep(0)
    monkeypatch.setattr(
        "backend.application.agent_executors.streaming_runtime_lifecycle._STREAM_CLEANUP_TIMEOUT_SECONDS",
        0.01,
    )

    assert await cancel_pending_stream_event(task) is False
    assert cancellation_seen.is_set() is True

    release.set()
    await asyncio.wait_for(task, timeout=0.5)


async def test_iterator_close_waits_for_inflight_read_to_finish(monkeypatch):
    read_started = asyncio.Event()
    cancellation_seen = asyncio.Event()
    release_read = asyncio.Event()
    iterator_closed = asyncio.Event()

    class SlowCancelIterator:
        running = False

        def __aiter__(self):
            return self

        async def __anext__(self):
            self.running = True
            read_started.set()
            try:
                await asyncio.Event().wait()
            except asyncio.CancelledError:
                cancellation_seen.set()
                await release_read.wait()
                raise
            finally:
                self.running = False

        async def aclose(self):
            if self.running:
                raise RuntimeError("aclose(): asynchronous generator is already running")
            iterator_closed.set()

    iterator = SlowCancelIterator()
    read_task = asyncio.create_task(iterator.__anext__())
    await read_started.wait()
    monkeypatch.setattr(
        "backend.application.agent_executors.streaming_runtime_lifecycle._STREAM_CLEANUP_TIMEOUT_SECONDS",
        0.01,
    )

    assert await cancel_pending_stream_event(read_task) is False
    assert cancellation_seen.is_set() is True
    assert await close_stream_event_iterator(
        iterator,
        active_read_task=read_task,
    ) is False
    assert iterator_closed.is_set() is False

    release_read.set()
    with pytest.raises(asyncio.CancelledError):
        await read_task
    await asyncio.wait_for(iterator_closed.wait(), timeout=0.5)


async def test_iterator_close_task_is_observed_when_cleanup_caller_is_cancelled():
    close_started = asyncio.Event()
    release_close = asyncio.Event()
    loop_errors = []
    loop = asyncio.get_running_loop()
    previous_handler = loop.get_exception_handler()

    class FailingCloseIterator:
        async def aclose(self):
            close_started.set()
            await release_close.wait()
            raise RuntimeError("aclose(): asynchronous generator is already running")

    loop.set_exception_handler(lambda _loop, context: loop_errors.append(context))
    try:
        cleanup = asyncio.create_task(
            close_stream_event_iterator(FailingCloseIterator())
        )
        await close_started.wait()
        cleanup.cancel()
        with pytest.raises(asyncio.CancelledError):
            await cleanup

        release_close.set()
        await asyncio.sleep(0.01)
        assert loop_errors == []
    finally:
        loop.set_exception_handler(previous_handler)


def test_build_stream_config_recursion_env_names_and_floor(monkeypatch):
    """通用变量名 AGENT_GRAPH_RECURSION_LIMIT 优先,旧 GROUNDWATER_ 名回退,
    低于 50 的配置截断到安全下限(带日志)而不是静默改写。"""

    from backend.application.agent_executors.stream_timeouts import (
        StreamTimeoutPolicy,
    )
    from backend.application.agent_executors.streaming_runtime_impl import (
        _build_stream_config,
    )

    cfg = _runtime_config()
    policy = StreamTimeoutPolicy()

    monkeypatch.setenv("AGENT_GRAPH_RECURSION_LIMIT", "120")
    monkeypatch.delenv("GROUNDWATER_GRAPH_RECURSION_LIMIT", raising=False)
    effective, _tid = _build_stream_config(
        cfg, policy, thread_id="t", config=None
    )
    assert effective["recursion_limit"] == 120

    monkeypatch.delenv("AGENT_GRAPH_RECURSION_LIMIT", raising=False)
    monkeypatch.setenv("GROUNDWATER_GRAPH_RECURSION_LIMIT", "130")
    effective, _tid = _build_stream_config(
        cfg, policy, thread_id="t", config=None
    )
    assert effective["recursion_limit"] == 130

    monkeypatch.setenv("AGENT_GRAPH_RECURSION_LIMIT", "10")
    effective, _tid = _build_stream_config(
        cfg, policy, thread_id="t", config=None
    )
    assert effective["recursion_limit"] == 50


def _stall_grace_session():
    from backend.application.agent_executors.streaming_runtime_impl import (
        _StreamSession,
    )

    return _StreamSession(
        question="q",
        effective_config={},
        effective_thread_id="thread-1",
        policy=None,
        stream_started_at=time.monotonic(),
        deep_research_mode=True,
        deep_research_timeout_budget=60.0,
    )


class _StallGraceHandler:
    def __init__(self, *, active: bool, stall_seconds: float = 0.0):
        self.deep_research_active_task_ids = {"task-1"} if active else set()
        self.deep_research_last_task_runtime_at = time.monotonic() - stall_seconds


def test_deep_research_stall_grace_extends_budget_exactly_once():
    """总预算耗尽但子任务仍活跃时,宽限必须真正延长预算且只延长一次。

    此前该宽限只在超时分支 ``continue``,下一轮迭代立刻被顶部预算检查打断,
    ``deep_research_task_stall_grace_seconds`` 配置从未生效。
    """

    from backend.application.agent_executors.streaming_runtime_impl import (
        _grant_deep_research_stall_grace,
    )

    session = _stall_grace_session()
    handler = _StallGraceHandler(active=True)
    cfg = _runtime_config()  # deep_research_task_stall_grace_seconds=5

    assert _grant_deep_research_stall_grace(session, handler, cfg) is True
    assert session.deep_research_timeout_budget == 65.0
    assert session.deep_research_stall_budget_applied is True

    assert _grant_deep_research_stall_grace(session, handler, cfg) is False
    assert session.deep_research_timeout_budget == 65.0


def test_deep_research_stall_grace_requires_recently_active_tasks():
    from backend.application.agent_executors.streaming_runtime_impl import (
        _grant_deep_research_stall_grace,
    )

    cfg = _runtime_config()

    no_tasks = _stall_grace_session()
    assert (
        _grant_deep_research_stall_grace(
            no_tasks, _StallGraceHandler(active=False), cfg
        )
        is False
    )
    assert no_tasks.deep_research_timeout_budget == 60.0

    stale_tasks = _stall_grace_session()
    assert (
        _grant_deep_research_stall_grace(
            stale_tasks,
            _StallGraceHandler(active=True, stall_seconds=10.0),
            cfg,
        )
        is False
    )
    assert stale_tasks.deep_research_timeout_budget == 60.0


async def test_agent_control_drain_timeout_is_bounded_on_python_310(monkeypatch):
    control = AgentControl("root")
    cancellation_seen = asyncio.Event()
    release = asyncio.Event()

    async def swallow_cancel_until_released():
        async with control.spawn("child"):
            try:
                await asyncio.Event().wait()
            except asyncio.CancelledError:
                cancellation_seen.set()
                await release.wait()

    owner = asyncio.create_task(swallow_cancel_until_released())
    while control.active_count == 0:
        await asyncio.sleep(0)
    await control.interrupt("root")
    await cancellation_seen.wait()
    monkeypatch.setattr(
        "backend.application.agent_executors.streaming_runtime_lifecycle._STREAM_CLEANUP_TIMEOUT_SECONDS",
        0.01,
    )

    assert await drain_agent_control(control, label="test") is False

    release.set()
    with pytest.raises(asyncio.CancelledError):
        await owner
