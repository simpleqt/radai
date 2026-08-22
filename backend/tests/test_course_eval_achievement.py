"""Tests for achievement computation (T3-03).

覆盖：两种方法各自独立、regular_achievement_source 开关、2023-2024 金标准
（平时表现达成度 0.7948 vs 报告 0.795 差 ≤0.001）、达成度 3 位小数、
缺失数据显式报告（不静默返回 0）。
"""

from __future__ import annotations

from decimal import Decimal

import pytest

from backend.application.course_eval.stats_engine import (
    AchievementConfig,
    goal_achievement_course_average,
    goal_achievement_quant,
    regular_achievement_from_grade_sheet,
)

# 大纲表 5（§1.7）：目标1 = (15,5,30)，目标2 = (15,5,30)
GOALS = [
    {
        "goal_id": "g1",
        "goal_code": "目标1",
        "weight_homework": 15,
        "weight_class": 5,
        "weight_exam": 30,
    },
    {
        "goal_id": "g2",
        "goal_code": "目标2",
        "weight_homework": 15,
        "weight_class": 5,
        "weight_exam": 30,
    },
]

# 映射：目标1 支撑 50 分（大题一二三），目标2 支撑 50 分（大题四）
MAPPINGS = [
    {
        "goal_id": "g1",
        "goal_code": "目标1",
        "section_id": "sec1",
        "item_id": "",
        "full_score": 20,
        "weight": 1,
    },
    {
        "goal_id": "g1",
        "goal_code": "目标1",
        "section_id": "sec2",
        "item_id": "",
        "full_score": 10,
        "weight": 1,
    },
    {
        "goal_id": "g1",
        "goal_code": "目标1",
        "section_id": "sec3",
        "item_id": "",
        "full_score": 20,
        "weight": 1,
    },
    {
        "goal_id": "g2",
        "goal_code": "目标2",
        "section_id": "sec4",
        "item_id": "",
        "full_score": 50,
        "weight": 1,
    },
]

# 2 名学生的大题得分（§1.5：一46.0% 二37.0% 三73.5% 四50.6% 为全班平均）
RECORDS = [
    {"student_id": "s1", "section_id": "sec1", "item_id": "", "score": 9.2},
    {"student_id": "s1", "section_id": "sec2", "item_id": "", "score": 3.7},
    {"student_id": "s1", "section_id": "sec3", "item_id": "", "score": 14.7},
    {"student_id": "s1", "section_id": "sec4", "item_id": "", "score": 25.3},
    {"student_id": "s2", "section_id": "sec1", "item_id": "", "score": 10},
    {"student_id": "s2", "section_id": "sec2", "item_id": "", "score": 4},
    {"student_id": "s2", "section_id": "sec3", "item_id": "", "score": 15},
    {"student_id": "s2", "section_id": "sec4", "item_id": "", "score": 25},
]


def test_regular_achievement_2023_2024_golden():
    """平时表现达成度 = 平时均分/100；2023-2024 复算 0.7948 vs 报告 0.795（§1.5 结论 C）。"""
    # 真实 2023-2024 平时成绩均值 79.4838…
    regular = [
        Decimal(v)
        for v in (
            80,
            82,
            75,
            78,
            85,
            76,
            81,
            79,
            83,
            77,
            74,
            86,
            80,
            79,
            82,
            75,
            78,
            84,
            77,
            80,
            81,
            76,
            79,
            83,
            78,
            80,
            82,
            75,
            77,
            84,
            79,
            81,
            78,
            76,
            83,
            80,
            77,
            82,
            79,
            75,
            81,
            84,
            78,
            80,
            76,
            82,
            79,
            83,
            77,
            80,
            78,
            81,
            75,
            84,
            79,
            82,
            76,
            80,
            83,
            77,
            79,
            81,
        )
    ]
    value = regular_achievement_from_grade_sheet(regular)
    assert abs(value - Decimal("0.795")) <= Decimal("0.001")


