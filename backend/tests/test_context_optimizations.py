"""上下文管理优化回归。

覆盖:
- 缓存友好渲染:<context_budget> 只保留请求内稳定属性,逐调用计数不进 prompt;
- CONTEXT_PACK_PLACEMENT=tail:system 前缀字节稳定,pack 作临时尾部消息且不入持久化;
- map-reduce 压缩摘要:长转写分块并行摘要,中段内容不再被头尾截断丢弃;
- 模型用量记账:网关 cached_tokens 进入 context_accounting.model_usage;
- 请求级激活分词缓存复用;
- 周期 GC 调度器:环境变量门控、单轮执行与失败隔离。
"""

import sqlite3
from dataclasses import replace
from types import SimpleNamespace

from langchain.agents.middleware.types import ModelRequest, ModelResponse
from langchain_core.messages import (
    AIMessage,
    HumanMessage,
    SystemMessage,
    ToolMessage,
)
from langgraph.prebuilt.tool_node import ToolCallRequest

from backend.core.agents.context import activation as activation_module
from backend.core.agents.context.activation import apply_activation_to_objects
from backend.core.agents.context.artifact_store import ArtifactStore
from backend.core.agents.context.engine import ContextEngine
from backend.core.agents.context.history_compactor import (
    HistoryCompactionAttempt,
    HistoryCompactionResult,
    HistoryCompactor,
)
from backend.core.agents.context.ledger import DYNAMIC_CONTEXT_HEADER
from backend.core.agents.context.object_store import ContextObjectStore
from backend.core.agents.context.objects import make_context_object, make_segment
from backend.core.agents.context.policy import ContextPolicy
from backend.core.agents.middlewares.context_pruning import ContextPruningMiddleware
from backend.services.context_maintenance import ContextMaintenanceScheduler


def _make_on_middleware(tmp_path, request_id, **policy_overrides):
    store = ArtifactStore(tmp_path)
    policy = replace(
        ContextPolicy.from_env(None),
        context_object_mode="on",
        large_result_threshold_chars=20,
        **policy_overrides,
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
        request_id=request_id,
        policy=policy,
    )
    middleware._context_engine = engine
    middleware._context_ledger = engine.ledger
    middleware._capture_registry = engine.capture_registry
    middleware._context_object_mode = engine.context_object_mode
    middleware._object_store = engine.object_store
    middleware._projection_compiler = engine.projection_compiler
    middleware._segmenter_registry = engine.segmenter_registry
    return middleware, policy


def _capture_kb_evidence(middleware):
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
    updates = result.update
    state = {
        "messages": [
            HumanMessage(content="3cm 砂层在哪", id="turn-1"),
            updates["messages"][0],
        ],
        "context_records": updates["context_records"],
        "context_objects": updates["context_objects"],
        "projection_receipts": updates["projection_receipts"],
    }
    return state


def _model_request(state, system_content="base-system"):
    return ModelRequest(
        model=SimpleNamespace(),
        messages=list(state["messages"]),
        system_message=SystemMessage(content=system_content),
        state=state,
        runtime=None,
    )


# ---------------------------------------------------------------------------
# 缓存友好渲染与投放位置
# ---------------------------------------------------------------------------


def test_default_system_placement_keeps_pack_in_system_without_counters(tmp_path):
    middleware, _policy = _make_on_middleware(tmp_path, "req-opt-sys")
    state = _capture_kb_evidence(middleware)

    captured = []
    response = middleware.wrap_model_call(
        _model_request(state),
        lambda patched: captured.append(patched)
        or ModelResponse(result=[AIMessage(content="done", id="a1")]),
    )

    patched = captured[0]
    system_content = str(patched.system_message.content or "")
    assert "<dynamic_context_pack>" in system_content
    assert "tokens_used=" not in system_content
    assert "base_tokens=" not in system_content
    assert 'token_budget="' in system_content
    # 默认投放:消息列表不追加临时 pack 消息。
    assert not any(
        isinstance(message, SystemMessage)
        and (getattr(message, "additional_kwargs", None) or {}).get(
            "context_pack_ephemeral"
        )
        for message in patched.messages
    )
    command = getattr(response, "command", None)
    assert command is not None
    assert command.update.get("context_pack") is not None


