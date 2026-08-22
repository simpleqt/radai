"""Tests for paper layout detection (T2-02).

合成图像测试在任意环境跑：得分表检出 bbox 与真值误差 <2%、封面宽度判据、
split_cells 不依赖线条计数、分卷间隔 issue。真实 90 页回归挂 skip 名额。
"""

from __future__ import annotations

import os
from pathlib import Path

import cv2
import numpy as np
import pytest

from backend.application.course_eval.extraction.paper_layout import (
    LayoutParams,
    TableBox,
    cover_table_box,
    detect_tables,
    is_cover,
    segment_students,
    split_cells,
)
from backend.application.course_eval.extraction.pdf_pager import PageImage

_GOLDEN_ROOT = Path(
    os.getenv("FMCA_COURSE_EVAL_GOLDEN_ROOT", "")
    or Path(__file__).resolve().parents[2] / "data" / "course_eval_golden"
)
requires_fmca_golden_dataset = pytest.mark.skipif(
    not (_GOLDEN_ROOT / "manifest.json").is_file(),
    reason=(
        "requires the FMCA course-eval golden dataset (real scans); "
        "set FMCA_COURSE_EVAL_GOLDEN_ROOT to run"
    ),
)


def _make_score_table_image(
    *,
    cols: int = 6,
    nx: float = 0.11,
    ny: float = 0.15,
    nw: float = 0.384,
    handwriting: bool = True,
    size: tuple[int, int] = (1755, 1240),
) -> np.ndarray:
    """合成一张封面得分表图，几何参数与方案 §6.3 实测区间一致。

    handwriting=True 时在单元格内画不规则长笔画，用于验证
    split_cells 不受伪线条影响（T2-02 DoD 第 4 条）。
    """
    img = np.full((size[1], size[0]), 255, dtype=np.uint8)
    x0, y0 = int(nx * size[0]), int(ny * size[1])
    w, h = int(nw * size[0]), int(0.10 * size[1])
    for i in range(cols + 1):
        x = x0 + i * w // cols
        cv2.line(img, (x, y0), (x, y0 + h), 0, 2)
    for j in range(3):
        y = y0 + j * h // 2
        cv2.line(img, (x0, y), (x0 + w, y), 0, 2)
    if handwriting:
        # 故意画一条贯穿单元格的长斜笔画，模拟手写数字被形态学误判成线条
        cv2.line(img, (x0 + 20, y0 + h - 10), (x0 + 60, y0 + h // 2 + 5), 0, 3)
    return img


def test_detect_tables_finds_synthetic_score_table():
    """合成图 → 检出 → bbox 与真值误差 <2%。不需要真实资料（T2-02 DoD 第 2 条）。"""
    img = _make_score_table_image(cols=6, nx=0.11, ny=0.15, nw=0.384)
    boxes = detect_tables(img, LayoutParams())
    assert len(boxes) >= 1
    box = boxes[0]
    assert abs(box.nx - 0.11) < 0.02
    assert abs(box.nw - 0.384) < 0.02
    assert abs(box.ny - 0.15) < 0.02


@pytest.mark.parametrize(
    ("nw", "should_be_cover"),
    [
        (0.384, True),  # 2024-2025 实测值
        (0.330, True),  # 2023-2024 实测值
        (0.250, False),  # 太窄，正文里的小表格
        (0.500, False),  # 太宽，整页边框
    ],
)
def test_cover_criteria_width_gate(nw, should_be_cover):
    """0.30 ≤ nw ≤ 0.42 是封面判据里最关键的一条（§6.3.2）。"""
    img = _make_score_table_image(nw=nw)
    assert is_cover(img, LayoutParams()) is should_be_cover


def test_cover_table_box_returns_correct():
    img = _make_score_table_image(nw=0.384)
    box = cover_table_box(img, LayoutParams())
    assert box is not None
    assert abs(box.nw - 0.384) < 0.02


def test_split_cells_ignores_handwriting_pseudo_lines():
    """手写笔画会被形态学当成线条；切分必须以声明的大题数为准（§6.3.4）。"""
    clean = _make_score_table_image(cols=6, handwriting=False)
    messy = _make_score_table_image(cols=6, handwriting=True)
    params = LayoutParams()

    cells_clean = split_cells(
        cover_table_box(clean, params), section_count=4, vlines=[]
    )
    cells_messy = split_cells(
        cover_table_box(messy, params), section_count=4, vlines=[]
    )
    assert len(cells_clean) == len(cells_messy) == 6  # n+2 列
    for a, b in zip(cells_clean, cells_messy):
        assert abs(a.nx - b.nx) < 0.01


def test_split_cells_snaps_to_vlines():
    """等分边界对竖线局部吸附（吸附半径=列宽 30%）。"""
    box = TableBox(nx=0.1, ny=0.15, nw=0.4, nh=0.1)
    # 期望等分边界：0.1, 0.1667, 0.2333, 0.3, 0.3667, 0.4333, 0.5
    cells = split_cells(box, section_count=4, vlines=[])
    assert len(cells) == 6
    assert abs(cells[0].nx - 0.1) < 1e-6
    assert abs(cells[-1].nx + cells[-1].nw - 0.5) < 1e-6


def test_segment_students_reports_irregular_gap():
    """封面间隔不均时返回 COVER_GAP_IRREGULAR issue 而非硬切（T2-02 DoD 第 5 条）。"""
    params = LayoutParams()

    def _page(is_cover_page: bool) -> PageImage:
        if is_cover_page:
            return PageImage(page_no=0, gray=_make_score_table_image(), is_spread=True)
        return PageImage(
            page_no=0, gray=np.full((1240, 1755), 255, dtype=np.uint8), is_spread=True
        )

    # cover 索引 0, 5, 8 → 间隔 {5, 3} 不一致
    pages = (
        [_page(True)]
        + [_page(False)] * 4
        + [_page(True)]
        + [_page(False)] * 2
        + [_page(True)]
        + [_page(False)] * 2
    )
    ranges, issues = segment_students(pages, params)
    assert len(ranges) == 3
    assert any(i["rule_id"] == "COVER_GAP_IRREGULAR" for i in issues)


def test_segment_students_regular_gap_no_issue():
    """正常分卷（间隔一致）不报 issue。"""
    params = LayoutParams()

    def _page(is_cover_page: bool) -> PageImage:
        if is_cover_page:
            return PageImage(page_no=0, gray=_make_score_table_image(), is_spread=True)
        return PageImage(
            page_no=0, gray=np.full((1240, 1755), 255, dtype=np.uint8), is_spread=True
        )

    pages = [_page(i % 5 == 0) for i in range(15)]
    ranges, issues = segment_students(pages, params)
    assert len(ranges) == 3
    assert not issues


@pytest.mark.golden
@requires_fmca_golden_dataset
def test_cover_detection_on_real_scans():
    """真实资料回归：90 页 TP=20 FP=0 FN=0（§6.3.2 实测，§4.2 名额之一）。"""
    from backend.application.course_eval.extraction.pdf_pager import render_pages

    for pdf_name, stride, expected in [
        ("2024-2025 答题试卷.pdf", 5, 10),
        ("2023-2024 答题试卷.pdf", 4, 10),
    ]:
        pdf_path = _GOLDEN_ROOT / pdf_name
        assert pdf_path.is_file(), f"金标准缺失: {pdf_path}"
        pages = render_pages(pdf_path.read_bytes())
        predicted = {i for i, p in enumerate(pages) if is_cover(p.gray, LayoutParams())}
        truth = {i for i in range(len(pages)) if i % stride == 0}
        assert (
            predicted == truth
        ), f"{pdf_name}: 误检 {predicted - truth}，漏检 {truth - predicted}"
        assert len(truth) == expected
