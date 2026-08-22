"""统计与达成度引擎（M-06）——全模块最重要的纯函数模块。

硬性约束（§1.3 / §6.6）：不 import ORM、不发起任何 IO、不调 LLM、无当前时间依赖。
输入是 dataclass/dict 容器，输出是可序列化 dataclass。任何破坏纯函数约束的修改
都会被 test_stats_engine_has_no_io_imports 拦截。

三套成绩分段是三个独立常量 + 三个独立函数，禁止参数化复用（附录 B）。
数值一律 Decimal，fmt 是唯一 Decimal -> str 出口。
"""

from __future__ import annotations

from dataclasses import dataclass, field
from decimal import ROUND_HALF_UP, Decimal
from typing import Sequence

# ── 三套分段口径（区间语义见方案 §6.6，已用两份真实成绩单判决）──

# 试卷分析表「成绩分布表」6 段：唯一例外是首段「50以下」实际含 50（x <= 50），
# 其余一律 [lo, hi)
BANDS_PAPER_ANALYSIS: tuple[tuple[str, Decimal | None, Decimal | None, bool], ...] = (
    ("50以下", None, Decimal("50"), True),  # x <= 50（名不副实地含 50，填报习惯）
    ("51-59", Decimal("50"), Decimal("60"), False),
    ("60-69", Decimal("60"), Decimal("70"), False),
    ("70-79", Decimal("70"), Decimal("80"), False),
    ("80-89", Decimal("80"), Decimal("90"), False),
    ("90以上", Decimal("90"), None, False),
)

# 成绩单「成绩总结」5 段（总评成绩，分母=应考人数，缺考按 0 计入）
BANDS_GRADE_SHEET: tuple[tuple[str, Decimal | None, Decimal | None, bool], ...] = (
    ("100-90", Decimal("90"), None, False),
    ("89-80", Decimal("80"), Decimal("90"), False),
    ("79-70", Decimal("70"), Decimal("80"), False),
    ("69-60", Decimal("60"), Decimal("70"), False),
    ("59以下", None, Decimal("60"), False),
)

# 附表 2「课程总评成绩分析」5 档
BANDS_APPENDIX2: tuple[tuple[str, Decimal | None, Decimal | None, bool], ...] = (
    ("优秀(≥90)", Decimal("90"), None, False),
    ("良好(≥80)", Decimal("80"), Decimal("90"), False),
    ("中等(≥70)", Decimal("70"), Decimal("80"), False),
    ("及格(≥60)", Decimal("60"), Decimal("70"), False),
    ("不及格(<60)", None, Decimal("60"), False),
)

# 附表 2 样本达成度分布（(0,0.6) / [0.6,0.7) / [0.7,0.8) / [0.8,0.9) / [0.9,1]）
BANDS_ACHIEVEMENT_DIST: tuple[tuple[str, Decimal | None, Decimal | None, bool], ...] = (
    ("(0,0.6)", Decimal("0"), Decimal("0.6"), False),
    ("[0.6,0.7)", Decimal("0.6"), Decimal("0.7"), False),
    ("[0.7,0.8)", Decimal("0.7"), Decimal("0.8"), False),
    ("[0.8,0.9)", Decimal("0.8"), Decimal("0.9"), False),
    ("[0.9,1]", Decimal("0.9"), None, False),
)


def _in_band(
    x: Decimal, lo: Decimal | None, hi: Decimal | None, lo_inclusive: bool
) -> bool:
    """区间判定。lo_inclusive 命名沿袭方案，实为「首段含上界」：
    首段「50以下」= x <= 50（lo 必为 None）；其余一律 [lo, hi)。
    """
    if lo_inclusive:
        return hi is None or x <= hi
    if lo is not None and x < lo:
        return False
    if hi is not None and x >= hi:
        return False
    return True


# ── 数值格式（fmt 是唯一 Decimal -> str 出口）──


@dataclass(frozen=True)
class NumberFormat:
    """小数位与后缀。format_spec 交给 Decimal.quantize。"""

    scale: int
    suffix: str = ""
    rounding: str = "ROUND_HALF_UP"


NF_INT = NumberFormat(0)
NF_PCT_1DP = NumberFormat(1, "%")
NF_AVG_2DP = NumberFormat(2)
NF_ACH_3DP = NumberFormat(3)


def fmt(value: Decimal, spec: NumberFormat) -> str:
    """按模板配置格式化 Decimal，唯一允许转 str 的地方。"""
    quantized = value.quantize(Decimal("1." + "0" * spec.scale), rounding=ROUND_HALF_UP)
    return f"{quantized}{spec.suffix}"


# ── 分布结果 ──


