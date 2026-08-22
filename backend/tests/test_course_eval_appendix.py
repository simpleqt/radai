"""Tests for appendix renderers (T3-04).

合成附表模板复刻真实实施办法 docx 的 3 张表结构（附表 1/2/3），
验证变行数克隆与数值写入；真实模板回读挂 skip 名额。
"""

from __future__ import annotations

import io
import os
from decimal import Decimal
from pathlib import Path

import pytest
from docx import Document

from backend.application.course_eval.report.appendix_renderer import (
    render_appendix1,
    render_appendix2,
    render_appendix3,
)

_GOLDEN_ROOT = Path(
    os.getenv("FMCA_COURSE_EVAL_GOLDEN_ROOT", "")
    or Path(__file__).resolve().parents[2] / "data" / "course_eval_golden"
)
requires_fmca_golden_dataset = pytest.mark.skipif(
    not (_GOLDEN_ROOT / "manifest.json").is_file(),
    reason=(
        "requires the FMCA course-eval golden dataset (real appendix template); "
        "set FMCA_COURSE_EVAL_GOLDEN_ROOT to run"
    ),
)

_REAL_APPENDIX_TEMPLATE = (
    Path(__file__).resolve().parents[2]
    / "tmp"
    / "course_eval_survey"
    / "2023-2024学年+2024-2025学年  第一学期《材料力学》"
    / "课程教学目标达成度-环境与土木工程学院面向产出课程教学质量评价实施办法20201021.docx"
)


def _make_appendix_template_docx() -> bytes:
    """合成实施办法 docx：3 张表（附表 1/2/3 结构同真实模板）。"""
    doc = Document()

    # 附表 1：7 行 × 8 列
    t0 = doc.add_table(rows=7, cols=8)
    t0.rows[0].cells[0].text = "学   院"
    t0.rows[0].cells[2].text = "开课年级"
    t0.rows[0].cells[4].text = "抽样人数"
    t0.rows[1].cells[0].text = "专   业"
    t0.rows[1].cells[2].text = "任课教师"
    t0.rows[2].cells[0].text = "课程目标"
    t0.rows[3].cells[0].text = "达成度分析结果评价"
    t0.rows[4].cells[0].text = "存在的问题及原因分析"
    t0.rows[5].cells[0].text = "持续改进意见及措施"

    # 附表 2：21 行 × 8 列
    t1 = doc.add_table(rows=21, cols=8)
    t1.rows[0].cells[0].text = "学   院"
    t1.rows[1].cells[0].text = "专   业"
    t1.rows[2].cells[0].text = "课程总评成绩分析"
    t1.rows[3].cells[0].text = "课程总评成绩分析"
    t1.rows[3].cells[1].text = "最 高 分"
    t1.rows[3].cells[3].text = "最低分"
    t1.rows[3].cells[6].text = "平均分"
    t1.rows[4].cells[1].text = "等    级"
    for i, label in enumerate(
        ["优秀(≥90)", "良好(≥80)", "中等(≥70)", "及格(≥60)", "不及格（<60）"]
    ):
        t1.rows[4].cells[3 + i].text = label
    t1.rows[5].cells[1].text = "人    数"
    t1.rows[6].cells[1].text = "百分比"
    t1.rows[7].cells[1].text = "课程目标"
    t1.rows[7].cells[6].text = "达成度\n期望值"
    t1.rows[7].cells[7].text = "实际达成度平均值"
    for i in range(5):
        t1.rows[9 + i].cells[0].text = "达成度情况"
        t1.rows[9 + i].cells[6].text = "0.6"
    t1.rows[14].cells[0].text = "平时表现达成度"
    t1.rows[14].cells[6].text = "0.6"
    t1.rows[15].cells[1].text = "课程目标"
    for i, label in enumerate(
        ["(0,0.6)", "[0.6,0.7)", "[0.7,0.8)", "[0.8,0.9)", "[0.9,1]"]
    ):
        t1.rows[15].cells[3 + i].text = label
    for i in range(5):
        t1.rows[16 + i].cells[0].text = "样本达成度分布情况"
        t1.rows[16 + i].cells[2].text = "人数\n占比"

    # 附表 3：10 行 × 9 列
    t2 = doc.add_table(rows=10, cols=9)
    t2.rows[0].cells[0].text = "序号"
    t2.rows[0].cells[1].text = "学生姓名"
    t2.rows[1].cells[2].text = "平时表现"
    t2.rows[2].cells[2].text = "平时表现"
    for i in range(6):
        t2.rows[2].cells[3 + i].text = f"课程目标【{i + 1}】"
    for i in range(5):
        t2.rows[3 + i].cells[0].text = str(i + 1)
    t2.rows[9].cells[0].text = "实际达成度\n平均值"

    buf = io.BytesIO()
    doc.save(buf)
    return buf.getvalue()


GOALS = [
    {
        "goal_code": "目标1",
        "description": "掌握材料力学基本概念",
        "expected": "0.6",
        "actual": "0.618",
    },
    {
        "goal_code": "目标2",
        "description": "具备工程问题分析能力",
        "expected": "0.6",
        "actual": "0.654",
    },
]


