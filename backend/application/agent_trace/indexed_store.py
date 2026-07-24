"""SQLite-indexed agent traces backed by an object/blob store.

The existing JSONL store remains the default trace backend.  This module offers
the same append/read/list/write surface while keeping complete records out of
the relational index.  Record blobs are content addressed, so an interrupted
write can leave only a harmless orphan blob; an index row is committed only
after its blob is durable.
"""

from __future__ import annotations

import hashlib
import io
import json
import os
import sqlite3
import tempfile
from collections.abc import Iterable
from dataclasses import dataclass
from datetime import datetime, timezone
from pathlib import Path, PurePosixPath
from typing import Any, Protocol, runtime_checkable

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

DEFAULT_INDEXED_TRACE_ROOT = Path(
    os.getenv(
        "AGENT_TRACE_INDEXED_ROOT",
        str(Path(__file__).resolve().parents[2] / ".runtime" / "agent_trace_index"),
    )
)


class TraceBlobMissingError(FileNotFoundError):
    """Raised when an index row points at a blob that does not exist."""


class TraceBlobIntegrityError(ValueError):
    """Raised when stored trace bytes do not match their indexed digest."""


class TraceBlobConflictError(ValueError):
    """Raised when a content-addressed key already contains different bytes."""


class TraceEventIdConflictError(ValueError):
    """Raised when an event id is reused for a different trace record."""


@runtime_checkable
class TraceBlobStore(Protocol):
    """Minimal synchronous object-store boundary used by indexed traces.

    A MinIO implementation only needs to translate these two byte-oriented
    operations; this module deliberately performs no network access itself.
    Implementations must make a successful ``put_bytes`` durable before it
    returns and treat writing the same bytes to the same key as idempotent.
    """

    def put_bytes(self, object_key: str, data: bytes) -> None:
        """Durably store bytes at ``object_key``."""

    def get_bytes(self, object_key: str) -> bytes:
        """Return bytes for ``object_key`` or raise ``FileNotFoundError``."""


class LocalFileTraceBlobStore:
    """Atomic local-filesystem implementation of :class:`TraceBlobStore`."""

    def __init__(self, root: str | Path) -> None:
        self._root = Path(root)
        self._root.mkdir(parents=True, exist_ok=True)

    @property
    def root(self) -> Path:
        return self._root

    def path_for_key(self, object_key: str) -> Path:
        key = str(object_key or "").strip()
        parts = PurePosixPath(key).parts
        if (
            not key
            or PurePosixPath(key).is_absolute()
            or "\\" in key
            or any(part in {"", ".", ".."} for part in parts)
        ):
            raise ValueError(f"Invalid trace blob object key: {object_key!r}")
        return self._root.joinpath(*parts)

    def put_bytes(self, object_key: str, data: bytes) -> None:
        if not isinstance(data, bytes):
            raise TypeError("Trace blob data must be bytes")
        path = self.path_for_key(object_key)
        path.parent.mkdir(parents=True, exist_ok=True)

        descriptor, temporary_name = tempfile.mkstemp(
            dir=path.parent,
            prefix=f".{path.name}.",
            suffix=".tmp",
        )
        temporary_path = Path(temporary_name)
        try:
            with os.fdopen(descriptor, "wb") as handle:
                handle.write(data)
                handle.flush()
                os.fsync(handle.fileno())
            try:
                os.link(temporary_path, path)
            except FileExistsError:
                if path.read_bytes() != data:
                    raise TraceBlobConflictError(
                        f"Trace blob key already contains different bytes: {object_key}"
                    ) from None
            else:
                _fsync_directory(path.parent)
        finally:
            temporary_path.unlink(missing_ok=True)

    def get_bytes(self, object_key: str) -> bytes:
        return self.path_for_key(object_key).read_bytes()


