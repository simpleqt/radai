"""Tests for docx anchor primitives and distribution rendering (T1-07).

合成模板 `_make_blank_template_docx` 复刻真实模板的四个坑（§6.7.1）：
表 0 r2c0 跨 2 列、表 1 表头三/六跨列、表 2/表 3 预置 '%'、复选框措辞不一致。
四个坑的回归测试在合成模板上任何环境都能跑；真实模板回读挂 skip 名额。
"""

from __future__ import annotations

import io
import os
from decimal import Decimal
from pathlib import Path

import pytest
from docx import Document

from backend.application.course_eval.report import docx_anchor
from backend.application.course_eval.report.docx_anchor import (
    CellAnchor,
    TemplateAnchorMismatch,
    build_header_index,
    clear_unused_columns,
    set_cell_text,
    tick_checkbox,
    write_guarded,
)
from backend.application.course_eval.report.paper_analysis_renderer import (
    SectionView,
    TaskView,
    band_result_from_snapshot,
    render_distribution,
    render_paper_analysis,
    render_qualitative_sections,
)
from backend.application.course_eval.stats_engine import distribute_paper_analysis

_GOLDEN_ROOT = Path(
    os.getenv("FMCA_COURSE_EVAL_GOLDEN_ROOT", "")
    or Path(__file__).resolve().parents[2] / "data" / "course_eval_golden"
)
requires_fmca_golden_dataset = pytest.mark.skipif(
    not (_GOLDEN_ROOT / "manifest.json").is_file(),
    reason=(
        "requires the FMCA course-eval golden dataset (real blank template); "
        "set FMCA_COURSE_EVAL_GOLDEN_ROOT to run"
    ),
)

_REAL_TEMPLATE = (
    Path(__file__).resolve().parents[2]
    / "tmp"
    / "course_eval_survey"
    / "试卷分析表.docx"
)


def _merge_cells(table, row_idx: int, col_a: int, col_b: int) -> None:
    """合并 (row_idx, col_a..col_b)，文本落在首格。"""
    a = table.rows[row_idx].cells[col_a]
    b = table.rows[row_idx].cells[col_b]
    a.merge(b)


