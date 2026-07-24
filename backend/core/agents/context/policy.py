"""Single source of truth for agent context budgets and compaction limits."""

from __future__ import annotations

import os
from dataclasses import dataclass, replace

from backend.core.agents.context.ledger import LedgerBudget


def context_env_int(
    name: str,
    default: int,
    *,
    minimum: int = 0,
    maximum: int | None = None,
) -> int:
    """Read a bounded integer without making agent construction env-fragile."""

    raw = (os.getenv(name, "") or "").strip()
    try:
        value = int(raw) if raw else default
    except ValueError:
        value = default
    value = max(minimum, value)
    if maximum is not None:
        value = min(maximum, value)
    return value


@dataclass(frozen=True)
class ContextPolicy:
    """Resolved context policy shared by parent, child and writer agents.

    Character limits govern result capture only. Model history and JIT assembly
    are always governed by the model's token budget.
    """

    model_name: str | None
    ledger_budget: LedgerBudget
    large_result_threshold_chars: int = 3000
    result_summary_input_chars: int = 60000
    result_summary_output_chars: int = 3000
    result_summary_max_concurrent: int = 5
    result_summary_timeout_seconds: int = 90
    result_summary_cache_ttl_seconds: int = 3600
    result_summary_cache_max_entries: int = 256
    context_item_summary_chars: int = 6000
    context_record_top_k: int = 18
    retrieval_fallback_blocks: int = 2
    retrieval_fallback_chars: int = 14000
    project_fallback_blocks: int = 4
    project_fallback_chars: int = 12000
    history_summary_trigger_tokens: int = 4096
    history_summary_keep_tokens: int = 2048
    dynamic_context_reserved_tokens: int = 2048
    raw_ref_injection_chars: int = 12000
    retrieval_raw_window_max_results: int = 8
    retrieval_raw_window_budget_percent: int = 20
    compaction_trigger_tokens: int = 6144
    compaction_retained_user_tokens: int = 12000
    compaction_summary_input_chars: int = 80000
    compaction_summary_output_chars: int = 4000
    compaction_timeout_seconds: int = 90

    @property
    def input_budget_tokens(self) -> int:
        return self.ledger_budget.input_budget_tokens

    @classmethod
    def from_env(cls, model_name: str | None) -> "ContextPolicy":
        budget = LedgerBudget.from_model_name(model_name)
        input_budget = budget.input_budget_tokens
        trigger_default = max(4096, int(input_budget * 0.80))
        keep_default = max(2048, int(input_budget * 0.25))
        trigger_tokens = context_env_int(
            "AGENT_HISTORY_SUMMARIZATION_TRIGGER_TOKENS",
            trigger_default,
            minimum=4096,
        )
        keep_tokens = context_env_int(
            "AGENT_HISTORY_SUMMARIZATION_KEEP_TOKENS",
            keep_default,
            minimum=2048,
        )
        keep_tokens = min(keep_tokens, max(2048, trigger_tokens // 2))
        output_chars = context_env_int(
            "RESULT_SUMMARY_MAX_CHARS", 3000, minimum=600
        )
        dynamic_reserved = context_env_int(
            "AGENT_DYNAMIC_CONTEXT_RESERVED_TOKENS",
            max(1024, min(4096, int(input_budget * 0.15))),
            minimum=512,
            maximum=max(512, input_budget // 2),
        )
        compaction_trigger = context_env_int(
            "AGENT_COMPACTION_TRIGGER_TOKENS",
            trigger_default,
            minimum=4096,
        )
        # The Codex-style replacement window is the primary path. Keep the
        # framework summarizer at the same or a later threshold as a fallback.
        compaction_trigger = min(compaction_trigger, trigger_tokens)
        retained_user_tokens = context_env_int(
            "AGENT_COMPACTION_RETAINED_USER_TOKENS",
            max(4000, min(20000, int(input_budget * 0.35))),
            minimum=1000,
            maximum=max(1000, input_budget),
        )
        compaction_output_chars = context_env_int(
            "AGENT_COMPACTION_SUMMARY_MAX_CHARS",
            4000,
            minimum=800,
        )
        return cls(
            model_name=model_name,
            ledger_budget=budget,
            large_result_threshold_chars=context_env_int(
                "AGENT_LARGE_RESULT_THRESHOLD_CHARS", 3000, minimum=600
            ),
            result_summary_input_chars=max(
                output_chars * 2,
                context_env_int(
                    "RESULT_SUMMARY_INPUT_MAX_CHARS", 60000, minimum=1200
                ),
            ),
            result_summary_output_chars=output_chars,
            result_summary_max_concurrent=context_env_int(
                "RESULT_SUMMARY_MAX_PER_ROUND", 5, minimum=1, maximum=5
            ),
            result_summary_timeout_seconds=context_env_int(
                "RESULT_SUMMARY_TIMEOUT_SECONDS", 90, minimum=10
            ),
            result_summary_cache_ttl_seconds=context_env_int(
                "RESULT_SUMMARY_CACHE_TTL_SECONDS",
                3600,
                minimum=0,
                maximum=86400,
            ),
            result_summary_cache_max_entries=context_env_int(
                "RESULT_SUMMARY_CACHE_MAX_ENTRIES",
                256,
                minimum=1,
                maximum=2048,
            ),
            context_item_summary_chars=context_env_int(
                "JIT_CONTEXT_ITEM_CHARS", 6000, minimum=2000
            ),
            context_record_top_k=max(
                12,
                context_env_int("JIT_CONTEXT_ITEM_TOP_K", 6, minimum=1) * 3,
            ),
            retrieval_fallback_blocks=context_env_int(
                "JIT_MAX_EVIDENCE_BLOCKS", 2, minimum=1
            ),
            retrieval_fallback_chars=context_env_int(
                "JIT_MAX_EVIDENCE_CHARS", 14000, minimum=2000
            ),
            project_fallback_blocks=context_env_int(
                "JIT_MAX_PROJECT_EVIDENCE_BLOCKS", 4, minimum=1
            ),
            project_fallback_chars=context_env_int(
                "JIT_MAX_PROJECT_EVIDENCE_CHARS", 12000, minimum=2000
            ),
            history_summary_trigger_tokens=trigger_tokens,
            history_summary_keep_tokens=keep_tokens,
            dynamic_context_reserved_tokens=dynamic_reserved,
            raw_ref_injection_chars=context_env_int(
                "JIT_RAW_REF_INJECTION_CHARS", 12000, minimum=500
            ),
            retrieval_raw_window_max_results=context_env_int(
                "JIT_RETRIEVAL_RAW_WINDOW_MAX_RESULTS",
                8,
                minimum=1,
                maximum=16,
            ),
            retrieval_raw_window_budget_percent=context_env_int(
                "JIT_RETRIEVAL_RAW_WINDOW_BUDGET_PERCENT",
                20,
                minimum=5,
                maximum=70,
            ),
            compaction_trigger_tokens=compaction_trigger,
            compaction_retained_user_tokens=retained_user_tokens,
            compaction_summary_input_chars=max(
                compaction_output_chars * 3,
                context_env_int(
                    "AGENT_COMPACTION_SUMMARY_INPUT_CHARS",
                    80000,
                    minimum=4000,
                ),
            ),
            compaction_summary_output_chars=compaction_output_chars,
            compaction_timeout_seconds=context_env_int(
                "AGENT_COMPACTION_TIMEOUT_SECONDS", 90, minimum=10
            ),
        )

    def with_capture_overrides(
        self,
        *,
        char_threshold: int | None = None,
    ) -> "ContextPolicy":
        """Keep compatibility with focused tests and explicit runtime overrides."""

        if char_threshold is None:
            return self
        return replace(
            self,
            large_result_threshold_chars=max(1, int(char_threshold)),
        )
