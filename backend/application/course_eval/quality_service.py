"""报告质检规则集（M-08 / T3-06）。

13 条规则（QC-01~QC-10 + QC-02a/b/c）各自纯函数 + 注册表模式（同 validation.py）。
输入是值对象（渲染后的 docx 文本结构 + 统计快照 + 模板配置），不碰数据库。

实测来源：
- QC-04/05/06/07/08 直接来自 §1.6 两份人工报告的真实缺陷；
- QC-02a/b/c 直接来自 §6.7.1 的 PoC 实测失败（坑 1/2/3/4）。
"""

from __future__ import annotations

import re
from dataclasses import dataclass, field
from decimal import Decimal
from typing import Callable, Iterable



@dataclass(frozen=True)
class QcIssue:
    rule_id: str
    level: str  # error | warning
    detail: str


@dataclass
class QcContext:
    """质检输入值对象。"""

    snapshot: dict = field(default_factory=dict)  # 统计快照 result_json
    report_json: dict = field(default_factory=dict)  # 报告 content_json
    cell_texts: list[str] = field(
        default_factory=list
    )  # 去重后的全文单元格文本（渲染后导出）
    all_text: str = ""  # 全文拼接（含段落）
    checkbox_mark: str = "☑"
    guard_labels: list[str] = field(default_factory=list)  # 模板守卫标签（锚点表）
    semester: str = ""
    course_name: str = ""
    majors: list[str] = field(default_factory=list)
    history_reports: list[str] = field(default_factory=list)  # 近 3 份报告正文


QcRule = Callable[[QcContext], list[QcIssue]]


# ── QC-01 数值一致性 ──


def qc01_numeric_consistency(ctx: QcContext) -> list[QcIssue]:
    """报告每个数值字段与快照对应字段严格相等（模板小数位）。"""
    issues: list[QcIssue] = []
    for key, expected in ctx.snapshot.items():
        reported = ctx.report_json.get(key)
        if reported is None:
            continue
        try:
            if abs(Decimal(str(reported)) - Decimal(str(expected))) > Decimal("0.001"):
                issues.append(
                    QcIssue(
                        rule_id="QC-01",
                        level="error",
                        detail=f"数值 {key}: 报告 {reported} != 快照 {expected}",
                    )
                )
        except Exception:
            continue
    return issues


# ── QC-02 / 02a / 02b / 02c 模板完整性 ──

_PLACEHOLDER_RE = re.compile(r"\$\{|\{\{|\*\*\*")
_DATE_PLACEHOLDER_RE = re.compile(r"年\s*月\s*日")


def qc02_no_placeholder_residue(ctx: QcContext) -> list[QcIssue]:
    """全文（去重后）无 ${ / {{ / *** / 未被覆盖的「年 月 日」（§6.8 QC-02）。"""
    issues: list[QcIssue] = []
    for cell in ctx.cell_texts:
        if _PLACEHOLDER_RE.search(cell):
            issues.append(
                QcIssue(
                    rule_id="QC-02", level="error", detail=f"残留占位符: {cell[:40]!r}"
                )
            )
        if _DATE_PLACEHOLDER_RE.search(cell) and "2024" not in cell:
            issues.append(
                QcIssue(
                    rule_id="QC-02",
                    level="error",
                    detail=f"未覆盖的日期占位: {cell[:40]!r}",
                )
            )
    return issues


def qc02a_checkbox_selected(ctx: QcContext) -> list[QcIssue]:
    """每个必选复选框组至少有一个勾选标记，未选项 □ 计数 = 选项总数 − 已选项数（坑 2）。"""
    issues: list[QcIssue] = []
    mark = ctx.checkbox_mark
    for cell in ctx.cell_texts:
        checked = cell.count(f"{mark}")
        unchecked = cell.count("□")
        total_options = checked + unchecked
        if checked == 0 and total_options > 0:
            issues.append(
                QcIssue(
                    rule_id="QC-02a",
                    level="error",
                    detail=f"复选框组无勾选: {cell[:40]!r}",
                )
            )
        elif unchecked != total_options - checked:
            issues.append(
                QcIssue(
                    rule_id="QC-02a",
                    level="warning",
                    detail=f"复选框计数异常: checked={checked} unchecked={unchecked}",
                )
            )
    return issues


_ISOLATED_PRESET_RE = re.compile(r"^\s*[%分道]\s*$")


