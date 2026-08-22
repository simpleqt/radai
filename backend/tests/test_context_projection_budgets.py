"""投影预算与粘滞窗口:编译器保真阶梯、渲染预算、TopK 硬帽、粘滞溢出、已发布 SID 窗口与加权分配。(拆自 test_context_objects.py)"""

from dataclasses import replace

from langchain_core.messages import HumanMessage, SystemMessage

from backend.application.tools.providers.context_read_tool import context_read
from backend.core.agents.context.artifact_store import ArtifactStore
from backend.core.agents.context.capability_ledger import CapabilityLedger
from backend.core.agents.context.engine import ContextEngine
from backend.core.agents.context.object_store import ContextObjectStore
from backend.core.agents.context.objects import (
    make_context_object,
    make_segment,
)
from backend.core.agents.context.policy import ContextPolicy
from backend.core.agents.context.projection import ProjectionCompiler
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


def test_projection_compiler_fidelity_ladder_and_receipt():
    compiler = ProjectionCompiler()
    obj = make_context_object(
        schema="multi_kb_search/v1",
        origin={"tool": "multi_kb_search"},
        segments=_sample_segments(),
    )

    generous = compiler.project(
        [obj],
        handle="obj:aaaa",
        budget_tokens=100000,
        turn_id="t1",
    )
    assert generous.receipt["omitted_sids"] == []
    assert all(
        entry["fidelity"] in {"full", "compressed", "headline"}
        for entry in generous.entries
    )
    assert "<context_object" in generous.rendered
    assert (
        sum(entry["tokens"] for entry in generous.entries)
        + generous.receipt["overhead_tokens"]
        == generous.tokens_spent
    )

    tight = compiler.project(
        [obj],
        handle="obj:bbbb",
        budget_tokens=60,
        turn_id="t1",
    )
    # Headline is the hard floor only while it fits the budget; otherwise the
    # omitted list keeps every sid addressable.
    assert set(tight.receipt["omitted_sids"]) == {"kb_378:c11", "kb_378:c12"}
    assert "另有 2 段省略" in tight.rendered
    assert tight.tokens_spent <= 60

    again = compiler.project(
        [obj],
        handle="obj:aaaa",
        budget_tokens=100000,
        turn_id="t1",
    )
    assert again.rendered == generous.rendered


