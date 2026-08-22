"""Unit and integration coverage for multi-turn activation scoring."""

from __future__ import annotations

from dataclasses import replace

from langchain_core.messages import HumanMessage

from backend.core.agents.context.activation import (
    apply_activation_to_objects,
    compute_activation,
    count_age_turns,
    list_regular_user_turn_ids,
)
from backend.core.agents.context.artifact_store import ArtifactStore
from backend.core.agents.context.capability_ledger import CapabilityLedger
from backend.core.agents.context.object_store import ContextObjectStore
from backend.core.agents.context.objects import (
    ContextObject,
    make_context_object,
    make_segment,
)
from backend.core.agents.context.policy import ContextPolicy
from backend.core.agents.context.projection import ProjectionCompiler


def _messages(*turn_ids: str) -> list[HumanMessage]:
    return [
        HumanMessage(content=f"问题 {index}", id=turn_id)
        for index, turn_id in enumerate(turn_ids)
    ]


def _object(
    *,
    sid: str,
    text: str,
    score: float,
    turn_id: str = "",
    query: str = "",
) -> ContextObject:
    return make_context_object(
        schema="multi_kb_search/v1",
        origin={"tool": "multi_kb_search", "turn_id": turn_id},
        index={"query": query},
        segments=[make_segment(sid=sid, title=sid, text=text, score=score)],
    )


def _enabled_policy() -> ContextPolicy:
    return replace(ContextPolicy.from_env(None), activation_scoring=True)


def test_regular_user_turn_ids_exclude_compaction_summary():
    messages = [
        HumanMessage(
            content="【历史摘要】旧内容",
            id="summary-1",
            additional_kwargs={"context_compaction": {"window_id": "w1"}},
        ),
        HumanMessage(content="本轮问题", id="turn-3"),
    ]
    assert list_regular_user_turn_ids(messages) == ["turn-3"]


def test_same_turn_has_no_decay_and_keeps_turn_boost():
    breakdown = compute_activation(
        retrieval_score=0.9,
        segment_text="修复技术选型与适用场景",
        segment_title="修复",
        origin_query="修复技术",
        current_query="修复技术选型",
        origin_turn_id="t0",
        active_turn_id="t0",
        messages=_messages("t0"),
    )
    assert breakdown.age_turns == 0
    assert breakdown.recency_decay == 1.0
    assert breakdown.turn_boost == 1.25
    assert breakdown.activation > breakdown.relevance
    assert breakdown.activation > 1.0  # 不 clamp（R5），仅排序/展示口径


def test_query_overlap_dominates_ordering():
    messages = _messages("t0")
    matched = compute_activation(
        retrieval_score=0.8,
        segment_text="热脱附温度与停留时间工艺参数",
        segment_title="热脱附",
        origin_query="热脱附",
        current_query="热脱附工艺参数",
        origin_turn_id="t0",
        active_turn_id="t0",
        messages=messages,
    )
    unrelated = compute_activation(
        retrieval_score=0.8,
        segment_text="完全无关的条款内容",
        segment_title="无关",
        origin_query="无关",
        current_query="热脱附工艺参数",
        origin_turn_id="t0",
        active_turn_id="t0",
        messages=messages,
    )
    assert matched.activation > unrelated.activation


def test_cross_turn_halflife_decay():
    breakdown = compute_activation(
        retrieval_score=0.9,
        segment_text="文本",
        segment_title="标题",
        origin_query="q",
        current_query="q",
        origin_turn_id="t0",
        active_turn_id="t2",
        messages=_messages("t0", "t1", "t2"),
    )
    assert breakdown.age_turns == 2
    assert round(breakdown.recency_decay, 4) == 0.5
    assert breakdown.turn_boost == 1.0


def test_created_at_never_enters_activation():
    policy = _enabled_policy()
    first = _object(
        sid="a:1",
        text="修复技术选型",
        score=0.9,
        turn_id="t0",
        query="修复",
    )
    first["origin"]["created_at"] = 100.0
    second = _object(
        sid="a:2",
        text="修复技术选型",
        score=0.9,
        turn_id="t0",
        query="修复",
    )
    second["origin"]["created_at"] = 999999.0
    messages = _messages("t0")
    activated = apply_activation_to_objects(
        [first, second],
        current_query="修复技术选型",
        messages=messages,
        active_turn_id="t0",
        policy=policy,
    )
    first_seg = activated[0]["segments"][0]
    second_seg = activated[1]["segments"][0]
    assert first_seg["_activation"] == second_seg["_activation"]


def test_missing_turn_id_uses_missing_turn_age_without_wall_clock():
    breakdown = compute_activation(
        retrieval_score=0.9,
        segment_text="文本",
        segment_title="标题",
        origin_query="q",
        current_query="q",
        origin_turn_id="",
        active_turn_id="t0",
        messages=_messages("t0"),
    )
    assert breakdown.age_turns == 3
    assert breakdown.missing_turn_id is True
    assert round(breakdown.recency_decay, 4) == 0.3536


