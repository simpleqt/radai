import math

import pytest
from langchain_core.messages import AIMessage, HumanMessage, ToolMessage

from backend.core.agents.context.artifact_store import ArtifactStore
from backend.core.agents.context.capture import (
    MultiKbCaptureHandler,
    WebSearchCaptureHandler,
)
from backend.core.agents.context.ledger import (
    DYNAMIC_CONTEXT_HEADER,
    ContextLedger,
    LedgerBudget,
    TokenCounter,
    make_evidence_record,
)
from backend.core.agents.context.policy import ContextPolicy
from backend.core.agents.state import merge_context_records


def test_context_ledger_reserves_output_tokens_and_deduplicates_records():
    ledger = ContextLedger(
        budget=LedgerBudget(
            model_max_input_tokens=2200,
            reserved_output_tokens=200,
            safety_margin_tokens=100,
        ),
        top_k=8,
    )
    first = make_evidence_record(
        source_tool="multi_kb_search",
        content="标准 A 的限值为 17 mg/L。来源：doc-a",
        category="retrieval",
        source_id="doc-a",
        chunk_id="c1",
        doc_title="doc-a",
        score=0.9,
    )
    duplicate = dict(first)
    duplicate["claim"] = "重复 claim 不应造成双份注入"
    low_value = make_evidence_record(
        source_tool="weather",
        content="无关天气信息",
        category="tool_result",
        score=0.1,
    )

    pack = ledger.build_pack(
        [low_value, first, duplicate],
        messages=[HumanMessage(content="标准 A 限值是多少")],
    )

    assert pack["budget"]["token_budget"] == 1900
    assert pack["budget"]["reserved_output_tokens"] == 200
    assert pack["budget"]["tokens_used"] <= pack["budget"]["token_budget"]
    assert pack["selected_ids"].count(first["id"]) == 1
    assert len(pack["selected_ids"]) == len(set(pack["selected_ids"]))
    assert "<dynamic_context_pack>" in pack["rendered"]
    assert "context_record id" in pack["rendered"]
    assert "never an artifact reference" in pack["rendered"]
    assert 'source_id="doc-a"' in pack["rendered"]
    assert 'chunk_id="c1"' in pack["rendered"]


def test_context_record_merge_moves_refreshed_record_to_latest_position():
    first = make_evidence_record(
        source_tool="multi_kb_search",
        content="first version",
        category="retrieval",
        source_id="doc-a",
        chunk_id="chunk-a",
    )
    second = make_evidence_record(
        source_tool="multi_kb_search",
        content="second record",
        category="retrieval",
        source_id="doc-b",
        chunk_id="chunk-b",
    )
    refreshed = dict(first)
    refreshed["content"] = "refreshed version"

    merged = merge_context_records([first, second], [refreshed])

    assert [record["id"] for record in merged] == [second["id"], first["id"]]
    assert merged[-1]["content"] == "refreshed version"


def test_ledger_budget_resolves_model_limits_from_config(monkeypatch):
    from backend.core.models import context_limits

    monkeypatch.setattr(context_limits, "resolve_max_input_tokens", lambda model_name=None: 8192)
    monkeypatch.setattr(context_limits, "resolve_max_tokens", lambda model_name=None: 1024)

    budget = LedgerBudget.from_model_name("mysql-configured-model", safety_margin_tokens=256)

    assert budget.model_max_input_tokens == 8192
    assert budget.reserved_output_tokens == 1024
    assert budget.input_budget_tokens == 6912


def test_budget_fallback_honors_env_floor_when_limits_unavailable(monkeypatch):
    def _unavailable(**_kwargs):
        raise RuntimeError("catalog down")

    monkeypatch.setattr(
        "backend.core.models.context_limits.resolve_max_input_tokens",
        _unavailable,
    )
    monkeypatch.setenv("MODEL_INPUT_BUDGET_FALLBACK_TOKENS", "32000")

    budget = LedgerBudget.from_model_name("qwen-any")

    assert budget.model_max_input_tokens == 32000

    monkeypatch.delenv("MODEL_INPUT_BUDGET_FALLBACK_TOKENS")
    default_budget = LedgerBudget.from_model_name("qwen-any")
    assert default_budget.model_max_input_tokens == 8192


