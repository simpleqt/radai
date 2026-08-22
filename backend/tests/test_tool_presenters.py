"""工具展示 presenter 的契约测试。

覆盖三件事：载荷形状合法、上限被强制执行、presenter 异常不影响主流程。
"""

from __future__ import annotations

import json

import pytest

from backend.application.agent_executors.stream_utils import make_process_event
from backend.application.agent_executors.tool_presenters import (
    build_display_payload,
    has_presenter,
    output_has_error,
    resolve_phase,
)
from backend.application.agent_executors.tool_presenters.course_eval import (
    LABELS,
    PHASES,
)
from backend.application.agent_executors.tool_presenters.types import (
    MAX_CHIPS,
    MAX_DISPLAY_BYTES,
    MAX_ISSUE_ITEMS,
    MAX_TABLE_ROWS,
    normalize_display_payload,
)

_VIEWS = {"text", "kv", "table", "issues", "sources", "artifact"}


def _assert_valid_payload(payload: dict) -> None:
    assert isinstance(payload["label"], str) and payload["label"]
    assert len(json.dumps(payload, ensure_ascii=False).encode()) <= MAX_DISPLAY_BYTES
    assert len(payload.get("chips", [])) <= MAX_CHIPS

    view = payload.get("view")
    if view is None:
        return
    assert view in _VIEWS
    data = payload["data"]
    if view == "table":
        assert data["columns"] and data["rows"]
        assert len(data["rows"]) <= MAX_TABLE_ROWS
    elif view == "issues":
        assert len(data["items"]) <= MAX_ISSUE_ITEMS
    elif view == "text":
        assert isinstance(data["text"], str)


# ── 课程评价流水线 ────────────────────────────────────────────────────

SAMPLES: dict[str, dict] = {
    "create_semester_task": {
        "semester_task_id": "t-1",
        "course_id": "c-1",
        "course_code": "AUTO-AB12CD34",
        "course_name": "机械原理",
        "semester": "2025-2026-1",
        "class_no": "01",
        "status": "created",
        "auto_generated": True,
    },
    "attach_material": {
        "material_id": "m-123456789",
        "material_type": "paper",
        "version_no": 2,
    },
    "extract_grade_sheet": {
        "written": 45,
        "records": 47,
        "summary": {"平均分": "78.5"},
        "meta": {},
    },
    "extract_paper": {
        "pages": 12,
        "cover_pages": [1],
        "score_table_pages": [1],
        "sections": [
            {
                "order_no": 1,
                "question_type": "选择题",
                "total_score": 20,
                "applicable_majors": [],
                "counted_in_sample": True,
            }
        ],
        "paper_sample_n": 45,
        "score_cells": 180,
        "pending_review_cells": 7,
        "auto_extract_warnings": ["第 3 页封面模糊"],
    },
    "extract_syllabus": {
        "written": 5,
        "course": {"name": "机械原理"},
        "score_formula": {"exam": "0.7"},
    },
    "run_validation": {
        "issues": [
            {
                "rule_id": "V-01",
                "level": "error",
                "target": "student:S001",
                "detail": "各题之和与总分不符",
                "candidates": [],
            }
        ],
        "error_count": 1,
        "passed": False,
    },
    "get_score_grid": {
        "students": 45,
        "sections": 6,
        "low_confidence_cells": [
            {
                "student_id": "s-1",
                "student_name": "张三",
                "section_id": "sec-1",
                "item_id": "",
                "score": "12",
                "raw_score": "12",
                "confidence": "0.62",
            }
        ],
        "row_checks_failed": [
            {
                "student_id": "s-2",
                "student_name": "李四",
                "sum_of_sections": "78",
                "reported_total": "80",
            }
        ],
    },
    "update_scores": {"updated": 3},
    "confirm_scores": {"confirmed": 7},
    "suggest_goal_mapping": {
        "suggestions": [{"section_id": "sec-1", "goal_id": "g-1", "weight": 0.4}],
        "excluded_sections": "第 2 题本班不考，未纳入映射",
    },
    "apply_mappings": {"updated": 6},
    "compute_statistics": {"snapshot_id": "snap-abcdef123"},
    "get_course_stats": {
        "scope": "class",
        "final_score_stats": {"average": "78.5", "pass_rate": "0.91"},
        "section_rates": [{"order_no": 1, "rate": "0.82"}],
        "blocking_issues": None,
    },
    "get_achievement": {
        "items": [
            {
                "goal_code": "CG1",
                "method": "course_average",
                "achievement": "0.78",
                "support_score": "62.4",
                "rate": "0.78",
                "blocking": None,
            }
        ]
    },
    "generate_report": {
        "report_id": "r-987654321",
        "status": "draft",
        "goal_achievements": [{"goal_code": "CG1"}],
        "blocking_issues": None,
    },
    "draft_report_narrative": {"section": "problems", "draft": "部分学生计算题失分集中。"},
    "check_report_quality": {
        "issues": [{"rule_id": "QC-03", "level": "warning", "detail": "缺少签署栏"}],
        "passed": False,
    },
    "transition_task": {"semester_task_id": "t-1", "status": "computed"},
    "list_course_eval_tasks": {
        "items": [
            {
                "semester_task_id": "t-1",
                "course_id": "c-1",
                "semester": "2025-2026-1",
                "class_no": "01",
                "status": "computed",
            }
        ],
        "total": 1,
    },
    "load_previous_reports": {
        "course_id": "c-1",
        "goals": [{"goal_code": "CG1", "weight_exam": "0.7"}],
        "note": "仅返回目标与权重摘要",
    },
}


def test_course_eval_tools_all_have_presenter_and_phase() -> None:
    assert set(SAMPLES) == set(LABELS) == set(PHASES)


