"""Internal agent event contract.

AgentEvent is intentionally transport-agnostic. SSE encoding is handled in
the application/event_stream layer.
"""

from __future__ import annotations

import json
import uuid
from collections.abc import Mapping
from dataclasses import dataclass, field
from datetime import datetime, timezone
from typing import Any

EVENT_ENVELOPE_SCHEMA_VERSION = "2.0"
DEFAULT_EVENT_PRODUCER = "agent-runtime"


def _utc_now_iso() -> str:
    return datetime.now(timezone.utc).isoformat()


def _optional_text(value: object) -> str | None:
    normalized = str(value or "").strip()
    return normalized or None


def _deterministic_event_id(
    *,
    event_type: str,
    request_id: str,
    thread_id: str,
    sequence: int,
    producer: str,
    unit_id: str | None = None,
) -> str:
    """Build a stable identity for retries of the same logical event."""

    identity = json.dumps(
        {
            "producer": producer,
            "request_id": request_id,
            "sequence": sequence,
            "thread_id": thread_id,
            "type": event_type,
            "unit_id": unit_id,
        },
        ensure_ascii=True,
        separators=(",", ":"),
        sort_keys=True,
    )
    return f"evt_{uuid.uuid5(uuid.NAMESPACE_URL, identity).hex}"


@dataclass(frozen=True)
class AgentEvent:
    type: str
    request_id: str
    thread_id: str
    sequence: int
    payload: dict[str, Any] = field(default_factory=dict)
    created_at: str = field(default_factory=lambda: datetime.now(timezone.utc).isoformat())

    @classmethod
    def from_runtime_payload(
        cls,
        raw: dict[str, Any],
        *,
        request_id: str,
        thread_id: str,
        sequence: int,
    ) -> "AgentEvent":
        event_type, payload = cls._standardize_runtime_payload(raw)
        return cls(
            type=event_type,
            request_id=request_id,
            thread_id=thread_id,
            sequence=sequence,
            payload=payload,
        )

    @classmethod
    def _standardize_runtime_payload(
        cls,
        raw: dict[str, Any],
    ) -> tuple[str, dict[str, Any]]:
        raw_type = str(raw.get("type") or "event")
        event_type = {
            "chunk": "message.delta",
            "thinking": "reasoning.delta",
            "status": "workflow.step",
            "event": "workflow.step",
            "error": "error",
            "follow_up_suggestions": "follow_up.created",
            "draft_chunk": "research.draft.delta",
        }.get(raw_type, raw_type)
        if event_type == raw_type and "." in raw_type and isinstance(raw.get("payload"), dict):
            return raw_type, dict(raw["payload"])
        if raw_type in _TASK_RUNTIME_EVENT_TYPES:
            return _TASK_RUNTIME_EVENT_TYPES[raw_type], cls._payload_from_task_runtime(raw)
        return event_type, cls._payload_from_runtime_shape(raw, raw_type)

    @staticmethod
    def _payload_from_runtime_shape(raw: dict[str, Any], raw_type: str) -> dict[str, Any]:
        if raw_type in {"chunk", "thinking", "draft_chunk"}:
            return {"content": str(raw.get("content") or "")}
        if raw_type == "status":
            content = raw.get("content", raw.get("status", ""))
            return {"content": str(content or ""), "status": str(raw.get("status", content) or "")}
        if raw_type == "event":
            event = raw.get("event") if isinstance(raw.get("event"), dict) else {}
            return {"event": event}
        if raw_type == "error":
            message = str(raw.get("message", raw.get("error", "")) or "")
            return {"message": message, "error": message}
        if raw_type == "follow_up_suggestions":
            return {
                "suggestions": list(raw.get("suggestions") or []),
                "metadata": dict(raw.get("metadata") or {}),
            }
        return {k: v for k, v in raw.items() if k != "type"}

    @staticmethod
    def _payload_from_task_runtime(raw: dict[str, Any]) -> dict[str, Any]:
        payload = {k: v for k, v in raw.items() if k != "type"}
        if "task_id" in payload:
            payload["unit_id"] = str(payload.get("task_id") or "")
        if "subagent_type" in payload:
            payload["agent_type"] = str(payload.get("subagent_type") or "")
        return payload

    def to_sse_payload(self) -> dict[str, Any]:
        """Return the standard frontend wire payload."""
        return {
            "type": self.type,
            "payload": self.payload,
            "request_id": self.request_id,
            "thread_id": self.thread_id,
            "sequence": self.sequence,
            "created_at": self.created_at,
        }

    def to_envelope(
        self,
        *,
        event_id: str | None = None,
        correlation_id: str | None = None,
        causation_id: str | None = None,
        producer: str = DEFAULT_EVENT_PRODUCER,
        unit_id: str | None = None,
        idempotency_key: str | None = None,
    ) -> EventEnvelopeV2:
        """Opt into the V2 envelope without changing the legacy SSE wire shape."""

        return EventEnvelopeV2.from_agent_event(
            self,
            event_id=event_id,
            correlation_id=correlation_id,
            causation_id=causation_id,
            producer=producer,
            unit_id=unit_id,
            idempotency_key=idempotency_key,
        )


