"""Utilities for separating model thinking tags from visible output."""

from __future__ import annotations

import re

_THINK_OPEN_RE = re.compile(r"<(think|thought|redacted_thinking)>", re.IGNORECASE)
_THINK_CLOSE_RE = re.compile(r"</(think|thought|redacted_thinking)>", re.IGNORECASE)
_THINK_TAG_TOKENS = (
    "<think>",
    "</think>",
    "<thought>",
    "</thought>",
    "<redacted_thinking>",
    "</redacted_thinking>",
)
_MAX_THINK_TAG_LEN = max(len(token) for token in _THINK_TAG_TOKENS)


def _extract_possible_tag_prefix(text: str) -> str:
    if not text:
        return ""

    max_prefix_len = min(len(text), _MAX_THINK_TAG_LEN - 1)
    for size in range(max_prefix_len, 0, -1):
        suffix = text[-size:]
        if any(token.startswith(suffix.lower()) for token in _THINK_TAG_TOKENS):
            return suffix
    return ""


def split_thinking_segments(
    text: str,
    in_thinking: bool,
    carry: str,
) -> tuple[str, str, bool, str]:
    """Split a streamed chunk into visible text and hidden thinking text."""
    merged = f"{carry}{str(text or '')}"
    if not merged:
        return "", "", in_thinking, ""

    cursor = 0
    visible_parts: list[str] = []
    thinking_parts: list[str] = []

    while cursor < len(merged):
        if in_thinking:
            close_match = _THINK_CLOSE_RE.search(merged, cursor)
            if close_match is None:
                remaining = merged[cursor:]
                carry = _extract_possible_tag_prefix(remaining)
                thinking = remaining[:-len(carry)] if carry else remaining
                if thinking:
                    thinking_parts.append(thinking)
                return "".join(visible_parts), "".join(thinking_parts), True, carry
            thinking_parts.append(merged[cursor:close_match.start()])
            cursor = close_match.end()
            in_thinking = False
            continue

        open_match = _THINK_OPEN_RE.search(merged, cursor)
        if open_match is None:
            trailing = merged[cursor:]
            carry = _extract_possible_tag_prefix(trailing)
            visible = trailing[:-len(carry)] if carry else trailing
            if visible:
                visible_parts.append(visible)
            return "".join(visible_parts), "".join(thinking_parts), False, carry

        visible_parts.append(merged[cursor:open_match.start()])
        cursor = open_match.end()
        in_thinking = True

    return "".join(visible_parts), "".join(thinking_parts), in_thinking, ""
