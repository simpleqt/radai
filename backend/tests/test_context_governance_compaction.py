"""上下文治理:剪枝/压实追踪、artifact 读取回路、请求预算压实、汇总缓存与可见压实事件。(拆自 test_context_governance.py)"""

import asyncio
import json
from dataclasses import replace
from types import SimpleNamespace

from langchain_core.messages import AIMessage, HumanMessage, SystemMessage, ToolMessage

from backend.application.agent_trace import AgentRunReplay, AgentTraceStore
from backend.core.agents.context.artifact_store import ArtifactStore
from backend.core.agents.context.engine import ContextEngine
from backend.core.agents.context.history_compactor import SUMMARY_PREFIX
from backend.core.agents.context.large_result_summarizer import (
    LargeResultSummarizer,
    _SUMMARY_CACHE,
    _SUMMARY_INFLIGHT,
)
from backend.core.agents.context.ledger import (
    ContextLedger,
    LedgerBudget,
    make_evidence_record,
)
from backend.core.agents.context.policy import ContextPolicy
from backend.core.agents.middlewares.context_pruning import ContextPruningMiddleware


def test_context_pruning_writes_context_pack_trace_when_enabled(tmp_path, monkeypatch):
    # 断言 legacy pack 轨迹契约(items_injected 计数):on 模式下投影对象存在时
    # legacy 记录选择被短路,its items_injected 诚实为 0;显式钉 off。
    monkeypatch.setenv("CONTEXT_OBJECT_MODE", "off")
    middleware = ContextPruningMiddleware(model_name=None)
    state = {
        "messages": [HumanMessage(content="标准 A 的限值是多少？")],
        "context_records": [
            make_evidence_record(
                source_tool="multi_kb_search",
                content="标准 A 的限值为 17 mg/L。",
                category="retrieval",
                source_id="kb-a",
                chunk_id="chunk-1",
                doc_title="标准 A",
                score=1.0,
            )
        ],
        "trace_context": {
            "enabled": True,
            "run_id": "ctx-trace-1",
            "workflow_name": "full",
            "trace_root": str(tmp_path),
            "unit_id": "unit-1",
        },
    }

    system_message = middleware._assemble_jit_system_message(
        SystemMessage(content="system"),
        state,
        messages=state["messages"],
    )

    summary = AgentRunReplay(AgentTraceStore(tmp_path)).dry_run("ctx-trace-1")
    records = AgentTraceStore(tmp_path).read_records("ctx-trace-1")

    assert system_message is not None
    assert "dynamic_context_pack" in str(system_message.content)
    # Accounting now travels through the ExtendedModelResponse command; the
    # node input state itself must stay unmutated.
    assert "context_pack" not in state
    assert summary.context_packs == 1
    pack_record = next(record for record in records if record.kind == "context_pack")
    assert pack_record.payload["unit_id"] == "unit-1"
    assert pack_record.payload["items_injected"] == 1


def test_context_pruning_merges_structured_and_legacy_evidence():
    middleware = ContextPruningMiddleware(model_name=None)
    state = {
        "messages": [HumanMessage(content="比较两类证据")],
        "context_records": [
            make_evidence_record(
                source_tool="structured_tool",
                content="结构化证据：样本 A 为 17 mg/L。",
                category="retrieval",
                source_id="structured-a",
            )
        ],
        "retrieved_evidence": ["遗留检索证据：样本 B 为 23 mg/L。"],
        "project_evidence": ["遗留项目证据：样本 C 为 5 mg/L。"],
    }

    system_message = middleware._assemble_jit_system_message(
        None,
        state,
        messages=state["messages"],
    )

    assert system_message is not None
    assert "结构化证据：样本 A" in system_message.content
    assert "遗留检索证据：样本 B" in system_message.content
    assert "遗留项目证据：样本 C" in system_message.content