class MinioTraceBlobStore:
    """Injected MinIO-compatible blob adapter with no constructor I/O."""

    def __init__(
        self,
        client: Any,
        *,
        bucket: str,
        prefix: str = "agent-traces",
    ) -> None:
        normalized_bucket = str(bucket or "").strip()
        if not normalized_bucket:
            raise ValueError("Trace MinIO bucket must not be empty")
        self._client = client
        self._bucket = normalized_bucket
        self._prefix = str(prefix or "").strip("/")

    @property
    def bucket(self) -> str:
        return self._bucket

    @property
    def prefix(self) -> str:
        return self._prefix

    def object_name(self, object_key: str) -> str:
        key = str(object_key or "").strip()
        parts = PurePosixPath(key).parts
        if (
            not key
            or PurePosixPath(key).is_absolute()
            or "\\" in key
            or any(part in {"", ".", ".."} for part in parts)
        ):
            raise ValueError(f"Invalid trace blob object key: {object_key!r}")
        return f"{self._prefix}/{key}" if self._prefix else key

    def put_bytes(self, object_key: str, data: bytes) -> None:
        if not isinstance(data, bytes):
            raise TypeError("Trace blob data must be bytes")
        self._client.put_object(
            self._bucket,
            self.object_name(object_key),
            io.BytesIO(data),
            len(data),
            content_type="application/json",
        )

    def get_bytes(self, object_key: str) -> bytes:
        object_name = self.object_name(object_key)
        try:
            response = self._client.get_object(self._bucket, object_name)
        except Exception as exc:
            if getattr(exc, "code", None) in {
                "NoSuchBucket",
                "NoSuchKey",
                "NoSuchObject",
                "NotFound",
            }:
                raise FileNotFoundError(
                    f"Trace blob not found: {self._bucket}/{object_name}"
                ) from exc
            raise
        try:
            return response.read()
        finally:
            close = getattr(response, "close", None)
            if callable(close):
                close()
            release = getattr(response, "release_conn", None)
            if callable(release):
                release()


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


