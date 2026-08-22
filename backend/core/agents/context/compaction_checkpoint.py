"""Bind history compaction records to LangGraph checkpoints.

LangGraph remains the only writer of graph state.  This module enriches a
compaction update before that state is checkpointed and provides one recovery
path for both the enriched format and checkpoints produced before the binding
metadata existed.
"""

from __future__ import annotations

import hashlib
import json
from collections.abc import Mapping
from dataclasses import dataclass, replace
from typing import Any

from langchain_core.messages import BaseMessage

from backend.core.agents.context.history_compactor import HistoryCompactionResult

CHECKPOINT_REF_KEY = "checkpoint_ref"
CHECKPOINT_REF_SCHEMA_VERSION = 1


class CompactionCheckpointError(RuntimeError):
    """Base error raised by the compaction/checkpoint boundary."""


class CompactionCheckpointUnavailable(CompactionCheckpointError):
    """Raised when recovery is requested without a durable checkpointer."""


class CompactionCheckpointIntegrityError(CompactionCheckpointError):
    """Raised when checkpoint metadata does not match its thread or summary."""


@dataclass(frozen=True)
class CompactionCheckpointReference:
    """Logical and physical address of one compacted context window."""

    thread_id: str
    checkpoint_ns: str
    window_id: str
    logical_id: str
    summary_sha256: str
    durable: bool
    checkpoint_id: str | None = None
    schema_version: int = CHECKPOINT_REF_SCHEMA_VERSION
    storage: str = "langgraph"

    def as_dict(self) -> dict[str, Any]:
        payload: dict[str, Any] = {
            "schema_version": self.schema_version,
            "storage": self.storage,
            "thread_id": self.thread_id,
            "checkpoint_ns": self.checkpoint_ns,
            "window_id": self.window_id,
            "logical_id": self.logical_id,
            "summary_sha256": self.summary_sha256,
            "durable": self.durable,
        }
        if self.checkpoint_id:
            payload["checkpoint_id"] = self.checkpoint_id
        return payload

    @classmethod
    def from_dict(cls, value: Mapping[str, Any]) -> CompactionCheckpointReference:
        thread_id = str(value.get("thread_id") or "").strip()
        window_id = str(value.get("window_id") or "").strip()
        logical_id = str(value.get("logical_id") or "").strip()
        summary_sha256 = str(value.get("summary_sha256") or "").strip()
        if not all((thread_id, window_id, logical_id, summary_sha256)):
            raise CompactionCheckpointIntegrityError(
                "compaction checkpoint reference is incomplete"
            )
        return cls(
            thread_id=thread_id,
            checkpoint_ns=str(value.get("checkpoint_ns") or ""),
            window_id=window_id,
            logical_id=logical_id,
            summary_sha256=summary_sha256,
            durable=bool(value.get("durable", True)),
            checkpoint_id=str(value.get("checkpoint_id") or "").strip() or None,
            schema_version=int(
                value.get("schema_version") or CHECKPOINT_REF_SCHEMA_VERSION
            ),
            storage=str(value.get("storage") or "langgraph"),
        )


@dataclass(frozen=True)
class BoundHistoryCompaction:
    """Compaction values ready to be committed by the graph checkpointer."""

    messages: list[Any]
    state_payload: dict[str, Any]
    reference: CompactionCheckpointReference | None


@dataclass(frozen=True)
class RecoveredHistoryCompaction:
    """Validated compaction state read from a LangGraph checkpoint."""

    messages: list[Any]
    state_payload: dict[str, Any]
    reference: CompactionCheckpointReference
    checkpoint_config: dict[str, Any]
    checkpoint_metadata: dict[str, Any]
    legacy: bool = False

    def state_updates(self) -> dict[str, Any]:
        """Return reducer-friendly values for an explicit state re-hydration."""

        updates: dict[str, Any] = {
            "context_window_id": self.reference.window_id,
            "context_compaction": dict(self.state_payload),
            "context_checkpoint_ref": self.reference.as_dict(),
            "context_compaction_history": [
                _history_entry(self.state_payload)
            ],
        }
        if self.messages:
            updates["messages"] = list(self.messages)
        return updates


