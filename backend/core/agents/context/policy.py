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


def context_env_float(
    name: str,
    default: float,
    *,
    minimum: float = 0.0,
    maximum: float | None = None,
) -> float:
    """Read a bounded float without making agent construction env-fragile."""

    raw = (os.getenv(name, "") or "").strip()
    try:
        value = float(raw) if raw else float(default)
    except ValueError:
        value = float(default)
    value = max(float(minimum), value)
    if maximum is not None:
        value = min(float(maximum), value)
    return value


@dataclass(frozen=True)
class ContextPolicy:
    """Resolved context policy shared by parent, child and writer agents.

    Character limits govern result capture only. Model history and JIT assembly
    are always governed by the model's token budget.
    """

    model_name: str | None
    ledger_budget: LedgerBudget
    context_object_mode: str = "off"
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
    compaction_trigger_tokens: int = 6144
    compaction_retained_window_tokens: int = 12000
    compaction_summary_input_chars: int = 80000
    compaction_summary_output_chars: int = 4000
    compaction_summary_max_chunks: int = 4
    compaction_early_ratio: float = 0.85
    compaction_timeout_seconds: int = 90
    context_pack_placement: str = "system"
    session_memory_enabled: bool = True
    context_navigator_token_pool: int = 96000
    context_navigator_max_tokens_per_read: int = 2000
    context_object_ttl_seconds: int = 14 * 86400
    activation_scoring: bool = True
    activation_overlap_floor: float = 0.15
    activation_half_life_turns: float = 2.0
    activation_turn_boost: float = 1.25
    activation_omit_threshold: float = 0.08
    activation_missing_turn_age: float = 3.0
    registration_top_k_segments: int = 300
    registration_top_k_objects: int = 64
    registration_sticky_window_calls: int = 1
    registration_ranked_reserved_segments: int = 75
    tool_msg_budget_use_remaining: bool = True

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
        output_chars = context_env_int("RESULT_SUMMARY_MAX_CHARS", 3000, minimum=600)
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
        retained_window_default = max(4000, min(20000, int(input_budget * 0.35)))
        retained_window_tokens = context_env_int(
            "AGENT_COMPACTION_RETAINED_WINDOW_TOKENS",
            context_env_int(
                "AGENT_COMPACTION_RETAINED_USER_TOKENS",
                retained_window_default,
                minimum=1000,
                maximum=max(1000, input_budget),
            ),
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
            # 2026-08-13 起 env 默认 on:正式生产与 Docker 测试环境均已长期
            # 运行 CONTEXT_OBJECT_MODE=on,off 默认只会让全新部署偏离真实
            # 运行态;显式设置 off/shadow 仍然生效,非法值回落 on。
            context_object_mode=(
                str(os.getenv("CONTEXT_OBJECT_MODE", "on").strip().lower())
                if str(os.getenv("CONTEXT_OBJECT_MODE", "on")).strip().lower()
                in {"off", "shadow", "on"}
                else "on"
            ),
            large_result_threshold_chars=context_env_int(
                "AGENT_LARGE_RESULT_THRESHOLD_CHARS", 3000, minimum=600
            ),
            result_summary_input_chars=max(
                output_chars * 2,
                context_env_int("RESULT_SUMMARY_INPUT_MAX_CHARS", 60000, minimum=1200),
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
            compaction_trigger_tokens=compaction_trigger,
            compaction_retained_window_tokens=retained_window_tokens,
            compaction_summary_input_chars=max(
                compaction_output_chars * 3,
                context_env_int(
                    "AGENT_COMPACTION_SUMMARY_INPUT_CHARS",
                    80000,
                    minimum=4000,
                ),
            ),
            compaction_summary_output_chars=compaction_output_chars,
            compaction_summary_max_chunks=context_env_int(
                "AGENT_COMPACTION_SUMMARY_MAX_CHUNKS",
                4,
                minimum=1,
                maximum=8,
            ),
            compaction_early_ratio=context_env_float(
                "AGENT_COMPACTION_EARLY_RATIO",
                0.85,
                minimum=0.5,
                maximum=1.0,
            ),
            compaction_timeout_seconds=context_env_int(
                "AGENT_COMPACTION_TIMEOUT_SECONDS", 90, minimum=10
            ),
            context_pack_placement=(
                str(os.getenv("CONTEXT_PACK_PLACEMENT", "system").strip().lower())
                if str(os.getenv("CONTEXT_PACK_PLACEMENT", "system")).strip().lower()
                in {"system", "tail"}
                else "system"
            ),
            session_memory_enabled=str(
                os.getenv("AGENT_SESSION_MEMORY", "on").strip().lower()
            )
            not in {"off", "0", "false", "no"},
            context_navigator_token_pool=context_env_int(
                "CONTEXT_OBJECT_TOKEN_POOL",
                96000,
                minimum=4096,
            ),
            context_navigator_max_tokens_per_read=context_env_int(
                "CONTEXT_READ_MAX_TOKENS",
                2000,
                minimum=128,
            ),
            context_object_ttl_seconds=context_env_int(
                "CONTEXT_OBJECT_TTL_SECONDS",
                14 * 86400,
                minimum=0,
            ),
            activation_scoring=str(
                os.getenv("CONTEXT_ACTIVATION_SCORING", "on").strip().lower()
            )
            == "on",
            activation_overlap_floor=context_env_float(
                "CONTEXT_ACTIVATION_OVERLAP_FLOOR",
                0.15,
                minimum=0.0,
                maximum=1.0,
            ),
            activation_half_life_turns=context_env_float(
                "CONTEXT_ACTIVATION_HALF_LIFE_TURNS",
                2.0,
                minimum=0.1,
            ),
            activation_turn_boost=context_env_float(
                "CONTEXT_ACTIVATION_TURN_BOOST",
                1.25,
                minimum=1.0,
                maximum=4.0,
            ),
            activation_omit_threshold=context_env_float(
                "CONTEXT_ACTIVATION_OMIT_THRESHOLD",
                0.08,
                minimum=0.0,
                maximum=1.0,
            ),
            activation_missing_turn_age=context_env_float(
                "CONTEXT_ACTIVATION_MISSING_TURN_AGE",
                3.0,
                minimum=0.0,
            ),
            registration_top_k_segments=context_env_int(
                "CONTEXT_REGISTRATION_TOP_K_SEGMENTS",
                300,
                minimum=50,
                maximum=2000,
            ),
            registration_top_k_objects=context_env_int(
                "CONTEXT_REGISTRATION_TOP_K_OBJECTS",
                64,
                minimum=10,
                maximum=200,
            ),
            registration_sticky_window_calls=context_env_int(
                "CONTEXT_STICKY_WINDOW_CALLS",
                1,
                minimum=1,
                maximum=8,
            ),
            registration_ranked_reserved_segments=context_env_int(
                "CONTEXT_REGISTRATION_RANKED_RESERVED_SEGMENTS",
                75,
                minimum=0,
                maximum=2000,
            ),
            tool_msg_budget_use_remaining=str(
                os.getenv("CONTEXT_TOOL_MSG_BUDGET_USE_REMAINING", "true")
                .strip()
                .lower()
            )
            == "true",
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
