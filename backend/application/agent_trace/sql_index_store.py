"""Shared SQL trace index backed by an injected SQLAlchemy Engine.

Complete trace records remain in a :class:`TraceBlobStore`; the relational
tables contain only lookup metadata, ordering counters, and content digests.
Blob bytes are made durable before their index transaction starts, so a failed
transaction can leave only an unreferenced content-addressed blob.

Construction performs no database I/O and the Engine remains caller-owned.
Deployments should create the tables through their migration system, or call
``create_schema()`` explicitly during controlled startup.  The Core schema and
transaction strategy support MySQL/InnoDB in production and file-backed SQLite
for focused tests.
"""

from __future__ import annotations

import hashlib
import json
import time
from collections.abc import Iterable
from dataclasses import dataclass
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, TypeVar, cast

from sqlalchemy import (
    BigInteger,
    CheckConstraint,
    Column,
    ForeignKeyConstraint,
    Index,
    MetaData,
    String,
    Table,
    UniqueConstraint,
    and_,
    delete,
    insert,
    select,
    update,
)
from sqlalchemy.engine import Connection, Engine, RowMapping
from sqlalchemy.exc import IntegrityError, OperationalError

from backend.application.agent_trace.indexed_store import (
    TraceBlobIntegrityError,
    TraceBlobMissingError,
    TraceBlobStore,
    TraceEventIdConflictError,
    validate_replacement_event_ids,
)
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

_MAX_INDEXED_ID_LENGTH = 191
_SQL_INDEX_RECEIPT = Path("sql-agent-trace-index")
_METADATA = MetaData()

_RUNS = Table(
    "agent_trace_index_runs",
    _METADATA,
    Column("run_id", String(_MAX_INDEXED_ID_LENGTH), primary_key=True),
    Column("next_ordinal", BigInteger, nullable=False),
    Column("version", BigInteger, nullable=False),
    Column("created_at", String(64), nullable=False),
    CheckConstraint("next_ordinal >= 0", name="ck_agent_trace_runs_next_ordinal"),
    CheckConstraint("version >= 1", name="ck_agent_trace_runs_version"),
    mysql_engine="InnoDB",
    mysql_charset="utf8mb4",
    mysql_collate="utf8mb4_bin",
)

_RECORDS = Table(
    "agent_trace_index_records",
    _METADATA,
    Column("run_id", String(_MAX_INDEXED_ID_LENGTH), primary_key=True),
    Column("ordinal", BigInteger, primary_key=True),
    Column("kind", String(64), nullable=False),
    Column("object_key", String(1024), nullable=False),
    Column("sha256", String(64), nullable=False),
    Column("event_sequence", BigInteger),
    Column("event_unit_id", String(_MAX_INDEXED_ID_LENGTH)),
    Column("event_id", String(_MAX_INDEXED_ID_LENGTH)),
    Column("created_at", String(64), nullable=False),
    CheckConstraint("ordinal >= 0", name="ck_agent_trace_records_ordinal"),
    ForeignKeyConstraint(
        ["run_id"],
        ["agent_trace_index_runs.run_id"],
        name="fk_agent_trace_records_run",
        ondelete="CASCADE",
    ),
    UniqueConstraint("event_id", name="uq_agent_trace_records_event_id"),
    mysql_engine="InnoDB",
    mysql_charset="utf8mb4",
    mysql_collate="utf8mb4_bin",
)
Index(
    "ix_agent_trace_records_run_sequence",
    _RECORDS.c.run_id,
    _RECORDS.c.event_sequence,
)
Index(
    "ix_agent_trace_records_run_unit",
    _RECORDS.c.run_id,
    _RECORDS.c.event_unit_id,
)


class SQLTraceIndexStoreError(RuntimeError):
    """Raised when a shared trace-index mutation exhausts safe retries."""


class _ConcurrentWrite(RuntimeError):
    """Internal signal for a lost run-counter compare-and-swap."""


@dataclass(frozen=True)
class _PreparedRecord:
    record: AgentRunRecord
    data: bytes
    sha256: str
    object_key: str
    event_sequence: int | None
    event_unit_id: str | None
    event_id: str | None
    created_at: str


_ResultT = TypeVar("_ResultT")