def test_tail_placement_keeps_system_stable_and_appends_ephemeral_pack(tmp_path):
    middleware, _policy = _make_on_middleware(
        tmp_path,
        "req-opt-tail",
        context_pack_placement="tail",
    )
    state = _capture_kb_evidence(middleware)

    captured = []
    response = middleware.wrap_model_call(
        _model_request(state),
        lambda patched: captured.append(patched)
        or ModelResponse(result=[AIMessage(content="done", id="a1")]),
    )

    patched = captured[0]
    # system 前缀保持字节稳定:不含动态 pack。
    assert str(patched.system_message.content or "") == "base-system"
    tail = patched.messages[-1]
    assert isinstance(tail, SystemMessage)
    assert (tail.additional_kwargs or {}).get("context_pack_ephemeral") is True
    assert "<dynamic_context_pack>" in str(tail.content)
    assert DYNAMIC_CONTEXT_HEADER.strip().splitlines()[0] in str(tail.content)
    # 临时 pack 消息绝不进入持久化回写。
    command = getattr(response, "command", None)
    assert command is not None
    model_response = response.model_response
    assert not any(
        (getattr(message, "additional_kwargs", None) or {}).get(
            "context_pack_ephemeral"
        )
        for message in model_response.result
    )


def test_on_mode_skips_legacy_record_rendering_when_objects_exist(tmp_path):
    middleware, _policy = _make_on_middleware(tmp_path, "req-opt-shortcut")
    state = _capture_kb_evidence(middleware)

    _messages, _system, _pruned, pack, _updates = middleware._compile_jit_request(
        SystemMessage(content="base"),
        state,
        messages=state["messages"],
    )

    # 投影是模型可见渲染;legacy 记录选择/渲染被短路(其输出必被替换)。
    assert "【证据台账】" in (pack.get("rendered") or "")
    assert int(pack.get("total_records") or 0) == 0
    assert int((pack.get("budget") or {}).get("items_injected") or 0) == 0


def test_on_mode_falls_back_to_legacy_render_without_objects(tmp_path):
    middleware, _policy = _make_on_middleware(tmp_path, "req-opt-fallback")
    # 角落场景:记录带 object_ref(状态适配器跳过提升)但 state 引用已被
    # CONTEXT_OBJECT_REF_CAP 逐出——对象集合为空,必须回退 legacy 渲染。
    state = {
        "messages": [HumanMessage(content="砂层多厚", id="turn-1")],
        "context_records": [
            {
                "id": "rec-legacy-1",
                "source_tool": "multi_kb_search",
                "category": "retrieval",
                "kind": "retrieval_evidence",
                "content": "遗留证据正文:砂层厚度 3cm。",
                "doc_title": "遗留文档",
                "score": 0.8,
                "object_ref": "f" * 64,
            }
        ],
        "context_objects": [],
    }

    _messages, _system, _pruned, pack, _updates = middleware._compile_jit_request(
        SystemMessage(content="base"),
        state,
        messages=state["messages"],
    )

    assert "遗留证据正文" in (pack.get("rendered") or "")
    assert int(pack.get("total_records") or 0) == 1


def test_jit_pack_render_is_byte_stable_across_calls_in_one_request(tmp_path):
    middleware, _policy = _make_on_middleware(tmp_path, "req-opt-stable")
    state = _capture_kb_evidence(middleware)

    _messages1, system1, _p1, pack1, _u1 = middleware._compile_jit_request(
        SystemMessage(content="base"),
        state,
        messages=state["messages"],
    )
    _messages2, system2, _p2, pack2, _u2 = middleware._compile_jit_request(
        SystemMessage(content="base"),
        state,
        messages=state["messages"],
    )
    assert pack1.get("rendered")
    assert pack1.get("rendered") == pack2.get("rendered")
    assert str(system1.content) == str(system2.content)


# ---------------------------------------------------------------------------
# map-reduce 压缩摘要
# ---------------------------------------------------------------------------


class _RecordingModel:
    def __init__(self):
        self.calls = []

    async def ainvoke(self, messages):
        self.calls.append(
            {
                "system": str(messages[0].content),
                "user": str(messages[1].content),
            }
        )
        return SimpleNamespace(content=f"摘要片段#{len(self.calls)}")


def _compactor(**kwargs):
    defaults = dict(
        model_name=None,
        trigger_tokens=1024,
        retained_window_tokens=1024,
        summary_input_chars=2000,
        summary_output_chars=800,
        timeout_seconds=10,
        summary_max_chunks=3,
    )
    defaults.update(kwargs)
    return HistoryCompactor(**defaults)


def test_partition_transcript_covers_every_region():
    transcript = "\n\n".join(
        f"[user]\n段落{index} " + "内" * 1200 for index in range(5)
    )
    chunks = HistoryCompactor._partition_transcript(transcript, 2000, 4)
    assert 1 < len(chunks) <= 4
    joined = "".join(chunks)
    for index in range(5):
        assert f"段落{index}" in joined


