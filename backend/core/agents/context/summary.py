"""Small deterministic helpers used by context governance."""

from __future__ import annotations

import json
import re
from typing import Any


SUMMARY_PREFIX = (
    "Another language model started to solve this problem and produced a summary "
    "of its work. Use this summary to continue without repeating prior steps:"
)
COMPACTION_REWRITE_METADATA_KEY = "context_compaction_rewrite"

# Single source for internal tool-message placeholder prefixes. Consumed by
# ContextPruningMiddleware (skip re-capturing already-replaced bodies) and by
# conversation-history serialization (hide internal placeholders from the
# history API); keep both behaviors in sync by editing only this tuple.
CAPTURED_CONTENT_PREFIXES = (
    "[State Elevation:",
    "[Context Isolation:",
    "[Result Capture:",
    "[Result Summarizer Handoff:",
    "[Structured Result Compaction:",
    "[Context Artifact Read:",
    "[Context Object:",
    "[System Notification:",
)


def normalize_text(value: Any) -> str:
    if isinstance(value, str):
        return value.strip()
    try:
        return json.dumps(value, ensure_ascii=False, default=str).strip()
    except (TypeError, ValueError):
        return str(value).strip()


def is_compaction_summary_text(value: Any) -> bool:
    """Return True when a message body is a generated context-checkpoint summary."""

    return normalize_text(value).startswith(SUMMARY_PREFIX)


def mark_compaction_rewrite(
    message: Any,
    *,
    reason: str,
    original_chars: int,
    rewritten_chars: int,
) -> Any:
    """Copy a message and record that compaction rewrote its content.

    The message id is intentionally preserved so LangGraph's reducer replaces
    the original checkpointed message; the metadata keeps an audit trail that
    this same id now carries a shortened body.
    """

    metadata = dict(getattr(message, "additional_kwargs", None) or {})
    metadata[COMPACTION_REWRITE_METADATA_KEY] = {
        "reason": str(reason),
        "original_chars": max(0, int(original_chars)),
        "rewritten_chars": max(0, int(rewritten_chars)),
    }
    return message.model_copy(update={"additional_kwargs": metadata})


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
