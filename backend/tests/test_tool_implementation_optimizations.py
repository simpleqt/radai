from __future__ import annotations

import asyncio
import json
import threading
from contextvars import copy_context
from types import SimpleNamespace

import duckdb
import pandas as pd
import pytest

from backend.application.tools.execution import (
    ToolReportedError,
    invoke_service_call,
    raise_for_tool_reported_error,
)
from backend.application.tools.providers import excel_engine
from backend.application.tools.providers import excel_engine_connections
from backend.application.tools.providers import async_web_search_tool as web_search_mod
from backend.application.tools.providers.async_web_search_tool import (
    _filter_relevant_results,
)
from backend.application.tools.providers.multi_kb_search_tool import (
    _build_reference_block,
    _fit_whole_parts,
    _format_fragment,
    _select_fragment_entries,
    execute_multi_kb_search,
)
from backend.application.tools.result import normalize_tool_error, normalize_tool_result
from backend.core.agents.middlewares.tool_error_handling import (
    ToolErrorHandlingMiddleware,
)
from backend.core.tools.request_limits import (
    TURN_MAX_EVIDENCE_CHARS,
    RetrievalRequestCache,
    get_tool_budget_remaining,
    get_web_search_no_progress_streak,
    reset_request_tool_budget,
    reset_tool_budgets,
    get_turn_kb_chars_remaining,
    reset_web_search_no_progress_streak,
    reserve_turn_kb_chars,
    set_request_tool_budget,
    set_tool_budgets,
    reset_turn_kb_context_budget,
    reset_turn_kb_context_scope,
    set_turn_kb_context_scope,
)


def test_structured_result_compaction_preserves_evidence_and_registered_images() -> (
    None
):
    result = normalize_tool_result(
        {
            "text": "正文" * 10_000,
            "data": {"raw": "x" * 50_000},
            "evidence_records": [{"evidence_id": f"ev-{index}"} for index in range(10)],
            "registered_images": [
                {"token": "[IMAGE:1]", "url": "https://example.test/a.png"}
            ],
            "stats": {"active_kbs": 2},
        },
        tool_name="multi_kb_search",
        max_output_chars=30_000,
    )

    assert result["summary"]
    assert result["text"]
    assert result["evidence_records"]
    assert result["registered_images"][0]["token"] == "[IMAGE:1]"
    assert len(json.dumps(result, ensure_ascii=False, separators=(",", ":"))) <= 30_000


@pytest.mark.asyncio
async def test_structured_service_call_can_defer_output_compaction() -> None:
    class StructuredTool:
        name = "structured"
        metadata = {"max_output_chars": 100}

    payload = {"text": "evidence" * 100, "evidence_records": [{"id": "ev-1"}]}
    result = await invoke_service_call(
        StructuredTool(),
        lambda: asyncio.sleep(0, result=payload),
        enforce_output_limit=False,
    )

    assert result == payload


def test_reported_tool_error_preserves_recovery_details() -> None:
    with pytest.raises(ToolReportedError) as captured:
        raise_for_tool_reported_error(
            {
                "error": "unknown_project_table",
                "message": "missing table",
                "unknown_tables": ["missing"],
                "available_tables": ["points", "constructed_wells"],
                "instruction": "inspect available tables and retry",
            }
        )

    normalized = normalize_tool_error(captured.value, tool_name="query_project_tables")
    assert normalized["error"]["code"] == "invalid_arguments"
    assert normalized["data"]["available_tables"] == ["points", "constructed_wells"]
    assert "retry" in normalized["data"]["instruction"]


def test_graph_tool_error_message_exposes_recovery_details() -> None:
    message = ToolErrorHandlingMiddleware()._build_error_message(
        SimpleNamespace(tool_call={"id": "call-1", "name": "query_project_tables"}),
        ToolReportedError(
            "missing table",
            error_code="invalid_arguments",
            details={"available_tables": ["points"], "instruction": "retry"},
        ),
    )

    assert message.status == "error"
    assert "available_tables" in message.content
    assert "points" in message.content


def test_similar_cache_rejects_opposite_polarity_and_chooses_best_match() -> None:
    cache = RetrievalRequestCache()
    cache.set_exact("该材料会导致地下水污染吗|same", "affirmative")
    cache.set_exact("该材料会导致严重地下水污染吗|same", "best")

    assert cache.get_similar("该材料不会导致地下水污染吗|same") is None
    assert (
        cache.get_similar("该材料会导致严重地下水污染么|same", threshold=0.5) == "best"
    )


