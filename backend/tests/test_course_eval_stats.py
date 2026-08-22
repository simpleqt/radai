"""Tests for the course-eval statistics engine (T1-06).

统计引擎是全模块最重要的测试：报告里每个数字都出自它。
覆盖：架构约束（纯函数）、区间边界语义、三套独立分段、金标准复算、无 float 泄漏。
"""

from __future__ import annotations

import ast
from decimal import Decimal
from pathlib import Path

import pytest

from backend.application.course_eval import stats_engine
from backend.application.course_eval.stats_engine import (
    NF_AVG_2DP,
    NF_INT,
    NF_PCT_1DP,
    StatsInputs,
    build_snapshot,
    distribute_appendix2,
    distribute_grade_sheet,
    distribute_paper_analysis,
)

# ── 1. 架构约束：AST 断言纯函数模块没有被污染（§1.3）──


def test_stats_engine_has_no_io_imports():
    """stats_engine 必须是纯函数模块（§1.3）。防止后来者顺手加 import。"""
    source = Path(stats_engine.__file__).read_text(encoding="utf-8")
    imported: set[str] = set()
    for node in ast.walk(ast.parse(source)):
        if isinstance(node, ast.Import):
            imported.update(alias.name.split(".")[0] for alias in node.names)
        elif isinstance(node, ast.ImportFrom) and node.module:
            imported.add(node.module.split(".")[0])

    forbidden = {"sqlalchemy", "httpx", "requests", "minio", "redis", "aiofiles", "os"}
    assert not (
        imported & forbidden
    ), f"stats_engine 引入了禁止的依赖: {imported & forbidden}"
    assert not any(m.startswith("backend.db") for m in imported)


# ── 2. 边界值：区间语义（方案 §6.6，已用两份真实成绩单判决）──


@pytest.mark.parametrize(
    ("score", "expected_band"),
    [
        (Decimal("49.9"), "50以下"),
        (Decimal("50"), "50以下"),  # ← 唯一例外：首段名为「50以下」但含 50
        (Decimal("50.1"), "51-59"),
        (Decimal("59"), "51-59"),
        (Decimal("60"), "60-69"),  # ← 判决点：错误实现会把它算进「51-59」
        (Decimal("69"), "60-69"),
        (Decimal("70"), "70-79"),
        (Decimal("80"), "80-89"),
        (Decimal("89"), "80-89"),
        (Decimal("90"), "90以上"),
        (Decimal("100"), "90以上"),
    ],
)
def test_paper_analysis_band_boundaries(score, expected_band):
    """除首段外一律 [lo, hi)；每个标签与其区间精确对应。"""
    result = distribute_paper_analysis([score])
    hit = [b for b in result.bands if b.count == 1]
    assert len(hit) == 1
    assert hit[0].label == expected_band


def test_band_50_is_inclusive():
    """2023-2024 有 3 人恰好 50 分；用 x<50 会把首段从 29 人算成 26 人。"""
    scores = [Decimal("50")] * 3 + [Decimal("45")] * 26 + [Decimal("75")] * 33
    result = distribute_paper_analysis(scores)
    assert result.band_of("50以下").count == 29


def test_band_60_is_passing():
    """2024-2025 有 2 人恰好 60 分。统一左开右闭会把及格算成不及格，
    六段变成 [28,14,15,6,5,1]，与人工报告 [28,12,17,6,5,1] 不符。
    这是区分正确实现与错误实现的唯一判别用例，不能删。
    """
    result = distribute_paper_analysis([Decimal("60")] * 2)
    assert result.band_of("60-69").count == 2
    assert result.band_of("51-59").count == 0


# ── 3. 三套口径独立（附录 B：分段边界/统计对象/分母没有任何两项相同）──


def test_three_band_functions_are_independent():
    """三套口径禁止参数化复用。

    关键：断言必须能区分「三个独立函数」与「一个函数被三处调用」——
    若实现是共享的，三者的段数/标签/边界必然同步，本断言就会失败。
    """
    scores = [Decimal("59"), Decimal("60"), Decimal("50")]
    a = distribute_paper_analysis(scores)
    b = distribute_grade_sheet(scores)
    c = distribute_appendix2(scores)
    assert [len(x.bands) for x in (a, b, c)] == [6, 5, 5]
    assert a.bands[0].label == "50以下" and b.bands[0].label == "100-90"
    assert [x.label for x in c.bands] == [
        "优秀(≥90)",
        "良好(≥80)",
        "中等(≥70)",
        "及格(≥60)",
        "不及格(<60)",
    ]
    # 同一批 [59, 60, 50] 落入不同段：
    #   试卷分析：59→51-59、60→60-69、50→50以下（60 不许算进 51-59）
    #   成绩单：59/50→59以下、60→69-60；附表2：59/50→不及格、60→及格
    assert (
        a.band_of("50以下").count == 1
        and a.band_of("51-59").count == 1
        and a.band_of("60-69").count == 1
    )
    assert b.band_of("59以下").count == 2 and b.band_of("69-60").count == 1
    assert c.band_of("不及格(<60)").count == 2 and c.band_of("及格(≥60)").count == 1


# ── 4. 金标准复算：与人工报告逐项一致（§1.5 结论 A）──


