"""工具展示载荷（`display`）的契约与上限。

过程事件的 `detail` 只放人话，工具原始返回值不进 UI。展示语义在服务端确定，
前端按 `view` 分发渲染，不再靠正则猜。

所有载荷都要过 `normalize_display_payload`：超限自动降级并记 warning，
绝不抛异常，渲染改造不应影响主流程可用性。
"""

from __future__ import annotations

import json
import logging
from collections.abc import Sequence
from dataclasses import dataclass, field
from typing import Any, Literal

logger = logging.getLogger(__name__)

ChipTone = Literal["info", "success", "warning", "error"]
DisplayViewName = Literal["text", "kv", "table", "issues", "sources", "artifact"]

MAX_CHIPS = 6
MAX_TABLE_ROWS = 20
MAX_TABLE_COLUMNS = 8
MAX_ISSUE_ITEMS = 20
MAX_KV_ROWS = 12
MAX_SOURCES = 6
MAX_ACTION_OPTIONS = 4
MAX_LABEL_CHARS = 120
MAX_STATUS_CHARS = 260
MAX_CELL_CHARS = 80
MAX_DISPLAY_BYTES = 8192

_VIEWS: frozenset[str] = frozenset(
    {"text", "kv", "table", "issues", "sources", "artifact"}
)
_TONES: frozenset[str] = frozenset({"info", "success", "warning", "error"})


@dataclass(frozen=True)
class DisplayChip:
    text: str
    tone: ChipTone = "info"


@dataclass(frozen=True)
class DisplayActionOption:
    label: str
    #: 点击后直接发送到对话的指令
    message: str
    tone: ChipTone = "info"


@dataclass(frozen=True)
class DisplayAction:
    kind: Literal["confirm", "choose", "edit"]
    prompt: str
    options: Sequence[DisplayActionOption] = ()
    #: 「我来修正」填入输入框的模板
    input_template: str = ""


@dataclass(frozen=True)
class ToolDisplay:
    label: str
    status_text: str = ""
    chips: Sequence[DisplayChip] = ()
    view: DisplayViewName = "text"
    data: dict[str, Any] = field(default_factory=dict)
    action: DisplayAction | None = None

    def to_payload(self) -> dict[str, Any]:
        payload: dict[str, Any] = {"label": self.label}
        if self.status_text:
            payload["status_text"] = self.status_text
        if self.chips:
            payload["chips"] = [
                {"text": chip.text, "tone": chip.tone} for chip in self.chips
            ]
        if self.view != "text" or self.data:
            payload["view"] = self.view
            payload["data"] = dict(self.data)
        if self.action is not None:
            action: dict[str, Any] = {
                "kind": self.action.kind,
                "prompt": self.action.prompt,
            }
            if self.action.options:
                action["options"] = [
                    {
                        "label": option.label,
                        "message": option.message,
                        "tone": option.tone,
                    }
                    for option in self.action.options
                ]
            if self.action.input_template:
                action["input_template"] = self.action.input_template
            payload["action"] = action
        return payload


@dataclass(frozen=True)
class ToolPresenterInput:
    """presenter 的输入。原始返回值只在这里出现，不会流向 UI。"""

    tool_name: str
    tool_input: dict[str, Any] = field(default_factory=dict)
    raw_output: Any = None
    duration_ms: int | None = None

    def parsed_output(self) -> dict[str, Any] | None:
        """把工具返回值解析成 dict；不是 JSON 对象时返回 None。"""
        value = self.raw_output
        if isinstance(value, dict):
            return value
        if not isinstance(value, str):
            value = getattr(value, "content", None)
            if not isinstance(value, str):
                return None
        text = value.strip()
        if not text.startswith("{"):
            return None
        try:
            parsed = json.loads(text)
        except (ValueError, TypeError):
            return None
        return parsed if isinstance(parsed, dict) else None


# ── 归一化与上限 ──────────────────────────────────────────────────────


def _text(value: Any, limit: int) -> str:
    text = " ".join(str(value or "").split())
    if len(text) > limit:
        return f"{text[:limit]}..."
    return text


def _tone(value: Any) -> str:
    tone = str(value or "info")
    return tone if tone in _TONES else "info"


