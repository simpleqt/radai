"""Generate fundamental-mechanics diagrams (free body / shear / moment).

This tool is the fallback for problems where the knowledge base or the
question itself does not already provide a usable figure.  It draws a beam
schematic with supports and loads, plus shear-force / bending-moment diagrams
for statically determinate simply-supported or cantilever beams.
"""

from __future__ import annotations

import asyncio
import json
import io
import math
import re
import uuid
from typing import Annotated, Any

import matplotlib

matplotlib.use("Agg")  # headless backend must be selected before pyplot import

import matplotlib.font_manager as fm
import matplotlib.pyplot as plt
import numpy as np
from matplotlib.patches import Circle, Polygon
from langchain.tools import InjectedToolCallId, ToolRuntime, tool
from langchain_core.messages import ToolMessage
from langgraph.types import Command
from langgraph.typing import ContextT

from backend.application.agent_executors.image_url_registry import register_image_url
from backend.application.tools.runtime_resolution import (
    resolve_runtime_thread_id as _resolve_runtime_thread_id,
)
from backend.config.loggers import get_logger
from backend.core.agents.state import ThreadState
from backend.core.thread_key import parse_internal_thread_key
from backend.services.chart_image_store import store_chart

logger = get_logger(__name__)

_DIAGRAM_TYPES = {
    "free_body": "free_body",
    "freebody": "free_body",
    "受力图": "free_body",
    "beam": "beam",
    "梁示意图": "beam",
    "梁": "beam",
    "shear": "shear",
    "剪力图": "shear",
    "剪力": "shear",
    "moment": "moment",
    "弯矩图": "moment",
    "弯矩": "moment",
    "combined": "combined",
    "内力图": "combined",
}