async def test_web_search_uses_deterministic_page_capture_without_llm_delay(monkeypatch):
    # 断言 legacy 大结果路径;CONTEXT_OBJECT_MODE env 默认已是 on,显式钉 off。
    monkeypatch.setenv("CONTEXT_OBJECT_MODE", "off")
    middleware = ContextPruningMiddleware()
    calls = []

    async def unexpected_summary(**kwargs):
        calls.append(kwargs)
        return "不应调用"

    monkeypatch.setattr(
        middleware._large_result_summarizer,
        "summarize",
        unexpected_summary,
    )
    payload = json.dumps(
        {
            "query": "BF3 detector efficiency",
            "engine": "firecrawl",
            "results": [
                {
                    "title": "Detector paper",
                    "url": "https://example.com/paper",
                    "content": "measured evidence " + ("x" * 4000),
                    "provider": "firecrawl",
                }
            ],
        }
    )
    state = {
        "messages": [
            HumanMessage(content="检查 BF3 探测效率"),
            ToolMessage(
                content=payload,
                name="web_search",
                tool_call_id="call-web",
            ),
        ]
    }

    updates = await middleware.abefore_model(state, None)

    assert calls == []
    assert updates is not None
    assert len(updates["context_records"]) == 1
    assert updates["context_records"][0]["source_id"] == "https://example.com/paper"
    assert "BF3 detector efficiency" in updates["messages"][0].content
    assert "https://example.com/paper" in updates["messages"][0].content


async def test_large_structured_rows_are_compacted_without_child_model(monkeypatch):
    middleware = ContextPruningMiddleware(char_threshold=600)
    summary_calls = []

    async def unexpected_summary(**kwargs):
        summary_calls.append(kwargs)
        return "不应调用"

    monkeypatch.setattr(
        middleware._large_result_summarizer,
        "summarize",
        unexpected_summary,
    )
    payload = json.dumps(
        {
            "project_id": "gw-test",
            "data_version": 4,
            "row_count": 100,
            "columns": ["point_code", "x_coord", "y_coord"],
            "rows": [
                {"point_code": f"GW{index}", "x_coord": index, "y_coord": index + 1}
                for index in range(100)
            ],
            "query_kind": "data_query",
            "counts_toward_query_budget": True,
        },
        ensure_ascii=False,
    )
    state = {
        "messages": [
            HumanMessage(content="分析井位分布"),
            ToolMessage(
                content=payload,
                name="query_project_tables",
                tool_call_id="call-points",
            ),
        ]
    }

    updates = await middleware.abefore_model(state, None)

    assert summary_calls == []
    replacement = updates["messages"][0].content
    assert replacement.startswith("[Structured Result Compaction:")
    assert '"row_count": 100' in replacement
    assert '"omitted_row_count": 90' in replacement
    record = updates["context_records"][0]
    assert record["kind"] == "project_data"
    assert "structured_result_compaction" in record["tags"]


async def test_context_artifact_read_is_consumed_once_without_recursive_capture(monkeypatch):
    middleware = ContextPruningMiddleware(model_name=None)
    summary_calls = []

    async def unexpected_summary(**kwargs):
        summary_calls.append(kwargs)
        return "不应调用"

    monkeypatch.setattr(
        middleware._large_result_summarizer,
        "summarize",
        unexpected_summary,
    )
    payload = json.dumps(
        {
            "raw_ref": "artifact://context/source-result.json",
            "content": "verified artifact evidence " + ("x" * 15000),
        }
    )
    state = {
        "messages": [
            HumanMessage(content="读取已保存证据"),
            ToolMessage(
                content=payload,
                name="read_context_artifact",
                tool_call_id="call-artifact",
            ),
        ]
    }

    updates = await middleware.abefore_model(state, None)

    assert summary_calls == []
    assert updates is not None
    assert "context_records" not in updates
    replacement = updates["messages"][0]
    assert replacement.content.startswith("[Context Artifact Read:")
    assert "verified artifact evidence" in replacement.content
    assert "source-result.json" not in replacement.content

    second_updates = await middleware.abefore_model(
        {"messages": [state["messages"][0], replacement]},
        None,
    )
    assert second_updates is None


