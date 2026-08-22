"""Shared token calibration, count caching, and incremental history fitting."""

import pytest
from langchain_core.messages import AIMessage, HumanMessage, ToolMessage

from backend.core.agents.context.ledger import (
    ContextLedger,
    LedgerBudget,
    TokenCounter,
    count_message_tokens,
    reset_shared_token_calibration,
)


@pytest.fixture(autouse=True)
def _clean_shared_calibration():
    reset_shared_token_calibration()
    yield
    reset_shared_token_calibration()


def _converge_downward(counter: TokenCounter, *, raw_estimate: int, actual: int, rounds: int) -> None:
    """Simulate the middleware feedback loop: estimates track the live factor."""

    for _ in range(rounds):
        estimated = int(raw_estimate * counter._calibration_factor)
        counter.observe(estimated_tokens=estimated, actual_tokens=actual)


def test_underestimate_raises_factor_immediately_even_for_small_samples():
    counter = TokenCounter("calibration-upward-model")
    before = counter._calibration_factor

    counter.observe(estimated_tokens=50, actual_tokens=100)

    assert counter._calibration_factor > before


def test_consistent_overestimates_converge_factor_downward_with_safety_margin():
    counter = TokenCounter("calibration-downward-model")
    assert counter._calibration_factor == pytest.approx(1.15)

    _converge_downward(counter, raw_estimate=10000, actual=4000, rounds=6)

    # Physical ratio is 0.4; the factor should settle at ratio * 1.10 safety.
    assert counter._calibration_factor == pytest.approx(0.44, abs=0.02)


def test_single_or_tiny_overestimate_sample_never_lowers_factor():
    counter = TokenCounter("calibration-cautious-model")

    # Tiny sample: ignored entirely.
    counter.observe(estimated_tokens=200, actual_tokens=50)
    assert counter._calibration_factor == pytest.approx(1.15)

    # First trustworthy sample seeds the EMA but does not move the factor yet.
    counter.observe(estimated_tokens=10000, actual_tokens=4000)
    assert counter._calibration_factor == pytest.approx(1.15)


def test_new_counter_for_same_model_inherits_converged_factor():
    first = TokenCounter("calibration-shared-model")
    _converge_downward(first, raw_estimate=10000, actual=4000, rounds=6)

    second = TokenCounter("calibration-shared-model")

    assert second._calibration_factor == pytest.approx(
        first._calibration_factor, rel=0.01
    )
    assert second._calibration_factor < 0.6


def test_context_overflow_raises_factor_and_blocks_immediate_relowering():
    counter = TokenCounter("calibration-overflow-model")
    _converge_downward(counter, raw_estimate=10000, actual=4000, rounds=6)
    converged = counter._calibration_factor

    counter.note_context_overflow()
    raised = counter._calibration_factor
    assert raised == pytest.approx(converged * 1.25, rel=0.01)

    # One post-overflow sample must not lower the factor again (fresh samples
    # are required before downward moves resume).
    counter.observe(
        estimated_tokens=int(10000 * raised), actual_tokens=4000
    )
    assert counter._calibration_factor == pytest.approx(raised, rel=0.01)


def test_kill_switch_restores_legacy_upward_only_behavior(monkeypatch):
    monkeypatch.setenv("AGENT_TOKEN_CALIBRATION_SHARED", "off")
    counter = TokenCounter("calibration-legacy-model")

    _converge_downward(counter, raw_estimate=10000, actual=4000, rounds=6)
    assert counter._calibration_factor == pytest.approx(1.15)

    counter.observe(estimated_tokens=100, actual_tokens=200)
    assert counter._calibration_factor > 1.15

    # And no shared state was recorded for the next counter.
    monkeypatch.delenv("AGENT_TOKEN_CALIBRATION_SHARED")
    fresh = TokenCounter("calibration-legacy-model")
    assert fresh._calibration_factor == pytest.approx(1.15)


def test_raw_count_cache_reuses_computation_and_survives_factor_changes():
    counter = TokenCounter("calibration-cache-model")
    text = "地下水污染迁移风险评估与治理方案" * 30
    calls = {"n": 0}
    original_compute = counter._compute_raw_count

    def _tracked(content: str) -> int:
        calls["n"] += 1
        return original_compute(content)

    counter._compute_raw_count = _tracked

    first = counter.count(text)
    second = counter.count(text)
    assert first == second
    assert calls["n"] == 1

    # Calibration changes rescale the cached raw count instead of stale totals.
    counter.observe(estimated_tokens=first, actual_tokens=first * 2)
    third = counter.count(text)
    assert calls["n"] == 1
    assert third > first


def test_count_message_tokens_matches_field_level_formula():
    counter = TokenCounter("calibration-formula-model")
    message = AIMessage(
        content="结论：污染羽在两年内到达水源地。",
        name="assistant",
        tool_calls=[
            {"name": "multi_kb_search", "args": {"query": "污染羽"}, "id": "tc-1"}
        ],
        additional_kwargs={"note": "keep"},
    )

    expected = (
        counter.count(message.content)
        + counter.count(message.tool_calls)
        + counter.count(message.additional_kwargs)
        + counter.count(message.name)
        + 6
    )

    assert count_message_tokens(counter, message) == expected