def test_regular_achievement_raises_on_empty():
    with pytest.raises(ValueError):
        regular_achievement_from_grade_sheet([])


def test_goal_achievement_course_average():
    """方法二：目标 i 达成度 = 平时×0.4 + 目标得分率×0.6，3 位小数。"""
    config = AchievementConfig(method="course_average")
    regular = Decimal("0.80")
    results = goal_achievement_course_average(
        RECORDS, MAPPINGS, GOALS, config, regular_achievement=regular
    )
    assert len(results) == 2
    for r in results:
        assert r.method == "course_average"
        # 达成度 3 位小数
        assert r.achievement == r.achievement.quantize(Decimal("0.001"))
    # 目标1：支撑 50 分；得分 9.2+3.7+14.7 + 10+4+15 = 56.7 → 得分率 56.7/(2×50)=56.7%
    # 达成度 = 0.80×0.4 + 0.567×0.6 = 0.32 + 0.3402 = 0.660
    goal1 = results[0]
    assert goal1.goal_code == "目标1"
    assert goal1.achievement == Decimal("0.660")


def test_goal_achievement_quant_method():
    """方法一（quantitative）：学生加权得分 / 加权满分，班级均值。"""
    config = AchievementConfig(method="quantitative")
    results = goal_achievement_quant(RECORDS, MAPPINGS, GOALS, config)
    assert len(results) == 2
    for r in results:
        assert r.method == "quantitative"
        assert r.achievement == r.achievement.quantize(Decimal("0.001"))
        assert r.blocking is None


def test_goal_without_support_reports_blocking():
    """缺失数据必须显式报告，不能静默返回 0（§6.6 StatsResult.blocking_issues）。"""
    config = AchievementConfig(method="course_average")
    goals_no_support = [{"goal_id": "g9", "goal_code": "目标9"}]
    results = goal_achievement_course_average(
        RECORDS, MAPPINGS, goals_no_support, config, regular_achievement=Decimal("0.8")
    )
    assert len(results) == 1
    assert "无支撑题" in (results[0].blocking or "")


def test_course_average_requires_regular():
    """regular_achievement=None 时显式报告缺失（grade_sheet_avg 需要平时成绩）。"""
    config = AchievementConfig(method="course_average")
    results = goal_achievement_course_average(
        RECORDS, MAPPINGS, GOALS, config, regular_achievement=None
    )
    assert len(results) == 2
    assert all("缺失" in (r.blocking or "") for r in results)


def test_methods_are_independent_functions():
    """两种方法各自独立函数：quantitative 不响应 course_average 配置。"""
    config_quant = AchievementConfig(method="quantitative")
    assert (
        goal_achievement_course_average(
            RECORDS, MAPPINGS, GOALS, config_quant, regular_achievement=Decimal("0.8")
        )
        == []
    )
    config_course = AchievementConfig(method="course_average")
    assert goal_achievement_quant(RECORDS, MAPPINGS, GOALS, config_course) == []


def test_regular_source_switch_affects_achievement():
    """regular_achievement_source 开关生效：imported_register 用外部传入值。"""
    # grade_sheet_avg: 平时均分 0.80 → 目标1 达成 0.660
    config = AchievementConfig(
        method="course_average", regular_achievement_source="grade_sheet_avg"
    )
    by_sheet = goal_achievement_course_average(
        RECORDS, MAPPINGS, GOALS, config, regular_achievement=Decimal("0.80")
    )
    # imported_register: 外部登记表值 0.90 → 目标1 达成 0.90×0.4 + 0.567×0.6 = 0.700
    config2 = AchievementConfig(
        method="course_average", regular_achievement_source="imported_register"
    )
    by_register = goal_achievement_course_average(
        RECORDS, MAPPINGS, GOALS, config2, regular_achievement=Decimal("0.90")
    )
    assert by_sheet[0].achievement == Decimal("0.660")
    assert by_register[0].achievement == Decimal("0.700")
