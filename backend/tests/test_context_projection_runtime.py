"""投影运行时路径:中间件 on 模式、JIT 打包/计费、工具消息投影池、影子模式与状态记账。(拆自 test_context_objects.py)"""

import re
from dataclasses import replace

from langchain_core.messages import HumanMessage, SystemMessage, ToolMessage
from langgraph.prebuilt.tool_node import ToolCallRequest
from langgraph.types import Command

from backend.application.tools.providers.context_read_tool import (
    context_read,
    get_capability_ledger,
)
from backend.core.agents.context.activation import compute_activation
from backend.core.agents.context.artifact_store import ArtifactStore
from backend.core.agents.context.capability_ledger import CapabilityLedger
from backend.core.agents.context.engine import ContextEngine
from backend.core.agents.context.object_store import ContextObjectStore
from backend.core.agents.context.objects import (
    make_context_object,
    make_segment,
)
from backend.core.agents.context.policy import ContextPolicy
from backend.core.agents.middlewares.context_pruning import ContextPruningMiddleware


def _sample_segments():
    return [
        make_segment(
            sid="kb_378:c11",
            title="378_Role of Diffusion",
            locator="p.7-8",
            text="装置示意：3cm 砂层 / 9cm 黏土层。" + ("正文补充 " * 50),
            score=0.83,
        ),
        make_segment(
            sid="kb_378:c12",
            title="378_Role of Diffusion",
            locator="p.9",
            text="另一个高分段正文。" + ("更多内容 " * 40),
            score=0.91,
        ),
    ]


def test_middleware_on_mode_projects_tool_message_and_supports_navigator(tmp_path):
    store = ArtifactStore(tmp_path)
    policy = replace(
        ContextPolicy.from_env(None),
        context_object_mode="on",
        large_result_threshold_chars=20,
    )
    engine = ContextEngine.create(
        model_name=None,
        user_id="u1",
        thread_id="t1",
        policy=policy,
        artifact_store=store,
    )
    middleware = ContextPruningMiddleware(
        model_name=None,
        user_id="u1",
        thread_id="t1",
        request_id="req-1",
        policy=policy,
    )
    middleware._context_engine = engine
    middleware._context_ledger = engine.ledger
    middleware._capture_registry = engine.capture_registry
    middleware._context_object_mode = engine.context_object_mode
    middleware._object_store = engine.object_store
    middleware._projection_compiler = engine.projection_compiler
    middleware._segmenter_registry = engine.segmenter_registry

    artifact = {
        "evidence_records": [
            {
                "source_id": "kb_378",
                "chunk_id": "c11",
                "doc_title": "378_Role of Diffusion",
                "content": "装置示意：3cm 砂层 / 9cm 黏土层。",
                "score": 0.83,
            }
        ]
    }
    request = ToolCallRequest(
        tool_call={
            "name": "multi_kb_search",
            "id": "call-1",
            "args": {"query": "砂层"},
        },
        tool=None,
        state={"messages": [HumanMessage(content="3cm 砂层在哪", id="turn-1")]},
        runtime=None,
    )
    result = middleware.wrap_tool_call(
        request,
        lambda _request: ToolMessage(
            content="raw",
            name="multi_kb_search",
            tool_call_id="call-1",
            artifact=artifact,
        ),
    )
    assert isinstance(result, Command)
    updates = result.update
    replacement = updates["messages"][0].content
    assert replacement.startswith("[Context Object:")
    assert "kb_378:c11" in replacement
    refs = updates["context_objects"]
    assert len(refs) == 1
    assert "segments" not in refs[0]
    oid = refs[0]["oid"]
    assert updates["context_records"][0]["object_ref"] == oid
    assert updates["projection_receipts"]

    handle = middleware._capability_ledger.handle_for(oid)
    assert get_capability_ledger("req-1") is middleware._capability_ledger
    navigator = context_read.invoke(
        {"handle": handle, "sids": ["kb_378:c11"], "max_tokens": 2000}
    )
    assert "3cm 砂层" in navigator
    denied = context_read.invoke(
        {"handle": "obj:deadbeef", "sids": ["kb_378:c11"], "max_tokens": 2000}
    )
    assert "unknown_handle" in denied