def test_excel_cleaning_keeps_mixed_text_values() -> None:
    cleaned = excel_engine._clean_dataframe(
        pd.DataFrame({"mixed": ["1", "2", "not-applicable", None]})
    )

    assert cleaned["mixed"].tolist()[:3] == ["1", "2", "not-applicable"]


def test_excel_query_limit_is_applied_inside_duckdb() -> None:
    conn = duckdb.connect()
    try:
        result, truncated = excel_engine._fetch_bounded_query(
            conn,
            "SELECT * FROM range(100000)",
            7,
        )
    finally:
        conn.close()

    assert len(result) == 7
    assert truncated is True
    assert "LIMIT 8" in excel_engine._bounded_query_sql("SELECT 1", 7)


def test_excel_full_table_statistics_do_not_report_sample_size_as_total(
    tmp_path,
    monkeypatch,
) -> None:
    csv_path = tmp_path / "large.csv"
    pd.DataFrame(
        {
            "value": range(6001),
            "other": [index * 2 for index in range(6001)],
        }
    ).to_csv(csv_path, index=False)
    monkeypatch.setattr(
        excel_engine_connections, "_resolve_local_path", lambda _path: str(csv_path)
    )

    try:
        query_result = excel_engine.execute_sql(
            str(csv_path),
            "SELECT * FROM data",
            max_rows=10,
        )
        statistics = excel_engine.get_table_statistics(str(csv_path), "data")
    finally:
        excel_engine.invalidate_cache(str(csv_path))

    assert len(query_result) == 10
    assert statistics["row_count"] == 6001
    value_summary = statistics["summary"].set_index("column_name").loc["value"]
    assert int(value_summary["count"]) == 6001
    assert float(value_summary["max"]) == 6000
    assert statistics["correlation"].loc["value", "other"] == pytest.approx(1.0)


def test_excel_cache_eviction_skips_active_query_without_blocking() -> None:
    cache_key = "test-active-query-lease"
    conn = duckdb.connect()
    entered = threading.Event()
    release = threading.Event()
    closed = threading.Event()
    close_results: list[bool] = []

    with excel_engine._cache_lock:
        excel_engine._cache[cache_key] = conn
        excel_engine._cache_last_used[cache_key] = 0.0
        excel_engine._cache_sources[cache_key] = set()

    def query_worker() -> None:
        with excel_engine._leased_connection(lambda: conn) as leased:
            entered.set()
            release.wait(timeout=2)
            leased.execute("SELECT 1").fetchone()

    def close_worker() -> None:
        with excel_engine._cache_lock:
            close_results.append(excel_engine._close_cache_entry_locked(cache_key))
        closed.set()

    query_thread = threading.Thread(target=query_worker)
    close_thread = threading.Thread(target=close_worker)
    query_thread.start()
    assert entered.wait(timeout=1)
    close_thread.start()
    assert closed.wait(timeout=1)
    assert close_results == [False]
    assert excel_engine._cache[cache_key] is conn
    release.set()
    query_thread.join(timeout=2)
    close_thread.join(timeout=2)

    assert not query_thread.is_alive()
    assert not close_thread.is_alive()
    with excel_engine._cache_lock:
        assert excel_engine._close_cache_entry_locked(cache_key) is True
    assert cache_key not in excel_engine._cache


@pytest.mark.asyncio
async def test_remote_multi_kb_result_charges_turn_character_budget(
    monkeypatch,
) -> None:
    from backend.application.knowledge_bases import catalog_service
    from backend.application.tools.providers import multi_kb_search_tool

    async def fake_catalog(_kb_ids, *, defaults_only):
        del defaults_only
        return [SimpleNamespace(kb_id="kb-1", kb_name="Knowledge Base")]

    async def fake_remote(**_kwargs):
        return {"text": "remote evidence", "evidence_records": [], "stats": {}}

    monkeypatch.setattr(catalog_service, "load_knowledge_base_catalog", fake_catalog)
    monkeypatch.setattr(
        multi_kb_search_tool, "_rag_service_base_url", lambda: "http://rag"
    )
    monkeypatch.setattr(
        multi_kb_search_tool,
        "_execute_remote_multi_kb_search",
        fake_remote,
    )
    reset_turn_kb_context_budget()

    result = await execute_multi_kb_search(
        "unique remote budget query",
        kb_ids=["kb-1"],
        _structured=True,
        _registry=SimpleNamespace(register=lambda *_args, **_kwargs: ""),
    )

    assert result["text"] == "remote evidence"
    assert get_turn_kb_chars_remaining(TURN_MAX_EVIDENCE_CHARS) == (
        TURN_MAX_EVIDENCE_CHARS - len("remote evidence")
    )


