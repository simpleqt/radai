from __future__ import annotations

import asyncio
import os
from pathlib import Path

import pytest

from eval.datasets.self_kb.eval_ragas_ablation import (
    AblatedSelfRetriever,
    COMPONENT_METRICS,
    _normalize_extra_body,
    build_retrieval_checks,
    composite_score,
    load_questions,
    parse_schemes,
    render_report,
)


def _self_kb_dataset_path(filename: str) -> Path:
    default_root = Path(__file__).parents[2] / "eval/datasets/self_kb"
    root = Path(os.getenv("SELF_KB_EVAL_DATASET_ROOT", default_root))
    path = root / filename
    if not path.is_file():
        pytest.skip(
            "optional self-KB evaluation dataset is unavailable; "
            "set SELF_KB_EVAL_DATASET_ROOT to run dataset contract checks"
        )
    return path


def test_composite_score_is_equal_weight_mean() -> None:
    scores = {metric: (index + 1) / 10 for index, metric in enumerate(COMPONENT_METRICS)}
    assert composite_score(scores) == pytest.approx(0.3)


def test_composite_score_requires_all_metrics() -> None:
    with pytest.raises(ValueError, match="缺少指标"):
        composite_score({"context_precision": 1.0})


def test_parse_schemes_rejects_unknown_scheme() -> None:
    with pytest.raises(ValueError, match="未知"):
        parse_schemes("full,legacy")


def test_parse_schemes_accepts_graph_optimization_profiles() -> None:
    assert parse_schemes(
        "entity_seed_only,graph_routed,graph_filtered,optimized_no_parent,optimized_no_rerank,optimized,optimized_rerank,pathrag_no_parent,pathrag"
    ) == [
        "entity_seed_only",
        "graph_routed",
        "graph_filtered",
        "optimized_no_parent",
        "optimized_no_rerank",
        "optimized",
        "optimized_rerank",
        "pathrag_no_parent",
        "pathrag",
    ]


class _FakeRetriever:
    async def _redis_chunk_scores(self, *_args, **_kwargs):
        return {"keyword": 1.0}

    async def _keyword_scores(self, *_args, **_kwargs):
        return {"mysql": 1.0}

    async def _call_milvus_chunk_scores(self, *_args, **_kwargs):
        return {"vector": 1.0}

    async def _call_milvus_media_scores(self, *_args, **_kwargs):
        return {"media-vector": 1.0}

    async def _media_scores(self, *_args, **_kwargs):
        return {"media-exact": 1.0}

    async def _call_milvus_entity_recall(self, *_args, **_kwargs):
        return {"chunk": 0.9}, ["entity-1"]

    async def _graph_chunk_scores(self, *_args, **_kwargs):
        return {"graph": 1.0}

    async def _fetch_graph_relationships(self, *_args, **_kwargs):
        return [{"relation_id": "r1"}]

    async def _fallback_chunks(self, *_args, **_kwargs):
        return ["fallback"]


def test_no_entity_keeps_graph_seed_but_removes_entity_score() -> None:
    fake = _FakeRetriever()
    AblatedSelfRetriever(fake, "no_entity")
    scores, seeds = asyncio.run(fake._call_milvus_entity_recall("query", 10, None))
    assert scores == {}
    assert seeds == ["entity-1"]


def test_no_keyword_disables_both_fast_and_fallback_keyword_paths() -> None:
    fake = _FakeRetriever()
    AblatedSelfRetriever(fake, "no_keyword")
    assert asyncio.run(fake._redis_chunk_scores([], 10)) == {}
    assert asyncio.run(fake._keyword_scores([], 10)) == {}


def test_vector_only_is_strict_chunk_vector_without_media_or_fallback() -> None:
    fake = _FakeRetriever()
    AblatedSelfRetriever(fake, "vector_only")
    assert asyncio.run(fake._call_milvus_chunk_scores("query", 10, None)) == {"vector": 1.0}
    assert asyncio.run(fake._call_milvus_media_scores("query", 10)) == {}
    assert asyncio.run(fake._media_scores("query", [], 10, media_refs=set())) == {}
    assert asyncio.run(fake._fallback_chunks([], 10)) == []


