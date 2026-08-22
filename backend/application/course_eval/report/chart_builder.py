"""图表生成（M-07 / T3-04）。

matplotlib 后端生成 PNG，同时返回绘图 JSON——前端用同一份 JSON 自绘 SVG，
保证「后端插入 docx 的图与前端预览同源同数」（§6.7）。
中文字体必须显式设置，否则出方块。
"""

from __future__ import annotations

import io
import json
from dataclasses import dataclass, replace
from decimal import Decimal

import matplotlib

import matplotlib.pyplot as plt

matplotlib.use("Agg")

# 显式设置中文字体（§6.7 硬规则）
plt.rcParams["font.sans-serif"] = [
    "Noto Sans CJK SC",
    "WenQuanYi Zen Hei",
    "SimHei",
    "Microsoft YaHei",
    "DejaVu Sans",
]
plt.rcParams["axes.unicode_minus"] = False

_ACCENT = "#7c3aed"  # 与 frontend_fmca 紫色主题一致
_BAND_COLORS = ["#f87171", "#fb923c", "#facc15", "#a3e635", "#4ade80", "#2dd4bf"]


@dataclass(frozen=True)
class ChartResult:
    """图表产物：PNG 字节 + 同源绘图 JSON。"""

    png_bytes: bytes
    data_json: str


def _save_fig(fig) -> ChartResult:
    # savefig 抛异常也必须 close，否则 figure 永久留在 pyplot 全局 Gcf 泄漏。
    try:
        buf = io.BytesIO()
        fig.savefig(buf, format="png", dpi=150, bbox_inches="tight")
        return ChartResult(png_bytes=buf.getvalue(), data_json="")
    finally:
        plt.close(fig)


def build_distribution_chart(
    band_labels: list[str], counts: list[int], pcts: list[Decimal]
) -> ChartResult:
    """成绩分布柱状图（6 段人数）。"""
    fig, ax = plt.subplots(figsize=(8, 4.5))
    bars = ax.bar(band_labels, counts, color=_BAND_COLORS[: len(counts)])
    for bar, pct in zip(bars, pcts):
        ax.text(
            bar.get_x() + bar.get_width() / 2,
            bar.get_height() + 0.3,
            f"{pct}%",
            ha="center",
            va="bottom",
            fontsize=9,
        )
    ax.set_ylabel("人数")
    ax.set_title("期末成绩分布")
    ax.grid(axis="y", alpha=0.3)
    result = _save_fig(fig)
    # 同源 JSON
    return replace(
        result,
        data_json=json.dumps(
            {
                "type": "bar",
                "labels": band_labels,
                "values": [int(c) for c in counts],
                "pcts": [str(p) for p in pcts],
            },
            ensure_ascii=False,
        ),
    )


def build_section_rate_chart(labels: list[str], rates: list[Decimal]) -> ChartResult:
    """大题得分率条形图。"""
    fig, ax = plt.subplots(figsize=(8, 4.5))
    ax.barh(labels, [float(r) for r in rates], color=_ACCENT)
    for i, rate in enumerate(rates):
        ax.text(float(rate) + 0.5, i, f"{rate}%", va="center", fontsize=9)
    ax.set_xlabel("得分率 (%)")
    ax.set_title("各大题得分率")
    ax.set_xlim(0, max(100, max(float(r) for r in rates) + 10))
    ax.grid(axis="x", alpha=0.3)
    result = _save_fig(fig)
    return replace(
        result,
        data_json=json.dumps(
            {"type": "barh", "labels": labels, "values": [str(r) for r in rates]},
            ensure_ascii=False,
        ),
    )


def build_goal_trend_chart(
    goals: list[str],
    current: list[Decimal],
    previous: list[Decimal] | None = None,
    *,
    y_min: float = 0.5,
    y_max: float = 0.9,
) -> ChartResult:
    """课程目标达成度历年对比柱状图（复刻附表 1 第 2 页：双系列、数据标签 3 位小数）。"""
    fig, ax = plt.subplots(figsize=(8, 4.5))
    x = range(len(goals))
    width = 0.35
    bars = ax.bar(
        [i - width / 2 for i in x],
        [float(v) for v in current],
        width,
        label="本期",
        color=_ACCENT,
    )
    for bar, v in zip(bars, current):
        ax.text(
            bar.get_x() + bar.get_width() / 2,
            bar.get_height() + 0.003,
            f"{v:.3f}",
            ha="center",
            fontsize=8,
        )
    if previous is not None:
        bars2 = ax.bar(
            [i + width / 2 for i in x],
            [float(v) for v in previous],
            width,
            label="往年",
            color="#94a3b8",
        )
        for bar, v in zip(bars2, previous):
            ax.text(
                bar.get_x() + bar.get_width() / 2,
                bar.get_height() + 0.003,
                f"{v:.3f}",
                ha="center",
                fontsize=8,
            )
        ax.legend()
    ax.set_xticks(list(x))
    ax.set_xticklabels(goals)
    ax.set_ylim(y_min, y_max)
    ax.set_ylabel("达成度")
    ax.set_title("课程目标达成度对比")
    ax.grid(axis="y", alpha=0.3)
    result = _save_fig(fig)
    return replace(
        result,
        data_json=json.dumps(
            {
                "type": "grouped_bar",
                "goals": goals,
                "current": [str(v) for v in current],
                "previous": [str(v) for v in previous] if previous else None,
            },
            ensure_ascii=False,
        ),
    )