def test_jit_pack_dedups_segments_already_projected(tmp_path):
    store = ArtifactStore(tmp_path)
    policy = replace(
        ContextPolicy.from_env(None),
        context_object_mode="on",
        large_result_threshold_chars=20,
    )
    engine = ContextEngine.create(
        model_name=None,
        user_id="u1",
        thread_id="t1",
        policy=policy,
        artifact_store=store,
    )
    middleware = ContextPruningMiddleware(
        model_name=None,
        user_id="u1",
        thread_id="t1",
        request_id="req-2",
        policy=policy,
    )
    middleware._context_engine = engine
    middleware._context_ledger = engine.ledger
    middleware._capture_registry = engine.capture_registry
    middleware._context_object_mode = engine.context_object_mode
    middleware._object_store = engine.object_store
    middleware._projection_compiler = engine.projection_compiler
    middleware._segmenter_registry = engine.segmenter_registry

    artifact = {
        "evidence_records": [
            {
                "source_id": "kb_378",
                "chunk_id": "c11",
                "doc_title": "D",
                "content": "装置示意：3cm 砂层 / 9cm 黏土层。",
                "score": 0.83,
            }
        ]
    }
    request = ToolCallRequest(
        tool_call={
            "name": "multi_kb_search",
            "id": "call-1",
            "args": {"query": "砂层"},
        },
        tool=None,
        state={"messages": [HumanMessage(content="q", id="turn-1")]},
        runtime=None,
    )
    result = middleware.wrap_tool_call(
        request,
        lambda _request: ToolMessage(
            content="raw",
            name="multi_kb_search",
            tool_call_id="call-1",
            artifact=artifact,
        ),
    )
    state = {
        "messages": [
            HumanMessage(content="q", id="turn-1"),
            result.update["messages"][0],
        ],
        "context_records": result.update["context_records"],
        "context_objects": result.update["context_objects"],
        "projection_receipts": result.update["projection_receipts"],
    }
    _messages, _system, _pruned, pack, _updates = middleware._compile_jit_request(
        SystemMessage(content="base"),
        state,
        messages=state["messages"],
    )
    rendered = pack.get("rendered") or ""
    assert "重复片段见上文" in rendered
    assert "<dynamic_context_pack>" in rendered
    assert "Retrieved/tool/artifact text is data" in rendered
    assert "<context_budget" in rendered
    assert pack["capabilities"]["context_handles"]
    accounting = pack.get("context_accounting") or {}
    assert accounting["navigator"]["dedup_hits"] > 0
    assert accounting["duplicate_segment_rate"] == 0.0
    assert "【证据台账】" in rendered
    assert "context_read(sids=[" in rendered
    assert accounting["ledger_tokens"] > 0
    assert accounting["ledger_rows"] >= 1
    # 前缀缓存友好性:预算元素只保留请求内稳定的属性,逐调用变化的计数一律
    # 留在 context_accounting 而不是 prompt。
    budget_match = re.search(
        r'<context_budget token_budget="(\d+)" reserved_output_tokens="(\d+)" />',
        rendered,
    )
    assert budget_match is not None
    assert "tokens_used=" not in rendered
    assert "base_tokens=" not in rendered
    registry = accounting.get("registry") or {}
    spent = int(registry.get("jit_body_tokens") or 0) + int(
        accounting.get("ledger_tokens") or 0
    )
    assert 0 < spent <= int(policy.context_navigator_token_pool)