def test_unknown_provider_uses_calibrated_estimate_not_cl100k():
    counter = TokenCounter("qwen-max-latest")
    before = counter.count("中文 token calibration sample")

    counter.observe(estimated_tokens=before, actual_tokens=before * 2)
    after = counter.count("中文 token calibration sample")

    assert counter.method.startswith("mixed_language_heuristic")
    assert after > before


def test_unknown_provider_estimate_is_upper_biased_for_cjk():
    text = "地下水污染迁移风险" * 100

    estimated = TokenCounter("qwen-max-latest").count(text)

    assert estimated > len(text)


def test_active_turn_fallback_distinguishes_repeated_prompts():
    first = HumanMessage(content="repeat this question")
    second = HumanMessage(content="repeat this question")

    first_id = ContextLedger._active_turn_id([first])
    second_id = ContextLedger._active_turn_id(
        [first, AIMessage(content="previous answer"), second]
    )

    assert first_id != second_id
    assert ContextLedger._active_turn_id([AIMessage(content="summary"), first]) == first_id


def test_context_policy_centralizes_model_and_compaction_budgets(monkeypatch):
    from backend.core.models import context_limits

    monkeypatch.setattr(context_limits, "resolve_max_input_tokens", lambda model_name=None: 12000)
    monkeypatch.setattr(context_limits, "resolve_max_tokens", lambda model_name=None: 2000)
    monkeypatch.setenv("AGENT_LARGE_RESULT_THRESHOLD_CHARS", "3000")
    monkeypatch.setenv("RESULT_SUMMARY_MAX_CHARS", "3000")
    monkeypatch.setenv("RESULT_SUMMARY_MAX_PER_ROUND", "9")

    policy = ContextPolicy.from_env("main-agent-model")

    assert policy.model_name == "main-agent-model"
    assert policy.input_budget_tokens == 9232
    assert policy.history_summary_trigger_tokens == int(9232 * 0.8)
    assert policy.history_summary_keep_tokens == int(9232 * 0.25)
    assert policy.compaction_trigger_tokens <= policy.history_summary_trigger_tokens
    assert policy.large_result_threshold_chars == 3000
    assert policy.result_summary_output_chars == 3000
    assert policy.result_summary_max_concurrent == 5


def test_context_policy_clamps_compaction_before_framework_fallback(monkeypatch):
    monkeypatch.setenv("AGENT_HISTORY_SUMMARIZATION_TRIGGER_TOKENS", "6000")
    monkeypatch.setenv("AGENT_COMPACTION_TRIGGER_TOKENS", "9000")
    monkeypatch.setenv("RESULT_SUMMARY_CACHE_TTL_SECONDS", "120")
    monkeypatch.setenv("RESULT_SUMMARY_CACHE_MAX_ENTRIES", "64")

    policy = ContextPolicy.from_env(None)

    assert policy.compaction_trigger_tokens == 6000
    assert policy.result_summary_cache_ttl_seconds == 120
    assert policy.result_summary_cache_max_entries == 64


def test_context_policy_handles_invalid_environment_values(monkeypatch):
    monkeypatch.setenv("RESULT_SUMMARY_MAX_CHARS", "not-a-number")
    monkeypatch.setenv("RESULT_SUMMARY_TIMEOUT_SECONDS", "-1")

    policy = ContextPolicy.from_env(None)

    assert policy.result_summary_output_chars == 3000
    assert policy.result_summary_timeout_seconds == 10


def test_model_limit_lookup_prefers_config_name_before_api_model():
    from backend.core.models.context_limits import _model_lookup_conditions
    from backend.db.models.llm_model import LlmModel

    conditions = _model_lookup_conditions(
        LlmModel,
        name="tenant-small-window",
        api_model="shared-api-model",
    )

    assert "llm_models.name" in str(conditions[0])
    assert "llm_models.model" in str(conditions[1])


