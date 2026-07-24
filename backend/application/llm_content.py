"""Helpers for normalizing content returned by chat-model integrations."""

from __future__ import annotations

from typing import Any


def normalize_llm_content(content: Any) -> str:
    """Flatten text and text-block responses into one string."""
    if isinstance(content, str):
        return content
    if isinstance(content, list):
        parts: list[str] = []
        for item in content:
            if isinstance(item, str):
                parts.append(item)
            elif isinstance(item, dict) and isinstance(item.get("text"), str):
                parts.append(item["text"])
        return "\n".join(parts)
    return str(content or "")


__all__ = ["normalize_llm_content"]