def test_partition_transcript_snaps_to_message_boundaries():
    marker = "中段唯一标记XYZ"
    transcript = (
        "[user]\n" + "A" * 1700 + "\n\n[assistant]\n" + marker + "B" * 1700
    )
    chunks = HistoryCompactor._partition_transcript(transcript, 2000, 3)
    assert len(chunks) == 2
    # 切点对齐消息分隔符:第二块从消息头开始,标记不被切开。
    assert chunks[1].startswith("[assistant]\n")
    assert any(marker in chunk for chunk in chunks)
    assert all(len(chunk) <= 2000 for chunk in chunks)


def test_partition_transcript_compresses_overlong_ranges():
    transcript = "C" * 30000
    chunks = HistoryCompactor._partition_transcript(transcript, 2000, 3)
    assert len(chunks) == 3
    assert all(len(chunk) <= 2000 for chunk in chunks)


async def test_no_savings_compaction_is_not_retried_for_unchanged_history(
    monkeypatch,
):
    """A fruitless attempt must not re-run the LLM summary every model call."""

    compactor = _compactor()
    summary_calls = {"count": 0}

    async def fake_summarize(messages):
        summary_calls["count"] += 1
        return "摘要"

    monkeypatch.setattr(compactor, "_summarize_pruned_messages", fake_summarize)
    # Force the no-savings outcome regardless of real token math.
    monkeypatch.setattr(compactor, "_build_result", lambda **kwargs: None)

    messages = []
    for index in range(3):
        messages.append(
            HumanMessage(content=f"问题{index}：" + "背景内容 " * 120, id=f"u{index}")
        )
        messages.append(
            AIMessage(content=f"回答{index}：" + "分析结论 " * 120, id=f"a{index}")
        )
    messages.append(HumanMessage(content="最新问题", id="u-final"))

    first = await compactor.acompact_attempt(messages)
    assert first.result is None
    assert first.skip_reason == "no_token_savings"
    assert summary_calls["count"] == 1

    second = await compactor.acompact_attempt(messages)
    assert second.result is None
    assert second.skip_reason == "no_token_savings_cooldown"
    assert summary_calls["count"] == 1

    # New history content invalidates the cooldown and allows a fresh attempt.
    grown = [*messages, HumanMessage(content="追加提问 " + "补充 " * 50, id="u-more")]
    third = await compactor.acompact_attempt(grown)
    assert third.skip_reason == "no_token_savings"
    assert summary_calls["count"] == 2


async def test_summary_only_prunable_history_skips_llm_resummarization(monkeypatch):
    """Re-summarizing nothing but the previous summary envelope is refused."""

    compactor = _compactor()

    async def fail_summarize(messages):
        raise AssertionError("LLM summary must not run for a summary-only window")

    monkeypatch.setattr(compactor, "_summarize_pruned_messages", fail_summarize)
    summary_message = HumanMessage(
        content=(
            "Another language model started to solve this problem and produced "
            "a summary of its work. Use this summary to continue without "
            "repeating prior steps:\n" + "旧窗口结论 " * 200
        ),
        id="compaction_summary_old",
        additional_kwargs={"context_compaction": {"window_id": "w-old"}},
    )
    messages = [
        summary_message,
        HumanMessage(content="当前问题 " + "上下文 " * 260, id="u-live"),
        AIMessage(content="当前回答 " + "内容 " * 260, id="a-live"),
    ]
    attempt = await compactor.acompact_attempt(
        messages,
        # Keep the live turn inside the retained window so only the old
        # summary envelope is prunable.
        threshold_tokens=1024,
    )
    assert attempt.result is None
    assert attempt.skip_reason in {"only_summary_prunable", "prunable_below_floor"}


async def test_map_reduce_summary_reaches_middle_of_long_history(monkeypatch):
    compactor = _compactor()
    model = _RecordingModel()

    async def fake_get_model():
        return model

    monkeypatch.setattr(compactor, "_get_model", fake_get_model)

    marker = "中段唯一标记XYZ"
    history = [
        HumanMessage(content="第一段问题 " + "甲" * 1800, id="u1"),
        AIMessage(content="中段结论:" + marker + " " + "乙" * 1800, id="a1"),
        HumanMessage(content="第三段追问 " + "丙" * 1800, id="u2"),
        AIMessage(content="第三段回答 " + "丁" * 500, id="a2"),
        HumanMessage(content="当前问题", id="u3"),
        AIMessage(content="当前回答", id="a3"),
    ]

    result = await compactor.acompact(history, threshold_tokens=1024)

    assert result is not None
    # map(N) + reduce(1):至少 2 次模型调用。
    assert len(model.calls) >= 3
    map_inputs = "\n".join(call["user"] for call in model.calls)
    assert marker in map_inputs
    reduce_calls = [
        call for call in model.calls if "<partial_summaries>" in call["user"]
    ]
    assert len(reduce_calls) == 1
    assert "摘要片段#" in result.summary