def _make_blank_template_docx() -> bytes:
    """合成一份与真实模板同构的 4 表 docx（含四个坑，§6.7.1）。"""
    doc = Document()

    # 表 0：5x12，r2c0 跨 2 列承载「参加考试年级」（坑 1 的结构）
    t0 = doc.add_table(rows=5, cols=12)
    t0.rows[0].cells[0].text = "课程名称"
    t0.rows[0].cells[4].text = "学时"
    t0.rows[0].cells[6].text = "学分"
    t0.rows[0].cells[9].text = "考核方式"
    t0.rows[1].cells[0].text = "课程属性"
    t0.rows[1].cells[1].text = "□公共基础课 □专业基础课 □专业方向课 □任选课"
    t0.rows[1].cells[6].text = "题量"
    t0.rows[1].cells[7].text = "分钟"
    t0.rows[1].cells[9].text = "考试时间"
    t0.rows[1].cells[11].text = "年  月  日"
    _merge_cells(t0, 2, 0, 1)
    t0.rows[2].cells[0].text = "参加考试年级"
    t0.rows[2].cells[2].text = "级"
    t0.rows[2].cells[3].text = "专业"
    t0.rows[3].cells[0].text = "教学班号"
    t0.rows[3].cells[8].text = "考试总人数"
    t0.rows[4].cells[0].text = "任课教师"
    t0.rows[4].cells[8].text = "试卷分析人"

    # 表 1：7x13，表头三/六跨列（坑 4 的合并，四→5、七→9）
    t1 = doc.add_table(rows=7, cols=13)
    _merge_cells(t1, 0, 3, 4)  # 三 跨 c3-c4
    _merge_cells(t1, 0, 7, 8)  # 六 跨 c7-c8
    t1.rows[0].cells[0].text = "题目（大题）序号"
    for ci, text in (
        (1, "一"),
        (2, "二"),
        (3, "三"),
        (5, "四"),
        (6, "五"),
        (7, "六"),
        (9, "七"),
        (10, "八"),
        (11, "九"),
        (12, "十"),
    ):
        t1.rows[0].cells[ci].text = text
    for ri in (1, 2, 3, 4):
        t1.rows[ri].cells[0].text = ["题型", "小题个数", "分值", "总分"][ri - 1]
    # 汇总行与真实模板同构：试题总个数/道、合计总分/分、试卷结构
    t1.rows[5].cells[0].text = "试题总个数"
    t1.rows[5].cells[1].text = "道"
    t1.rows[5].cells[2].text = "合计总分"
    t1.rows[5].cells[4].text = "分"
    t1.rows[5].cells[6].text = "试卷结构"
    t1.rows[6].cells[0].text = "试题覆盖面"

    # 表 2：3x10，r2 的 c0-c5 与 c7 预置 '%'（坑 3 的预置符）
    t2 = doc.add_table(rows=3, cols=10)
    t2.rows[1].cells[0].text = "50以下"
    t2.rows[1].cells[1].text = "51-59"
    t2.rows[1].cells[2].text = "60-69"
    t2.rows[1].cells[3].text = "70-79"
    t2.rows[1].cells[4].text = "80-89"
    t2.rows[1].cells[5].text = "90以上"
    t2.rows[1].cells[6].text = "平均成绩"
    t2.rows[1].cells[7].text = "及格率"
    t2.rows[1].cells[8].text = "最高分"
    t2.rows[1].cells[9].text = "最低分"
    for ci in (0, 1, 2, 3, 4, 5, 7):
        t2.rows[2].cells[ci].text = "%"

    # 表 3：4x11，r3 全部 10 列预置 '%'（坑 3 的另一个来源）
    t3 = doc.add_table(rows=4, cols=11)
    for ci, text in enumerate(
        ["题目（大题）序号", "一", "二", "三", "四", "五", "六", "七", "八", "九", "十"]
    ):
        t3.rows[0].cells[ci].text = text
    for ci in range(1, 11):
        t3.rows[3].cells[ci].text = "%"

    # 定性段落（四~七 + 签字栏），与真实模板同构
    doc.add_paragraph("四、对不及格成绩的原因分析")
    doc.add_paragraph("1、")
    doc.add_paragraph("")
    doc.add_paragraph("2、")
    doc.add_paragraph("")
    doc.add_paragraph("五、考试分析结论")
    doc.add_paragraph("")
    doc.add_paragraph("")
    doc.add_paragraph("六、今后改进意见（建议）")
    doc.add_paragraph("1、")
    doc.add_paragraph("")
    doc.add_paragraph("2、")
    doc.add_paragraph("")
    doc.add_paragraph("七、其它需要说明的问题")
    doc.add_paragraph("")
    doc.add_paragraph("任课教师签字：")

    buf = io.BytesIO()
    doc.save(buf)
    return buf.getvalue()


