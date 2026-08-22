"""SSE 编码器与流式处理契约（拆自 test_agent_platform_standardization.py）。"""

import asyncio
import inspect
import json

from backend.application.agent_executors.stream_handler import NEAgentStreamHandler
from backend.application.event_stream.sse_encoder import SSEEncoder
from backend.domain.agents.events import AgentEvent
from contextvars import ContextVar
from langchain_core.messages import AIMessage, AIMessageChunk


async def test_stream_handler_reports_malformed_tool_call_finish():
    handler = NEAgentStreamHandler(thread_id="thread-1", selected_skills=set())

    start_events = [
        event
        async for event in handler.handle({"event": "on_chat_model_start", "data": {}})
    ]
    assert start_events == []

    message = AIMessage(content="\n\n\n", response_metadata={"finish_reason": "tool_calls"})
    end_events = [
        event
        async for event in handler.handle({"event": "on_chat_model_end", "data": {"output": message}})
    ]
    final_events = [event async for event in handler.finalize()]

    assert any(event.get("type") == "chunk" for event in end_events)
    malformed_chunks = [
        event.get("content", "")
        for event in final_events
        if event.get("type") == "chunk"
    ]
    assert any("工具调用结束信号" in chunk for chunk in malformed_chunks)


async def test_stream_handler_does_not_expose_unparsed_tool_call_as_answer():
    handler = NEAgentStreamHandler(thread_id="thread-1", selected_skills=set())

    _ = [
        event
        async for event in handler.handle({"event": "on_chat_model_start", "data": {}})
    ]
    message = AIMessage(
        content=(
            "<tool_call>multi_kb_search<arg_key>query</arg_key>"
            "<arg_value>重复查询</arg_value></tool_call>"
        ),
        response_metadata={"finish_reason": "tool_calls"},
    )
    end_events = [
        event
        async for event in handler.handle(
            {"event": "on_chat_model_end", "data": {"output": message}}
        )
    ]
    final_events = [event async for event in handler.finalize()]

    assert not any(event.get("type") == "chunk" for event in end_events)
    visible = "".join(
        str(event.get("content") or "")
        for event in final_events
        if event.get("type") == "chunk"
    )
    assert "<tool_call>" not in visible
    assert "工具调用结束信号" in visible


async def test_stream_handler_marks_empty_completed_round_as_failed():
    handler = NEAgentStreamHandler(thread_id="thread-1", selected_skills=set())

    final_events = [event async for event in handler.finalize()]

    chunks = [
        str(event.get("content") or "")
        for event in final_events
        if event.get("type") == "chunk"
    ]
    answer_events = [
        event["event"]
        for event in final_events
        if event.get("type") == "event"
        and event.get("event", {}).get("kind") == "answer"
    ]

    assert any("没有生成可展示的最终答案" in chunk for chunk in chunks)
    assert len(answer_events) == 1
    assert answer_events[0]["title"] == "回答生成失败"
    assert answer_events[0]["level"] == "error"
    assert answer_events[0]["metadata"]["finalization_failed"] is True


async def test_stream_handler_recovers_final_text_from_model_end():
    handler = NEAgentStreamHandler(thread_id="thread-1", selected_skills=set())

    _ = [
        event
        async for event in handler.handle({"event": "on_chat_model_start", "data": {}})
    ]
    message = AIMessage(content="最终分析结果：实验效果稳定。", response_metadata={"finish_reason": "stop"})
    events = [
        event
        async for event in handler.handle({"event": "on_chat_model_end", "data": {"output": message}})
    ]

    chunks = [event["content"] for event in events if event.get("type") == "chunk"]

    assert "".join(chunks) == "最终分析结果：实验效果稳定。"
    assert handler.chunk_count >= 1
    assert handler._normal_output_chars == len("最终分析结果：实验效果稳定。")


