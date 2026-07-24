"""Small deterministic helpers used by context governance."""

from __future__ import annotations

import json
import re
from typing import Any


def normalize_text(value: Any) -> str:
    if isinstance(value, str):
        return value.strip()
    try:
        return json.dumps(value, ensure_ascii=False, default=str).strip()
    except (TypeError, ValueError):
        return str(value).strip()


def generate_summary(value: Any, *, max_chars: int = 4000) -> str:
    """Create a loss-aware extract without another model call.

    The head retains schemas and primary results while the tail commonly keeps
    totals, errors, citations, and file paths. The complete payload is stored
    separately when it exceeds the configured persistence threshold.
    """

    text = normalize_text(value)
    if len(text) <= max_chars:
        return text
    if max_chars < 200:
        return text[:max_chars]

    marker = "\n... [middle omitted; raw result available by reference] ...\n"
    available = max_chars - len(marker)
    head_size = max(1, int(available * 0.72))
    tail_size = max(1, available - head_size)
    return f"{text[:head_size].rstrip()}{marker}{text[-tail_size:].lstrip()}"


def extract_keywords(value: Any, *, max_keywords: int = 24) -> list[str]:
    text = normalize_text(value).lower()
    tokens = re.findall(r"[\u4e00-\u9fff]{2,}|[a-zA-Z_][a-zA-Z0-9_.-]{2,}", text)
    keywords: list[str] = []
    seen: set[str] = set()
    for token in tokens:
        if token in seen:
            continue
        seen.add(token)
        keywords.append(token)
        if len(keywords) >= max_keywords:
            break
    return keywords


def estimate_chars(parts: list[str]) -> int:
    return sum(len(part) for part in parts)