class _CharCounter:
    """Deterministic duck-typed counter used by existing ledger tests."""

    def count(self, text):
        return len(str(text or ""))


def _naive_fit_messages(ledger, messages, *, system_content=None, request_overhead=None):
    """Reference implementation of the previous O(n^2) fitting algorithm."""

    original = list(messages or [])
    history_budget = ledger.history_token_budget

    def total(candidate):
        return ledger.count_base_tokens(
            messages=candidate,
            system_content=system_content,
            request_overhead=request_overhead,
        )

    if total(original) <= history_budget:
        return original, False
    latest_human_index = next(
        (
            index
            for index in range(len(original) - 1, -1, -1)
            if ContextLedger._is_regular_user_message(original[index])
        ),
        max(0, len(original) - 1),
    )
    current_turn = ContextLedger._compact_current_turn(original[latest_human_index:])
    current_turn_tokens = total(current_turn)
    effective_budget = history_budget
    if current_turn_tokens > history_budget and current_turn_tokens <= ledger.token_budget:
        effective_budget = ledger.token_budget
    if current_turn_tokens > ledger.token_budget:
        raise ValueError("current turn exceeds budget")
    history_messages = original[:latest_human_index]
    anchor = next(
        (
            message
            for message in reversed(history_messages)
            if ContextLedger._is_compaction_summary(message)
        ),
        None,
    )
    if anchor is not None:
        history_messages = [
            message
            for message in history_messages
            if not ContextLedger._is_compaction_summary(message)
            or ContextLedger._same_message(message, anchor)
        ]
    selected = list(current_turn)
    # Mirror fit_messages: the anchor's budget is reserved before history
    # groups, but the anchor itself is inserted at the head afterwards so
    # retained groups never precede the summary that covers older turns.
    anchor_selected = False
    if anchor is not None and total([anchor, *selected]) <= effective_budget:
        anchor_selected = True
    reserve = [anchor] if anchor_selected else []
    for group in reversed(ContextLedger._history_groups(history_messages)):
        if anchor is not None:
            group = [
                message
                for message in group
                if not ContextLedger._same_message(message, anchor)
            ]
        if not group:
            continue
        candidate = [*group, *selected]
        if total([*reserve, *candidate]) <= effective_budget:
            selected = candidate
    if anchor_selected:
        selected = [anchor, *selected]
    return selected, True


def _make_history(anchor: bool) -> list:
    messages: list = []
    if anchor:
        messages.append(
            HumanMessage(
                content="[前情摘要] 早期窗口结论。",
                id="anchor-1",
                additional_kwargs={"context_compaction": {"window_id": "w1"}},
            )
        )
    for turn in range(6):
        messages.append(HumanMessage(content=f"问题 {turn}：" + "背景 " * 40, id=f"u{turn}"))
        messages.append(
            AIMessage(
                content=f"回答 {turn}：" + "分析 " * 60,
                id=f"a{turn}",
                tool_calls=[
                    {"name": "multi_kb_search", "args": {"query": f"q{turn}"}, "id": f"tc{turn}"}
                ],
            )
        )
        messages.append(
            ToolMessage(
                content="证据 " * 80,
                tool_call_id=f"tc{turn}",
                name="multi_kb_search",
                id=f"t{turn}",
            )
        )
    messages.append(HumanMessage(content="最新问题：请综合结论。", id="u-final"))
    return messages


@pytest.mark.parametrize("anchor", [False, True])
@pytest.mark.parametrize("max_input_tokens", [900, 1400, 2600, 100000])
def test_incremental_fit_messages_matches_naive_reference(anchor, max_input_tokens):
    def build_ledger():
        return ContextLedger(
            budget=LedgerBudget(
                model_max_input_tokens=max_input_tokens,
                reserved_output_tokens=100,
                safety_margin_tokens=50,
            ),
            token_counter=_CharCounter(),
        )

    messages = _make_history(anchor)
    fitted, pruned = build_ledger().fit_messages(
        list(messages), system_content="系统提示", request_overhead={"tools": []}
    )
    expected, expected_pruned = _naive_fit_messages(
        build_ledger(),
        list(messages),
        system_content="系统提示",
        request_overhead={"tools": []},
    )

    assert pruned == expected_pruned
    assert [(m.id, str(m.content)) for m in fitted] == [
        (m.id, str(m.content)) for m in expected
    ]


def test_incremental_fit_messages_still_rejects_unfittable_current_turn():
    ledger = ContextLedger(
        budget=LedgerBudget(
            model_max_input_tokens=300,
            reserved_output_tokens=50,
            safety_margin_tokens=20,
        ),
        token_counter=_CharCounter(),
    )
    messages = [HumanMessage(content="超" * 5000, id="huge")]

    with pytest.raises(ValueError):
        ledger.fit_messages(messages, system_content="系统提示")