class SQLAlchemyIndexedAgentTraceStore:
    """Synchronous shared trace index over a caller-owned SQLAlchemy Engine.

    Event IDs are globally idempotent.  Re-appending the exact same event is a
    no-op; using its ID for different content or another run raises
    :class:`TraceEventIdConflictError`.

    Appends lock the run counter on databases that implement ``FOR UPDATE``
    and additionally compare-and-swap its version.  The CAS keeps the same
    algorithm safe on SQLite, where ``FOR UPDATE`` is ignored.  Transient
    deadlocks, lock timeouts, SQLite busy errors, and uniqueness races are
    retried in fresh transactions.
    """

    def __init__(
        self,
        engine: Engine,
        *,
        blob_store: TraceBlobStore,
        max_transaction_retries: int = 32,
        retry_base_delay_seconds: float = 0.002,
    ) -> None:
        if not isinstance(engine, Engine):
            raise TypeError("engine must be a synchronous SQLAlchemy Engine")
        if not isinstance(blob_store, TraceBlobStore):
            raise TypeError("blob_store must implement TraceBlobStore")
        retries = int(max_transaction_retries)
        if retries < 1:
            raise ValueError("max_transaction_retries must be at least 1")
        retry_delay = float(retry_base_delay_seconds)
        if retry_delay < 0:
            raise ValueError("retry_base_delay_seconds must not be negative")

        self.engine = engine
        self._blob_store = blob_store
        self.max_transaction_retries = retries
        self.retry_base_delay_seconds = retry_delay

    @property
    def blob_store(self) -> TraceBlobStore:
        return self._blob_store

    @property
    def index_path(self) -> Path:
        """Return a logical receipt compatible with file-backed stores."""

        return _SQL_INDEX_RECEIPT

    def create_schema(self) -> None:
        """Explicitly create the shared index tables and indexes."""

        _METADATA.create_all(self.engine)

    def append_run(self, run: AgentRunTrace) -> None:
        self.append_record(run.run_id, AgentRunRecord.from_dataclass("run", run))

    def append_event(self, event: AgentRunEvent) -> None:
        self.append_record(
            event.run_id,
            AgentRunRecord.from_dataclass("event", event),
        )

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
        normalized_run_id = _validate_indexed_id(run_id, field_name="run_id")
        prepared = _prepare_record(record)
        self._blob_store.put_bytes(prepared.object_key, prepared.data)

        def operation(connection: Connection) -> None:
            if prepared.event_id is not None:
                existing = (
                    connection.execute(
                        select(_RECORDS)
                        .where(_RECORDS.c.event_id == prepared.event_id)
                        .with_for_update()
                    )
                    .mappings()
                    .first()
                )
                if existing is not None:
                    _resolve_existing_event(
                        existing,
                        run_id=normalized_run_id,
                        prepared=prepared,
                    )
                    return

            run = _load_or_create_run(connection, normalized_run_id)
            ordinal = cast(int, run["next_ordinal"])
            _advance_run_counter(
                connection,
                run_id=normalized_run_id,
                current_version=cast(int, run["version"]),
                next_ordinal=ordinal + 1,
            )
            connection.execute(
                insert(_RECORDS).values(
                    _record_values(
                        run_id=normalized_run_id,
                        ordinal=ordinal,
                        prepared=prepared,
                    )
                )
            )

        self._run_retrying_transaction(
            operation,
            operation_name=f"append trace record {normalized_run_id}",
        )

    def read_records(self, run_id: str) -> list[AgentRunRecord]:
        normalized_run_id = _validate_indexed_id(run_id, field_name="run_id")
        with self.engine.connect() as connection:
            next_ordinal = connection.execute(
                select(_RUNS.c.next_ordinal).where(_RUNS.c.run_id == normalized_run_id)
            ).scalar_one_or_none()
            if next_ordinal is None:
                raise FileNotFoundError(f"Agent trace not found: {normalized_run_id}")
            rows = (
                connection.execute(
                    select(
                        _RECORDS.c.ordinal,
                        _RECORDS.c.kind,
                        _RECORDS.c.object_key,
                        _RECORDS.c.sha256,
                    )
                    .where(_RECORDS.c.run_id == normalized_run_id)
                    .order_by(_RECORDS.c.ordinal.asc())
                )
                .mappings()
                .all()
            )

        _validate_index_state(
            normalized_run_id,
            rows,
            next_ordinal=cast(int, next_ordinal),
        )
        return [self._read_indexed_record(normalized_run_id, row) for row in rows]

    def list_run_ids(self) -> list[str]:
        with self.engine.connect() as connection:
            rows = connection.execute(
                select(_RUNS.c.run_id).order_by(_RUNS.c.run_id.asc())
            ).all()
        return [cast(str, row[0]) for row in rows]

    def write_records(
        self,
        run_id: str,
        records: Iterable[AgentRunRecord],
    ) -> Path:
        normalized_run_id = _validate_indexed_id(run_id, field_name="run_id")
        prepared_records = [_prepare_record(record) for record in records]
        validate_replacement_event_ids(prepared_records)
        for prepared in prepared_records:
            self._blob_store.put_bytes(prepared.object_key, prepared.data)

        def operation(connection: Connection) -> None:
            event_ids = [
                prepared.event_id
                for prepared in prepared_records
                if prepared.event_id is not None
            ]
            if event_ids:
                conflict = (
                    connection.execute(
                        select(_RECORDS.c.event_id, _RECORDS.c.run_id)
                        .where(
                            and_(
                                _RECORDS.c.event_id.in_(event_ids),
                                _RECORDS.c.run_id != normalized_run_id,
                            )
                        )
                        .with_for_update()
                    )
                    .mappings()
                    .first()
                )
                if conflict is not None:
                    raise TraceEventIdConflictError(
                        "Trace event_id is already indexed for another run: "
                        f"{conflict['event_id']}"
                    )

            run = _load_or_create_run(connection, normalized_run_id)
            _advance_run_counter(
                connection,
                run_id=normalized_run_id,
                current_version=cast(int, run["version"]),
                next_ordinal=len(prepared_records),
            )
            connection.execute(
                delete(_RECORDS).where(_RECORDS.c.run_id == normalized_run_id)
            )
            for ordinal, prepared in enumerate(prepared_records):
                connection.execute(
                    insert(_RECORDS).values(
                        _record_values(
                            run_id=normalized_run_id,
                            ordinal=ordinal,
                            prepared=prepared,
                        )
                    )
                )

        self._run_retrying_transaction(
            operation,
            operation_name=f"replace trace records {normalized_run_id}",
        )
        return self.index_path

    def _read_indexed_record(
        self,
        run_id: str,
        row: RowMapping,
    ) -> AgentRunRecord:
        object_key = cast(str, row["object_key"])
        try:
            data = self._blob_store.get_bytes(object_key)
        except FileNotFoundError as exc:
            raise TraceBlobMissingError(
                "Trace blob is missing for "
                f"run_id={run_id!r}, ordinal={row['ordinal']}, "
                f"object_key={object_key!r}"
            ) from exc

        expected_digest = cast(str, row["sha256"])
        actual_digest = hashlib.sha256(data).hexdigest()
        if actual_digest != expected_digest:
            raise TraceBlobIntegrityError(
                "Trace blob SHA-256 mismatch for "
                f"run_id={run_id!r}, ordinal={row['ordinal']}, "
                f"object_key={object_key!r}: expected {expected_digest}, "
                f"got {actual_digest}"
            )
        try:
            raw = json.loads(data.decode("utf-8"))
            if not isinstance(raw, dict):
                raise ValueError("record must be a mapping")
            record = AgentRunRecord.from_json_dict(raw)
        except (UnicodeDecodeError, json.JSONDecodeError, ValueError) as exc:
            raise TraceBlobIntegrityError(
                "Invalid trace record blob for "
                f"run_id={run_id!r}, ordinal={row['ordinal']}, "
                f"object_key={object_key!r}: {exc}"
            ) from exc
        expected_kind = cast(str, row["kind"])
        if record.kind != expected_kind:
            raise TraceBlobIntegrityError(
                "Trace blob kind does not match its index for "
                f"run_id={run_id!r}, ordinal={row['ordinal']}: "
                f"expected {expected_kind!r}, got {record.kind!r}"
            )
        return record

    def _run_retrying_transaction(
        self,
        operation: Any,
        *,
        operation_name: str,
    ) -> _ResultT:
        for attempt in range(self.max_transaction_retries):
            try:
                with self.engine.begin() as connection:
                    return cast(_ResultT, operation(connection))
            except _ConcurrentWrite as exc:
                last_error: Exception = exc
            except IntegrityError as exc:
                # Run creation, ordinal allocation, and event-id insertion can
                # all lose a legitimate race.  A fresh transaction observes
                # the winner and either continues or reports an ID conflict.
                last_error = exc
            except OperationalError as exc:
                if not _is_retryable_operational_error(exc):
                    raise
                last_error = exc
            self._retry_delay(attempt)
        raise SQLTraceIndexStoreError(
            f"{operation_name} failed after "
            f"{self.max_transaction_retries} transaction attempts"
        ) from last_error

    def _retry_delay(self, attempt: int) -> None:
        if self.retry_base_delay_seconds <= 0:
            return
        time.sleep(min(self.retry_base_delay_seconds * (2**attempt), 0.25))