async def test_short_history_uses_single_summary_call(monkeypatch):
    compactor = _compactor()
    model = _RecordingModel()

    async def fake_get_model():
        return model

    monkeypatch.setattr(compactor, "_get_model", fake_get_model)

    history = [
        HumanMessage(content="旧问题 " + "甲" * 300, id="u1"),
        AIMessage(content="旧回答 " + "乙" * 300, id="a1"),
        HumanMessage(content="当前问题", id="u2"),
        AIMessage(content="当前回答", id="a2"),
    ]

    result = await compactor.acompact(history, threshold_tokens=1024)

    assert result is not None
    assert len(model.calls) == 1
    assert "<conversation>" in model.calls[0]["user"]


async def test_failed_chunk_falls_back_to_deterministic_excerpt(monkeypatch):
    compactor = _compactor()

    class FlakyModel:
        def __init__(self):
            self.calls = 0

        async def ainvoke(self, messages):
            self.calls += 1
            content = str(messages[1].content)
            if 'index="2"' in content:
                raise RuntimeError("chunk model failed")
            return SimpleNamespace(content="OK摘要")

    model = FlakyModel()

    async def fake_get_model():
        return model

    monkeypatch.setattr(compactor, "_get_model", fake_get_model)

    history = [
        HumanMessage(content="第一段 " + "甲" * 1900, id="u1"),
        AIMessage(content="第二段坐标NAB123 " + "乙" * 1900, id="a1"),
        HumanMessage(content="第三段 " + "丙" * 1900, id="u2"),
        HumanMessage(content="当前问题", id="u3"),
        AIMessage(content="当前回答", id="a3"),
    ]

    result = await compactor.acompact(history, threshold_tokens=1024)

    # 单块失败不放弃整体摘要:失败块退化为确定性摘录参与 reduce。
    assert result is not None
    assert result.summary


# ---------------------------------------------------------------------------
# 模型用量记账(prefix cache 命中可观测)
# ---------------------------------------------------------------------------


def test_cached_input_tokens_recorded_into_accounting(tmp_path):
    middleware, _policy = _make_on_middleware(tmp_path, "req-opt-usage")
    response = ModelResponse(
        result=[
            AIMessage(
                content="done",
                id="a1",
                usage_metadata={
                    "input_tokens": 1200,
                    "output_tokens": 20,
                    "total_tokens": 1220,
                    "input_token_details": {"cache_read": 900},
                },
            )
        ]
    )

    finalized = middleware._finalize_model_call_result(
        response,
        None,
        [],
        {"context_pack": {"rendered": ""}},
    )

    command = getattr(finalized, "command", None)
    assert command is not None
    usage = command.update["context_accounting"]["model_usage"]
    assert usage == {"input_tokens": 1200, "cached_input_tokens": 900}


def test_openai_style_cached_tokens_shape_is_read():
    response = ModelResponse(
        result=[
            AIMessage(
                content="done",
                id="a1",
                response_metadata={
                    "token_usage": {
                        "prompt_tokens": 800,
                        "prompt_tokens_details": {"cached_tokens": 512},
                    }
                },
            )
        ]
    )
    assert ContextPruningMiddleware._response_cached_input_tokens(response) == 512
    assert ContextPruningMiddleware._response_input_tokens(response) == 800


# ---------------------------------------------------------------------------
# 请求级激活分词缓存
# ---------------------------------------------------------------------------


def test_activation_reuses_request_scoped_term_cache(monkeypatch):
    calls = []
    original = activation_module._simple_terms

    def counting_terms(text):
        calls.append(str(text)[:24])
        return original(text)

    monkeypatch.setattr(activation_module, "_simple_terms", counting_terms)

    obj = make_context_object(
        schema="kb/v1",
        origin={"turn_id": "turn-1"},
        index={"query": "砂层结构"},
        segments=[
            make_segment(
                sid="kb:seg-1",
                title="砂层",
                text="砂层厚度 3cm,黏土层 9cm。",
                score=0.8,
            )
        ],
    )
    shared_cache: dict = {}
    messages = [HumanMessage(content="砂层多厚", id="turn-1")]

    apply_activation_to_objects(
        [obj],
        current_query="砂层多厚",
        messages=messages,
        active_turn_id="turn-1",
        term_cache=shared_cache,
    )
    first_round_calls = len(calls)
    assert first_round_calls > 0
    assert shared_cache

    apply_activation_to_objects(
        [obj],
        current_query="砂层多厚",
        messages=messages,
        active_turn_id="turn-1",
        term_cache=shared_cache,
    )
    assert len(calls) == first_round_calls  # 第二次投影零分词


