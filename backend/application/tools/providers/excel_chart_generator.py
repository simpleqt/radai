"""实验数据可视化生成器

基于 matplotlib + seaborn 生成科研级静态图表，
图表保存为 PNG 文件并上传到 MinIO，返回可访问的 URL。

支持图表类型：
  scatter / bar / line / box / violin / histogram / heatmap / bubble / radar
  auto — 根据数据特征自动推断
"""

from __future__ import annotations

import io
import os
import time
import uuid
import warnings
from typing import Any

import matplotlib
matplotlib.use("Agg")  # 无头模式，必须在 import pyplot 之前设置

import matplotlib.pyplot as plt
import matplotlib.font_manager as fm
import matplotlib.dates as mdates
import numpy as np
import pandas as pd
import seaborn as sns

from backend.config.loggers import get_logger

logger = get_logger(__name__)

# 禁止非关键警告
warnings.filterwarnings("ignore", category=UserWarning, module="matplotlib")

# ── 字体配置（支持中文） ──────────────────────────────────────────────
_CHINESE_FONT_CANDIDATES = [
    "WenQuanYi Micro Hei",
    "WenQuanYi Zen Hei",
    "Noto Sans CJK SC",
    "Noto Sans SC",
    "SimHei",
    "AR PL UMing CN",
    "DejaVu Sans",
]

_CJK_FONT_FILES = (
    "/usr/share/fonts/opentype/noto/NotoSansCJKsc-Regular.otf",
    "/usr/share/fonts/truetype/noto/NotoSansCJKsc-Regular.otf",
    "/usr/share/fonts/opentype/noto/NotoSansSC-Regular.otf",
    "/usr/share/fonts/opentype/noto/NotoSansCJK-Regular.ttc",
    "/usr/share/fonts/truetype/noto/NotoSansCJK-Regular.ttc",
)

def _get_chinese_font() -> str:
    for font_path in _CJK_FONT_FILES:
        if not os.path.isfile(font_path):
            continue
        try:
            fm.fontManager.addfont(font_path)
        except Exception:
            pass
    available = {f.name for f in fm.fontManager.ttflist}
    for candidate in _CHINESE_FONT_CANDIDATES + ["Noto Sans CJK JP"]:
        if candidate in available:
            return candidate
    return "DejaVu Sans"

_FONT_FAMILY = _get_chinese_font()
if _FONT_FAMILY == "Noto Sans CJK JP":
    for candidate in ("Noto Sans CJK SC", "Noto Sans SC"):
        if candidate in {f.name for f in fm.fontManager.ttflist}:
            _FONT_FAMILY = candidate
            break

# 科研图表全局样式
_SCIENCE_RC = {
    "font.family": _FONT_FAMILY,
    "font.size": 11,
    "axes.titlesize": 13,
    "axes.labelsize": 12,
    "xtick.labelsize": 10,
    "ytick.labelsize": 10,
    "legend.fontsize": 10,
    "figure.dpi": 150,
    "axes.linewidth": 1.2,
    "axes.spines.top": False,
    "axes.spines.right": False,
    "axes.grid": True,
    "grid.alpha": 0.35,
    "grid.linewidth": 0.7,
    "lines.linewidth": 1.8,
    "lines.markersize": 6,
    "figure.facecolor": "white",
    "axes.facecolor": "#f8f9fa",
    "savefig.bbox": "tight",
    "savefig.pad_inches": 0.15,
}

# seaborn 配色
_PALETTE = "husl"


# ── 图表类型自动推断 ──────────────────────────────────────────────────

