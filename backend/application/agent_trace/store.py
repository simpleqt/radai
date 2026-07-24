"""JSONL-backed trace store for agent run replay."""

from __future__ import annotations

import json
import os
import re
from collections.abc import Iterable
from pathlib import Path
from typing import TYPE_CHECKING, Protocol, runtime_checkable

if TYPE_CHECKING:
    from sqlalchemy.engine import Engine

from backend.application.agent_trace.models import (
    AgentRunCompaction,
    AgentRunContextPack,
    AgentRunEvent,
    AgentRunHandoff,
    AgentRunRecord,
    AgentRunToolCall,
    AgentRunTrace,
    AgentRunUnit,
)

DEFAULT_TRACE_ROOT = Path(
    os.getenv(
        "AGENT_TRACE_ROOT",
        str(Path(__file__).resolve().parents[2] / ".runtime" / "agent_traces"),
    )
)


@runtime_checkable
class AgentTraceStoreProtocol(Protocol):
    """Storage surface shared by JSONL and indexed/object backends."""

    def append_run(self, run: AgentRunTrace) -> None: ...

    def append_event(self, event: AgentRunEvent) -> None: ...

    def append_unit(self, unit: AgentRunUnit) -> None: ...

    def append_tool_call(self, tool_call: AgentRunToolCall) -> None: ...

    def append_context_pack(self, pack: AgentRunContextPack) -> None: ...

    def append_compaction(self, compaction: AgentRunCompaction) -> None: ...

    def append_handoff(self, handoff: AgentRunHandoff) -> None: ...

    def append_record(self, run_id: str, record: AgentRunRecord) -> None: ...

    def read_records(self, run_id: str) -> list[AgentRunRecord]: ...

    def list_run_ids(self) -> list[str]: ...

    def write_records(
        self,
        run_id: str,
        records: Iterable[AgentRunRecord],
    ) -> Path: ...


class AgentTraceStore:
    def __init__(self, root: str | Path | None = None) -> None:
        self._root = Path(root or DEFAULT_TRACE_ROOT)
        self._root.mkdir(parents=True, exist_ok=True)

    @property
    def root(self) -> Path:
        return self._root

    def append_run(self, run: AgentRunTrace) -> None:
        self.append_record(run.run_id, AgentRunRecord.from_dataclass("run", run))

    def append_event(self, event: AgentRunEvent) -> None:
        self.append_record(event.run_id, AgentRunRecord.from_dataclass("event", event))

    def append_unit(self, unit: AgentRunUnit) -> None:
        self.append_record(unit.run_id, AgentRunRecord.from_dataclass("unit", unit))

    def append_tool_call(self, tool_call: AgentRunToolCall) -> None:
        self.append_record(
            tool_call.run_id,
            AgentRunRecord.from_dataclass("tool_call", tool_call),
        )

    def append_context_pack(self, pack: AgentRunContextPack) -> None:
        self.append_record(
            pack.run_id,
            AgentRunRecord.from_dataclass("context_pack", pack),
        )

    def append_compaction(self, compaction: AgentRunCompaction) -> None:
        self.append_record(
            compaction.run_id,
            AgentRunRecord.from_dataclass("compaction", compaction),
        )

    def append_handoff(self, handoff: AgentRunHandoff) -> None:
        self.append_record(
            handoff.run_id,
            AgentRunRecord.from_dataclass("handoff", handoff),
        )

    def append_record(self, run_id: str, record: AgentRunRecord) -> None:
        path = self.path_for_run(run_id)
        path.parent.mkdir(parents=True, exist_ok=True)
        with path.open("a", encoding="utf-8") as handle:
            handle.write(
                json.dumps(record.to_json_dict(), ensure_ascii=False, sort_keys=True)
            )
            handle.write("\n")

    def read_records(self, run_id: str) -> list[AgentRunRecord]:
        path = self.path_for_run(run_id)
        if not path.is_file():
            raise FileNotFoundError(f"Agent trace not found: {run_id}")
        records: list[AgentRunRecord] = []
        for line_number, line in enumerate(
            path.read_text(encoding="utf-8").splitlines(), start=1
        ):
            if not line.strip():
                continue
            try:
                raw = json.loads(line)
                if not isinstance(raw, dict):
                    raise ValueError("record must be a mapping")
                records.append(AgentRunRecord.from_json_dict(raw))
            except (json.JSONDecodeError, ValueError) as exc:
                raise ValueError(
                    f"Invalid agent trace record at {path}:{line_number}: {exc}"
                ) from exc
        return records

    def list_run_ids(self) -> list[str]:
        return sorted(
            path.stem for path in self._root.glob("*.jsonl") if path.is_file()
        )

    def write_records(self, run_id: str, records: Iterable[AgentRunRecord]) -> Path:
        path = self.path_for_run(run_id)
        path.parent.mkdir(parents=True, exist_ok=True)
        with path.open("w", encoding="utf-8") as handle:
            for record in records:
                handle.write(
                    json.dumps(
                        record.to_json_dict(), ensure_ascii=False, sort_keys=True
                    )
                )
                handle.write("\n")
        return path

    def path_for_run(self, run_id: str) -> Path:
        safe_run_id = self._safe_run_id(run_id)
        return self._root / f"{safe_run_id}.jsonl"

    @staticmethod
    def _safe_run_id(value: str) -> str:
        cleaned = re.sub(r"[^a-zA-Z0-9_.:-]+", "_", str(value or "")).strip("._")
        if not cleaned or cleaned in {".", ".."}:
            raise ValueError("run_id must not be empty")
        return cleaned[:160]


