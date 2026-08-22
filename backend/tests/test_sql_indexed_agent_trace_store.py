from __future__ import annotations

import threading
from concurrent.futures import ThreadPoolExecutor

import pytest
from sqlalchemy import create_engine, event, inspect, select

from backend.application.agent_trace.indexed_store import (
    TraceBlobIntegrityError,
    TraceBlobMissingError,
    TraceBlobStore,
    TraceEventIdConflictError,
)
from backend.application.agent_trace.models import (
    AgentRunEvent,
    AgentRunRecord,
)
from backend.application.agent_trace.sql_index_store import (
    SQLAlchemyIndexedAgentTraceStore,
)
from backend.application.agent_trace.store import (
    AgentTraceStoreProtocol,
    create_agent_trace_store,
)
from backend.db.migrate import MIGRATIONS, _split_sql


class _MemoryBlobStore:
    def __init__(self) -> None:
        self.objects: dict[str, bytes] = {}
        self._lock = threading.Lock()

    def put_bytes(self, object_key: str, data: bytes) -> None:
        with self._lock:
            existing = self.objects.get(object_key)
            if existing is not None and existing != data:
                raise AssertionError("content-addressed key changed")
            self.objects[object_key] = data

    def get_bytes(self, object_key: str) -> bytes:
        try:
            return self.objects[object_key]
        except KeyError:
            raise FileNotFoundError(object_key) from None


def _engine(tmp_path):
    engine = create_engine(
        f"sqlite:///{tmp_path / 'shared-trace-index.sqlite3'}",
        connect_args={"timeout": 30, "check_same_thread": False},
    )

    @event.listens_for(engine, "connect")
    def configure_sqlite(dbapi_connection, _connection_record):
        cursor = dbapi_connection.cursor()
        try:
            cursor.execute("PRAGMA journal_mode = WAL")
            cursor.execute("PRAGMA foreign_keys = ON")
            cursor.execute("PRAGMA busy_timeout = 30000")
        finally:
            cursor.close()

    return engine


def _event(
    index: int,
    *,
    run_id: str = "run-1",
    event_id: str | None = None,
    value: str = "ok",
) -> AgentRunEvent:
    return AgentRunEvent(
        run_id=run_id,
        event_id=event_id or f"event-{index}",
        sequence=index,
        event_type="unit.completed",
        unit_id="unit-1",
        payload={"value": value, "large": "x" * 2_048},
        created_at="2026-07-11T01:02:03+00:00",
    )


def test_sql_trace_index_constructor_has_no_io_and_schema_is_explicit(tmp_path):
    engine = _engine(tmp_path)
    opened_connections: list[object] = []

    @event.listens_for(engine, "connect")
    def on_connect(dbapi_connection, _connection_record):
        opened_connections.append(dbapi_connection)

    blobs = _MemoryBlobStore()
    store = SQLAlchemyIndexedAgentTraceStore(engine, blob_store=blobs)

    assert isinstance(blobs, TraceBlobStore)
    assert isinstance(store, AgentTraceStoreProtocol)
    assert opened_connections == []

    store.create_schema()

    assert opened_connections
    inspector = inspect(engine)
    assert {
        "agent_trace_index_runs",
        "agent_trace_index_records",
    } <= set(inspector.get_table_names())
    columns = {
        column["name"] for column in inspector.get_columns("agent_trace_index_records")
    }
    assert columns == {
        "run_id",
        "ordinal",
        "kind",
        "object_key",
        "sha256",
        "event_sequence",
        "event_unit_id",
        "event_id",
        "created_at",
    }
    assert "payload" not in columns
    engine.dispose()