def _normalize_chips(raw: Any) -> list[dict[str, Any]]:
    chips: list[dict[str, Any]] = []
    for item in raw if isinstance(raw, list) else []:
        if not isinstance(item, dict):
            continue
        text = _text(item.get("text"), 40)
        if not text:
            continue
        chips.append({"text": text, "tone": _tone(item.get("tone"))})
        if len(chips) >= MAX_CHIPS:
            break
    return chips


def _normalize_table(data: Any) -> dict[str, Any] | None:
    if not isinstance(data, dict):
        return None

    columns: list[dict[str, Any]] = []
    for item in data.get("columns") or []:
        if not isinstance(item, dict):
            continue
        key = str(item.get("key") or "").strip()
        if not key:
            continue
        align = item.get("align")
        columns.append(
            {
                "key": key,
                "label": _text(item.get("label") or key, 40),
                "align": align if align in {"left", "center", "right"} else "left",
            }
        )
        if len(columns) >= MAX_TABLE_COLUMNS:
            break
    if not columns:
        return None

    all_rows = [row for row in (data.get("rows") or []) if isinstance(row, dict)]
    rows: list[dict[str, Any]] = []
    for row in all_rows[:MAX_TABLE_ROWS]:
        cell: dict[str, Any] = {}
        for column in columns:
            value = row.get(column["key"])
            if value is None or isinstance(value, (int, float, bool)):
                cell[column["key"]] = value
            else:
                cell[column["key"]] = _text(value, MAX_CELL_CHARS)
        rows.append(cell)
    if not rows:
        return None

    declared_total = data.get("total_rows")
    total_rows = (
        int(declared_total)
        if isinstance(declared_total, int) and declared_total >= len(all_rows)
        else len(all_rows)
    )
    return {
        "columns": columns,
        "rows": rows,
        "total_rows": total_rows,
        "truncated": total_rows > len(rows),
    }


def _normalize_issues(data: Any) -> dict[str, Any] | None:
    if not isinstance(data, dict):
        return None

    items: list[dict[str, Any]] = []
    for item in data.get("items") or []:
        if not isinstance(item, dict):
            continue
        message = _text(item.get("message"), 200)
        if not message:
            continue
        level = str(item.get("level") or "info")
        entry: dict[str, Any] = {
            "level": level if level in {"error", "warning", "info"} else "info",
            "message": message,
        }
        code = _text(item.get("code"), 32)
        if code:
            entry["code"] = code
        locator = _text(item.get("locator"), 60)
        if locator:
            entry["locator"] = locator
        items.append(entry)
    if not items:
        return None

    ordered = (
        [item for item in items if item["level"] == "error"]
        + [item for item in items if item["level"] == "warning"]
        + [item for item in items if item["level"] == "info"]
    )[:MAX_ISSUE_ITEMS]
    return {
        "items": ordered,
        "counts": {
            "error": sum(1 for item in items if item["level"] == "error"),
            "warning": sum(1 for item in items if item["level"] == "warning"),
        },
    }


def _normalize_kv(data: Any) -> dict[str, Any] | None:
    if not isinstance(data, dict):
        return None
    rows: list[dict[str, Any]] = []
    for item in data.get("rows") or []:
        if not isinstance(item, dict):
            continue
        label = _text(item.get("label"), 40)
        value = _text(item.get("value"), 120)
        if not label and not value:
            continue
        row: dict[str, Any] = {"label": label or "-", "value": value}
        if item.get("tone") in _TONES:
            row["tone"] = item["tone"]
        rows.append(row)
        if len(rows) >= MAX_KV_ROWS:
            break
    return {"rows": rows} if rows else None


def _normalize_sources(data: Any) -> dict[str, Any] | None:
    if not isinstance(data, dict):
        return None
    items: list[dict[str, Any]] = []
    seen: set[str] = set()
    for item in data.get("items") or []:
        if not isinstance(item, dict):
            continue
        url = str(item.get("url") or "").strip()
        if not url or url in seen:
            continue
        seen.add(url)
        entry: dict[str, Any] = {"url": url}
        title = _text(item.get("title"), 120)
        if title:
            entry["title"] = title
        domain = str(item.get("domain") or "").strip()
        if domain:
            entry["domain"] = domain
        items.append(entry)
        if len(items) >= MAX_SOURCES:
            break
    return {"items": items} if items else None