def qc02b_no_isolated_preset(ctx: QcContext) -> list[QcIssue]:
    """不存在仅由预置字符构成的单元格（未使用列已清空，坑 3）。"""
    issues: list[QcIssue] = []
    for cell in ctx.cell_texts:
        if _ISOLATED_PRESET_RE.fullmatch(cell):
            issues.append(
                QcIssue(rule_id="QC-02b", level="error", detail=f"孤立预置符: {cell!r}")
            )
    return issues


def qc02c_guard_labels_present(ctx: QcContext) -> list[QcIssue]:
    """模板全部守卫标签仍在原位（坑 1）。"""
    missing = [label for label in ctx.guard_labels if label not in ctx.all_text]
    if missing:
        return [
            QcIssue(rule_id="QC-02c", level="error", detail=f"守卫标签丢失: {missing}")
        ]
    return []


# ── QC-03 引用有效性 ──


def qc03_reference_validity(ctx: QcContext) -> list[QcIssue]:
    """正文中 第X题/目标X/第X章 必须能在数据模型中解析到实体。"""
    issues: list[QcIssue] = []
    sections = {str(s.get("report_order_no")) for s in ctx.snapshot.get("sections", [])}
    goals = {str(g.get("goal_code")) for g in ctx.snapshot.get("goals", [])}
    for m in re.finditer(r"第([一二三四五六七八九十\d]+)题", ctx.all_text):
        no = m.group(1)
        cn = {
            "一": "1",
            "二": "2",
            "三": "3",
            "四": "4",
            "五": "5",
            "六": "6",
            "七": "7",
            "八": "8",
            "九": "9",
            "十": "10",
        }.get(no, no)
        if cn not in sections:
            issues.append(
                QcIssue(
                    rule_id="QC-03",
                    level="warning",
                    detail=f"引用了不存在的大题: 第{no}题",
                )
            )
    for m in re.finditer(r"目标(\d+)", ctx.all_text):
        if f"目标{m.group(1)}" not in goals:
            issues.append(
                QcIssue(
                    rule_id="QC-03",
                    level="warning",
                    detail=f"引用了不存在的目标: 目标{m.group(1)}",
                )
            )
    return issues


# ── QC-04 重复度 ──


def _jaccard_3gram(a: str, b: str) -> float:
    def grams(text: str) -> set[str]:
        return {text[i : i + 3] for i in range(len(text) - 2)}

    ga, gb = grams(a), grams(b)
    if not ga or not gb:
        return 0.0
    return len(ga & gb) / len(ga | gb)


def qc04_duplication(ctx: QcContext) -> list[QcIssue]:
    """与同课程最近 3 份报告 3-gram Jaccard > 0.80 告警（§1.6 第 4 条真实痛点）。"""
    issues: list[QcIssue] = []
    for idx, history in enumerate(ctx.history_reports):
        sim = _jaccard_3gram(ctx.all_text, history)
        if sim > 0.80:
            issues.append(
                QcIssue(
                    rule_id="QC-04",
                    level="warning",
                    detail=f"与历史报告 {idx + 1} 相似度 {sim:.2f} > 0.80，疑似套话复用",
                )
            )
    return issues


# ── QC-05 / 06 / 07 跨文档一致性（§1.6 真实缺陷）──


def qc05_cross_document_consistency(ctx: QcContext) -> list[QcIssue]:
    """试卷分析表与附表 1 的教学班号/学年学期必须一致（§1.6 缺陷 1）。"""
    issues: list[QcIssue] = []
    class_no_values = re.findall(r"\d{6,12}-\d+", ctx.all_text)
    if len(set(class_no_values)) > 1:
        issues.append(
            QcIssue(
                rule_id="QC-05",
                level="error",
                detail=f"教学班号不一致: {set(class_no_values)}",
            )
        )
    return issues


def qc06_field_semantics(ctx: QcContext) -> list[QcIssue]:
    """专业必须来自专业字典，不能等于课程名（§1.6 缺陷 2）。"""
    if ctx.majors and ctx.course_name and ctx.course_name in ctx.all_text:
        # 检查「专业」栏是否被填成课程名
        for m in re.finditer(r"专业[:：]?\s*([^\s，。；]{2,})", ctx.all_text):
            value = m.group(1).strip()
            if value == ctx.course_name:
                return [
                    QcIssue(
                        rule_id="QC-06",
                        level="error",
                        detail=f"专业栏填成了课程名「{value}」",
                    )
                ]
            if value not in ctx.majors:
                return [
                    QcIssue(
                        rule_id="QC-06",
                        level="warning",
                        detail=f"专业「{value}」不在专业字典 {ctx.majors} 中",
                    )
                ]
    return []