async def test_context_artifact_read_failure_stops_reference_retry_loop():
    middleware = ContextPruningMiddleware(model_name=None)
    state = {
        "messages": [
            HumanMessage(content="读取已保存证据"),
            ToolMessage(
                content=json.dumps(
                    {
                        "error": "artifact_access_denied",
                        "raw_ref": "artifact://context/guessed.json",
                    }
                ),
                name="read_context_artifact",
                tool_call_id="call-artifact-denied",
            ),
        ]
    }

    updates = await middleware.abefore_model(state, None)

    assert updates is not None
    replacement = updates["messages"][0].content
    assert "artifact_access_denied" in replacement
    assert "Do not retry" in replacement
    assert "guessed.json" not in replacement


def test_retrieval_keeps_artifact_reader_for_raw_ref_without_auto_load(
    tmp_path,
    monkeypatch,
):
    from langchain.agents.middleware.types import ModelRequest, ModelResponse

    # 断言 legacy artifact-ref 编译路径;env 默认已是 on,显式钉 off。
    monkeypatch.setenv("CONTEXT_OBJECT_MODE", "off")
    store = ArtifactStore(tmp_path)
    raw_ref = store.store(
        "Ogata-Banks equation evidence " * 80,
        thread_id="thread-1",
        source_tool="multi_kb_search",
    )
    record = make_evidence_record(
        source_tool="multi_kb_search",
        content="structured equation evidence",
        category="retrieval",
        source_id="hydrogeology-book",
        chunk_id="chunk-1",
        raw_ref=raw_ref,
    )
    middleware = ContextPruningMiddleware(model_name=None)
    middleware._context_ledger = ContextLedger(
        artifact_store=store,
        raw_ref_char_limit=12000,
    )
    messages = [HumanMessage(content="请使用已检索公式回答")]
    request = ModelRequest(
        model=SimpleNamespace(),
        messages=messages,
        tools=[
            {
                "type": "function",
                "function": {"name": "read_context_artifact"},
            },
            {"type": "function", "function": {"name": "calculator"}},
        ],
        tool_choice="auto",
        state={"messages": messages, "context_records": [record]},
        runtime=None,
    )
    captured = []

    middleware.wrap_model_call(
        request,
        lambda patched: captured.append(patched)
        or ModelResponse(result=[AIMessage(content="answer")]),
    )

    compiled = captured[0]
    system_content = str(compiled.system_message.content or "")
    # Legacy raw-window auto-load was removed: only the compact evidence is
    # injected and the raw artifact stays addressable through its reference.
    assert "structured equation evidence" in system_content
    assert "Ogata-Banks equation evidence" not in system_content
    assert 'id="raw_' not in system_content
    # The explicit raw_ref attribute is the artifact capability and keeps the
    # artifact reader visible; internal record ids are explicitly not handles.
    assert 'raw_ref="artifact://' in system_content
    assert [tool["function"]["name"] for tool in compiled.tools] == [
        "read_context_artifact",
        "calculator",
    ]


