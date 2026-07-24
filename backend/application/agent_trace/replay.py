"""Dry-run replay for agent trace JSONL files."""

from __future__ import annotations

from dataclasses import asdict, dataclass, field
from typing import Any

from backend.application.agent_trace.models import AgentRunRecord
from backend.application.agent_trace.store import (
    AgentTraceStoreProtocol,
    create_agent_trace_store,
)


@dataclass(frozen=True)
class ReplaySummary:
    run_id: str
    mode: str
    status: str
    events: int = 0
    units: int = 0
    tool_calls: int = 0
    context_packs: int = 0
    compactions: int = 0
    compaction_attempts: int = 0
    compaction_skipped: int = 0
    handoffs: int = 0
    selected_unit_id: str | None = None
    run: dict[str, Any] = field(default_factory=dict)
    cached_tool_results: list[dict[str, Any]] = field(default_factory=list)
    replay_notes: list[str] = field(default_factory=list)

    def to_json_dict(self) -> dict[str, Any]:
        return asdict(self)


class AgentRunReplay:
    def __init__(self, store: AgentTraceStoreProtocol | None = None) -> None:
        self._store = store or create_agent_trace_store()

    def dry_run(self, run_id: str, *, from_unit: str | None = None) -> ReplaySummary:
        return self.replay(run_id, mode="dry-run", from_unit=from_unit)

    def replay(
        self,
        run_id: str,
        *,
        mode: str = "dry-run",
        from_unit: str | None = None,
    ) -> ReplaySummary:
        if mode not in {"dry-run", "tool-cache", "full"}:
            raise ValueError(f"Unsupported replay mode: {mode}")
        records = self._store.read_records(run_id)
        if from_unit:
            records = self._filter_from_unit(records, from_unit)
        run_payload = next(
            (record.payload for record in reversed(records) if record.kind == "run"),
            {},
        )
        tool_cache = self._tool_cache(records) if mode in {"tool-cache", "full"} else []
        notes: list[str] = []
        if mode == "tool-cache":
            notes.append("Loaded completed tool results from trace; model calls are not executed.")
        if mode == "full":
            notes.append(
                "Offline full replay reconstructed run structure and tool cache from trace; "
                "external model/tool calls are intentionally not executed by this CLI path."
            )
        return ReplaySummary(
            run_id=run_id,
            mode=mode,
            status=str(run_payload.get("status") or "unknown"),
            events=sum(1 for record in records if record.kind == "event"),
            units=sum(1 for record in records if record.kind == "unit"),
            tool_calls=sum(1 for record in records if record.kind == "tool_call"),
            context_packs=sum(1 for record in records if record.kind == "context_pack"),
            compactions=sum(
                1
                for record in records
                if record.kind == "compaction"
                and str(record.payload.get("status") or "completed") == "completed"
            ),
            compaction_attempts=sum(
                1 for record in records if record.kind == "compaction"
            ),
            compaction_skipped=sum(
                1
                for record in records
                if record.kind == "compaction"
                and str(record.payload.get("status") or "completed") == "skipped"
            ),
            handoffs=sum(1 for record in records if record.kind == "handoff"),
            selected_unit_id=from_unit,
            run=dict(run_payload),
            cached_tool_results=tool_cache,
            replay_notes=notes,
        )

    @staticmethod
    def _filter_from_unit(
        records: list[AgentRunRecord],
        unit_id: str,
    ) -> list[AgentRunRecord]:
        filtered: list[AgentRunRecord] = []
        for record in records:
            if record.kind == "run":
                filtered.append(record)
                continue
            payload_unit = str(record.payload.get("unit_id") or "")
            if payload_unit == unit_id:
                filtered.append(record)
        return filtered

    @staticmethod
    def _tool_cache(records: list[AgentRunRecord]) -> list[dict[str, Any]]:
        cache: list[dict[str, Any]] = []
        for record in records:
            if record.kind != "tool_call":
                continue
            payload = record.payload
            if payload.get("status") != "completed":
                continue
            cache.append(
                {
                    "tool_call_id": payload.get("tool_call_id"),
                    "unit_id": payload.get("unit_id"),
                    "tool_name": payload.get("tool_name"),
                    "args_preview": payload.get("args_preview") or {},
                    "result_preview": payload.get("result_preview"),
                    "metadata": payload.get("metadata") or {},
                }
            )
        return cache