def _normalize_artifact(data: Any) -> dict[str, Any] | None:
    if not isinstance(data, dict):
        return None
    title = _text(data.get("title"), 120)
    if not title:
        return None
    artifact: dict[str, Any] = {"title": title}
    for key in ("kind", "url", "size"):
        value = str(data.get(key) or "").strip()
        if value:
            artifact[key] = value
    return artifact


def _normalize_text(data: Any, fallback: str) -> dict[str, Any] | None:
    text = ""
    if isinstance(data, dict):
        text = _text(data.get("text"), MAX_STATUS_CHARS)
    text = text or fallback
    return {"text": text} if text else None


_VIEW_NORMALIZERS = {
    "table": _normalize_table,
    "issues": _normalize_issues,
    "kv": _normalize_kv,
    "sources": _normalize_sources,
    "artifact": _normalize_artifact,
}


def _normalize_action(raw: Any) -> dict[str, Any] | None:
    if not isinstance(raw, dict):
        return None
    prompt = _text(raw.get("prompt"), MAX_STATUS_CHARS)
    if not prompt:
        return None

    kind = str(raw.get("kind") or "confirm")
    options: list[dict[str, Any]] = []
    for item in raw.get("options") or []:
        if not isinstance(item, dict):
            continue
        label = _text(item.get("label"), 40)
        message = str(item.get("message") or "").strip()
        if not label or not message:
            continue
        options.append(
            {"label": label, "message": message, "tone": _tone(item.get("tone"))}
        )
        if len(options) >= MAX_ACTION_OPTIONS:
            break

    input_template = str(raw.get("input_template") or "").strip()
    if not options and not input_template:
        return None

    action: dict[str, Any] = {
        "kind": kind if kind in {"confirm", "choose", "edit"} else "confirm",
        "prompt": prompt,
    }
    if options:
        action["options"] = options
    if input_template:
        action["input_template"] = input_template
    return action


def _payload_size(payload: dict[str, Any]) -> int:
    return len(json.dumps(payload, ensure_ascii=False).encode("utf-8"))


def normalize_display_payload(
    raw: Any, *, tool_name: str = ""
) -> dict[str, Any] | None:
    """校验并裁剪展示载荷。形状不合法或超限时降级，永不抛异常。"""

    if not isinstance(raw, dict):
        return None

    label = _text(raw.get("label"), MAX_LABEL_CHARS)
    if not label:
        return None

    status_text = _text(raw.get("status_text"), MAX_STATUS_CHARS)
    payload: dict[str, Any] = {"label": label}
    if status_text:
        payload["status_text"] = status_text

    chips = _normalize_chips(raw.get("chips"))
    if chips:
        payload["chips"] = chips

    view = str(raw.get("view") or "text")
    if view not in _VIEWS:
        view = "text"
    normalizer = _VIEW_NORMALIZERS.get(view)
    data = (
        normalizer(raw.get("data"))
        if normalizer is not None
        else _normalize_text(raw.get("data"), status_text)
    )
    if data is None and view != "text":
        # 结构化载荷不合法时回落文本，不把原始对象透给渲染层。
        view, data = "text", _normalize_text(None, status_text)
    if data:
        payload["view"] = view
        payload["data"] = data

    action = _normalize_action(raw.get("action"))
    if action:
        payload["action"] = action

    if _payload_size(payload) <= MAX_DISPLAY_BYTES:
        return payload

    # 超限降级：先丢结构化载荷，再丢 chips。
    logger.warning(
        "[ToolPresenter] display payload over budget, degrading: tool=%s view=%s",
        tool_name or "unknown",
        view,
    )
    payload.pop("data", None)
    payload.pop("view", None)
    if _payload_size(payload) <= MAX_DISPLAY_BYTES:
        return payload

    payload.pop("chips", None)
    if _payload_size(payload) <= MAX_DISPLAY_BYTES:
        return payload

    return {"label": label}