def test_context_ledger_borrows_idle_budget_up_to_hard_cap():
    class FixedCounter:
        def count(self, text):
            return 250 if "<context_record" in str(text) else 1

    ledger = ContextLedger(
        budget=LedgerBudget(
            model_max_input_tokens=1600,
            reserved_output_tokens=300,
            safety_margin_tokens=100,
        ),
        top_k=4,
    )
    ledger._counter = FixedCounter()  # type: ignore[attr-defined]
    records = [
        make_evidence_record(
            source_tool="multi_kb_search",
            content=f"证据 {idx}: 标准 A 的限值为 {idx} mg/L。来源：doc-{idx}",
            category="retrieval",
            source_id=f"doc-{idx}",
            chunk_id=f"c{idx}",
            doc_title=f"doc-{idx}",
            score=1.0 - idx / 10,
        )
        for idx in range(2)
    ]

    pack = ledger.build_pack(records, query="标准 A 限值")

    assert len(pack["selected_ids"]) == 2
    assert pack["usage_by_category"]["retrieval"] == 500


def test_context_ledger_subtracts_existing_system_and_history_tokens():
    class FixedCounter:
        def count(self, text):
            if not text:
                return 0
            value = str(text or "")
            if value == "large-system":
                return 900
            if "<context_record" in value:
                return 250
            return 10

    ledger = ContextLedger(
        budget=LedgerBudget(
            model_max_input_tokens=1600,
            reserved_output_tokens=300,
            safety_margin_tokens=100,
        )
    )
    ledger._counter = FixedCounter()  # type: ignore[attr-defined]
    record = make_evidence_record(
        source_tool="multi_kb_search",
        content="标准 A 的限值为 17 mg/L。",
        category="retrieval",
    )

    pack = ledger.build_pack(
        [record],
        messages=[HumanMessage(content="标准 A")],
        system_content="large-system",
    )

    assert pack["budget"]["base_tokens"] == 916
    # The ledger reserves the complete static JIT envelope before selecting
    # evidence, so the usable dynamic slice is smaller than the old tag-only
    # estimate.
    assert pack["budget"]["dynamic_token_budget"] == 136
    assert pack["budget"]["tokens_used"] <= pack["budget"]["token_budget"]


def test_context_ledger_counts_tool_schema_request_overhead():
    ledger = ContextLedger(
        budget=LedgerBudget(
            model_max_input_tokens=4096,
            reserved_output_tokens=512,
            safety_margin_tokens=256,
        )
    )
    record = make_evidence_record(
        source_tool="multi_kb_search",
        content="标准 A 的限值为 17 mg/L。",
        category="retrieval",
    )

    plain = ledger.build_pack([record], query="标准 A")
    with_tools = ledger.build_pack(
        [record],
        query="标准 A",
        request_overhead={
            "tools": [
                {
                    "type": "function",
                    "function": {
                        "name": "multi_kb_search",
                        "description": "检索知识库并返回引用证据。" * 40,
                    },
                }
            ]
        },
    )

    assert with_tools["budget"]["base_tokens"] > plain["budget"]["base_tokens"]
    assert with_tools["budget"]["dynamic_token_budget"] < plain["budget"]["dynamic_token_budget"]


def test_context_ledger_prunes_old_turns_when_base_context_exceeds_budget():
    class CharCounter:
        def count(self, text):
            return len(str(text or ""))

    ledger = ContextLedger(
        budget=LedgerBudget(
            model_max_input_tokens=1600,
            reserved_output_tokens=300,
            safety_margin_tokens=100,
        )
    )
    ledger._counter = CharCounter()  # type: ignore[attr-defined]
    messages = [
        HumanMessage(content="old-1 " + "x" * 500),
        AIMessage(content="answer-1 " + "y" * 500),
        HumanMessage(content="old-2 " + "x" * 500),
        AIMessage(content="answer-2 " + "y" * 500),
        HumanMessage(content="current question"),
    ]

    fitted, pruned = ledger.fit_messages(messages, system_content="system")

    assert pruned is True
    assert fitted[-1].content == "current question"
    assert len(fitted) < len(messages)
    assert ledger.count_base_tokens(messages=fitted, system_content="system") <= ledger.token_budget