def _load_or_create_run(connection: Connection, run_id: str) -> RowMapping:
    row = (
        connection.execute(
            select(_RUNS).where(_RUNS.c.run_id == run_id).with_for_update()
        )
        .mappings()
        .first()
    )
    if row is not None:
        return row

    created_at = _utc_now_iso()
    connection.execute(
        insert(_RUNS).values(
            run_id=run_id,
            next_ordinal=0,
            version=1,
            created_at=created_at,
        )
    )
    inserted = (
        connection.execute(select(_RUNS).where(_RUNS.c.run_id == run_id))
        .mappings()
        .one()
    )
    return inserted


def _advance_run_counter(
    connection: Connection,
    *,
    run_id: str,
    current_version: int,
    next_ordinal: int,
) -> None:
    result = connection.execute(
        update(_RUNS)
        .where(
            and_(
                _RUNS.c.run_id == run_id,
                _RUNS.c.version == current_version,
            )
        )
        .values(
            next_ordinal=next_ordinal,
            version=current_version + 1,
        )
    )
    if result.rowcount != 1:
        raise _ConcurrentWrite(f"Trace run counter changed during write: {run_id}")


def _prepare_record(record: AgentRunRecord) -> _PreparedRecord:
    encoded = json.dumps(
        record.to_json_dict(),
        ensure_ascii=False,
        separators=(",", ":"),
        sort_keys=True,
    ).encode("utf-8")
    digest = hashlib.sha256(encoded).hexdigest()
    payload = record.payload
    event_sequence: int | None = None
    event_unit_id: str | None = None
    event_id: str | None = None
    if record.kind == "event":
        raw_sequence = payload.get("sequence")
        if isinstance(raw_sequence, int) and not isinstance(raw_sequence, bool):
            event_sequence = raw_sequence
        event_unit_id = _optional_indexed_id(
            payload.get("unit_id"),
            field_name="event unit_id",
        )
        event_id = _optional_indexed_id(
            payload.get("event_id"),
            field_name="event_id",
        )
    created_at = _optional_text(payload.get("created_at")) or _utc_now_iso()
    if len(created_at) > 64:
        raise ValueError("created_at exceeds the SQL trace index limit of 64")
    if len(record.kind) > 64:
        raise ValueError("record kind exceeds the SQL trace index limit of 64")
    object_key = f"records/{digest[:2]}/{digest}.json"
    if len(object_key) > 1024:
        raise ValueError("trace blob object key exceeds the SQL index limit")
    return _PreparedRecord(
        record=record,
        data=encoded,
        sha256=digest,
        object_key=object_key,
        event_sequence=event_sequence,
        event_unit_id=event_unit_id,
        event_id=event_id,
        created_at=created_at,
    )