@pytest.mark.asyncio
async def test_oversized_kb_result_is_fitted_instead_of_discarded(monkeypatch) -> None:
    from backend.application.knowledge_bases import catalog_service
    from backend.application.tools.providers import multi_kb_search_tool

    async def fake_catalog(_kb_ids, *, defaults_only):
        del defaults_only
        return [SimpleNamespace(kb_id="kb-1", kb_name="Knowledge Base")]

    async def fake_remote(**_kwargs):
        return {"text": "evidence " * 2_000, "evidence_records": [], "stats": {}}

    monkeypatch.setattr(catalog_service, "load_knowledge_base_catalog", fake_catalog)
    monkeypatch.setattr(
        multi_kb_search_tool, "_rag_service_base_url", lambda: "http://rag"
    )
    monkeypatch.setattr(
        multi_kb_search_tool, "_execute_remote_multi_kb_search", fake_remote
    )
    reset_turn_kb_context_budget()

    result = await execute_multi_kb_search(
        "oversized remote budget query",
        kb_ids=["kb-1"],
        max_result_chars=5_000,
        _structured=True,
        _registry=SimpleNamespace(register=lambda *_args, **_kwargs: ""),
    )

    assert result["text"]
    assert "检索结果未注入" not in result["text"]
    assert len(result["text"]) <= 5_000
    assert result["stats"]["output_budget_truncated"] is True
    assert get_turn_kb_chars_remaining(TURN_MAX_EVIDENCE_CHARS) == (
        TURN_MAX_EVIDENCE_CHARS - len(result["text"])
    )


@pytest.mark.asyncio
async def test_kb_cache_key_does_not_change_with_remaining_turn_budget(
    monkeypatch,
) -> None:
    from backend.application.knowledge_bases import catalog_service
    from backend.application.tools.providers import multi_kb_search_tool

    remote_calls = 0

    async def fake_catalog(_kb_ids, *, defaults_only):
        del defaults_only
        return [SimpleNamespace(kb_id="kb-1", kb_name="Knowledge Base")]

    async def fake_remote(**_kwargs):
        nonlocal remote_calls
        remote_calls += 1
        return {"text": "x" * 4_000, "evidence_records": [], "stats": {}}

    monkeypatch.setattr(catalog_service, "load_knowledge_base_catalog", fake_catalog)
    monkeypatch.setattr(
        multi_kb_search_tool, "_rag_service_base_url", lambda: "http://rag"
    )
    monkeypatch.setattr(
        multi_kb_search_tool, "_execute_remote_multi_kb_search", fake_remote
    )
    reset_turn_kb_context_budget()

    first = await execute_multi_kb_search(
        "stable cache budget query",
        kb_ids=["kb-1"],
        max_result_chars=5_000,
        _structured=True,
        _registry=SimpleNamespace(register=lambda *_args, **_kwargs: ""),
    )
    second = await execute_multi_kb_search(
        "stable cache budget query",
        kb_ids=["kb-1"],
        max_result_chars=5_000,
        _structured=True,
        _registry=SimpleNamespace(register=lambda *_args, **_kwargs: ""),
    )

    assert first["text"] != second["text"]
    assert "未再次占用检索上下文预算" in second["text"]
    assert second["stats"]["cache_hit"] == "exact"
    assert second["stats"]["cache_reused"] is True
    assert remote_calls == 1
    assert get_turn_kb_chars_remaining(TURN_MAX_EVIDENCE_CHARS) == (
        TURN_MAX_EVIDENCE_CHARS - 4_000
    )


def test_cached_reuse_message_includes_reference_block() -> None:
    from backend.application.tools.providers.multi_kb_search_tool import (
        _cached_reuse_result,
    )

    reused = _cached_reuse_result(
        {
            "text": "original",
            "evidence_records": [
                {"doc_title": "24_环境土壤学 第2版，第 300 页"},
                {"doc_title": "24_环境土壤学 第2版，第 300 页"},
                {"doc_title": "28_地下水与土壤修复，第 236 页"},
            ],
            "stats": {},
        },
        "exact",
    )
    assert "## 可引用文献" in reused["text"]
    assert "- [1] 24_环境土壤学 第2版，第 300 页" in reused["text"]
    assert "- [2] 28_地下水与土壤修复，第 236 页" in reused["text"]


def test_turn_kb_budget_reservation_is_atomic_across_copied_contexts() -> None:
    reset_turn_kb_context_budget()
    contexts = [copy_context(), copy_context()]
    reserved: list[int] = []

    def reserve(context) -> None:
        reserved.append(
            context.run(
                reserve_turn_kb_chars,
                600,
                turn_max=1_000,
                minimum_chars=1,
            )
        )

    workers = [
        threading.Thread(target=reserve, args=(context,)) for context in contexts
    ]
    for worker in workers:
        worker.start()
    for worker in workers:
        worker.join(timeout=1)

    assert sorted(reserved) == [400, 600]
    assert get_turn_kb_chars_remaining(1_000) == 0


