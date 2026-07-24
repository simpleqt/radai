"""Small JSON repair helpers for LLM extraction responses."""

from __future__ import annotations

import json
import re
from typing import Any


_FENCE_RE = re.compile(r"```(?:json)?\s*(.*?)```", re.DOTALL | re.IGNORECASE)
_TRAILING_COMMA_RE = re.compile(r",\s*([}\]])")
_CONTROL_CHAR_RE = re.compile(r"[\x00-\x08\x0b\x0c\x0e-\x1f]")


def _extract_json_span(text: str) -> str:
    raw = (text or "").strip()
    match = _FENCE_RE.search(raw)
    if match:
        raw = match.group(1).strip()
    if not raw.startswith("{"):
        start = raw.find("{")
        end = raw.rfind("}")
        if start >= 0 and end > start:
            raw = raw[start : end + 1]
    return _CONTROL_CHAR_RE.sub("", raw)


def _fix_trailing_commas(text: str) -> str:
    return _TRAILING_COMMA_RE.sub(r"\1", text)


def _escape_invalid_string_backslashes(text: str) -> str:
    """Escape LaTeX-style backslashes that are invalid inside JSON strings."""
    result: list[str] = []
    in_string = False
    escape = False
    idx = 0
    while idx < len(text):
        ch = text[idx]
        if not in_string:
            result.append(ch)
            if ch == '"':
                in_string = True
            idx += 1
            continue

        if escape:
            if ch in '"\\/bfnrt':
                result.append(ch)
            elif ch == "u" and idx + 4 < len(text) and re.match(r"^[0-9a-fA-F]{4}$", text[idx + 1 : idx + 5]):
                result.append(ch)
            else:
                result.append("\\")
                result.append(ch)
            escape = False
            idx += 1
            continue

        if ch == "\\":
            result.append(ch)
            escape = True
            idx += 1
            continue

        result.append(ch)
        if ch == '"':
            in_string = False
        idx += 1

    if escape:
        result.append("\\")
    return "".join(result)


def _balance_brackets(text: str) -> str:
    """Close unclosed braces/brackets for truncated model output."""
    stack: list[str] = []
    in_string = False
    escape = False
    for ch in text:
        if in_string:
            if escape:
                escape = False
            elif ch == "\\":
                escape = True
            elif ch == '"':
                in_string = False
            continue
        if ch == '"':
            in_string = True
        elif ch == "{":
            stack.append("}")
        elif ch == "[":
            stack.append("]")
        elif ch in "}]" and stack and stack[-1] == ch:
            stack.pop()

    trimmed = text.rstrip()
    while trimmed and trimmed[-1] in ",: \n\r\t":
        trimmed = trimmed[:-1].rstrip()
    if in_string:
        trimmed += '"'
    return trimmed + "".join(reversed(stack))


def _find_balanced(text: str, start: int, open_ch: str, close_ch: str) -> int:
    depth = 0
    in_string = False
    escape = False
    for idx in range(start, len(text)):
        ch = text[idx]
        if in_string:
            if escape:
                escape = False
            elif ch == "\\":
                escape = True
            elif ch == '"':
                in_string = False
            continue
        if ch == '"':
            in_string = True
        elif ch == open_ch:
            depth += 1
        elif ch == close_ch:
            depth -= 1
            if depth == 0:
                return idx
    return -1


def _parse_object_list_relaxed(array_text: str) -> list[dict[str, Any]]:
    items: list[dict[str, Any]] = []
    pos = 0
    while pos < len(array_text):
        start = array_text.find("{", pos)
        if start < 0:
            break
        end = _find_balanced(array_text, start, "{", "}")
        if end < 0:
            break
        fragment = _fix_trailing_commas(array_text[start : end + 1])
        try:
            parsed = json.loads(fragment)
            if isinstance(parsed, dict):
                items.append(parsed)
        except json.JSONDecodeError:
            pass
        pos = end + 1
    return items


def _salvage_top_level_arrays(text: str) -> dict[str, Any] | None:
    """Recover entities/relations arrays when the outer JSON object is malformed."""
    result: dict[str, Any] = {}
    for key in ("entities", "relations"):
        match = re.search(rf'"{key}"\s*:\s*\[', text)
        if not match:
            continue
        start = match.end() - 1
        end = _find_balanced(text, start, "[", "]")
        if end < 0:
            array_text = text[start:]
            items = _parse_object_list_relaxed(array_text)
        else:
            array_text = text[start : end + 1]
            try:
                parsed = json.loads(_fix_trailing_commas(array_text))
                items = parsed if isinstance(parsed, list) else []
            except json.JSONDecodeError:
                items = _parse_object_list_relaxed(array_text)
        if items:
            result[key] = items
    return result or None


def _attempt_parse(text: str) -> dict[str, Any]:
    parsed = json.loads(text)
    if not isinstance(parsed, dict):
        raise ValueError("LLM extraction response must be a JSON object")
    return parsed


def loads_json_object(text: str) -> dict[str, Any]:
    """Parse a JSON object from a model response with multi-stage cleanup."""
    raw = _extract_json_span(text)
    trailing_fixed = _fix_trailing_commas(raw)
    escaped = _escape_invalid_string_backslashes(trailing_fixed)
    candidates = [
        raw,
        trailing_fixed,
        escaped,
        _balance_brackets(trailing_fixed),
        _balance_brackets(escaped),
    ]
    seen: set[str] = set()
    last_error: Exception | None = None
    for candidate in candidates:
        if not candidate or candidate in seen:
            continue
        seen.add(candidate)
        try:
            return _attempt_parse(candidate)
        except json.JSONDecodeError as exc:
            last_error = exc
            continue

    salvaged = _salvage_top_level_arrays(raw)
    if salvaged:
        salvaged.setdefault("entities", [])
        salvaged.setdefault("relations", [])
        return salvaged

    if last_error is not None:
        raise last_error
    raise ValueError("LLM extraction response is empty or not JSON")
