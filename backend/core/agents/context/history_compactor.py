"""Codex-style history compaction helpers."""

from __future__ import annotations

import asyncio
import concurrent.futures
import logging
import threading
import time
import uuid
from dataclasses import dataclass
from typing import Any

from langchain_core.messages import AIMessage, HumanMessage, RemoveMessage, SystemMessage, ToolMessage
from langgraph.graph.message import REMOVE_ALL_MESSAGES

from backend.core.agents.context.ledger import TokenCounter, count_message_tokens
from backend.core.agents.context.summary import (
    SUMMARY_PREFIX,
    generate_summary,
    mark_compaction_rewrite,
    normalize_text,
)
from backend.core.models import create_chat_model_async

logger = logging.getLogger(__name__)

# Used only as a fixed-size placeholder while estimating the token cost of the
# self-describing compaction metadata.  Real token counters never approach it,
# so the resulting estimate is a safe upper bound instead of a fixed point.
_METADATA_TOKEN_UPPER_BOUND = 10**12

# Below this many prunable tokens a compaction cannot beat the cost of its own
# summary message plus metadata, so attempting one (with up to a full
# map-reduce of LLM calls) is guaranteed waste.
_MIN_PRUNABLE_TOKENS = 512


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
        retained_window_tokens: int,
        summary_input_chars: int,
        summary_output_chars: int,
        timeout_seconds: int,
        summary_max_chunks: int = 4,
        token_counter: TokenCounter | None = None,
    ):
        self._model_name = model_name
        self._counter = token_counter or TokenCounter(model_name)
        self._trigger_tokens = max(1024, int(trigger_tokens))
        self._retained_window_tokens = max(0, int(retained_window_tokens))
        self._summary_input_chars = max(2000, int(summary_input_chars))
        self._summary_output_chars = max(800, int(summary_output_chars))
        self._summary_max_chunks = max(1, min(8, int(summary_max_chunks)))
        self._timeout_seconds = max(10, int(timeout_seconds))
        self._model: Any | None = None
        self._model_future: concurrent.futures.Future[Any] | None = None
        self._model_guard = threading.Lock()
        # Signature of the last attempt that reached the threshold but saved
        # no tokens. Without it every subsequent model call re-ran the full
        # LLM map-reduce summary (up to summary_max_chunks calls plus the
        # compaction timeout) against the unchanged history, each round.
        self._no_savings_signature: tuple | None = None

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
        return sum(
            count_message_tokens(self._counter, message) for message in messages
        )

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
        signature = self._attempt_signature(
            messages, tokens_before - fixed_request_tokens
        )
        skip_reason = self._pre_summary_skip_reason(pruned, signature)
        if skip_reason is not None:
            return HistoryCompactionAttempt(
                trigger=trigger,
                tokens_before=tokens_before,
                threshold_tokens=threshold,
                fixed_request_tokens=fixed_request_tokens,
                previous_window_id=previous_window_id,
                skip_reason=skip_reason,
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
        self._no_savings_signature = None if result is not None else signature
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
        signature = self._attempt_signature(
            messages, tokens_before - fixed_request_tokens
        )
        skip_reason = self._pre_summary_skip_reason(pruned, signature)
        if skip_reason is not None:
            return HistoryCompactionAttempt(
                trigger=trigger,
                tokens_before=tokens_before,
                threshold_tokens=threshold,
                fixed_request_tokens=fixed_request_tokens,
                previous_window_id=previous_window_id,
                skip_reason=skip_reason,
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
        self._no_savings_signature = None if result is not None else signature
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

    @staticmethod
    def _attempt_signature(messages: list[Any], message_tokens: int) -> tuple:
        """Identity of one compaction candidate history for cooldown checks."""

        last_id = ""
        if messages:
            last_id = str(getattr(messages[-1], "id", "") or "")
        return (len(messages), last_id, int(message_tokens))

    @classmethod
    def _is_summary_envelope(cls, message: Any) -> bool:
        return isinstance(message, HumanMessage) and not cls._is_regular_user_message(
            message
        )

    def _pre_summary_skip_reason(
        self,
        pruned: list[Any],
        signature: tuple,
    ) -> str | None:
        """Refuse attempts that are guaranteed (or already proven) fruitless.

        Without these gates a request whose fixed system/tool tokens alone
        exceed the trigger re-ran the full LLM summary on every model call:
        either against unchanged history (cooldown), against a prunable slice
        too small to beat the summary's own cost (floor), or against nothing
        but the previous window's summary envelope (re-summarizing a summary
        capped at the same output size cannot shrink it).
        """

        if signature == self._no_savings_signature:
            return "no_token_savings_cooldown"
        pruned_tokens = self.count_messages(pruned)
        # Scale the floor down for small configured triggers (tests, miniature
        # deployments) so the gate never blocks a legitimately tiny setup.
        floor = min(_MIN_PRUNABLE_TOKENS, max(128, self._trigger_tokens // 8))
        if pruned_tokens < floor:
            return "prunable_below_floor"
        if all(self._is_summary_envelope(message) for message in pruned):
            return "only_summary_prunable"
        return None

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
        def metadata_payload(*, tokens_after: int, summary_tokens: int) -> dict[str, Any]:
            return {
                "window_id": window_id,
                "previous_window_id": previous_window_id,
                "trigger": trigger,
                "tokens_before": tokens_before,
                "tokens_after": tokens_after,
                "summary_tokens": summary_tokens,
                "fixed_request_tokens": fixed_request_tokens,
                "threshold_tokens": threshold_tokens,
                "retained_user_messages": retained_user_messages,
                "removed_messages": removed_messages,
                "created_at": created_at,
                "status": "completed",
            }

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
            # Estimate the self-describing metadata with a fixed-size upper
            # bound instead of iterating a fixed point that depends on the
            # very token count stored inside the metadata.
            metadata = metadata_payload(
                tokens_after=_METADATA_TOKEN_UPPER_BOUND,
                summary_tokens=summary_tokens,
            )
            metadata_tokens = self._counter.count(
                {"context_compaction": metadata}
            )
            base_tokens = (
                self._counter.count(candidate.content)
                + self._counter.count(getattr(candidate, "tool_calls", None))
                + self._counter.count(getattr(candidate, "name", None))
                + 6
                + self.count_messages(retained)
                + fixed_request_tokens
            )
            tokens_after = base_tokens + metadata_tokens
            metadata["tokens_after"] = tokens_after
            candidate = candidate.model_copy(
                update={
                    "additional_kwargs": {
                        "context_compaction": metadata,
                    }
                }
            )
            actual_tokens = (
                self.count_messages([candidate, *retained])
                + fixed_request_tokens
            )
            if actual_tokens > tokens_after:
                # Defensive widening; the placeholder upper bound should
                # already make the estimate conservative.
                tokens_after = actual_tokens + 32
                metadata["tokens_after"] = tokens_after
                candidate = candidate.model_copy(
                    update={
                        "additional_kwargs": {
                            "context_compaction": metadata,
                        }
                    }
                )
            summary_message = candidate
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
            if used + tokens > self._retained_window_tokens:
                if not retained_groups and self._retained_window_tokens > 0:
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
        max_user_chars = max(500, self._retained_window_tokens * 4)
        compacted: list[Any] = []
        for message in group:
            content = normalize_text(getattr(message, "content", ""))
            if isinstance(message, HumanMessage) and len(content) > max_user_chars:
                rewritten = generate_summary(
                    content,
                    max_chars=max_user_chars,
                )
                compacted.append(
                    mark_compaction_rewrite(
                        message.model_copy(update={"content": rewritten}),
                        reason="long_user_summary",
                        original_chars=len(content),
                        rewritten_chars=len(rewritten),
                    )
                )
            elif isinstance(message, ToolMessage) and len(content) > 800:
                # Keep a bounded head/tail excerpt: with CONTEXT_OBJECT_MODE=off
                # a fully omitted body may have no addressable copy anywhere,
                # while the excerpt still preserves conclusions/errors/citations.
                rewritten = (
                    "[Tool result truncated after context compaction; use the "
                    "dynamic context pack or raw_ref evidence for the full text "
                    "when available.]\n"
                    + generate_summary(content, max_chars=400)
                )
                compacted.append(
                    mark_compaction_rewrite(
                        message.model_copy(update={"content": rewritten}),
                        reason="tool_result_omitted",
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

    _CHECKPOINT_PROMPT = (
        "You are performing a CONTEXT CHECKPOINT COMPACTION. Create a handoff summary "
        "for another LLM that will resume the task.\n\n"
        "Include:\n"
        "- Current progress and key decisions made\n"
        "- Important context, constraints, or user preferences\n"
        "- What remains to be done as clear next steps\n"
        "- Critical data, examples, files, artifact refs, and evidence refs needed to continue\n\n"
        "Be concise, structured, faithful to the transcript, and do not invent facts."
    )

    _CHUNK_PROMPT = (
        "You are summarizing ONE CONTIGUOUS PART of an older conversation transcript "
        "for a context checkpoint. Other parts are summarized separately and merged "
        "later.\n\n"
        "Extract only what a resuming LLM needs: progress, key decisions, user "
        "constraints/preferences, task state, and critical data, files, artifact refs "
        "or evidence refs. Be concise, faithful to this part, and do not invent facts."
    )

    async def _summarize_pruned_messages(self, messages: list[Any]) -> str:
        # The compaction request has its own model context window.  Do not let
        # an 80K-character transcript overflow that request before the summary
        # model can even run; each model call receives at most this many chars.
        summary_input_limit = min(
            self._summary_input_chars,
            max(4000, self._trigger_tokens * 3),
        )
        transcript = "\n\n".join(
            self._render_message(message) for message in messages
        )
        try:
            if (
                len(transcript) <= summary_input_limit
                or self._summary_max_chunks <= 1
            ):
                # Small window: one call, head/tail-bounded like before.
                summary = await self._invoke_summary(
                    self._CHECKPOINT_PROMPT,
                    "<conversation>\n"
                    + generate_summary(transcript, max_chars=summary_input_limit)
                    + "\n</conversation>",
                )
                if not summary:
                    raise RuntimeError("empty compaction summary")
            else:
                # Map-reduce: partition the FULL transcript into contiguous
                # ranges so the middle of a long conversation also reaches the
                # summary model instead of being head/tail-truncated away.
                summary = await self._map_reduce_summary(
                    transcript,
                    summary_input_limit,
                )
        except Exception:
            logger.warning(
                "[HistoryCompactor] model compaction failed; using deterministic summary",
                exc_info=True,
            )
            summary = self._deterministic_summary(messages)
        return generate_summary(summary, max_chars=self._summary_output_chars)

    @staticmethod
    def _partition_transcript(
        transcript: str,
        chunk_char_limit: int,
        max_chunks: int,
    ) -> list[str]:
        """Split into at most ``max_chunks`` contiguous ranges covering all text.

        Cuts prefer the ``\\n\\n`` separator between rendered messages, then
        fall back to the nearest earlier newline, so a message (and the facts
        inside it) is not split mid-line whenever the transcript has that
        structure. When a range still exceeds the per-call limit, it is
        head/tail-compressed, so each region of the conversation contributes
        to the checkpoint.
        """

        total = len(transcript)
        limit = max(2000, int(chunk_char_limit))
        count = max(1, min(int(max_chunks), -(-total // limit)))
        span = -(-total // count)
        chunks: list[str] = []
        offset = 0
        for index in range(count):
            if index == count - 1:
                end = total
            else:
                end = min(total, offset + span)
                if end < total:
                    window_start = min(offset + max(1, int(span * 0.6)), end)
                    separator = transcript.rfind("\n\n", window_start, end)
                    if separator > offset:
                        end = separator + 2
                    else:
                        newline = transcript.rfind("\n", window_start, end)
                        if newline > offset:
                            end = newline + 1
            piece = transcript[offset:end]
            offset = end
            if not piece:
                continue
            if len(piece) > limit:
                piece = generate_summary(piece, max_chars=limit)
            chunks.append(piece)
            if offset >= total:
                break
        return chunks

    async def _map_reduce_summary(
        self,
        transcript: str,
        summary_input_limit: int,
    ) -> str:
        chunks = self._partition_transcript(
            transcript,
            summary_input_limit,
            self._summary_max_chunks,
        )
        if len(chunks) == 1:
            summary = await self._invoke_summary(
                self._CHECKPOINT_PROMPT,
                f"<conversation>\n{chunks[0]}\n</conversation>",
            )
            if not summary:
                raise RuntimeError("empty compaction summary")
            return summary
        total = len(chunks)
        partials = await asyncio.gather(
            *(
                self._summarize_chunk(index, total, chunk)
                for index, chunk in enumerate(chunks)
            )
        )
        merged_input = "\n\n".join(
            f"[part {index + 1}/{total}]\n{partial}"
            for index, partial in enumerate(partials)
            if partial
        )
        merged_input = generate_summary(merged_input, max_chars=summary_input_limit)
        reduce_prompt = (
            self._CHECKPOINT_PROMPT
            + "\n\nThe input below is a sequence of partial summaries covering the "
            "conversation in order. Merge them into one coherent handoff summary "
            "and remove duplicates."
        )
        try:
            reduced = await self._invoke_summary(
                reduce_prompt,
                f"<partial_summaries>\n{merged_input}\n</partial_summaries>",
            )
        except Exception:
            # A reduce timeout/error must not discard the successful map
            # phase: hand off the ordered partials instead of letting the
            # exception fall back to the head/tail extract of the transcript.
            logger.warning(
                "[HistoryCompactor] reduce summary failed; handing off "
                "ordered partial summaries",
                exc_info=True,
            )
            reduced = ""
        if reduced:
            return reduced
        # An empty reduce still hands off the ordered partials instead of
        # falling all the way back to the head/tail extract.
        return merged_input

    async def _summarize_chunk(self, index: int, total: int, chunk: str) -> str:
        try:
            partial = await self._invoke_summary(
                self._CHUNK_PROMPT,
                f"<conversation_part index=\"{index + 1}\" total=\"{total}\">\n"
                f"{chunk}\n</conversation_part>",
            )
        except Exception:
            logger.warning(
                "[HistoryCompactor] chunk summary failed (part %d/%d); "
                "keeping a deterministic excerpt",
                index + 1,
                total,
                exc_info=True,
            )
            partial = ""
        if partial:
            return generate_summary(partial, max_chars=2400)
        return generate_summary(chunk, max_chars=1200)

    async def _invoke_summary(self, system_prompt: str, user_content: str) -> str:
        async def invoke_summary_model() -> Any:
            model = await self._get_model()
            return await model.ainvoke(
                [
                    SystemMessage(content=system_prompt),
                    HumanMessage(content=user_content),
                ]
            )

        response = await asyncio.wait_for(
            invoke_summary_model(),
            timeout=self._timeout_seconds,
        )
        return normalize_text(getattr(response, "content", response))

    def _deterministic_summary(self, messages: list[Any]) -> str:
        transcript = "\n\n".join(self._render_message(message) for message in messages)
        return (
            "Compacted conversation summary:\n"
            f"{generate_summary(transcript, max_chars=self._summary_output_chars)}"
        )

    async def _get_model(self) -> Any:
        with self._model_guard:
            if self._model is not None:
                return self._model
            pending = self._model_future
            is_owner = pending is None
            if pending is None:
                pending = concurrent.futures.Future()
                self._model_future = pending

        if not is_owner:
            return await asyncio.shield(asyncio.wrap_future(pending))

        try:
            model = (await create_chat_model_async(
                name=self._model_name,
                thinking_enabled=False,
            )).with_config(
                tags=["history-compaction"],
                metadata={"purpose": "history_compaction"},
            )
        except BaseException as exc:
            with self._model_guard:
                if self._model_future is pending:
                    self._model_future = None
            pending.set_exception(exc)
            raise

        with self._model_guard:
            if self._model is None:
                self._model = model
            resolved_model = self._model
            if self._model_future is pending:
                self._model_future = None
        pending.set_result(resolved_model)
        return resolved_model

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