# ---------------------------------------------------------------------------
# COS 并发预取
# ---------------------------------------------------------------------------


def _put_sample_object(store: ContextObjectStore, index: int) -> str:
    return store.put(
        make_context_object(
            schema="kb/v1",
            origin={"user_id": "u1", "thread_id": "t1"},
            index={"query": f"q{index}"},
            segments=[
                make_segment(
                    sid=f"kb:s{index}",
                    title=f"标题{index}",
                    text=f"正文{index} " * 30,
                    score=0.5,
                )
            ],
        )
    )


def test_read_many_prefetches_misses_and_backfills_cache(tmp_path):
    writer = ContextObjectStore(ArtifactStore(tmp_path))
    oids = [_put_sample_object(writer, index) for index in range(3)]

    # 新实例模拟新请求:读缓存为空,全部走并发预取。
    fresh = ContextObjectStore(writer.artifact_store)
    results = fresh.read_many([*oids, "0" * 64, oids[0]])

    assert set(results) == set(oids)
    assert all(results[oid]["oid"] == oid for oid in oids)
    stats = fresh.stats()
    assert stats["objects_read"] == 4  # 3 命中 + 1 不存在,都计一次读

    # 第二次全部命中请求级缓存,不再触发存储读取。
    again = fresh.read_many(oids)
    assert set(again) == set(oids)
    stats = fresh.stats()
    assert stats["objects_read"] == 4
    assert stats["object_cache_hits"] >= 3


def test_read_many_result_matches_single_read(tmp_path):
    writer = ContextObjectStore(ArtifactStore(tmp_path))
    oid = _put_sample_object(writer, 7)
    via_many = ContextObjectStore(writer.artifact_store).read_many([oid])[oid]
    via_single = ContextObjectStore(writer.artifact_store).read(oid)
    assert via_many == via_single


# ---------------------------------------------------------------------------
# 早触发压缩(非首轮模型调用)
# ---------------------------------------------------------------------------


def test_early_compaction_threshold_applies_from_second_round(tmp_path):
    middleware, policy = _make_on_middleware(tmp_path, "req-opt-early")
    trigger = middleware._history_compactor.trigger_tokens

    # 首轮(本请求还没有模型调用):保持全额触发。
    assert middleware._request_model_calls == 0
    assert middleware._early_compaction_threshold() is None

    middleware._request_model_calls = 2
    early = middleware._early_compaction_threshold()
    assert early == max(1024, int(trigger * policy.compaction_early_ratio))
    assert early < trigger


def test_early_compaction_disabled_at_ratio_one(tmp_path):
    middleware, _policy = _make_on_middleware(
        tmp_path,
        "req-opt-early-off",
        compaction_early_ratio=1.0,
    )
    middleware._request_model_calls = 3
    assert middleware._early_compaction_threshold() is None


def test_before_model_passes_early_threshold_to_compactor(tmp_path, monkeypatch):
    middleware, policy = _make_on_middleware(tmp_path, "req-opt-early-wire")
    captured: dict = {}

    def fake_attempt(
        messages,
        *,
        previous_window_id=None,
        trigger="auto",
        additional_tokens=0,
        threshold_tokens=None,
    ):
        captured["threshold"] = threshold_tokens
        return HistoryCompactionAttempt(
            trigger=trigger,
            tokens_before=0,
            threshold_tokens=int(threshold_tokens or 0),
            fixed_request_tokens=0,
            previous_window_id=previous_window_id,
            skip_reason="below_trigger",
        )

    monkeypatch.setattr(
        middleware._history_compactor,
        "compact_deterministic_attempt",
        fake_attempt,
    )
    state = {"messages": [HumanMessage(content="q", id="turn-1")]}

    middleware.before_model(state, None)
    assert captured["threshold"] is None

    middleware._request_model_calls = 1
    middleware.before_model(state, None)
    trigger = middleware._history_compactor.trigger_tokens
    assert captured["threshold"] == max(
        1024, int(trigger * policy.compaction_early_ratio)
    )


# ---------------------------------------------------------------------------
# 主路径持久会话记忆
# ---------------------------------------------------------------------------


def _fake_memory() -> dict:
    return {
        "version": 1,
        "summary": "用户在分析砂层扩散实验。",
        "key_entities": ["砂层", "黏土层"],
        "user_preferences": ["用户偏好: 回答使用中文并引用文献编号"],
        "confirmed_facts": ["已确认: 砂层厚度 3cm"],
        "open_questions": [],
    }