@dataclass(frozen=True)
class Band:
    label: str
    count: int
    pct: Decimal  # 百分点（40.6 表示 40.6%），与 fmt 约定一致

    @property
    def pct_str(self) -> str:
        return fmt(self.pct, NF_PCT_1DP)


@dataclass(frozen=True)
class BandResult:
    bands: list[Band]
    average: Decimal
    pass_rate: Decimal | None = None
    max_score: Decimal | None = None
    min_score: Decimal | None = None

    def band_of(self, label: str) -> Band | None:
        for b in self.bands:
            if b.label == label:
                return b
        return None


def _distribute(
    scores: Sequence[Decimal],
    bands: tuple[tuple[str, Decimal | None, Decimal | None, bool], ...],
) -> list[Band]:
    counts = {label: 0 for label, _, _, _ in bands}
    for score in scores:
        for label, lo, hi, lo_inclusive in bands:
            if _in_band(score, lo, hi, lo_inclusive):
                counts[label] += 1
                break
    n = len(scores)
    return [
        Band(
            label=label,
            count=count,
            pct=((Decimal(count) / Decimal(n)) * 100 if n else Decimal(0)),
        )
        for label, count in counts.items()
    ]


def distribute_paper_analysis(scores: Sequence[Decimal]) -> BandResult:
    """试卷分析表 6 段（期末卷面，分母=应考−缺考−违纪）。"""
    values = [Decimal(s) for s in scores]
    n = len(values)
    return BandResult(
        bands=_distribute(values, BANDS_PAPER_ANALYSIS),
        average=(sum(values) / Decimal(n) if n else Decimal(0)),
        pass_rate=(
            (Decimal(sum(1 for s in values if s >= 60)) / Decimal(n)) * 100
            if n
            else Decimal(0)
        ),
        max_score=max(values) if values else None,
        min_score=min(values) if values else None,
    )


def distribute_grade_sheet(scores: Sequence[Decimal]) -> BandResult:
    """成绩单总结 5 段（总评成绩，分母=应考人数）。"""
    values = [Decimal(s) for s in scores]
    n = len(values)
    return BandResult(
        bands=_distribute(values, BANDS_GRADE_SHEET),
        average=(sum(values) / Decimal(n) if n else Decimal(0)),
        max_score=max(values) if values else None,
        min_score=min(values) if values else None,
    )


def distribute_appendix2(scores: Sequence[Decimal]) -> BandResult:
    """附表 2 五档（总评成绩，分母=抽样人数）。"""
    values = [Decimal(s) for s in scores]
    n = len(values)
    return BandResult(
        bands=_distribute(values, BANDS_APPENDIX2),
        average=(sum(values) / Decimal(n) if n else Decimal(0)),
        max_score=max(values) if values else None,
        min_score=min(values) if values else None,
    )


# ── 大题得分率 ──


@dataclass(frozen=True)
class SectionRate:
    report_order_no: int
    question_type: str
    full_score: Decimal
    avg_score: Decimal
    rate: Decimal  # 百分点（46.0 表示 46.0%）


def section_rates(
    records: Sequence[dict],
    sections: Sequence[dict],
) -> list[SectionRate]:
    """大题平均得分与得分率。

    records: [{"section_id":..., "score": Decimal}, ...]（小题得分也按大题聚合）
    sections: [{"section_id":..., "report_order_no":..., "question_type":...,
                "full_score":...}, ...]
    得分率 = Σ得分 / (学生数 × 大题满分)。
    """
    by_section: dict[str, list[Decimal]] = {}
    full_scores: dict[str, tuple[int, str, Decimal]] = {}
    for sec in sections:
        sid = sec["section_id"]
        full_scores[sid] = (
            int(sec["report_order_no"]),
            str(sec["question_type"]),
            Decimal(sec["full_score"]),
        )
        by_section.setdefault(sid, [])
    for rec in records:
        sid = rec["section_id"]
        if sid in by_section:
            by_section[sid].append(Decimal(rec["score"]))
    out: list[SectionRate] = []
    for sid, scores in by_section.items():
        order, qtype, full = full_scores[sid]
        n_students = len(scores)
        if n_students == 0:
            continue
        total = sum(scores)
        # 显式舍入到方案 §6.7 数值格式表的口径（平均得分 2 位、得分率 1 位），
        # 避免除不尽时把 28 位小数带进快照与对话
        avg = (total / Decimal(n_students)).quantize(
            Decimal("0.01"), rounding=ROUND_HALF_UP
        )
        rate = ((total / (Decimal(n_students) * full)) * 100).quantize(
            Decimal("0.1"), rounding=ROUND_HALF_UP
        )
        out.append(
            SectionRate(
                report_order_no=order,
                question_type=qtype,
                full_score=full,
                avg_score=avg,
                rate=rate,
            )
        )
    out.sort(key=lambda r: r.report_order_no)
    return out


