"""Shared retrieval/runtime limits for agent execution modes."""

from __future__ import annotations

import os
from dataclasses import dataclass


def _context_navigator_tokens() -> int:
    """Read the per-request context_read token allowance (bounded)."""

    try:
        value = int(os.getenv("CONTEXT_NAVIGATOR_TOKENS", "10000") or "10000")
    except (TypeError, ValueError):
        value = 10000
    return max(0, min(200000, value))


@dataclass(frozen=True)
class RetrievalRuntimePolicy:
    kb_top_k: int
    kb_chunk_top_k: int
    merge_strategy: str = "rrf"
    max_result_chars: int | None = None
    web_max_results: int = 5
    web_timelimit: str | None = None
    kb_calls: int = 2
    web_calls: int = 10
    academic_calls: int = 2
    context_artifact_calls: int = 1
    context_navigator_tokens: int = 10000

    @classmethod
    def for_mode(
        cls,
        *,
        lightweight: bool = False,
        deep_research: bool = False,
        academic_enabled: bool = False,
    ) -> "RetrievalRuntimePolicy":
        navigator_tokens = _context_navigator_tokens()
        if deep_research:
            return cls(
                kb_top_k=8,
                kb_chunk_top_k=5,
                max_result_chars=6500,
                kb_calls=3,
                web_calls=5,
                academic_calls=5 if academic_enabled else 2,
                context_navigator_tokens=navigator_tokens,
            )
        if lightweight:
            return cls(
                kb_top_k=10,
                kb_chunk_top_k=6,
                kb_calls=2,
                academic_calls=2,
                context_navigator_tokens=navigator_tokens,
            )
        return cls(
            kb_top_k=10,
            kb_chunk_top_k=6,
            kb_calls=2,
            academic_calls=5 if academic_enabled else 2,
            context_navigator_tokens=navigator_tokens,
        )

    def tool_budgets(self) -> dict[str, int]:
        budgets = {
            "multi_kb_search": self.kb_calls,
            "web_search": self.web_calls,
            "read_context_artifact": self.context_artifact_calls,
            "context_read": (
                max(1, self.context_navigator_tokens // 2000)
                if self.context_navigator_tokens > 0
                else 0
            ),
        }
        if self.academic_calls > 0:
            budgets["academic_search"] = self.academic_calls
        return budgets

    def kb_tool_args(self, query: str) -> dict[str, object]:
        args: dict[str, object] = {
            "query": query,
            "top_k": self.kb_top_k,
            "chunk_top_k": self.kb_chunk_top_k,
            "merge_strategy": self.merge_strategy,
        }
        if self.max_result_chars is not None:
            args["max_result_chars"] = self.max_result_chars
        return args

    def web_tool_args(self, query: str) -> dict[str, object]:
        return {
            "query": query,
            "max_results": self.web_max_results,
            "timelimit": self.web_timelimit,
        }