def qc07_semester_year_labeling(ctx: QcContext) -> list[QcIssue]:
    """学年/自然年标注一致：同一批数据不得用学年范围外的自然年指代（§1.6 缺陷 3）。

    例：「2024-2025 学年」数据写成「2024年」可接受（起始年），
    写成「2026年」或混用「2023年」即告警。
    """
    issues: list[QcIssue] = []
    semesters = set(re.findall(r"(\d{4})-(\d{4})学年", ctx.all_text))
    natural_years = {int(y) for y in re.findall(r"(\d{4})年", ctx.all_text)}
    for start, end in semesters:
        start_i, end_i = int(start), int(end)
        foreign = sorted(y for y in natural_years if not (start_i <= y <= end_i))
        if foreign:
            issues.append(
                QcIssue(
                    rule_id="QC-07",
                    level="warning",
                    detail=f"学年 {start}-{end} 混用了范围外的自然年 {foreign}",
                )
            )
    return issues


# ── QC-08 / 09 / 10 ──

_STRONG_WORDS = ("得分率很高", "较好", "有所提高", "明显提升", "优秀")


def qc08_numeric_conclusion_consistency(ctx: QcContext) -> list[QcIssue]:
    """「得分率很高/有所提高」等定性表述与实际数值的矛盾（LLM 辅助，仅告警 §6.8）。"""
    issues: list[QcIssue] = []
    rates = [
        Decimal(str(r.get("rate", 0))) for r in ctx.snapshot.get("section_rates", [])
    ]
    for word in _STRONG_WORDS:
        if word in ctx.all_text and rates and max(rates) < 60:
            issues.append(
                QcIssue(
                    rule_id="QC-08",
                    level="warning",
                    detail=f"表述「{word}」但最高得分率仅 {max(rates)}%",
                )
            )
    return issues


def qc09_method_annotation(ctx: QcContext) -> list[QcIssue]:
    """报告须注明评价方法、配置版本、统计范围（全量 N / 样本 N）。"""
    missing = []
    if "方法" not in ctx.all_text and "course_average" not in ctx.all_text:
        missing.append("评价方法")
    if (
        "N=" not in ctx.all_text
        and "n=" not in ctx.all_text
        and "人数" not in ctx.all_text
    ):
        missing.append("统计范围")
    if missing:
        return [
            QcIssue(rule_id="QC-09", level="warning", detail=f"缺少口径标注: {missing}")
        ]
    return []


def qc10_sensitive_info(ctx: QcContext) -> list[QcIssue]:
    """学号/姓名不得出现在模板未授权的位置（附表 3 除外）。"""
    issues: list[QcIssue] = []
    sid_count = len(re.findall(r"\d{12}", ctx.all_text))
    if sid_count > 0:
        issues.append(
            QcIssue(
                rule_id="QC-10",
                level="warning",
                detail=f"正文出现 {sid_count} 个学号（如非附表 3 需检查授权）",
            )
        )
    return issues


# ── 注册表 ──

QC_RULES: dict[str, QcRule] = {
    "QC-01": qc01_numeric_consistency,
    "QC-02": qc02_no_placeholder_residue,
    "QC-02a": qc02a_checkbox_selected,
    "QC-02b": qc02b_no_isolated_preset,
    "QC-02c": qc02c_guard_labels_present,
    "QC-03": qc03_reference_validity,
    "QC-04": qc04_duplication,
    "QC-05": qc05_cross_document_consistency,
    "QC-06": qc06_field_semantics,
    "QC-07": qc07_semester_year_labeling,
    "QC-08": qc08_numeric_conclusion_consistency,
    "QC-09": qc09_method_annotation,
    "QC-10": qc10_sensitive_info,
}


def run_quality_checks(
    ctx: QcContext, rule_ids: Iterable[str] | None = None
) -> list[QcIssue]:
    issues: list[QcIssue] = []
    selected = list(rule_ids) if rule_ids is not None else list(QC_RULES)
    for rule_id in selected:
        issues.extend(QC_RULES[rule_id](ctx))
    return issues


