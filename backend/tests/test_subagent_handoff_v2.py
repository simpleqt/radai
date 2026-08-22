import json
import re

import pytest

from backend.core.agents.context.subagent_adapter import (
    SubagentResultValidationError,
    build_subagent_result,
    format_subagent_handoff,
    normalize_subagent_result,
    validate_subagent_result_strict,
)
from backend.core.agents.context.types import SubagentResultV2Model


def test_subagent_handoff_v2_exposes_machine_readable_coverage():
    handoff = build_subagent_result(
        role="researcher",
        task_id="task-1",
        objective="核验标准 A 的限值",
        answer="结论：标准 A 的限值为 17 mg/L。",
        evidence_records=[
            {
                "source_id": "kb-a",
                "chunk_id": "chunk-1",
                "doc_title": "标准 A",
                "claim": "标准 A 的限值为 17 mg/L",
            }
        ],
        raw_artifact_refs=["artifact://context/thread/tool-raw.txt"],
    )

    rendered = format_subagent_handoff(handoff)
    match = re.search(
        r"<subagent_result_json>\s*(\{.*?\})\s*</subagent_result_json>",
        rendered,
        re.S,
    )

    assert handoff["schema_version"] == "2.0"
    assert handoff["task_id"] == "task-1"
    assert handoff["source_coverage"]["total_claims"] >= 1
    assert handoff["source_coverage"]["kb_refs"] >= 1
    assert handoff["source_coverage"]["raw_artifact_refs"] == 1
    assert match is not None
    payload = json.loads(match.group(1))
    assert payload["schema_version"] == "2.0"
    assert payload["source_coverage"]["kb_refs"] >= 1


def test_subagent_handoff_v2_normalizes_json_and_warns_on_unsupported_claim():
    handoff = normalize_subagent_result(
        {
            "task_id": "task-json",
            "summary": "已核验，但仍有一个断言缺少引用。",
            "claims": [{"claim": "缺少引用的断言", "supporting_refs": []}],
            "confidence": 0.6,
        },
        role="researcher",
        objective="核验标准",
        answer_fallback="fallback",
    )

    assert handoff["schema_version"] == "2.0"
    assert handoff["task_id"] == "task-json"
    assert handoff["source_coverage"]["unsupported_claims"] == 1
    assert handoff["validation_warnings"] == ["1 claim(s) have no supporting_refs"]

    strict = validate_subagent_result_strict(handoff)
    assert strict.source_coverage.unsupported_claims == 1
    assert strict.validation_warnings == ["1 claim(s) have no supporting_refs"]


def test_normalize_rebinds_empty_claim_refs_from_evidence_records():
    handoff = normalize_subagent_result(
        {
            "task_id": "task-rebind",
            "summary": "模型返回了结论但漏写 supporting_refs",
            "claims": [
                {
                    "claim": "标准 A 的限值为 17 mg/L",
                    "supporting_refs": [],
                    "confidence": 0.7,
                }
            ],
            "confidence": 0.7,
        },
        role="researcher",
        objective="核验标准 A 的限值",
        answer_fallback="结论：标准 A 的限值为 17 mg/L。",
        evidence_records=[
            {
                "source_id": "kb-a",
                "chunk_id": "chunk-1",
                "doc_title": "标准 A",
                "content": "标准 A 的限值为 17 mg/L。",
            }
        ],
    )

    assert handoff["claims"][0]["supporting_refs"] == ["[标准 A#chunk-1]"]
    assert handoff["source_coverage"]["unsupported_claims"] == 0
    assert "[标准 A#chunk-1]" in handoff["evidence_refs"]


def test_subagent_handoff_v2_strict_validation_returns_runtime_model():
    handoff = build_subagent_result(
        role="researcher",
        task_id="task-strict",
        objective="核验标准 A 的限值",
        answer="结论：标准 A 的限值为 17 mg/L。",
        evidence_records=[
            {
                "source_id": "kb-a",
                "chunk_id": "chunk-1",
                "doc_title": "标准 A",
                "claim": "标准 A 的限值为 17 mg/L",
            }
        ],
    )

    validated = validate_subagent_result_strict(handoff)

    assert isinstance(validated, SubagentResultV2Model)
    assert validated.schema_version == "2.0"
    assert validated.task_id == "task-strict"
    assert validated.claims[0].supporting_refs == ["[标准 A#chunk-1]"]
    assert validated.source_coverage.total_claims == len(validated.claims)