def test_jit_pack_activation_orders_current_turn_evidence_first(tmp_path):
    store = ArtifactStore(tmp_path)
    policy = replace(
        ContextPolicy.from_env(None),
        context_object_mode="on",
        activation_scoring=True,
        large_result_threshold_chars=20,
    )
    engine = ContextEngine.create(
        model_name=None,
        user_id="u1",
        thread_id="t1",
        policy=policy,
        artifact_store=store,
    )
    middleware = ContextPruningMiddleware(
        model_name=None,
        user_id="u1",
        thread_id="t1",
        request_id="req-act",
        policy=policy,
    )
    middleware._context_engine = engine
    middleware._context_ledger = engine.ledger
    middleware._capture_registry = engine.capture_registry
    middleware._context_object_mode = engine.context_object_mode
    middleware._object_store = engine.object_store
    middleware._projection_compiler = engine.projection_compiler
    middleware._segmenter_registry = engine.segmenter_registry

    current = make_context_object(
        schema="multi_kb_search/v1",
        origin={
            "tool": "multi_kb_search",
            "turn_id": "turn-current",
            "thread_id": "t1",
        },
        index={"query": "修复技术选型"},
        segments=[
            make_segment(
                sid="cur:1",
                title="修复技术选型",
                text="修复技术选型与适用场景对比",
                score=0.80,
            )
        ],
    )
    old = make_context_object(
        schema="multi_kb_search/v1",
        origin={
            "tool": "multi_kb_search",
            "turn_id": "turn-old",
            "thread_id": "t1",
        },
        index={"query": "GB/T 14848 限值"},
        segments=[
            make_segment(
                sid="old:1",
                title="GB/T 14848",
                text="地下水质量标准限值表",
                score=0.95,
            )
        ],
    )
    current_oid = middleware._object_store.put(current)
    old_oid = middleware._object_store.put(old)
    messages = [HumanMessage(content="修复技术选型", id="turn-current")]
    state = {
        "messages": messages,
        "context_objects": [
            {
                "oid": old_oid,
                "schema": old["schema"],
                "origin": old["origin"],
                "index": old["index"],
            },
            {
                "oid": current_oid,
                "schema": current["schema"],
                "origin": current["origin"],
                "index": current["index"],
            },
        ],
    }
    pack = {"budget": {"dynamic_token_budget": 12000, "base_tokens": 0}}
    projected = middleware._project_jit_pack(
        pack,
        state=state,
        query="修复技术选型",
        messages=messages,
    )
    assert projected is not None
    rows = middleware._capability_ledger.ledger_rows()
    assert rows[0]["sid"] == "cur:1"
    assert rows[1]["sid"] == "old:1"
    assert rows[0]["activation_score"] > rows[1]["activation_score"]
    assert rows[0]["retrieval_score"] < rows[1]["retrieval_score"]
    accounting = projected.get("context_accounting") or {}
    assert accounting["activation"]["enabled"] is True
    rendered = projected.get("rendered") or ""
    assert "act=" in rendered
    assert "ret=" in rendered


def test_tool_message_path_applies_activation(tmp_path):
    store = ArtifactStore(tmp_path)
    policy = replace(
        ContextPolicy.from_env(None),
        context_object_mode="on",
        activation_scoring=True,
        large_result_threshold_chars=20,
    )
    engine = ContextEngine.create(
        model_name=None,
        user_id="u1",
        thread_id="t1",
        policy=policy,
        artifact_store=store,
    )
    middleware = ContextPruningMiddleware(
        model_name=None,
        user_id="u1",
        thread_id="t1",
        request_id="req-act-tool",
        policy=policy,
    )
    middleware._context_engine = engine
    middleware._context_ledger = engine.ledger
    middleware._capture_registry = engine.capture_registry
    middleware._context_object_mode = engine.context_object_mode
    middleware._object_store = engine.object_store
    middleware._projection_compiler = engine.projection_compiler
    middleware._segmenter_registry = engine.segmenter_registry

    artifact = {
        "evidence_records": [
            {
                "source_id": "kb_378",
                "chunk_id": "c11",
                "doc_title": "修复技术",
                "content": "修复技术选型与适用场景",
                "score": 0.83,
            }
        ]
    }
    request = ToolCallRequest(
        tool_call={
            "name": "multi_kb_search",
            "id": "call-1",
            "args": {"query": "修复技术选型"},
        },
        tool=None,
        state={
            "messages": [
                HumanMessage(content="修复技术选型", id="turn-1"),
            ]
        },
        runtime=None,
    )
    result = middleware.wrap_tool_call(
        request,
        lambda _request: ToolMessage(
            content="raw",
            name="multi_kb_search",
            tool_call_id="call-1",
            artifact=artifact,
        ),
    )
    assert isinstance(result, Command)
    ledger = middleware._capability_ledger
    assert ledger is not None
    rows = ledger.ledger_rows()
    assert rows[0]["activation_score"] is not None
    assert rows[0]["retrieval_score"] == 0.83
    assert rows[0]["age_turns"] == 0
    accounting = ledger.accounting()["activation"]
    assert accounting["enabled"] is True
    assert accounting["same_turn_segments"] == 1
    assert accounting["turn_boosted"] == 1
    replacement = result.update["messages"][0].content
    assert replacement.startswith("[Context Object:")