def test_compressed_rendering_fits_remaining_budget_and_is_shorter_than_full():
    compiler = ProjectionCompiler()
    text = "地下水污染迁移研究。" * 70  # 840 CJK chars
    obj = make_context_object(
        schema="text/v1",
        origin={"tool": "t"},
        segments=[make_segment(sid="a:1", text=text, score=1.0)],
    )

    full = compiler.project(
        [obj],
        handle="obj:full",
        budget_tokens=100000,
        turn_id="t1",
    )
    full_entry = next(entry for entry in full.entries if entry["fidelity"] == "full")

    # A budget between full and headline must produce a compressed block, not
    # silently fall back to headline.
    compressed_budget = max(64, full_entry["tokens"] // 2)
    compressed = compiler.project(
        [obj],
        handle="obj:comp",
        budget_tokens=compressed_budget,
        turn_id="t1",
    )
    compressed_entries = [
        entry for entry in compressed.entries if entry["fidelity"] == "compressed"
    ]
    assert compressed_entries
    assert compressed_entries[0]["tokens"] <= compressed_budget
    assert compressed_entries[0]["tokens"] < full_entry["tokens"]
    assert "正文" in compressed.rendered


def test_render_ledger_respects_hard_budget_and_row_cap():
    compiler = ProjectionCompiler()
    rows = [
        {
            "n": index + 1,
            "sid": f"kb:{index + 1}",
            "title": "文档",
            "locator": "",
            "score": 1.0 - index / 100,
            "fidelity": "omitted",
        }
        for index in range(100)
    ]

    tight_block, tight_cost = compiler.render_ledger(
        rows,
        budget_tokens=200,
        dynamic_budget=1000,
    )
    assert tight_cost <= 200
    assert "【证据台账】" in tight_block

    generous_block, generous_cost = compiler.render_ledger(
        rows,
        budget_tokens=100000,
        dynamic_budget=100000,
    )
    assert generous_cost <= 100000
    rendered_rows = generous_block.count("\n[")
    assert 20 <= rendered_rows <= 80


def test_projection_topk_respects_hard_cap_with_sticky():
    policy = replace(
        ContextPolicy.from_env(None),
        registration_top_k_segments=300,
        registration_top_k_objects=64,
    )
    middleware = ContextPruningMiddleware(model_name=None, policy=policy)
    objects = []
    for obj_index in range(70):
        segments = [
            make_segment(
                sid=f"o{obj_index}:s{seg_index}",
                title=f"obj{obj_index}",
                text="候选正文",
                score=0.01 if seg_index == 0 else float(seg_index),
            )
            for seg_index in range(10)
        ]
        objects.append(
            make_context_object(
                schema="text/v1",
                origin={"tool": "x"},
                segments=segments,
            )
        )
    # 20 sticky sids with the lowest possible score: they would rank out of the
    # top-K but must still be selected.
    sticky = {f"o{i}:s0" for i in range(20)}
    selected, weights, stats = middleware._select_projection_objects(
        objects,
        sticky=frozenset(sticky),
    )
    assert stats["total_segments"] == 700
    assert stats["selected_segments"] == 300
    assert stats["sticky_segments"] == 20
    assert stats["dropped_by_topk"] == 400
    assert stats["selected_objects"] <= 64
    selected_sids = {
        str(segment.get("sid") or "")
        for obj in selected
        for segment in obj.get("segments") or []
    }
    assert sticky <= selected_sids
    assert len(weights) == len(selected)
    assert all(weight >= 0 for weight in weights)


def test_projection_topk_hard_bound_with_sticky_overflow():
    policy = replace(
        ContextPolicy.from_env(None),
        registration_top_k_segments=300,
        registration_top_k_objects=64,
    )
    middleware = ContextPruningMiddleware(model_name=None, policy=policy)
    objects = []
    sticky = set()
    for obj_index in range(70):
        segments = []
        for seg_index in range(10):
            sid = f"o{obj_index}:s{seg_index}"
            # Make 400 of the 700 sids sticky with the lowest score so they
            # would all be outranked; sticky must still be trimmed to K.
            is_sticky = obj_index * 10 + seg_index < 400
            if is_sticky:
                sticky.add(sid)
            segments.append(
                make_segment(
                    sid=sid,
                    title=f"obj{obj_index}",
                    text="候选正文",
                    score=0.01 if is_sticky else float(seg_index),
                )
            )
        objects.append(
            make_context_object(
                schema="text/v1",
                origin={"tool": "x"},
                segments=segments,
            )
        )
    assert len(sticky) == 400
    selected, weights, stats = middleware._select_projection_objects(
        objects,
        sticky=frozenset(sticky),
    )
    selected_sids = {
        str(segment.get("sid") or "")
        for obj in selected
        for segment in obj.get("segments") or []
    }
    assert stats["selected_segments"] <= 300
    assert len(selected_sids) <= 300
    assert stats["sticky_segments"] <= 300
    assert stats["sticky_trimmed"] >= 100
    assert stats["selected_objects"] <= 64
    assert len(selected) <= 64
    assert len(weights) == len(selected)


def test_sticky_overflow_reserves_slots_for_new_evidence():
    policy = replace(
        ContextPolicy.from_env(None),
        registration_top_k_segments=3,
        registration_top_k_objects=8,
        registration_ranked_reserved_segments=1,
    )
    middleware = ContextPruningMiddleware(model_name=None, policy=policy)
    objects = [
        make_context_object(
            schema="text/v1",
            origin={"tool": "x"},
            segments=[
                make_segment(sid="s:low", text="低分 sticky", score=0.1),
                make_segment(sid="s:mid", text="中分 sticky", score=0.5),
                make_segment(sid="s:high", text="高分 sticky", score=0.9),
                make_segment(sid="s:higher", text="更高 sticky", score=0.95),
                make_segment(sid="s:fresh", text="新高分", score=0.99),
            ],
        )
    ]
    sticky = frozenset({"s:low", "s:mid", "s:high", "s:higher"})
    selected, _weights, stats = middleware._select_projection_objects(
        objects,
        sticky=sticky,
    )
    selected_sids = {
        str(segment.get("sid") or "")
        for obj in selected
        for segment in obj.get("segments") or []
    }
    assert stats["selected_segments"] == 3
    assert stats["sticky_budget"] == 2
    assert stats["ranked_reserved"] == 1
    assert stats["sticky_trimmed"] == 2
    # Highest-score sticky fill the sticky budget, and the reserved ranked slot
    # always lets new evidence enter even when sticky overflows.
    assert selected_sids == {"s:higher", "s:high", "s:fresh"}
    assert "s:low" not in selected_sids
    assert "s:mid" not in selected_sids


def test_sticky_overflow_without_reservation_prefers_higher_score_sticky():
    policy = replace(
        ContextPolicy.from_env(None),
        registration_top_k_segments=3,
        registration_top_k_objects=8,
        registration_ranked_reserved_segments=0,
    )
    middleware = ContextPruningMiddleware(model_name=None, policy=policy)
    objects = [
        make_context_object(
            schema="text/v1",
            origin={"tool": "x"},
            segments=[
                make_segment(sid="s:low", text="低分 sticky", score=0.1),
                make_segment(sid="s:mid", text="中分 sticky", score=0.5),
                make_segment(sid="s:high", text="高分 sticky", score=0.9),
                make_segment(sid="s:higher", text="更高 sticky", score=0.95),
                make_segment(sid="s:fresh", text="新高分", score=0.99),
            ],
        )
    ]
    selected, _weights, stats = middleware._select_projection_objects(
        objects,
        sticky=frozenset({"s:low", "s:mid", "s:high", "s:higher"}),
    )
    selected_sids = {
        str(segment.get("sid") or "")
        for obj in selected
        for segment in obj.get("segments") or []
    }
    assert stats["ranked_reserved"] == 0
    assert selected_sids == {"s:higher", "s:high", "s:mid"}
    assert "s:fresh" not in selected_sids


def test_published_sid_window_keeps_only_recent_renders():
    single = CapabilityLedger(sticky_window_calls=1)
    single.record_published_sids(["a:1", "a:2"])
    assert single.published_sids == frozenset({"a:1", "a:2"})
    single.record_published_sids(["b:1"])
    assert single.published_sids == frozenset({"b:1"})

    two = CapabilityLedger(sticky_window_calls=2)
    two.record_published_sids(["a:1"])
    two.record_published_sids(["b:1"])
    assert two.published_sids == frozenset({"a:1", "b:1"})
    two.record_published_sids(["c:1"])
    assert two.published_sids == frozenset({"b:1", "c:1"})


def test_published_sid_dropped_from_topk_still_readable(tmp_path):
    store = ContextObjectStore(ArtifactStore(tmp_path))
    old_obj = make_context_object(
        schema="text/v1",
        origin={"tool": "x", "user_id": "u1", "thread_id": "t1"},
        segments=[
            make_segment(sid="old:1", title="旧证据", text="旧证据正文", score=0.05)
        ],
    )
    old_obj["oid"] = store.put(old_obj)
    policy = replace(
        ContextPolicy.from_env(None),
        registration_top_k_segments=10,
        registration_top_k_objects=8,
    )
    middleware = ContextPruningMiddleware(model_name=None, policy=policy)
    ledger = CapabilityLedger(
        turn_id="turn-1",
        token_pool=100000,
        object_store=store,
        user_id="u1",
        thread_id="t1",
    )
    # The old sid was minted in an earlier projection (e.g. a tool message).
    ledger.register_objects([old_obj])
    new_objects = []
    for obj_index in range(12):
        new_objects.append(
            make_context_object(
                schema="text/v1",
                origin={"tool": "x", "user_id": "u1", "thread_id": "t1"},
                segments=[
                    make_segment(
                        sid=f"new:{obj_index}",
                        title="新证据",
                        text="新证据正文",
                        score=0.9,
                    )
                ],
            )
        )
    # No sticky set: the old sid is outranked and dropped from selection.
    selected, _weights, stats = middleware._select_projection_objects(
        [old_obj, *new_objects],
        sticky=frozenset(),
    )
    selected_sids = {
        str(segment.get("sid") or "")
        for obj in selected
        for segment in obj.get("segments") or []
    }
    assert "old:1" not in selected_sids
    assert stats["selected_segments"] <= 10
    # Authorization is append-only within the request, so the sid that was
    # visible earlier remains readable even after it leaves the top-K.
    assert ledger.resolve_sid("old:1") is not None
    from backend.application.tools.providers.context_read_tool import (
        reset_request_capability_ledger,
        set_request_capability_ledger,
    )

    token = set_request_capability_ledger("", ledger)
    try:
        result = context_read.invoke({"sids": ["old:1"], "max_tokens": 2000})
    finally:
        reset_request_capability_ledger(token, "")
    assert "旧证据正文" in result


def test_oversized_checkpoint_refs_converge_on_read(tmp_path, monkeypatch):
    monkeypatch.setenv("CONTEXT_OBJECT_REF_CAP", "10")
    store = ArtifactStore(tmp_path)
    policy = replace(
        ContextPolicy.from_env(None),
        context_object_mode="on",
        large_result_threshold_chars=20,
        registration_top_k_segments=300,
        registration_top_k_objects=64,
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
        request_id="req-converge",
        policy=policy,
    )
    middleware._context_engine = engine
    middleware._context_ledger = engine.ledger
    middleware._capture_registry = engine.capture_registry
    middleware._context_object_mode = engine.context_object_mode
    middleware._object_store = engine.object_store
    middleware._projection_compiler = engine.projection_compiler
    middleware._segmenter_registry = engine.segmenter_registry

    refs = []
    for index in range(25):
        obj = make_context_object(
            schema="text/v1",
            origin={
                "tool": "x",
                "user_id": "u1",
                "thread_id": "t1",
                "last_seen_at": float(index),
            },
            segments=[
                make_segment(
                    sid=f"old:{index}",
                    title="旧",
                    text="旧 checkpoint 正文",
                    score=0.5,
                )
            ],
        )
        oid = engine.object_store.put(obj)
        refs.append(
            {
                "oid": oid,
                "schema": obj["schema"],
                "origin": obj["origin"],
                "index": obj.get("index"),
            }
        )
    state = {
        "messages": [HumanMessage(content="q", id="turn-1")],
        "context_records": [],
        "context_objects": refs,
    }
    _messages, _system, _pruned, pack, _updates = middleware._compile_jit_request(
        SystemMessage(content="base"),
        state,
        messages=state["messages"],
    )
    assert pack.get("rendered")
    accounting = pack.get("context_accounting") or {}
    registry = accounting.get("registry") or {}
    assert registry["state_object_refs"] == 10
    # Only the 10 newest refs (by last_seen_at) are read from storage.
    assert registry["objects_read"] <= 10
    assert engine.object_store.stats()["objects_read"] <= 10
    assert registry["selected_segments"] <= 10
    from backend.core.agents.state import converge_context_object_refs

    converged = converge_context_object_refs(refs)
    assert len(converged) == 10
    kept_last_seen = [
        float((ref.get("origin") or {}).get("last_seen_at") or 0.0)
        for ref in converged
    ]
    assert kept_last_seen == [float(index) for index in range(15, 25)]


def test_weighted_projection_allocation_prefers_high_score_object(tmp_path):
    store = ContextObjectStore(ArtifactStore(tmp_path))
    high = make_context_object(
        schema="text/v1",
        origin={"tool": "x", "user_id": "u1", "thread_id": "t1"},
        segments=[
            make_segment(
                sid="h:1",
                title="高分",
                text="高分段正文 " * 60,
                score=0.95,
            )
        ],
    )
    low = make_context_object(
        schema="text/v1",
        origin={"tool": "x", "user_id": "u1", "thread_id": "t1"},
        segments=[
            make_segment(
                sid="l:1",
                title="低分",
                text="低分段正文 " * 60,
                score=0.2,
            )
        ],
    )
    high["oid"] = store.put(high)
    low["oid"] = store.put(low)
    ledger = CapabilityLedger(token_pool=100000, object_store=store)
    result = ledger.project(
        [low, high],
        budget_tokens=3000,
        budget_weights=[0.1, 0.9],
        reason="jit_pack",
    )
    assert result is not None
    assert result.tokens_spent <= 3000
    entries = {str(entry["sid"]): entry for entry in result.entries}
    assert entries["h:1"]["fidelity"] in {"full", "compressed"}
    assert entries["l:1"]["fidelity"] in {"headline", "omitted", "compressed"}
    assert entries["h:1"]["tokens"] >= entries["l:1"]["tokens"]
