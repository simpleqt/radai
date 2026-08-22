"""Bounded retention GC for the LangGraph SQLite checkpoint database."""

import sqlite3

import pytest

from backend.application.checkpointing.gc import (
    CheckpointGcReport,
    gc_sqlite_checkpoints,
    resolve_default_checkpoint_db,
)

_SCHEMA = """
CREATE TABLE IF NOT EXISTS checkpoints (
    thread_id TEXT NOT NULL,
    checkpoint_ns TEXT NOT NULL DEFAULT '',
    checkpoint_id TEXT NOT NULL,
    parent_checkpoint_id TEXT,
    type TEXT,
    checkpoint BLOB,
    metadata BLOB,
    PRIMARY KEY (thread_id, checkpoint_ns, checkpoint_id)
);
CREATE TABLE IF NOT EXISTS writes (
    thread_id TEXT NOT NULL,
    checkpoint_ns TEXT NOT NULL DEFAULT '',
    checkpoint_id TEXT NOT NULL,
    task_id TEXT NOT NULL,
    idx INTEGER NOT NULL,
    channel TEXT NOT NULL,
    type TEXT,
    value BLOB,
    PRIMARY KEY (thread_id, checkpoint_ns, checkpoint_id, task_id, idx)
);
"""


def _make_db(tmp_path, *, threads: int, checkpoints_per_thread: int):
    db_path = tmp_path / "checkpoints.db"
    conn = sqlite3.connect(str(db_path))
    conn.executescript(_SCHEMA)
    with conn:
        for thread in range(threads):
            thread_id = f"thread-{thread}"
            for index in range(checkpoints_per_thread):
                # Lexicographically sortable ids, mirroring LangGraph UUID6s.
                checkpoint_id = f"1ef00000-0000-6000-8000-{index:012d}"
                conn.execute(
                    "INSERT INTO checkpoints VALUES (?, '', ?, NULL, 'msgpack', x'00', x'00')",
                    (thread_id, checkpoint_id),
                )
                conn.execute(
                    "INSERT INTO writes VALUES (?, '', ?, 'task', 0, 'messages', 'msgpack', x'00')",
                    (thread_id, checkpoint_id),
                )
    conn.close()
    return db_path


def _remaining(db_path):
    conn = sqlite3.connect(str(db_path))
    try:
        checkpoints = conn.execute(
            "SELECT thread_id, checkpoint_id FROM checkpoints ORDER BY thread_id, checkpoint_id"
        ).fetchall()
        writes = conn.execute(
            "SELECT thread_id, checkpoint_id FROM writes ORDER BY thread_id, checkpoint_id"
        ).fetchall()
        return checkpoints, writes
    finally:
        conn.close()


def test_gc_keeps_latest_n_per_thread_and_prunes_matching_writes(tmp_path):
    db_path = _make_db(tmp_path, threads=3, checkpoints_per_thread=7)

    report = gc_sqlite_checkpoints(db_path, keep_per_thread=2)

    assert isinstance(report, CheckpointGcReport)
    assert report.threads_total == 3
    assert report.checkpoints_total == 21
    assert report.checkpoints_deleted == 15
    assert report.writes_deleted == 15

    checkpoints, writes = _remaining(db_path)
    assert len(checkpoints) == 6
    assert len(writes) == 6
    # The two newest checkpoint ids per thread survive.
    for thread in range(3):
        ids = [cid for tid, cid in checkpoints if tid == f"thread-{thread}"]
        assert ids == [
            "1ef00000-0000-6000-8000-000000000005",
            "1ef00000-0000-6000-8000-000000000006",
        ]
    assert checkpoints == writes


def test_gc_dry_run_reports_without_deleting(tmp_path):
    db_path = _make_db(tmp_path, threads=2, checkpoints_per_thread=5)

    report = gc_sqlite_checkpoints(db_path, keep_per_thread=1, dry_run=True)

    assert report.dry_run is True
    assert report.checkpoints_deleted == 8
    assert report.writes_deleted == 8
    checkpoints, writes = _remaining(db_path)
    assert len(checkpoints) == 10
    assert len(writes) == 10


def test_gc_is_a_noop_when_threads_are_within_retention(tmp_path):
    db_path = _make_db(tmp_path, threads=2, checkpoints_per_thread=3)

    report = gc_sqlite_checkpoints(db_path, keep_per_thread=10)

    assert report.checkpoints_deleted == 0
    assert report.writes_deleted == 0
    checkpoints, _writes = _remaining(db_path)
    assert len(checkpoints) == 6


def test_gc_also_removes_writes_orphaned_by_earlier_deletions(tmp_path):
    db_path = _make_db(tmp_path, threads=1, checkpoints_per_thread=3)
    conn = sqlite3.connect(str(db_path))
    with conn:
        conn.execute(
            "INSERT INTO writes VALUES ('thread-0', '', 'orphan-checkpoint', 'task', 0, 'messages', 'msgpack', x'00')"
        )
    conn.close()

    report = gc_sqlite_checkpoints(db_path, keep_per_thread=10)

    assert report.checkpoints_deleted == 0
    assert report.writes_deleted == 1


def test_gc_rejects_retention_below_one(tmp_path):
    db_path = _make_db(tmp_path, threads=1, checkpoints_per_thread=1)

    with pytest.raises(ValueError):
        gc_sqlite_checkpoints(db_path, keep_per_thread=0)


def test_gc_rejects_missing_database(tmp_path):
    with pytest.raises(FileNotFoundError):
        gc_sqlite_checkpoints(tmp_path / "absent.db")


def test_gc_rejects_non_checkpoint_database(tmp_path):
    db_path = tmp_path / "other.db"
    conn = sqlite3.connect(str(db_path))
    conn.execute("CREATE TABLE misc (id INTEGER PRIMARY KEY)")
    conn.close()

    with pytest.raises(ValueError):
        gc_sqlite_checkpoints(db_path)


def test_default_db_path_honors_env_override(monkeypatch, tmp_path):
    monkeypatch.setenv("AGENT_CHECKPOINT_DATA_DIR", str(tmp_path))

    assert resolve_default_checkpoint_db() == tmp_path / "checkpoints.db"