def test_trace_factory_reuses_shared_engine_for_sql_index(tmp_path, monkeypatch):
    engine = _engine(tmp_path)
    monkeypatch.setenv("AGENT_TRACE_BACKEND", "sql")
    monkeypatch.setenv("AGENT_TRACE_BLOB_BACKEND", "local")

    store = create_agent_trace_store(
        tmp_path / "factory-blobs",
        sql_engine=engine,
    )

    assert isinstance(store, SQLAlchemyIndexedAgentTraceStore)
    assert store.engine is engine
    store.create_schema()
    store.append_event(_event(1, run_id="factory-run"))
    assert store.read_records("factory-run")[0].payload["event_id"] == "event-1"
    engine.dispose()


def test_shared_trace_index_schema_is_a_tracked_mysql_migration():
    migration = next(
        item
        for item in MIGRATIONS
        if item.version == "202607110002_add_agent_trace_index_tables"
    )
    statements = _split_sql(migration.path.read_text(encoding="utf-8"))

    assert len(statements) == 2
    assert all("ENGINE=InnoDB" in statement for statement in statements)
    assert all("CHARSET=utf8mb4" in statement for statement in statements)
    assert any(
        "CREATE TABLE IF NOT EXISTS agent_trace_index_runs" in statement
        for statement in statements
    )
    assert any(
        "CREATE TABLE IF NOT EXISTS agent_trace_index_records" in statement
        for statement in statements
    )


def test_sql_trace_index_round_trip_reopen_and_global_event_id_semantics(tmp_path):
    engine = _engine(tmp_path)
    blobs = _MemoryBlobStore()
    store = SQLAlchemyIndexedAgentTraceStore(engine, blob_store=blobs)
    store.create_schema()
    first = _event(1, event_id="stable-event")

    store.append_event(first)
    store.append_event(first)

    reopened = SQLAlchemyIndexedAgentTraceStore(engine, blob_store=blobs)
    records = reopened.read_records("run-1")
    assert len(records) == 1
    assert records[0].payload["payload"]["large"] == "x" * 2_048
    assert len(blobs.objects) == 1

    with pytest.raises(TraceEventIdConflictError, match="stable-event"):
        reopened.append_event(_event(1, event_id="stable-event", value="changed"))
    with pytest.raises(TraceEventIdConflictError, match="stable-event"):
        reopened.append_event(_event(1, run_id="run-2", event_id="stable-event"))
    engine.dispose()


def test_sql_trace_index_concurrent_appends_allocate_one_contiguous_run_order(
    tmp_path,
):
    engine = _engine(tmp_path)
    blobs = _MemoryBlobStore()
    bootstrap = SQLAlchemyIndexedAgentTraceStore(engine, blob_store=blobs)
    bootstrap.create_schema()
    stores = [
        SQLAlchemyIndexedAgentTraceStore(
            engine,
            blob_store=blobs,
            max_transaction_retries=64,
        )
        for _ in range(8)
    ]
    events = [_event(index) for index in range(24)]

    def append(item):
        index, trace_event = item
        stores[index % len(stores)].append_event(trace_event)

    with ThreadPoolExecutor(max_workers=len(stores)) as pool:
        list(pool.map(append, enumerate(events)))

    records = bootstrap.read_records("run-1")
    assert len(records) == len(events)
    assert {record.payload["event_id"] for record in records} == {
        f"event-{index}" for index in range(24)
    }
    record_table = _record_table(engine)
    with engine.connect() as connection:
        ordinals = (
            connection.execute(
                select(record_table.c.ordinal)
                .where(record_table.c.run_id == "run-1")
                .order_by(record_table.c.ordinal)
            )
            .scalars()
            .all()
        )
    assert ordinals == list(range(24))
    engine.dispose()


def test_sql_trace_index_concurrent_exact_retry_is_one_record(tmp_path):
    engine = _engine(tmp_path)
    blobs = _MemoryBlobStore()
    store = SQLAlchemyIndexedAgentTraceStore(engine, blob_store=blobs)
    store.create_schema()
    stores = [
        SQLAlchemyIndexedAgentTraceStore(engine, blob_store=blobs) for _ in range(8)
    ]
    trace_event = _event(1, event_id="retry-event")

    with ThreadPoolExecutor(max_workers=len(stores)) as pool:
        list(pool.map(lambda item: item.append_event(trace_event), stores))

    assert len(store.read_records("run-1")) == 1
    engine.dispose()


