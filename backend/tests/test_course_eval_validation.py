"""Tests for validation rule set (T2-04).

每条规则正反各一例（§4.4 断言强度要求第 4 条）；规则是纯函数，不写库。
"""

from __future__ import annotations

from decimal import Decimal

from backend.application.course_eval.extraction.validation import (
    ValidationContext,
    run_rules,
    rule_gs01_record_count_matches_summary,
    rule_gs03_recompute_matches_total,
    rule_gs06_unique_student_no,
    rule_gs07_scores_in_range,
    rule_ps01_score_within_range,
    rule_ps02_section_sum_matches_total,
    rule_ps05_section_count_matches_declared,
    rule_ps06_cross_check_with_grade_sheet,
)

# ── GS-01 ──


def test_gs01_positive():
    ctx = ValidationContext(
        records=[{"student_no": "1"}, {"student_no": "2"}], summary={"expected": 2}
    )
    assert rule_gs01_record_count_matches_summary(ctx) == []


def test_gs01_negative():
    ctx = ValidationContext(records=[{"student_no": "1"}], summary={"expected": 2})
    issues = rule_gs01_record_count_matches_summary(ctx)
    assert len(issues) == 1
    assert issues[0].rule_id == "GS-01" and issues[0].level == "error"


# ── GS-03 ──


def test_gs03_positive_recompute_matches():
    # 90×0.4 + 86×0.6 = 36 + 51.6 = 87.6 ≈ 88（容差 0.5 内）
    ctx = ValidationContext(
        records=[
            {"student_no": "a", "regular": 90, "final": 86, "total": 88, "flag": ""}
        ],
        regular_weight=Decimal("0.4"),
        exam_weight=Decimal("0.6"),
    )
    assert rule_gs03_recompute_matches_total(ctx) == []


def test_gs03_negative_mismatch():
    ctx = ValidationContext(
        records=[
            {"student_no": "a", "regular": 90, "final": 88, "total": 60, "flag": ""}
        ],
        regular_weight=Decimal("0.4"),
        exam_weight=Decimal("0.6"),
    )
    issues = rule_gs03_recompute_matches_total(ctx)
    assert len(issues) == 1
    assert issues[0].rule_id == "GS-03" and issues[0].level == "warning"


def test_gs03_skips_absent():
    ctx = ValidationContext(
        records=[
            {
                "student_no": "a",
                "flag": "缺考",
                "regular": None,
                "final": None,
                "total": None,
            }
        ],
    )
    assert rule_gs03_recompute_matches_total(ctx) == []


# ── GS-06 / GS-07 ──


def test_gs06_duplicate_detected():
    ctx = ValidationContext(records=[{"student_no": "1"}, {"student_no": "1"}])
    issues = rule_gs06_unique_student_no(ctx)
    assert issues and issues[0].rule_id == "GS-06"


def test_gs06_unique_ok():
    ctx = ValidationContext(records=[{"student_no": "1"}, {"student_no": "2"}])
    assert rule_gs06_unique_student_no(ctx) == []


def test_gs07_out_of_range_detected():
    ctx = ValidationContext(
        records=[{"student_no": "1", "regular": 101, "final": 90, "total": 88}]
    )
    issues = rule_gs07_scores_in_range(ctx)
    assert len(issues) == 1 and issues[0].rule_id == "GS-07"


def test_gs07_in_range_ok():
    ctx = ValidationContext(
        records=[{"student_no": "1", "regular": 100, "final": 0, "total": 88}]
    )
    assert rule_gs07_scores_in_range(ctx) == []


# ── PS-01 ──


def test_ps01_positive():
    ctx = ValidationContext(
        score_records=[{"student_id": "s1", "score": 16, "full_score": 20}]
    )
    assert rule_ps01_score_within_range(ctx) == []


def test_ps01_negative():
    ctx = ValidationContext(
        score_records=[{"student_id": "s1", "score": 21, "full_score": 20}]
    )
    issues = rule_ps01_score_within_range(ctx)
    assert (
        len(issues) == 1 and issues[0].rule_id == "PS-01" and issues[0].level == "error"
    )


# ── PS-02 ──


