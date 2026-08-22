"""确定性校验规则集（M-02/M-03 / T2-04）。

每条规则一个纯函数，统一签名 Rule = Callable[[ValidationContext], list[Issue]]，
注册进 RULES 表。规则函数不许直接写库，只返回 Issue 列表，由调用方决定落库还是丢弃——
这样规则可以被穷举测试。

GS-* 成绩单规则 / PS-* 扫描卷规则。
"""

from __future__ import annotations

from dataclasses import dataclass, field
from decimal import Decimal
from typing import Callable, Literal

# ── 数据结构 ──

IssueLevel = Literal["error", "warning"]


@dataclass(frozen=True)
class Issue:
    rule_id: str
    level: IssueLevel
    target: str
    detail: str
    candidates: list[str] = field(default_factory=list)


@dataclass
class ValidationContext:
    """规则上下文：成绩单与得分数据（尽量用值对象，避免规则碰数据库）。"""

    # 成绩单
    records: list[dict] = field(
        default_factory=list
    )  # [{student_no, regular, final, total, flag}]
    summary: dict = field(default_factory=dict)  # {expected, absent, violation, bands}
    regular_weight: Decimal = Decimal("0.4")
    exam_weight: Decimal = Decimal("0.6")

    # 扫描卷得分
    score_records: list[dict] = field(
        default_factory=list
    )  # [{student_id, section_id, item_id, score, full_score}]
    sections: list[dict] = field(
        default_factory=list
    )  # [{section_id, order_no, report_order_no, total_score, applicable_majors}]
    section_totals: dict[str, Decimal] = field(
        default_factory=dict
    )  # {student_id: 总分框读数}
    grade_sheet_finals: dict[str, Decimal] = field(
        default_factory=dict
    )  # {student_id: 成绩单期末}
    config: dict = field(
        default_factory=dict
    )  # {paper_full_score, standard_total_score, ps06_tolerance}


Rule = Callable[[ValidationContext], list[Issue]]


# ── GS-*：成绩单规则 ──


def rule_gs01_record_count_matches_summary(ctx: ValidationContext) -> list[Issue]:
    """GS-01：解析记录数 == 成绩单「应考人数」（error）。"""
    expected = ctx.summary.get("expected")
    if expected is None:
        return []
    n = len(ctx.records)
    if n != int(expected):
        return [
            Issue(
                rule_id="GS-01",
                level="error",
                target="summary",
                detail=f"解析记录数 {n} != 应考人数 {expected}",
            )
        ]
    return []


def rule_gs03_recompute_matches_total(ctx: ValidationContext) -> list[Issue]:
    """GS-03：|平时×w_r + 期末×w_f − 总评| ≤ 0.5（warning，逐行）。"""
    issues: list[Issue] = []
    for rec in ctx.records:
        if rec.get("flag"):
            continue
        regular, final, total = rec.get("regular"), rec.get("final"), rec.get("total")
        if regular is None or final is None or total is None:
            continue
        recomputed = (
            Decimal(regular) * ctx.regular_weight + Decimal(final) * ctx.exam_weight
        )
        if abs(recomputed - Decimal(total)) > Decimal("0.5"):
            issues.append(
                Issue(
                    rule_id="GS-03",
                    level="warning",
                    target=rec.get("student_no", ""),
                    detail=f"平时×{ctx.regular_weight}+期末×{ctx.exam_weight}={recomputed} != 总评 {total}",
                )
            )
    return issues


def rule_gs06_unique_student_no(ctx: ValidationContext) -> list[Issue]:
    """GS-06：学号唯一（error）。"""
    seen: set[str] = set()
    for rec in ctx.records:
        sid = rec.get("student_no", "")
        if sid in seen:
            return [
                Issue(rule_id="GS-06", level="error", target=sid, detail="学号重复")
            ]
        seen.add(sid)
    return []


def rule_gs07_scores_in_range(ctx: ValidationContext) -> list[Issue]:
    """GS-07：所有成绩 ∈ [0, 100]（error）。"""
    issues: list[Issue] = []
    for rec in ctx.records:
        for key in ("regular", "final", "total"):
            value = rec.get(key)
            if value is None:
                continue
            if not (Decimal(0) <= Decimal(value) <= Decimal(100)):
                issues.append(
                    Issue(
                        rule_id="GS-07",
                        level="error",
                        target=rec.get("student_no", ""),
                        detail=f"{key}={value} 超出 [0,100]",
                    )
                )
    return issues


# ── PS-*：扫描卷规则 ──


def rule_ps01_score_within_range(ctx: ValidationContext) -> list[Issue]:
    """PS-01：0 ≤ score ≤ full_score（error）。"""
    issues: list[Issue] = []
    for rec in ctx.score_records:
        score, full = Decimal(rec["score"]), Decimal(rec["full_score"])
        if not (Decimal(0) <= score <= full):
            issues.append(
                Issue(
                    rule_id="PS-01",
                    level="error",
                    target=rec.get("student_id", ""),
                    detail=f"score={score} 超出 [0,{full}]",
                )
            )
    return issues