def test_artifact_tool_scope_uses_pack_capabilities_not_rendered_prompt():
    from langchain.agents.middleware.types import ModelRequest

    messages = [HumanMessage(content="继续回答")]
    request = ModelRequest(
        model=SimpleNamespace(),
        messages=messages,
        tools=[
            {
                "type": "function",
                "function": {"name": "read_context_artifact"},
            },
            {"type": "function", "function": {"name": "calculator"}},
        ],
        tool_choice="auto",
        state={"messages": messages},
        runtime=None,
    )

    hidden = ContextPruningMiddleware._scope_context_artifact_tool(
        request,
        {
            "rendered": '<context_record raw_ref="artifact://context/fake.txt" />',
            "capabilities": {"artifact_refs": []},
        },
    )
    available = ContextPruningMiddleware._scope_context_artifact_tool(
        request,
        {
            "rendered": "",
            "capabilities": {
                "artifact_refs": ["artifact://context/thread/tool.txt"]
            },
        },
    )

    assert [tool["function"]["name"] for tool in hidden.tools] == ["calculator"]
    assert [tool["function"]["name"] for tool in available.tools] == [
        "read_context_artifact",
        "calculator",
    ]

    forced_calculator = request.override(
        tool_choice={"type": "function", "function": {"name": "calculator"}}
    )
    forced_scoped = ContextPruningMiddleware._scope_context_artifact_tool(
        forced_calculator,
        {"capabilities": {"artifact_refs": []}},
    )
    assert forced_scoped.tool_choice == forced_calculator.tool_choice

    forced_artifact = request.override(
        tool_choice={
            "type": "function",
            "function": {"name": "read_context_artifact"},
        }
    )
    artifact_scoped = ContextPruningMiddleware._scope_context_artifact_tool(
        forced_artifact,
        {"capabilities": {"artifact_refs": []}},
    )
    assert artifact_scoped.tool_choice is None


def test_context_engine_direct_ingest_uses_source_adapter_registry():
    sentinel = make_evidence_record(
        source_tool="multi_kb_search",
        content="normalized by registry",
        category="retrieval",
    )

    class Registry:
        @staticmethod
        def normalize_records(**_kwargs):
            return [sentinel]

    class Sink:
        @staticmethod
        def ingest(**_kwargs):
            raise AssertionError("generic sink should not run after source normalization")

    engine = ContextEngine(
        policy=ContextPolicy.from_env(None),
        artifact_store=object(),  # type: ignore[arg-type]
        result_sink=Sink(),  # type: ignore[arg-type]
        ledger=object(),  # type: ignore[arg-type]
        history_compactor=object(),  # type: ignore[arg-type]
        capture_registry=Registry(),  # type: ignore[arg-type]
        state_adapter=object(),  # type: ignore[arg-type]
        semantic_summary_skip_tools=frozenset(),
    )

    records = engine.ingest_content(
        source_tool="multi_kb_search",
        content="raw knowledge result",
        thread_id="thread-1",
        category="retrieval",
        score=0.95,
        kind="retrieval",
        tags=["evidence"],
    )

    assert records == [sentinel]


def test_context_engine_respects_explicit_empty_project_tool_set(tmp_path):
    store = ArtifactStore(tmp_path)
    policy = replace(
        ContextPolicy.from_env(None),
        large_result_threshold_chars=20,
    )
    engine = ContextEngine.create(
        model_name=None,
        policy=policy,
        artifact_store=store,
        project_evidence_tools=set(),
    )

    captured = engine.capture_registry.capture(
        tool_name="query_project_tables",
        content="generic result " + ("x" * 100),
        tool_args={},
        thread_id="thread-1",
    )

    assert captured is not None
    assert captured.context_records
    assert captured.context_records[0]["kind"] == "analysis_result"