def test_ps02_warning_not_error():
    """PS-02 是 warning：纸面本身可能不一致（§1.2 实测 80 vs 78）。"""
    ctx = ValidationContext(
        score_records=[
            {"student_id": "s1", "item_id": "", "score": 16},
            {"student_id": "s1", "item_id": "", "score": 7},
        ],
        section_totals={"s1": Decimal("20")},  # 16+7=23 != 20
    )
    issues = rule_ps02_section_sum_matches_total(ctx)
    assert len(issues) == 1
    assert issues[0].rule_id == "PS-02" and issues[0].level == "warning"


def test_ps02_ok():
    ctx = ValidationContext(
        score_records=[{"student_id": "s1", "item_id": "", "score": 16}],
        section_totals={"s1": Decimal("16")},
    )
    assert rule_ps02_section_sum_matches_total(ctx) == []


# ── PS-06 ──


def _ps05_ctx(*, records_sections: list[str]) -> ValidationContext:
    return ValidationContext(
        score_records=[
            {"student_id": "st1", "section_id": sid, "item_id": "", "score": 1}
            for sid in records_sections
        ],
        sections=[
            {"section_id": "s1", "order_no": 1, "question_type": "汉译英",
             "applicable_majors": ["工程力学专业"]},
            {"section_id": "s2", "order_no": 2, "question_type": "填空题",
             "applicable_majors": None},
            {"section_id": "s3", "order_no": 3, "question_type": "计算题",
             "applicable_majors": None},
        ],
    )


def test_ps05_major_restricted_section_without_scores_is_expected():
    """专业专属题（本班不考）全班无得分不是异常，不应报错。"""
    ctx = _ps05_ctx(records_sections=["s2", "s3"])
    assert rule_ps05_section_count_matches_declared(ctx) == []


def test_ps05_missing_regular_section_reported_in_human_terms():
    """普通大题没有任何得分记录要报错，且报「第X题（题型）」而不是 UUID。"""
    ctx = _ps05_ctx(records_sections=["s2"])
    issues = rule_ps05_section_count_matches_declared(ctx)
    assert len(issues) == 1
    assert "第3题（计算题）" in issues[0].detail
    assert "s3" not in issues[0].detail  # 不暴露内部 ID
    assert issues[0].level == "error"


def test_ps05_undeclared_section_records_reported():
    ctx = _ps05_ctx(records_sections=["s2", "s3", "ghost"])
    issues = rule_ps05_section_count_matches_declared(ctx)
    assert any("未声明大题" in i.detail for i in issues)


def test_ps06_cross_check_passes():
    """110 分卷面折算到 100 分标准分后与成绩单期末一致。"""
    ctx = ValidationContext(
        score_records=[
            {"student_id": "s1", "item_id": "", "score": 88}
        ],  # 110 卷面得 88
        grade_sheet_finals={"s1": Decimal("80")},  # 88 × 100/110 = 80
        config={
            "paper_full_score": 110,
            "standard_total_score": 100,
            "ps06_tolerance": 0.5,
        },
    )
    assert rule_ps06_cross_check_with_grade_sheet(ctx) == []


def test_ps06_cross_check_fails():
    ctx = ValidationContext(
        score_records=[{"student_id": "s1", "item_id": "", "score": 50}],
        grade_sheet_finals={"s1": Decimal("80")},
        config={
            "paper_full_score": 110,
            "standard_total_score": 100,
            "ps06_tolerance": 0.5,
        },
    )
    issues = rule_ps06_cross_check_with_grade_sheet(ctx)
    assert len(issues) == 1 and issues[0].rule_id == "PS-06"


# ── run_rules 注册表 ──


def test_run_rules_by_registry():
    ctx = ValidationContext(
        records=[
            {"student_no": "1", "regular": 90, "final": 86, "total": 88, "flag": ""}
        ],
        summary={"expected": 1},
    )
    issues = run_rules(ctx, rule_ids=["GS-01", "GS-03", "GS-06", "GS-07"])
    assert issues == []


def test_run_rules_detects_violations():
    ctx = ValidationContext(
        records=[
            {"student_no": "1", "regular": 90, "final": 88, "total": 60, "flag": ""}
        ],
        summary={"expected": 2},
        regular_weight=Decimal("0.4"),
        exam_weight=Decimal("0.6"),
    )
    issues = run_rules(ctx)
    assert {i.rule_id for i in issues} >= {"GS-01", "GS-03"}