def test_active_turn_unresolved_falls_back_instead_of_zero():
    breakdown = compute_activation(
        retrieval_score=0.9,
        segment_text="文本",
        segment_title="标题",
        origin_query="q",
        current_query="q",
        origin_turn_id="t0",
        active_turn_id="ghost-turn",
        messages=_messages("t0"),
    )
    assert breakdown.active_turn_unresolved is True
    assert breakdown.age_turns == 3
    assert breakdown.recency_decay < 1.0


def test_fresh_object_missing_turn_id_is_treated_as_current():
    policy = _enabled_policy()
    obj = _object(sid="a:1", text="修复技术选型", score=0.9, turn_id="")
    activated = apply_activation_to_objects(
        [obj],
        current_query="修复技术选型",
        messages=_messages("t0"),
        active_turn_id="t0",
        fresh_oids={obj["oid"]},
        policy=policy,
    )
    segment = activated[0]["segments"][0]
    assert segment["_activation_age_turns"] == 0
    assert segment["_activation_turn_boost"] == 1.25
    assert segment["_activation_missing_turn_id"] is False


def test_fresh_oid_with_old_turn_id_does_not_get_turn_boost():
    """R3: fresh set only rescues missing turn_id; it must not boost aged evidence."""

    policy = _enabled_policy()
    obj = _object(sid="a:1", text="旧标准限值", score=0.9, turn_id="t0")
    activated = apply_activation_to_objects(
        [obj],
        current_query="旧标准限值",
        messages=_messages("t0", "t1", "t2"),
        active_turn_id="t2",
        fresh_oids={obj["oid"]},
        policy=policy,
    )
    segment = activated[0]["segments"][0]
    assert segment["_activation_age_turns"] == 2
    assert segment["_activation_turn_boost"] == 1.0
    assert segment["_activation_recency_decay"] < 1.0


def test_zero_relevance_still_blocks_full_upgrade():
    """``relevance=0.0`` must not be treated as missing via ``x or 1.0``."""

    from backend.core.agents.context.activation import (
        KEY_OMIT_THRESHOLD,
        KEY_RELEVANCE,
    )

    segment = {
        "sid": "zero:1",
        "title": "无关",
        "text": "完全无关的正文内容 " * 40,
        "score": 0.9,
        KEY_RELEVANCE: 0.0,
        KEY_OMIT_THRESHOLD: 0.08,
    }
    obj = {
        "oid": "a" * 64,
        "schema": "text/v1",
        "origin": {},
        "index": {},
        "segments": [segment],
    }
    result = ProjectionCompiler().project(
        [obj],
        handle="obj:test",
        budget_tokens=100000,
    )
    assert result.entries[0]["fidelity"] == "headline"


def test_compaction_removes_old_turn_ids_and_falls_back_bounded():
    # 压缩后 messages 只剩保留窗（t5..t8）；t0 的真实 age=8，但只能有界回退。
    messages = _messages("t5", "t6", "t7", "t8")
    age, missing, _unresolved = count_age_turns(
        origin_turn_id="t0",
        active_turn_id="t8",
        messages=messages,
        missing_turn_age=3.0,
    )
    assert missing is True
    assert age == 3
    assert age != 8


def test_old_highly_relevant_evidence_survives_relevance_threshold():
    # age=8、overlap=1.0：activation 低于 0.08，但 relevance 高于阈值（R4）。
    messages = _messages(*[f"t{i}" for i in range(9)])
    breakdown = compute_activation(
        retrieval_score=0.95,
        segment_text="修复技术选型对比",
        segment_title="修复技术选型",
        origin_query="修复技术选型",
        current_query="修复技术选型",
        origin_turn_id="t0",
        active_turn_id="t8",
        messages=messages,
    )
    assert breakdown.age_turns == 8
    assert breakdown.relevance > 0.9
    assert breakdown.activation < 0.08


def test_projection_low_relevance_skips_full_upgrade():
    policy = _enabled_policy()
    obj = make_context_object(
        schema="multi_kb_search/v1",
        origin={"tool": "multi_kb_search", "turn_id": "t0"},
        segments=[
            make_segment(
                sid="low:1",
                title="低相关",
                text="与问题完全无关",
                score=0.2,
            ),
            make_segment(
                sid="high:1",
                title="热脱附",
                text="热脱附工艺参数与温度",
                score=0.8,
            ),
        ],
    )
    activated = apply_activation_to_objects(
        [obj],
        current_query="热脱附工艺参数",
        messages=_messages("t0"),
        active_turn_id="t0",
        policy=policy,
    )
    compiler = ProjectionCompiler()
    result = compiler.project(
        activated,
        handle="obj:test",
        budget_tokens=100000,
        turn_id="t0",
    )
    entries = {entry["sid"]: entry["fidelity"] for entry in result.entries}
    assert entries["low:1"] == "headline"
    assert entries["high:1"] == "full"

    # 关闭激活分时两个都允许 full（回归基线）。
    plain = compiler.project(
        [obj],
        handle="obj:test",
        budget_tokens=100000,
        turn_id="t0",
    )
    plain_entries = {entry["sid"]: entry["fidelity"] for entry in plain.entries}
    assert plain_entries["low:1"] == "full"
    assert plain_entries["high:1"] == "full"


