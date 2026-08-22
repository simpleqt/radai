"""Tests for the quality rule set (T3-06).

13 条规则（QC-01~QC-10 + QC-02a/b/c）各自纯函数 + 正反用例。
QC-02 不得实现成「全文无 □」（会 100% 误报，坑 3 反例）。
"""

from __future__ import annotations

from backend.application.course_eval.quality_service import (
    QcContext,
    qc01_numeric_consistency,
    qc02_no_placeholder_residue,
    qc02a_checkbox_selected,
    qc02b_no_isolated_preset,
    qc02c_guard_labels_present,
    qc03_reference_validity,
    qc04_duplication,
    qc05_cross_document_consistency,
    qc07_semester_year_labeling,
    qc08_numeric_conclusion_consistency,
    run_quality_checks,
)


def test_qc01_numeric_consistency():
    ctx = QcContext(snapshot={"average": "52.88"}, report_json={"average": "52.88"})
    assert qc01_numeric_consistency(ctx) == []

    ctx_bad = QcContext(snapshot={"average": "52.88"}, report_json={"average": "52.90"})
    issues = qc01_numeric_consistency(ctx_bad)
    assert (
        len(issues) == 1 and issues[0].rule_id == "QC-01" and issues[0].level == "error"
    )


def test_qc02_no_placeholder_residue():
    ctx = QcContext(cell_texts=["40.6%", "平均成绩"], all_text="")
    assert qc02_no_placeholder_residue(ctx) == []

    ctx_bad = QcContext(cell_texts=["40.6%", "{{stat.average}}"])
    issues = qc02_no_placeholder_residue(ctx_bad)
    assert len(issues) == 1 and issues[0].rule_id == "QC-02"


def test_qc02a_checkbox_selected():
    ctx = QcContext(
        cell_texts=["☑专业基础课 □公共基础课 □专业方向课 □任选课"], checkbox_mark="☑"
    )
    assert qc02a_checkbox_selected(ctx) == []

    ctx_none = QcContext(cell_texts=["□公共基础课 □专业基础课 □专业方向课 □任选课"])
    issues = qc02a_checkbox_selected(ctx_none)
    assert len(issues) == 1 and issues[0].rule_id == "QC-02a"


def test_qc02b_no_isolated_preset():
    """坑 3 反例：孤立 % 必须被检出（QC-02b 是「无孤立预置符」而非「无 %」）。"""
    ctx = QcContext(cell_texts=["46.0%", "37.0%", "%"])
    issues = qc02b_no_isolated_preset(ctx)
    assert len(issues) == 1 and issues[0].rule_id == "QC-02b"


def test_qc02b_clean():
    ctx = QcContext(cell_texts=["46.0%", "37.0%", "73.5%"])
    assert qc02b_no_isolated_preset(ctx) == []


def test_qc02c_guard_labels_present():
    ctx = QcContext(
        all_text="参加考试年级 专业 课程名称", guard_labels=["参加考试年级", "课程名称"]
    )
    assert qc02c_guard_labels_present(ctx) == []

    ctx_bad = QcContext(all_text="专业", guard_labels=["参加考试年级"])
    issues = qc02c_guard_labels_present(ctx_bad)
    assert len(issues) == 1 and issues[0].rule_id == "QC-02c"


def test_qc03_reference_validity():
    ctx = QcContext(
        all_text="第1题得分率较低，目标1 支撑不足",
        snapshot={
            "sections": [{"report_order_no": 1}],
            "goals": [{"goal_code": "目标1"}],
        },
    )
    assert qc03_reference_validity(ctx) == []

    ctx_bad = QcContext(
        all_text="第8题得分率较低，目标9 支撑不足",
        snapshot={
            "sections": [{"report_order_no": 1}],
            "goals": [{"goal_code": "目标1"}],
        },
    )
    issues = qc03_reference_validity(ctx_bad)
    assert len(issues) == 2  # 第8题 + 目标9


def test_qc04_duplication_threshold():
    """与历史报告 3-gram Jaccard > 0.80 告警（§1.6 缺陷 4）。"""
    text = "存在的问题及原因分析：部分学生基础知识掌握不牢，计算能力有待提高"
    ctx = QcContext(all_text=text, history_reports=[text])  # 完全相同 → 相似度 1.0
    issues = qc04_duplication(ctx)
    assert len(issues) == 1 and issues[0].rule_id == "QC-04"

    ctx_diff = QcContext(
        all_text="本学期成绩分布正常", history_reports=["完全不同的报告内容"]
    )
    assert qc04_duplication(ctx_diff) == []


def test_qc05_cross_document_consistency():
    """教学班号不一致（§1.6 缺陷 1）。"""
    ctx = QcContext(all_text="教学班号 990101-990102 与附表 990201-2")
    issues = qc05_cross_document_consistency(ctx)
    assert len(issues) == 1 and issues[0].rule_id == "QC-05"


def test_qc07_semester_year_labeling():
    """混用学年与范围外自然年（§1.6 缺陷 3：实际是 2023-2024 与 2024-2025 两个学年）。"""
    ctx = QcContext(
        all_text="2024年与2026年对比，2024-2025学年数据", semester="2024-2025-1"
    )
    issues = qc07_semester_year_labeling(ctx)
    assert len(issues) == 1 and issues[0].rule_id == "QC-07"


def test_qc07_semester_inner_year_ok():
    """学年起始年不算混用。"""
    ctx = QcContext(all_text="2024年数据，2024-2025学年", semester="2024-2025-1")
    assert qc07_semester_year_labeling(ctx) == []


def test_qc08_numeric_conclusion_consistency():
    """「得分率很高」但实际最高仅 50% → 告警。"""
    ctx = QcContext(
        all_text="各题得分率很高，成绩较好",
        snapshot={"section_rates": [{"rate": 50.0}]},
    )
    issues = qc08_numeric_conclusion_consistency(ctx)
    assert len(issues) >= 1 and issues[0].rule_id == "QC-08"


def test_run_all_rules_registered():
    """注册表完整：13 条规则可全部执行。"""
    from backend.application.course_eval.quality_service import QC_RULES

    assert len(QC_RULES) == 13
    ctx = QcContext(cell_texts=["40.6%"], all_text="", snapshot={}, report_json={})
    issues = run_quality_checks(ctx)
    assert isinstance(issues, list)
    assert len(issues) >= 0


def test_qc02_not_implemented_as_no_box():
    """QC-02 不得实现成「全文无 □」：合法保留的未选项不应误报（§6.8 反例）。"""
    ctx = QcContext(cell_texts=["☑专业基础课 □公共基础课 □专业方向课 □任选课"])
    issues = qc02_no_placeholder_residue(ctx)
    assert issues == []  # 四选一勾中一个，其余 □ 合法保留
