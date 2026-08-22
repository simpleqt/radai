"""扫描卷版面检测（M-03 / T2-02）。

方案 §6.3 实测定稿：
- 形态学横竖线提取 → 网格 → 连通域 → 「横线≥3 且竖线≥4」筛表格候选；
- 封面判别是四条几何判据的合取（纯几何，不用 OCR 文本）；
- 单元格切分**不依赖线条计数**（手写笔画会污染），以声明大题数等分 + 竖线吸附；
- 分卷间隔不均时返回 issue 而非硬切。

detect_tables / is_cover / cover_table_box / split_cells / segment_students
吃 numpy 数组吐 dataclass，[纯] 函数，可用合成图像穷举测试。
"""

from __future__ import annotations

from dataclasses import dataclass

import cv2
import numpy as np

from backend.application.course_eval.extraction.pdf_pager import NormBox, PageImage


@dataclass(frozen=True)
class LayoutParams:
    """全部默认值来自方案 §6.3 的 90 页实测（可随 ce_report_templates 按学年存）。"""

    dpi: int = 150
    adaptive_block_size: int = 25
    adaptive_c: int = 10
    h_kernel_ratio: int = 45  # W // 45
    v_kernel_ratio: int = 55  # H // 55
    min_table_w_ratio: float = 0.12
    cover_nx_max: float = 0.50
    cover_ny_max: float = 0.35
    cover_nw_range: tuple[float, float] = (0.30, 0.42)
    cover_aspect_max: float = 0.45
    width_consistency_tol: float = 0.05
    student_gap_tol: int = 1  # 封面间隔允许的类别数


@dataclass(frozen=True)
class TableBox:
    """检出的表格 bbox（归一化）。"""

    nx: float
    ny: float
    nw: float
    nh: float


@dataclass(frozen=True)
class StudentPageRange:
    """一名学生的 PDF 页范围（封面页索引起，不含下一名）。"""

    cover_page: int
    end_page: int  # 最后一个 PDF 页索引（含）


def _binarize(gray: np.ndarray, params: LayoutParams) -> np.ndarray:
    return cv2.adaptiveThreshold(
        gray,
        255,
        cv2.ADAPTIVE_THRESH_MEAN_C,
        cv2.THRESH_BINARY_INV,
        params.adaptive_block_size,
        params.adaptive_c,
    )


