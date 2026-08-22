from __future__ import annotations

import pytest
from types import SimpleNamespace

from langchain_core.messages import AIMessage, HumanMessage, ToolMessage

from backend.application.agent_executors.lightweight_qa_agent import (
    LightweightQAAgent,
    _kb_result_warning,
)
from backend.core.agents.context import HistoryCompactor
from backend.core.agents.context.artifact_store import ArtifactStore


@pytest.mark.asyncio
async def test_lightweight_answer_uses_shared_jit_context_pack(monkeypatch, tmp_path):
    import backend.application.agent_executors.lightweight_qa_agent as module

    monkeypatch.setattr(
        module,
        "create_context_artifact_store",
        lambda: ArtifactStore(tmp_path),
    )
    agent = LightweightQAAgent(
        graph=None,
        model_name=None,
        kb_ids=["kb-1"],
        use_search=False,
        user_id="user-1",
        thread_id="thread-1",
        prompt_profile="ne",
        domain="nuclear-electronics",
    )

    messages, metadata = await agent._assemble_lightweight_context(
        clean_q="标准 A 的限值是多少？",
        effective_config={"configurable": {"thread_id": "thread-1"}},
        history_messages=[],
        persistent_memory_text="",
        kb_context="### 片段 1（来自：doc-a）\n标准 A 的限值为 17 mg/L。",
        web_context="",
        file_index_context="",
        table_analysis_context="",
        permanent_file_prefix="",
        image_contents=None,
    )

    system_text = str(messages[0].content)
    assert "<dynamic_context_pack>" in system_text
    assert "17 mg/L" in system_text
    assert metadata["pack"]["budget"]["items_injected"] >= 1
    assert metadata["final_tokens"] <= agent._context_ledger.token_budget
    assert isinstance(messages[-1], HumanMessage)


def test_lightweight_large_results_are_externalized_before_jit_rendering(
    monkeypatch,
    tmp_path,
):
    import backend.application.agent_executors.lightweight_qa_agent as module

    # 断言 legacy 大结果外置路径;CONTEXT_OBJECT_MODE env 默认已是 on,显式钉 off。
    monkeypatch.setenv("CONTEXT_OBJECT_MODE", "off")

    monkeypatch.setattr(
        module,
        "create_context_artifact_store",
        lambda: ArtifactStore(tmp_path),
    )
    agent = LightweightQAAgent(
        graph=None,
        model_name=None,
        kb_ids=["kb-1"],
        use_search=False,
        user_id="user-1",
        thread_id="thread-1",
        prompt_profile="ne",
        domain="nuclear-electronics",
    )
    records = agent._build_lightweight_context_records(
        clean_q="限值",
        effective_config={"configurable": {"thread_id": "thread-1"}},
        history_messages=[],
        persistent_memory_text="",
        kb_context=(
            "### 片段 1（来自：doc-a）\n标准 A 的限值为 17 mg/L。\n"
            + ("补充数据。" * 2000)
        ),
        web_context="",
        file_index_context="",
        table_analysis_context="",
        permanent_file_prefix="",
    )

    assert records
    assert records[0].get("raw_ref", "").startswith("artifact://context/")
    pack = agent._context_ledger.build_pack(
        records,
        messages=[HumanMessage(content="限值")],
        query="限值",
        system_content="system",
        request_overhead={"tools": []},
    )
    assert pack["selected_ids"]
    assert "raw_ref=" in pack["rendered"]