def test_coordinated_kb_budget_is_shared_but_capped_per_branch() -> None:
    reset_turn_kb_context_budget(max_chars=10_000)

    first_token = set_turn_kb_context_scope("branch-1", max_chars=4_000)
    try:
        assert reserve_turn_kb_chars(5_000, turn_max=8_000, minimum_chars=1) == 4_000
        assert get_turn_kb_chars_remaining(8_000) == 0
    finally:
        reset_turn_kb_context_scope(first_token)

    second_token = set_turn_kb_context_scope("branch-2", max_chars=4_000)
    try:
        assert get_turn_kb_chars_remaining(8_000) == 4_000
        assert reserve_turn_kb_chars(4_000, turn_max=8_000, minimum_chars=1) == 4_000
    finally:
        reset_turn_kb_context_scope(second_token)

    assert get_turn_kb_chars_remaining(8_000) == 2_000


def test_first_oversized_kb_fragment_is_bounded_and_marked_truncated() -> None:
    kept, truncated = _select_fragment_entries(
        None,
        [("Detector handbook", "x" * 2_000)],
        500,
    )

    assert truncated is True
    assert len(_format_fragment(1, *kept[0])) <= 500
    assert "片段因上下文预算截断" in kept[0][1]


def test_first_oversized_fallback_context_is_bounded() -> None:
    kept, truncated = _fit_whole_parts(["x" * 2_000], 400)

    assert truncated is True
    assert len(kept[0]) <= 400
    assert "片段因上下文预算截断" in kept[0]


def test_web_search_relevance_filter_drops_off_topic_results() -> None:
    query = "重金属污染土壤 异位化学淋洗 螯合剂 去除率 实验研究 2024 2025"
    results = [
        {
            "title": "虎牙直播梗介绍",
            "url": "https://zhidao.baidu.com/question/2215496668177481308.html",
            "content": "LOL 虎牙四犬是什么梗，直播平台热词介绍。",
        },
        {
            "title": "绿色螯合剂 GLDA 在土壤淋洗中的应用",
            "url": "https://example.com/glda-soil-washing",
            "content": "重金属污染土壤异位化学淋洗实验：GLDA 去除率与螯合剂浓度关系。",
        },
        {
            "title": "ex 网络用语",
            "url": "https://baike.baidu.com/item/ex/23641953",
            "content": "ex 是英语单词，常被用作网络用语表示前任。",
        },
    ]
    kept, dropped = _filter_relevant_results(query, results)
    assert dropped == 2
    assert len(kept) == 1
    assert kept[0]["url"] == "https://example.com/glda-soil-washing"


async def test_web_search_no_progress_exhausts_budget(monkeypatch) -> None:
    async def fake_search(query, max_results=5, timeout=20, timelimit=None, isolated=False):
        return [
            {
                "title": "无关页面",
                "url": "https://junk.example/page",
                "content": "完全无关的娱乐八卦与体育赛事新闻内容。",
            }
        ]

    monkeypatch.setattr(web_search_mod, "_search_web_async", fake_search)
    budget_token = set_tool_budgets({"web_search": 5})
    request_budget_token = set_request_tool_budget({"web_search": 5})
    reset_web_search_no_progress_streak()
    try:
        first = await web_search_mod.async_web_search_tool.ainvoke(
            {"query": "重金属污染土壤异位化学淋洗去除率"}
        )
        assert "相关性过低" in first
        assert get_web_search_no_progress_streak() == 1
        assert get_tool_budget_remaining("web_search") == 4

        second = await web_search_mod.async_web_search_tool.ainvoke(
            {"query": "EDDS EDTA 混合淋洗剂实验"}
        )
        assert "已停止联网" in second
        assert get_web_search_no_progress_streak() == 2
        assert get_tool_budget_remaining("web_search") == 0
    finally:
        reset_tool_budgets(budget_token)
        reset_request_tool_budget(request_budget_token)
        reset_web_search_no_progress_streak()


def test_multi_kb_reference_block_notes_local_numbering() -> None:
    block = _build_reference_block(
        [("doc:1", 0.9)],
        chunk_contents={"doc:1": {"doc_name": "某文献", "page_nums": (3,)}},
        kb_display_names=set(),
    )
    assert block is not None
    assert "局部编号" in block
    assert "证据台账" in block
