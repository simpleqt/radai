"""ECharts 交互式图表 payload 生成器

把 `visualize_excel_data` 的 DataFrame 翻译成 **纯 JSON** 的 ECharts option
（缩放/tooltip/图例等交互由前端 ne-full MarkdownRenderer 挂载实例后获得）。

约束：
- option 中禁止出现任何 JS 函数——归一化、分箱、趋势线、五数概括全部在
  Python 端算好；需要函数的行为（tooltip 格式化、下钻）由前端注入。
- 所有数值经 `_num` 清洗（NaN/Inf → None，numpy 标量 → Python float），
  保证 `json.dumps(..., allow_nan=False)` 可序列化。
- 与 `excel_chart_generator.generate_chart` 支持同一组 chart_type；violin
  没有 ECharts 原生形态，以 boxplot 呈现（meta.rendered_as 标注）。

本模块刻意不 import matplotlib/seaborn，保持导入轻量。
"""

from __future__ import annotations

import json
import math
from typing import Any

import numpy as np
import pandas as pd

from backend.config.loggers import get_logger

logger = get_logger(__name__)

PAYLOAD_VERSION = 1

# 序列化后的 payload 字符上限；超限先丢 drilldown
MAX_PAYLOAD_CHARS = 512_000

# drilldown 明细最大行数
MAX_DRILLDOWN_ROWS = 500

# 雷达图最多绘制的样本行数
MAX_RADAR_ROWS = 20

# 相关性热力图最多纳入的数值列数（与 summarize_excel_data 的约定一致）
MAX_CORR_COLUMNS = 20

_PALETTE = [
    "#5470c6", "#91cc75", "#fac858", "#ee6666", "#73c0de",
    "#3ba272", "#fc8452", "#9a60b4", "#ea7ccc",
]

_HEAT_WARM = ["#fff7ec", "#fee8c8", "#fdd49e", "#fdbb84", "#fc8d59", "#e34a33", "#b30000"]
_HEAT_DIVERGING = ["#2166ac", "#67a9cf", "#d1e5f0", "#f7f7f7", "#fddbc7", "#ef8a62", "#b2182b"]


# ── 基础清洗 ──────────────────────────────────────────────────────────

def _num(value: Any) -> float | int | None:
    """把任意标量清洗成 JSON 安全的数值（NaN/Inf → None）。"""
    if value is None:
        return None
    try:
        f = float(value)
    except (TypeError, ValueError):
        return None
    if math.isnan(f) or math.isinf(f):
        return None
    if f.is_integer() and abs(f) < 1e15:
        return int(f)
    # 6 位有效数字足够展示，且避免 0.30000000000000004 之类的噪声
    return float(f"{f:.6g}")


def _cell(value: Any) -> Any:
    """drilldown 单元格：数值走 _num，其余转字符串。"""
    if value is None:
        return None
    if isinstance(value, (int, float, np.integer, np.floating)):
        return _num(value)
    if isinstance(value, (pd.Timestamp, np.datetime64)):
        try:
            return pd.Timestamp(value).isoformat()
        except (ValueError, TypeError):
            return str(value)
    text = str(value)
    return text if text != "nan" else None


def _categories(series: pd.Series) -> list[str]:
    return [str(v) for v in series.tolist()]


