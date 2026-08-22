"""Bounded retention GC for the LangGraph SQLite checkpoint database.

``AsyncSqliteSaver`` keeps every checkpoint of every super-step forever; the
only built-in cleanup is ``adelete_thread`` when a conversation is deleted.
Long-lived threads therefore accumulate one full copy of the state channels
per step. This module trims each ``(thread_id, checkpoint_ns)`` pair down to
its most recent N checkpoints without touching the latest one, so resume,
compaction recovery, and recent rollback keep working.

LangGraph checkpoint ids are lexicographically sortable (the saver itself
resolves "latest" with ``ORDER BY checkpoint_id DESC``), which makes
count-based retention safe without parsing checkpoint blobs.

``doc-agent checkpoints-gc`` is the manual maintenance entry point. The Agent
API can additionally run this collector (together with the COS GC) on a fixed
interval through ``backend.services.context_maintenance`` when
``CONTEXT_MAINTENANCE_GC_INTERVAL_SECONDS`` is set; enabling that automatic
deletion remains a deployment decision (default off).
"""

from __future__ import annotations

import os
import sqlite3
from dataclasses import asdict, dataclass
from pathlib import Path

DEFAULT_KEEP_PER_THREAD = 50


@dataclass(frozen=True)
class CheckpointGcReport:
    db_path: str
    keep_per_thread: int
    dry_run: bool
    threads_total: int
    checkpoints_total: int
    checkpoints_deleted: int
    writes_deleted: int
    vacuumed: bool

    def as_dict(self) -> dict:
        return asdict(self)


def resolve_default_checkpoint_db(db_name: str = "checkpoints.db") -> Path:
    """Mirror ``init_sqlite_checkpointer`` path resolution for CLI use."""

    data_dir = os.getenv("AGENT_CHECKPOINT_DATA_DIR") or None
    base = (
        Path(data_dir)
        if data_dir
        else Path(__file__).resolve().parents[2] / "data"
    )
    return base / db_name


_DOOMED_CHECKPOINTS_SUBQUERY = """
SELECT rowid FROM (
    SELECT rowid, ROW_NUMBER() OVER (
        PARTITION BY thread_id, checkpoint_ns
        ORDER BY checkpoint_id DESC
    ) AS rn
    FROM checkpoints
) WHERE rn > :keep
"""

_ORPHAN_WRITES_PREDICATE = """
NOT EXISTS (
    SELECT 1 FROM checkpoints AS c
    WHERE c.thread_id = writes.thread_id
      AND c.checkpoint_ns = writes.checkpoint_ns
      AND c.checkpoint_id = writes.checkpoint_id
)
"""

# Checkpoint rows carry full state-channel blobs (potentially hundreds of KB),
# so their delete batches stay small; writes rows are much lighter.
_CHECKPOINT_DELETE_BATCH = 500
_WRITES_DELETE_BATCH = 5000