def _record_values(
    *,
    run_id: str,
    ordinal: int,
    prepared: _PreparedRecord,
) -> dict[str, Any]:
    return {
        "run_id": run_id,
        "ordinal": ordinal,
        "kind": prepared.record.kind,
        "object_key": prepared.object_key,
        "sha256": prepared.sha256,
        "event_sequence": prepared.event_sequence,
        "event_unit_id": prepared.event_unit_id,
        "event_id": prepared.event_id,
        "created_at": prepared.created_at,
    }


def _resolve_existing_event(
    existing: RowMapping,
    *,
    run_id: str,
    prepared: _PreparedRecord,
) -> None:
    if (
        existing["run_id"] == run_id
        and existing["kind"] == prepared.record.kind
        and existing["sha256"] == prepared.sha256
    ):
        return
    raise TraceEventIdConflictError(
        f"Trace event_id is already indexed with different content: {prepared.event_id}"
    )


def _validate_index_state(
    run_id: str,
    rows: list[RowMapping],
    *,
    next_ordinal: int,
) -> None:
    if next_ordinal != len(rows):
        raise TraceBlobIntegrityError(
            "Trace index count does not match its run counter for "
            f"run_id={run_id!r}: expected {next_ordinal}, got {len(rows)}"
        )
    for expected, row in enumerate(rows):
        actual = cast(int, row["ordinal"])
        if actual != expected:
            raise TraceBlobIntegrityError(
                "Trace index ordinal gap for "
                f"run_id={run_id!r}: expected {expected}, got {actual}"
            )


def _validate_indexed_id(value: object, *, field_name: str) -> str:
    normalized = str(value or "").strip()
    if not normalized:
        raise ValueError(f"{field_name} must not be empty")
    if len(normalized) > _MAX_INDEXED_ID_LENGTH:
        raise ValueError(
            f"{field_name} exceeds the SQL trace index limit of "
            f"{_MAX_INDEXED_ID_LENGTH} characters"
        )
    return normalized


def _optional_indexed_id(value: object, *, field_name: str) -> str | None:
    normalized = _optional_text(value)
    if normalized is None:
        return None
    return _validate_indexed_id(normalized, field_name=field_name)


def _optional_text(value: object) -> str | None:
    if value is None:
        return None
    normalized = str(value).strip()
    return normalized or None


def _is_retryable_operational_error(error: OperationalError) -> bool:
    original = error.orig
    code = None
    args = getattr(original, "args", ())
    if args:
        code = args[0]
    if code in {5, 6, 1205, 1213}:
        return True
    message = str(original).lower()
    return any(
        marker in message
        for marker in (
            "database is locked",
            "database table is locked",
            "deadlock found",
            "lock wait timeout",
        )
    )


def _utc_now_iso() -> str:
    return datetime.now(timezone.utc).isoformat()  # noqa: UP017 -- Python 3.10