def test_subagent_handoff_v2_strict_validation_rejects_coercion_and_extra_fields():
    handoff = build_subagent_result(
        role="researcher",
        task_id="task-invalid",
        objective="核验标准 A 的限值",
        answer="结论：标准 A 的限值为 17 mg/L。",
        evidence_refs=["https://example.test/standard-a"],
    )
    invalid = {
        **handoff,
        "schema_version": "1.0",
        "confidence": "0.8",
        "unexpected": True,
    }

    with pytest.raises(SubagentResultValidationError) as exc_info:
        validate_subagent_result_strict(invalid)

    assert any(
        detail.startswith("schema_version:") for detail in exc_info.value.details
    )
    assert any(detail.startswith("confidence:") for detail in exc_info.value.details)
    assert any(detail.startswith("unexpected:") for detail in exc_info.value.details)
    assert "Invalid SubagentResult V2" in str(exc_info.value)


def test_subagent_handoff_v2_strict_validation_rejects_inconsistent_coverage():
    handoff = build_subagent_result(
        role="researcher",
        task_id="task-coverage",
        objective="核验标准 A 的限值",
        answer="结论：标准 A 的限值为 17 mg/L。",
        evidence_refs=["https://example.test/standard-a"],
    )
    handoff["source_coverage"] = {
        **handoff["source_coverage"],
        "total_claims": 99,
    }

    with pytest.raises(SubagentResultValidationError) as exc_info:
        validate_subagent_result_strict(handoff)

    assert "source_coverage.total_claims must be" in str(exc_info.value)


def test_subagent_handoff_v2_keeps_legacy_text_fallback_compatible():
    handoff = normalize_subagent_result(
        "旧子智能体返回的非 JSON 文本",
        role="researcher",
        objective="复核设备状态",
        answer_fallback="结论：设备状态正常，但仍需进行现场复核。",
        task_id="task-legacy",
        evidence_refs=["[现场记录#chunk-1]"],
    )

    assert handoff["schema_version"] == "2.0"
    assert handoff["task_id"] == "task-legacy"
    assert handoff["summary"] == "结论：设备状态正常，但仍需进行现场复核。"
    assert handoff["claims"]
    assert handoff["evidence_refs"] == ["[现场记录#chunk-1]"]


def test_single_branch_ref_does_not_auto_support_unrelated_claims():
    handoff = build_subagent_result(
        role="data_analyst",
        objective="核验项目数据",
        answer=(
            "结论：监测井 A 的浓度为 10 mg/L。\n"
            "结论：该项目完全符合所有法规要求。"
        ),
        evidence_refs=["[project-db:query_project_tables]"],
    )

    assert len(handoff["claims"]) == 2
    assert all(not claim["supporting_refs"] for claim in handoff["claims"])
    assert handoff["source_coverage"]["unsupported_claims"] == 2


def test_large_subagent_handoff_compacts_fields_without_corrupting_envelope():
    answer = "\n".join(
        f"结论：第 {index} 项核验结果为有效，来源：https://example.test/{index}/"
        + ("very-long-segment-" * 30)
        for index in range(24)
    )
    handoff = build_subagent_result(
        role="researcher",
        task_id="task-large",
        objective="核验一组较长的标准结果",
        answer=answer,
        evidence_refs=[
            f"https://example.test/reference/{index}/" + ("x" * 300)
            for index in range(20)
        ],
    )

    rendered = format_subagent_handoff(handoff, max_chars=6000)
    match = re.search(
        r"<subagent_result_json>\s*(\{.*?\})\s*</subagent_result_json>",
        rendered,
        re.S,
    )

    assert len(rendered) <= 6000
    assert rendered.endswith("</agent_handoff>")
    assert match is not None
    compact_payload = json.loads(match.group(1))
    strict = validate_subagent_result_strict(compact_payload)
    assert strict.task_id == "task-large"
    assert strict.claims