async def test_session_memory_block_injected_into_system_prefix(
    tmp_path, monkeypatch
):
    from backend.application.conversations.session_memory import (
        ConversationMemoryStore,
    )

    middleware, _policy = _make_on_middleware(tmp_path, "req-opt-mem")
    loads: list[dict] = []

    async def fake_load(self, *, user_id, thread_id):
        loads.append({"user_id": user_id, "thread_id": thread_id})
        return _fake_memory()

    monkeypatch.setattr(ConversationMemoryStore, "load", fake_load)

    await middleware._ensure_session_memory_loaded()
    await middleware._ensure_session_memory_loaded()  # 每请求只加载一次
    assert len(loads) == 1
    assert loads[0] == {"user_id": "u1", "thread_id": "t1"}

    state = _capture_kb_evidence(middleware)
    _messages, system, _pruned, _pack, _updates = middleware._compile_jit_request(
        SystemMessage(content="base-system"),
        state,
        messages=state["messages"],
    )
    content = str(system.content)
    assert "<persistent_session_memory>" in content
    assert "[持久会话摘要]" in content
    assert "砂层厚度 3cm" in content
    # 记忆属于稳定前缀:位于动态 pack 之前。
    assert content.index("<persistent_session_memory>") < content.index(
        "<dynamic_context_pack>"
    )


async def test_session_memory_disabled_skips_load(tmp_path, monkeypatch):
    from backend.application.conversations.session_memory import (
        ConversationMemoryStore,
    )

    middleware, _policy = _make_on_middleware(
        tmp_path,
        "req-opt-mem-off",
        session_memory_enabled=False,
    )
    loads: list[str] = []

    async def fake_load(self, *, user_id, thread_id):
        loads.append(thread_id)
        return _fake_memory()

    monkeypatch.setattr(ConversationMemoryStore, "load", fake_load)

    await middleware._ensure_session_memory_loaded()

    assert loads == []
    assert middleware._session_memory_block() == ""


def _compaction_result(summary: str) -> HistoryCompactionResult:
    import time as _time

    return HistoryCompactionResult(
        window_id="ctxwin_mem_test",
        previous_window_id=None,
        summary=summary,
        messages=[],
        tokens_before=9000,
        tokens_after=2000,
        retained_user_messages=1,
        removed_messages=6,
        summary_tokens=300,
        fixed_request_tokens=0,
        threshold_tokens=4096,
        trigger="auto",
        created_at=_time.time(),
    )


async def test_compaction_summary_is_persisted_into_agent_memory(
    tmp_path, monkeypatch
):
    """持久化走事务内合并:回调收到行内当前值,合并不得丢既有条目。"""

    from backend.application.conversations import (
        session_memory as session_memory_module,
    )
    from backend.core.agents.context.summary import SUMMARY_PREFIX

    middleware, _policy = _make_on_middleware(tmp_path, "req-opt-mem-save")
    saved: list[dict] = []

    class _FakeCatalog:
        async def merge_metadata_value_with(self, *, user_id, thread_id, key, merge):
            # 模拟行事务:merge 回调基于"锁内读到的当前值"计算合并结果。
            previous = {"confirmed_facts": ["已确认: 旧事实保留。"]}
            saved.append(
                {
                    "user_id": user_id,
                    "thread_id": thread_id,
                    "key": key,
                    "memory": merge(previous),
                }
            )
            return True

    monkeypatch.setattr(
        session_memory_module,
        "get_local_conversation_catalog",
        lambda: _FakeCatalog(),
    )

    result = _compaction_result(
        f"{SUMMARY_PREFIX}\n已确认: 砂层厚度 3cm。\n用户偏好: 回答使用中文。"
    )
    middleware._persist_session_memory(result)
    assert middleware._session_memory_tasks
    import asyncio as _asyncio

    await _asyncio.gather(*middleware._session_memory_tasks)

    assert len(saved) == 1
    assert saved[0]["key"] == "agent_memory"
    memory = saved[0]["memory"]
    # 摘要前缀信封被剥离,结构化字段被提炼。
    assert SUMMARY_PREFIX not in memory["summary"]
    assert "砂层厚度 3cm" in memory["summary"]
    assert any("用户偏好" in item for item in memory["user_preferences"])
    assert any("已确认" in item for item in memory["confirmed_facts"])
    # 事务内读到的既有条目在合并结果中保留(load-merge-save 竞态的回归)。
    assert any("旧事实保留" in item for item in memory["confirmed_facts"])
    assert memory["last_compressed_message_count"] == 6
    assert memory["last_compressed_message_id"] == "ctxwin_mem_test"


