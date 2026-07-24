"""Rule-based ranking for structured context items."""

from __future__ import annotations

import math
import time
from collections.abc import Iterable

from backend.core.agents.context.summary import extract_keywords
from backend.core.agents.context.types import ContextItem


class MemoryIndex:
    def __init__(self, decay_half_life_seconds: float = 86400.0):
        self._decay_half_life = max(1.0, decay_half_life_seconds)

    def recall(
        self,
        items: Iterable[ContextItem],
        *,
        query: str,
        top_k: int,
        kinds: set[str] | None = None,
    ) -> list[ContextItem]:
        query_tokens = set(extract_keywords(query, max_keywords=48))
        now = time.time()
        scored: list[tuple[float, ContextItem]] = []
        for item in items:
            if kinds and item.get("kind") not in kinds:
                continue
            item_tokens = set(item.get("keywords") or [])
            overlap = len(query_tokens & item_tokens)
            age = max(0.0, now - float(item.get("created_at") or now))
            decay = math.pow(0.5, age / self._decay_half_life)
            kind_bonus = 0.3 if item.get("kind") in {"retrieval_evidence", "subagent_result"} else 0.0
            score = float(item.get("importance") or 0.0) + overlap * 0.35 + decay * 0.2 + kind_bonus
            scored.append((score, item))
        scored.sort(key=lambda pair: pair[0], reverse=True)
        return [item for _, item in scored[: max(0, top_k)]]

    @staticmethod
    def deduplicate(items: Iterable[ContextItem]) -> list[ContextItem]:
        seen: set[tuple[str, str, str]] = set()
        ordered: list[ContextItem] = []
        for item in items:
            key = (
                str(item.get("kind") or "generic"),
                str(item.get("source_tool") or "unknown"),
                str(item.get("summary") or "")[:240],
            )
            if key in seen:
                continue
            seen.add(key)
            ordered.append(item)
        return ordered