async def test_stream_handler_splits_think_markup_from_model_end_text():
    handler = NEAgentStreamHandler(thread_id="thread-1", selected_skills=set())

    _ = [
        event
        async for event in handler.handle({"event": "on_chat_model_start", "data": {}})
    ]
    message = AIMessage(
        content="<think>内部推理过程</think>最终答案",
        response_metadata={"finish_reason": "stop"},
    )
    events = [
        event
        async for event in handler.handle({"event": "on_chat_model_end", "data": {"output": message}})
    ]

    thinking = [event["content"] for event in events if event.get("type") == "thinking"]
    chunks = [event["content"] for event in events if event.get("type") == "chunk"]

    assert "".join(thinking) == "内部推理过程"
    assert "".join(chunks) == "最终答案"
    assert "<think>" not in "".join(handler.answer_fragments)


async def test_stream_handler_does_not_replay_streamed_answer_from_thinking_wrapped_model_end():
    handler = NEAgentStreamHandler(thread_id="thread-1", selected_skills=set())
    answer = "充水含水层是能够向矿坑或地下工程充水的含水层。"

    _ = [
        event
        async for event in handler.handle({"event": "on_chat_model_start", "data": {}})
    ]
    stream_events = [
        event
        async for event in handler.handle(
            {
                "event": "on_chat_model_stream",
                "data": {
                    "chunk": AIMessageChunk(
                        content=answer,
                        additional_kwargs={"reasoning_content": "先核对术语和出处。"},
                    )
                },
            }
        )
    ]
    end_events = [
        event
        async for event in handler.handle(
            {
                "event": "on_chat_model_end",
                "data": {
                    "output": AIMessage(
                        content=f"<think>先核对术语和出处。</think>{answer}",
                        response_metadata={"finish_reason": "stop"},
                    )
                },
            }
        )
    ]

    visible = [
        event["content"]
        for event in [*stream_events, *end_events]
        if event.get("type") == "chunk"
    ]
    thinking = [
        event["content"]
        for event in [*stream_events, *end_events]
        if event.get("type") == "thinking"
    ]

    assert "".join(visible) == answer
    assert thinking == ["先核对术语和出处。"]
    assert handler._model_round_summaries[-1]["missing_recovered_len"] == 0


async def test_stream_handler_recovers_only_current_round_tail_after_tool_round():
    handler = NEAgentStreamHandler(thread_id="thread-1", selected_skills=set())
    pre_tool_text = "我先读取知识库图片详情。"
    final_answer = (
        "已找到相关图片。\n\n"
        "![曲柄滑块机构示意图](https://example.edu/slider-crank.png)\n\n"
        "图注：这是一张机械原理示意图。\n\n"
        "来源：理论力学教材。"
    )
    streamed_prefix = final_answer[: final_answer.index("\n\n图注")]

    _ = [
        event
        async for event in handler.handle({"event": "on_chat_model_start", "data": {}})
    ]
    first_round_stream = [
        event
        async for event in handler.handle(
            {
                "event": "on_chat_model_stream",
                "data": {"chunk": AIMessageChunk(content=pre_tool_text)},
            }
        )
    ]
    first_round_end = [
        event
        async for event in handler.handle(
            {
                "event": "on_chat_model_end",
                "data": {
                    "output": AIMessage(
                        content=pre_tool_text,
                        tool_calls=[
                            {
                                "name": "read_context_artifact",
                                "args": {"artifact_id": "ctx-1"},
                                "id": "call-1",
                            }
                        ],
                        response_metadata={"finish_reason": "tool_calls"},
                    )
                },
            }
        )
    ]

    _ = [
        event
        async for event in handler.handle({"event": "on_chat_model_start", "data": {}})
    ]
    second_round_stream = [
        event
        async for event in handler.handle(
            {
                "event": "on_chat_model_stream",
                "data": {"chunk": AIMessageChunk(content=streamed_prefix)},
            }
        )
    ]
    second_round_end = [
        event
        async for event in handler.handle(
            {
                "event": "on_chat_model_end",
                "data": {
                    "output": AIMessage(
                        content=final_answer,
                        response_metadata={"finish_reason": "stop"},
                    )
                },
            }
        )
    ]

    visible = "".join(
        str(event.get("content") or "")
        for event in [
            *first_round_stream,
            *first_round_end,
            *second_round_stream,
            *second_round_end,
        ]
        if event.get("type") == "chunk"
    )

    assert visible == pre_tool_text + final_answer
    assert visible.count("曲柄滑块机构示意图") == 1
    assert handler._model_round_summaries[-1]["missing_recovered_len"] == (
        len(final_answer) - len(streamed_prefix)
    )