def _golden_inputs_2024_2025() -> StatsInputs:
    """2024-2025 真实期末卷面成绩（69 人，仅数字无身份信息，§1.5）。

    由真实成绩单坐标法解析提取，平均 52.88、最高 94、最低 6、
    6 段 28/12/17/6/5/1、及格率 42.0%（§1.5 结论 A 逐项吻合）。
    """
    final_scores = [
        Decimal(v)
        for v in (
            6,
            12,
            17,
            22,
            23,
            25,
            26,
            27,
            30,
            34,
            36,
            37,
            37,
            37,
            38,
            39,
            39,
            40,
            40,
            41,
            42,
            42,
            44,
            44,
            44,
            46,
            46,
            46,
            51,
            51,
            51,
            51,
            53,
            53,
            53,
            54,
            54,
            55,
            56,
            56,
            60,
            60,
            61,
            61,
            62,
            62,
            63,
            63,
            63,
            64,
            65,
            65,
            65,
            67,
            68,
            69,
            69,
            71,
            72,
            73,
            73,
            78,
            79,
            81,
            84,
            84,
            87,
            88,
            94,
        )
    ]
    return StatsInputs(
        final_scores=final_scores,
        grade_sheet_scores=final_scores,
        paper_sample_scores=final_scores,
        expected_students=70,
        absent_students=1,
        violation_students=0,
        section_records=[
            {"section_id": "sec1", "score": Decimal(9.2)},
            {"section_id": "sec2", "score": Decimal(3.7)},
            {"section_id": "sec3", "score": Decimal(14.7)},
            {"section_id": "sec4", "score": Decimal(25.3)},
        ],
        sections=[
            {
                "section_id": "sec1",
                "report_order_no": 1,
                "question_type": "填空题",
                "full_score": Decimal(20),
            },
            {
                "section_id": "sec2",
                "report_order_no": 2,
                "question_type": "证明题",
                "full_score": Decimal(10),
            },
            {
                "section_id": "sec3",
                "report_order_no": 3,
                "question_type": "作图题",
                "full_score": Decimal(20),
            },
            {
                "section_id": "sec4",
                "report_order_no": 4,
                "question_type": "计算题",
                "full_score": Decimal(50),
            },
        ],
    )


def test_build_snapshot_matches_2024_2025_manual_report():
    """整份快照与人工报告逐项一致（§1.5 表格）。"""
    snapshot = build_snapshot(_golden_inputs_2024_2025())
    dist = snapshot.final_score_stats

    # 平均分精确值为 52.884057…，人工报告 52.88 是 2 位小数舍入（§1.5 表格）
    assert stats_engine.fmt(dist.average, NF_AVG_2DP) == "52.88"
    # 及格率精确值 42.029…，人工报告 42.0% 是 1 位小数舍入
    assert stats_engine.fmt(dist.pass_rate, NF_PCT_1DP) == "42.0%"
    assert dist.max_score == Decimal("94") and dist.min_score == Decimal("6")
    assert [b.count for b in dist.bands] == [28, 12, 17, 6, 5, 1]
    assert [stats_engine.fmt(b.pct, NF_PCT_1DP) for b in dist.bands] == [
        "40.6%",
        "17.4%",
        "24.6%",
        "8.7%",
        "7.2%",
        "1.4%",
    ]
    assert snapshot.scope["grade_sheet_n"] == 69
    assert snapshot.scope["paper_sample_n"] == 69
    assert snapshot.scope["is_full_sample"] is True


def test_section_rates_compute():
    """大题得分率：46.0% / 37.0% / 73.5% / 50.6%（§1.5 复算表）。"""
    inputs = _golden_inputs_2024_2025()
    # 用与报告一致的平均得分构造：每题 1 名学生（n=1 时 avg=总分）
    inputs.section_records = [
        {"section_id": "sec1", "score": Decimal("9.2")},
        {"section_id": "sec2", "score": Decimal("3.7")},
        {"section_id": "sec3", "score": Decimal("14.7")},
        {"section_id": "sec4", "score": Decimal("25.3")},
    ]
    inputs.paper_sample_scores = [Decimal("52.88")]
    rates = stats_engine.section_rates(inputs.section_records, inputs.sections)
    assert [stats_engine.fmt(r.rate, NF_PCT_1DP) for r in rates] == [
        "46.0%",
        "37.0%",
        "73.5%",
        "50.6%",
    ]
    # 得分率合计应回到平均分：9.2 + 3.7 + 14.7 + 25.3 = 52.9
    assert sum(r.avg_score for r in rates) == Decimal("52.9")


def test_no_float_leaks_into_results():
    snapshot = build_snapshot(_golden_inputs_2024_2025())
    values = [snapshot.final_score_stats.average, snapshot.final_score_stats.pass_rate]
    values += [b.pct for b in snapshot.final_score_stats.bands]
    for value in values:
        assert isinstance(value, Decimal), f"发现 float 泄漏: {value!r}"


# ── 5. fmt 唯一出口 ──


def test_fmt_formats_by_spec():
    assert stats_engine.fmt(Decimal("52.88"), NF_AVG_2DP) == "52.88"
    assert stats_engine.fmt(Decimal("42.0"), NF_PCT_1DP) == "42.0%"
    assert stats_engine.fmt(Decimal("94"), NF_INT) == "94"
    # ROUND_HALF_UP：0.125 → 0.13
    assert stats_engine.fmt(Decimal("0.125"), NF_AVG_2DP) == "0.13"