async def test_apply_compaction_result_triggers_memory_persistence(tmp_path):
    middleware, _policy = _make_on_middleware(tmp_path, "req-opt-mem-wire")
    captured: list = []
    middleware._persist_session_memory = captured.append

    result = _compaction_result("摘要正文")
    state = {"messages": [HumanMessage(content="q", id="turn-1")]}
    middleware._apply_compaction_result(state, {}, result)

    assert captured == [result]


def test_web_search_error_payload_keeps_raw_tool_message(tmp_path):
    """空段对象不得覆盖原文:错误与熔断指令必须原样可见。

    web_search 的错误/熔断载荷分段为 0 段,修复前空对象的摘要投影仍渲染
    "共 0 段证据"非空文本并替换原文,模型收不到"停止联网"指令会继续重试。
    """

    middleware, _policy = _make_on_middleware(tmp_path, "req-opt-websearch-err")
    error_payload = (
        '{"error": "连续 2 次联网检索未获得相关结果，已停止联网。'
        '不要再次调用 web_search。"}'
    )
    request = ToolCallRequest(
        tool_call={
            "name": "web_search",
            "id": "call-ws-err",
            "args": {"query": "某个查询"},
        },
        tool=None,
        state={"messages": [HumanMessage(content="q", id="turn-1")]},
        runtime=None,
    )
    result = middleware.wrap_tool_call(
        request,
        lambda _request: ToolMessage(
            content=error_payload,
            name="web_search",
            tool_call_id="call-ws-err",
        ),
    )
    if hasattr(result, "update"):
        content = str(result.update["messages"][0].content)
    else:
        content = str(result.content)
    assert "不要再次调用 web_search" in content
    assert "[Context Object:" not in content
    assert "共 0 段证据" not in content


# ---------------------------------------------------------------------------
# 历史序列化占位前缀对齐
# ---------------------------------------------------------------------------


def test_history_serializer_hides_all_internal_placeholders():
    from backend.application.conversations.message_serializer import (
        SUMMARY_MARKERS,
        serialize_checkpoint_messages,
    )
    from backend.core.agents.context.summary import CAPTURED_CONTENT_PREFIXES

    # 单一来源:序列化过滤清单必须覆盖中间件的全部占位前缀。
    for prefix in CAPTURED_CONTENT_PREFIXES:
        assert prefix in SUMMARY_MARKERS

    messages = [
        {"type": "human", "content": "问题"},
        {"type": "tool", "content": "[Context Object: 工具结果已按投影编译注入。]"},
        {"type": "tool", "content": "[Result Summarizer Handoff: ...]"},
        {"type": "ai", "content": "回答"},
    ]
    serialized = serialize_checkpoint_messages(messages)
    assert [item["role"] for item in serialized] == ["human", "ai"]


# ---------------------------------------------------------------------------
# 会话目录初始化缓存
# ---------------------------------------------------------------------------


async def test_local_catalog_initializes_schema_once(tmp_path, monkeypatch):
    from backend.application.conversations import local_catalog as catalog_module

    catalog_module.reset_local_catalog_initialization()
    catalog = catalog_module.SQLiteConversationCatalog(tmp_path / "conv.db")
    init_calls: list[str] = []
    original_initialize = catalog_module._initialize_catalog_path

    def counting_initialize(path):
        init_calls.append(str(path))
        original_initialize(path)

    monkeypatch.setattr(
        catalog_module,
        "_initialize_catalog_path",
        counting_initialize,
    )

    await catalog.rename(user_id="u1", thread_id="t1", title="标题")
    await catalog.touch(user_id="u1", thread_id="t1")
    record = await catalog.get(user_id="u1", thread_id="t1")

    assert record is not None and record.title == "标题"
    # 目录/权限/WAL/建表的一次性初始化只在首次操作执行。
    assert init_calls == [str(catalog.path)]
    catalog_module.reset_local_catalog_initialization()


# ---------------------------------------------------------------------------
# Guest 历史 has_messages 单调缓存
# ---------------------------------------------------------------------------


async def test_guest_has_messages_cache_short_circuits_checkpoint_read():
    from backend.application.checkpointing import guest_fallback

    guest_fallback.reset_known_message_threads()
    try:
        guest_fallback._remember_message_threads(["thread-known"])
        # 命中缓存时完全不接触 checkpointer(request=None 若被使用会立即报错)。
        assert await guest_fallback.has_checkpoint_messages(None, "thread-known")
        assert guest_fallback._cached_message_threads(
            ["thread-known", "thread-unknown"]
        ) == {"thread-known"}
    finally:
        guest_fallback.reset_known_message_threads()