class CompactionCheckpointCoordinator:
    """Unify compaction metadata with LangGraph checkpoint addressing.

    ``bind`` is intentionally synchronous and has no storage side effects.  A
    graph writes the returned messages and state payload atomically through its
    configured checkpointer.  ``arecover`` reads that same checkpoint contract,
    including legacy compactions that have no explicit reference yet.
    """

    def __init__(
        self,
        *,
        checkpointer: Any | None = None,
        thread_id: str | None = None,
        checkpoint_ns: str = "",
    ) -> None:
        self._checkpointer = checkpointer
        self._thread_id = str(thread_id or "").strip() or None
        self._checkpoint_ns = str(checkpoint_ns or "")

    @property
    def thread_id(self) -> str | None:
        return self._thread_id

    @property
    def checkpoint_ns(self) -> str:
        return self._checkpoint_ns

    @property
    def durable(self) -> bool:
        return self._checkpointer is not None

    def checkpoint_config(
        self,
        *,
        thread_id: str | None = None,
        checkpoint_id: str | None = None,
    ) -> dict[str, Any]:
        resolved_thread_id = self._resolve_thread_id(thread_id=thread_id)
        configurable: dict[str, Any] = {"thread_id": resolved_thread_id}
        if self._checkpoint_ns:
            configurable["checkpoint_ns"] = self._checkpoint_ns
        if checkpoint_id:
            configurable["checkpoint_id"] = str(checkpoint_id)
        return {"configurable": configurable}

    def bind(
        self,
        result: HistoryCompactionResult,
        *,
        state: Mapping[str, Any] | None = None,
    ) -> BoundHistoryCompaction:
        """Attach a stable idempotency key and thread address to a result."""

        payload = result.state_payload()
        messages = list(result.messages)
        thread_id = self._thread_id or _thread_id_from_state(state)
        if not thread_id:
            return BoundHistoryCompaction(
                messages=messages,
                state_payload=payload,
                reference=None,
            )

        reference = self._make_reference(
            thread_id=thread_id,
            window_id=result.window_id,
            summary=result.summary,
        )
        payload[CHECKPOINT_REF_KEY] = reference.as_dict()
        messages = _bind_message_metadata(
            messages,
            window_id=result.window_id,
            payload=payload,
        )
        return BoundHistoryCompaction(
            messages=messages,
            state_payload=payload,
            reference=reference,
        )

    async def arecover(
        self,
        *,
        thread_id: str | None = None,
        checkpoint_id: str | None = None,
    ) -> RecoveredHistoryCompaction | None:
        """Read and validate the latest or a specified physical checkpoint."""

        if self._checkpointer is None:
            raise CompactionCheckpointUnavailable(
                "compaction recovery requires a configured LangGraph checkpointer"
            )
        resolved_thread_id = self._resolve_thread_id(thread_id=thread_id)
        requested_config = self.checkpoint_config(
            thread_id=resolved_thread_id,
            checkpoint_id=checkpoint_id,
        )
        checkpoint, resolved_config, metadata = await self._read_checkpoint(
            requested_config
        )
        if checkpoint is None:
            return None

        channel_values = checkpoint.get("channel_values") or {}
        if not isinstance(channel_values, Mapping):
            raise CompactionCheckpointIntegrityError(
                "checkpoint channel_values must be a mapping"
            )
        messages = list(channel_values.get("messages") or [])
        payload = _find_compaction_payload(channel_values, messages)
        if payload is None:
            return None

        raw_reference = payload.get(CHECKPOINT_REF_KEY)
        legacy = not isinstance(raw_reference, Mapping)
        summary = _find_compaction_summary(payload, messages)
        if summary and not payload.get("summary"):
            payload["summary"] = summary
        if isinstance(raw_reference, Mapping):
            reference = CompactionCheckpointReference.from_dict(raw_reference)
            self._validate_reference(
                reference,
                requested_thread_id=resolved_thread_id,
                payload=payload,
                summary=summary,
            )
        else:
            window_id = str(payload.get("window_id") or "").strip()
            if not window_id:
                raise CompactionCheckpointIntegrityError(
                    "legacy compaction checkpoint has no window_id"
                )
            reference = self._make_reference(
                thread_id=resolved_thread_id,
                window_id=window_id,
                summary=summary,
            )
            payload[CHECKPOINT_REF_KEY] = reference.as_dict()

        physical_checkpoint_id = str(
            (resolved_config.get("configurable") or {}).get("checkpoint_id") or ""
        ).strip()
        if physical_checkpoint_id:
            reference = replace(reference, checkpoint_id=physical_checkpoint_id)
        payload[CHECKPOINT_REF_KEY] = reference.as_dict()
        messages = _bind_message_metadata(
            messages,
            window_id=reference.window_id,
            payload=payload,
        )
        return RecoveredHistoryCompaction(
            messages=messages,
            state_payload=payload,
            reference=reference,
            checkpoint_config=resolved_config,
            checkpoint_metadata=metadata,
            legacy=legacy,
        )

    def _resolve_thread_id(self, *, thread_id: str | None) -> str:
        resolved = str(thread_id or self._thread_id or "").strip()
        if not resolved:
            raise ValueError("checkpoint thread_id is required")
        return resolved

    def _make_reference(
        self,
        *,
        thread_id: str,
        window_id: str,
        summary: str,
    ) -> CompactionCheckpointReference:
        summary_sha256 = _sha256(summary)
        identity = json.dumps(
            {
                "schema_version": CHECKPOINT_REF_SCHEMA_VERSION,
                "thread_id": thread_id,
                "checkpoint_ns": self._checkpoint_ns,
                "window_id": window_id,
                "summary_sha256": summary_sha256,
            },
            ensure_ascii=False,
            sort_keys=True,
            separators=(",", ":"),
        )
        logical_id = f"cmp_{_sha256(identity)[:24]}"
        return CompactionCheckpointReference(
            thread_id=thread_id,
            checkpoint_ns=self._checkpoint_ns,
            window_id=window_id,
            logical_id=logical_id,
            summary_sha256=summary_sha256,
            durable=self.durable,
        )

    async def _read_checkpoint(
        self,
        config: dict[str, Any],
    ) -> tuple[Mapping[str, Any] | None, dict[str, Any], dict[str, Any]]:
        tuple_getter = getattr(self._checkpointer, "aget_tuple", None)
        if callable(tuple_getter):
            checkpoint_tuple = await tuple_getter(config)
            if checkpoint_tuple is None:
                return None, config, {}
            return (
                checkpoint_tuple.checkpoint,
                dict(checkpoint_tuple.config or config),
                dict(checkpoint_tuple.metadata or {}),
            )

        getter = getattr(self._checkpointer, "aget", None)
        if not callable(getter):
            raise CompactionCheckpointUnavailable(
                "configured checkpointer does not implement aget_tuple or aget"
            )
        checkpoint = await getter(config)
        return checkpoint, config, {}

    @staticmethod
    def _validate_reference(
        reference: CompactionCheckpointReference,
        *,
        requested_thread_id: str,
        payload: Mapping[str, Any],
        summary: str,
    ) -> None:
        if reference.thread_id != requested_thread_id:
            raise CompactionCheckpointIntegrityError(
                "compaction checkpoint thread_id does not match requested thread"
            )
        if reference.window_id != str(payload.get("window_id") or "").strip():
            raise CompactionCheckpointIntegrityError(
                "compaction checkpoint window_id does not match state payload"
            )
        if reference.summary_sha256 != _sha256(summary):
            raise CompactionCheckpointIntegrityError(
                "compaction checkpoint summary digest mismatch"
            )