def test_context_pruning_compacts_history_and_writes_trace(tmp_path):
    policy = ContextPolicy(
        model_name=None,
        ledger_budget=LedgerBudget(
            model_max_input_tokens=2200,
            reserved_output_tokens=200,
            safety_margin_tokens=100,
        ),
        compaction_trigger_tokens=200,
        compaction_retained_window_tokens=80,
        compaction_summary_output_chars=1000,
    )
    middleware = ContextPruningMiddleware(model_name=None, policy=policy)
    state = {
        "messages": [
            HumanMessage(content="旧问题 " + "甲" * 400, id="u1"),
            SystemMessage(content="中间说明 " + "乙" * 400, id="s1"),
            HumanMessage(content="当前问题是什么？", id="u2"),
        ],
        "trace_context": {
            "enabled": True,
            "run_id": "compact-trace-1",
            "workflow_name": "full",
            "trace_root": str(tmp_path),
            "unit_id": "unit-1",
        },
    }

    updates = middleware.before_model(state, runtime=None)

    assert updates is not None
    assert updates["context_window_id"].startswith("ctxwin_")
    assert updates["messages"][0].id == "__remove_all__"
    assert SUMMARY_PREFIX in str(updates["messages"][1].content)
    assert updates["messages"][-1].content == "当前问题是什么？"
    assert updates["context_compaction_history"][0]["window_id"] == updates["context_window_id"]

    records = AgentTraceStore(tmp_path).read_records("compact-trace-1")
    compaction = next(record for record in records if record.kind == "compaction")
    assert compaction.payload["tokens_before"] > compaction.payload["tokens_after"]
    assert compaction.payload["retained_user_messages"] == 1


def test_context_pruning_retains_recent_turn_assistant_and_tool_messages(tmp_path):
    policy = ContextPolicy(
        model_name=None,
        ledger_budget=LedgerBudget(
            model_max_input_tokens=2200,
            reserved_output_tokens=200,
            safety_margin_tokens=100,
        ),
        compaction_trigger_tokens=200,
        compaction_retained_window_tokens=400,
        compaction_summary_output_chars=1000,
    )
    middleware = ContextPruningMiddleware(model_name=None, policy=policy)
    state = {
        "messages": [
            HumanMessage(content="旧问题 " + "甲" * 500, id="u1"),
            AIMessage(content="旧回答 " + "乙" * 300, id="a1"),
            HumanMessage(content="请继续检查当前数据", id="u2"),
            AIMessage(content="我会读取当前工具结果", id="a2"),
            ToolMessage(
                content="当前工具结果：A=17，B=23",
                name="check_current",
                tool_call_id="tc-current",
                id="t2",
            ),
        ],
        "trace_context": {
            "enabled": True,
            "run_id": "compact-turn-trace-1",
            "workflow_name": "full",
            "trace_root": str(tmp_path),
            "unit_id": "unit-1",
        },
    }

    updates = middleware.before_model(state, runtime=None)

    assert updates is not None
    retained_contents = [str(getattr(message, "content", "")) for message in updates["messages"]]
    assert any("请继续检查当前数据" in content for content in retained_contents)
    assert any("我会读取当前工具结果" in content for content in retained_contents)
    assert any("当前工具结果：A=17，B=23" in content for content in retained_contents)


