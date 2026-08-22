"""Tests for the grade-sheet coordinate parser (T1-05).

确定性逻辑用合成夹具在任意环境跑；「69/62 条真实复算」挂
requires_fmca_golden_dataset skip 标记（§4.2 四个名额之一）。
"""

from __future__ import annotations

import os
from decimal import Decimal
from pathlib import Path

import pytest

from backend.application.course_eval.extraction import grade_sheet_parser as gsp
from backend.application.course_eval.extraction.grade_sheet_parser import (
    Word,
    assign_cells,
    cluster_rows,
    detect_columns,
)

_GOLDEN_ROOT = Path(
    os.getenv("FMCA_COURSE_EVAL_GOLDEN_ROOT", "")
    or Path(__file__).resolve().parents[2] / "data" / "course_eval_golden"
)
requires_fmca_golden_dataset = pytest.mark.skipif(
    not (_GOLDEN_ROOT / "manifest.json").is_file(),
    reason=(
        "requires the FMCA course-eval golden dataset (real grade-sheet PDFs); "
        "set FMCA_COURSE_EVAL_GOLDEN_ROOT to run"
    ),
)

HEADERS = ("学号", "平时", "期末", "总成绩", "标志")

# 实测列锚点（§1.3）：左栏 61.9 / 185.3 / 213.0 / 244.3，右栏 = 左栏 + 246.3
L = {"学号": 61.9, "平时": 185.3, "期末": 213.0, "总成绩": 244.3, "标志": 272.0}
GAP = 246.3


def _w(text: str, x0: float, y0: float = 100.0) -> Word:
    return Word(text=text, x0=x0, y0=y0, x1=x0 + len(text) * 5.0, y1=y0 + 8.0)


def _make_grade_sheet_words(*, shuffled: bool = False) -> list[Word]:
    """合成带 bbox 的词序列：每行并排两名学生（左栏 + 右栏）。"""
    pairs = [
        (
            "990103000101",
            "张一鸣",
            "75",
            "68",
            "71",
            "990103000102",
            "",
            None,
            None,
            None,
        ),
        (
            "990103000103",
            "李二萌",
            "90",
            "87",
            "88",
            "990103000104",
            "陈普阳",
            "90",
            "88",
            "89",
        ),
        (
            "990103000105",
            "王三强",
            "90",
            "84",
            "86",
            "990103000106",
            "赵四美",
            "75",
            "64",
            "68",
        ),
    ]
    words: list[Word] = []
    for row_idx, row in enumerate(pairs):
        y0 = 160.9 + row_idx * 18.6
        sid_l, name_l, r_l, f_l, t_l, sid_r, name_r, r_r, f_r, t_r = row
        words.append(_w(sid_l, L["学号"], y0))
        words.append(_w(name_l, 139.9, y0 - 0.5))
        words.append(_w(r_l, L["平时"], y0))
        words.append(_w(f_l, L["期末"], y0))
        words.append(_w(t_l, L["总成绩"], y0))
        words.append(_w(sid_r, L["学号"] + GAP, y0))
        if name_r:
            words.append(_w(name_r, 139.9 + GAP, y0 - 0.5))
        if r_r:
            words.append(_w(r_r, L["平时"] + GAP, y0))
            words.append(_w(f_r, L["期末"] + GAP, y0))
            words.append(_w(t_r, L["总成绩"] + GAP, y0))
        else:
            words.append(_w("缺考", L["标志"] + GAP, y0))
    if shuffled:
        import random

        random.Random(42).shuffle(words)
    return words


# ── 阶段函数单元测试 ──


def test_cluster_rows_tolerates_baseline_jitter():
    """同一行内 0.5pt 的基线差不得拆散同伴（贪心合并，band 边界失效场景）。"""
    words = [_w("张三", 100.0, 100.0), _w("85", 150.0, 101.5), _w("90", 200.0, 99.2)]
    rows = cluster_rows(words)
    assert len(rows) == 1


def test_cluster_rows_separates_far_rows():
    words = [_w("a", 100.0, 100.0), _w("b", 100.0, 140.0)]  # 40pt 间距
    rows = cluster_rows(words)
    assert len(rows) == 2


def test_detect_columns_left_and_right_anchors():
    """列检测：学号聚两簇得左右栏，数字列偏移正确。"""
    words = _make_grade_sheet_words()
    columns = detect_columns(cluster_rows(words), header_keywords=HEADERS)
    by_name = {c.name: c.x for c in columns}
    assert abs(by_name["学号"] - L["学号"]) < 10
    assert abs(by_name["平时"] - L["平时"]) < 10
    assert abs(by_name["期末"] - L["期末"]) < 10
    assert abs(by_name["总成绩"] - L["总成绩"]) < 10