def _thread_id_from_state(state: Mapping[str, Any] | None) -> str | None:
    if not isinstance(state, Mapping):
        return None
    existing_ref = state.get("context_checkpoint_ref")
    if isinstance(existing_ref, Mapping):
        thread_id = str(existing_ref.get("thread_id") or "").strip()
        if thread_id:
            return thread_id
    thread_data = state.get("thread_data")
    if isinstance(thread_data, Mapping):
        for key in ("thread_id", "internal_thread_id"):
            thread_id = str(thread_data.get(key) or "").strip()
            if thread_id:
                return thread_id
    trace_context = state.get("trace_context")
    if isinstance(trace_context, Mapping):
        thread_id = str(trace_context.get("thread_id") or "").strip()
        if thread_id:
            return thread_id
    return None


def _find_compaction_payload(
    channel_values: Mapping[str, Any],
    messages: list[Any],
) -> dict[str, Any] | None:
    payload = channel_values.get("context_compaction")
    if isinstance(payload, Mapping) and payload.get("window_id"):
        return dict(payload)
    for message in reversed(messages):
        metadata = getattr(message, "additional_kwargs", None)
        if isinstance(message, Mapping):
            metadata = message.get("additional_kwargs") or message.get("metadata")
        if not isinstance(metadata, Mapping):
            continue
        payload = metadata.get("context_compaction")
        if isinstance(payload, Mapping) and payload.get("window_id"):
            return dict(payload)
    return None


