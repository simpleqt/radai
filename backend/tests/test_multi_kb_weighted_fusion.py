from backend.application.tools.providers.multi_kb_search_tool import _merge_results_weighted

def test_merge_results_weighted_calibration():
    # Mock search results for two KBs: 'kb_ne' and 'kb_paper'
    results = [
        {
            "success": True,
            "kb_id": "kb_ne",
            "chunks": [
                {"content": "NE chunk 1", "id": "ne_1", "_fused_score": 10.0, "metadata": {"doc_name": "NE_Doc"}},
                {"content": "NE chunk 2", "id": "ne_2", "_fused_score": 5.0, "metadata": {"doc_name": "NE_Doc"}},
                {"content": "NE chunk 3", "id": "ne_3", "_fused_score": 0.0, "metadata": {"doc_name": "NE_Doc"}},
            ]
        },
        {
            "success": True,
            "kb_id": "kb_paper",
            "chunks": [
                {"content": "Paper chunk 1", "id": "paper_1", "_fused_score": 100.0, "metadata": {"doc_name": "Paper_Doc"}},
                {"content": "Paper chunk 2", "id": "paper_2", "_fused_score": 90.0, "metadata": {"doc_name": "Paper_Doc"}},
                {"content": "Paper chunk 3", "id": "paper_3", "_fused_score": 80.0, "metadata": {"doc_name": "Paper_Doc"}},
            ]
        }
    ]

    # Weights: NE has bias 1.5, Paper has 1.0
    kb_weights = {"kb_ne": 1.5, "kb_paper": 1.0}

    # Execute merge
    context, kept_count, total_count, truncated = _merge_results_weighted(
        results=results,
        final_top_n=10,
        kb_weights=kb_weights,
        max_context_chars=1000,
    )

    # For NE: max=10, min=0, diff=10.
    # Scores: ne_1 -> 1.0 * 1.5 = 1.5
    #         ne_2 -> 0.5 * 1.5 = 0.75
    #         ne_3 -> 0.0 * 1.5 = 0.0
    # For Paper: max=100, min=80, diff=20.
    # Scores: paper_1 -> 1.0 * 1.0 = 1.0
    #         paper_2 -> 0.5 * 1.0 = 0.5
    #         paper_3 -> 0.0 * 1.0 = 0.0
    # Total sorted: [ne_1 (1.5), paper_1 (1.0), ne_2 (0.75), paper_2 (0.5), ne_3 (0.0)/paper_3 (0.0)]
    assert "NE chunk 1" in context
    assert "Paper chunk 1" in context
    assert "NE chunk 2" in context
    assert "Paper chunk 2" in context


def test_merge_results_weighted_density_truncation():
    # Mock results with a clear drop-off after the 5th chunk (index 4)
    # Total sorted scores: [1.5, 1.4, 1.3, 1.2, 1.1, 0.2, 0.1, 0.0]
    # Drop-off at index 4 (1.1 -> 0.2, drop = 0.9, which is > 0.3 * 1.5 = 0.45)
    results = [
        {
            "success": True,
            "kb_id": "kb_ne",
            "chunks": [
                {"content": "NE 1", "id": "ne_1", "_fused_score": 10.0, "metadata": {"doc_name": "Doc"}},
                {"content": "NE 2", "id": "ne_2", "_fused_score": 9.3, "metadata": {"doc_name": "Doc"}},
                {"content": "NE 3", "id": "ne_3", "_fused_score": 8.6, "metadata": {"doc_name": "Doc"}},
                {"content": "NE 4", "id": "ne_4", "_fused_score": 8.0, "metadata": {"doc_name": "Doc"}},
                {"content": "NE 5", "id": "ne_5", "_fused_score": 7.3, "metadata": {"doc_name": "Doc"}},
                {"content": "NE 6", "id": "ne_6", "_fused_score": 1.3, "metadata": {"doc_name": "Doc"}},
                {"content": "NE 7", "id": "ne_7", "_fused_score": 0.6, "metadata": {"doc_name": "Doc"}},
                {"content": "NE 8", "id": "ne_8", "_fused_score": 0.0, "metadata": {"doc_name": "Doc"}},
            ]
        }
    ]

    kb_weights = {"kb_ne": 1.5}

    context, kept_count, total_count, truncated = _merge_results_weighted(
        results=results,
        final_top_n=10,
        kb_weights=kb_weights,
        max_context_chars=2000,
    )

    # With drop-off, the top-5 chunks should be kept (index 0, 1, 2, 3, 4)
    # The 6th chunk (index 5) should be truncated dynamically.
    assert kept_count == 5
    assert "NE 5" in context
    assert "NE 6" not in context


def test_merge_results_weighted_promotes_exact_evidence_within_selected_set():
    results = [
        {
            "success": True,
            "kb_id": "kb-csh",
            "chunks": [
                {
                    "content": "地质环境质量评价的一般背景" + ("背景" * 180),
                    "id": "generic",
                    "_fused_score": 1.0,
                    "metadata": {"doc_name": "相关文献"},
                },
                {
                    "content": (
                        "表7-1 地质环境质量评价定权方法表："
                        "专家打分法、调查统计法、序列综合法、公式法、"
                        "数理统计法、层次分析法、复杂度分析法。"
                    ),
                    "id": "exact",
                    "_fused_score": 0.8,
                    "metadata": {"doc_name": "地下水污染与防治"},
                },
            ],
        }
    ]

    context, kept_count, _, truncated = _merge_results_weighted(
        results=results,
        final_top_n=2,
        max_context_chars=500,
        query="请给出地质环境质量评价定权方法表",
    )

    assert kept_count == 1
    assert truncated is True
    assert "专家打分法" in context
    assert "复杂度分析法" in context
    assert "一般背景" not in context