def test_request_budget_compaction_is_sent_and_persisted_as_replacement_history():
    from langchain.agents.middleware.types import ModelRequest, ModelResponse
    from langgraph.graph.message import add_messages
    from backend.application.conversations.message_serializer import (
        serialize_checkpoint_messages,
    )

    policy = ContextPolicy(
        model_name=None,
        ledger_budget=LedgerBudget(
            model_max_input_tokens=6000,
            reserved_output_tokens=500,
            safety_margin_tokens=500,
        ),
        dynamic_context_reserved_tokens=1000,
        compaction_trigger_tokens=5000,
        compaction_retained_window_tokens=200,
        compaction_summary_output_chars=1000,
    )
    middleware = ContextPruningMiddleware(model_name=None, policy=policy)
    messages = [
        HumanMessage(content="old fact alpha beta gamma " * 400, id="u-old"),
        AIMessage(content="old answer", id="a-old"),
        HumanMessage(content="continue from the current result", id="u-current"),
    ]
    request = ModelRequest(
        model=SimpleNamespace(),
        messages=messages,
        system_message=SystemMessage(
            content="tool schema field required string " * 450
        ),
        state={"messages": messages},
        runtime=None,
    )
    captured = []

    response = middleware.wrap_model_call(
        request,
        lambda patched: captured.append(patched)
        or ModelResponse(result=[AIMessage(content="done", id="a-current")]),
    )

    assert len(captured) == 1
    assert not any(
        SUMMARY_PREFIX in str(message.content)
        for message in captured[0].messages
        if isinstance(message, HumanMessage)
    )
    assert SUMMARY_PREFIX in str(captured[0].system_message.content)
    assert "<internal_context_checkpoint>" in str(captured[0].system_message.content)
    assert captured[0].messages[-1].content == "continue from the current result"
    model_response = getattr(response, "model_response", response)
    assert model_response.result[0].id == "__remove_all__"
    summary_message = model_response.result[1]
    metadata = summary_message.additional_kwargs["context_compaction"]
    assert metadata["trigger"] == "request_budget"
    assert metadata["fixed_request_tokens"] > 0
    assert metadata["threshold_tokens"] == 4000

    merged_messages = add_messages(messages, model_response.result)
    assert [message.id for message in merged_messages][-1] == "a-current"
    serialized = serialize_checkpoint_messages(merged_messages)
    assert serialized[0]["role"] == "context_compaction"
    assert serialized[0]["metadata"]["context_compaction"]["window_id"] == metadata["window_id"]
    restored = middleware.after_model(
        {"messages": merged_messages},
        runtime=None,
    )
    assert restored is not None
    assert restored["context_window_id"] == metadata["window_id"]
    assert "summary" in restored["context_compaction"]
    assert "summary" not in restored["context_compaction_history"][0]
    assert restored["context_compaction_history"][0]["summary_sha256"]


def test_compaction_skip_is_traced_without_counting_as_completed(tmp_path):
    policy = ContextPolicy(
        model_name=None,
        ledger_budget=LedgerBudget(
            model_max_input_tokens=8000,
            reserved_output_tokens=500,
            safety_margin_tokens=500,
        ),
        compaction_trigger_tokens=1024,
        compaction_retained_window_tokens=200,
    )
    middleware = ContextPruningMiddleware(model_name=None, policy=policy)
    state = {
        "messages": [HumanMessage(content="only active turn " * 1200, id="u1")],
        "trace_context": {
            "enabled": True,
            "run_id": "compact-skip-1",
            "workflow_name": "full",
            "trace_root": str(tmp_path),
        },
    }

    updates = middleware.before_model(state, runtime=None)

    assert updates is None
    records = AgentTraceStore(tmp_path).read_records("compact-skip-1")
    attempt = next(record for record in records if record.kind == "compaction")
    replay = AgentRunReplay(AgentTraceStore(tmp_path)).dry_run("compact-skip-1")
    assert attempt.payload["status"] == "skipped"
    assert attempt.payload["error"] == "no_prunable_history"
    assert replay.compactions == 0
    assert replay.compaction_attempts == 1
    assert replay.compaction_skipped == 1


async def test_large_result_summarizer_cache_singleflights(monkeypatch):
    _SUMMARY_CACHE.clear()
    _SUMMARY_INFLIGHT.clear()
    calls = 0

    async def fake_summarize_uncached(self, *, source_tool, content, user_query=""):
        nonlocal calls
        calls += 1
        await asyncio.sleep(0.01)
        return f"summary:{source_tool}:{user_query}:{len(content)}"

    monkeypatch.setattr(
        LargeResultSummarizer,
        "_summarize_uncached",
        fake_summarize_uncached,
    )
    summarizer = LargeResultSummarizer(
        model_name="test-model",
        max_output_chars=600,
        max_input_chars=1200,
    )

    results = await asyncio.gather(
        *[
            summarizer.summarize(
                source_tool="tool-a",
                content="same content",
                user_query="same query",
            )
            for _ in range(3)
        ]
    )
    cached = await summarizer.summarize(
        source_tool="tool-a",
        content="same content",
        user_query="same query",
    )

    assert results == ["summary:tool-a:same query:12"] * 3
    assert cached == "summary:tool-a:same query:12"
    assert calls == 1