_CHINESE_FONT_CANDIDATES = [
    "WenQuanYi Micro Hei",
    "WenQuanYi Zen Hei",
    "Noto Sans CJK SC",
    "Noto Sans SC",
    "SimHei",
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
        if not __import__("os").path.isfile(font_path):
            continue
        try:
            fm.fontManager.addfont(font_path)
        except Exception:
            logger.debug("力学图字体加载失败: path=%s", font_path, exc_info=True)
    available = {font.name for font in fm.fontManager.ttflist}
    for candidate in _CHINESE_FONT_CANDIDATES:
        if candidate in available:
            return candidate
    return "DejaVu Sans"


_FONT_FAMILY = _get_chinese_font()
plt.rcParams["font.family"] = _FONT_FAMILY


def _normalize_diagram_type(value: str) -> str:
    key = str(value or "").strip().lower()
    if key in _DIAGRAM_TYPES:
        return _DIAGRAM_TYPES[key]
    raise ValueError(
        "未知 diagram_type，可选：free_body / beam / shear / moment / combined"
    )


_NUMBER_RE = re.compile(r"-?\d+(?:\.\d+)?")


def _as_float(value: Any, field: str) -> float:
    """Coerce a numeric field, tolerating units like ``2m`` / ``10 kN`` / ``5kN/m``."""

    if isinstance(value, bool):
        raise ValueError(f"字段 {field} 必须是数值")
    try:
        return float(value)
    except (TypeError, ValueError):
        pass
    raw = str(value or "").strip().replace("，", ".").replace("．", ".")
    if not raw:
        raise ValueError(f"字段 {field} 必须是数值")
    match = _NUMBER_RE.search(raw)
    if not match:
        raise ValueError(
            f"字段 {field} 必须是数值（当前值：{value!r}；请直接写数字，长度用 m、力用 kN、均布荷载用 kN/m）"
        )
    return float(match.group())


def _parse_spec(spec: str) -> dict[str, Any]:
    raw = str(spec or "").strip()
    if not raw:
        raise ValueError("spec 不能为空")
    try:
        parsed = json.loads(raw)
    except json.JSONDecodeError as exc:
        raise ValueError("spec 必须是合法 JSON") from exc
    if not isinstance(parsed, dict):
        raise ValueError("spec 必须是 JSON 对象")

    length = _as_float(parsed.get("length", 1.0), "length")
    if length <= 0:
        raise ValueError("length 必须大于 0")

    supports = []
    for index, item in enumerate(parsed.get("supports") or []):
        if not isinstance(item, dict):
            raise ValueError(f"supports[{index}] 必须是对象")
        support_type = str(item.get("type") or "roller").strip().lower()
        if support_type not in {"pinned", "roller", "fixed"}:
            raise ValueError(f"supports[{index}].type 只支持 pinned/roller/fixed")
        supports.append(
            {
                "x": _as_float(item.get("x"), f"supports[{index}].x"),
                "type": support_type,
                "label": str(item.get("label") or "").strip(),
            }
        )
    if not supports:
        raise ValueError("spec 至少需要一个支座")

    loads = []
    for index, item in enumerate(parsed.get("loads") or []):
        if not isinstance(item, dict):
            raise ValueError(f"loads[{index}] 必须是对象")
        loads.append(
            {
                "x": _as_float(item.get("x"), f"loads[{index}].x"),
                "value": _as_float(item.get("value"), f"loads[{index}].value"),
                "label": str(item.get("label") or "").strip(),
            }
        )

    uniforms = []
    for index, item in enumerate(parsed.get("uniforms") or []):
        if not isinstance(item, dict):
            raise ValueError(f"uniforms[{index}] 必须是对象")
        start = _as_float(item.get("start"), f"uniforms[{index}].start")
        end = _as_float(item.get("end"), f"uniforms[{index}].end")
        if end <= start:
            raise ValueError(f"uniforms[{index}].end 必须大于 start")
        uniforms.append(
            {
                "start": start,
                "end": end,
                "value": _as_float(item.get("value"), f"uniforms[{index}].value"),
                "label": str(item.get("label") or "").strip(),
            }
        )

    moments = []
    for index, item in enumerate(parsed.get("moments") or []):
        if not isinstance(item, dict):
            raise ValueError(f"moments[{index}] 必须是对象")
        moments.append(
            {
                "x": _as_float(item.get("x"), f"moments[{index}].x"),
                "value": _as_float(item.get("value"), f"moments[{index}].value"),
                "label": str(item.get("label") or "").strip(),
            }
        )

    reactions = []
    for index, item in enumerate(parsed.get("reactions") or []):
        if not isinstance(item, dict):
            raise ValueError(f"reactions[{index}] 必须是对象")
        reactions.append(
            {
                "x": _as_float(item.get("x"), f"reactions[{index}].x"),
                "value": _as_float(item.get("value"), f"reactions[{index}].value"),
                "label": str(item.get("label") or f"R{index+1}").strip(),
            }
        )

    return {
        "length": length,
        "supports": supports,
        "loads": loads,
        "uniforms": uniforms,
        "moments": moments,
        "reactions": reactions,
    }


def _compute_reactions(spec: dict[str, Any]) -> list[dict[str, Any]]:
    """Return vertical reactions (and fixed-end moment for cantilevers)."""

    if spec["reactions"]:
        return list(spec["reactions"])

    supports = sorted(spec["supports"], key=lambda item: item["x"])
    total_force = sum(item["value"] for item in spec["loads"]) + sum(
        item["value"] * (item["end"] - item["start"]) for item in spec["uniforms"]
    )
    moment_about_zero = sum(item["value"] * item["x"] for item in spec["loads"])
    moment_about_zero += sum(
        item["value"] * (item["start"] + item["end"]) / 2.0 * (item["end"] - item["start"])
        for item in spec["uniforms"]
    )
    moment_about_zero += sum(item["value"] for item in spec["moments"])

    if len(supports) == 1 and supports[0]["type"] == "fixed":
        x0 = supports[0]["x"]
        reaction = total_force
        reaction_moment = -(moment_about_zero - total_force * x0)
        return [
            {
                "x": x0,
                "value": reaction,
                "label": supports[0]["label"] or "R",
                "moment": reaction_moment,
            }
        ]

    if len(supports) == 2:
        x_left, x_right = supports[0]["x"], supports[1]["x"]
        span = x_right - x_left
        if span <= 0:
            raise ValueError("两个支座位置不能重合")
        right_force = (moment_about_zero - total_force * x_left) / span
        left_force = total_force - right_force
        return [
            {"x": x_left, "value": left_force, "label": supports[0]["label"] or "R_A"},
            {"x": x_right, "value": right_force, "label": supports[1]["label"] or "R_B"},
        ]

    raise ValueError(
        "仅支持简支/两支座静定梁或固定端悬臂梁；多支座需在 spec.reactions 中给定反力"
    )


def _shear_moment_curves(
    spec: dict[str, Any],
    reactions: list[dict[str, Any]],
) -> tuple[np.ndarray, np.ndarray, np.ndarray]:
    """Compute dense shear/moment curves for the given statically determinate beam."""

    length = spec["length"]
    xs = np.linspace(0.0, length, 2001)
    shear = np.zeros_like(xs)
    moment = np.zeros_like(xs)

    for item in spec["loads"]:
        shear += np.where(xs > item["x"], -item["value"], 0.0)
    for item in spec["uniforms"]:
        start = item["start"]
        end = item["end"]
        q = item["value"]
        contribution = np.where(
            xs < start,
            0.0,
            np.where(xs < end, -q * (xs - start), -q * (end - start)),
        )
        shear += contribution
    for item in reactions:
        if "moment" in item:
            moment += np.where(xs > item["x"], item["moment"], 0.0)
        else:
            shear += np.where(xs > item["x"], item["value"], 0.0)
    for item in spec["moments"]:
        moment += np.where(xs > item["x"], item["value"], 0.0)

    moment = np.concatenate(([0.0], np.cumsum(np.diff(xs) * shear[1:]))).astype(float)
    return xs, shear, moment


def _draw_beam_schematic(
    ax: plt.Axes,
    spec: dict[str, Any],
    reactions: list[dict[str, Any]],
) -> None:
    length = spec["length"]
    beam_half = 0.12
    ax.plot([0, length], [0, 0], color="#1f2937", linewidth=6, solid_capstyle="butt")
    ax.plot(
        [0, length],
        [beam_half, beam_half],
        color="#9ca3af",
        linewidth=1,
        linestyle="--",
    )

    for support in spec["supports"]:
        x = support["x"]
        kind = support["type"]
        if kind == "fixed":
            ax.plot(
                [x - 0.12, x, x - 0.12],
                [-0.55, -0.12, -0.55],
                color="#111827",
                linewidth=2,
            )
            ax.plot(
                [x - 0.28, x - 0.28],
                [-0.18, -0.62],
                color="#374151",
                linewidth=3,
            )
            for offset in (-0.05, 0.0, 0.05):
                ax.plot(
                    [x - 0.28, x - 0.28 + offset * 8],
                    [-0.62 + offset * 2, -0.62 + offset * 2],
                    color="#6b7280",
                    linewidth=1,
                )
        else:
            triangle = np.array([[x - 0.14, -0.12], [x + 0.14, -0.12], [x, -0.5]])
            ax.add_patch(Polygon(triangle, closed=True, facecolor="#4f46e5", edgecolor="#1e293b"))
            ax.plot(
                [x - 0.28, x + 0.28],
                [-0.5, -0.5],
                color="#374151",
                linewidth=2,
            )
            for offset in (-0.04, 0.0, 0.04):
                ax.plot(
                    [x - 0.28, x + 0.28],
                    [-0.5 + offset, -0.5 + offset],
                    color="#6b7280",
                    linewidth=1,
                )
            if kind == "roller":
                circle = Circle((x, -0.33), 0.12, facecolor="#ffffff", edgecolor="#1e293b")
                ax.add_patch(circle)

    for item in spec["loads"]:
        x = item["x"]
        label = item["label"] or f"P={item['value']:g}"
        ax.annotate(
            "",
            xy=(x, -0.45),
            xytext=(x, 0.42),
            arrowprops=dict(arrowstyle="-|>", color="#dc2626", lw=2.2),
        )
        ax.text(x, 0.5, label, ha="center", va="bottom", fontsize=10, color="#b91c1c")

    for item in spec["uniforms"]:
        start, end = item["start"], item["end"]
        label = item["label"] or f"q={item['value']:g}kN/m"
        ax.fill_between(
            [start, end],
            [-0.05, -0.05],
            [-0.45, -0.45],
            color="#f59e0b",
            alpha=0.35,
        )
        ax.text(
            (start + end) / 2,
            -0.52,
            label,
            ha="center",
            va="top",
            fontsize=9,
            color="#92400e",
        )
        for i in range(7):
            x_i = start + (end - start) * i / 6
            ax.annotate(
                "",
                xy=(x_i, -0.4),
                xytext=(x_i, -0.1),
                arrowprops=dict(arrowstyle="-|>", color="#d97706", lw=1.4),
            )

    for item in spec["moments"]:
        x = item["x"]
        label = item["label"] or f"M={item['value']:g}"
        theta = np.linspace(math.pi, 2 * math.pi, 60)
        ax.plot(
            x + 0.28 * np.cos(theta),
            0.28 + 0.28 * np.sin(theta),
            color="#0891b2",
            linewidth=2,
        )
        ax.text(x + 0.38, 0.42, label, ha="left", va="center", fontsize=10, color="#0e7490")

    for index, item in enumerate(reactions):
        x = item["x"]
        label = item["label"] or f"R{index+1}"
        ax.annotate(
            "",
            xy=(x, 0.42),
            xytext=(x, -0.45),
            arrowprops=dict(arrowstyle="-|>", color="#16a34a", lw=2.2),
        )
        ax.text(x, 0.5, label, ha="center", va="bottom", fontsize=10, color="#15803d")
        if "moment" in item:
            ax.text(x - 0.32, 0.18, f"M={item['moment']:g}", ha="right", fontsize=9, color="#0e7490")

    ax.set_xlim(-0.45, length + 0.45)
    ax.set_ylim(-0.85, 0.85)
    ax.set_aspect("equal")
    ax.axis("off")


def _draw_internal_diagram(
    ax: plt.Axes,
    xs: np.ndarray,
    values: np.ndarray,
    *,
    title: str,
    unit: str,
    fill_color: str,
) -> None:
    ax.axhline(0, color="#9ca3af", linewidth=1)
    ax.plot(xs, values, color="#111827", linewidth=2)
    ax.fill_between(xs, values, 0, color=fill_color, alpha=0.22)
    ax.set_title(title, fontsize=12)
    ax.set_xlabel("x (m)")
    ax.set_ylabel(unit)
    ax.grid(True, alpha=0.3)
    ax.margins(x=0.02)


def _render_figure(
    diagram_type: str,
    spec: dict[str, Any],
    reactions: list[dict[str, Any]],
    title: str,
) -> bytes:
    # 面向对象 API(Figure + FigureCanvasAgg),不进 pyplot 全局 Gcf:该工具现在
    # 跑在工作线程,而 excel 出图也在工作线程操作 pyplot 全局态,共用 plt 会让
    # 二者的 figure 号快照互相误关。独立 Figure 由 GC 回收,无需 plt.close。
    from matplotlib.backends.backend_agg import FigureCanvasAgg
    from matplotlib.figure import Figure

    if diagram_type in {"free_body", "beam"}:
        fig = Figure(figsize=(9, 3.2), dpi=160)
        FigureCanvasAgg(fig)
        ax = fig.subplots()
        _draw_beam_schematic(ax, spec, reactions)
        ax.set_title(title, fontsize=13)
    elif diagram_type == "combined":
        fig = Figure(figsize=(12, 3.8), dpi=160)
        FigureCanvasAgg(fig)
        axes = fig.subplots(1, 2)
        xs, shear, moment = _shear_moment_curves(spec, reactions)
        _draw_internal_diagram(
            axes[0],
            xs,
            shear,
            title="剪力图",
            unit="V (kN)",
            fill_color="#ef4444",
        )
        _draw_internal_diagram(
            axes[1],
            xs,
            moment,
            title="弯矩图",
            unit="M (kN·m)",
            fill_color="#3b82f6",
        )
        fig.suptitle(title, fontsize=13)
        fig.tight_layout(rect=(0, 0, 1, 0.94))
    else:
        xs, shear, moment = _shear_moment_curves(spec, reactions)
        fig = Figure(figsize=(9, 3.6), dpi=160)
        FigureCanvasAgg(fig)
        ax = fig.subplots()
        if diagram_type == "shear":
            _draw_internal_diagram(ax, xs, shear, title=title or "剪力图", unit="V (kN)", fill_color="#ef4444")
        else:
            _draw_internal_diagram(ax, xs, moment, title=title or "弯矩图", unit="M (kN·m)", fill_color="#3b82f6")
        fig.tight_layout()

    buffer = io.BytesIO()
    fig.savefig(buffer, format="png", bbox_inches="tight", facecolor="white")
    return buffer.getvalue()


def _safe_chart_id(filename: str | None) -> str:
    raw = str(filename or "").strip()
    if raw:
        raw = re.sub(r"[^A-Za-z0-9_-]+", "-", raw).strip("-")
    return raw[:96] or uuid.uuid4().hex[:24]


@tool("generate_mechanics_diagram", parse_docstring=True)
async def generate_mechanics_diagram_tool(
    runtime: ToolRuntime[ContextT, ThreadState],
    diagram_type: str,
    spec: str,
    title: str,
    tool_call_id: Annotated[str, InjectedToolCallId],
    filename: str | None = None,
) -> Command:
    """Generate a fundamental-mechanics diagram as a PNG artifact.

    Use this tool only when the question or knowledge base has no existing
    figure and a diagram is necessary for understanding (free-body diagram,
    shear-force diagram, bending-moment diagram).

    Args:
        diagram_type: `free_body` (or `beam`) for a beam schematic with
            supports/loads, `shear`, `moment`, or `combined` for internal force
            diagrams.
        spec: JSON string describing the beam, e.g.
            {"length": 4, "supports": [{"x": 0, "type": "pinned"},
            {"x": 4, "type": "roller"}], "loads": [{"x": 2, "value": 10,
            "label": "P=10kN"}], "uniforms": [], "moments": []}.
            Supported loads: `loads` (point forces, positive downward),
            `uniforms` (distributed loads, positive downward kN/m),
            `moments` (applied couples). Supports: `pinned`, `roller`, or
            `fixed`. For statically indeterminate beams provide `reactions`.
        title: Short diagram title, such as `简支梁受力图`.
        filename: Optional stable chart id (A-Za-z0-9_-).
    """
    try:
        normalized_type = _normalize_diagram_type(diagram_type)
        parsed = _parse_spec(spec)
        reactions = _compute_reactions(parsed)
        # matplotlib 出图是 CPU 密集(数十毫秒到数秒),离线到线程,避免卡住整个
        # worker 的事件循环(与 excel 出图一致)。_render_figure 内部已用面向
        # 对象的 Figure()+FigureCanvasAgg,不碰 pyplot 全局态,线程安全。
        png_bytes = await asyncio.to_thread(
            _render_figure,
            normalized_type,
            parsed,
            reactions,
            str(title or "").strip(),
        )
    except ValueError as exc:
        logger.warning("[mechanics-diagram] 参数错误: %s", exc)
        return Command(
            update={
                "messages": [
                    ToolMessage(
                        content=f"Error: {exc}",
                        tool_call_id=tool_call_id,
                    )
                ]
            }
        )
    except Exception as exc:
        logger.exception("[mechanics-diagram] 生成失败")
        return Command(
            update={
                "messages": [
                    ToolMessage(
                        content=f"Error: 力学图生成失败：{exc}",
                        tool_call_id=tool_call_id,
                    )
                ]
            }
        )

    thread_id = str(_resolve_runtime_thread_id(runtime) or "").strip()
    user_id = "public"
    if thread_id:
        try:
            user_id, _ = parse_internal_thread_key(thread_id)
        except Exception:
            user_id = "public"

    chart_id = _safe_chart_id(filename)
    url = await store_chart(
        png_bytes,
        user_id=user_id or "public",
        chart_id=chart_id,
    )
    caption = str(title or "").strip() or f"力学图-{normalized_type}"
    token = register_image_url(url, caption)
    content = (
        f"已生成力学图：{caption}\n"
        f"图片白名单：`{token}`\n"
        f"Markdown：![{caption}]({token})\n"
        "可直接用 `![...]([IMAGE:xxx])` 嵌入回答或 PPT slides_outline。"
    )
    return Command(
        update={
            "messages": [
                ToolMessage(
                    content=content,
                    tool_call_id=tool_call_id,
                )
            ]
        }
    )