def _coerce_time_axis(series: pd.Series) -> pd.Series | None:
    """与 excel_chart_generator 相同的时间轴判定（避免引入 matplotlib 依赖）。"""
    parsed = pd.to_datetime(series, errors="coerce")
    if parsed.notna().sum() < max(2, len(series) // 2):
        return None
    return parsed


def auto_detect_chart_type(
    df: pd.DataFrame,
    x_col: str | None,
    y_cols: list[str] | None,
) -> str:
    """与 excel_chart_generator.auto_detect_chart_type 保持一致的推断规则。"""
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


# ── 公共 option 骨架 ──────────────────────────────────────────────────

def _base_option(title: str | None, *, subtext: str | None = None) -> dict:
    option: dict = {
        "color": list(_PALETTE),
        "title": {
            "text": title or "",
            "left": "center",
            "textStyle": {"fontSize": 14, "fontWeight": "bold"},
        },
        "tooltip": {"trigger": "item", "confine": True},
        "toolbox": {
            "right": 12,
            "top": 4,
            "feature": {
                "saveAsImage": {"title": "保存图片"},
                "restore": {"title": "还原"},
                "dataView": {
                    "title": "数据视图",
                    "readOnly": True,
                    "lang": ["数据视图", "关闭", "刷新"],
                },
            },
        },
    }
    if subtext:
        option["title"]["subtext"] = subtext
    return option


def _cartesian(option: dict, *, zoom_axis: str = "x", with_slider: bool = False) -> dict:
    option["grid"] = {"left": 48, "right": 32, "top": 64, "bottom": 56, "containLabel": True}
    option["toolbox"]["feature"]["dataZoom"] = {
        "title": {"zoom": "区域缩放", "back": "还原缩放"},
    }
    inside: dict[str, Any] = {"type": "inside"}
    slider: dict[str, Any] = {"type": "slider", "height": 18, "bottom": 8}
    if zoom_axis == "y":
        inside["yAxisIndex"] = 0
        slider = {"type": "slider", "width": 18, "right": 8, "yAxisIndex": 0}
    zooms = [inside]
    if with_slider:
        zooms.append(slider)
        if zoom_axis == "x":
            option["grid"]["bottom"] = 72
    option["dataZoom"] = zooms
    return option


def _value_axis(name: str | None) -> dict:
    return {
        "type": "value",
        "name": name or "",
        "nameLocation": "middle",
        "nameGap": 32,
        "scale": True,
        "splitLine": {"lineStyle": {"opacity": 0.35}},
    }


def _category_axis(categories: list[str], name: str | None) -> dict:
    axis: dict = {
        "type": "category",
        "data": categories,
        "name": name or "",
        "nameLocation": "middle",
        "nameGap": 36,
        "axisTick": {"alignWithLabel": True},
    }
    if len(categories) > 6 or any(len(c) > 10 for c in categories):
        axis["axisLabel"] = {"rotate": 30, "hideOverlap": True}
    return axis


def _legend(option: dict) -> None:
    option["legend"] = {"top": 30, "type": "scroll"}
    grid = option.get("grid")
    if isinstance(grid, dict):
        grid["top"] = 84


# ── 各图型 builder ────────────────────────────────────────────────────

def _pick_y(df: pd.DataFrame, y_cols: list[str]) -> str:
    if y_cols:
        return y_cols[0]
    numeric = df.select_dtypes("number").columns
    if not len(numeric):
        raise ValueError("数据中没有数值列，无法生成图表")
    return str(numeric[0])


def _trend_line(x: pd.Series, y: pd.Series) -> tuple[list[list[float]], float | None]:
    xn = pd.to_numeric(x, errors="coerce")
    yn = pd.to_numeric(y, errors="coerce")
    mask = xn.notna() & yn.notna()
    if mask.sum() < 3:
        return [], None
    coef = np.polyfit(xn[mask], yn[mask], 1)
    xr = np.linspace(float(xn[mask].min()), float(xn[mask].max()), 50)
    points = [[_num(px), _num(py)] for px, py in zip(xr, np.polyval(coef, xr), strict=False)]
    corr = float(xn[mask].corr(yn[mask])) if mask.sum() >= 2 else None
    return points, (None if corr is None or math.isnan(corr) else corr)


def _build_scatter(df, *, x_col, y_cols, group_by, title, x_label, y_label) -> dict:
    y_col = _pick_y(df, y_cols)
    if not x_col:
        raise ValueError("散点图需要 x_column")
    subtext = None
    series: list[dict] = []

    if group_by and group_by in df.columns:
        for grp in df[group_by].astype(str).unique().tolist():
            sub = df[df[group_by].astype(str) == grp]
            data = [
                [_num(a), _num(b)]
                for a, b in zip(sub[x_col], sub[y_col], strict=False)
                if _num(a) is not None and _num(b) is not None
            ]
            series.append({
                "name": grp,
                "type": "scatter",
                "data": data,
                "symbolSize": 9,
                "itemStyle": {"opacity": 0.78},
            })
    else:
        data = [
            [_num(a), _num(b)]
            for a, b in zip(df[x_col], df[y_col], strict=False)
            if _num(a) is not None and _num(b) is not None
        ]
        series.append({
            "name": y_col,
            "type": "scatter",
            "data": data,
            "symbolSize": 9,
            "itemStyle": {"opacity": 0.78},
        })

    trend, corr = _trend_line(df[x_col], df[y_col])
    if trend:
        series.append({
            "name": "趋势线",
            "type": "line",
            "data": trend,
            "showSymbol": False,
            "lineStyle": {"type": "dashed", "width": 1.6, "color": "#e74c3c"},
            "tooltip": {"show": False},
            "z": 3,
        })
    if corr is not None:
        subtext = f"Pearson r = {corr:.3f}"

    option = _cartesian(_base_option(title, subtext=subtext))
    option["xAxis"] = _value_axis(x_label or x_col)
    option["yAxis"] = _value_axis(y_label or y_col)
    option["series"] = series
    if len(series) > 1:
        _legend(option)
    return option


def _build_bar(df, *, x_col, y_cols, group_by, title, x_label, y_label) -> tuple[dict, pd.DataFrame | None]:
    """返回 (option, 聚合前明细 df 或 None)；明细用于 drilldown。"""
    y_col = _pick_y(df, y_cols)
    detail: pd.DataFrame | None = None
    series: list[dict[str, Any]]

    option = _base_option(title)
    option["tooltip"] = {"trigger": "axis", "confine": True, "axisPointer": {"type": "shadow"}}

    if group_by and group_by in df.columns and x_col:
        pivot = df.pivot_table(index=x_col, columns=group_by, values=y_col, aggfunc="mean")
        categories = [str(v) for v in pivot.index.tolist()]
        series = [
            {
                "name": str(col),
                "type": "bar",
                "data": [_num(v) for v in pivot[col].tolist()],
                "emphasis": {"focus": "series"},
            }
            for col in pivot.columns
        ]
        detail = df[[c for c in (x_col, group_by, y_col) if c in df.columns]]
    elif x_col:
        agg = df.groupby(x_col)[y_col].agg(["mean", "std"]).reset_index()
        categories = [str(v) for v in agg[x_col].tolist()]
        data = [
            {"value": _num(m), "std": _num(s)}
            for m, s in zip(agg["mean"], agg["std"], strict=False)
        ]
        series = [{
            "name": y_col,
            "type": "bar",
            "data": data,
            "label": {"show": len(agg) <= 20, "position": "top", "fontSize": 10},
        }]
        detail = df[[c for c in (x_col, y_col) if c in df.columns]]
    else:
        num_col = _pick_y(df, y_cols)
        categories = [str(i) for i in range(len(df))]
        series = [{
            "name": num_col,
            "type": "bar",
            "data": [_num(v) for v in df[num_col].tolist()],
        }]

    _cartesian(option, with_slider=len(categories) > 12)
    option["xAxis"] = _category_axis(categories, x_label or x_col)
    option["yAxis"] = _value_axis(y_label or y_col)
    option["series"] = series
    if len(series) > 1:
        _legend(option)
    return option, detail


def _build_barh(df, *, x_col, y_cols, group_by, title, x_label, y_label) -> tuple[dict, pd.DataFrame | None]:
    y_col = _pick_y(df, y_cols)
    if not x_col:
        raise ValueError("水平柱状图需要 x_column")
    categories = [str(v) for v in df[x_col].tolist()]
    values = [_num(v) for v in pd.to_numeric(df[y_col], errors="coerce").tolist()]

    option = _base_option(title)
    option["tooltip"] = {"trigger": "axis", "confine": True, "axisPointer": {"type": "shadow"}}
    _cartesian(option, zoom_axis="y", with_slider=len(categories) > 20)
    # 与 matplotlib invert_yaxis 一致：第一行显示在最上方
    option["yAxis"] = {
        "type": "category",
        "data": categories[::-1],
        "name": x_label or x_col,
        "axisLabel": {"hideOverlap": True},
    }
    option["xAxis"] = _value_axis(y_label or y_col)
    option["series"] = [{
        "name": y_col,
        "type": "bar",
        "data": values[::-1],
        "label": {"show": len(categories) <= 30, "position": "right", "fontSize": 10},
    }]
    detail = df[[c for c in (x_col, y_col) if c in df.columns]]
    return option, detail


def _build_line(df, *, x_col, y_cols, group_by, title, x_label, y_label) -> dict:
    ys = y_cols or [_pick_y(df, y_cols)]
    line_df = df.copy()
    time_axis = False
    if x_col and x_col in line_df.columns:
        parsed = _coerce_time_axis(line_df[x_col])
        if parsed is not None:
            line_df = line_df.assign(**{x_col: parsed}).sort_values(x_col)
            time_axis = True

    option = _base_option(title)
    option["tooltip"] = {"trigger": "axis", "confine": True}
    _cartesian(option, with_slider=len(line_df) > 30)

    series: list[dict] = []

    def _line_series(name: str, frame: pd.DataFrame, y: str) -> dict:
        if time_axis:
            data = [
                [pd.Timestamp(t).isoformat(), _num(v)]
                for t, v in zip(frame[x_col], frame[y], strict=False)
                if pd.notna(t)
            ]
        elif x_col and x_col in frame.columns:
            # [类目, 值] 成对给出：分组子集与全量类目按名称对齐，而非按下标
            data = [
                [str(x), _num(v)]
                for x, v in zip(frame[x_col], frame[y], strict=False)
            ]
        else:
            data = [_num(v) for v in frame[y].tolist()]
        return {
            "name": name,
            "type": "line",
            "data": data,
            "symbol": "circle",
            "symbolSize": 6,
            "connectNulls": True,
        }

    if group_by and group_by in line_df.columns:
        for grp in line_df[group_by].astype(str).unique().tolist():
            sub = line_df[line_df[group_by].astype(str) == grp]
            sub = sub.sort_values(x_col) if x_col and x_col in sub.columns else sub
            for y in ys:
                series.append(_line_series(f"{y}({grp})", sub, y))
    else:
        data_df = line_df.sort_values(x_col) if x_col and x_col in line_df.columns else line_df
        for y in ys:
            series.append(_line_series(y, data_df, y))

    if time_axis:
        option["xAxis"] = {"type": "time", "name": x_label or x_col or ""}
    else:
        if x_col and x_col in line_df.columns:
            # 去重保序：分组数据的 x 值会重复出现
            categories = list(dict.fromkeys(_categories(line_df[x_col])))
        else:
            categories = [str(i) for i in range(len(line_df))]
        option["xAxis"] = _category_axis(categories, x_label or x_col or "索引")
        option["xAxis"]["boundaryGap"] = False
    option["yAxis"] = _value_axis(y_label or (ys[0] if ys else ""))
    option["series"] = series
    if len(series) > 1:
        _legend(option)
    return option


def _five_number(values: pd.Series) -> tuple[list[float | int | None], list[float]]:
    """返回 ([low, Q1, median, Q3, high], outliers)，whisker 采用 1.5×IQR。"""
    data = pd.to_numeric(values, errors="coerce").dropna()
    if data.empty:
        return [None, None, None, None, None], []
    q1, q2, q3 = (float(data.quantile(q)) for q in (0.25, 0.5, 0.75))
    iqr = q3 - q1
    lo_bound, hi_bound = q1 - 1.5 * iqr, q3 + 1.5 * iqr
    inliers = data[(data >= lo_bound) & (data <= hi_bound)]
    low = float(inliers.min()) if not inliers.empty else float(data.min())
    high = float(inliers.max()) if not inliers.empty else float(data.max())
    outliers = [float(v) for v in data[(data < lo_bound) | (data > hi_bound)].tolist()]
    return (
        [_num(low), _num(q1), _num(q2), _num(q3), _num(high)],
        [v for v in (_num(o) for o in outliers) if v is not None],
    )


def _build_box(df, *, x_col, y_cols, group_by, title, x_label, y_label) -> dict:
    y_col = _pick_y(df, y_cols)
    option = _base_option(title)

    if x_col and x_col in df.columns:
        categories = [str(v) for v in df[x_col].astype(str).unique().tolist()]
        boxes, outlier_points = [], []
        for idx, cat in enumerate(categories):
            summary, outliers = _five_number(df[df[x_col].astype(str) == cat][y_col])
            boxes.append(summary)
            outlier_points.extend([[idx, v] for v in outliers])
        axis_name = x_label or x_col
        value_name = y_label or y_col
    else:
        cols = [c for c in (y_cols or df.select_dtypes("number").columns.tolist()) if c in df.columns]
        categories = [str(c) for c in cols]
        boxes, outlier_points = [], []
        for idx, col in enumerate(cols):
            summary, outliers = _five_number(df[col])
            boxes.append(summary)
            outlier_points.extend([[idx, v] for v in outliers])
        axis_name = x_label or ""
        value_name = y_label or ""

    _cartesian(option, with_slider=len(categories) > 12)
    option["xAxis"] = _category_axis(categories, axis_name)
    option["yAxis"] = _value_axis(value_name)
    option["series"] = [
        {
            "name": y_col,
            "type": "boxplot",
            "data": boxes,
            "itemStyle": {"borderWidth": 1.4},
        },
    ]
    if outlier_points:
        option["series"].append({
            "name": "离群点",
            "type": "scatter",
            "data": outlier_points,
            "symbolSize": 6,
            "itemStyle": {"opacity": 0.6},
        })
    return option


def _build_histogram(df, *, x_col, y_cols, group_by, title, x_label, y_label) -> dict:
    col = y_cols[0] if y_cols else (x_col or _pick_y(df, y_cols))
    data_all = pd.to_numeric(df[col], errors="coerce").dropna()
    if data_all.empty:
        raise ValueError(f"列 {col} 没有可用数值，无法生成直方图")

    option = _base_option(title, subtext=f"均值 = {float(data_all.mean()):.4g}")
    option["tooltip"] = {"trigger": "axis", "confine": True, "axisPointer": {"type": "shadow"}}

    if group_by and group_by in df.columns:
        bins = np.histogram_bin_edges(data_all, bins=20)
        labels = [f"{bins[i]:.4g}~{bins[i + 1]:.4g}" for i in range(len(bins) - 1)]
        series = []
        for grp in df[group_by].astype(str).unique().tolist():
            sub = pd.to_numeric(df[df[group_by].astype(str) == grp][col], errors="coerce").dropna()
            counts, _ = np.histogram(sub, bins=bins)
            series.append({
                "name": grp,
                "type": "bar",
                "data": [int(c) for c in counts],
                "barGap": "10%",
            })
    else:
        counts, bins = np.histogram(data_all, bins=25)
        labels = [f"{bins[i]:.4g}~{bins[i + 1]:.4g}" for i in range(len(bins) - 1)]
        series = [{
            "name": "频数",
            "type": "bar",
            "data": [int(c) for c in counts],
            "barCategoryGap": "0%",
            "itemStyle": {"opacity": 0.85, "borderColor": "#fff", "borderWidth": 0.5},
        }]

    _cartesian(option, with_slider=len(labels) > 30)
    option["xAxis"] = _category_axis(labels, x_label or col)
    option["yAxis"] = _value_axis("频数")
    option["series"] = series
    if len(series) > 1:
        _legend(option)
    return option


def _heatmap_common(option: dict, x_cats: list[str], y_cats: list[str]) -> None:
    option["grid"] = {"left": 48, "right": 96, "top": 64, "bottom": 72, "containLabel": True}
    option["xAxis"] = {
        "type": "category",
        "data": x_cats,
        "axisLabel": {"rotate": 30 if any(len(c) > 6 for c in x_cats) else 0, "hideOverlap": True},
        "splitArea": {"show": True},
    }
    option["yAxis"] = {"type": "category", "data": y_cats, "splitArea": {"show": True}}


def _build_heatmap(df, *, x_col, y_cols, group_by, title, x_label, y_label) -> dict:
    if x_col and y_cols and group_by and x_col in df.columns and group_by in df.columns:
        try:
            pivot = df.pivot_table(index=group_by, columns=x_col, values=y_cols[0], aggfunc="mean")
            x_cats = [str(v) for v in pivot.columns.tolist()]
            y_cats = [str(v) for v in pivot.index.tolist()]
            data = []
            vals = []
            for yi, row in enumerate(pivot.values):
                for xi, v in enumerate(row):
                    n = _num(v)
                    data.append([xi, yi, n])
                    if n is not None:
                        vals.append(n)
            option = _base_option(title)
            _heatmap_common(option, x_cats, y_cats)
            option["visualMap"] = {
                "min": min(vals) if vals else 0,
                "max": max(vals) if vals else 1,
                "calculable": True,
                "orient": "vertical",
                "right": 8,
                "top": "center",
                "inRange": {"color": list(_HEAT_WARM)},
                "text": [y_cols[0], ""],
            }
            option["series"] = [{
                "name": y_cols[0],
                "type": "heatmap",
                "data": data,
                "label": {"show": len(x_cats) * len(y_cats) <= 200, "fontSize": 9},
            }]
            return option
        except Exception:
            logger.warning("热力图透视计算失败，回退为相关性矩阵", exc_info=True)

    num_cols = df.select_dtypes("number").columns.tolist()[:MAX_CORR_COLUMNS]
    if len(num_cols) < 2:
        raise ValueError("数值列不足 2 个，无法生成相关性热力图")
    corr = df[num_cols].corr()
    cats = [str(c) for c in num_cols]
    data = []
    for yi in range(len(cats)):
        for xi in range(len(cats)):
            v = _num(round(float(corr.iloc[yi, xi]), 2)) if pd.notna(corr.iloc[yi, xi]) else None
            data.append([xi, yi, v])
    option = _base_option(title or "相关性热力图")
    _heatmap_common(option, cats, cats)
    option["visualMap"] = {
        "min": -1,
        "max": 1,
        "calculable": True,
        "orient": "vertical",
        "right": 8,
        "top": "center",
        "inRange": {"color": list(_HEAT_DIVERGING)},
        "text": ["Pearson r", ""],
    }
    option["series"] = [{
        "name": "Pearson r",
        "type": "heatmap",
        "data": data,
        "label": {"show": len(cats) <= 14, "fontSize": 9},
    }]
    return option


def _build_bubble(df, *, x_col, y_cols, group_by, title, x_label, y_label) -> dict:
    y_col = _pick_y(df, y_cols)
    size_col = y_cols[1] if y_cols and len(y_cols) > 1 else y_col
    if not x_col:
        raise ValueError("气泡图需要 x_column")

    xn = pd.to_numeric(df[x_col], errors="coerce")
    yn = pd.to_numeric(df[y_col], errors="coerce")
    sn = pd.to_numeric(df[size_col], errors="coerce")
    smin, smax = float(sn.min()), float(sn.max())
    span = (smax - smin) or 1.0

    def _items(mask=None) -> list[dict]:
        idx = df.index if mask is None else df.index[mask.to_numpy()]
        items = []
        for i in idx:
            x, y, s = _num(xn.get(i)), _num(yn.get(i)), _num(sn.get(i))
            if x is None or y is None:
                continue
            px = 10 + ((float(s) - smin) / span) * 42 if s is not None else 10
            items.append({"value": [x, y, s], "symbolSize": _num(px)})
        return items

    option = _base_option(title)
    series: list[dict] = []
    if group_by and group_by in df.columns:
        grouped = df[group_by].astype(str)
        for grp in grouped.unique().tolist():
            series.append({
                "name": grp,
                "type": "scatter",
                "data": _items(grouped == grp),
                "itemStyle": {"opacity": 0.68},
            })
    else:
        series.append({
            "name": y_col,
            "type": "scatter",
            "data": _items(),
            "itemStyle": {"opacity": 0.68},
        })
        vals = [v for v in (_num(v) for v in sn.tolist()) if v is not None]
        if vals:
            option["visualMap"] = {
                "dimension": 2,
                "min": min(vals),
                "max": max(vals),
                "orient": "vertical",
                "right": 8,
                "top": "center",
                "calculable": True,
                "inRange": {"color": ["#440154", "#31688e", "#35b779", "#fde725"]},
                "text": [size_col, ""],
            }

    _cartesian(option)
    option["grid"]["right"] = 96 if "visualMap" in option else 32
    option["xAxis"] = _value_axis(x_label or x_col)
    option["yAxis"] = _value_axis(y_label or y_col)
    option["series"] = series
    if len(series) > 1:
        _legend(option)
    return option


def _build_radar(df, *, x_col, y_cols, group_by, title, x_label, y_label) -> dict:
    if not y_cols or not x_col:
        raise ValueError("雷达图需要 x_column（分组列）与 y_columns（各维度）")
    dims = [c for c in y_cols if c in df.columns and pd.api.types.is_numeric_dtype(df[c])]
    if len(dims) < 3:
        raise ValueError("雷达图至少需要 3 个数值维度")

    frame = df.head(MAX_RADAR_ROWS)
    indicators = []
    for c in dims:
        col_max = _num(pd.to_numeric(df[c], errors="coerce").max())
        indicators.append({"name": str(c), "max": col_max if col_max not in (None, 0) else 1})

    data = []
    for i, (_, row) in enumerate(frame.iterrows()):
        values = [_num(row[c]) or 0 for c in dims]
        name = str(row[x_col]) if x_col in row.index else f"样本{i + 1}"
        data.append({"value": values, "name": name, "areaStyle": {"opacity": 0.12}})

    option = _base_option(title)
    option["legend"] = {"top": 30, "type": "scroll"}
    option["radar"] = {
        "indicator": indicators,
        "center": ["50%", "58%"],
        "radius": "62%",
        "axisName": {"fontSize": 11},
    }
    option["series"] = [{
        "name": title or "雷达图",
        "type": "radar",
        "data": data,
        "symbolSize": 4,
    }]
    return option


# ── payload 组装 ──────────────────────────────────────────────────────

def _drilldown_block(
    detail: pd.DataFrame | None,
    *,
    x_col: str | None,
    y_col: str | None,
    group_by: str | None,
) -> dict | None:
    if detail is None or detail.empty or not x_col or not y_col:
        return None
    if x_col not in detail.columns or y_col not in detail.columns:
        return None
    columns = [c for c in (x_col, group_by, y_col) if c and c in detail.columns]
    rows = [
        [_cell(row[c]) for c in columns]
        for _, row in detail.head(MAX_DRILLDOWN_ROWS).iterrows()
    ]
    return {
        "x_col": x_col,
        "y_col": y_col,
        "group_by": group_by if group_by in columns else None,
        "columns": columns,
        "rows": rows,
    }


def build_echarts_payload(
    df: pd.DataFrame,
    chart_type: str = "auto",
    *,
    x_col: str | None = None,
    y_cols: list[str] | None = None,
    group_by: str | None = None,
    title: str | None = None,
    x_label: str | None = None,
    y_label: str | None = None,
) -> dict:
    """生成交互式图表 payload：{version, chart_type, option, meta, drilldown?}。

    抛出的异常由调用方兜底（工具端捕获后仅存 PNG）。
    """
    if df.empty:
        raise ValueError("数据为空，无法生成图表")

    y_cols = [c for c in (y_cols or []) if c in df.columns]
    if x_col and x_col not in df.columns:
        x_col = None
    if group_by and group_by not in df.columns:
        group_by = None
    if chart_type == "auto":
        chart_type = auto_detect_chart_type(df, x_col, y_cols)

    kwargs = {
        "x_col": x_col, "y_cols": y_cols, "group_by": group_by,
        "title": title, "x_label": x_label, "y_label": y_label,
    }

    rendered_as = chart_type
    detail: pd.DataFrame | None = None
    height = 380

    if chart_type == "scatter":
        option = _build_scatter(df, **kwargs)
    elif chart_type == "bar":
        option, detail = _build_bar(df, **kwargs)
    elif chart_type == "barh":
        option, detail = _build_barh(df, **kwargs)
        height = max(320, min(900, 26 * len(df) + 140))
    elif chart_type == "line":
        option = _build_line(df, **kwargs)
    elif chart_type in ("box", "violin"):
        option = _build_box(df, **kwargs)
        rendered_as = "boxplot"
    elif chart_type == "histogram":
        option = _build_histogram(df, **kwargs)
    elif chart_type == "heatmap":
        option = _build_heatmap(df, **kwargs)
        rows = len(option.get("yAxis", {}).get("data", []) or [])
        height = max(320, min(900, 26 * rows + 160))
    elif chart_type == "bubble":
        option = _build_bubble(df, **kwargs)
    elif chart_type == "radar":
        option = _build_radar(df, **kwargs)
        height = 440
    else:
        option, detail = _build_bar(df, **kwargs)
        rendered_as = "bar"

    payload: dict = {
        "version": PAYLOAD_VERSION,
        "chart_type": chart_type,
        "option": option,
        "meta": {
            "x_col": x_col,
            "y_cols": y_cols,
            "group_by": group_by,
            "rows": int(len(df)),
            "height": int(height),
        },
    }
    if rendered_as != chart_type:
        payload["meta"]["rendered_as"] = rendered_as

    drilldown = _drilldown_block(
        detail,
        x_col=x_col,
        y_col=(y_cols[0] if y_cols else _pick_y(df, y_cols)),
        group_by=group_by,
    )
    if drilldown:
        payload["drilldown"] = drilldown

    # 大小上限：先丢 drilldown，仍超限则报错走 PNG 兜底
    serialized = json.dumps(payload, ensure_ascii=False, allow_nan=False)
    if len(serialized) > MAX_PAYLOAD_CHARS:
        payload.pop("drilldown", None)
        serialized = json.dumps(payload, ensure_ascii=False, allow_nan=False)
        if len(serialized) > MAX_PAYLOAD_CHARS:
            raise ValueError(
                f"图表交互数据过大（{len(serialized)} 字符），超出 {MAX_PAYLOAD_CHARS} 上限"
            )
        logger.info("[EChartsGen] payload 超限，已移除 drilldown 明细")

    return payload