def gc_sqlite_checkpoints(
    db_path: str | Path,
    *,
    keep_per_thread: int = DEFAULT_KEEP_PER_THREAD,
    dry_run: bool = False,
    vacuum: bool = False,
) -> CheckpointGcReport:
    """Trim every thread to its most recent ``keep_per_thread`` checkpoints.

    The newest checkpoint per thread is always retained (``keep_per_thread``
    must be at least 1), and writes are removed only when their checkpoint row
    no longer exists. ``vacuum`` reclaims disk space afterwards but briefly
    blocks concurrent writers, so it stays opt-in.
    """

    keep = int(keep_per_thread)
    if keep < 1:
        raise ValueError("keep_per_thread must be >= 1 to preserve resumability")
    path = Path(db_path)
    if not path.exists():
        raise FileNotFoundError(f"checkpoint database not found: {path}")

    conn = sqlite3.connect(str(path))
    try:
        conn.execute("PRAGMA busy_timeout=5000")
        tables = {
            row[0]
            for row in conn.execute(
                "SELECT name FROM sqlite_master WHERE type='table'"
            )
        }
        if "checkpoints" not in tables:
            raise ValueError(
                f"{path} has no 'checkpoints' table; not a LangGraph checkpoint DB"
            )
        has_writes = "writes" in tables

        threads_total = int(
            conn.execute(
                "SELECT COUNT(*) FROM ("
                "SELECT DISTINCT thread_id, checkpoint_ns FROM checkpoints)"
            ).fetchone()[0]
        )
        checkpoints_total = int(
            conn.execute("SELECT COUNT(*) FROM checkpoints").fetchone()[0]
        )
        doomed_checkpoints = int(
            conn.execute(
                f"SELECT COUNT(*) FROM checkpoints WHERE rowid IN ({_DOOMED_CHECKPOINTS_SUBQUERY})",
                {"keep": keep},
            ).fetchone()[0]
        )

        if dry_run:
            doomed_writes = 0
            if has_writes and doomed_checkpoints:
                # Writes whose checkpoint row is scheduled for deletion, plus
                # writes already orphaned by earlier manual deletions.
                doomed_writes = int(
                    conn.execute(
                        "SELECT COUNT(*) FROM writes WHERE "
                        "EXISTS (SELECT 1 FROM checkpoints AS c WHERE "
                        "c.thread_id = writes.thread_id AND "
                        "c.checkpoint_ns = writes.checkpoint_ns AND "
                        "c.checkpoint_id = writes.checkpoint_id AND "
                        f"c.rowid IN ({_DOOMED_CHECKPOINTS_SUBQUERY}))"
                        f" OR {_ORPHAN_WRITES_PREDICATE}",
                        {"keep": keep},
                    ).fetchone()[0]
                )
            elif has_writes:
                doomed_writes = int(
                    conn.execute(
                        f"SELECT COUNT(*) FROM writes WHERE {_ORPHAN_WRITES_PREDICATE}"
                    ).fetchone()[0]
                )
            return CheckpointGcReport(
                db_path=str(path),
                keep_per_thread=keep,
                dry_run=True,
                threads_total=threads_total,
                checkpoints_total=checkpoints_total,
                checkpoints_deleted=doomed_checkpoints,
                writes_deleted=doomed_writes,
                vacuumed=False,
            )

        # Delete in small per-batch transactions: one monolithic transaction
        # over a large backlog (first run on a long-lived database) holds the
        # write lock long enough to push live streams' checkpoint writes past
        # their 5s busy_timeout. Each batch recomputes the doomed set against
        # the live table, so the newest ``keep`` checkpoints per thread stay
        # protected even while writers interleave between batches; a crash
        # between batches only leaves orphan writes for the next run.
        checkpoints_deleted = 0
        while True:
            with conn:
                cursor = conn.execute(
                    "DELETE FROM checkpoints WHERE rowid IN ("
                    f"SELECT rowid FROM ({_DOOMED_CHECKPOINTS_SUBQUERY}) "
                    "LIMIT :batch)",
                    {"keep": keep, "batch": _CHECKPOINT_DELETE_BATCH},
                )
                deleted = int(cursor.rowcount or 0)
            checkpoints_deleted += deleted
            if deleted < _CHECKPOINT_DELETE_BATCH:
                break
        writes_deleted = 0
        if has_writes:
            while True:
                with conn:
                    cursor = conn.execute(
                        "DELETE FROM writes WHERE rowid IN ("
                        "SELECT rowid FROM writes "
                        f"WHERE {_ORPHAN_WRITES_PREDICATE} "
                        "LIMIT :batch)",
                        {"batch": _WRITES_DELETE_BATCH},
                    )
                    deleted = int(cursor.rowcount or 0)
                writes_deleted += deleted
                if deleted < _WRITES_DELETE_BATCH:
                    break

        vacuumed = False
        if vacuum and (checkpoints_deleted or writes_deleted):
            conn.execute("VACUUM")
            vacuumed = True

        return CheckpointGcReport(
            db_path=str(path),
            keep_per_thread=keep,
            dry_run=False,
            threads_total=threads_total,
            checkpoints_total=checkpoints_total,
            checkpoints_deleted=checkpoints_deleted,
            writes_deleted=writes_deleted,
            vacuumed=vacuumed,
        )
    finally:
        conn.close()