async def test_large_result_summarizer_can_disable_cache_with_zero_ttl(monkeypatch):
    _SUMMARY_CACHE.clear()
    _SUMMARY_INFLIGHT.clear()
    calls = 0

    async def fake_summarize_uncached(self, *, source_tool, content, user_query=""):
        nonlocal calls
        calls += 1
        return f"summary-{calls}"

    monkeypatch.setattr(
        LargeResultSummarizer,
        "_summarize_uncached",
        fake_summarize_uncached,
    )
    summarizer = LargeResultSummarizer(
        model_name="test-model",
        cache_ttl_seconds=0,
    )

    first = await summarizer.summarize(source_tool="tool", content="same")
    second = await summarizer.summarize(source_tool="tool", content="same")

    assert first == "summary-1"
    assert second == "summary-2"
    assert calls == 2
    assert not _SUMMARY_CACHE


async def test_stream_handler_emits_visible_compaction_summary_once():
    from backend.application.agent_executors.stream_handler import NEAgentStreamHandler

    handler = NEAgentStreamHandler(thread_id="thread-1")
    event = {
        "event": "on_chain_end",
        "name": "ContextPruningMiddleware.after_model",
        "data": {
            "output": {
                "context_compaction": {
                    "window_id": "ctxwin-visible-1",
                    "previous_window_id": None,
                    "trigger": "request_budget",
                    "tokens_before": 4200,
                    "tokens_after": 1300,
                    "removed_messages": 8,
                    "summary": f"{SUMMARY_PREFIX}\n已完成数据核验，下一步生成报告。",
                }
            }
        },
    }

    first = [item async for item in handler.handle(event)]
    second = [item async for item in handler.handle(event)]

    assert len(first) == 1
    assert first[0]["type"] == "event"
    assert first[0]["event"]["kind"] == "context_compaction"
    assert first[0]["event"]["summary"] == "已完成数据核验，下一步生成报告。"
    assert first[0]["event"]["metadata"]["window_id"] == "ctxwin-visible-1"
    assert second == []

    filtered = [
        item
        async for item in handler.handle(
            {
                "event": "on_chat_model_end",
                "tags": ["history-compaction"],
                "metadata": {"purpose": "history_compaction"},
                "data": {"output": AIMessage(content="internal summary")},
            }
        )
    ]
    assert [item["type"] for item in filtered] == ["status", "event"]
    assert all(item.get("type") != "chunk" for item in filtered)


def test_wrap_model_call_returns_durable_context_updates_command():
    """Context accounting must flow through the official command channel.

    Mutating ``request.state`` inside ``wrap_model_call`` is never persisted by
    LangGraph, so ``context_pack``/``context_budget``/``context_accounting``
    and the trace index must be returned as an ``ExtendedModelResponse``
    command and the input state must stay untouched.
    """

    from langchain.agents.middleware.types import (
        ExtendedModelResponse,
        ModelRequest,
        ModelResponse,
    )

    middleware = ContextPruningMiddleware()
    user_message = HumanMessage(content="question")
    state = {
        "messages": [user_message],
        "loaded_skills": {},
        "retrieved_evidence": ["durable evidence text"],
        "project_evidence": [],
        "context_pack_trace_index": 3,
    }
    request = ModelRequest(
        model=SimpleNamespace(),
        messages=[user_message],
        system_message=SystemMessage(content="base"),
        state=state,
        runtime=None,
    )

    response = middleware.wrap_model_call(
        request,
        lambda patched: ModelResponse(result=[AIMessage(content="ok")]),
    )

    assert isinstance(response, ExtendedModelResponse)
    assert response.command is not None
    update = response.command.update
    # Large rendered payloads must not be checkpointed; metadata is kept.
    assert update["context_pack"]["rendered"] == ""
    assert update["context_budget"] is not None
    # The model-call index continues from the persisted value instead of
    # silently resetting to 1 on every super-step.
    assert update["context_pack_trace_index"] == 4
    # The node input state itself is left unmutated.
    assert state["context_pack_trace_index"] == 3
    assert "context_pack" not in state
    assert "context_budget" not in state