def test_context_ledger_reserves_dynamic_budget_when_fitting_history():
    class CharCounter:
        def count(self, text):
            return len(str(text or ""))

    ledger = ContextLedger(
        budget=LedgerBudget(
            model_max_input_tokens=1600,
            reserved_output_tokens=300,
            safety_margin_tokens=100,
        ),
        dynamic_reserve_tokens=300,
    )
    ledger._counter = CharCounter()  # type: ignore[attr-defined]
    messages = [
        HumanMessage(content="old " + "x" * 260),
        AIMessage(content="old answer " + "y" * 260),
        HumanMessage(content="current question " + "z" * 520),
    ]

    fitted, pruned = ledger.fit_messages(messages, system_content="system")

    assert pruned is True
    assert fitted[-1].content.startswith("current question")
    assert ledger.count_base_tokens(messages=fitted, system_content="system") <= 900


def test_context_ledger_preserves_compaction_summary_before_older_turns():
    class CharCounter:
        def count(self, text):
            return len(str(text or ""))

    ledger = ContextLedger(
        budget=LedgerBudget(
            model_max_input_tokens=1600,
            reserved_output_tokens=300,
            safety_margin_tokens=100,
        ),
        dynamic_reserve_tokens=300,
    )
    ledger._counter = CharCounter()  # type: ignore[attr-defined]
    summary = HumanMessage(
        content="compacted summary " + "s" * 100,
        id="summary-1",
        additional_kwargs={
            "context_compaction": {"window_id": "ctxwin-1"}
        },
    )
    messages = [
        summary,
        HumanMessage(content="older question " + "x" * 420, id="u-old"),
        AIMessage(content="older answer " + "y" * 420, id="a-old"),
        HumanMessage(content="current question " + "z" * 350, id="u-current"),
    ]

    fitted, pruned = ledger.fit_messages(messages, system_content="system")

    assert pruned is True
    assert [message.id for message in fitted] == ["summary-1", "u-current"]
    assert ledger.count_base_tokens(messages=fitted, system_content="system") <= 900


def test_context_ledger_drops_superseded_compaction_summaries_when_fitting():
    class CharCounter:
        def count(self, text):
            return len(str(text or ""))

    ledger = ContextLedger(
        budget=LedgerBudget(
            model_max_input_tokens=1600,
            reserved_output_tokens=300,
            safety_margin_tokens=100,
        ),
        dynamic_reserve_tokens=300,
    )
    ledger._counter = CharCounter()  # type: ignore[attr-defined]
    summary_old = HumanMessage(
        content="old summary " + "s" * 40,
        id="summary-old",
        additional_kwargs={
            "context_compaction": {"window_id": "ctxwin-old"}
        },
    )
    summary_latest = HumanMessage(
        content="compacted summary " + "s" * 100,
        id="summary-latest",
        additional_kwargs={
            "context_compaction": {"window_id": "ctxwin-latest"}
        },
    )
    messages = [
        summary_old,
        summary_latest,
        HumanMessage(content="older question " + "x" * 420, id="u-old"),
        AIMessage(content="older answer " + "y" * 420, id="a-old"),
        HumanMessage(content="current question " + "z" * 350, id="u-current"),
    ]

    fitted, pruned = ledger.fit_messages(messages, system_content="system")

    assert pruned is True
    assert [message.id for message in fitted] == ["summary-latest", "u-current"]


def test_context_ledger_history_groups_ignore_compaction_summary_boundaries():
    summary = HumanMessage(
        content="compacted summary " + "s" * 40,
        id="summary-1",
        additional_kwargs={
            "context_compaction": {"window_id": "ctxwin-1"}
        },
    )
    first_user = HumanMessage(content="first question", id="u-1")
    first_answer = AIMessage(content="first answer", id="a-1")
    second_user = HumanMessage(content="second question", id="u-2")

    groups = ContextLedger._history_groups(
        [first_user, first_answer, summary, second_user]
    )

    assert groups == [[first_user, first_answer, summary], [second_user]]


