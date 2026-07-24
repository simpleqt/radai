"""Codex-style history compaction helpers."""

from __future__ import annotations

import asyncio
import logging
import time
import uuid
from dataclasses import dataclass
from typing import Any

from langchain_core.messages import AIMessage, HumanMessage, RemoveMessage, SystemMessage, ToolMessage
from langgraph.graph.message import REMOVE_ALL_MESSAGES

from backend.core.agents.context.ledger import TokenCounter
from backend.core.agents.context.summary import generate_summary, normalize_text
from backend.core.models import create_chat_model_async

logger = logging.getLogger(__name__)

SUMMARY_PREFIX = (
    "Another language model started to solve this problem and produced a summary "
    "of its work. Use this summary to continue without repeating prior steps:"
)


@dataclass(frozen=True)
class HistoryCompactionResult:
    window_id: str
    previous_window_id: str | None
    summary: str
    messages: list[Any]
    tokens_before: int
    tokens_after: int
    retained_user_messages: int
    removed_messages: int
    summary_tokens: int
    fixed_request_tokens: int
    threshold_tokens: int
    trigger: str
    created_at: float

    @property
    def model_messages(self) -> list[Any]:
        """Replacement history without reducer-only remove directives."""

        return [message for message in self.messages if not isinstance(message, RemoveMessage)]

    def state_payload(self, *, include_summary: bool = True) -> dict[str, Any]:
        payload: dict[str, Any] = {
            "window_id": self.window_id,
            "previous_window_id": self.previous_window_id,
            "trigger": self.trigger,
            "tokens_before": self.tokens_before,
            "tokens_after": self.tokens_after,
            "summary_tokens": self.summary_tokens,
            "fixed_request_tokens": self.fixed_request_tokens,
            "threshold_tokens": self.threshold_tokens,
            "retained_user_messages": self.retained_user_messages,
            "removed_messages": self.removed_messages,
            "created_at": self.created_at,
            "status": "completed",
        }
        if include_summary:
            payload["summary"] = self.summary
        return payload


@dataclass(frozen=True)
class HistoryCompactionAttempt:
    """Observable outcome for a compaction threshold check."""

    trigger: str
    tokens_before: int
    threshold_tokens: int
    fixed_request_tokens: int
    previous_window_id: str | None
    result: HistoryCompactionResult | None = None
    skip_reason: str | None = None
    created_at: float = 0.0

    @property
    def threshold_reached(self) -> bool:
        return self.tokens_before >= self.threshold_tokens


@dataclass(frozen=True)
class _RetainedHistory:
    messages: list[Any]
    start_index: int | None


