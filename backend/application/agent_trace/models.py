"""Serializable trace records for agent run replay."""

from __future__ import annotations

from dataclasses import asdict, dataclass, field
from datetime import datetime, timezone
from typing import Any, Literal

TraceRecordKind = Literal[
    "run",
    "event",
    "unit",
    "tool_call",
    "context_pack",
    "compaction",
    "handoff",
]


def utc_now_iso() -> str:
    return datetime.now(timezone.utc).isoformat()


@dataclass(frozen=True)
class AgentRunTrace:
    run_id: str
    request_id: str
    user_id: str
    thread_id: str
    agent_code: str
    workflow: str
    model_name: str | None = None
    selected_skills: list[str] = field(default_factory=list)
    tool_policy: str | None = None
    status: str = "running"
    started_at: str = field(default_factory=utc_now_iso)
    ended_at: str | None = None
    total_tokens_estimated: int = 0
    total_tool_calls: int = 0
    error_code: str | None = None


@dataclass(frozen=True)
class AgentRunEvent:
    run_id: str
    sequence: int
    event_type: str
    payload: dict[str, Any] = field(default_factory=dict)
    unit_id: str | None = None
    created_at: str = field(default_factory=utc_now_iso)
    event_id: str | None = None
    schema_version: str | None = None
    request_id: str | None = None
    thread_id: str | None = None
    correlation_id: str | None = None
    causation_id: str | None = None
    producer: str | None = None
    idempotency_key: str | None = None


@dataclass(frozen=True)
class AgentRunUnit:
    unit_id: str
    run_id: str
    role: str
    agent_type: str
    task_title: str
    task_question: str
    parent_unit_id: str | None = None
    status: str = "running"
    started_at: str = field(default_factory=utc_now_iso)
    ended_at: str | None = None
    timeout_seconds: int | None = None
    tool_access: bool = True


@dataclass(frozen=True)
class AgentRunToolCall:
    tool_call_id: str
    run_id: str
    tool_name: str
    status: str
    unit_id: str | None = None
    started_at: str = field(default_factory=utc_now_iso)
    ended_at: str | None = None
    elapsed_ms: float | None = None
    args_preview: dict[str, Any] = field(default_factory=dict)
    result_preview: str | None = None
    error: str | None = None
    metadata: dict[str, Any] = field(default_factory=dict)


@dataclass(frozen=True)
class AgentRunContextPack:
    pack_id: str
    run_id: str
    unit_id: str | None
    model_call_index: int
    base_tokens: int
    dynamic_token_budget: int
    dynamic_tokens_used: int
    items_total: int
    items_injected: int
    selected_context_ids: list[str] = field(default_factory=list)
    usage_by_category: dict[str, int] = field(default_factory=dict)
    created_at: str = field(default_factory=utc_now_iso)


@dataclass(frozen=True)
class AgentRunCompaction:
    compaction_id: str
    run_id: str
    unit_id: str | None
    model_call_index: int
    trigger: str
    window_id: str
    previous_window_id: str | None
    tokens_before: int
    tokens_after: int
    summary_tokens: int
    retained_user_messages: int
    removed_messages: int
    threshold_tokens: int = 0
    fixed_request_tokens: int = 0
    status: str = "completed"
    error: str | None = None
    summary_preview: str | None = None
    created_at: str = field(default_factory=utc_now_iso)


@dataclass(frozen=True)
class AgentRunHandoff:
    handoff_id: str
    run_id: str
    unit_id: str | None
    role: str
    payload: dict[str, Any] = field(default_factory=dict)
    schema_version: str = "2.0"
    created_at: str = field(default_factory=utc_now_iso)


@dataclass(frozen=True)
class AgentRunRecord:
    kind: TraceRecordKind
    payload: dict[str, Any]

    @classmethod
    def from_dataclass(cls, kind: TraceRecordKind, value: Any) -> "AgentRunRecord":
        return cls(kind=kind, payload=asdict(value))

    def to_json_dict(self) -> dict[str, Any]:
        return {"kind": self.kind, "payload": self.payload}

    @classmethod
    def from_json_dict(cls, raw: dict[str, Any]) -> "AgentRunRecord":
        kind = str(raw.get("kind") or "")
        if kind not in {"run", "event", "unit", "tool_call", "context_pack", "compaction", "handoff"}:
            raise ValueError(f"Unknown agent trace record kind: {kind}")
        payload = raw.get("payload") or {}
        if not isinstance(payload, dict):
            raise ValueError("Agent trace record payload must be a mapping")
        return cls(kind=kind, payload=dict(payload))  # type: ignore[arg-type]