def test_context_ledger_compacted_current_turn_marks_rewritten_messages():
    tool_message = ToolMessage(
        content="tool payload " + "x" * 900,
        id="tool-1",
        tool_call_id="call-1",
    )
    long_answer = AIMessage(content="long answer " + "y" * 1300, id="answer-1")
    short_message = HumanMessage(content="short", id="user-1")

    compacted = ContextLedger._compact_current_turn(
        [tool_message, long_answer, short_message]
    )

    tool_rewrite = compacted[0].additional_kwargs.get(
        "context_compaction_rewrite"
    )
    answer_rewrite = compacted[1].additional_kwargs.get(
        "context_compaction_rewrite"
    )
    assert tool_rewrite is not None
    assert tool_rewrite["reason"] == "tool_result_elevated"
    assert tool_rewrite["original_chars"] > tool_rewrite["rewritten_chars"]
    assert answer_rewrite is not None
    assert answer_rewrite["reason"] == "long_assistant_summary"
    assert compacted[0].id == "tool-1"
    assert compacted[1].id == "answer-1"
    assert "context_compaction_rewrite" not in (
        compacted[2].additional_kwargs or {}
    )


def test_compacted_current_turn_keeps_bounded_tool_excerpt():
    tool_message = ToolMessage(
        content="结论：抽样水质合格。" + "x" * 2000 + " 依据标准GB14848结束",
        id="tool-1",
        tool_call_id="call-1",
    )

    compacted = ContextLedger._compact_current_turn([tool_message])

    content = str(compacted[0].content)
    # Head keeps conclusions, tail keeps citations; the body stays bounded.
    assert "结论：抽样水质合格。" in content
    assert "依据标准GB14848结束" in content
    assert len(content) < 700


def test_context_ledger_injects_bounded_raw_ref_excerpt(tmp_path):
    store = ArtifactStore(tmp_path)
    raw_ref = store.store(
        "原始校准明细: channel 17 PASS; threshold=0.42; " + ("detail " * 200),
        thread_id="thread-1",
        source_tool="calibration",
    )
    ledger = ContextLedger(
        budget=LedgerBudget(
            model_max_input_tokens=4096,
            reserved_output_tokens=512,
            safety_margin_tokens=256,
        ),
        artifact_store=store,
        raw_ref_char_limit=300,
    )
    record = make_evidence_record(
        source_tool="calibration",
        content="校准结果可用。",
        category="tool_result",
        raw_ref=raw_ref,
    )

    rendered = ledger.render_record(record)

    assert "raw_excerpt" in rendered
    assert "channel 17 PASS" in rendered
    assert len(rendered) < 1400


def test_context_ledger_caches_raw_ref_excerpt_reads():
    class CountingStore:
        def __init__(self):
            self.reads = 0

        def read(self, raw_ref, *, max_chars=None):
            self.reads += 1
            return "原始明细：channel 17 PASS; threshold=0.42; " + ("detail " * 20)

        def metadata(self, raw_ref):
            return None

    store = CountingStore()
    ledger = ContextLedger(
        budget=LedgerBudget(
            model_max_input_tokens=4096,
            reserved_output_tokens=512,
            safety_margin_tokens=256,
        ),
        artifact_store=store,  # type: ignore[arg-type]
        raw_ref_char_limit=300,
    )
    record = make_evidence_record(
        source_tool="calibration",
        content="校准结果可用。",
        category="tool_result",
        raw_ref="artifact://context/raw-1",
    )

    first = ledger.render_record(record)
    second = ledger.render_record(record)

    assert store.reads == 1
    assert 'trust="untrusted_tool_data"' in first
    assert "channel 17 PASS" in second


def test_context_ledger_keeps_claim_when_raw_ref_excerpt_exceeds_category_budget(tmp_path):
    class CharCounter:
        def count(self, text):
            return len(str(text or ""))

    store = ArtifactStore(tmp_path)
    raw_ref = store.store(
        "原始校准明细 " + ("channel 17 PASS; " * 100),
        thread_id="thread-1",
        source_tool="calibration",
    )
    ledger = ContextLedger(
        budget=LedgerBudget(
            model_max_input_tokens=2200,
            reserved_output_tokens=300,
            safety_margin_tokens=100,
        ),
        artifact_store=store,
        raw_ref_char_limit=800,
    )
    ledger._counter = CharCounter()  # type: ignore[attr-defined]
    record = make_evidence_record(
        source_tool="calibration",
        content="校准结果可用。",
        category="tool_result",
        raw_ref=raw_ref,
    )

    pack = ledger.build_pack([record], query="校准结果")

    assert pack["selected_ids"] == [record["id"]]
    assert "校准结果可用" in pack["rendered"]
    assert "raw_excerpt" not in pack["rendered"]
    assert f'raw_ref="{raw_ref}"' in pack["rendered"]