def test_activation_off_returns_original_objects():
    policy = replace(ContextPolicy.from_env(None), activation_scoring=False)
    obj = _object(sid="a:1", text="文本", score=0.85, turn_id="t0")
    activated = apply_activation_to_objects(
        [obj],
        current_query="q",
        messages=_messages("t0"),
        active_turn_id="t0",
        policy=policy,
    )
    assert activated[0] is obj
    assert activated[0]["segments"][0]["score"] == 0.85


def test_policy_env_activation_parsing(monkeypatch):
    monkeypatch.setenv("CONTEXT_ACTIVATION_SCORING", "on")
    monkeypatch.setenv("CONTEXT_ACTIVATION_OVERLAP_FLOOR", "0.3")
    monkeypatch.setenv("CONTEXT_ACTIVATION_HALF_LIFE_TURNS", "4")
    monkeypatch.setenv("CONTEXT_ACTIVATION_TURN_BOOST", "1.5")
    monkeypatch.setenv("CONTEXT_ACTIVATION_OMIT_THRESHOLD", "0.05")
    monkeypatch.setenv("CONTEXT_ACTIVATION_MISSING_TURN_AGE", "5")
    policy = ContextPolicy.from_env(None)
    assert policy.activation_scoring is True
    assert policy.activation_overlap_floor == 0.3
    assert policy.activation_half_life_turns == 4.0
    assert policy.activation_turn_boost == 1.5
    assert policy.activation_omit_threshold == 0.05
    assert policy.activation_missing_turn_age == 5.0

    monkeypatch.setenv("CONTEXT_ACTIVATION_SCORING", "bogus")
    monkeypatch.setenv("CONTEXT_ACTIVATION_TURN_BOOST", "not-a-float")
    fallback = ContextPolicy.from_env(None)
    assert fallback.activation_scoring is False
    assert fallback.activation_turn_boost == 1.25


def test_apply_activation_never_mutates_store(tmp_path):
    policy = _enabled_policy()
    store = ContextObjectStore(ArtifactStore(tmp_path))
    obj = _object(
        sid="a:1",
        text="修复技术选型",
        score=0.9,
        turn_id="t0",
        query="修复",
    )
    oid = store.put(obj)
    loaded = store.read(oid)
    assert loaded is not None
    apply_activation_to_objects(
        [loaded],
        current_query="修复技术选型",
        messages=_messages("t0"),
        active_turn_id="t0",
        policy=policy,
    )
    reloaded = store.read(oid)
    assert reloaded is not None
    assert reloaded["segments"][0]["score"] == 0.9
    assert "_activation" not in reloaded["segments"][0]


def test_ledger_rows_carry_dual_scores_and_accounting():
    policy = _enabled_policy()
    current = _object(
        sid="cur:1",
        text="修复技术选型",
        score=0.8,
        turn_id="t1",
        query="修复",
    )
    old = _object(
        sid="old:1",
        text="修复技术选型",
        score=0.95,
        turn_id="t0",
        query="修复",
    )
    messages = _messages("t0", "t1")
    activated = apply_activation_to_objects(
        [current, old],
        current_query="修复技术选型",
        messages=messages,
        active_turn_id="t1",
        policy=policy,
    )
    ledger = CapabilityLedger(
        turn_id="t1",
        token_pool=50000,
        activation_enabled=True,
        activation_omit_threshold=policy.activation_omit_threshold,
    )
    ledger.register_objects(activated)
    ledger.reassign_citation_numbers()
    rows = ledger.ledger_rows()
    assert rows[0]["sid"] == "cur:1"
    assert rows[0]["activation_score"] > rows[1]["activation_score"]
    assert rows[0]["retrieval_score"] < rows[1]["retrieval_score"]
    assert rows[0]["age_turns"] == 0
    accounting = ledger.accounting()["activation"]
    assert accounting["enabled"] is True
    assert accounting["segments"] == 2
    assert accounting["same_turn_segments"] == 1
    assert accounting["decayed_segments"] == 1
    assert accounting["turn_boosted"] == 1


def test_rendered_ledger_shows_activation_and_retrieval():
    policy = _enabled_policy()
    obj = _object(
        sid="a:1",
        text="修复技术选型",
        score=0.9,
        turn_id="t0",
        query="修复",
    )
    messages = _messages("t0")
    activated = apply_activation_to_objects(
        [obj],
        current_query="修复技术选型",
        messages=messages,
        active_turn_id="t0",
        policy=policy,
    )
    ledger = CapabilityLedger(
        turn_id="t0",
        token_pool=50000,
        activation_enabled=True,
        activation_omit_threshold=policy.activation_omit_threshold,
    )
    ledger.register_objects(activated)
    ledger.reassign_citation_numbers()
    rendered, _cost = ProjectionCompiler().render_ledger(
        ledger.ledger_rows(),
        budget_tokens=50000,
        dynamic_budget=50000,
    )
    assert "act=" in rendered
    assert "ret=" in rendered