@pytest.mark.asyncio
async def test_lightweight_compaction_is_written_to_graph_state():
    class Graph:
        def __init__(self):
            self.update = None

        async def aupdate_state(self, _config, update, as_node=None):
            self.update = (update, as_node)

    agent = LightweightQAAgent(
        graph=None,
        model_name=None,
        kb_ids=[],
        use_search=False,
        user_id="user-1",
        thread_id="thread-1",
        prompt_profile="ne",
        domain="nuclear-electronics",
    )
    agent._graph = Graph()
    agent._history_compactor = HistoryCompactor(
        model_name=None,
        trigger_tokens=1024,
        retained_window_tokens=1024,
        summary_input_chars=8000,
        summary_output_chars=800,
        timeout_seconds=10,
    )
    history = []
    for index in range(12):
        history.extend(
            [
                HumanMessage(
                    content=f"用户确认事实 {index}: " + ("重要数据。" * 150),
                    id=f"u{index}",
                ),
                AIMessage(
                    content=f"助手回答 {index}: " + ("保留结论。" * 150),
                    id=f"a{index}",
                ),
            ]
        )
    result = agent._history_compactor.compact_deterministic(
        history,
        trigger="request_budget",
        threshold_tokens=1024,
    )
    assert result is not None

    await agent._persist_lightweight_compaction(
        {"configurable": {"thread_id": "thread-1"}},
        result,
    )

    update, node_name = agent._graph.update
    assert node_name == "model"
    assert update["context_window_id"] == result.window_id
    assert update["messages"]
    assert update["context_compaction"]["status"] == "completed"


async def test_persist_lightweight_compaction_retries_transient_failure(monkeypatch):
    import backend.application.agent_executors.lightweight_compression as module

    monkeypatch.setattr(module, "_COMPRESSION_PERSIST_RETRY_DELAY_SECONDS", 0.0)

    class Graph:
        def __init__(self):
            self.calls = 0
            self.fail_first = True

        async def aupdate_state(self, _config, _update, as_node=None):
            self.calls += 1
            if self.fail_first:
                self.fail_first = False
                raise RuntimeError("transient persistence failure")

    agent = LightweightQAAgent(
        graph=None,
        model_name=None,
        kb_ids=[],
        use_search=False,
        user_id="user-1",
        thread_id="thread-1",
        prompt_profile="ne",
        domain="nuclear-electronics",
    )
    agent._graph = Graph()
    agent._history_compactor = HistoryCompactor(
        model_name=None,
        trigger_tokens=1024,
        retained_window_tokens=1024,
        summary_input_chars=8000,
        summary_output_chars=800,
        timeout_seconds=10,
    )
    history = []
    for index in range(12):
        history.extend(
            [
                HumanMessage(content=f"用户确认事实 {index}: " + ("重要数据。" * 150)),
                AIMessage(content=f"助手回答 {index}: " + ("保留结论。" * 150)),
            ]
        )
    result = agent._history_compactor.compact_deterministic(
        history,
        trigger="request_budget",
        threshold_tokens=1024,
    )
    assert result is not None

    persisted = await agent._persist_lightweight_compaction(
        {"configurable": {"thread_id": "thread-1"}},
        result,
    )

    assert persisted is True
    assert agent._graph.calls == 2


async def test_persist_lightweight_compaction_returns_false_after_final_failure(
    monkeypatch,
):
    import backend.application.agent_executors.lightweight_compression as module

    monkeypatch.setattr(module, "_COMPRESSION_PERSIST_RETRY_DELAY_SECONDS", 0.0)

    class Graph:
        def __init__(self):
            self.calls = 0

        async def aupdate_state(self, _config, _update, as_node=None):
            self.calls += 1
            raise RuntimeError("permanent persistence failure")

    agent = LightweightQAAgent(
        graph=None,
        model_name=None,
        kb_ids=[],
        use_search=False,
        user_id="user-1",
        thread_id="thread-1",
        prompt_profile="ne",
        domain="nuclear-electronics",
    )
    agent._graph = Graph()
    agent._history_compactor = HistoryCompactor(
        model_name=None,
        trigger_tokens=1024,
        retained_window_tokens=1024,
        summary_input_chars=8000,
        summary_output_chars=800,
        timeout_seconds=10,
    )
    history = []
    for index in range(12):
        history.extend(
            [
                HumanMessage(content=f"用户确认事实 {index}: " + ("重要数据。" * 150)),
                AIMessage(content=f"助手回答 {index}: " + ("保留结论。" * 150)),
            ]
        )
    result = agent._history_compactor.compact_deterministic(
        history,
        trigger="request_budget",
        threshold_tokens=1024,
    )
    assert result is not None

    persisted = await agent._persist_lightweight_compaction(
        {"configurable": {"thread_id": "thread-1"}},
        result,
    )

    assert persisted is False
    assert agent._graph.calls == module._COMPRESSION_PERSIST_MAX_ATTEMPTS


