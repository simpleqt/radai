"""Token-aware context ledger and evidence selection."""

from __future__ import annotations

import logging
import os
import uuid
from collections import OrderedDict

from langchain_core.messages import AIMessage, HumanMessage, ToolMessage

from backend.core.agents.context.artifact_store import ContextArtifactStore
from backend.core.agents.context.summary import (
    generate_summary,
    is_compaction_summary_text,
    mark_compaction_rewrite,
    normalize_text,
)
from backend.core.agents.context.types import (
    ContextCategory,
    ContextPack,
    EvidenceRecord,
)

from backend.core.agents.context.ledger_compile import (  # noqa: F401  (含兼容 re-export)
    DYNAMIC_CONTEXT_HEADER,
    _CATEGORY_CONFIG,
)
from backend.core.agents.context.ledger_evidence import (  # noqa: F401  (含兼容 re-export)
    _simple_terms,
    _token_overlap,
    context_item_to_record,
    extract_claim_summary,
    make_evidence_record,
    parse_multikb_text_evidence,
)
from backend.core.agents.context.ledger_tokens import (  # noqa: F401  (含兼容 re-export)
    LedgerBudget,
    TokenCounter,
    count_message_tokens,
    reset_shared_token_calibration,
)
from backend.core.agents.context.ledger_compile import (
    fit_messages as _impl_fit_messages,
    build_pack as _impl_build_pack,
    compile_request as _impl_compile_request,
    render_record as _impl_render_record,
    _read_raw_excerpt as _impl_read_raw_excerpt,
    _score as _impl_score,
    _deduplicate as _impl_deduplicate,
)

logger = logging.getLogger(__name__)

try:  # pragma: no cover - exercised when optional wheel is present.
    import tiktoken
except Exception:  # pragma: no cover
    tiktoken = None  # type: ignore[assignment]