def test_media_only_is_strict_media_vector_without_chunk_vector_or_fallback() -> None:
    fake = _FakeRetriever()
    AblatedSelfRetriever(fake, "media_only")
    assert asyncio.run(fake._call_milvus_media_scores("query", 10)) == {"media-vector": 1.0}
    assert asyncio.run(fake._call_milvus_chunk_scores("query", 10, None)) == {}
    assert asyncio.run(fake._fallback_chunks([], 10)) == []


@pytest.mark.parametrize(
    ("scheme", "profile"),
    [
        ("entity_seed_only", "seed_only"),
        ("graph_routed", "routed"),
        ("graph_filtered", "filtered"),
        ("optimized_no_parent", "optimized"),
        ("optimized_no_rerank", "optimized"),
        ("optimized", "optimized"),
        ("optimized_rerank", "optimized"),
        ("pathrag_no_parent", "pathrag"),
        ("pathrag", "pathrag"),
    ],
)
def test_graph_ablation_scheme_selects_retriever_profile(scheme: str, profile: str) -> None:
    fake = _FakeRetriever()
    AblatedSelfRetriever(fake, scheme)
    assert fake._graph_profile == profile


def test_optimized_ablation_controls_second_stage_reranker() -> None:
    without_rerank = _FakeRetriever()
    AblatedSelfRetriever(without_rerank, "optimized_no_rerank")
    assert without_rerank._rerank_enabled is False

    optimized = _FakeRetriever()
    AblatedSelfRetriever(optimized, "optimized")
    assert optimized._rerank_enabled is False

    with_rerank = _FakeRetriever()
    AblatedSelfRetriever(with_rerank, "optimized_rerank")
    assert with_rerank._rerank_enabled is True


def test_parent_window_ablation_disables_only_parent_expansion() -> None:
    fake = _FakeRetriever()
    fake._parent_expansion_enabled = True

    AblatedSelfRetriever(fake, "optimized_no_parent")

    assert fake._graph_profile == "optimized"
    assert fake._rerank_enabled is False
    assert fake._parent_expansion_enabled is False


def test_pathrag_parent_window_ablation_disables_only_parent_expansion() -> None:
    fake = _FakeRetriever()
    fake._parent_expansion_enabled = True

    AblatedSelfRetriever(fake, "pathrag_no_parent")

    assert fake._graph_profile == "pathrag"
    assert fake._rerank_enabled is False
    assert fake._parent_expansion_enabled is False


def test_registry_extra_body_gateway_wrapper_is_preserved() -> None:
    assert _normalize_extra_body(
        {"extra_body": {"chat_template_kwargs": {"enable_thinking": False}}}
    ) == {"extra_body": {"chat_template_kwargs": {"enable_thinking": False}}}


def test_multi_evidence_checks_distinguish_any_hit_coverage_and_all_hit() -> None:
    trace = [
        {
            "chunk_id": "gold-a",
            "doc_id": "doc-a",
            "context_chunk_ids": ["gold-a", "gold-b"],
        },
        {
            "chunk_id": "other",
            "doc_id": "doc-b",
            "context_chunk_ids": ["other"],
        },
    ]

    checks = build_retrieval_checks(
        trace,
        expected_chunk_ids=["gold-a", "gold-b", "gold-c"],
        expected_doc_ids=["doc-a"],
        contexts=["text"],
    )

    assert checks["target_chunk_hit"] is True
    assert checks["target_chunk_coverage"] == pytest.approx(1 / 3, abs=1e-6)
    assert checks["target_all_chunks_hit"] is False
    assert checks["target_evidence_hit"] is True
    assert checks["target_evidence_coverage"] == pytest.approx(2 / 3, abs=1e-6)
    assert checks["target_all_evidence_hit"] is False
    assert checks["target_doc_hit"] is True
    assert checks["target_doc_coverage"] == pytest.approx(1.0, abs=1e-6)
    assert checks["target_all_docs_hit"] is True


