import json
import sqlite3
from concurrent.futures import ThreadPoolExecutor

import pytest

from backend.application.agent_trace.indexed_store import (
    IndexedAgentTraceStore,
    LocalFileTraceBlobStore,
    MinioTraceBlobStore,
    TraceBlobIntegrityError,
    TraceBlobMissingError,
    TraceBlobStore,
    TraceEventIdConflictError,
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
from backend.application.agent_trace.store import (
    AgentTraceStoreProtocol,
    create_agent_trace_store,
)


def _event(
    *,
    run_id: str,
    event_id: str,
    sequence: int,
    value: str = "ok",
) -> AgentRunEvent:
    return AgentRunEvent(
        run_id=run_id,
        event_id=event_id,
        sequence=sequence,
        event_type="unit.completed",
        unit_id="unit-1",
        payload={"value": value, "large": "x" * 4_096},
        created_at="2026-07-11T01:02:03+00:00",
    )


def test_indexed_trace_store_reopens_in_append_order_and_keeps_payload_in_blob(
    tmp_path,
):
    root = tmp_path / "indexed-traces"
    store = IndexedAgentTraceStore(root)

    store.append_run(
        AgentRunTrace(
            run_id="run-b",
            request_id="request-1",
            user_id="user-1",
            thread_id="thread-1",
            agent_code="researcher",
            workflow="plan_solve",
        )
    )
    store.append_event(_event(run_id="run-b", event_id="event-9", sequence=9))
    store.append_unit(
        AgentRunUnit(
            unit_id="unit-1",
            run_id="run-b",
            role="researcher",
            agent_type="deep-research",
            task_title="核验",
            task_question="核验结论",
        )
    )

    reopened = IndexedAgentTraceStore(root)
    records = reopened.read_records("run-b")

    assert [record.kind for record in records] == ["run", "event", "unit"]
    assert records[1].payload["sequence"] == 9
    assert records[1].payload["payload"]["large"] == "x" * 4_096

    with sqlite3.connect(reopened.index_path) as connection:
        columns = {
            row[1] for row in connection.execute("PRAGMA table_info(trace_records)")
        }
        rows = connection.execute(
            """
            SELECT ordinal, kind, object_key, sha256,
                   event_sequence, event_unit_id, event_id, created_at
            FROM trace_records
            WHERE run_id = ?
            ORDER BY ordinal
            """,
            ("run-b",),
        ).fetchall()

    assert "payload" not in columns
    assert [row[0] for row in rows] == [0, 1, 2]
    assert rows[1][4:] == (
        9,
        "unit-1",
        "event-9",
        "2026-07-11T01:02:03+00:00",
    )
    assert len(rows[1][3]) == 64

    blob_store = reopened.blob_store
    assert isinstance(blob_store, TraceBlobStore)
    blob = json.loads(blob_store.get_bytes(rows[1][2]).decode("utf-8"))
    assert blob == records[1].to_json_dict()


def test_indexed_trace_store_supports_all_append_facades(tmp_path):
    store = IndexedAgentTraceStore(tmp_path)
    run_id = "run-all"

    store.append_run(
        AgentRunTrace(
            run_id=run_id,
            request_id="request-1",
            user_id="user-1",
            thread_id="thread-1",
            agent_code="researcher",
            workflow="plan_solve",
        )
    )
    store.append_event(_event(run_id=run_id, event_id="event-all", sequence=1))
    store.append_unit(
        AgentRunUnit(
            unit_id="unit-1",
            run_id=run_id,
            role="researcher",
            agent_type="deep-research",
            task_title="核验",
            task_question="核验结论",
        )
    )
    store.append_tool_call(
        AgentRunToolCall(
            tool_call_id="tool-1",
            run_id=run_id,
            tool_name="search",
            status="completed",
        )
    )
    store.append_context_pack(
        AgentRunContextPack(
            pack_id="pack-1",
            run_id=run_id,
            unit_id="unit-1",
            model_call_index=1,
            base_tokens=10,
            dynamic_token_budget=20,
            dynamic_tokens_used=5,
            items_total=1,
            items_injected=1,
        )
    )
    store.append_compaction(
        AgentRunCompaction(
            compaction_id="compaction-1",
            run_id=run_id,
            unit_id="unit-1",
            model_call_index=1,
            trigger="budget",
            window_id="window-1",
            previous_window_id=None,
            tokens_before=100,
            tokens_after=40,
            summary_tokens=20,
            retained_user_messages=1,
            removed_messages=2,
        )
    )
    store.append_handoff(
        AgentRunHandoff(
            handoff_id="handoff-1",
            run_id=run_id,
            unit_id="unit-1",
            role="researcher",
            payload={"summary": "完成"},
        )
    )

    assert [record.kind for record in store.read_records(run_id)] == [
        "run",
        "event",
        "unit",
        "tool_call",
        "context_pack",
        "compaction",
        "handoff",
    ]


def test_indexed_trace_store_event_id_is_idempotent_and_conflicts_are_clear(
    tmp_path,
):
    store = IndexedAgentTraceStore(tmp_path)
    event = _event(run_id="run-1", event_id="event-1", sequence=1)

    store.append_event(event)
    store.append_event(event)

    assert len(store.read_records("run-1")) == 1

    with pytest.raises(TraceEventIdConflictError, match="event-1"):
        store.append_event(
            _event(
                run_id="run-1",
                event_id="event-1",
                sequence=1,
                value="different",
            )
        )


def test_indexed_trace_store_concurrent_event_retry_is_idempotent(tmp_path):
    stores = [IndexedAgentTraceStore(tmp_path) for _ in range(8)]
    event = _event(run_id="run-concurrent", event_id="event-concurrent", sequence=1)

    with ThreadPoolExecutor(max_workers=len(stores)) as pool:
        list(pool.map(lambda store: store.append_event(event), stores))

    assert len(stores[0].read_records("run-concurrent")) == 1


def test_indexed_trace_store_detects_hash_corruption(tmp_path):
    store = IndexedAgentTraceStore(tmp_path)
    store.append_event(_event(run_id="run-1", event_id="event-1", sequence=1))

    with sqlite3.connect(store.index_path) as connection:
        object_key = connection.execute(
            "SELECT object_key FROM trace_records WHERE run_id = 'run-1'"
        ).fetchone()[0]
    blob_store = store.blob_store
    assert isinstance(blob_store, LocalFileTraceBlobStore)
    blob_store.path_for_key(object_key).write_bytes(b"corrupted")

    with pytest.raises(TraceBlobIntegrityError, match="SHA-256 mismatch"):
        store.read_records("run-1")


def test_indexed_trace_store_reports_missing_blob(tmp_path):
    store = IndexedAgentTraceStore(tmp_path)
    store.append_event(_event(run_id="run-1", event_id="event-1", sequence=1))

    with sqlite3.connect(store.index_path) as connection:
        object_key = connection.execute(
            "SELECT object_key FROM trace_records WHERE run_id = 'run-1'"
        ).fetchone()[0]
    blob_store = store.blob_store
    assert isinstance(blob_store, LocalFileTraceBlobStore)
    blob_store.path_for_key(object_key).unlink()

    with pytest.raises(
        TraceBlobMissingError,
        match=r"run_id='run-1'.*ordinal=0",
    ):
        store.read_records("run-1")


def test_indexed_trace_store_write_records_replaces_atomically_and_tracks_empty_runs(
    tmp_path,
):
    store = IndexedAgentTraceStore(tmp_path)
    original = AgentRunRecord.from_dataclass(
        "event",
        _event(run_id="run-a", event_id="event-a", sequence=1),
    )
    replacement = AgentRunRecord(
        kind="handoff",
        payload={"run_id": "run-a", "summary": "replacement"},
    )

    assert store.write_records("run-a", [original]) == store.index_path
    store.write_records("run-a", [replacement])
    store.write_records("run-empty", [])

    assert store.read_records("run-a") == [replacement]
    assert store.read_records("run-empty") == []
    assert store.list_run_ids() == ["run-a", "run-empty"]


def test_indexed_trace_store_lists_runs_after_reopen(tmp_path):
    root = tmp_path / "indexed-traces"
    store = IndexedAgentTraceStore(root)
    store.append_event(_event(run_id="run-z", event_id="event-z", sequence=2))
    store.append_event(_event(run_id="run-a", event_id="event-a", sequence=1))

    reopened = IndexedAgentTraceStore(root)

    assert reopened.list_run_ids() == ["run-a", "run-z"]


def test_indexed_trace_store_recovers_when_interrupted_after_blob_write(
    tmp_path,
    monkeypatch,
):
    root = tmp_path / "indexed-traces"
    store = IndexedAgentTraceStore(root)
    event = _event(run_id="run-retry", event_id="event-retry", sequence=1)

    def fail_before_index_transaction():
        raise RuntimeError("simulated index interruption")

    monkeypatch.setattr(store, "_connect", fail_before_index_transaction)
    with pytest.raises(RuntimeError, match="simulated index interruption"):
        store.append_event(event)

    blob_files = list((root / "objects").glob("records/*/*.json"))
    assert len(blob_files) == 1

    reopened = IndexedAgentTraceStore(root)
    assert reopened.list_run_ids() == []
    reopened.append_event(event)

    assert [
        record.payload["event_id"] for record in reopened.read_records("run-retry")
    ] == ["event-retry"]


def test_trace_store_factory_selects_indexed_backend_explicitly(
    tmp_path,
    monkeypatch,
):
    monkeypatch.setenv("AGENT_TRACE_BACKEND", "indexed")
    monkeypatch.setenv("AGENT_TRACE_BLOB_BACKEND", "local")

    store = create_agent_trace_store(tmp_path / "factory-indexed")
    store.append_event(
        _event(run_id="factory-run", event_id="factory-event", sequence=1)
    )

    assert isinstance(store, AgentTraceStoreProtocol)
    assert isinstance(store, IndexedAgentTraceStore)
    assert store.read_records("factory-run")[0].payload["event_id"] == "factory-event"


class _FakeMinioResponse:
    def __init__(self, data: bytes) -> None:
        self._data = data
        self.closed = False
        self.released = False

    def read(self) -> bytes:
        return self._data

    def close(self) -> None:
        self.closed = True

    def release_conn(self) -> None:
        self.released = True


class _FakeMinioError(Exception):
    def __init__(self, code: str) -> None:
        super().__init__(code)
        self.code = code


class _FakeMinioClient:
    def __init__(self) -> None:
        self.objects: dict[tuple[str, str], bytes] = {}
        self.last_response: _FakeMinioResponse | None = None

    def put_object(
        self,
        bucket: str,
        object_name: str,
        data,
        length: int,
        **_kwargs,
    ) -> None:
        self.objects[(bucket, object_name)] = data.read(length)

    def get_object(self, bucket: str, object_name: str) -> _FakeMinioResponse:
        try:
            data = self.objects[(bucket, object_name)]
        except KeyError:
            raise _FakeMinioError("NoSuchKey") from None
        self.last_response = _FakeMinioResponse(data)
        return self.last_response


def test_minio_trace_blob_store_round_trip_and_missing_mapping():
    client = _FakeMinioClient()
    store = MinioTraceBlobStore(client, bucket="trace-bucket", prefix="runtime/v1")

    store.put_bytes("records/ab/digest.json", b'{"ok":true}')

    assert store.get_bytes("records/ab/digest.json") == b'{"ok":true}'
    assert client.objects == {
        ("trace-bucket", "runtime/v1/records/ab/digest.json"): b'{"ok":true}'
    }
    assert client.last_response is not None
    assert client.last_response.closed is True
    assert client.last_response.released is True
    with pytest.raises(FileNotFoundError, match="missing.json"):
        store.get_bytes("records/ab/missing.json")


def test_trace_store_factory_selects_minio_blob_backend(
    tmp_path,
    monkeypatch,
):
    import backend.storage as storage

    client = _FakeMinioClient()
    monkeypatch.setenv("AGENT_TRACE_BACKEND", "indexed")
    monkeypatch.setenv("AGENT_TRACE_BLOB_BACKEND", "minio")
    monkeypatch.setenv("AGENT_TRACE_MINIO_BUCKET", "trace-bucket")
    monkeypatch.setenv("AGENT_TRACE_MINIO_PREFIX", "trace-prefix")
    monkeypatch.setattr(storage, "_get_minio_client", lambda: client)

    store = create_agent_trace_store(tmp_path / "factory-minio")
    store.append_event(
        _event(run_id="factory-run", event_id="factory-event", sequence=1)
    )

    assert isinstance(store, IndexedAgentTraceStore)
    assert isinstance(store.blob_store, MinioTraceBlobStore)
    assert store.read_records("factory-run")[0].payload["event_id"] == "factory-event"
    assert next(iter(client.objects))[1].startswith("trace-prefix/records/")