def _grid_mask(
    gray: np.ndarray, params: LayoutParams
) -> tuple[np.ndarray, np.ndarray, np.ndarray]:
    """形态学横竖线提取。返回 (grid, hor, ver)：

    - grid 用于连通域找表格外框（3x3 dilate 加粗）；
    - hor/ver 是未加粗的线图，用于 rows/cols 统计（A.2 范式：
      grid 加粗后整块会被误判为"全线"，线数统计必须用线图本身）。
    """
    bw = _binarize(gray, params)
    h, w = gray.shape
    hk = cv2.getStructuringElement(
        cv2.MORPH_RECT, (max(20, w // params.h_kernel_ratio), 1)
    )
    vk = cv2.getStructuringElement(
        cv2.MORPH_RECT, (1, max(12, h // params.v_kernel_ratio))
    )
    hor = cv2.dilate(cv2.erode(bw, hk), hk)
    ver = cv2.dilate(cv2.erode(bw, vk), vk)
    grid = cv2.dilate(cv2.bitwise_or(hor, ver), np.ones((3, 3), np.uint8))
    return grid, hor, ver


def detect_tables(
    gray: np.ndarray, params: LayoutParams | None = None
) -> list[TableBox]:
    """形态学网格检测，返回归一化 bbox。

    rows/cols 统计仅用于候选筛选（横线≥3 且竖线≥4），**不可信**（§6.3.4：
    手写笔画会被当线条），切分不能用它。
    """
    params = params or LayoutParams()
    grid, hor, ver = _grid_mask(gray, params)
    h, w = gray.shape
    n, _, stats, _ = cv2.connectedComponentsWithStats(grid, 8)
    out: list[TableBox] = []
    for i in range(1, n):
        x, y, bw_, bh, _area = stats[i]
        if bw_ < w * params.min_table_w_ratio:
            continue
        if bh < h * 0.02 or bh > h * 0.5:
            continue
        rows = int(
            np.diff(
                np.r_[
                    0,
                    (hor[y : y + bh, x : x + bw_].sum(axis=1) > bw_ * 0.6).astype(int),
                ]
            )
            .clip(0)
            .sum()
        )
        cols = int(
            np.diff(
                np.r_[
                    0, (ver[y : y + bh, x : x + bw_].sum(axis=0) > bh * 0.6).astype(int)
                ]
            )
            .clip(0)
            .sum()
        )
        if rows >= 3 and cols >= 4:
            out.append(TableBox(nx=x / w, ny=y / h, nw=bw_ / w, nh=bh / h))
    return out


def is_cover(gray: np.ndarray, params: LayoutParams | None = None) -> bool:
    """四条几何判据的合取（§6.3.2，实测 90 页零误检）。"""
    params = params or LayoutParams()
    lo, hi = params.cover_nw_range
    for box in detect_tables(gray, params):
        if (
            box.nx < params.cover_nx_max
            and box.ny < params.cover_ny_max
            and lo <= box.nw <= hi
            and box.nh / box.nw < params.cover_aspect_max
        ):
            return True
    return False


def cover_table_box(
    gray: np.ndarray, params: LayoutParams | None = None
) -> TableBox | None:
    """取封面得分表 bbox（is_cover 为 True 时才有意义）。"""
    params = params or LayoutParams()
    lo, hi = params.cover_nw_range
    for box in detect_tables(gray, params):
        if (
            box.nx < params.cover_nx_max
            and box.ny < params.cover_ny_max
            and lo <= box.nw <= hi
            and box.nh / box.nw < params.cover_aspect_max
        ):
            return box
    return None


def split_cells(
    box: TableBox,
    section_count: int,
    vlines: list[float],
    *,
    snap_ratio: float = 0.30,
) -> list[NormBox]:
    """按声明大题数等分 + 竖线吸附，不数线条（§6.3.4）。

    列数 = section_count + 2（表头列 + n 个题号列 + 总分列），等分宽度
    = box.nw / (section_count + 2)。vlines 为归一化竖线 x，仅用于局部吸附。
    """
    col_width = box.nw / (section_count + 2)
    edges = [box.nx + i * col_width for i in range(section_count + 3)]
    # 吸附：对每个内部边界，找最近的竖线（容差 = col_width * snap_ratio）
    for i in range(1, len(edges) - 1):
        best = edges[i]
        best_d = col_width * snap_ratio
        for vline in vlines:
            d = abs(vline - edges[i])
            if d < best_d:
                best_d = d
                best = vline
        edges[i] = best
    return [
        NormBox(
            nx=edges[i],
            ny=box.ny,
            nw=edges[i + 1] - edges[i],
            nh=box.nh,
        )
        for i in range(section_count + 2)
    ]


def segment_students(
    pages: list[PageImage], params: LayoutParams | None = None
) -> tuple[list[StudentPageRange], list[dict]]:
    """分卷 + 间隔/宽度一致性校验（§6.3.2）。间隔不均报 issue 而非硬切。"""
    params = params or LayoutParams()
    covers = [i for i, p in enumerate(pages) if is_cover(p.gray, params)]
    issues: list[dict] = []

    gaps = {covers[i + 1] - covers[i] for i in range(len(covers) - 1)}
    if len(gaps) > 1:
        issues.append(
            {
                "rule_id": "COVER_GAP_IRREGULAR",
                "detail": f"封面间隔不均：{sorted(gaps)}",
                "level": "warning",
            }
        )

    widths = [cover_table_box(pages[i].gray, params).nw for i in covers]
    if widths and (max(widths) - min(widths)) > params.width_consistency_tol:
        issues.append(
            {
                "rule_id": "COVER_TABLE_WIDTH_INCONSISTENT",
                "detail": f"得分表宽度不一致：{[round(w, 3) for w in widths]}",
                "level": "warning",
            }
        )

    ranges = [
        StudentPageRange(
            cover_page=covers[i],
            end_page=(covers[i + 1] - 1 if i + 1 < len(covers) else len(pages) - 1),
        )
        for i in range(len(covers))
    ]
    return ranges, issues