# ── 快照 ──


@dataclass
class StatsInputs:
    """build_snapshot 的输入容器，由调用方从数据库组装（装满 Decimal 与 dataclass）。"""

    grade_sheet_scores: list[Decimal] = field(
        default_factory=list
    )  # 总评成绩（含缺考按 0）
    final_scores: list[Decimal] = field(default_factory=list)  # 期末卷面（有效学生）
    paper_sample_scores: list[Decimal] = field(
        default_factory=list
    )  # 有得分记录学生的期末
    section_records: list[dict] = field(default_factory=list)  # [{section_id, score}]
    sections: list[dict] = field(
        default_factory=list
    )  # [{section_id, report_order_no, question_type, full_score}]
    expected_students: int = 0
    absent_students: int = 0
    violation_students: int = 0


@dataclass
class StatsResult:
    """可直接序列化的统计快照。所有数字为 Decimal。"""

    scope: dict = field(default_factory=dict)
    final_score_stats: BandResult | None = None
    total_score_stats: BandResult | None = None
    appendix2_grade_stats: BandResult | None = None
    section_rates: list[SectionRate] = field(default_factory=list)
    blocking_issues: list[str] = field(default_factory=list)


def build_snapshot(inputs: StatsInputs) -> StatsResult:
    """编排全部计算，产出可直接序列化的快照（纯函数）。"""
    grade_sheet_n = max(
        inputs.expected_students - inputs.absent_students - inputs.violation_students, 0
    )
    paper_sample_n = len(inputs.paper_sample_scores)

    issues: list[str] = []
    if paper_sample_n == 0:
        issues.append("没有得分记录，得分率无法计算")
    if not inputs.final_scores:
        issues.append("缺少期末成绩，分布统计无法计算")

    result = StatsResult(
        scope={
            "grade_sheet_n": grade_sheet_n,
            "paper_sample_n": paper_sample_n,
            "is_full_sample": paper_sample_n == grade_sheet_n,
        },
        blocking_issues=issues,
    )
    if inputs.final_scores:
        result.final_score_stats = distribute_paper_analysis(inputs.final_scores)
    if inputs.grade_sheet_scores:
        result.total_score_stats = distribute_grade_sheet(inputs.grade_sheet_scores)
    if inputs.grade_sheet_scores:
        result.appendix2_grade_stats = distribute_appendix2(inputs.grade_sheet_scores)
    result.section_rates = section_rates(inputs.section_records, inputs.sections)
    return result


# ── 达成度计算（M-06 / T3-03）──


@dataclass(frozen=True)
class AchievementConfig:
    """达成度计算口径值对象（由调用方从 ce_achievement_configs 组装）。"""

    method: str = "course_average"  # quantitative | course_average
    regular_weight: Decimal = Decimal("0.40")
    exam_weight: Decimal = Decimal("0.60")
    regular_achievement_source: str = (
        "grade_sheet_avg"  # grade_sheet_avg | imported_register
    )
    pass_line: Decimal = Decimal("0.60")


@dataclass(frozen=True)
class GoalAchievement:
    """单个课程目标的达成度结果。"""

    goal_code: str
    method: str
    achievement: Decimal  # 3 位小数
    support_score: Decimal | None = None  # 目标支撑题满分合计
    avg_score: Decimal | None = None  # 目标支撑题平均得分
    rate: Decimal | None = None  # 目标考试题得分率（百分点）
    blocking: str | None = None  # 缺失数据显式报告