def test_doc_coverage_tracks_missing_gold_document() -> None:
    # Cross-document question: only one of the two gold documents surfaced.
    trace = [
        {"chunk_id": "gold-a", "doc_id": "doc-a", "context_chunk_ids": []},
        {"chunk_id": "noise", "doc_id": "doc-x", "context_chunk_ids": []},
    ]

    checks = build_retrieval_checks(
        trace,
        expected_chunk_ids=["gold-a", "gold-b"],
        expected_doc_ids=["doc-a", "doc-b"],
        contexts=["text"],
    )

    assert checks["target_doc_hit"] is True
    assert checks["target_doc_coverage"] == pytest.approx(0.5, abs=1e-6)
    assert checks["target_all_docs_hit"] is False


def test_extended_dataset_has_hard_and_targeted_image_questions() -> None:
    path = _self_kb_dataset_path("hydrogeology_extended_queries.jsonl")
    questions = load_questions(path, None)
    assert len(questions) == 53
    assert sum(item["difficulty"] == "easy" for item in questions) == 20
    assert sum(item["difficulty"] == "hard" for item in questions) == 22
    image_questions = [item for item in questions if item["modality"] == "image"]
    assert len(image_questions) == 11
    assert all(item["expected_chunk_ids"] for item in image_questions)
    grounded_text = [
        item
        for item in questions
        if item["modality"] == "text" and item["expected_chunk_ids"]
    ]
    assert len(grounded_text) >= 17


def test_journal_dataset_has_independent_hard_and_image_questions() -> None:
    path = _self_kb_dataset_path("hydrogeology_journal_hard_queries.jsonl")
    questions = load_questions(path, None)

    assert len(questions) == 16
    assert sum(item["difficulty"] == "hard" for item in questions) == 10
    assert sum(item["difficulty"] == "image" for item in questions) == 6
    assert all(item["expected_chunk_ids"] for item in questions)
    assert all(item["expected_doc_ids"] for item in questions)


def test_journal_expanded_dataset_has_50_grounded_hard_questions() -> None:
    path = _self_kb_dataset_path("hydrogeology_journal_hard_56q.jsonl")
    questions = load_questions(path, None)
    hard = [item for item in questions if item["difficulty"] == "hard"]
    generated = [item for item in hard if int(item["id"].rsplit("-", 1)[1]) >= 11]

    assert len(questions) == 56
    assert len(hard) == 50
    assert sum(item["modality"] == "image" for item in questions) == 6
    assert len(generated) == 40
    assert all(2 <= len(item["expected_chunk_ids"]) <= 3 for item in generated)
    assert all(len(item["expected_doc_ids"]) == 1 for item in generated)


def test_render_report_includes_composite_definition() -> None:
    summary = {
        "avg_composite": 0.5,
        "avg_context_precision": 0.5,
        "avg_context_recall": 0.5,
        "avg_answer_relevancy": 0.5,
        "avg_faithfulness": 0.5,
        "avg_answer_correctness": 0.5,
        "avg_retrieval_composite": 0.5,
        "avg_generation_composite": 0.5,
        "completed": 1,
        "errors": 0,
        "avg_retrieval_latency": 0.1,
    }
    payload = {
        "meta": {
            "kb": {
                "kb_name": "test",
                "kb_id": "kb",
                "ingestion_backend": "self_ingestion",
                "retrieval_backend": "self_ingestion",
                "document_count": 1,
                "chunk_count": 1,
            },
            "num_questions": 1,
            "top_k": 1,
            "eval_llm_model": "judge",
            "embedding_model": "embedding",
            "schemes": ["full"],
        },
        "results": {"full": {"label": "full", "summary": summary}},
    }
    assert "五项等权平均" in render_report(payload)