def test_render_appendix2_grade_and_goals():
    """附表 2：等级人数/百分比 + 达成度期望/实际 + 平时表现达成度。"""
    out = render_appendix2(
        _make_appendix_template_docx(),
        grade_counts=[1, 11, 12, 5, 0],
        grade_pcts=[
            Decimal("3.45"),
            Decimal("37.93"),
            Decimal("41.38"),
            Decimal("17.24"),
            Decimal("0.00"),
        ],
        max_score=Decimal("90.8"),
        min_score=Decimal("63"),
        average=Decimal("78.01"),
        goals=GOALS,
        regular_achievement=Decimal("0.795"),
        achievement_dist=[
            {
                "goal_code": "目标1",
                "bands": [("(0,0.6)", 6, 13.6), ("[0.6,0.7)", 25, 56.8)],
            },
        ],
    )
    doc = Document(io.BytesIO(out))
    t1 = doc.tables[1]
    assert t1.rows[5].cells[3].text.strip() == "1"
    assert t1.rows[6].cells[3].text.strip() == "3.45"
    # 目标1 实际达成度 3 位小数
    assert t1.rows[9].cells[7].text.strip() == "0.618"
    # 平时表现达成度 3 位小数
    assert t1.rows[14].cells[7].text.strip() == "0.795"


def test_render_appendix2_clones_rows_for_more_goals():
    """目标数超过模板行时变行数克隆（附表 2 达成度情况）。"""
    goals = [
        {"goal_code": f"目标{i}", "description": "", "expected": "0.6", "actual": "0.6"}
        for i in range(1, 7)
    ]  # 6 个目标 > 模板 5 行
    out = render_appendix2(
        _make_appendix_template_docx(),
        grade_counts=[1, 2, 3, 4, 5],
        grade_pcts=[
            Decimal("1"),
            Decimal("2"),
            Decimal("3"),
            Decimal("4"),
            Decimal("5"),
        ],
        max_score=Decimal("90"),
        min_score=Decimal("60"),
        average=Decimal("75"),
        goals=goals,
        regular_achievement=Decimal("0.8"),
        achievement_dist=[],
    )
    doc = Document(io.BytesIO(out))
    t1 = doc.tables[1]
    assert t1.rows[9].cells[1].text.strip().startswith("【1】")
    assert (
        t1.rows[14].cells[1].text.strip().startswith("【6】")
    )  # 克隆后第 6 个目标落 r14


def test_render_appendix3_students_and_averages():
    """附表 3：学生明细（变行数）+ 末行平均值。"""
    students = [
        {
            "name": "张三",
            "regular": "80",
            "goal_achievements": [Decimal("0.8"), Decimal("0.7")],
        },
        {
            "name": "李四",
            "regular": "75",
            "goal_achievements": [Decimal("0.6"), Decimal("0.65")],
        },
        {
            "name": "王五",
            "regular": "90",
            "goal_achievements": [Decimal("0.9"), Decimal("0.8")],
        },
        {
            "name": "赵六",
            "regular": "85",
            "goal_achievements": [Decimal("0.75"), Decimal("0.7")],
        },
    ]
    out = render_appendix3(
        _make_appendix_template_docx(),
        students=students,
        goal_count=2,
        averages=[Decimal("0.7625"), Decimal("0.7125")],
    )
    doc = Document(io.BytesIO(out))
    t2 = doc.tables[2]
    # 4 名学生 ≤ 模板 5 行，直接写
    assert t2.rows[3].cells[0].text.strip() == "1"
    assert t2.rows[3].cells[1].text.strip() == "张三"
    assert t2.rows[3].cells[3].text.strip() == "0.800"
    # 末行平均值
    assert t2.rows[9].cells[3].text.strip() == "0.763"
    assert t2.rows[9].cells[4].text.strip() == "0.713"


def test_render_appendix3_clones_for_many_students():
    """学生数超过模板 5 行时变行数克隆。"""
    students = [
        {
            "name": f"学生{i}",
            "regular": "80",
            "goal_achievements": [Decimal("0.6"), Decimal("0.6")],
        }
        for i in range(1, 8)
    ]  # 7 名学生 > 5 行
    out = render_appendix3(
        _make_appendix_template_docx(),
        students=students,
        goal_count=2,
        averages=[Decimal("0.6"), Decimal("0.6")],
    )
    doc = Document(io.BytesIO(out))
    t2 = doc.tables[2]
    assert t2.rows[3].cells[1].text.strip() == "学生1"
    assert t2.rows[9].cells[1].text.strip() == "学生7"  # 克隆后第 7 名学生落 r9 前
    # 平均值行被推到 r10（克隆后表格变 12 行）
    assert len(t2.rows) >= 11


def test_render_appendix1_basic_info():
    out = render_appendix1(
        _make_appendix_template_docx(),
        college="环境与土木工程学院",
        grade="2023级",
        sample_count=29,
        major="土木工程",
        teacher_name="张老师",
        goals=GOALS,
        conclusion="达成度总体良好",
    )
    doc = Document(io.BytesIO(out))
    t0 = doc.tables[0]
    assert t0.rows[0].cells[1].text.strip() == "环境与土木工程学院"
    assert t0.rows[1].cells[1].text.strip() == "土木工程"
    assert "0.618" in t0.rows[2].cells[1].text


@pytest.mark.golden
@requires_fmca_golden_dataset
def test_render_real_appendix_template():
    """真实实施办法模板渲染附表 2 回读（§4.2 名额之一）。"""
    assert _REAL_APPENDIX_TEMPLATE.is_file()
    out = render_appendix2(
        _REAL_APPENDIX_TEMPLATE.read_bytes(),
        grade_counts=[1, 11, 12, 5, 0],
        grade_pcts=[
            Decimal("3.45"),
            Decimal("37.93"),
            Decimal("41.38"),
            Decimal("17.24"),
            Decimal("0.00"),
        ],
        max_score=Decimal("90.8"),
        min_score=Decimal("63"),
        average=Decimal("78.01"),
        goals=GOALS,
        regular_achievement=Decimal("0.795"),
        achievement_dist=[],
    )
    doc = Document(io.BytesIO(out))
    t1 = doc.tables[1]
    assert t1.rows[5].cells[3].text.strip() == "1"
    assert t1.rows[9].cells[7].text.strip() == "0.618"