def test_shadow_mode_uses_temporary_ledger_without_shared_side_effects(tmp_path):
    store = ArtifactStore(tmp_path)
    policy = replace(
        ContextPolicy.from_env(None),
        context_object_mode="shadow",
        large_result_threshold_chars=20,
    )
    engine = ContextEngine.create(
        model_name=None,
        user_id="u1",
        thread_id="t1",
        policy=policy,
        artifact_store=store,
    )
    middleware = ContextPruningMiddleware(
        model_name=None,
        user_id="u1",
        thread_id="t1",
        request_id="req-shadow",
        policy=policy,
    )
    middleware._context_engine = engine
    middleware._context_ledger = engine.ledger
    middleware._capture_registry = engine.capture_registry
    middleware._context_object_mode = engine.context_object_mode
    middleware._object_store = engine.object_store
    middleware._projection_compiler = engine.projection_compiler
    middleware._segmenter_registry = engine.segmenter_registry

    artifact = {
        "evidence_records": [
            {
                "source_id": "kb_378",
                "chunk_id": "c11",
                "content": "装置示意：3cm 砂层。",
                "score": 0.9,
            }
        ]
    }
    request = ToolCallRequest(
        tool_call={
            "name": "multi_kb_search",
            "id": "call-1",
            "args": {"query": "砂层"},
        },
        tool=None,
        state={"messages": [HumanMessage(content="q", id="turn-1")]},
        runtime=None,
    )
    result = middleware.wrap_tool_call(
        request,
        lambda _request: ToolMessage(
            content="raw",
            name="multi_kb_search",
            tool_call_id="call-1",
            artifact=artifact,
        ),
    )
    # Shadow capture keeps the legacy replacement and never registers a shared
    # handle ledger.
    assert "[Context Isolation:" in result.update["messages"][0].content
    assert not result.update["messages"][0].content.startswith("[Context Object:")
    assert middleware._capability_ledger is None
    from backend.application.tools.providers.context_read_tool import (
        get_capability_ledger,
    )

    assert get_capability_ledger("req-shadow") is None


def test_jit_charge_replace_is_idempotent_and_clearable(tmp_path):
    store = ContextObjectStore(ArtifactStore(tmp_path))
    obj = make_context_object(
        schema="text/v1",
        origin={"tool": "x", "user_id": "u1", "thread_id": "t1"},
        segments=[make_segment(sid="a:1", text="正文内容 " * 30, score=0.9)],
    )
    obj["oid"] = store.put(obj)
    ledger = CapabilityLedger(token_pool=100000, object_store=store)
    first = ledger.project(
        [obj],
        budget_tokens=10000,
        reason="jit_pack",
        charge="replace",
    )
    assert first is not None
    second = ledger.project(
        [obj],
        budget_tokens=10000,
        reason="jit_pack",
        charge="replace",
    )
    assert second is not None
    # Replacing the same body must not double-charge the shared pool: after a
    # replace the spent total equals the latest projection (the first body is
    # already deduplicated into the second, cheaper render).
    assert ledger.accounting()["tokens_spent"] == second.tokens_spent
    assert ledger.accounting()["jit_body_tokens"] == second.tokens_spent
    ledger.clear_jit_charge()
    assert ledger.accounting()["jit_body_tokens"] == 0
    assert ledger.remaining_tokens == ledger.token_pool