def test_kb_budget_message_is_warning_not_evidence():
    message = "本轮剩余检索上下文预算不足，检索结果未注入。"

    assert _kb_result_warning(message) == message
    assert (
        _kb_result_warning("### 片段 1（来自：探测器教材）\n高纯锗探测器内容") is None
    )


def test_history_compaction_token_after_is_metadata_upper_bound():
    compactor = HistoryCompactor(
        model_name=None,
        trigger_tokens=1024,
        retained_window_tokens=1024,
        summary_input_chars=8000,
        summary_output_chars=800,
        timeout_seconds=10,
    )
    history = []
    for index in range(10):
        history.extend(
            [
                HumanMessage(content=f"用户确认事实 {index}: " + ("重要数据。" * 150)),
                AIMessage(content=f"助手回答 {index}: " + ("保留结论。" * 150)),
            ]
        )

    result = compactor.compact_deterministic(
        history,
        trigger="request_budget",
        threshold_tokens=1024,
    )

    assert result is not None
    summary_message = result.messages[1]
    retained = [
        message
        for message in result.model_messages
        if message.id != summary_message.id
    ]
    actual_tokens = (
        compactor.count_messages([summary_message, *retained])
        + result.fixed_request_tokens
    )
    assert result.tokens_after >= actual_tokens
    assert result.tokens_after < result.tokens_before


def test_history_compactor_marks_rewritten_retained_messages():
    compactor = HistoryCompactor(
        model_name=None,
        trigger_tokens=1024,
        retained_window_tokens=256,
        summary_input_chars=8000,
        summary_output_chars=800,
        timeout_seconds=10,
    )
    group = [
        HumanMessage(content="用户长问题 " + "甲" * 1200, id="user-long"),
        AIMessage(content="助手长回答 " + "乙" * 1300, id="answer-long"),
        ToolMessage(
            content="工具长结果 " + "丙" * 900,
            id="tool-long",
            tool_call_id="call-1",
        ),
    ]

    compacted = compactor._compact_retained_turn(group)

    assert compacted[0].additional_kwargs["context_compaction_rewrite"][
        "reason"
    ] == "long_user_summary"
    assert compacted[1].additional_kwargs["context_compaction_rewrite"][
        "reason"
    ] == "long_assistant_summary"
    assert compacted[2].additional_kwargs["context_compaction_rewrite"][
        "reason"
    ] == "tool_result_omitted"
    assert [message.id for message in compacted] == [
        "user-long",
        "answer-long",
        "tool-long",
    ]


def test_compact_retained_turn_keeps_bounded_tool_excerpt():
    compactor = HistoryCompactor(
        model_name=None,
        trigger_tokens=1024,
        retained_window_tokens=256,
        summary_input_chars=8000,
        summary_output_chars=800,
        timeout_seconds=10,
    )
    tool_message = ToolMessage(
        content="结论：水质达标。" + "丙" * 2000 + " 依据GB14848判定",
        id="tool-long",
        tool_call_id="call-1",
    )

    compacted = compactor._compact_retained_turn(
        [HumanMessage(content="问题", id="user-short"), tool_message]
    )

    content = str(compacted[1].content)
    # Head keeps conclusions, tail keeps citations; the body stays bounded.
    assert "结论：水质达标。" in content
    assert "依据GB14848判定" in content
    assert len(content) < 700
    assert compacted[0].content == "问题"