class IndexedAgentTraceStore:
    """Agent trace store with a compact SQLite index and external record blobs.

    Ordinals are zero based and preserve append/write order.  Event IDs are
    globally idempotent: appending the exact same event again is a no-op, while
    reusing an event ID for different content raises a clear conflict error.
    """

    def __init__(
        self,
        root: str | Path | None = None,
        *,
        index_path: str | Path | None = None,
        blob_store: TraceBlobStore | None = None,
        sqlite_timeout_seconds: float = 30.0,
    ) -> None:
        self._root = Path(root or DEFAULT_INDEXED_TRACE_ROOT)
        self._root.mkdir(parents=True, exist_ok=True)
        self._index_path = Path(index_path or self._root / "trace_index.sqlite3")
        self._index_path.parent.mkdir(parents=True, exist_ok=True)
        self._blob_store = blob_store or LocalFileTraceBlobStore(self._root / "objects")
        self._sqlite_timeout_seconds = max(0.1, float(sqlite_timeout_seconds))
        self._initialize_index()

    @property
    def root(self) -> Path:
        return self._root

    @property
    def index_path(self) -> Path:
        return self._index_path

    @property
    def blob_store(self) -> TraceBlobStore:
        return self._blob_store

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
        normalized_run_id = _validate_run_id(run_id)
        prepared = self._prepare_record(record)
        self._blob_store.put_bytes(prepared.object_key, prepared.data)

        connection = self._connect()
        try:
            connection.execute("BEGIN IMMEDIATE")
            if prepared.event_id is not None:
                existing = connection.execute(
                    """
                    SELECT run_id, kind, sha256
                    FROM trace_records
                    WHERE event_id = ?
                    """,
                    (prepared.event_id,),
                ).fetchone()
                if existing is not None:
                    if (
                        existing["run_id"] == normalized_run_id
                        and existing["kind"] == record.kind
                        and existing["sha256"] == prepared.sha256
                    ):
                        connection.rollback()
                        return
                    raise TraceEventIdConflictError(
                        "Trace event_id is already indexed with different content: "
                        f"{prepared.event_id}"
                    )

            connection.execute(
                "INSERT OR IGNORE INTO trace_runs(run_id, created_at) VALUES (?, ?)",
                (normalized_run_id, _utc_now_iso()),
            )
            ordinal = connection.execute(
                """
                SELECT COALESCE(MAX(ordinal), -1) + 1
                FROM trace_records
                WHERE run_id = ?
                """,
                (normalized_run_id,),
            ).fetchone()[0]
            self._insert_prepared(
                connection,
                run_id=normalized_run_id,
                ordinal=int(ordinal),
                prepared=prepared,
            )
            connection.commit()
        except sqlite3.IntegrityError as exc:
            connection.rollback()
            raise _translate_integrity_error(exc, prepared.event_id) from exc
        except Exception:
            connection.rollback()
            raise
        finally:
            connection.close()

    def read_records(self, run_id: str) -> list[AgentRunRecord]:
        normalized_run_id = _validate_run_id(run_id)
        connection = self._connect()
        try:
            run_exists = connection.execute(
                "SELECT 1 FROM trace_runs WHERE run_id = ?",
                (normalized_run_id,),
            ).fetchone()
            if run_exists is None:
                raise FileNotFoundError(f"Agent trace not found: {normalized_run_id}")
            rows = connection.execute(
                """
                SELECT ordinal, kind, object_key, sha256
                FROM trace_records
                WHERE run_id = ?
                ORDER BY ordinal ASC
                """,
                (normalized_run_id,),
            ).fetchall()
        finally:
            connection.close()

        return [self._read_indexed_record(normalized_run_id, row) for row in rows]

    def list_run_ids(self) -> list[str]:
        connection = self._connect()
        try:
            rows = connection.execute(
                "SELECT run_id FROM trace_runs ORDER BY run_id ASC"
            ).fetchall()
        finally:
            connection.close()
        return [str(row["run_id"]) for row in rows]

    def write_records(
        self,
        run_id: str,
        records: Iterable[AgentRunRecord],
    ) -> Path:
        normalized_run_id = _validate_run_id(run_id)
        prepared_records = [self._prepare_record(record) for record in records]
        validate_replacement_event_ids(prepared_records)
        for prepared in prepared_records:
            self._blob_store.put_bytes(prepared.object_key, prepared.data)

        connection = self._connect()
        try:
            connection.execute("BEGIN IMMEDIATE")
            connection.execute(
                "INSERT OR IGNORE INTO trace_runs(run_id, created_at) VALUES (?, ?)",
                (normalized_run_id, _utc_now_iso()),
            )
            connection.execute(
                "DELETE FROM trace_records WHERE run_id = ?",
                (normalized_run_id,),
            )
            for ordinal, prepared in enumerate(prepared_records):
                self._insert_prepared(
                    connection,
                    run_id=normalized_run_id,
                    ordinal=ordinal,
                    prepared=prepared,
                )
            connection.commit()
        except sqlite3.IntegrityError as exc:
            connection.rollback()
            raise _translate_integrity_error(exc, None) from exc
        except Exception:
            connection.rollback()
            raise
        finally:
            connection.close()
        return self._index_path

    def _initialize_index(self) -> None:
        connection = self._connect()
        try:
            connection.execute("PRAGMA journal_mode = WAL")
            connection.executescript(
                """
                CREATE TABLE IF NOT EXISTS trace_runs (
                    run_id TEXT PRIMARY KEY,
                    created_at TEXT NOT NULL
                );

                CREATE TABLE IF NOT EXISTS trace_records (
                    run_id TEXT NOT NULL,
                    ordinal INTEGER NOT NULL CHECK (ordinal >= 0),
                    kind TEXT NOT NULL,
                    object_key TEXT NOT NULL,
                    sha256 TEXT NOT NULL CHECK (length(sha256) = 64),
                    event_sequence INTEGER,
                    event_unit_id TEXT,
                    event_id TEXT,
                    created_at TEXT NOT NULL,
                    PRIMARY KEY (run_id, ordinal),
                    FOREIGN KEY (run_id) REFERENCES trace_runs(run_id)
                        ON DELETE CASCADE
                );

                CREATE UNIQUE INDEX IF NOT EXISTS ux_trace_records_event_id
                    ON trace_records(event_id)
                    WHERE event_id IS NOT NULL;
                CREATE INDEX IF NOT EXISTS ix_trace_records_run_sequence
                    ON trace_records(run_id, event_sequence)
                    WHERE event_sequence IS NOT NULL;
                CREATE INDEX IF NOT EXISTS ix_trace_records_run_unit
                    ON trace_records(run_id, event_unit_id)
                    WHERE event_unit_id IS NOT NULL;
                """
            )
            connection.commit()
        finally:
            connection.close()

    def _connect(self) -> sqlite3.Connection:
        connection = sqlite3.connect(
            self._index_path,
            timeout=self._sqlite_timeout_seconds,
        )
        connection.row_factory = sqlite3.Row
        connection.execute("PRAGMA foreign_keys = ON")
        connection.execute("PRAGMA synchronous = FULL")
        connection.execute(
            f"PRAGMA busy_timeout = {int(self._sqlite_timeout_seconds * 1000)}"
        )
        return connection

    def _prepare_record(self, record: AgentRunRecord) -> _PreparedRecord:
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
            event_unit_id = _optional_text(payload.get("unit_id"))
            event_id = _optional_text(payload.get("event_id"))
        created_at = _optional_text(payload.get("created_at")) or _utc_now_iso()
        return _PreparedRecord(
            record=record,
            data=encoded,
            sha256=digest,
            object_key=f"records/{digest[:2]}/{digest}.json",
            event_sequence=event_sequence,
            event_unit_id=event_unit_id,
            event_id=event_id,
            created_at=created_at,
        )

    @staticmethod
    def _insert_prepared(
        connection: sqlite3.Connection,
        *,
        run_id: str,
        ordinal: int,
        prepared: _PreparedRecord,
    ) -> None:
        connection.execute(
            """
            INSERT INTO trace_records(
                run_id,
                ordinal,
                kind,
                object_key,
                sha256,
                event_sequence,
                event_unit_id,
                event_id,
                created_at
            ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?)
            """,
            (
                run_id,
                ordinal,
                prepared.record.kind,
                prepared.object_key,
                prepared.sha256,
                prepared.event_sequence,
                prepared.event_unit_id,
                prepared.event_id,
                prepared.created_at,
            ),
        )

    def _read_indexed_record(
        self,
        run_id: str,
        row: sqlite3.Row,
    ) -> AgentRunRecord:
        object_key = str(row["object_key"])
        try:
            data = self._blob_store.get_bytes(object_key)
        except FileNotFoundError as exc:
            raise TraceBlobMissingError(
                "Trace blob is missing for "
                f"run_id={run_id!r}, ordinal={row['ordinal']}, "
                f"object_key={object_key!r}"
            ) from exc

        actual_digest = hashlib.sha256(data).hexdigest()
        expected_digest = str(row["sha256"])
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
        if record.kind != row["kind"]:
            raise TraceBlobIntegrityError(
                "Trace blob kind does not match its index for "
                f"run_id={run_id!r}, ordinal={row['ordinal']}: "
                f"expected {row['kind']!r}, got {record.kind!r}"
            )
        return record