async def test_stream_heartbeat_does_not_cancel_slow_upstream_event():
    import asyncio

    from backend.application.agent_executors.streaming_runtime import (
        cancel_pending_stream_event,
        wait_for_stream_event,
    )

    cancelled = False

    async def slow_events():
        nonlocal cancelled
        try:
            await asyncio.sleep(0.05)
            yield {"event": "on_chat_model_stream", "data": {"chunk": "answer"}}
        except asyncio.CancelledError:
            cancelled = True
            raise

    event_iter = slow_events().__aiter__()
    pending_task = None
    heartbeat_count = 0
    try:
        while True:
            event, pending_task = await wait_for_stream_event(
                event_iter,
                pending_task,
                timeout=0.01,
            )
            if event is None:
                heartbeat_count += 1
                continue
            break
    finally:
        await cancel_pending_stream_event(pending_task)
        await event_iter.aclose()

    assert heartbeat_count >= 2
    assert event["event"] == "on_chat_model_stream"
    assert cancelled is False


def test_agent_event_wraps_runtime_chunk_as_message_delta():
    event = AgentEvent.from_runtime_payload(
        {"type": "chunk", "content": "abc"},
        request_id="req-1",
        thread_id="thread-1",
        sequence=1,
    )

    assert event.type == "message.delta"
    assert event.payload == {"content": "abc"}


def test_agent_event_wraps_task_runtime_as_subagent_event():
    event = AgentEvent.from_runtime_payload(
        {
            "type": "task_tool_end",
            "task_id": "task-1",
            "subagent_type": "deep-research",
            "tool": "multi_kb_search",
            "summary": "done",
        },
        request_id="req-1",
        thread_id="thread-1",
        sequence=2,
    )

    assert event.type == "subagent.tool.completed"
    assert event.payload["unit_id"] == "task-1"
    assert event.payload["agent_type"] == "deep-research"
    assert event.payload["tool"] == "multi_kb_search"


def test_sse_encoder_emits_standard_event_payload():
    event = AgentEvent.from_runtime_payload(
        {"type": "status", "status": "running"},
        request_id="req-1",
        thread_id="thread-1",
        sequence=1,
    )

    encoded = SSEEncoder().encode(event)

    assert encoded.startswith('data: {"type": "workflow.step", "payload": {"content": "running", "status": "running"}')
    assert '"request_id": "req-1"' in encoded


def test_sse_encoder_golden_message_delta_payload():
    event = AgentEvent(
        type="message.delta",
        request_id="req-1",
        thread_id="thread-1",
        sequence=7,
        payload={"content": "hello"},
        created_at="2026-06-08T00:00:00+00:00",
    )

    encoded = SSEEncoder().encode(event)
    payload = json.loads(encoded.removeprefix("data: ").strip())

    assert payload == {
        "type": "message.delta",
        "payload": {"content": "hello"},
        "request_id": "req-1",
        "thread_id": "thread-1",
        "sequence": 7,
        "created_at": "2026-06-08T00:00:00+00:00",
    }