def _synthetic_stats():
    """真实 2024-2025 期末卷面成绩（69 人，仅数字），与 §1.5 复算一致。"""
    scores = [
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
    return distribute_paper_analysis(scores)


# ── 坑 1：合并单元格 ──


def test_grade_anchor_does_not_destroy_label():
    """坑 1：写 r2c2（经守卫）不得摧毁 r2c0 的「参加考试年级」标签。"""
    doc = Document(io.BytesIO(_make_blank_template_docx()))
    t0 = doc.tables[0]
    anchor = CellAnchor(
        name="grade",
        row=2,
        col=2,
        guard="参加考试年级",
        guard_row=2,
        guard_col=0,
    )
    write_guarded(t0, anchor, "2023 级")
    assert t0.rows[2].cells[0].text.strip() == "参加考试年级"
    assert t0.rows[2].cells[2].text.strip() == "2023 级"


def test_write_guarded_rejects_merged_target():
    """坑 1：把 grade 锚点故意改成 r2c1（合并格）应抛 TemplateAnchorMismatch。"""
    doc = Document(io.BytesIO(_make_blank_template_docx()))
    bad = CellAnchor(
        name="grade",
        row=2,
        col=1,
        guard="参加考试年级",
        guard_row=2,
        guard_col=0,
    )
    with pytest.raises(TemplateAnchorMismatch, match="同一个合并单元格"):
        write_guarded(doc.tables[0], bad, "2023 级")


def test_write_guarded_rejects_missing_guard():
    """守卫标签不在原位也应抛 TemplateAnchorMismatch。"""
    doc = Document(io.BytesIO(_make_blank_template_docx()))
    anchor = CellAnchor(
        name="grade",
        row=2,
        col=2,
        guard="不存在的标签",
        guard_row=2,
        guard_col=0,
    )
    with pytest.raises(TemplateAnchorMismatch, match="守卫标签期望"):
        write_guarded(doc.tables[0], anchor, "2023 级")


# ── 坑 2：术语不一致 ──


def test_tick_checkbox_with_term_mapping():
    """业务值「专业基础课程」→ 模板选项「专业基础课」映射后勾选成功。"""
    doc = Document(io.BytesIO(_make_blank_template_docx()))
    cell = doc.tables[0].rows[1].cells[1]
    assert tick_checkbox(cell, "专业基础课") is True
    assert "☑专业基础课" in cell.text


def test_tick_checkbox_returns_false_on_unmatched():
    """未命中术语映射时 tick_checkbox 返回 False（调用方必须检查，坑 2 硬规则）。"""
    doc = Document(io.BytesIO(_make_blank_template_docx()))
    cell = doc.tables[0].rows[1].cells[1]
    assert tick_checkbox(cell, "不存在的选项") is False
    assert "☑不存在的选项" not in cell.text


# ── 坑 3：未使用列残留预置符 ──


def test_unused_columns_cleared():
    """表 3 得分率行 10 列全预置 '%'，4 大题时第 5 列必须被清空（坑 3）。"""
    doc = Document(io.BytesIO(_make_blank_template_docx()))
    t3 = doc.tables[3]
    col = build_header_index(t3)
    used = {col["一"], col["二"], col["三"], col["四"]}
    # 模拟 4 大题写入
    for ci in used:
        set_cell_text(t3.rows[3].cells[ci], "46.0%")
    clear_unused_columns(t3, rows=(3,), used=used, all_cols=range(1, 11))
    for ci in used:
        assert t3.rows[3].cells[ci].text.strip().endswith("%")
    for ci in range(5, 11):
        assert t3.rows[3].cells[ci].text.strip() == "", f"c{ci} 残留预置符"


# ── 坑 4：合并单元格重复计数 ──


def test_residual_scan_dedups_merged_cells():
    """表 1 表头「三」跨两列：去重遍历后只计一次（坑 4）。"""
    doc = Document(io.BytesIO(_make_blank_template_docx()))
    # 只扫表 1（表 3 表头也有「三」，会让全文档计数 = 2）
    count = sum(
        1
        for _row, _ci, cell in docx_anchor.iter_unique_cells(doc.tables[1])
        if cell.text.strip() == "三"
    )
    assert count == 1  # 合并单元格被 row.cells 重复返回会数成 2


def test_header_index_skips_merged_columns():
    """表 1 表头三/六跨列：四→5、七→9（§1.4 预判，实测一致）。"""
    doc = Document(io.BytesIO(_make_blank_template_docx()))
    idx1 = build_header_index(doc.tables[1])
    assert idx1["一"] == 1 and idx1["二"] == 2 and idx1["三"] == 3
    assert idx1["四"] == 5 and idx1["六"] == 7 and idx1["七"] == 9
    idx3 = build_header_index(doc.tables[3])
    assert idx3["四"] == 4  # 表 3 规整


# ── 成绩分布表渲染（T1-07 竖切闭环）──


def test_distribution_render_readback():
    """渲染表 2：c0=40.6%、c6=52.88、c8=94、c9=6（§1.5 金标准）。"""
    stats = _synthetic_stats()
    out = render_distribution(_make_blank_template_docx(), final_score_stats=stats)
    doc = Document(io.BytesIO(out))
    t2 = doc.tables[2]
    row = t2.rows[2]
    assert row.cells[0].text.strip() == "40.6%"
    assert row.cells[1].text.strip() == "17.4%"
    assert row.cells[2].text.strip() == "24.6%"
    assert row.cells[3].text.strip() == "8.7%"
    assert row.cells[4].text.strip() == "7.2%"
    assert row.cells[5].text.strip() == "1.4%"
    assert row.cells[6].text.strip() == "52.88"
    assert row.cells[7].text.strip() == "42.0%"
    assert row.cells[8].text.strip() == "94"
    assert row.cells[9].text.strip() == "6"


def test_distribution_render_preserves_percent_columns():
    """预置 % 被整体覆盖而非追加：不得出现 40.6%% 或丢失 %（§6.7.3 表 2）。"""
    stats = _synthetic_stats()
    out = render_distribution(_make_blank_template_docx(), final_score_stats=stats)
    doc = Document(io.BytesIO(out))
    t2 = doc.tables[2]
    for ci in range(6):
        text = t2.rows[2].cells[ci].text.strip()
        assert "%%" not in text
        assert text.endswith("%")


# ── 整表渲染（表 0/1/3 + 表 2）──


def _stats_dict() -> dict:
    """把金标准 BandResult 序列化成 latest_snapshot 的 final_score_stats 形态。"""
    stats = _synthetic_stats()
    return {
        "bands": [
            {"label": b.label, "count": b.count, "pct": str(b.pct)}
            for b in stats.bands
        ],
        "average": str(stats.average),
        "pass_rate": str(stats.pass_rate),
        "max": str(stats.max_score),
        "min": str(stats.min_score),
    }


def _task_view() -> TaskView:
    return TaskView(
        course_name="材料力学",
        grade="2022级",
        major="地质工程",
        class_no="2022031001-2022031002",
        teacher_name="张老师",
        analyzer_name="张老师",
        hours="64",
        credit="4",
        exam_mode="闭卷",
        course_nature="学科基础课程",
        exam_date="2023 年 12 月 27 日",
        exam_duration="120 分钟",
        expected_students="62",
    )


def _sections_2324() -> list[SectionView]:
    """2023-2024 真实卷结构 + 人工表的平均得分/得分率。"""
    return [
        SectionView(1, "填空题", "2分×10", Decimal("20"),
                    avg_score=Decimal("7"), rate=Decimal("35.0")),
        SectionView(2, "作图题", "10分×2", Decimal("20"),
                    avg_score=Decimal("16.13"), rate=Decimal("80.6")),
        SectionView(3, "计算题", "15分×4", Decimal("60"),
                    avg_score=Decimal("29.53"), rate=Decimal("49.2")),
    ]


def test_full_render_fills_all_four_tables():
    out = render_paper_analysis(
        _make_blank_template_docx(),
        task=_task_view(),
        sections=_sections_2324(),
        final_score_stats=_stats_dict(),
    )
    doc = Document(io.BytesIO(out.docx_bytes))

    t0 = doc.tables[0]
    assert t0.rows[0].cells[1].text.strip() == "材料力学"
    assert t0.rows[0].cells[5].text.strip() == "64"
    assert t0.rows[0].cells[7].text.strip() == "4"
    assert t0.rows[0].cells[11].text.strip() == "闭卷"
    assert t0.rows[1].cells[7].text.strip() == "120 分钟"
    assert t0.rows[1].cells[11].text.strip() == "2023 年 12 月 27 日"
    # 坑 1：写年级不摧毁标签
    assert t0.rows[2].cells[0].text.strip() == "参加考试年级"
    assert t0.rows[2].cells[2].text.strip() == "2022级"
    assert t0.rows[2].cells[4].text.strip() == "地质工程"
    assert t0.rows[3].cells[1].text.strip() == "2022031001-2022031002"
    assert t0.rows[3].cells[10].text.strip() == "62"
    # 坑 2：学科基础课程 → 模板措辞「专业基础课」命中
    assert "☑专业基础课" in t0.rows[1].cells[1].text
    assert out.warnings == []

    t1 = doc.tables[1]
    # 一/二/三列（列 1/2/3），四（列 5，合并跳号）应被清空
    assert [t1.rows[1].cells[c].text.strip() for c in (1, 2, 3)] == [
        "填空题", "作图题", "计算题",
    ]
    assert [t1.rows[2].cells[c].text.strip() for c in (1, 2, 3)] == ["10", "2", "4"]
    assert [t1.rows[3].cells[c].text.strip() for c in (1, 2, 3)] == ["2", "10", "15"]
    assert [t1.rows[4].cells[c].text.strip() for c in (1, 2, 3)] == ["20", "20", "60"]
    assert t1.rows[1].cells[5].text.strip() == ""
    assert t1.rows[5].cells[1].text.strip() == "16 道"
    assert t1.rows[5].cells[4].text.strip() == "100 分"

    t2 = doc.tables[2]
    assert t2.rows[2].cells[0].text.strip() == "40.6%"
    assert t2.rows[2].cells[6].text.strip() == "52.88"

    t3 = doc.tables[3]
    assert [t3.rows[1].cells[c].text.strip() for c in (1, 2, 3)] == ["20", "20", "60"]
    assert [t3.rows[2].cells[c].text.strip() for c in (1, 2, 3)] == [
        "7.00", "16.13", "29.53",
    ]
    assert [t3.rows[3].cells[c].text.strip() for c in (1, 2, 3)] == [
        "35.0%", "80.6%", "49.2%",
    ]
    # 坑 3：没用到的列（四~十）预置 % 必须被清空
    for ci in range(4, 11):
        assert t3.rows[3].cells[ci].text.strip() == "", f"表3 c{ci} 残留预置符"


def test_full_render_mixed_pattern_and_missing_fields():
    """混合分值（10×2+15×2）与缺失字段（留白）都要正确呈现。"""
    sections = [
        SectionView(1, "计算题", "10分×2+15分×2", Decimal("50"),
                    avg_score=Decimal("34.10"), rate=Decimal("68.2")),
        SectionView(2, "画图题", "", Decimal("20")),  # 无分值模式、无得分
    ]
    out = render_paper_analysis(
        _make_blank_template_docx(),
        task=TaskView(course_name="材料力学"),
        sections=sections,
        final_score_stats=None,
    )
    doc = Document(io.BytesIO(out.docx_bytes))
    t1 = doc.tables[1]
    assert t1.rows[2].cells[1].text.strip() == "4"  # 2+2
    assert t1.rows[3].cells[1].text.strip() == "10×2+15×2"
    assert t1.rows[2].cells[2].text.strip() == ""  # 无模式 → 留白
    # 个数不全时汇总不写「N 道」，合计总分仍写
    assert t1.rows[5].cells[1].text.strip() == "道"
    assert t1.rows[5].cells[4].text.strip() == "70 分"
    assert any("成绩分布" in w for w in out.warnings)
    t3 = doc.tables[3]
    assert t3.rows[1].cells[2].text.strip() == "20"
    assert t3.rows[3].cells[2].text.strip() == ""  # 无得分率 → 清空预置 %


def test_full_render_rejects_wrong_structure_template():
    """表 1 行标签不符（模板换版）必须抛 TemplateAnchorMismatch，不出错表。"""
    doc = Document(io.BytesIO(_make_blank_template_docx()))
    doc.tables[1].rows[1].cells[0].text = "改掉的标签"
    buf = io.BytesIO()
    doc.save(buf)
    with pytest.raises(TemplateAnchorMismatch):
        render_paper_analysis(
            buf.getvalue(),
            task=_task_view(),
            sections=_sections_2324(),
            final_score_stats=None,
        )


def test_band_result_from_snapshot_roundtrip():
    src = _synthetic_stats()
    restored = band_result_from_snapshot(_stats_dict())
    assert [b.label for b in restored.bands] == [b.label for b in src.bands]
    assert [b.pct for b in restored.bands] == [b.pct for b in src.bands]
    assert restored.average == src.average
    assert restored.pass_rate == src.pass_rate
    assert restored.max_score == src.max_score
    assert restored.min_score == src.min_score


def test_qualitative_sections_filled_into_template():
    """四/五/六段草稿写入「1、」「2、」与空白行，七段写数据关注事项。"""
    doc = Document(io.BytesIO(_make_blank_template_docx()))
    warnings: list[str] = []
    render_qualitative_sections(
        doc,
        fail_reasons=["教的方面：部分知识点讲授偏快。", "学的方面：练习投入不足。"],
        conclusion="试卷覆盖面较广，成绩分布基本正常。",
        improvements=["加强作业反馈。", "增加课堂案例分析。"],
        other_notes=["第3题（证明题）有样本学生缺得分，达成度会被低估"],
        warnings=warnings,
    )
    assert warnings == []
    texts = [p.text.strip() for p in doc.paragraphs]
    assert "1、教的方面：部分知识点讲授偏快。" in texts
    assert "2、学的方面：练习投入不足。" in texts
    assert "试卷覆盖面较广，成绩分布基本正常。" in texts
    assert "1、加强作业反馈。" in texts
    assert "2、增加课堂案例分析。" in texts
    assert any("缺得分" in t for t in texts)
    # 六的「1、」不能误写进四的区段：各区段独立定位
    i4 = texts.index("四、对不及格成绩的原因分析")
    i6 = texts.index("六、今后改进意见（建议）")
    assert texts.index("1、教的方面：部分知识点讲授偏快。") > i4
    assert texts.index("1、加强作业反馈。") > i6


def test_qualitative_sections_blank_drafts_leave_template_untouched():
    """草稿为空时段落保持留白；缺标题只告警不中断。"""
    template = _make_blank_template_docx()
    doc = Document(io.BytesIO(template))
    warnings: list[str] = []
    render_qualitative_sections(
        doc,
        fail_reasons=[],
        conclusion="",
        improvements=[],
        other_notes=[],
        warnings=warnings,
    )
    assert warnings == []
    before = [p.text for p in Document(io.BytesIO(template)).paragraphs]
    assert [p.text for p in doc.paragraphs] == before


@pytest.mark.asyncio
async def test_draft_qualitative_texts_rejects_digits_and_degrades(monkeypatch):
    """定性起草：含数字整体重试；重试尽留白告警；子调用必须带内部标签。"""
    from backend.application.course_eval import render_service

    calls = {"n": 0}

    class _Resp:
        def __init__(self, content):
            self.content = content

    class _Model:
        async def ainvoke(self, prompt, config=None):
            calls["n"] += 1
            assert config and "tool-internal-llm" in config.get("tags", [])
            if calls["n"] == 1:
                return _Resp(
                    '{"fail_reasons": ["得分率为45.7%"], "conclusion": "", "improvements": []}'
                )
            return _Resp(
                '{"fail_reasons": ["教的方面讲授偏快。", "学的方面练习不足。"], '
                '"conclusion": "成绩分布基本正常。", '
                '"improvements": ["加强反馈。", "多做案例。"]}'
            )

    async def _fake_factory(scope, **kwargs):
        return _Model()

    monkeypatch.setattr(
        "backend.core.models.factory.create_scoped_chat_model_async", _fake_factory
    )
    drafts, warnings = await render_service.draft_qualitative_texts("课程：材料力学")
    assert calls["n"] == 2  # 第一次含数字被拒，第二次通过
    assert warnings == []
    assert drafts["fail_reasons"] == ["教的方面讲授偏快。", "学的方面练习不足。"]
    assert drafts["conclusion"] == "成绩分布基本正常。"

    class _AlwaysDigit:
        async def ainvoke(self, prompt, config=None):
            return _Resp(
                '{"fail_reasons": ["100%"], "conclusion": "1", "improvements": []}'
            )

    async def _digit_factory(scope, **kwargs):
        return _AlwaysDigit()

    monkeypatch.setattr(
        "backend.core.models.factory.create_scoped_chat_model_async", _digit_factory
    )
    drafts2, warnings2 = await render_service.draft_qualitative_texts("课程：材料力学")
    assert drafts2["fail_reasons"] == [] and drafts2["conclusion"] == ""
    assert any("留白" in w for w in warnings2)


def test_qualitative_context_contains_no_digits():
    """起草上下文本身也必须无数字（程度词表述）。"""
    from decimal import Decimal as D

    from backend.application.course_eval.render_service import qualitative_context
    from backend.application.course_eval.report.narrative_drafter import (
        assert_no_digits,
    )

    ctx = qualitative_context(
        TaskView(course_name="材料力学"),
        [
            SectionView(1, "填空题", rate=D("50.0")),
            SectionView(2, "计算题", rate=D("68.2")),
            SectionView(3, "画图题", rate=D("76.5")),
        ],
        [
            {"goal_code": "目标1", "achievement": "0.595"},
            {"goal_code": "目标2", "achievement": "0.745"},
        ],
    )
    # goal 名里的阿拉伯数字已转中文（模型要原样引用达成结论，不能撞数字校验）
    assert assert_no_digits(ctx)
    assert "目标一未达成" in ctx and "目标二已达成" in ctx
    assert "偏低" in ctx and "中等" in ctx and "较好" in ctx


def _uuid_like(text: str) -> bool:
    import re

    return bool(re.search(r"[0-9a-f]{8}-[0-9a-f]{4}-[0-9a-f]{4}", text))


def test_full_render_preserves_template_structure():
    """结构一致性：渲染只能改值格，不得增删表格/行列、不得动标签与正文段落。"""
    template_bytes = _make_blank_template_docx()
    out = render_paper_analysis(
        template_bytes,
        task=_task_view(),
        sections=_sections_2324(),
        final_score_stats=_stats_dict(),
    )
    before = Document(io.BytesIO(template_bytes))
    after = Document(io.BytesIO(out.docx_bytes))

    # 表格数量与每张表的行列数不变
    assert len(after.tables) == len(before.tables)
    for ti, (t_before, t_after) in enumerate(zip(before.tables, after.tables)):
        assert len(t_after.rows) == len(t_before.rows), f"表{ti} 行数变了"
        assert len(t_after.columns) == len(t_before.columns), f"表{ti} 列数变了"

    # 表格外正文段落一字不动（定性分析段留白由教师写）
    assert [p.text for p in after.paragraphs] == [p.text for p in before.paragraphs]

    # 所有标签格原样保留
    labels = [
        (0, 0, 0, "课程名称"), (0, 0, 4, "学时"), (0, 0, 6, "学分"),
        (0, 0, 9, "考核方式"), (0, 1, 6, "题量"), (0, 1, 9, "考试时间"),
        (0, 2, 0, "参加考试年级"), (0, 2, 3, "专业"), (0, 3, 0, "教学班号"),
        (0, 3, 8, "考试总人数"), (0, 4, 0, "任课教师"), (0, 4, 8, "试卷分析人"),
        (1, 0, 0, "题目（大题）序号"), (1, 5, 0, "试题总个数"), (1, 5, 2, "合计总分"),
        (1, 6, 0, "试题覆盖面"), (2, 1, 6, "平均成绩"), (2, 1, 7, "及格率"),
        (3, 0, 0, "题目（大题）序号"),
    ]
    for ti, ri, ci, expected in labels:
        actual = after.tables[ti].rows[ri].cells[ci].text.strip()
        assert expected in actual, f"表{ti} r{ri}c{ci} 标签丢失: {actual!r}"

    # 任何单元格都不得出现内部 UUID
    for ti, table in enumerate(after.tables):
        for row in table.rows:
            for cell in row.cells:
                assert not _uuid_like(cell.text), f"表{ti} 泄漏内部 ID: {cell.text!r}"


# ── 真实模板回读（挂 skip 名额）──


@pytest.mark.golden
@requires_fmca_golden_dataset
def test_render_real_template_readback():
    """真实空白模板渲染真实 2024-2025 数据，回读断言（§4.2 名额之一）。"""
    assert _REAL_TEMPLATE.is_file(), f"真实模板缺失: {_REAL_TEMPLATE}"
    stats = _synthetic_stats()
    out = render_distribution(_REAL_TEMPLATE.read_bytes(), final_score_stats=stats)
    doc = Document(io.BytesIO(out))
    t2 = doc.tables[2]
    assert t2.rows[2].cells[0].text.strip() == "40.6%"
    assert t2.rows[2].cells[6].text.strip() == "52.88"
    assert t2.rows[2].cells[8].text.strip() == "94"


requires_real_template = pytest.mark.skipif(
    not _REAL_TEMPLATE.is_file(),
    reason="requires the real college blank template (tmp/course_eval_survey)",
)


@pytest.mark.golden
@requires_real_template
def test_real_template_full_conformance_2324():
    """真实模板 × 2023-2024 人工报告数据：内容与结构逐格符合学院要求。

    基准取自人工《成都理工大学课程考试试卷分析表》(2023-2024(1))：
    题型 填空/作图/计算，小题 10/2/4，分值 2/10/15，总分 20/20/60，合计 100 分；
    成绩分布 46.8/9.7/19.3/16.1/8.1/0%，平均 52.66，及格率 43.5%；
    得分率 35.0%/80.6%/49.2%（平均得分 7/16.13/29.53）。
    """
    template_bytes = _REAL_TEMPLATE.read_bytes()
    stats_2324 = {
        "bands": [
            {"label": "50以下", "count": 29, "pct": "46.8"},
            {"label": "51-59", "count": 6, "pct": "9.7"},
            {"label": "60-69", "count": 12, "pct": "19.3"},
            {"label": "70-79", "count": 10, "pct": "16.1"},
            {"label": "80-89", "count": 5, "pct": "8.1"},
            {"label": "90以上", "count": 0, "pct": "0"},
        ],
        "average": "52.66",
        "pass_rate": "43.5",
        "max": "88",
        "min": "6",
    }
    task = TaskView(
        course_name="材料力学", grade="2022级", major="地质工程",
        class_no="2022031001-2022031002", hours="64", credit="4",
        exam_mode="闭卷", course_nature="学科基础课程",
        exam_date="2023 年 12 月 27 日", exam_duration="120 分钟",
        expected_students="62",
    )
    out = render_paper_analysis(
        template_bytes, task=task, sections=_sections_2324(),
        final_score_stats=stats_2324,
    )
    assert out.warnings == []
    before = Document(io.BytesIO(template_bytes))
    doc = Document(io.BytesIO(out.docx_bytes))

    # 结构：4 张表、行列数、表外段落全部与学院模板一致
    assert len(doc.tables) == 4
    for t_before, t_after in zip(before.tables, doc.tables):
        assert len(t_after.rows) == len(t_before.rows)
        assert len(t_after.columns) == len(t_before.columns)
    assert [p.text for p in doc.paragraphs] == [p.text for p in before.paragraphs]

    # 表 0 基本情况
    t0 = doc.tables[0]
    assert t0.rows[0].cells[1].text.strip() == "材料力学"
    assert t0.rows[0].cells[5].text.strip() == "64"
    assert t0.rows[0].cells[7].text.strip() == "4"
    assert t0.rows[0].cells[11].text.strip() == "闭卷"
    assert t0.rows[2].cells[2].text.strip() == "2022级"
    assert t0.rows[2].cells[4].text.strip() == "地质工程"
    assert t0.rows[3].cells[10].text.strip() == "62"
    assert "☑" in t0.rows[1].cells[1].text  # 课程属性已勾选

    # 表 1 题型及分值分布（与人工表逐格一致）
    t1 = doc.tables[1]
    assert [t1.rows[1].cells[c].text.strip() for c in (1, 2, 3)] == [
        "填空题", "作图题", "计算题",
    ]
    assert [t1.rows[2].cells[c].text.strip() for c in (1, 2, 3)] == ["10", "2", "4"]
    assert [t1.rows[3].cells[c].text.strip() for c in (1, 2, 3)] == ["2", "10", "15"]
    assert [t1.rows[4].cells[c].text.strip() for c in (1, 2, 3)] == ["20", "20", "60"]
    assert t1.rows[5].cells[4].text.strip() == "100 分"
    # 未使用列（四~十，含合并跳号列）无残留
    header_cols = [5, 6, 7, 9, 10, 11, 12]
    for ri in (1, 2, 3, 4):
        for ci in header_cols:
            assert t1.rows[ri].cells[ci].text.strip() == ""

    # 表 2 成绩分布（与人工表一致）
    t2 = doc.tables[2]
    assert [t2.rows[2].cells[c].text.strip() for c in range(6)] == [
        "46.8%", "9.7%", "19.3%", "16.1%", "8.1%", "0.0%",
    ]
    assert t2.rows[2].cells[6].text.strip() == "52.66"
    assert t2.rows[2].cells[7].text.strip() == "43.5%"
    assert t2.rows[2].cells[8].text.strip() == "88"
    assert t2.rows[2].cells[9].text.strip() == "6"

    # 表 3 得分率（与人工表一致；未用列预置 % 清空）
    t3 = doc.tables[3]
    assert [t3.rows[1].cells[c].text.strip() for c in (1, 2, 3)] == ["20", "20", "60"]
    assert [t3.rows[2].cells[c].text.strip() for c in (1, 2, 3)] == [
        "7.00", "16.13", "29.53",
    ]
    assert [t3.rows[3].cells[c].text.strip() for c in (1, 2, 3)] == [
        "35.0%", "80.6%", "49.2%",
    ]
    for ci in range(4, 11):
        assert t3.rows[3].cells[ci].text.strip() == ""

    # 全文不得出现内部 UUID
    for table in doc.tables:
        for row in table.rows:
            for cell in row.cells:
                assert not _uuid_like(cell.text)