def _validate_run_id(value: str) -> str:
    run_id = str(value or "")
    if not run_id.strip():
        raise ValueError("run_id must not be empty")
    return run_id


def _optional_text(value: object) -> str | None:
    if value is None:
        return None
    normalized = str(value).strip()
    return normalized or None


def validate_replacement_event_ids(records: Iterable[Any]) -> None:
    seen: dict[str, str] = {}
    for prepared in records:
        if prepared.event_id is None:
            continue
        previous_digest = seen.get(prepared.event_id)
        if previous_digest is None:
            seen[prepared.event_id] = prepared.sha256
            continue
        if previous_digest != prepared.sha256:
            raise TraceEventIdConflictError(
                "Replacement trace contains event_id with different content: "
                f"{prepared.event_id}"
            )
        raise TraceEventIdConflictError(
            f"Replacement trace contains duplicate event_id: {prepared.event_id}"
        )


def _translate_integrity_error(
    error: sqlite3.IntegrityError,
    event_id: str | None,
) -> ValueError:
    message = str(error)
    if "event_id" in message:
        suffix = f": {event_id}" if event_id else ""
        return TraceEventIdConflictError(
            f"Trace event_id is already indexed with different content{suffix}"
        )
    return ValueError(f"Invalid indexed trace record: {message}")


def _utc_now_iso() -> str:
    return datetime.now(timezone.utc).isoformat()  # noqa: UP017 -- Python 3.10


def _fsync_directory(path: Path) -> None:
    flags = os.O_RDONLY | getattr(os, "O_DIRECTORY", 0)
    descriptor = os.open(path, flags)
    try:
        os.fsync(descriptor)
    finally:
        os.close(descriptor)