class ContextLedger:
    """Select evidence by claim quality and token budget, then render one pack."""

    def __init__(
        self,
        *,
        model_name: str | None = None,
        budget: LedgerBudget | None = None,
        top_k: int = 24,
        dynamic_reserve_tokens: int = 0,
        artifact_store: ContextArtifactStore | None = None,
        raw_ref_char_limit: int = 4000,
        max_record_tokens: int = 2048,
        raw_excerpt_cache_entries: int | None = None,
        artifact_user_id: str | None = None,
        artifact_thread_id: str | None = None,
        enforce_artifact_scope: bool = False,
        token_counter: TokenCounter | None = None,
    ):
        self._counter = token_counter or TokenCounter(model_name)
        self._budget = budget or LedgerBudget()
        self._top_k = max(1, top_k)
        self._dynamic_reserve_tokens = max(0, int(dynamic_reserve_tokens or 0))
        self._artifact_store = artifact_store
        self._artifact_user_id = str(artifact_user_id or "").strip() or None
        self._artifact_thread_id = str(artifact_thread_id or "").strip() or None
        self._enforce_artifact_scope = bool(enforce_artifact_scope)
        self._raw_ref_char_limit = max(0, int(raw_ref_char_limit or 0))
        self._max_record_tokens = max(256, int(max_record_tokens or 2048))
        configured_cache_entries = (
            raw_excerpt_cache_entries
            if raw_excerpt_cache_entries is not None
            else os.getenv("AGENT_CONTEXT_RAW_EXCERPT_CACHE_ENTRIES", "128")
        )
        try:
            configured_cache_entries = int(configured_cache_entries)
        except (TypeError, ValueError):
            configured_cache_entries = 128
        self._raw_excerpt_cache_entries = max(1, min(configured_cache_entries, 4096))
        self._raw_excerpt_cache: OrderedDict[tuple[str, int], str] = OrderedDict()

    @property
    def token_budget(self) -> int:
        return self._budget.input_budget_tokens

    @property
    def history_token_budget(self) -> int:
        """Budget available before the reserved dynamic context slice."""

        return self._history_token_budget()

    def count_base_tokens(
        self,
        *,
        messages: list,
        system_content: object | None = None,
        request_overhead: object | None = None,
    ) -> int:
        return self._count_base_tokens(
            messages=messages,
            system_content=system_content,
            request_overhead=request_overhead,
        )

    @property
    def token_estimate_method(self) -> str:
        return self._counter.method

    def observe_actual_input_tokens(
        self,
        actual_tokens: int,
        *,
        estimated_tokens: int,
    ) -> None:
        self._counter.observe(
            estimated_tokens=max(0, int(estimated_tokens)),
            actual_tokens=max(0, int(actual_tokens)),
        )

    def note_provider_context_overflow(self) -> None:
        self._counter.note_context_overflow()

    # 实现在 ledger_compile.py(模块级函数,self 经 descriptor 绑定)
    fit_messages = _impl_fit_messages

    def _sum_message_tokens(self, messages: list) -> int:
        return sum(
            count_message_tokens(self._counter, message) for message in messages
        )

    @staticmethod
    def _is_compaction_summary(message: object) -> bool:
        if not isinstance(message, HumanMessage):
            return False
        if is_compaction_summary_text(getattr(message, "content", "")):
            return True
        additional_kwargs = getattr(message, "additional_kwargs", None)
        return isinstance(additional_kwargs, dict) and isinstance(
            additional_kwargs.get("context_compaction"),
            dict,
        )

    @staticmethod
    def _is_regular_user_message(message: object) -> bool:
        """Match HistoryCompactor's turn-boundary definition."""

        return isinstance(
            message, HumanMessage
        ) and not ContextLedger._is_compaction_summary(message)

    @staticmethod
    def _same_message(left: object, right: object) -> bool:
        if left is right:
            return True
        left_id = getattr(left, "id", None)
        return bool(left_id and left_id == getattr(right, "id", None))

    # 实现在 ledger_compile.py(模块级函数,self 经 descriptor 绑定)
    build_pack = _impl_build_pack

    # 实现在 ledger_compile.py(模块级函数,self 经 descriptor 绑定)
    compile_request = _impl_compile_request

    @staticmethod
    def _system_with_pack(
        system_content: str | None,
        pack: ContextPack,
    ) -> str | None:
        rendered = str(pack.get("rendered") or "")
        if not rendered:
            return system_content
        return f"{system_content or ''}{DYNAMIC_CONTEXT_HEADER}{rendered}"

    def _count_base_tokens(
        self,
        *,
        messages: list,
        system_content: object | None,
        request_overhead: object | None,
    ) -> int:
        total = self._counter.count(system_content) + self._counter.count(
            request_overhead
        )
        for message in messages:
            total += count_message_tokens(self._counter, message)
        return total

    @staticmethod
    def _history_groups(messages: list) -> list[list]:
        groups: list[list] = []
        current: list = []
        for message in messages:
            if ContextLedger._is_regular_user_message(message) and current:
                groups.append(current)
                current = [message]
            else:
                current.append(message)
        if current:
            groups.append(current)
        return groups

    @staticmethod
    def _compact_current_turn(messages: list) -> list:
        compacted: list = []
        for message in messages:
            content = normalize_text(getattr(message, "content", ""))
            if isinstance(message, ToolMessage) and len(content) > 800:
                # Keep a bounded head/tail excerpt: elevation may not have an
                # addressable copy for every tool (CONTEXT_OBJECT_MODE=off),
                # and the excerpt preserves conclusions/errors/citations.
                rewritten = (
                    "[Tool result truncated to satisfy the model input budget; "
                    "elevated evidence remains in the dynamic context pack.]\n"
                    + generate_summary(content, max_chars=400)
                )
                compacted.append(
                    mark_compaction_rewrite(
                        message.model_copy(update={"content": rewritten}),
                        reason="tool_result_elevated",
                        original_chars=len(content),
                        rewritten_chars=len(rewritten),
                    )
                )
            elif isinstance(message, AIMessage) and len(content) > 1200:
                rewritten = generate_summary(content, max_chars=800)
                compacted.append(
                    mark_compaction_rewrite(
                        message.model_copy(update={"content": rewritten}),
                        reason="long_assistant_summary",
                        original_chars=len(content),
                        rewritten_chars=len(rewritten),
                    )
                )
            else:
                compacted.append(message)
        return compacted

    # 实现在 ledger_compile.py(模块级函数,self 经 descriptor 绑定)
    render_record = _impl_render_record

    def _history_token_budget(self) -> int:
        if self._dynamic_reserve_tokens <= 0:
            return self.token_budget
        return max(1024, self.token_budget - self._dynamic_reserve_tokens)

    # 实现在 ledger_compile.py(模块级函数,self 经 descriptor 绑定)
    _read_raw_excerpt = _impl_read_raw_excerpt

    # 实现在 ledger_compile.py(模块级函数,self 经 descriptor 绑定)
    _score = _impl_score

    # 实现在 ledger_compile.py(模块级函数,self 经 descriptor 绑定)
    _deduplicate = _impl_deduplicate

    @staticmethod
    def _category(record: EvidenceRecord) -> ContextCategory:
        category = str(record.get("category") or "")
        if category in _CATEGORY_CONFIG:
            return category  # type: ignore[return-value]
        kind = str(record.get("kind") or "")
        if kind in {"retrieval", "retrieval_evidence"}:
            return "retrieval"
        if kind in {"skill_rule"}:
            return "skills"
        if kind in {"subagent_result"}:
            return "handoff"
        if kind in {"artifact_ref"}:
            return "artifact"
        return "tool_result"

    @staticmethod
    def _extract_latest_user_query(messages: list) -> str:
        for message in reversed(messages):
            if isinstance(message, HumanMessage):
                return normalize_text(message.content)
        return ""

    @staticmethod
    def _active_turn_id(messages: list) -> str:
        for index in range(len(messages) - 1, -1, -1):
            message = messages[index]
            if not ContextLedger._is_regular_user_message(message):
                continue
            message_id = str(getattr(message, "id", None) or "").strip()
            if message_id:
                return message_id
            content = normalize_text(message.content)
            if content:
                # Assign a durable id to legacy/direct messages that arrived
                # without one. Keeping it on the message makes the identifier
                # stable if history compaction changes the message position.
                generated = f"turn_{uuid.uuid4().hex[:16]}"
                try:
                    setattr(message, "id", generated)
                except (AttributeError, TypeError, ValueError):
                    pass
                else:
                    if str(getattr(message, "id", None) or "") == generated:
                        return generated
                # Immutable message implementations still get a position-aware
                # compatibility id, avoiding content-only collisions.
                digest = uuid.uuid5(
                    uuid.NAMESPACE_URL,
                    f"{index}:{content}",
                ).hex[:16]
                return f"turn_{digest}"
        return ""