def create_agent_trace_store(
    root: str | Path | None = None,
    *,
    sql_engine: Engine | None = None,
    backend: str | None = None,
) -> AgentTraceStoreProtocol:
    """Create a trace backend with an explicit configuration seam.

    CLI callers can pass ``backend="jsonl"`` when a trace root is supplied so
    a process-level production setting cannot silently reinterpret a local
    replay/eval directory as SQL or object storage.
    """

    backend = str(backend or os.getenv("AGENT_TRACE_BACKEND", "jsonl")).strip().lower()
    if backend in {"", "jsonl", "file"}:
        return AgentTraceStore(root)
    if backend in {
        "indexed",
        "index",
        "object",
        "sql",
        "mysql",
        "sql-indexed",
    }:
        from backend.application.agent_trace.indexed_store import (
            DEFAULT_INDEXED_TRACE_ROOT,
            IndexedAgentTraceStore,
            LocalFileTraceBlobStore,
            MinioTraceBlobStore,
        )

        indexed_root = Path(
            root or os.getenv("AGENT_TRACE_INDEXED_ROOT") or DEFAULT_INDEXED_TRACE_ROOT
        )
        blob_backend = os.getenv("AGENT_TRACE_BLOB_BACKEND", "local").strip().lower()
        if blob_backend in {"", "local", "file", "filesystem"}:
            blob_store = LocalFileTraceBlobStore(indexed_root / "objects")
        elif blob_backend in {"minio", "s3", "object"}:
            from backend.storage import (
                _get_minio_client,
                get_user_minio_bucket_name,
            )

            bucket = (
                os.getenv("AGENT_TRACE_MINIO_BUCKET", "").strip()
                or get_user_minio_bucket_name()
            )
            prefix = os.getenv("AGENT_TRACE_MINIO_PREFIX", "agent-traces")
            blob_store = MinioTraceBlobStore(
                _get_minio_client(),
                bucket=bucket,
                prefix=prefix,
            )
        else:
            raise ValueError(
                "AGENT_TRACE_BLOB_BACKEND must be local or minio when "
                "AGENT_TRACE_BACKEND is indexed or sql"
            )

        if backend in {"indexed", "index", "object"}:
            return IndexedAgentTraceStore(indexed_root, blob_store=blob_store)

        from backend.application.agent_trace.sql_index_store import (
            SQLAlchemyIndexedAgentTraceStore,
        )

        if sql_engine is None:
            from backend.db.connection import get_engine

            sql_engine = get_engine()
        return SQLAlchemyIndexedAgentTraceStore(
            sql_engine,
            blob_store=blob_store,
        )
    raise ValueError("AGENT_TRACE_BACKEND must be jsonl, indexed, or sql")