def test_assign_cells_reconstructs_shuffled_text_layer():
    """PDF 文本层顺序错乱时，坐标法仍能还原正确行列（§1.3，坐标法的存在理由）。"""
    ordered = _make_grade_sheet_words(shuffled=False)
    shuffled = _make_grade_sheet_words(shuffled=True)
    columns = detect_columns(cluster_rows(ordered), header_keywords=HEADERS)
    a = assign_cells(cluster_rows(ordered), columns)
    b = assign_cells(cluster_rows(shuffled), columns)
    assert a == b


def test_assign_cells_two_per_row_by_x():
    """每行并排两名学生：数字按 x 分配给最近的左侧学号（T1-05 DoD 第 2 条）。"""
    records = assign_cells(
        cluster_rows(_make_grade_sheet_words()),
        detect_columns(
            cluster_rows(_make_grade_sheet_words()), header_keywords=HEADERS
        ),
    )
    assert len(records) == 6
    r = {rec.student_no: rec for rec in records}
    assert r["990103000101"].regular == Decimal("75")
    assert r["990103000101"].total == Decimal("71")
    # 右栏学生
    assert r["990103000104"].regular == Decimal("90")
    assert r["990103000104"].final == Decimal("88")
    # 缺考行：只有标志，无成绩
    assert r["990103000102"].flag == "缺考"
    assert r["990103000102"].final is None


def test_recompute_matches_total():
    """合成成绩单：逐行复算 平时×0.4 + 期末×0.6 == 总评（容差 0.5）。

    合成夹具只验证逻辑，不承诺 69/62 条——那组金标准数字由真实复算用例承担。
    """
    words = _make_grade_sheet_words()
    columns = detect_columns(cluster_rows(words), header_keywords=HEADERS)
    records = assign_cells(cluster_rows(words), columns)
    assert len(records) >= 2
    for r in records:
        if r.regular is None or r.final is None or r.total is None:
            continue
        expected = r.regular * Decimal("0.4") + r.final * Decimal("0.6")
        assert abs(expected - r.total) <= Decimal("0.5"), f"{r.student_no} 复算不符"
        assert len(r.student_no) == 12 and r.student_no.isdigit()


# ── 真实资料金标准（挂 skip 标记，§4.2 名额）──


@pytest.mark.golden
@requires_fmca_golden_dataset
def test_real_grade_sheet_recompute():
    """真实成绩单：69/69 与 62/62 全部相符、期末均分与人工报告一致（§1.5 结论 A/B）。"""
    cases = [
        ("2024-2025 课程成绩单.pdf", 69, Decimal("52.88")),
        ("2023-2024 课程成绩单.pdf", 62, Decimal("52.66")),
    ]
    for pdf_name, expected_n, expected_avg in cases:
        pdf_path = _GOLDEN_ROOT / pdf_name
        assert pdf_path.is_file(), f"金标准缺失: {pdf_path}"
        result = gsp.parse_grade_sheet(pdf_path.read_bytes())
        valid = [r for r in result.records if not r.flag]
        assert len(valid) == expected_n, f"{pdf_name}: 有效记录数不符"
        for r in valid:
            assert r.name, f"{r.student_no} 姓名缺失"
            expected = r.regular * Decimal("0.4") + r.final * Decimal("0.6")
            assert abs(expected - r.total) <= Decimal("0.5"), f"{r.student_no} 复算不符"
            assert len(r.student_no) == 12 and r.student_no.isdigit()
        finals = [r.final for r in valid if r.final is not None]
        avg = sum(finals, Decimal(0)) / len(finals)
        assert abs(avg - expected_avg) <= Decimal("0.01"), f"{pdf_name}: 期末均分 {avg}"


# ── 真实成绩单的完整业务场景（合成 PDF 构造 + 金标准数字复算）──