def test_compile_request_no_longer_materializes_legacy_raw_windows(tmp_path):
    store = ArtifactStore(tmp_path)
    raw_ref = store.store(
        "RAW-WINDOW-CONTENT " * 100,
        thread_id="thread-1",
        source_tool="multi_kb_search",
    )
    record = make_evidence_record(
        source_tool="multi_kb_search",
        content="compact retrieval evidence",
        category="retrieval",
        raw_ref=raw_ref,
    )
    ledger = ContextLedger(
        budget=LedgerBudget(
            model_max_input_tokens=12000,
            reserved_output_tokens=500,
            safety_margin_tokens=200,
        ),
        artifact_store=store,
        raw_ref_char_limit=12000,
    )
    compiled = ledger.compile_request(
        [record],
        messages=[HumanMessage(content="use the retrieval")],
    )
    rendered = compiled["pack"]["rendered"]
    assert "compact retrieval evidence" in rendered
    assert "RAW-WINDOW-CONTENT" not in rendered
    assert "ctxmat_" not in rendered
    assert "materialized_refs" not in compiled["pack"]["capabilities"]


def test_context_ledger_chinese_overlap_uses_short_ngrams():
    ledger = ContextLedger()
    record = make_evidence_record(
        source_tool="multi_kb_search",
        content="地下水污染羽迁移方向为东北，风险受渗透系数控制。",
        category="retrieval",
    )

    assert ledger._score(record, "污染羽迁移方向") > ledger._score(record, "完全无关问题")


def test_context_ledger_renders_complete_retrieval_content_not_claim_only():
    ledger = ContextLedger()
    record = make_evidence_record(
        source_tool="multi_kb_search",
        content=(
            "表7-1 地质环境质量评价定权方法表\n"
            "1 专家打分法\n2 调查统计法\n3 序列综合法\n4 公式法\n"
            "5 数理统计法\n6 层次分析法\n7 复杂度分析法"
        ),
        category="retrieval",
        source_id="kb-csh",
        chunk_id="table-7-1",
    )
    record["claim"] = "表7-1：1 专家打分法（摘要到此为止）"

    rendered = ledger.render_record(record, include_raw_excerpt=False)

    assert "1 专家打分法" in rendered
    assert "4 公式法" in rendered
    assert "7 复杂度分析法" in rendered


def test_context_ledger_rejects_unavoidable_current_turn_overflow():
    class CharCounter:
        def count(self, text):
            return len(str(text or ""))

    ledger = ContextLedger(
        budget=LedgerBudget(
            model_max_input_tokens=1600,
            reserved_output_tokens=300,
            safety_margin_tokens=100,
        )
    )
    ledger._counter = CharCounter()  # type: ignore[attr-defined]

    with pytest.raises(ValueError, match="exceed the model input budget"):
        ledger.fit_messages(
            [HumanMessage(content="current question")],
            system_content="s" * 1300,
        )


def test_context_compiler_trims_low_priority_records_before_dropping_jit():
    class BoundaryCounter:
        def count(self, text):
            value = str(text or "")
            tokens = math.ceil(len(value) / 4) if value else 0
            # Simulate a tokenizer boundary effect that only appears after the
            # static system prompt and dynamic suffix are serialized together.
            if value.startswith("base-system") and DYNAMIC_CONTEXT_HEADER in value:
                tokens += 500
            return tokens

    ledger = ContextLedger(
        budget=LedgerBudget(
            model_max_input_tokens=1500,
            reserved_output_tokens=300,
            safety_margin_tokens=100,
        ),
        top_k=8,
        max_record_tokens=512,
    )
    ledger._counter = BoundaryCounter()  # type: ignore[attr-defined]
    records = [
        make_evidence_record(
            source_tool="multi_kb_search",
            content=f"fact-{index} " + ("x" * 500),
            category="retrieval",
            source_id=f"doc-{index}",
            chunk_id=f"chunk-{index}",
            score=1.0 - index / 10,
        )
        for index in range(3)
    ]

    compiled = ledger.compile_request(
        records,
        messages=[HumanMessage(content="question")],
        system_content="base-system",
    )

    pack = compiled["pack"]
    assert 0 < len(pack["selected_ids"]) < len(records)
    assert pack["budget"]["jit_trimmed_for_exact_budget"] is True
    assert pack["budget"]["jit_dropped_for_exact_budget"] is False
    assert pack["budget"]["exact_budget_items_dropped"] >= 1
    assert compiled["final_tokens"] <= ledger.token_budget