def test_before_model_compaction_counts_request_fixed_tokens():
    """The hook trigger must include the measured system/tool overhead.

    ``before_model`` has no compiled request, so it reuses the fixed-token
    measurement from the previous model call in the same request; with zero it
    systematically compacted later than the configured trigger.
    """

    policy = ContextPolicy(
        model_name=None,
        ledger_budget=LedgerBudget(
            model_max_input_tokens=64000,
            reserved_output_tokens=1000,
            safety_margin_tokens=500,
        ),
        compaction_trigger_tokens=1024,
        compaction_retained_window_tokens=200,
    )
    middleware = ContextPruningMiddleware(model_name=None, policy=policy)
    messages = [
        HumanMessage(content="first question " + "history filler " * 60, id="u1"),
        AIMessage(content="first answer " + "answer filler " * 60, id="a1"),
        HumanMessage(content="second question", id="u2"),
    ]
    tokens_alone = middleware._history_compactor.count_messages(messages)
    assert tokens_alone < 1024

    updates = middleware.before_model({"messages": list(messages)}, runtime=None)
    assert not (updates or {}).get("context_compaction")

    middleware._last_request_fixed_tokens = 1024 - tokens_alone + 1
    updates = middleware.before_model({"messages": list(messages)}, runtime=None)
    assert (updates or {}).get("context_compaction")
    assert updates["context_compaction"]["fixed_request_tokens"] == (
        middleware._last_request_fixed_tokens
    )


def test_provider_overflow_markers_are_env_extensible(monkeypatch):
    detector = ContextPruningMiddleware._is_provider_context_overflow

    exotic = Exception("gateway said: PROMPT_TOKENS_OVER_CAPACITY for request")
    assert detector(exotic) is False

    monkeypatch.setenv(
        "AGENT_CONTEXT_OVERFLOW_MARKERS",
        "prompt_tokens_over_capacity, another marker",
    )
    assert detector(exotic) is True
    assert detector(Exception("context_length_exceeded")) is True


async def test_context_updates_persist_across_model_rounds_in_graph():
    """End to end: the command channel really lands in the checkpointed state."""

    from langchain_core.language_models.fake_chat_models import FakeListChatModel
    from langgraph.checkpoint.memory import InMemorySaver

    from backend.core.agents import RuntimeFeatures, ThreadState, create_agent_graph
    from backend.core.agents.middlewares.context_pruning import (
        ContextPruningMiddleware,
    )

    graph = create_agent_graph(
        model=FakeListChatModel(responses=["第一轮回答", "第二轮回答"]),
        tools=[],
        system_prompt="system",
        features=RuntimeFeatures(
            summarization=False,
            vision=False,
            subagent=False,
            clarification=False,
            guardrail=False,
        ),
        extra_middleware=[ContextPruningMiddleware(model_name=None)],
        checkpointer=InMemorySaver(),
        state_schema=ThreadState,
    )
    config = {"configurable": {"thread_id": "trace-e2e"}}

    await graph.ainvoke(
        {
            "messages": [HumanMessage(content="第一问")],
            "retrieved_evidence": ["证据 A：样本值 17 mg/L"],
        },
        config,
    )
    first_state = await graph.aget_state(config)
    assert first_state.values.get("context_pack_trace_index") == 1

    await graph.ainvoke({"messages": [HumanMessage(content="第二问")]}, config)
    second_state = await graph.aget_state(config)
    assert second_state.values.get("context_pack_trace_index") == 2
    assert (second_state.values.get("context_pack") or {}).get("rendered") == ""
    assert second_state.values.get("context_budget") is not None
