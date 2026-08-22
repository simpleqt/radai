"""PDF 渲染与整页输出（M-03 / T2-01）。

- render_pages：整页渲染 @150dpi（实测两份资料均 1755x1240），**不切中缝**
  （§6.3.0：中缝检测不可靠，左右归属由 bbox 中心判定）。
- crop_region：按归一化 bbox 局部高清重渲染 @300dpi，供单元格裁图。
- is_watermark_image：剔除 203x69 的「扫描全能王」水印图（固定尺寸）。
"""

from __future__ import annotations

from dataclasses import dataclass

import numpy as np
import pymupdf

# 实测水印尺寸（§1.2）：203 x 69 px，扫描全能王
_WATERMARK_W = 203
_WATERMARK_H = 69
_WATERMARK_TOL = 8  # 允许少量尺寸抖动


@dataclass(frozen=True)
class PageImage:
    """整页渲染结果。gray 为 0-255 uint8 数组。"""

    page_no: int
    gray: np.ndarray
    is_spread: bool  # 横向双页拼版


@dataclass(frozen=True)
class NormBox:
    """归一化 bbox（相对页面宽高）。"""

    nx: float
    ny: float
    nw: float
    nh: float


def render_pages(pdf_bytes: bytes, *, dpi: int = 150) -> list[PageImage]:
    """整页渲染，不做中缝切分。"""
    doc = pymupdf.open(stream=pdf_bytes, filetype="pdf")
    out: list[PageImage] = []
    try:
        for pno, page in enumerate(doc):
            pix = page.get_pixmap(dpi=dpi)
            img = np.frombuffer(pix.samples, dtype=np.uint8).reshape(
                pix.height, pix.width, pix.n
            )
            if pix.n >= 3:
                gray = np.ascontiguousarray(img[:, :, :3]) @ np.array(
                    [0.299, 0.587, 0.114], dtype=np.float32
                )
                gray = gray.astype(np.uint8)
            else:
                gray = img[:, :, 0]
            out.append(
                PageImage(
                    page_no=pno,
                    gray=gray,
                    is_spread=page.rect.width > page.rect.height,
                )
            )
    finally:
        doc.close()
    return out


def crop_region(
    pdf_bytes: bytes, page_no: int, bbox: NormBox, *, dpi: int = 300
) -> bytes:
    """按归一化 bbox 局部高清重渲染，返回 PNG 字节。"""
    doc = pymupdf.open(stream=pdf_bytes, filetype="pdf")
    try:
        page = doc[page_no]
        rect = page.rect
        clip = pymupdf.Rect(
            bbox.nx * rect.width,
            bbox.ny * rect.height,
            (bbox.nx + bbox.nw) * rect.width,
            (bbox.ny + bbox.nh) * rect.height,
        )
        pix = page.get_pixmap(dpi=dpi, clip=clip)
        return pix.tobytes("png")
    finally:
        doc.close()


def is_watermark_image(width: int, height: int) -> bool:
    """剔除 203x69 的扫描全能王水印图。"""
    return (
        abs(width - _WATERMARK_W) <= _WATERMARK_TOL
        and abs(height - _WATERMARK_H) <= _WATERMARK_TOL
    )


def render_page_to_png(pdf_bytes: bytes, page_no: int, *, dpi: int = 150) -> bytes:
    """整页渲染为 PNG 字节（供前端页面清单展示）。"""
    doc = pymupdf.open(stream=pdf_bytes, filetype="pdf")
    try:
        pix = doc[page_no].get_pixmap(dpi=dpi)
        return pix.tobytes("png")
    finally:
        doc.close()