def test_multi_kb_capture_prefers_structured_tool_artifact():
    class Sink:
        def ingest(self, **kwargs):
            return {"raw_ref": "artifact://raw", "summary": kwargs["raw_content"]}

    handler = MultiKbCaptureHandler(Sink())
    captured = handler.capture(
        tool_name="multi_kb_search",
        content="markdown fallback",
        tool_args={},
        thread_id="thread-1",
        artifact={
            "evidence_records": [
                {
                    "id": "ev-1",
                    "source_id": "kb-a",
                    "chunk_id": "chunk-9",
                    "doc_title": "标准 A",
                    "claim": "限值为 17 mg/L",
                    "category": "retrieval",
                }
            ]
        },
    )

    assert captured.context_records[0]["chunk_id"] == "chunk-9"
    assert captured.context_records[0]["doc_title"] == "标准 A"
    assert captured.context_records[0]["raw_ref"] == "artifact://raw"


def test_multi_kb_capture_preserves_provider_raw_ref():
    class Sink:
        def ingest(self, **kwargs):
            return {
                "raw_ref": "artifact://context/persisted-result.txt",
                "summary": kwargs["raw_content"],
            }

    handler = MultiKbCaptureHandler(Sink())
    captured = handler.capture(
        tool_name="multi_kb_search",
        content="markdown fallback",
        tool_args={},
        thread_id="thread-1",
        artifact={
            "evidence_records": [
                {
                    "id": "ev-provider",
                    "source_id": "kb-a",
                    "raw_ref": "artifact://context/provider-result.txt",
                },
                {
                    "id": "ev-missing-ref",
                    "source_id": "kb-b",
                },
            ]
        },
    )

    assert captured.context_records[0]["raw_ref"] == (
        "artifact://context/provider-result.txt"
    )
    assert captured.context_records[1]["raw_ref"] == (
        "artifact://context/persisted-result.txt"
    )


def test_web_search_capture_keeps_each_page_and_source_mapping():
    class Sink:
        calls = []

        def ingest(self, **kwargs):
            self.calls.append(kwargs)
            return {"raw_ref": "artifact://web/raw", "summary": "web results"}

    sink = Sink()
    handler = WebSearchCaptureHandler(sink)
    captured = handler.capture(
        tool_name="web_search",
        content='''{
          "query": "BF3 detector efficiency",
          "engine": "firecrawl",
          "retrieval_budget": {"remaining": 0, "exhausted": true},
          "results": [
            {
              "title": "Detector paper",
              "url": "https://example.com/paper",
              "content": "Measured efficiency was 61 percent.",
              "provider": "firecrawl"
            },
            {
              "title": "Specification",
              "url": "https://example.org/spec",
              "description": "Manufacturer specification."
            }
          ]
        }''',
        tool_args={},
        thread_id="thread-1",
    )

    assert len(sink.calls) == 1
    assert len(captured.context_records) == 2
    assert captured.context_records[0]["source_id"] == "https://example.com/paper"
    assert captured.context_records[0]["doc_title"] == "Detector paper"
    assert captured.context_records[0]["raw_ref"] == "artifact://web/raw"
    assert "61 percent" in captured.context_records[0]["content"]
    assert "BF3 detector efficiency" in captured.replacement
    assert "https://example.com/paper" in captured.replacement
    assert "https://example.org/spec" in captured.replacement
    assert "do not call any retrieval tool again" in captured.replacement
