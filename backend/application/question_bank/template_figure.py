"""题库上传模板内嵌的示例受力图（运行时生成，不把二进制放进 Git）。

与 ``mechanics_diagram_tool`` 同款纪律：面向对象 Figure + FigureCanvasAgg，
不碰 pyplot 全局态；字节按进程缓存，模板下载端点只在首次绘制时付一次成本。
"""

from __future__ import annotations

import io

import matplotlib

matplotlib.use("Agg")

from matplotlib.backends.backend_agg import FigureCanvasAgg  # noqa: E402
from matplotlib.figure import Figure  # noqa: E402
from matplotlib.patches import Circle  # noqa: E402

_TEMPLATE_FIGURE_CACHE: bytes | None = None


def build_template_figure_png() -> bytes:
    """简支梁跨中集中力受力图（A 固定铰 / B 滚动铰 / 虚线支反力 / 尺寸标注）。"""
    global _TEMPLATE_FIGURE_CACHE
    if _TEMPLATE_FIGURE_CACHE is not None:
        return _TEMPLATE_FIGURE_CACHE

    figure = Figure(figsize=(6.4, 3.2), dpi=150)
    FigureCanvasAgg(figure)  # 绑定渲染器（tight_layout/savefig 需要）
    axis = figure.add_subplot(111)
    axis.set_xlim(-0.8, 4.8)
    axis.set_ylim(-1.15, 1.75)
    axis.set_aspect("equal")
    axis.axis("off")

    # 梁 AB（双线）
    axis.plot([0, 4], [0.06, 0.06], color="#1f2937", lw=2.5)
    axis.plot([0, 4], [-0.06, -0.06], color="#1f2937", lw=2.5)
    # 固定铰支座 A（三角形 + 斜线基础）
    axis.fill(
        [0, -0.28, 0.28], [0, -0.5, -0.5], color="none", edgecolor="#1f2937", lw=1.8
    )
    for x in (-0.42, -0.28, -0.14, 0, 0.14, 0.28):
        axis.plot([x, x - 0.14], [-0.5, -0.66], color="#6b7280", lw=1.0)
    # 滚动铰支座 B（三角形 + 滚轮 + 基础线）
    axis.fill(
        [4, 3.74, 4.26], [0, -0.44, -0.44], color="none", edgecolor="#1f2937", lw=1.8
    )
    axis.add_patch(Circle((3.87, -0.53), 0.09, fill=False, ec="#1f2937", lw=1.2))
    axis.add_patch(Circle((4.13, -0.53), 0.09, fill=False, ec="#1f2937", lw=1.2))
    axis.plot([3.6, 4.4], [-0.66, -0.66], color="#6b7280", lw=1.4)
    # 跨中集中力
    axis.annotate(
        "",
        xy=(2, 0.1),
        xytext=(2, 1.15),
        arrowprops={"arrowstyle": "-|>", "color": "#b91c1c", "lw": 2.4},
    )
    axis.text(2.1, 0.95, r"$P=10\ \mathrm{kN}$", fontsize=13, color="#b91c1c")
    # 支反力（虚线）
    axis.annotate(
        "",
        xy=(0, -0.02),
        xytext=(0, -1.0),
        arrowprops={"arrowstyle": "-|>", "color": "#1d4ed8", "lw": 1.8, "ls": "--"},
    )
    axis.text(0.1, -1.02, r"$F_A$", fontsize=12, color="#1d4ed8")
    axis.annotate(
        "",
        xy=(4, -0.02),
        xytext=(4, -1.0),
        arrowprops={"arrowstyle": "-|>", "color": "#1d4ed8", "lw": 1.8, "ls": "--"},
    )
    axis.text(4.1, -1.02, r"$F_B$", fontsize=12, color="#1d4ed8")
    axis.text(-0.16, 0.16, "A", fontsize=13, style="italic")
    axis.text(4.08, 0.16, "B", fontsize=13, style="italic")
    # 跨度尺寸线
    axis.annotate(
        "",
        xy=(0, 1.35),
        xytext=(4, 1.35),
        arrowprops={"arrowstyle": "<|-|>", "color": "#374151", "lw": 1.2},
    )
    axis.text(1.85, 1.44, r"$l=4\ \mathrm{m}$", fontsize=12, color="#374151")

    figure.tight_layout()
    buffer = io.BytesIO()
    figure.savefig(buffer, format="png")
    _TEMPLATE_FIGURE_CACHE = buffer.getvalue()
    return _TEMPLATE_FIGURE_CACHE