async def test_lightweight_compaction_failure_queues_pending(monkeypatch):
    import backend.application.agent_executors.lightweight_compression as module

    monkeypatch.setattr(module, "_COMPRESSION_PERSIST_RETRY_DELAY_SECONDS", 0.0)

    class FailingGraph:
        async def aupdate_state(self, _config, _update, as_node=None):
            raise RuntimeError("persist failure")

    class MemoryStore:
        def __init__(self):
            self.memory = None

        async def load(self, **kwargs):
            return self.memory

        async def save(self, **kwargs):
            self.memory = kwargs["memory"]
            return True

    agent = LightweightQAAgent(
        graph=None,
        model_name=None,
        kb_ids=[],
        use_search=False,
        user_id="user-1",
        thread_id="thread-1",
        prompt_profile="ne",
        domain="nuclear-electronics",
    )
    agent._graph = FailingGraph()
    agent._memory_store = MemoryStore()
    agent._history_compactor = HistoryCompactor(
        model_name=None,
        trigger_tokens=1024,
        retained_window_tokens=1024,
        summary_input_chars=8000,
        summary_output_chars=800,
        timeout_seconds=10,
    )
    history = []
    for index in range(12):
        history.extend(
            [
                HumanMessage(
                    content=f"用户确认事实 {index}: " + ("重要数据。" * 150),
                    id=f"u{index}",
                ),
                AIMessage(
                    content=f"助手回答 {index}: " + ("保留结论。" * 150),
                    id=f"a{index}",
                ),
            ]
        )

    compacted, result = await agent._compact_lightweight_history(
        history_messages=history,
        effective_config={"configurable": {"thread_id": "thread-1"}},
        system_content="system",
        current_message=HumanMessage(content="当前问题", id="current"),
    )

    assert result is not None
    assert compacted == result.model_messages
    pending = agent._memory_store.memory["pending_context_compaction"]
    assert pending["result"]["window_id"] == result.window_id
    assert pending["attempts"] == 1
    module._pending_compactions.pop("thread-1", None)


async def test_pending_lightweight_compaction_retries_with_appended_turns():
    from backend.application.agent_executors.lightweight_qa_agent import (
        _pending_compactions,
    )

    class MemoryStore:
        def __init__(self):
            self.memory = None

        async def load(self, **kwargs):
            return self.memory

        async def save(self, **kwargs):
            self.memory = kwargs["memory"]
            return True

    class RetryGraph:
        def __init__(self, messages):
            self.messages = messages
            self.update = None

        async def aget_state(self, _config):
            return SimpleNamespace(values={"messages": self.messages})

        async def aupdate_state(self, _config, update, as_node=None):
            self.update = update

    agent = LightweightQAAgent(
        graph=None,
        model_name=None,
        kb_ids=[],
        use_search=False,
        user_id="user-1",
        thread_id="thread-1",
        prompt_profile="ne",
        domain="nuclear-electronics",
    )
    agent._memory_store = MemoryStore()
    agent._history_compactor = HistoryCompactor(
        model_name=None,
        trigger_tokens=1024,
        retained_window_tokens=1024,
        summary_input_chars=8000,
        summary_output_chars=800,
        timeout_seconds=10,
    )
    history = []
    for index in range(8):
        history.extend(
            [
                HumanMessage(
                    content=f"用户确认事实 {index}: " + ("重要数据。" * 150),
                    id=f"u{index}",
                ),
                AIMessage(
                    content=f"助手回答 {index}: " + ("保留结论。" * 150),
                    id=f"a{index}",
                ),
            ]
        )
    result = agent._history_compactor.compact_deterministic(
        history,
        trigger="request_budget",
        threshold_tokens=1024,
    )
    assert result is not None
    config = {"configurable": {"thread_id": "thread-1"}}
    assert await agent._queue_pending_compaction(config, result) is True

    current_messages = [
        *history,
        HumanMessage(content="压缩期间的新问题", id="new-user"),
        AIMessage(content="新回答", id="new-answer"),
    ]
    graph = RetryGraph(current_messages)
    agent._graph = graph
    _pending_compactions.pop("thread-1", None)

    retried = await agent._retry_pending_lightweight_compaction(config)

    assert retried is True
    assert "pending_context_compaction" not in agent._memory_store.memory
    assert graph.update is not None
    updated_ids = [
        getattr(message, "id", None) for message in graph.update["messages"]
    ]
    assert "new-user" in updated_ids
    assert "new-answer" in updated_ids