def test_frontend_runtime_endpoints_use_standard_sse_encoder():
    import backend.api.http_server as http_server
    import backend.api.mcp_server as mcp_server
    import backend.api.routers.agent_runtime as agent_runtime
    import backend.api.routers.explore as explore
    import backend.api.routers.guest_agent as guest_agent
    import backend.api.routers.mcp as mcp

    agent_runtime_source = inspect.getsource(agent_runtime)
    explore_source = inspect.getsource(explore)
    guest_source = inspect.getsource(guest_agent)
    mcp_source = inspect.getsource(mcp)
    http_server_source = inspect.getsource(http_server)
    mcp_server_source = inspect.getsource(mcp_server)

    assert "SSEEncoder" in agent_runtime_source
    assert "SSEEncoder" in explore_source
    assert "to_legacy_sse_payload" not in agent_runtime_source
    assert "to_legacy_sse_payload" not in explore_source
    assert "legacy_events" not in guest_source
    assert "legacy_events" not in mcp_source
    assert "AgentService" in guest_source
    assert "AgentService" in mcp_source
    assert "legacy_events" not in http_server_source
    assert "legacy_events" not in mcp_server_source
    assert "create_mcp_sse_app" in http_server_source
    assert "create_mcp_sse_app" in mcp_server_source


def test_sse_encoder_emits_done_marker():
    event = AgentEvent(
        type="done",
        request_id="req-1",
        thread_id="thread-1",
        sequence=2,
    )

    payload = json.loads(SSEEncoder().encode(event).removeprefix("data: ").strip())

    assert payload["type"] == "done"
    assert payload["request_id"] == "req-1"
    assert payload["thread_id"] == "thread-1"
    assert payload["sequence"] == 2


async def test_sse_encoder_stream_preserves_done_envelope_and_compatibility_marker():
    async def events():
        yield AgentEvent(
            type="done",
            request_id="req-1",
            thread_id="thread-1",
            sequence=2,
        )

    chunks = [chunk async for chunk in SSEEncoder().encode_stream(events())]

    assert json.loads(chunks[0].removeprefix("data: ").strip())["type"] == "done"
    assert chunks[1] == "data: [DONE]\n\n"


async def test_sse_encoder_emits_transport_heartbeat_while_source_is_silent():
    async def events():
        await asyncio.sleep(0.03)
        yield AgentEvent(
            type="done",
            request_id="req-1",
            thread_id="thread-1",
            sequence=1,
        )

    chunks = [
        chunk
        async for chunk in SSEEncoder(
            heartbeat_interval_seconds=0.01,
        ).encode_stream(events())
    ]

    assert chunks[0] == ": keep-alive\n\n"
    assert any('"type": "done"' in chunk for chunk in chunks)
    assert chunks[-1] == "data: [DONE]\n\n"


async def test_sse_encoder_keeps_upstream_contextvar_lifecycle_in_one_task():
    scoped_value: ContextVar[str] = ContextVar("sse_test_value", default="unset")

    async def events():
        token = scoped_value.set("active")
        try:
            yield AgentEvent(
                type="done",
                request_id="req-1",
                thread_id="thread-1",
                sequence=1,
            )
        finally:
            scoped_value.reset(token)

    chunks = [chunk async for chunk in SSEEncoder().encode_stream(events())]

    assert chunks[-1] == "data: [DONE]\n\n"
    assert scoped_value.get() == "unset"


async def test_sse_encoder_converts_upstream_failure_to_error_and_done():
    async def events():
        raise RuntimeError("upstream failed")
        yield  # pragma: no cover

    chunks = [chunk async for chunk in SSEEncoder().encode_stream(events())]

    payloads = [
        json.loads(chunk.removeprefix("data: ").strip())
        for chunk in chunks
        if chunk.startswith("data: {")
    ]
    assert [payload["type"] for payload in payloads] == ["error", "done"]
    assert payloads[0]["payload"]["message"] == "智能体执行失败，请稍后重试"
    assert payloads[0]["payload"]["error_code"] == "agent_execution_failed"
    assert chunks[-1] == "data: [DONE]\n\n"


async def test_sse_encoder_keeps_last_event_correlation_on_upstream_failure():
    async def events():
        yield AgentEvent(
            type="workflow.started",
            request_id="req-correlation",
            thread_id="thread-correlation",
            sequence=3,
        )
        raise RuntimeError("upstream failed")

    chunks = [chunk async for chunk in SSEEncoder().encode_stream(events())]
    payloads = [
        json.loads(chunk.removeprefix("data: ").strip())
        for chunk in chunks
        if chunk.startswith("data: {")
    ]

    assert payloads[-2]["request_id"] == "req-correlation"
    assert payloads[-2]["thread_id"] == "thread-correlation"
    assert payloads[-2]["sequence"] == 4
    assert payloads[-1]["sequence"] == 5