def _find_compaction_summary(payload: Mapping[str, Any], messages: list[Any]) -> str:
    summary = str(payload.get("summary") or "")
    if summary:
        return summary
    window_id = str(payload.get("window_id") or "").strip()
    for message in reversed(messages):
        metadata = getattr(message, "additional_kwargs", None)
        content = getattr(message, "content", "")
        if isinstance(message, Mapping):
            metadata = message.get("additional_kwargs") or message.get("metadata")
            content = message.get("content", "")
        if not isinstance(metadata, Mapping):
            continue
        compaction = metadata.get("context_compaction")
        if (
            isinstance(compaction, Mapping)
            and str(compaction.get("window_id") or "").strip() == window_id
        ):
            return str(content or "")
    return ""


def _bind_message_metadata(
    messages: list[Any],
    *,
    window_id: str,
    payload: Mapping[str, Any],
) -> list[Any]:
    # The summary message content already IS the summary text. Copying the
    # full summary into additional_kwargs doubled its token cost on every
    # later count (count_message_tokens charges kwargs) and doubled the
    # checkpoint footprint, while the compaction-benefit check in
    # HistoryCompactor._build_result never saw that extra weight. Recovery
    # paths re-derive the summary from message content when the payload
    # lacks it (_find_compaction_summary / _restore_compaction_state).
    message_payload = {
        key: value for key, value in payload.items() if key != "summary"
    }
    bound: list[Any] = []
    for message in messages:
        metadata = getattr(message, "additional_kwargs", None)
        if not isinstance(message, BaseMessage) or not isinstance(metadata, Mapping):
            bound.append(message)
            continue
        compaction = metadata.get("context_compaction")
        if not isinstance(compaction, Mapping) or str(
            compaction.get("window_id") or ""
        ).strip() != window_id:
            bound.append(message)
            continue
        bound.append(
            message.model_copy(
                update={
                    "additional_kwargs": {
                        **dict(metadata),
                        "context_compaction": dict(message_payload),
                    }
                }
            )
        )
    return bound


def _history_entry(compaction_state: Mapping[str, Any]) -> dict[str, Any]:
    summary = str(compaction_state.get("summary") or "")
    compact = {
        key: value
        for key, value in compaction_state.items()
        if key != "summary"
    }
    if summary:
        compact["summary_preview"] = summary[:1000]
        compact["summary_sha256"] = _sha256(summary)
    return compact


def _sha256(value: str) -> str:
    return hashlib.sha256(value.encode("utf-8", errors="ignore")).hexdigest()