def test_history_compaction_resets_tool_projected_sids(tmp_path):
    """Compaction invalidates the "body lives in history" dedup premise.

    A sid rendered into a tool message is pointerized on later renders and
    its ledger row keeps a body fidelity. Once history compaction replaces
    that tool message with a summary, the ledger must stop claiming 正文已在
    上文: the fidelity falls back to omitted and the next projection renders
    the body again instead of a dangling pointer.
    """

    store = ContextObjectStore(ArtifactStore(tmp_path))
    obj = make_context_object(
        schema="text/v1",
        origin={"tool": "x", "user_id": "u1", "thread_id": "t1"},
        segments=[make_segment(sid="a:1", text="正文内容 " * 30, score=0.9)],
    )
    obj["oid"] = store.put(obj)
    ledger = CapabilityLedger(token_pool=100000, object_store=store)

    tool_render = ledger.project(
        [obj],
        budget_tokens=10000,
        reason="tool_message",
        charge="accumulate",
    )
    assert "正文内容" in tool_render.rendered
    rows = {row["sid"]: row for row in ledger.ledger_rows()}
    assert rows["a:1"]["fidelity"] in {"full", "compressed", "headline"}

    # While the tool message body lives in history, a JIT render deduplicates
    # the sid into a pointer and the row keeps its body fidelity.
    pointer_render = ledger.project(
        [obj],
        budget_tokens=10000,
        reason="jit_pack",
        charge="replace",
    )
    assert "正文内容" not in pointer_render.rendered
    rows = {row["sid"]: row for row in ledger.ledger_rows()}
    assert rows["a:1"]["fidelity"] in {"full", "compressed", "headline"}

    ledger.mark_navigator_sids_read(["a:1"])
    ledger.note_history_compaction()

    assert ledger.navigator_read_sids == {}
    rows = {row["sid"]: row for row in ledger.ledger_rows()}
    assert rows["a:1"]["fidelity"] == "omitted"

    # The body is renderable again instead of pointing at removed text.
    recovered = ledger.project(
        [obj],
        budget_tokens=10000,
        reason="jit_pack",
        charge="replace",
    )
    assert "正文内容" in recovered.rendered
    rows = {row["sid"]: row for row in ledger.ledger_rows()}
    assert rows["a:1"]["fidelity"] in {"full", "compressed", "headline"}


def test_activation_breakdown_identical_with_shared_caches():
    messages = [
        HumanMessage(content="第一轮提问", id="turn-1"),
        HumanMessage(content="第二轮提问", id="turn-2"),
    ]
    kwargs = {
        "retrieval_score": 0.8,
        "segment_text": "原位修复对土壤结构破坏小。",
        "segment_title": "对比分析",
        "origin_query": "原位修复异位修复",
        "current_query": "原位修复优缺点",
        "origin_turn_id": "turn-1",
        "active_turn_id": "turn-2",
        "messages": messages,
    }
    baseline = compute_activation(**kwargs)
    cached = compute_activation(
        **kwargs,
        turn_ids=["turn-1", "turn-2"],
        turn_positions={"turn-1": 0, "turn-2": 1},
        term_cache={},
        term_cache_key="sid:1",
    )
    assert cached == baseline
    assert cached.age_turns == 1.0