def rule_ps02_section_sum_matches_total(ctx: ValidationContext) -> list[Issue]:
    """PS-02：|Σ 大题得分 − 总分框读数| ≤ 0.5（warning，纸面本身可能不一致 §1.2）。"""
    issues: list[Issue] = []
    per_student: dict[str, Decimal] = {}
    for rec in ctx.score_records:
        if not rec.get("item_id"):  # 只累计大题级
            per_student[rec["student_id"]] = per_student.get(
                rec["student_id"], Decimal(0)
            ) + Decimal(rec["score"])
    for student_id, reported in ctx.section_totals.items():
        summed = per_student.get(student_id)
        if summed is None:
            continue
        if abs(summed - reported) > Decimal("0.5"):
            issues.append(
                Issue(
                    rule_id="PS-02",
                    level="warning",
                    target=student_id,
                    detail=f"Σ大题={summed} vs 总分框={reported}",
                )
            )
    return issues


def rule_ps05_section_count_matches_declared(ctx: ValidationContext) -> list[Issue]:
    """PS-05：得分记录覆盖的大题与声明的大题结构一致（error）。

    - 专业专属大题（applicable_majors 非空）全班无得分是「本班不考」的预期情况，
      不算异常；
    - 报错信息用「第X题（题型）」而不是内部 UUID，教师才看得懂。
    """
    declared = {s.get("section_id"): s for s in ctx.sections if s.get("section_id")}
    actual = {r["section_id"] for r in ctx.score_records if not r.get("item_id")}
    if not declared or not actual:
        return []

    def _label(section_id: str) -> str:
        meta = declared.get(section_id) or {}
        order = meta.get("order_no")
        qtype = str(meta.get("question_type") or "").strip()
        if order:
            return f"第{order}题" + (f"（{qtype}）" if qtype else "")
        return f"大题 {str(section_id)[:8]}"

    issues: list[Issue] = []
    missing = [
        sid
        for sid, meta in declared.items()
        if sid not in actual and not meta.get("applicable_majors")
    ]
    if missing:
        names = "、".join(_label(sid) for sid in sorted(missing, key=_label))
        issues.append(
            Issue(
                rule_id="PS-05",
                level="error",
                target="sections",
                detail=f"{names} 声明在试卷结构中，但没有任何学生的得分记录，请补录或核实",
            )
        )
    extra = sorted(actual - set(declared))
    if extra:
        issues.append(
            Issue(
                rule_id="PS-05",
                level="error",
                target="sections",
                detail=f"存在 {len(extra)} 道未声明大题的得分记录，试卷结构可能没解析全",
            )
        )
    return issues


def rule_ps06_cross_check_with_grade_sheet(ctx: ValidationContext) -> list[Issue]:
    """PS-06：扫描卷总分按 paper_full_score/standard_total_score 折算后
    与成绩单期末成绩差 ≤ ps06_tolerance（warning，跨源交叉校验 §6.3.6）。"""
    cfg = ctx.config
    paper_full = Decimal(cfg.get("paper_full_score", 100))
    standard_total = Decimal(cfg.get("standard_total_score", 100))
    tolerance = Decimal(cfg.get("ps06_tolerance", 0.5))
    issues: list[Issue] = []
    per_student: dict[str, Decimal] = {}
    for rec in ctx.score_records:
        if not rec.get("item_id"):
            per_student[rec["student_id"]] = per_student.get(
                rec["student_id"], Decimal(0)
            ) + Decimal(rec["score"])
    for student_id, total in per_student.items():
        final = ctx.grade_sheet_finals.get(student_id)
        if final is None:
            continue
        converted = total * standard_total / paper_full
        if abs(converted - final) > tolerance:
            issues.append(
                Issue(
                    rule_id="PS-06",
                    level="warning",
                    target=student_id,
                    detail=f"折算后 {converted} vs 成绩单期末 {final}",
                )
            )
    return issues


# ── 注册表 ──

RULES: dict[str, Rule] = {
    "GS-01": rule_gs01_record_count_matches_summary,
    "GS-03": rule_gs03_recompute_matches_total,
    "GS-06": rule_gs06_unique_student_no,
    "GS-07": rule_gs07_scores_in_range,
    "PS-01": rule_ps01_score_within_range,
    "PS-02": rule_ps02_section_sum_matches_total,
    "PS-05": rule_ps05_section_count_matches_declared,
    "PS-06": rule_ps06_cross_check_with_grade_sheet,
}


def run_rules(ctx: ValidationContext, rule_ids: list[str] | None = None) -> list[Issue]:
    """按注册表执行规则；rule_ids 为 None 时跑全部。"""
    issues: list[Issue] = []
    selected = rule_ids or list(RULES)
    for rule_id in selected:
        rule = RULES[rule_id]
        issues.extend(rule(ctx))
    return issues
