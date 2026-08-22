from backend.application.tools.providers.multi_kb_search_tool import (
    _build_raw_chunk_evidence_records,
    _extract_explicit_document_codes,
    _merge_results_rrf,
)


def _result(*chunks: dict) -> list[dict]:
    return [{"success": True, "kb_id": "kb-test", "chunks": list(chunks)}]


def test_extracts_explicit_groundwater_standard_code() -> None:
    codes = _extract_explicit_document_codes("查询 GB/T 14848-2017 地下水 IV 类限值")

    assert codes == [("GB/T 14848-2017", "GBT148482017")]


def test_extracts_chinese_quoted_document_title() -> None:
    constraints = _extract_explicit_document_codes(
        "在《污染水文地质学》P53-54中查找算例"
    )

    assert constraints == [("污染水文地质学", "污染水文地质学")]


def test_marks_related_document_as_target_miss() -> None:
    context, _, _, _ = _merge_results_rrf(
        _result({"id": "1", "file_name": "HJ 1019-2019.pdf", "content": "地下水监测要求"}),
        required_document_codes=[("GB/T 14848-2017", "GBT148482017")],
    )

    assert "<target_document_match>false</target_document_match>" in context
    assert "不能作为该标准限值依据" in context


def test_prioritizes_exact_target_document_over_higher_ranked_related_document() -> None:
    context, _, _, _ = _merge_results_rrf(
        _result(
            {"id": "1", "file_name": "HJ 1019-2019.pdf", "content": "相关监测要求"},
            {"id": "2", "file_name": "GB-T 14848-2017.pdf", "content": "地下水 IV 类限值"},
        ),
        final_top_n=1,
        required_document_codes=[("GB/T 14848-2017", "GBT148482017")],
    )

    assert "<target_document_match>true</target_document_match>" in context
    assert "地下水 IV 类限值" in context
    assert "相关监测要求" not in context


def test_prioritizes_quoted_book_title_with_numeric_file_prefix() -> None:
    context, _, _, _ = _merge_results_rrf(
        _result(
            {"id": "1", "file_name": "6_应用水文地质学.pdf", "content": "相关算例"},
            {
                "id": "2",
                "file_name": "2_污染水文地质学.pdf",
                "content": "目标算例结果 715 mg/L",
            },
        ),
        final_top_n=1,
        required_document_codes=_extract_explicit_document_codes(
            "查询《污染水文地质学》中的目标算例"
        ),
    )

    assert "<target_document_match>true</target_document_match>" in context
    assert "目标算例结果 715 mg/L" in context
    assert "相关算例" not in context


def test_prioritizes_answer_bearing_chunk_within_explicit_document() -> None:
    query = (
        "查询《污染水文地质学》中40cm填土圆筒、500s为48mg/L、"
        "4000s为252mg/L的弥散度结果"
    )
    context, kept, _, truncated = _merge_results_rrf(
        _result(
            {
                "id": "image-neighbor",
                "file_name": "2_污染水文地质学.pdf",
                "content": "污染物羽流示意图" + ("背景说明" * 600),
            },
            {
                "id": "formula-result",
                "file_name": "2_污染水文地质学.pdf",
                "content": (
                    "40 cm填土圆筒，500 s时48 mg/L，4000 s时252 mg/L；"
                    "计算结果 D_L=0.102 cm²/s。"
                ),
            },
        ),
        final_top_n=2,
        max_context_chars=520,
        required_document_codes=_extract_explicit_document_codes(query),
        query=query,
    )

    assert kept == 1
    assert truncated is True
    assert "D_L=0.102 cm²/s" in context
    assert "污染物羽流示意图" not in context


def test_promotes_strongest_query_supported_table_without_explicit_source() -> None:
    query = "请给出地质环境质量评价定权方法表"
    context, kept, _, truncated = _merge_results_rrf(
        _result(
            {
                "id": "generic",
                "file_name": "相关评价方法.pdf",
                "content": "地质环境质量评价的一般背景" + ("背景说明" * 180),
            },
            {"id": "d2", "content": "地下水评价指标说明"},
            {"id": "d3", "content": "污染风险评价说明"},
            {"id": "d4", "content": "环境质量评价计算案例"},
            {"id": "d5", "content": "层次分析方法说明"},
            {
                "id": "complete-table",
                "file_name": "9_地下水污染与防治.pdf",
                "content": (
                    "表7-1 地质环境质量评价定权方法表："
                    "1 专家打分法；2 调查统计法；3 序列综合法；4 公式法；"
                    "5 数理统计法；6 层次分析法；7 复杂度分析法。"
                ),
            },
        ),
        final_top_n=6,
        max_context_chars=500,
        query=query,
    )

    assert kept == 1
    assert truncated is True
    assert "1 专家打分法" in context
    assert "7 复杂度分析法" in context
    assert "一般背景" not in context


def test_raw_evidence_records_boost_query_supported_primary_chunk() -> None:
    query = "请给出地质环境质量评价定权方法表"
    records = _build_raw_chunk_evidence_records(
        _result(
            {
                "id": "generic",
                "content": "地质环境质量评价的一般背景",
            },
            {
                "id": "complete-table",
                "content": (
                    "表7-1 地质环境质量评价定权方法表："
                    "专家打分法、调查统计法、序列综合法、公式法、"
                    "数理统计法、层次分析法、复杂度分析法。"
                ),
            },
        ),
        per_kb_limit=6,
        query=query,
    )

    assert records[0]["chunk_id"] == "complete-table"
    assert records[0]["score"] > records[1]["score"]
    assert "query_supported_primary" in records[0]["tags"]
