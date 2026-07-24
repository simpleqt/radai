"""Shared retrieval/runtime limits for agent execution modes."""

from __future__ import annotations

from dataclasses import dataclass


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

    @classmethod
    def for_mode(
        cls,
        *,
        lightweight: bool = False,
        deep_research: bool = False,
        academic_enabled: bool = False,
    ) -> "RetrievalRuntimePolicy":
        if deep_research:
            return cls(
                kb_top_k=8,
                kb_chunk_top_k=5,
                max_result_chars=6500,
                kb_calls=3,
                web_calls=5,
                academic_calls=5 if academic_enabled else 2,
            )
        if lightweight:
            return cls(
                kb_top_k=10,
                kb_chunk_top_k=6,
                kb_calls=2,
                academic_calls=2,
            )
        return cls(
            kb_top_k=10,
            kb_chunk_top_k=6,
            kb_calls=2,
            academic_calls=5 if academic_enabled else 2,
        )

    def tool_budgets(self) -> dict[str, int]:
        budgets = {
            "multi_kb_search": self.kb_calls,
            "web_search": self.web_calls,
            "read_context_artifact": self.context_artifact_calls,
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