def goal_achievement_quant(
    records: Sequence[dict],
    mappings: Sequence[dict],
    goals: Sequence[dict],
    config: AchievementConfig,
) -> list[GoalAchievement]:
    """定量法（方法一 quantitative，方案 §6.6）：

    学生 s 目标 i = Σ_j (环节 j 得分 × w_ij) / Σ_j (环节 j 满分 × w_ij)，
    环节 j ∈ {课程作业, 课堂表现, 期末考试}，w_ij 来自大纲表 5；
    期末考试环节的「得分」= 该生目标 i 支撑题的加权得分。
    班级目标 i = Σ_s 学生达成度 / paper_sample_n。
    缺失数据必须显式报告，不能静默返回 0。
    """
    if config.method != "quantitative":
        return []
    results: list[GoalAchievement] = []
    for goal in goals:
        goal_code = goal["goal_code"]
        # 该目标的支撑题满分（期末环节）
        support = [
            m
            for m in mappings
            if m.get("goal_id") == goal.get("goal_id")
            or m.get("goal_code") == goal_code
        ]
        if not support:
            results.append(
                GoalAchievement(
                    goal_code=goal_code,
                    method="quantitative",
                    achievement=Decimal(0),
                    blocking=f"{goal_code} 无支撑题，无法计算达成度",
                )
            )
            continue
        # 期末环节满分 = Σ (支撑题满分 × weight)
        # 本实现简化：满分按支撑题 full_score × weight 加权
        full_total = sum(
            (Decimal(m.get("full_score", 0)) * Decimal(m.get("weight", 1)))
            for m in support
        )
        # 每名学生：该生目标支撑题加权得分 / full_total
        per_student: list[Decimal] = []
        students = {r.get("student_id") for r in records if r.get("student_id")}
        for student_id in students:
            student_scores = [
                Decimal(r["score"]) * Decimal(m["weight"])
                for r in records
                for m in support
                if r.get("student_id") == student_id
                and r.get("section_id") == m.get("section_id")
                and (r.get("item_id") or "") == (m.get("item_id") or "")
            ]
            if not student_scores:
                continue
            achieved = sum(student_scores) / full_total if full_total else Decimal(0)
            per_student.append(achieved)
        if not per_student:
            results.append(
                GoalAchievement(
                    goal_code=goal_code,
                    method="quantitative",
                    achievement=Decimal(0),
                    blocking=f"{goal_code} 无学生得分数据，达成度无法计算",
                )
            )
            continue
        class_avg = sum(per_student) / Decimal(len(per_student))
        results.append(
            GoalAchievement(
                goal_code=goal_code,
                method="quantitative",
                achievement=class_avg.quantize(
                    Decimal("0.001"), rounding=ROUND_HALF_UP
                ),
            )
        )
    return results


def goal_achievement_course_average(
    records: Sequence[dict],
    mappings: Sequence[dict],
    goals: Sequence[dict],
    config: AchievementConfig,
    *,
    regular_achievement: Decimal | None,
) -> list[GoalAchievement]:
    """方法二（course_average，方案 §1.5 结论 C/D）：

    平时表现达成度 = 平时成绩均值 / 100（grade_sheet_avg）或导入登记表值；
    目标 i 达成度 = 平时表现达成度 × regular_weight + 目标 i 得分率 × exam_weight；
    目标 i 得分率 = Σ_题 (该题得分 × weight) / (paper_sample_n × Σ_题 (满分 × weight)) × 100。
    """
    if config.method != "course_average":
        return []
    if regular_achievement is None:
        return [
            GoalAchievement(
                goal_code=g["goal_code"],
                method="course_average",
                achievement=Decimal(0),
                blocking="平时表现达成度缺失（grade_sheet_avg 需要平时成绩）",
            )
            for g in goals
        ]
    results: list[GoalAchievement] = []
    for goal in goals:
        goal_code = goal["goal_code"]
        support = [
            m
            for m in mappings
            if m.get("goal_code") == goal_code
            or m.get("goal_id") == goal.get("goal_id")
        ]
        if not support:
            results.append(
                GoalAchievement(
                    goal_code=goal_code,
                    method="course_average",
                    achievement=Decimal(0),
                    blocking=f"{goal_code} 无支撑题，无法计算达成度",
                )
            )
            continue
        # 目标支撑题得分率（百分点）
        weight_full = sum(
            Decimal(m.get("full_score", 0)) * Decimal(m.get("weight", 1))
            for m in support
        )
        students = {r.get("student_id") for r in records if r.get("student_id")}
        if not students or weight_full == 0:
            results.append(
                GoalAchievement(
                    goal_code=goal_code,
                    method="course_average",
                    achievement=Decimal(0),
                    blocking=f"{goal_code} 无学生得分数据，得分率无法计算",
                )
            )
            continue
        n = len(students)
        total_score = sum(
            Decimal(r["score"]) * Decimal(m["weight"])
            for r in records
            for m in support
            if r.get("student_id") in students
            and r.get("section_id") == m.get("section_id")
            and (r.get("item_id") or "") == (m.get("item_id") or "")
        )
        rate = (total_score / (Decimal(n) * weight_full)) * 100
        achievement = (
            regular_achievement * config.regular_weight
            + (rate / 100) * config.exam_weight
        )
        results.append(
            GoalAchievement(
                goal_code=goal_code,
                method="course_average",
                achievement=achievement.quantize(
                    Decimal("0.001"), rounding=ROUND_HALF_UP
                ),
                support_score=weight_full,
                rate=rate.quantize(Decimal("0.1"), rounding=ROUND_HALF_UP),
            )
        )
    return results


def regular_achievement_from_grade_sheet(
    regular_scores: Sequence[Decimal],
) -> Decimal:
    """平时表现达成度 = 平时成绩均值 / 100（grade_sheet_avg 口径）。"""
    if not regular_scores:
        raise ValueError("平时成绩缺失，无法计算平时表现达成度")
    return (
        sum(Decimal(s) for s in regular_scores) / Decimal(len(regular_scores))
    ) / 100
