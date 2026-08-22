"""轻量流事件泵回归:检索期心跳、断连取消检索任务、memory 单次读取。"""

from __future__ import annotations

import asyncio

import pytest
from langchain_core.messages import AIMessageChunk, HumanMessage, SystemMessage

from backend.application.agent_executors import (
    lightweight_stream as stream_module,
)
from backend.application.agent_executors.lightweight_qa_agent import (
    LightweightQAAgent,
)


class _CountingMemoryStore:
    def __init__(self):
        self.load_calls = 0

    async def load(self, **_kwargs):
        self.load_calls += 1
        return None

    async def save(self, **_kwargs):
        return True


class _FakeModel:
    async def astream(self, _messages):
        yield AIMessageChunk(content="最终答案")


def _lightweight_agent(
    monkeypatch,
    *,
    kb_delay_seconds: float,
    kb_cancelled: dict,
):
    agent = LightweightQAAgent(
        graph=None,
        model_name=None,
        kb_ids=["kb-1"],
        use_search=False,
        user_id=None,
        thread_id=None,
        prompt_profile="ne",
        domain="nuclear-electronics",
    )
    memory_store = _CountingMemoryStore()
    agent._memory_store = memory_store

    async def fake_recent_messages(_config):
        return []

    async def fake_history(_config, **_kwargs):
        return "无"

    async def fake_assemble(**_kwargs):
        return (
            [SystemMessage(content="s"), HumanMessage(content="q")],
            {"pack": {}, "system_chars": 1},
        )

    async def slow_kb_search(_query):
        try:
            await asyncio.sleep(kb_delay_seconds)
        except asyncio.CancelledError:
            kb_cancelled["cancelled"] = True
            raise
        return "### 片段 1（来自：doc-a）\n证据内容"

    async def fake_save(*_args, **_kwargs):
        return None

    monkeypatch.setattr(agent, "_load_recent_messages", fake_recent_messages)
    monkeypatch.setattr(agent, "_load_and_compress_history", fake_history)
    monkeypatch.setattr(agent, "_assemble_lightweight_context", fake_assemble)
    monkeypatch.setattr(agent, "_run_kb_search", slow_kb_search)
    monkeypatch.setattr(agent, "_save_turn", fake_save)
    monkeypatch.setattr(
        agent, "_trigger_async_compression", lambda _config: None
    )
    monkeypatch.setattr(
        LightweightQAAgent, "STREAM_HEARTBEAT_INTERVAL_SECONDS", 0.05
    )

    async def fake_create_model(**_kwargs):
        return _FakeModel()

    monkeypatch.setattr(
        stream_module, "create_chat_model_async", fake_create_model
    )
    return agent, memory_store


async def test_retrieval_phase_emits_heartbeats_and_reads_memory_once(
    monkeypatch,
):
    """检索 gather 期间必须持续发 ping(此前整个检索阶段零输出,KB/联网慢时
    SSE 会被网关断连);入口 memory 只读两次(retry-pending 内部一次 +
    快照一次),冗余预读已删除。"""

    kb_cancelled: dict = {}
    agent, memory_store = _lightweight_agent(
        monkeypatch,
        kb_delay_seconds=0.3,
        kb_cancelled=kb_cancelled,
    )

    events = [
        event
        async for event in agent.aquery_stream(
            question="问题",
            effective_config={"configurable": {"thread_id": "lw-hb-1"}},
        )
    ]

    kb_end_index = next(
        index
        for index, event in enumerate(events)
        if event.get("type") == "event"
        and event["event"].get("kind") == "tool_end"
    )
    ping_before_kb_end = sum(
        1 for event in events[:kb_end_index] if event.get("type") == "ping"
    )
    assert ping_before_kb_end >= 1
    assert any(
        event.get("type") == "chunk"
        and "最终答案" in str(event.get("content"))
        for event in events
    )
    assert memory_store.load_calls == 2


async def test_disconnect_during_retrieval_cancels_search_tasks(monkeypatch):
    """消费方在检索期心跳处断连时,整组检索任务必须被取消并有界回收,
    不留孤儿任务继续消耗检索预算。"""

    kb_cancelled: dict = {}
    agent, _memory_store = _lightweight_agent(
        monkeypatch,
        kb_delay_seconds=30.0,
        kb_cancelled=kb_cancelled,
    )

    stream = agent.aquery_stream(
        question="问题",
        effective_config={"configurable": {"thread_id": "lw-hb-2"}},
    )
    saw_ping = False
    async for event in stream:
        if event.get("type") == "ping":
            saw_ping = True
            break
    assert saw_ping is True
    await stream.aclose()
    await asyncio.sleep(0)

    assert kb_cancelled.get("cancelled") is True


async def test_reap_task_bounded_cancels_and_observes_running_task():
    from backend.core.utils.async_tasks import reap_task_bounded

    started = asyncio.Event()

    async def cooperative():
        started.set()
        await asyncio.Event().wait()

    task = asyncio.create_task(cooperative())
    await started.wait()

    assert await reap_task_bounded(task, timeout=1.0) is True
    assert task.cancelled() is True


async def test_reap_task_bounded_is_bounded_for_uncooperative_task():
    from backend.core.utils.async_tasks import reap_task_bounded

    cancellation_seen = asyncio.Event()
    release = asyncio.Event()

    async def swallow_first_cancel():
        try:
            await asyncio.Event().wait()
        except asyncio.CancelledError:
            cancellation_seen.set()
            await release.wait()
            raise

    task = asyncio.create_task(swallow_first_cancel())
    await asyncio.sleep(0)

    assert await reap_task_bounded(task, timeout=0.01) is False
    assert cancellation_seen.is_set() is True

    release.set()
    with pytest.raises(asyncio.CancelledError):
        await task


async def test_reap_task_bounded_leaves_finished_task_result_to_caller():
    from backend.core.utils.async_tasks import reap_task_bounded

    async def failing():
        raise RuntimeError("call-site owns this exception")

    task = asyncio.create_task(failing())
    await asyncio.sleep(0)
    assert task.done() is True

    assert await reap_task_bounded(task, timeout=0.1) is True
    # 已完成任务的结果留给调用方业务逻辑消费,不被清理工具吞掉。
    assert isinstance(task.exception(), RuntimeError)