@pytest.mark.parametrize("tool_name", sorted(SAMPLES))
def test_course_eval_presenter_payload_is_valid(tool_name: str) -> None:
    payload = build_display_payload(
        tool_name, raw_output=json.dumps(SAMPLES[tool_name], ensure_ascii=False)
    )
    assert payload is not None
    _assert_valid_payload(payload)
    # draft_report_narrative 会在步骤名后追加章节，其余工具与登记名一致。
    assert payload["label"].startswith(LABELS[tool_name])
    assert resolve_phase(tool_name) == PHASES[tool_name]


@pytest.mark.parametrize(
    # extract_paper 不再带确认动作：核对由随后的 get_score_grid 卡统一承担，
    # 避免同一批待核对格子弹两张确认卡
    "tool_name", ["run_validation", "get_score_grid", "suggest_goal_mapping"]
)
def test_review_steps_expose_action(tool_name: str) -> None:
    payload = build_display_payload(
        tool_name, raw_output=json.dumps(SAMPLES[tool_name], ensure_ascii=False)
    )
    action = payload["action"]
    assert action["prompt"]
    assert action.get("options") or action.get("input_template")


def test_extract_paper_exposes_no_duplicate_confirm_action() -> None:
    """extract_paper 不带确认动作（get_score_grid 卡已含明细与确认按钮）。"""
    payload = build_display_payload(
        "extract_paper",
        raw_output=json.dumps(SAMPLES["extract_paper"], ensure_ascii=False),
    )
    assert not payload.get("action")


def test_raw_json_never_leaks_into_payload() -> None:
    """展示载荷里不应出现内部标识符和原始 JSON 键名。"""
    for tool_name, sample in SAMPLES.items():
        payload = build_display_payload(
            tool_name, raw_output=json.dumps(sample, ensure_ascii=False)
        )
        text = json.dumps(payload, ensure_ascii=False)
        assert '"next"' not in text
        assert "semester_task_id" not in text


def test_tool_error_falls_back_to_readable_message() -> None:
    payload = build_display_payload(
        "extract_paper",
        raw_output=json.dumps({"error": "未找到该任务的试卷材料"}, ensure_ascii=False),
    )
    assert payload["label"] == LABELS["extract_paper"]
    assert payload["data"]["text"] == "未找到该任务的试卷材料"
    assert output_has_error(json.dumps({"error": "x"}))
    assert not output_has_error(json.dumps({"ok": 1}))


# ── 通用回落与上限 ────────────────────────────────────────────────────


def test_unregistered_tool_without_error_has_no_display() -> None:
    assert build_display_payload("web_search", raw_output="plain text") is None
    assert build_display_payload("web_search", raw_output='{"results": []}') is None


def test_unregistered_tool_with_json_error_gets_display() -> None:
    payload = build_display_payload("some_tool", raw_output='{"error": "配额不足"}')
    assert payload is not None
    assert "配额不足" in json.dumps(payload, ensure_ascii=False)
    # 兜底不算专属 presenter：调用方据此保留检索类工具原有的结果摘要事件。
    assert not has_presenter("some_tool")
    assert has_presenter("extract_paper")


def test_table_rows_are_capped_and_marked_truncated() -> None:
    payload = normalize_display_payload(
        {
            "label": "大表",
            "view": "table",
            "data": {
                "columns": [{"key": "n", "label": "序号"}],
                "rows": [{"n": index} for index in range(200)],
            },
        }
    )
    assert len(payload["data"]["rows"]) == MAX_TABLE_ROWS
    assert payload["data"]["total_rows"] == 200
    assert payload["data"]["truncated"] is True


def test_oversized_payload_degrades_instead_of_bloating_the_frame() -> None:
    payload = normalize_display_payload(
        {
            "label": "超大载荷",
            "status_text": "摘要仍然保留",
            "view": "issues",
            "data": {
                "items": [
                    {"level": "error", "message": "x" * 200, "locator": "y" * 60}
                    for _ in range(MAX_ISSUE_ITEMS)
                ]
            },
        }
    )
    assert len(json.dumps(payload, ensure_ascii=False).encode()) <= MAX_DISPLAY_BYTES
    assert payload["status_text"] == "摘要仍然保留"


def test_invalid_view_data_falls_back_to_text() -> None:
    payload = normalize_display_payload(
        {"label": "坏表", "status_text": "有摘要", "view": "table", "data": {"junk": 1}}
    )
    assert payload["view"] == "text"
    assert payload["data"]["text"] == "有摘要"


def test_presenter_exception_does_not_break_stream(monkeypatch: pytest.MonkeyPatch) -> None:
    from backend.application.agent_executors import tool_presenters

    def boom(_payload):
        raise RuntimeError("presenter 崩了")

    monkeypatch.setitem(tool_presenters._REGISTRY, "extract_paper", boom)
    assert build_display_payload("extract_paper", raw_output="{}") is None


# ── 事件契约 ──────────────────────────────────────────────────────────


def test_make_process_event_carries_phase_and_display() -> None:
    event = make_process_event(
        kind="tool_end",
        title="工具调用完成",
        detail="extract_paper 调用完成",
        tool="extract_paper",
        phase="material.extract",
        display={"label": "解析答题试卷", "status_text": "已完成"},
    )
    assert event["phase"] == "material.extract"
    assert event["display"]["label"] == "解析答题试卷"


def test_make_process_event_drops_invalid_display() -> None:
    event = make_process_event(
        kind="tool_end",
        title="工具调用完成",
        detail="调用完成",
        tool="whatever",
        display={"status_text": "没有 label"},
    )
    assert "display" not in event