def _coerce_time_axis(series: pd.Series) -> pd.Series | None:
    parsed = pd.to_datetime(series, errors="coerce")
    if parsed.notna().sum() < max(2, len(series) // 2):
        return None
    return parsed


def _apply_axis_layout(ax: plt.Axes, x_values: pd.Series | None = None) -> None:
    if x_values is not None:
        parsed = _coerce_time_axis(pd.Series(x_values))
        if parsed is not None:
            locator = mdates.AutoDateLocator(minticks=4, maxticks=8)
            formatter = mdates.ConciseDateFormatter(locator)
            ax.xaxis.set_major_locator(locator)
            ax.xaxis.set_major_formatter(formatter)

    labels = ax.get_xticklabels()
    if len(labels) > 6:
        plt.setp(labels, rotation=30, ha="right")
    elif any(len(label.get_text()) > 10 for label in labels):
        plt.setp(labels, rotation=20, ha="right")

    ax.margins(x=0.03)
    ax.tick_params(axis="x", pad=8)

def auto_detect_chart_type(
    df: pd.DataFrame,
    x_col: str | None,
    y_cols: list[str] | None,
) -> str:
    if df.empty or not y_cols:
        return "bar"
    x_is_num = x_col and pd.api.types.is_numeric_dtype(df[x_col]) if x_col else False
    y_is_num = all(pd.api.types.is_numeric_dtype(df[c]) for c in y_cols if c in df.columns)
    n_unique_x = df[x_col].nunique() if x_col and x_col in df.columns else 0

    if x_is_num and y_is_num:
        return "scatter"
    if not x_is_num and y_is_num and n_unique_x <= 30:
        return "bar"
    if y_is_num and len(y_cols) == 1:
        return "histogram"
    return "bar"


# ── 核心生成函数 ──────────────────────────────────────────────────────

def generate_chart(
    df: pd.DataFrame,
    chart_type: str = "auto",
    *,
    x_col: str | None = None,
    y_cols: list[str] | None = None,
    group_by: str | None = None,
    title: str | None = None,
    x_label: str | None = None,
    y_label: str | None = None,
    style: dict | None = None,
) -> bytes:
    """生成图表并返回 PNG bytes

    Args:
        df: 数据 DataFrame
        chart_type: 图表类型（auto/scatter/bar/line/box/violin/histogram/heatmap/bubble/radar）
        x_col: X 轴列名
        y_cols: Y 轴列名列表（支持多系列）
        group_by: 分组/着色列名
        title: 图表标题
        x_label: X 轴标签（默认用列名）
        y_label: Y 轴标签（默认用列名）
        style: 额外样式配置（覆盖默认值）
    """
    if df.empty:
        raise ValueError("数据为空，无法生成图表")

    y_cols = y_cols or []
    if chart_type == "auto":
        chart_type = auto_detect_chart_type(df, x_col, y_cols)

    rc = {**_SCIENCE_RC, **(style or {})}
    with plt.rc_context(rc):
        sns.set_theme(style="whitegrid", palette=_PALETTE, font=_FONT_FAMILY)

        _gen_func = {
            "scatter": _gen_scatter,
            "bar": _gen_bar,
            "line": _gen_line,
            "box": _gen_box,
            "violin": _gen_violin,
            "histogram": _gen_histogram,
            "heatmap": _gen_heatmap,
            "bubble": _gen_bubble,
            "radar": _gen_radar,
            "barh": _gen_barh,
        }.get(chart_type, _gen_bar)

        fig = _gen_func(
            df, x_col=x_col, y_cols=y_cols, group_by=group_by,
            title=title, x_label=x_label, y_label=y_label,
        )

        buf = io.BytesIO()
        fig.savefig(buf, format="png", dpi=rc.get("figure.dpi", 150))
        plt.close(fig)
        buf.seek(0)
        return buf.read()


# ── 各图表实现 ────────────────────────────────────────────────────────

def _prep_fig(title: str | None, figsize=(9, 5.5)) -> tuple[plt.Figure, plt.Axes]:
    fig, ax = plt.subplots(figsize=figsize)
    if title:
        ax.set_title(title, fontsize=13, fontweight="bold", pad=12)
    return fig, ax


def _gen_scatter(df, *, x_col, y_cols, group_by, title, x_label, y_label, **_) -> plt.Figure:
    y_col = y_cols[0] if y_cols else df.select_dtypes("number").columns[0]
    fig, ax = _prep_fig(title)

    if group_by and group_by in df.columns:
        groups = df[group_by].unique()
        palette = sns.color_palette(_PALETTE, len(groups))
        for i, grp in enumerate(groups):
            sub = df[df[group_by] == grp]
            ax.scatter(sub[x_col], sub[y_col], label=str(grp), color=palette[i], alpha=0.75, edgecolors="white", linewidths=0.5)

        # 整体趋势线
        try:
            from numpy.polynomial import polynomial as P
            xn = pd.to_numeric(df[x_col], errors="coerce")
            yn = pd.to_numeric(df[y_col], errors="coerce")
            mask = xn.notna() & yn.notna()
            if mask.sum() >= 3:
                coef = np.polyfit(xn[mask], yn[mask], 1)
                xr = np.linspace(xn[mask].min(), xn[mask].max(), 100)
                ax.plot(xr, np.polyval(coef, xr), "k--", lw=1.2, alpha=0.5, label="趋势线")
        except Exception:
            pass
        ax.legend(title=group_by, framealpha=0.8)
    else:
        xn = pd.to_numeric(df[x_col], errors="coerce") if x_col else None
        yn = pd.to_numeric(df[y_col], errors="coerce")
        if xn is not None:
            sns.regplot(x=xn, y=yn, ax=ax, scatter_kws={"alpha": 0.65, "edgecolors": "white"},
                        line_kws={"color": "#e74c3c", "lw": 1.5}, ci=95)
            mask = xn.notna() & yn.notna()
            if mask.sum() >= 2:
                corr = xn[mask].corr(yn[mask])
                ax.text(0.05, 0.95, f"r = {corr:.3f}", transform=ax.transAxes,
                        fontsize=10, va="top", bbox=dict(boxstyle="round,pad=0.3", facecolor="white", alpha=0.7))

    ax.set_xlabel(x_label or x_col or "", fontsize=11)
    ax.set_ylabel(y_label or y_col, fontsize=11)
    fig.tight_layout()
    return fig


def _gen_bar(df, *, x_col, y_cols, group_by, title, x_label, y_label, **_) -> plt.Figure:
    y_col = y_cols[0] if y_cols else df.select_dtypes("number").columns[0]
    width = max(10, min(16, len(df) * 0.55))
    fig, ax = _prep_fig(title, figsize=(width, 5.8))

    if group_by and group_by in df.columns:
        pivot = df.pivot_table(index=x_col, columns=group_by, values=y_col, aggfunc="mean")
        pivot.plot(kind="bar", ax=ax, colormap="tab10", edgecolor="white", linewidth=0.5)
        ax.legend(title=group_by, framealpha=0.8)
    else:
        agg = df.groupby(x_col)[y_col].agg(["mean", "std"]).reset_index() if x_col else None
        if agg is not None:
            colors = sns.color_palette(_PALETTE, len(agg))
            bars = ax.bar(agg[x_col].astype(str), agg["mean"], yerr=agg["std"],
                          color=colors, edgecolor="white", capsize=4, alpha=0.85)
            # 标注数值
            for bar, val in zip(bars, agg["mean"]):
                ax.text(bar.get_x() + bar.get_width() / 2, bar.get_height() * 1.01,
                        f"{val:.2f}", ha="center", va="bottom", fontsize=9)
        else:
            num_col = df.select_dtypes("number").columns[0]
            df[num_col].plot(kind="bar", ax=ax, color="#3498db", edgecolor="white")

    ax.set_xlabel(x_label or x_col or "", fontsize=11)
    ax.set_ylabel(y_label or y_col, fontsize=11)
    _apply_axis_layout(ax, df[x_col] if x_col and x_col in df.columns else None)
    fig.tight_layout()
    return fig


def _gen_barh(df, *, x_col, y_cols, group_by, title, x_label, y_label, **_) -> plt.Figure:
    y_col = y_cols[0] if y_cols else df.select_dtypes("number").columns[0]
    fig, ax = _prep_fig(title, figsize=(9, max(4, len(df) * 0.35)))
    colors = sns.color_palette(_PALETTE, len(df))
    xdata = pd.to_numeric(df[y_col], errors="coerce")
    ax.barh(df[x_col].astype(str), xdata, color=colors, edgecolor="white", alpha=0.85)
    for i, (val, label) in enumerate(zip(xdata, df[x_col])):
        ax.text(val * 1.005, i, f"{val:.2e}" if abs(val) > 1e4 else f"{val:.3f}",
                va="center", fontsize=9)
    ax.set_xlabel(y_label or y_col, fontsize=11)
    ax.set_ylabel(x_label or x_col or "", fontsize=11)
    ax.invert_yaxis()
    fig.tight_layout()
    return fig


def _gen_line(df, *, x_col, y_cols, group_by, title, x_label, y_label, **_) -> plt.Figure:
    width = max(10, min(16, len(df) * 0.6))
    fig, ax = _prep_fig(title, figsize=(width, 5.8))
    line_df = df.copy()
    x_data = None
    if x_col and x_col in line_df.columns:
        parsed = _coerce_time_axis(line_df[x_col])
        if parsed is not None:
            line_df = line_df.assign(**{x_col: parsed}).sort_values(x_col)
        x_data = line_df[x_col]
    palette = sns.color_palette(_PALETTE, max(len(y_cols), 1))
    for i, y_col in enumerate(y_cols or [df.select_dtypes("number").columns[0]]):
        if group_by and group_by in df.columns:
            for grp in line_df[group_by].unique():
                sub = line_df[line_df[group_by] == grp].sort_values(x_col)
                ax.plot(sub[x_col], sub[y_col], marker="o", markersize=4,
                        label=f"{y_col}({grp})")
        else:
            data = line_df.sort_values(x_col) if x_col else line_df
            ax.plot(data[x_col] if x_col else range(len(data)), data[y_col],
                    marker="o", markersize=4, color=palette[i], label=y_col)

    ax.set_xlabel(x_label or x_col or "索引", fontsize=11)
    ax.set_ylabel(y_label or (y_cols[0] if y_cols else ""), fontsize=11)
    if len(y_cols) > 1 or group_by:
        ax.legend(framealpha=0.8)
    _apply_axis_layout(ax, x_data)
    fig.tight_layout()
    return fig


def _gen_box(df, *, x_col, y_cols, group_by, title, x_label, y_label, **_) -> plt.Figure:
    y_col = y_cols[0] if y_cols else df.select_dtypes("number").columns[0]
    fig, ax = _prep_fig(title)
    if x_col and x_col in df.columns:
        sns.boxplot(data=df, x=x_col, y=y_col, hue=group_by,
                    palette=_PALETTE, ax=ax, linewidth=1.2, fliersize=4)
        sns.stripplot(data=df, x=x_col, y=y_col, hue=group_by,
                      palette=_PALETTE, ax=ax, size=3, alpha=0.5,
                      dodge=bool(group_by), legend=False)
    else:
        num_df = df[y_cols] if y_cols else df.select_dtypes("number")
        sns.boxplot(data=num_df, palette=_PALETTE, ax=ax, linewidth=1.2)
    ax.set_xlabel(x_label or x_col or "", fontsize=11)
    ax.set_ylabel(y_label or y_col, fontsize=11)
    _apply_axis_layout(ax, df[x_col] if x_col and x_col in df.columns else None)
    fig.tight_layout()
    return fig


def _gen_violin(df, *, x_col, y_cols, group_by, title, x_label, y_label, **_) -> plt.Figure:
    y_col = y_cols[0] if y_cols else df.select_dtypes("number").columns[0]
    fig, ax = _prep_fig(title)
    sns.violinplot(data=df, x=x_col, y=y_col, hue=group_by,
                   palette=_PALETTE, ax=ax, inner="box", linewidth=1.1)
    ax.set_xlabel(x_label or x_col or "", fontsize=11)
    ax.set_ylabel(y_label or y_col, fontsize=11)
    _apply_axis_layout(ax, df[x_col] if x_col and x_col in df.columns else None)
    fig.tight_layout()
    return fig


def _gen_histogram(df, *, x_col, y_cols, group_by, title, x_label, y_label, **_) -> plt.Figure:
    col = y_cols[0] if y_cols else (x_col or df.select_dtypes("number").columns[0])
    fig, ax = _prep_fig(title)
    data = pd.to_numeric(df[col], errors="coerce").dropna()
    if group_by and group_by in df.columns:
        for grp in df[group_by].unique():
            sub = pd.to_numeric(df[df[group_by] == grp][col], errors="coerce").dropna()
            sns.histplot(sub, ax=ax, label=str(grp), kde=True, alpha=0.55, bins=20)
        ax.legend(title=group_by)
    else:
        sns.histplot(data, ax=ax, kde=True, color="#3498db", bins=25, alpha=0.75)
        mean_val = data.mean()
        ax.axvline(mean_val, color="#e74c3c", ls="--", lw=1.5, label=f"均值={mean_val:.2f}")
        ax.legend()
    ax.set_xlabel(x_label or col, fontsize=11)
    ax.set_ylabel("频率", fontsize=11)
    fig.tight_layout()
    return fig


def _gen_heatmap(df, *, x_col, y_cols, group_by, title, x_label, y_label, **_) -> plt.Figure:
    # 如果 x_col/y_cols/group_by 均给定，构造透视热力图；否则计算相关性热力图
    num_cols = df.select_dtypes("number").columns.tolist()
    if x_col and y_cols and group_by and x_col in df.columns and group_by in df.columns:
        try:
            pivot = df.pivot_table(index=group_by, columns=x_col,
                                   values=y_cols[0], aggfunc="mean")
            fig, ax = _prep_fig(title, figsize=(max(8, len(pivot.columns) * 0.8 + 2),
                                                 max(5, len(pivot.index) * 0.6 + 1.5)))
            sns.heatmap(pivot, annot=True, fmt=".2f", cmap="YlOrRd",
                        linewidths=0.5, ax=ax, cbar_kws={"label": y_cols[0]})
            ax.set_xlabel(x_label or x_col, fontsize=11)
            ax.set_ylabel(y_label or group_by, fontsize=11)
            fig.tight_layout()
            return fig
        except Exception:
            pass
    # 相关性矩阵
    corr = df[num_cols].corr()
    fig, ax = _prep_fig(title or "相关性热力图",
                        figsize=(max(6, len(num_cols) * 0.9), max(5, len(num_cols) * 0.75)))
    mask = np.triu(np.ones_like(corr, dtype=bool))
    sns.heatmap(corr, mask=mask, annot=True, fmt=".2f", cmap="RdBu_r",
                center=0, linewidths=0.5, ax=ax, vmin=-1, vmax=1,
                cbar_kws={"label": "Pearson r"})
    ax.set_xticklabels(ax.get_xticklabels(), rotation=30, ha="right")
    fig.tight_layout()
    return fig


def _gen_bubble(df, *, x_col, y_cols, group_by, title, x_label, y_label, **_) -> plt.Figure:
    y_col = y_cols[0] if y_cols else df.select_dtypes("number").columns[0]
    size_col = y_cols[1] if len(y_cols) > 1 else y_col
    fig, ax = _prep_fig(title)
    xn = pd.to_numeric(df[x_col], errors="coerce") if x_col else None
    yn = pd.to_numeric(df[y_col], errors="coerce")
    sn = pd.to_numeric(df[size_col], errors="coerce")
    sn_norm = (sn - sn.min()) / (sn.max() - sn.min() + 1e-9) * 800 + 30

    if group_by and group_by in df.columns:
        groups = df[group_by].unique()
        palette = sns.color_palette(_PALETTE, len(groups))
        for i, grp in enumerate(groups):
            mask = df[group_by] == grp
            ax.scatter(xn[mask] if xn is not None else range(mask.sum()),
                       yn[mask], s=sn_norm[mask],
                       color=palette[i], alpha=0.65, label=str(grp), edgecolors="white")
        ax.legend(title=group_by)
    else:
        ax.scatter(xn if xn is not None else range(len(df)), yn, s=sn_norm,
                   c=sn, cmap="viridis", alpha=0.7, edgecolors="white")
    ax.set_xlabel(x_label or x_col or "", fontsize=11)
    ax.set_ylabel(y_label or y_col, fontsize=11)
    fig.tight_layout()
    return fig


def _gen_radar(df, *, x_col, y_cols, group_by, title, x_label, y_label, **_) -> plt.Figure:
    """雷达图：x_col 为分组列，y_cols 为各维度"""
    if not y_cols or not x_col:
        return _gen_bar(df, x_col=x_col, y_cols=y_cols, group_by=group_by,
                        title=title, x_label=x_label, y_label=y_label)
    num_df = df[y_cols].select_dtypes("number")
    labels = num_df.columns.tolist()
    if len(labels) < 3:
        return _gen_bar(df, x_col=x_col, y_cols=y_cols, group_by=group_by,
                        title=title, x_label=x_label, y_label=y_label)

    fig = plt.figure(figsize=(7, 7))
    ax = fig.add_subplot(111, polar=True)
    if title:
        ax.set_title(title, size=13, fontweight="bold", pad=20)

    angles = np.linspace(0, 2 * np.pi, len(labels), endpoint=False).tolist()
    angles += angles[:1]
    palette = sns.color_palette(_PALETTE, len(df))

    for i, (_, row) in enumerate(df.iterrows()):
        values = pd.to_numeric(row[labels], errors="coerce").fillna(0).tolist()
        # 归一化到 0-1
        vmax = max(values) or 1
        values = [v / vmax for v in values]
        values += values[:1]
        label = str(row[x_col]) if x_col in row.index else f"样本{i+1}"
        ax.plot(angles, values, "o-", lw=1.8, color=palette[i], label=label)
        ax.fill(angles, values, alpha=0.12, color=palette[i])

    ax.set_xticks(angles[:-1])
    ax.set_xticklabels(labels, size=10)
    ax.set_yticklabels([])
    ax.legend(loc="upper right", bbox_to_anchor=(1.3, 1.15), framealpha=0.8)
    fig.tight_layout()
    return fig


# ── 图表存储（通过 API 代理，不直接暴露 MinIO URL）────────────────────

async def save_chart(
    png_bytes: bytes,
    *,
    user_id: str | None = None,
    chart_id: str | None = None,
) -> str:
    """保存图表并返回 API 代理 URL（/api/v1/charts/{user_id}/{chart_id}）。

    通过 API Server 代理而非直接 MinIO URL 的原因：
    1. MinIO 端口（9004）通常不对外公开
    2. MinIO bucket 无公开访问策略
    3. 统一由 API Server（8082）服务，权限可控
    4. URL 中包含 user_id 实现用户归属
    """
    from backend.api.routers.chart_images import store_chart
    return await store_chart(png_bytes, user_id=user_id, chart_id=chart_id)


# 向后兼容旧调用（不传 user_id 时用 public 命名空间）
async def save_chart_to_minio(png_bytes: bytes, filename: str | None = None) -> str | None:
    """兼容旧接口：返回 API 代理 URL（而非 MinIO 直链）"""
    cid = (filename or "").replace(".png", "").replace("chart_", "") or uuid.uuid4().hex[:16]
    return await save_chart(png_bytes, user_id="public", chart_id=cid)


def save_chart_locally(png_bytes: bytes, filename: str | None = None) -> str:
    """将图表保存到本地 backend/data/charts/ 目录（MinIO 降级用），返回本地路径"""
    from pathlib import Path

    charts_dir = Path(__file__).resolve().parents[2] / "data" / "charts" / "public"
    charts_dir.mkdir(parents=True, exist_ok=True)
    fname = filename or f"chart_{int(time.time())}_{uuid.uuid4().hex[:8]}.png"
    dest = charts_dir / fname
    dest.write_bytes(png_bytes)
    return str(dest)