def test_sql_trace_index_write_records_is_atomic_and_tracks_empty_runs(tmp_path):
    engine = _engine(tmp_path)
    blobs = _MemoryBlobStore()
    store = SQLAlchemyIndexedAgentTraceStore(engine, blob_store=blobs)
    store.create_schema()
    original = AgentRunRecord.from_dataclass("event", _event(1))
    replacement = AgentRunRecord(
        kind="handoff",
        payload={"run_id": "run-1", "summary": "replacement"},
    )

    assert store.write_records("run-1", [original]) == store.index_path
    store.write_records("run-1", [replacement])
    store.write_records("run-empty", [])

    assert store.read_records("run-1") == [replacement]
    assert store.read_records("run-empty") == []
    assert store.list_run_ids() == ["run-1", "run-empty"]
    engine.dispose()


def test_sql_trace_index_failed_replacement_rolls_back_existing_trace(tmp_path):
    engine = _engine(tmp_path)
    blobs = _MemoryBlobStore()
    store = SQLAlchemyIndexedAgentTraceStore(engine, blob_store=blobs)
    store.create_schema()
    original = AgentRunRecord(
        kind="handoff",
        payload={"run_id": "run-1", "summary": "keep"},
    )
    store.write_records("run-1", [original])
    store.append_event(_event(2, run_id="run-2", event_id="owned-by-run-2"))
    conflicting = AgentRunRecord.from_dataclass(
        "event",
        _event(3, run_id="run-1", event_id="owned-by-run-2"),
    )

    with pytest.raises(TraceEventIdConflictError, match="owned-by-run-2"):
        store.write_records("run-1", [conflicting])

    assert store.read_records("run-1") == [original]
    engine.dispose()


def test_sql_trace_index_detects_blob_corruption_missing_blob_and_ordinal_gap(
    tmp_path,
):
    engine = _engine(tmp_path)
    blobs = _MemoryBlobStore()
    store = SQLAlchemyIndexedAgentTraceStore(engine, blob_store=blobs)
    store.create_schema()
    store.append_event(_event(1))
    object_key = next(iter(blobs.objects))
    original = blobs.objects[object_key]

    blobs.objects[object_key] = b"corrupt"
    with pytest.raises(TraceBlobIntegrityError, match="SHA-256 mismatch"):
        store.read_records("run-1")

    blobs.objects[object_key] = original
    del blobs.objects[object_key]
    with pytest.raises(TraceBlobMissingError, match=r"run_id='run-1'.*ordinal=0"):
        store.read_records("run-1")

    blobs.objects[object_key] = original
    records = _record_table(engine)
    with engine.begin() as connection:
        connection.execute(
            records.update().where(records.c.run_id == "run-1").values(ordinal=1)
        )
    with pytest.raises(TraceBlobIntegrityError, match="ordinal gap"):
        store.read_records("run-1")
    engine.dispose()


def test_sql_trace_index_rejects_oversized_index_identifiers_before_blob_write(
    tmp_path,
):
    engine = _engine(tmp_path)
    blobs = _MemoryBlobStore()
    store = SQLAlchemyIndexedAgentTraceStore(engine, blob_store=blobs)
    store.create_schema()

    with pytest.raises(ValueError, match="191"):
        store.append_event(_event(1, event_id="e" * 192))
    with pytest.raises(ValueError, match="191"):
        store.append_event(_event(1, run_id="r" * 192))
    assert blobs.objects == {}
    engine.dispose()


def _record_table(engine):
    from sqlalchemy import MetaData, Table

    return Table(
        "agent_trace_index_records",
        MetaData(),
        autoload_with=engine,
    )