def _make_synthetic_grade_sheet_pdf() -> bytes:
    """用 pymupdf 合成一份与真实成绩单同构的双栏 PDF（每行两名学生、含缺考行）。

    列坐标严格对齐 §1.3 实测：左栏 61.9/185.3/213.0/244.3，右栏 = 左栏 + 246.3。
    """
    import pymupdf

    doc = pymupdf.open()
    page = doc.new_page(width=595, height=842)
    # 表头
    for x, label in [
        (82.1, "学号"),
        (180.2, "平时"),
        (208.0, "期末"),
        (244.3, "总成绩"),
    ]:
        page.insert_text((x, 130), label, fontsize=9)
    rows = [
        ("990103000101", "75", "68", "71", "990103000102", None, None, None, "缺考"),
        ("990103000103", "90", "87", "88", "990103000104", "90", "88", "89", None),
        ("990103000105", "90", "84", "86", "990103000106", "75", "64", "68", None),
        ("990103000107", "85", "81", "83", "990103000108", "80", "73", "76", None),
        ("990103000109", "95", "94", "94", "990103000110", "60", "61", "61", None),
        ("990103000111", "75", "60", "66", "990103000112", "70", "69", "69", None),
    ]
    for i, row in enumerate(rows):
        y = 160 + i * 18.6
        sid_l, r_l, f_l, t_l, sid_r, r_r, f_r, t_r, flag = row
        page.insert_text((61.9, y), sid_l, fontsize=9)
        page.insert_text((185.3, y), r_l, fontsize=9)
        page.insert_text((213.0, y), f_l, fontsize=9)
        page.insert_text((244.3, y), t_l, fontsize=9)
        page.insert_text((308.2, y), sid_r, fontsize=9)
        if r_r:
            page.insert_text((431.5, y), r_r, fontsize=9)
            page.insert_text((459.3, y), f_r, fontsize=9)
            page.insert_text((490.6, y), t_r, fontsize=9)
        else:
            page.insert_text((518.4, y), flag, fontsize=9)
    data = doc.tobytes()
    doc.close()
    return data


def test_parse_synthetic_grade_sheet_full_flow():
    """合成 PDF 全流程：双栏解析 + 缺考行 + 复算。"""
    result = gsp.parse_grade_sheet(_make_synthetic_grade_sheet_pdf())
    # 6 行 × 2 栏 = 12 条（含 1 条缺考行：pymupdf 合成 PDF 会把中文拆成单字 word，
    # 缺考标志文本的匹配由 word 级构造用例 test_assign_cells_two_per_row_by_x 覆盖；
    # 此处验证 §6.2 健壮性措施 4：缺考行数字缺失不得报解析失败）
    assert len(result.records) == 12
    valid = [r for r in result.records if r.regular is not None and r.final is not None]
    assert len(valid) == 11
    absent = [r for r in result.records if r.student_no == "990103000102"][0]
    assert absent.final is None and absent.regular is None  # 缺考行无成绩数字
    # 复算全符
    for r in valid:
        expected = r.regular * Decimal("0.4") + r.final * Decimal("0.6")
        assert abs(expected - r.total) <= Decimal("0.5")
        assert len(r.student_no) == 12 and r.student_no.isdigit()


def test_parse_synthetic_grade_sheet_distribution():
    """解析结果 → 统计引擎分布，验证成绩单链路端到端。"""
    from backend.application.course_eval.stats_engine import (
        distribute_paper_analysis,
        distribute_grade_sheet,
    )

    result = gsp.parse_grade_sheet(_make_synthetic_grade_sheet_pdf())
    valid = [r for r in result.records if not r.flag]
    finals = [r.final for r in valid if r.final is not None]
    totals = [r.total for r in valid if r.total is not None]
    # 试卷分析 6 段（期末卷面）与成绩单 5 段（总评）分属不同口径
    paper_dist = distribute_paper_analysis(finals)
    sheet_dist = distribute_grade_sheet(totals)
    assert len(paper_dist.bands) == 6
    assert len(sheet_dist.bands) == 5
    assert sum(b.count for b in paper_dist.bands) == len(finals)
    assert sum(b.count for b in sheet_dist.bands) == len(totals)


def test_detect_columns_with_no_header_fallback():
    """表头缺失时回退到左栏数字聚类（§6.2 健壮性措施 1/2）。"""
    words = []
    for i in range(6):
        y = 160 + i * 18.6
        words += [
            Word("990103000101", 61.9, y, 122, y + 8),
            Word("75", 185.3, y, 195, y + 8),
            Word("68", 213.0, y, 223, y + 8),
            Word("71", 244.3, y, 254, y + 8),
        ]
    rows = cluster_rows(words)
    columns = detect_columns(rows, header_keywords=())
    by_name = {c.name: c.x for c in columns}
    assert abs(by_name["平时"] - 185.3) < 8
    assert abs(by_name["期末"] - 213.0) < 8
    assert abs(by_name["总成绩"] - 244.3) < 8