class HistoryCompactor:
    """Build a replacement history from a handoff summary plus recent turns."""

    def __init__(
        self,
        *,
        model_name: str | None,
        trigger_tokens: int,
        retained_user_tokens: int,
        summary_input_chars: int,
        summary_output_chars: int,
        timeout_seconds: int,
        token_counter: TokenCounter | None = None,
    ):
        self._model_name = model_name
        self._counter = token_counter or TokenCounter(model_name)
        self._trigger_tokens = max(1024, int(trigger_tokens))
        self._retained_user_tokens = max(0, int(retained_user_tokens))
        self._summary_input_chars = max(2000, int(summary_input_chars))
        self._summary_output_chars = max(800, int(summary_output_chars))
        self._timeout_seconds = max(10, int(timeout_seconds))
        self._model: Any | None = None

    @property
    def trigger_tokens(self) -> int:
        return self._trigger_tokens

    def should_compact(
        self,
        messages: list[Any],
        *,
        additional_tokens: int = 0,
        threshold_tokens: int | None = None,
    ) -> bool:
        threshold = self._resolve_threshold(threshold_tokens)
        return (
            self.count_messages(messages) + max(0, int(additional_tokens))
            >= threshold
        )

    def _resolve_threshold(self, threshold_tokens: int | None) -> int:
        if threshold_tokens is None:
            return self._trigger_tokens
        return max(1024, min(self._trigger_tokens, int(threshold_tokens)))

    def count_messages(self, messages: list[Any]) -> int:
        total = 0
        for message in messages:
            total += self._counter.count(getattr(message, "content", message))
            total += self._counter.count(getattr(message, "tool_calls", None))
            total += self._counter.count(getattr(message, "additional_kwargs", None))
            total += self._counter.count(getattr(message, "name", None)) + 6
        return total

    async def acompact(
        self,
        messages: list[Any],
        *,
        previous_window_id: str | None = None,
        trigger: str = "auto",
        additional_tokens: int = 0,
        threshold_tokens: int | None = None,
    ) -> HistoryCompactionResult | None:
        attempt = await self.acompact_attempt(
            messages,
            previous_window_id=previous_window_id,
            trigger=trigger,
            additional_tokens=additional_tokens,
            threshold_tokens=threshold_tokens,
        )
        return attempt.result

    async def acompact_attempt(
        self,
        messages: list[Any],
        *,
        previous_window_id: str | None = None,
        trigger: str = "auto",
        additional_tokens: int = 0,
        threshold_tokens: int | None = None,
    ) -> HistoryCompactionAttempt:
        fixed_request_tokens = max(0, int(additional_tokens))
        tokens_before = self.count_messages(messages) + fixed_request_tokens
        threshold = self._resolve_threshold(threshold_tokens)
        created_at = time.time()
        if tokens_before < threshold:
            return HistoryCompactionAttempt(
                trigger=trigger,
                tokens_before=tokens_before,
                threshold_tokens=threshold,
                fixed_request_tokens=fixed_request_tokens,
                previous_window_id=previous_window_id,
                skip_reason="below_trigger",
                created_at=created_at,
            )
        retained = self._select_recent_turn_messages(messages)
        pruned = self._messages_before_retained(messages, retained)
        if not pruned:
            return HistoryCompactionAttempt(
                trigger=trigger,
                tokens_before=tokens_before,
                threshold_tokens=threshold,
                fixed_request_tokens=fixed_request_tokens,
                previous_window_id=previous_window_id,
                skip_reason="no_prunable_history",
                created_at=created_at,
            )
        summary = await self._summarize_pruned_messages(pruned)
        result = self._build_result(
            original=messages,
            retained=retained.messages,
            summary=summary,
            previous_window_id=previous_window_id,
            trigger=trigger,
            additional_tokens=fixed_request_tokens,
            threshold_tokens=threshold,
            created_at=created_at,
        )
        return HistoryCompactionAttempt(
            trigger=trigger,
            tokens_before=tokens_before,
            threshold_tokens=threshold,
            fixed_request_tokens=fixed_request_tokens,
            previous_window_id=previous_window_id,
            result=result,
            skip_reason=None if result is not None else "no_token_savings",
            created_at=created_at,
        )

    def compact_deterministic(
        self,
        messages: list[Any],
        *,
        previous_window_id: str | None = None,
        trigger: str = "auto",
        additional_tokens: int = 0,
        threshold_tokens: int | None = None,
    ) -> HistoryCompactionResult | None:
        attempt = self.compact_deterministic_attempt(
            messages,
            previous_window_id=previous_window_id,
            trigger=trigger,
            additional_tokens=additional_tokens,
            threshold_tokens=threshold_tokens,
        )
        return attempt.result

    def compact_deterministic_attempt(
        self,
        messages: list[Any],
        *,
        previous_window_id: str | None = None,
        trigger: str = "auto",
        additional_tokens: int = 0,
        threshold_tokens: int | None = None,
    ) -> HistoryCompactionAttempt:
        fixed_request_tokens = max(0, int(additional_tokens))
        tokens_before = self.count_messages(messages) + fixed_request_tokens
        threshold = self._resolve_threshold(threshold_tokens)
        created_at = time.time()
        if tokens_before < threshold:
            return HistoryCompactionAttempt(
                trigger=trigger,
                tokens_before=tokens_before,
                threshold_tokens=threshold,
                fixed_request_tokens=fixed_request_tokens,
                previous_window_id=previous_window_id,
                skip_reason="below_trigger",
                created_at=created_at,
            )
        retained = self._select_recent_turn_messages(messages)
        pruned = self._messages_before_retained(messages, retained)
        if not pruned:
            return HistoryCompactionAttempt(
                trigger=trigger,
                tokens_before=tokens_before,
                threshold_tokens=threshold,
                fixed_request_tokens=fixed_request_tokens,
                previous_window_id=previous_window_id,
                skip_reason="no_prunable_history",
                created_at=created_at,
            )
        summary = self._deterministic_summary(pruned)
        result = self._build_result(
            original=messages,
            retained=retained.messages,
            summary=summary,
            previous_window_id=previous_window_id,
            trigger=trigger,
            additional_tokens=fixed_request_tokens,
            threshold_tokens=threshold,
            created_at=created_at,
        )
        return HistoryCompactionAttempt(
            trigger=trigger,
            tokens_before=tokens_before,
            threshold_tokens=threshold,
            fixed_request_tokens=fixed_request_tokens,
            previous_window_id=previous_window_id,
            result=result,
            skip_reason=None if result is not None else "no_token_savings",
            created_at=created_at,
        )

    def _build_result(
        self,
        *,
        original: list[Any],
        retained: list[Any],
        summary: str,
        previous_window_id: str | None,
        trigger: str,
        additional_tokens: int,
        threshold_tokens: int,
        created_at: float,
    ) -> HistoryCompactionResult | None:
        window_id = f"ctxwin_{uuid.uuid4().hex[:12]}"
        fixed_request_tokens = max(0, int(additional_tokens))
        tokens_before = self.count_messages(original) + fixed_request_tokens
        retained_user_messages = sum(
            1 for message in retained if isinstance(message, HumanMessage)
        )
        removed_messages = max(0, len(original) - len(retained))
        summary_message: HumanMessage | None = None
        tokens_after = tokens_before
        max_char_candidates = list(
            dict.fromkeys(
                [self._summary_output_chars, 1200, 800, 500, 300, 180]
            )
        )
        for max_chars in max_char_candidates:
            if max_chars > self._summary_output_chars:
                continue
            candidate = self._make_summary_message(
                window_id=window_id,
                previous_window_id=previous_window_id,
                trigger=trigger,
                summary=summary,
                max_chars=max_chars,
            )
            summary_tokens = self._counter.count(candidate.content)
            measured_tokens = 0
            for _ in range(3):
                metadata = {
                    "window_id": window_id,
                    "previous_window_id": previous_window_id,
                    "trigger": trigger,
                    "tokens_before": tokens_before,
                    "tokens_after": measured_tokens,
                    "summary_tokens": summary_tokens,
                    "fixed_request_tokens": fixed_request_tokens,
                    "threshold_tokens": threshold_tokens,
                    "retained_user_messages": retained_user_messages,
                    "removed_messages": removed_messages,
                    "created_at": created_at,
                    "status": "completed",
                }
                candidate = candidate.model_copy(
                    update={
                        "additional_kwargs": {
                            **dict(candidate.additional_kwargs or {}),
                            "context_compaction": metadata,
                        }
                    }
                )
                updated_tokens = (
                    self.count_messages([candidate, *retained])
                    + fixed_request_tokens
                )
                if updated_tokens == measured_tokens:
                    break
                measured_tokens = updated_tokens
            summary_message = candidate
            tokens_after = measured_tokens
            if tokens_after < tokens_before:
                break
        if tokens_after >= tokens_before:
            logger.info(
                "[HistoryCompactor] skipped compaction with no token savings: before=%d after=%d",
                tokens_before,
                tokens_after,
            )
            return None
        assert summary_message is not None
        replacement_messages: list[Any] = [
            RemoveMessage(id=REMOVE_ALL_MESSAGES),
            summary_message,
            *retained,
        ]
        return HistoryCompactionResult(
            window_id=window_id,
            previous_window_id=previous_window_id,
            summary=str(summary_message.content),
            messages=replacement_messages,
            tokens_before=tokens_before,
            tokens_after=tokens_after,
            retained_user_messages=retained_user_messages,
            removed_messages=removed_messages,
            summary_tokens=self._counter.count(summary_message.content),
            fixed_request_tokens=fixed_request_tokens,
            threshold_tokens=threshold_tokens,
            trigger=trigger,
            created_at=created_at,
        )

    @staticmethod
    def _make_summary_message(
        *,
        window_id: str,
        previous_window_id: str | None,
        trigger: str,
        summary: str,
        max_chars: int,
    ) -> HumanMessage:
        return HumanMessage(
            content=f"{SUMMARY_PREFIX}\n{generate_summary(summary, max_chars=max_chars)}",
            id=f"compaction_summary_{window_id}",
            additional_kwargs={
                "context_compaction": {
                    "window_id": window_id,
                    "previous_window_id": previous_window_id,
                    "trigger": trigger,
                }
            },
        )

    def _select_recent_turn_messages(self, messages: list[Any]) -> _RetainedHistory:
        retained_groups: list[list[Any]] = []
        used = 0
        for _start_index, group in reversed(self._turn_groups(messages)):
            if not any(self._is_regular_user_message(message) for message in group):
                continue
            selected_group = group
            tokens = self.count_messages(selected_group)
            if used + tokens > self._retained_user_tokens:
                if not retained_groups and self._retained_user_tokens > 0:
                    selected_group = self._compact_retained_turn(group)
                    retained_groups.append(selected_group)
                break
            retained_groups.append(selected_group)
            used += tokens
        retained_groups.reverse()
        retained = [message for group in retained_groups for message in group]
        start_index = None
        if retained_groups:
            first_group = retained_groups[0]
            start_index = self._find_group_start(messages, first_group)
        return _RetainedHistory(messages=retained, start_index=start_index)

    @staticmethod
    def _messages_before_retained(messages: list[Any], retained: _RetainedHistory) -> list[Any]:
        if retained.start_index is None:
            return list(messages)
        return list(messages[: retained.start_index])

    def _turn_groups(self, messages: list[Any]) -> list[tuple[int, list[Any]]]:
        groups: list[tuple[int, list[Any]]] = []
        current: list[Any] = []
        current_start = 0
        for index, message in enumerate(messages):
            if self._is_regular_user_message(message):
                if current:
                    groups.append((current_start, current))
                current = [message]
                current_start = index
            elif current:
                current.append(message)
            else:
                groups.append((index, [message]))
        if current:
            groups.append((current_start, current))
        return groups

    @staticmethod
    def _is_regular_user_message(message: Any) -> bool:
        if not isinstance(message, HumanMessage):
            return False
        return not normalize_text(message.content).startswith(SUMMARY_PREFIX)

    def _compact_retained_turn(self, group: list[Any]) -> list[Any]:
        max_user_chars = max(500, self._retained_user_tokens * 4)
        compacted: list[Any] = []
        for message in group:
            content = normalize_text(getattr(message, "content", ""))
            if isinstance(message, HumanMessage) and len(content) > max_user_chars:
                compacted.append(
                    message.model_copy(
                        update={
                            "content": generate_summary(
                                content,
                                max_chars=max_user_chars,
                            )
                        }
                    )
                )
            elif isinstance(message, ToolMessage) and len(content) > 800:
                compacted.append(
                    message.model_copy(
                        update={
                            "content": (
                                "[Tool result content was omitted from retained history after "
                                "context compaction; use the dynamic context pack or raw_ref evidence "
                                "when available.]"
                            )
                        }
                    )
                )
            elif isinstance(message, AIMessage) and len(content) > 1200:
                compacted.append(
                    message.model_copy(
                        update={"content": generate_summary(content, max_chars=800)}
                    )
                )
            else:
                compacted.append(message)
        return compacted

    @staticmethod
    def _find_group_start(messages: list[Any], group: list[Any]) -> int | None:
        if not group:
            return None
        first = group[0]
        first_id = getattr(first, "id", None)
        for index, message in enumerate(messages):
            if message is first:
                return index
            if first_id and getattr(message, "id", None) == first_id:
                return index
        return None

    async def _summarize_pruned_messages(self, messages: list[Any]) -> str:
        # The compaction request has its own model context window.  Do not let
        # an 80K-character transcript overflow that request before the summary
        # model can even run; retain a bounded head/tail envelope instead.
        summary_input_limit = min(
            self._summary_input_chars,
            max(4000, self._trigger_tokens * 3),
        )
        transcript = generate_summary(
            "\n\n".join(self._render_message(message) for message in messages),
            max_chars=summary_input_limit,
        )
        prompt = (
            "You are performing a CONTEXT CHECKPOINT COMPACTION. Create a handoff summary "
            "for another LLM that will resume the task.\n\n"
            "Include:\n"
            "- Current progress and key decisions made\n"
            "- Important context, constraints, or user preferences\n"
            "- What remains to be done as clear next steps\n"
            "- Critical data, examples, files, artifact refs, and evidence refs needed to continue\n\n"
            "Be concise, structured, faithful to the transcript, and do not invent facts."
        )
        try:
            response = await asyncio.wait_for(
                (await self._get_model()).ainvoke(
                    [
                        SystemMessage(content=prompt),
                        HumanMessage(content=f"<conversation>\n{transcript}\n</conversation>"),
                    ]
                ),
                timeout=self._timeout_seconds,
            )
            summary = normalize_text(getattr(response, "content", response))
        except Exception:
            logger.warning("[HistoryCompactor] model compaction failed; using deterministic summary", exc_info=True)
            summary = self._deterministic_summary(messages)
        return generate_summary(summary, max_chars=self._summary_output_chars)

    def _deterministic_summary(self, messages: list[Any]) -> str:
        transcript = "\n\n".join(self._render_message(message) for message in messages)
        return (
            "Compacted conversation summary:\n"
            f"{generate_summary(transcript, max_chars=self._summary_output_chars)}"
        )

    async def _get_model(self) -> Any:
        if self._model is None:
            self._model = (await create_chat_model_async(
                name=self._model_name,
                thinking_enabled=False,
            )).with_config(
                tags=["history-compaction"],
                metadata={"purpose": "history_compaction"},
            )
        return self._model

    @staticmethod
    def _render_message(message: Any) -> str:
        role = "message"
        if isinstance(message, HumanMessage):
            role = "user"
        elif isinstance(message, AIMessage):
            role = "assistant"
        elif isinstance(message, SystemMessage):
            role = "system"
        elif isinstance(message, ToolMessage):
            role = f"tool:{message.name or 'unknown'}"
        content = normalize_text(getattr(message, "content", message))
        if isinstance(message, AIMessage) and getattr(message, "tool_calls", None):
            content = f"{content}\ntool_calls={normalize_text(message.tool_calls)}"
        return f"[{role}]\n{content}"