@dataclass(frozen=True, kw_only=True)
class EventEnvelopeV2:
    """Versioned event identity and lineage contract for durable transports.

    ``AgentEvent`` remains the compatibility-facing SSE event. Callers opt into
    this envelope when they need stable identity, causality, idempotency, or a
    producer/unit boundary for persistence and message-bus delivery.
    """

    event_id: str
    type: str
    request_id: str
    thread_id: str
    sequence: int
    correlation_id: str | None = None
    causation_id: str | None = None
    producer: str = DEFAULT_EVENT_PRODUCER
    unit_id: str | None = None
    idempotency_key: str | None = None
    payload: dict[str, Any] = field(default_factory=dict)
    created_at: str = field(default_factory=_utc_now_iso)
    schema_version: str = EVENT_ENVELOPE_SCHEMA_VERSION

    def __post_init__(self) -> None:
        if self.schema_version != EVENT_ENVELOPE_SCHEMA_VERSION:
            raise ValueError(
                "EventEnvelopeV2 requires schema_version "
                f"{EVENT_ENVELOPE_SCHEMA_VERSION!r}"
            )

        event_id = str(self.event_id or "").strip()
        event_type = str(self.type or "").strip()
        producer = str(self.producer or "").strip()
        created_at = str(self.created_at or "").strip()
        if not event_id:
            raise ValueError("event_id must not be empty")
        if not event_type:
            raise ValueError("type must not be empty")
        if not producer:
            raise ValueError("producer must not be empty")
        if not created_at:
            raise ValueError("created_at must not be empty")
        if not isinstance(self.payload, Mapping):
            raise TypeError("payload must be a mapping")

        request_id = str(self.request_id or "")
        thread_id = str(self.thread_id or "")
        correlation_id = (
            _optional_text(self.correlation_id)
            or _optional_text(request_id)
            or _optional_text(thread_id)
            or event_id
        )
        object.__setattr__(self, "event_id", event_id)
        object.__setattr__(self, "type", event_type)
        object.__setattr__(self, "request_id", request_id)
        object.__setattr__(self, "thread_id", thread_id)
        object.__setattr__(self, "sequence", int(self.sequence))
        object.__setattr__(self, "correlation_id", correlation_id)
        object.__setattr__(self, "causation_id", _optional_text(self.causation_id))
        object.__setattr__(self, "producer", producer)
        object.__setattr__(self, "unit_id", _optional_text(self.unit_id))
        object.__setattr__(
            self,
            "idempotency_key",
            _optional_text(self.idempotency_key) or event_id,
        )
        object.__setattr__(self, "payload", dict(self.payload))
        object.__setattr__(self, "created_at", created_at)

    @classmethod
    def from_agent_event(
        cls,
        event: AgentEvent,
        *,
        event_id: str | None = None,
        correlation_id: str | None = None,
        causation_id: str | None = None,
        producer: str = DEFAULT_EVENT_PRODUCER,
        unit_id: str | None = None,
        idempotency_key: str | None = None,
    ) -> EventEnvelopeV2:
        """Promote a legacy event while deriving stable retry-safe defaults."""

        normalized_producer = str(producer or "").strip()
        resolved_unit_id = _optional_text(unit_id)
        if resolved_unit_id is None:
            resolved_unit_id = _optional_text(event.payload.get("unit_id"))
        resolved_event_id = _optional_text(event_id) or _deterministic_event_id(
            event_type=event.type,
            request_id=event.request_id,
            thread_id=event.thread_id,
            sequence=event.sequence,
            producer=normalized_producer,
            unit_id=resolved_unit_id,
        )
        return cls(
            event_id=resolved_event_id,
            type=event.type,
            request_id=event.request_id,
            thread_id=event.thread_id,
            sequence=event.sequence,
            correlation_id=correlation_id,
            causation_id=causation_id,
            producer=normalized_producer,
            unit_id=resolved_unit_id,
            idempotency_key=idempotency_key,
            payload=event.payload,
            created_at=event.created_at,
        )

    @classmethod
    def from_dict(cls, raw: Mapping[str, Any]) -> EventEnvelopeV2:
        """Parse the explicit V2 representation used by durable transports."""

        payload = raw.get("payload", {})
        if not isinstance(payload, Mapping):
            raise TypeError("payload must be a mapping")
        return cls(
            schema_version=str(raw.get("schema_version") or ""),
            event_id=str(raw.get("event_id") or ""),
            type=str(raw.get("type") or ""),
            request_id=str(raw.get("request_id") or ""),
            thread_id=str(raw.get("thread_id") or ""),
            sequence=int(raw.get("sequence", 0)),
            correlation_id=_optional_text(raw.get("correlation_id")),
            causation_id=_optional_text(raw.get("causation_id")),
            producer=str(raw.get("producer") or ""),
            unit_id=_optional_text(raw.get("unit_id")),
            idempotency_key=_optional_text(raw.get("idempotency_key")),
            payload=dict(payload),
            created_at=str(raw.get("created_at") or ""),
        )

    def to_dict(self) -> dict[str, Any]:
        """Return the complete V2 envelope, including nullable lineage fields."""

        return {
            "schema_version": self.schema_version,
            "event_id": self.event_id,
            "type": self.type,
            "request_id": self.request_id,
            "thread_id": self.thread_id,
            "sequence": self.sequence,
            "correlation_id": self.correlation_id,
            "causation_id": self.causation_id,
            "producer": self.producer,
            "unit_id": self.unit_id,
            "idempotency_key": self.idempotency_key,
            "payload": dict(self.payload),
            "created_at": self.created_at,
        }

    def to_agent_event(self) -> AgentEvent:
        """Project back to the existing transport-neutral compatibility event."""

        return AgentEvent(
            type=self.type,
            request_id=self.request_id,
            thread_id=self.thread_id,
            sequence=self.sequence,
            payload=dict(self.payload),
            created_at=self.created_at,
        )


# Stable ergonomic name for consumers that do not need to spell the version in
# imports; the serialized schema_version remains the source of truth.
EventEnvelope = EventEnvelopeV2


_TASK_RUNTIME_EVENT_TYPES = {
    "task_started": "subagent.started",
    "task_running": "subagent.message",
    "task_tool_start": "subagent.tool.started",
    "task_tool_end": "subagent.tool.completed",
    "task_completed": "subagent.completed",
    "task_failed": "subagent.failed",
    "task_timed_out": "subagent.timed_out",
    "task_cancelled": "subagent.cancelled",
}
