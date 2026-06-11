"""Internal agent event contract.

AgentEvent is intentionally transport-agnostic. SSE encoding is handled in
the application/event_stream layer.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from datetime import datetime, timezone
from typing import Any


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