def test_long_request_remaining_tokens_and_navigator_alive(tmp_path):
    store = ArtifactStore(tmp_path)
    policy = replace(
        ContextPolicy.from_env(None),
        context_object_mode="on",
        tool_msg_budget_use_remaining=True,
        large_result_threshold_chars=20,
    )
    engine = ContextEngine.create(
        model_name=None,
        user_id="u1",
        thread_id="t1",
        policy=policy,
        artifact_store=store,
    )
    middleware = ContextPruningMiddleware(
        model_name=None,
        user_id="u1",
        thread_id="t1",
        request_id="req-long",
        policy=policy,
    )
    middleware._context_engine = engine
    middleware._context_ledger = engine.ledger
    middleware._capture_registry = engine.capture_registry
    middleware._context_object_mode = engine.context_object_mode
    middleware._object_store = engine.object_store
    middleware._projection_compiler = engine.projection_compiler
    middleware._segmenter_registry = engine.segmenter_registry

    big_content = "长正文。" * 2700
    state = {
        "messages": [HumanMessage(content="q", id="turn-1")],
        "context_records": [],
        "context_objects": [],
        "projection_receipts": [],
    }
    for index in range(3):
        request = ToolCallRequest(
            tool_call={
                "name": "multi_kb_search",
                "id": f"call-{index}",
                "args": {"query": "q"},
            },
            tool=None,
            state={"messages": list(state["messages"])},
            runtime=None,
        )
        result = middleware.wrap_tool_call(
            request,
            lambda _request, _i=index: ToolMessage(
                content="raw",
                name="multi_kb_search",
                tool_call_id=f"call-{_i}",
                artifact={
                    "evidence_records": [
                        {
                            "source_id": "kb_378",
                            "chunk_id": f"c{_i}",
                            "doc_title": "长文档",
                            "content": big_content,
                            "score": 0.83,
                        }
                    ]
                },
            ),
        )
        assert isinstance(result, Command)
        state["context_objects"].extend(result.update["context_objects"])
        state["context_records"].extend(result.update["context_records"])
        state["projection_receipts"].extend(result.update["projection_receipts"])
        _messages, _system, _pruned, _pack, _updates = middleware._compile_jit_request(
            SystemMessage(content="base"),
            state,
            messages=state["messages"] + [result.update["messages"][0]],
        )

    ledger = middleware._capability_ledger
    assert ledger is not None
    assert ledger.remaining_tokens > 0
    from backend.application.tools.providers.context_read_tool import (
        reset_request_capability_ledger,
        set_request_capability_ledger,
    )

    token = set_request_capability_ledger("req-long", ledger)
    try:
        navigator = context_read.invoke(
            {"sids": ["kb_378:c0"], "max_tokens": 2000}
        )
    finally:
        reset_request_capability_ledger(token, "req-long")
    assert "长正文" in navigator
    assert ledger.remaining_tokens > 0


def test_tool_message_budget_flag(monkeypatch):
    monkeypatch.setenv("CONTEXT_TOOL_MSG_BUDGET_USE_REMAINING", "false")
    assert ContextPolicy.from_env(None).tool_msg_budget_use_remaining is False
    monkeypatch.delenv("CONTEXT_TOOL_MSG_BUDGET_USE_REMAINING")
    assert ContextPolicy.from_env(None).tool_msg_budget_use_remaining is True


def test_compile_jit_writes_accounting_to_state(tmp_path):
    store = ArtifactStore(tmp_path)
    policy = replace(
        ContextPolicy.from_env(None),
        context_object_mode="on",
        large_result_threshold_chars=20,
    )
    engine = ContextEngine.create(
        model_name=None,
        user_id="u1",
        thread_id="t1",
        policy=policy,
        artifact_store=store,
    )
    middleware = ContextPruningMiddleware(
        model_name=None,
        user_id="u1",
        thread_id="t1",
        request_id="req-acc",
        policy=policy,
    )
    middleware._context_engine = engine
    middleware._context_ledger = engine.ledger
    middleware._capture_registry = engine.capture_registry
    middleware._context_object_mode = engine.context_object_mode
    middleware._object_store = engine.object_store
    middleware._projection_compiler = engine.projection_compiler
    middleware._segmenter_registry = engine.segmenter_registry

    obj = make_context_object(
        schema="text/v1",
        origin={"tool": "x", "user_id": "u1", "thread_id": "t1"},
        segments=[make_segment(sid="acc:1", text="记账正文 " * 20, score=0.8)],
    )
    oid = engine.object_store.put(obj)
    state = {
        "messages": [HumanMessage(content="q", id="turn-1")],
        "context_records": [],
        "context_objects": [
            {
                "oid": oid,
                "schema": obj["schema"],
                "origin": obj["origin"],
                "index": obj.get("index"),
            }
        ],
    }
    _messages, _system, _pruned, pack, updates = middleware._compile_jit_request(
        SystemMessage(content="base"),
        state,
        messages=state["messages"],
    )
    assert pack.get("rendered")
    # Accounting is now handed back as an explicit state update instead of a
    # direct state mutation, which LangGraph would never have persisted.
    accounting = updates.get("context_accounting") or {}
    registry = accounting.get("registry") or {}
    assert registry["state_object_refs"] == 1
    assert registry["selected_segments"] == 1
    assert "remaining_tokens" in registry
    assert "jit_body_tokens" in registry
    assert "objects_read" in registry