async def test_stream_handler_rechecks_evidence_after_retrieval_changes():
    from backend.application.agent_executors.stream_handler import NEAgentStreamHandler

    handler = NEAgentStreamHandler(is_deep_research=False, thread_id="thread-test")
    async for _ in handler._on_tool_end(
        {
            "name": "query_project_tables",
            "data": {"output": '{"point_code":"GW18","datasets":{"water_quality_measurements":[]}}'},
        }
    ):
        pass

    for idx in range(2):
        async for _ in handler._on_tool_end(
            {
                "name": "multi_kb_search",
                "data": {"output": f"标准知识库证据 {idx + 1}"},
            }
        ):
            pass

    assert handler.should_evaluate_evidence(min_retrieval_blocks=1)
    assert not handler.should_evaluate_evidence(min_retrieval_blocks=3)

    fingerprint = handler.evidence_fingerprint()
    handler.mark_evidence_evaluated(fingerprint)
    assert not handler.should_evaluate_evidence(min_retrieval_blocks=1)

    async for _ in handler._on_tool_end(
        {
            "name": "multi_kb_search",
            "data": {"output": "标准知识库证据 3"},
        }
    ):
        pass

    assert handler.should_evaluate_evidence(min_retrieval_blocks=3)


async def test_agent_graph_emits_messages_mode_stream_events():
    from collections.abc import AsyncIterator
    from typing import Any

    from langchain_core.language_models import BaseChatModel
    from langchain_core.messages import AIMessage, AIMessageChunk, BaseMessage
    from langchain_core.outputs import ChatGeneration, ChatGenerationChunk, ChatResult

    from backend.core.agents.factory import create_agent_graph

    class MockChatModel(BaseChatModel):
        def _generate(
            self,
            messages: list[BaseMessage],
            stop: list[str] | None = None,
            run_manager: Any | None = None,
            **kwargs: Any,
        ) -> ChatResult:
            return ChatResult(generations=[ChatGeneration(message=AIMessage(content="hello world"))])

        async def _agenerate(
            self,
            messages: list[BaseMessage],
            stop: list[str] | None = None,
            run_manager: Any | None = None,
            **kwargs: Any,
        ) -> ChatResult:
            return ChatResult(generations=[ChatGeneration(message=AIMessage(content="hello world"))])

        async def _astream(
            self,
            messages: list[BaseMessage],
            stop: list[str] | None = None,
            run_manager: Any | None = None,
            **kwargs: Any,
        ) -> AsyncIterator[ChatGenerationChunk]:
            yield ChatGenerationChunk(message=AIMessageChunk(content="hello"))
            yield ChatGenerationChunk(message=AIMessageChunk(content=" world"))

        @property
        def _llm_type(self) -> str:
            return "mock"

    model = MockChatModel()
    graph = create_agent_graph(model=model, system_prompt="you are a helpful assistant")

    events = []
    async for event in graph.astream_events(
        {"messages": [("user", "hi")]},
        version="v2",
        stream_mode=["messages", "custom", "updates", "tasks"],
    ):
        events.append(event)

    message_stream_events = [
        event
        for event in events
        if event.get("event") == "on_chain_stream"
        and isinstance((event.get("data") or {}).get("chunk"), tuple)
        and (event.get("data") or {}).get("chunk")[0] == "messages"
    ]
    assert message_stream_events, "No LangGraph messages-mode event emitted"

    handler = NEAgentStreamHandler(thread_id="thread-stream")
    output = []
    for event in message_stream_events:
        output.extend([item async for item in handler.handle(event)])
    assert "".join(
        str(item.get("content") or "")
        for item in output
        if item.get("type") == "chunk"
    ) == "hello world"