def test_guest_has_messages_cache_is_bounded(monkeypatch):
    from backend.application.checkpointing import guest_fallback

    guest_fallback.reset_known_message_threads()
    monkeypatch.setattr(guest_fallback, "_KNOWN_MESSAGE_THREADS_MAX", 3)
    try:
        guest_fallback._remember_message_threads(
            [f"thread-{index}" for index in range(6)]
        )
        cached = guest_fallback._cached_message_threads(
            [f"thread-{index}" for index in range(6)]
        )
        assert cached == {"thread-3", "thread-4", "thread-5"}
    finally:
        guest_fallback.reset_known_message_threads()


_CHECKPOINT_SCHEMA = """
CREATE TABLE IF NOT EXISTS checkpoints (
    thread_id TEXT NOT NULL,
    checkpoint_ns TEXT NOT NULL DEFAULT '',
    checkpoint_id TEXT NOT NULL,
    parent_checkpoint_id TEXT,
    type TEXT,
    checkpoint BLOB,
    metadata BLOB,
    PRIMARY KEY (thread_id, checkpoint_ns, checkpoint_id)
);
"""


def _make_checkpoint_db(tmp_path, *, checkpoints: int):
    db_path = tmp_path / "checkpoints.db"
    conn = sqlite3.connect(str(db_path))
    conn.executescript(_CHECKPOINT_SCHEMA)
    with conn:
        for index in range(checkpoints):
            conn.execute(
                "INSERT INTO checkpoints VALUES (?, '', ?, NULL, 'msgpack', x'00', x'00')",
                ("thread-1", f"1ef00000-0000-6000-8000-{index:012d}"),
            )
    conn.close()
    return db_path


def test_scheduler_disabled_by_default(monkeypatch):
    monkeypatch.delenv("CONTEXT_MAINTENANCE_GC_INTERVAL_SECONDS", raising=False)
    scheduler = ContextMaintenanceScheduler()
    assert scheduler.enabled is False
    assert scheduler.interval_seconds == 0


def test_scheduler_interval_is_bounded(monkeypatch):
    monkeypatch.setenv("CONTEXT_MAINTENANCE_GC_INTERVAL_SECONDS", "60")
    scheduler = ContextMaintenanceScheduler()
    assert scheduler.enabled is True
    assert scheduler.interval_seconds == 300


async def test_scheduler_start_is_noop_when_disabled(monkeypatch):
    monkeypatch.setenv("CONTEXT_MAINTENANCE_GC_INTERVAL_SECONDS", "0")
    scheduler = ContextMaintenanceScheduler()
    await scheduler.start()
    assert scheduler._task is None
    await scheduler.stop()


async def test_scheduler_run_once_trims_checkpoints_and_contains_failures(
    tmp_path, monkeypatch
):
    db_path = _make_checkpoint_db(tmp_path, checkpoints=7)
    monkeypatch.setenv("AGENT_CHECKPOINT_DATA_DIR", str(tmp_path))
    monkeypatch.setenv("CHECKPOINT_GC_KEEP_PER_THREAD", "2")
    monkeypatch.setattr(
        ContextMaintenanceScheduler,
        "_gc_context_objects",
        staticmethod(lambda: {"scanned": 0, "kept": 0, "removed_count": 0}),
    )

    scheduler = ContextMaintenanceScheduler()
    report = await scheduler.run_once()

    assert report["checkpoints"]["checkpoints_deleted"] == 5
    conn = sqlite3.connect(str(db_path))
    remaining = conn.execute("SELECT COUNT(*) FROM checkpoints").fetchone()[0]
    conn.close()
    assert remaining == 2

    # 失败隔离:checkpoint GC 抛异常时 COS GC 仍执行,报告不抛出。
    def broken_gc():
        raise RuntimeError("boom")

    monkeypatch.setattr(
        ContextMaintenanceScheduler,
        "_gc_checkpoints",
        staticmethod(broken_gc),
    )
    report = await scheduler.run_once()
    assert report["checkpoints"] == {"error": "failed"}
    assert report["context_objects"]["scanned"] == 0


async def test_scheduler_run_once_skips_missing_db(tmp_path, monkeypatch):
    monkeypatch.setenv("AGENT_CHECKPOINT_DATA_DIR", str(tmp_path / "nonexistent"))
    monkeypatch.setattr(
        ContextMaintenanceScheduler,
        "_gc_context_objects",
        staticmethod(lambda: {"scanned": 0, "kept": 0, "removed_count": 0}),
    )
    scheduler = ContextMaintenanceScheduler()
    report = await scheduler.run_once()
    assert report["checkpoints"]["skipped"] == "db_missing"