def test_tool_message_projection_uses_remaining_pool(tmp_path):
    store = ArtifactStore(tmp_path)
    policy = replace(
        ContextPolicy.from_env(None),
        context_object_mode="on",
        tool_msg_budget_use_remaining=True,
        large_result_threshold_chars=20,
    )
    engine = ContextEngine.create(
        model_name=None,
        user_id="u1",
        thread_id="t1",
        policy=policy,
        artifact_store=store,
    )
    middleware = ContextPruningMiddleware(
        model_name=None,
        user_id="u1",
        thread_id="t1",
        request_id="req-budget",
        policy=policy,
    )
    middleware._context_engine = engine
    middleware._context_ledger = engine.ledger
    middleware._capture_registry = engine.capture_registry
    middleware._context_object_mode = engine.context_object_mode
    middleware._object_store = engine.object_store
    middleware._projection_compiler = engine.projection_compiler
    middleware._segmenter_registry = engine.segmenter_registry

    def _request(tool_call_id: str, content: str) -> ToolCallRequest:
        return ToolCallRequest(
            tool_call={
                "name": "multi_kb_search",
                "id": tool_call_id,
                "args": {"query": "q"},
            },
            tool=None,
            state={"messages": [HumanMessage(content="q", id="turn-1")]},
            runtime=None,
        )

    def _artifact(content: str, chunk_id: str = "c11") -> dict:
        return {
            "evidence_records": [
                {
                    "source_id": "kb_378",
                    "chunk_id": chunk_id,
                    "doc_title": "修复技术",
                    "content": content,
                    "score": 0.83,
                }
            ]
        }

    first = middleware.wrap_tool_call(
        _request("call-1", "小正文"),
        lambda _request: ToolMessage(
            content="raw",
            name="multi_kb_search",
            tool_call_id="call-1",
            artifact=_artifact("小正文"),
        ),
    )
    assert isinstance(first, Command)
    ledger = middleware._capability_ledger
    assert ledger is not None
    # Leave only 5000 tokens for the second tool message.
    ledger._used_tokens = max(0, ledger.token_pool - 5000)

    big_content = "长正文。" * 4000
    second = middleware.wrap_tool_call(
        _request("call-2", big_content),
        lambda _request: ToolMessage(
            content="raw",
            name="multi_kb_search",
            tool_call_id="call-2",
            artifact=_artifact(big_content),
        ),
    )
    assert isinstance(second, Command)
    assert second.update["messages"][0].content.startswith("[Context Object:")
    assert ledger.remaining_tokens >= 0
    last_receipt = ledger.receipts[-1]
    assert last_receipt["tokens_spent"] <= 5000

    # Rollback path: with the env flag disabled the budget returns to 24000.
    middleware._policy = replace(
        middleware._policy,
        tool_msg_budget_use_remaining=False,
    )
    ledger._used_tokens = max(0, ledger.token_pool - 5000)
    other_content = "另一段长正文。" * 4000
    third = middleware.wrap_tool_call(
        _request("call-3", other_content),
        lambda _request: ToolMessage(
            content="raw",
            name="multi_kb_search",
            tool_call_id="call-3",
            artifact=_artifact(other_content, chunk_id="c12"),
        ),
    )
    assert isinstance(third, Command)
    assert ledger.receipts[-1]["tokens_spent"] > 5000
