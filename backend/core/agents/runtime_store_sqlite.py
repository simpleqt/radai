"""Agent 运行时 SQLite 存储:schema、序列化与行映射。(拆自 runtime_store.py)"""

from __future__ import annotations

from collections.abc import Mapping
from pathlib import Path
from typing import Any
import asyncio
import json
import os
import sqlite3
import time
from backend.core.agents.runtime_store_model import (
    AgentLifecycleEvent,
    AgentRunSnapshot,
    AgentRuntimeSnapshot,
    AgentRuntimeStoreError,
    AgentRuntimeVersionConflict,
    _clone_agent,
    _clone_run,
    _new_event_id,
    _normalize_expected_version,
    _optional_text,
    _payload_copy,
    _required_text,
    _resolve_upsert,
)


def _json_dumps(value: Mapping[str, Any]) -> str:
    return json.dumps(
        dict(value),
        ensure_ascii=False,
        separators=(",", ":"),
        sort_keys=True,
    )


def _json_loads(value: str) -> dict[str, Any]:
    parsed = json.loads(value or "{}")
    if not isinstance(parsed, dict):
        raise AgentRuntimeStoreError("stored JSON value is not an object")
    return parsed


_SCHEMA_STATEMENTS = (
    """
    CREATE TABLE IF NOT EXISTS agent_runtime_runs (
        root_id TEXT PRIMARY KEY,
        status TEXT NOT NULL,
        metadata TEXT NOT NULL,
        created_at REAL NOT NULL,
        updated_at REAL NOT NULL,
        started_at REAL,
        finished_at REAL,
        error TEXT,
        version INTEGER NOT NULL CHECK (version >= 1)
    )
    """,
    """
    CREATE TABLE IF NOT EXISTS agent_runtime_agents (
        root_id TEXT NOT NULL,
        agent_id TEXT NOT NULL,
        parent_id TEXT,
        depth INTEGER NOT NULL CHECK (depth >= 0),
        status TEXT NOT NULL,
        metadata TEXT NOT NULL,
        created_at REAL NOT NULL,
        updated_at REAL NOT NULL,
        started_at REAL,
        finished_at REAL,
        error TEXT,
        version INTEGER NOT NULL CHECK (version >= 1),
        PRIMARY KEY (root_id, agent_id)
    )
    """,
    """
    CREATE TABLE IF NOT EXISTS agent_runtime_events (
        root_id TEXT NOT NULL,
        sequence INTEGER NOT NULL CHECK (sequence >= 1),
        event_id TEXT NOT NULL,
        agent_id TEXT,
        event_type TEXT NOT NULL,
        payload TEXT NOT NULL,
        created_at REAL NOT NULL,
        PRIMARY KEY (root_id, sequence),
        UNIQUE (root_id, event_id)
    )
    """,
    """
    CREATE INDEX IF NOT EXISTS idx_agent_runtime_agents_tree
    ON agent_runtime_agents (root_id, depth, created_at, agent_id)
    """,
    """
    CREATE INDEX IF NOT EXISTS idx_agent_runtime_events_agent
    ON agent_runtime_events (root_id, agent_id, sequence)
    """,
)


class SQLiteAgentRuntimeStore:
    """File-backed SQLite runtime store with transactional CAS updates.

    ``path`` is mandatory: this class never consults project database settings
    and never chooses a default location. Each operation uses a short-lived
    connection in ``asyncio.to_thread`` so blocking sqlite calls stay off the
    event loop.
    """

    def __init__(
        self,
        path: str | os.PathLike[str],
        *,
        busy_timeout_ms: int = 5_000,
    ) -> None:
        normalized_path = os.fspath(path)
        if not str(normalized_path).strip():
            raise ValueError("path must not be empty")
        if normalized_path == ":memory:":
            raise ValueError(
                "SQLiteAgentRuntimeStore requires a file path; use "
                "InMemoryAgentRuntimeStore for ephemeral state"
            )
        timeout = int(busy_timeout_ms)
        if timeout < 1:
            raise ValueError("busy_timeout_ms must be at least 1")
        self.path = Path(normalized_path)
        self.busy_timeout_ms = timeout
        self._initialized = False
        self._initialize_lock = asyncio.Lock()

    def _connect(self) -> sqlite3.Connection:
        connection = sqlite3.connect(
            self.path,
            timeout=self.busy_timeout_ms / 1000,
            isolation_level=None,
        )
        connection.row_factory = sqlite3.Row
        connection.execute(f"PRAGMA busy_timeout = {self.busy_timeout_ms}")
        return connection

    def _initialize_sync(self) -> None:
        connection = self._connect()
        try:
            connection.execute("PRAGMA journal_mode = WAL")
            connection.execute("PRAGMA synchronous = NORMAL")
            connection.execute("BEGIN IMMEDIATE")
            try:
                for statement in _SCHEMA_STATEMENTS:
                    connection.execute(statement)
                connection.commit()
            except Exception:
                connection.rollback()
                raise
        finally:
            connection.close()

    async def _ensure_initialized(self) -> None:
        if self._initialized:
            return
        async with self._initialize_lock:
            if self._initialized:
                return
            await asyncio.to_thread(self._initialize_sync)
            self._initialized = True

    async def upsert_run(
        self,
        snapshot: AgentRunSnapshot,
        *,
        expected_version: int | None = None,
    ) -> AgentRunSnapshot:
        await self._ensure_initialized()
        incoming = _clone_run(snapshot)
        expected = _normalize_expected_version(expected_version)
        return await asyncio.to_thread(
            self._upsert_run_sync,
            incoming,
            expected,
        )

    async def upsert_tree(
        self,
        snapshot: AgentRunSnapshot,
        *,
        expected_version: int | None = None,
    ) -> AgentRunSnapshot:
        return await self.upsert_run(
            snapshot,
            expected_version=expected_version,
        )

    def _upsert_run_sync(
        self,
        incoming: AgentRunSnapshot,
        expected_version: int | None,
    ) -> AgentRunSnapshot:
        connection = self._connect()
        try:
            connection.execute("BEGIN IMMEDIATE")
            try:
                row = connection.execute(
                    "SELECT * FROM agent_runtime_runs WHERE root_id = ?",
                    (incoming.root_id,),
                ).fetchone()
                current = None if row is None else _row_to_run(row)
                resolved, changed = _resolve_upsert(
                    current,
                    incoming,
                    expected_version=expected_version,
                    identity=f"run {incoming.root_id}",
                )
                assert isinstance(resolved, AgentRunSnapshot)
                if changed:
                    previous_version = -1 if current is None else current.version
                    cursor = connection.execute(
                        """
                        INSERT INTO agent_runtime_runs (
                            root_id, status, metadata, created_at, updated_at,
                            started_at, finished_at, error, version
                        ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?)
                        ON CONFLICT(root_id) DO UPDATE SET
                            status = excluded.status,
                            metadata = excluded.metadata,
                            created_at = excluded.created_at,
                            updated_at = excluded.updated_at,
                            started_at = excluded.started_at,
                            finished_at = excluded.finished_at,
                            error = excluded.error,
                            version = excluded.version
                        WHERE agent_runtime_runs.version = ?
                        """,
                        _run_values(resolved) + (previous_version,),
                    )
                    if cursor.rowcount != 1:
                        raise AgentRuntimeVersionConflict(
                            f"run {incoming.root_id} changed during upsert"
                        )
                connection.commit()
                return _clone_run(resolved)
            except Exception:
                connection.rollback()
                raise
        finally:
            connection.close()

    async def load_run(self, root_id: str) -> AgentRunSnapshot | None:
        await self._ensure_initialized()
        normalized_root = _required_text(root_id, field_name="root_id")
        return await asyncio.to_thread(self._load_run_sync, normalized_root)

    def _load_run_sync(self, root_id: str) -> AgentRunSnapshot | None:
        connection = self._connect()
        try:
            row = connection.execute(
                "SELECT * FROM agent_runtime_runs WHERE root_id = ?",
                (root_id,),
            ).fetchone()
            return None if row is None else _row_to_run(row)
        finally:
            connection.close()

    async def upsert_agent(
        self,
        snapshot: AgentRuntimeSnapshot,
        *,
        expected_version: int | None = None,
    ) -> AgentRuntimeSnapshot:
        await self._ensure_initialized()
        incoming = _clone_agent(snapshot)
        expected = _normalize_expected_version(expected_version)
        return await asyncio.to_thread(
            self._upsert_agent_sync,
            incoming,
            expected,
        )

    def _upsert_agent_sync(
        self,
        incoming: AgentRuntimeSnapshot,
        expected_version: int | None,
    ) -> AgentRuntimeSnapshot:
        connection = self._connect()
        try:
            connection.execute("BEGIN IMMEDIATE")
            try:
                row = connection.execute(
                    """
                    SELECT * FROM agent_runtime_agents
                    WHERE root_id = ? AND agent_id = ?
                    """,
                    (incoming.root_id, incoming.agent_id),
                ).fetchone()
                current = None if row is None else _row_to_agent(row)
                resolved, changed = _resolve_upsert(
                    current,
                    incoming,
                    expected_version=expected_version,
                    identity=f"agent {incoming.root_id}/{incoming.agent_id}",
                )
                assert isinstance(resolved, AgentRuntimeSnapshot)
                if changed:
                    previous_version = -1 if current is None else current.version
                    cursor = connection.execute(
                        """
                        INSERT INTO agent_runtime_agents (
                            root_id, agent_id, parent_id, depth, status,
                            metadata, created_at, updated_at, started_at,
                            finished_at, error, version
                        ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
                        ON CONFLICT(root_id, agent_id) DO UPDATE SET
                            parent_id = excluded.parent_id,
                            depth = excluded.depth,
                            status = excluded.status,
                            metadata = excluded.metadata,
                            created_at = excluded.created_at,
                            updated_at = excluded.updated_at,
                            started_at = excluded.started_at,
                            finished_at = excluded.finished_at,
                            error = excluded.error,
                            version = excluded.version
                        WHERE agent_runtime_agents.version = ?
                        """,
                        _agent_values(resolved) + (previous_version,),
                    )
                    if cursor.rowcount != 1:
                        raise AgentRuntimeVersionConflict(
                            "agent "
                            f"{incoming.root_id}/{incoming.agent_id} changed "
                            "during upsert"
                        )
                connection.commit()
                return _clone_agent(resolved)
            except Exception:
                connection.rollback()
                raise
        finally:
            connection.close()

    async def load_tree_agents(
        self,
        root_id: str,
    ) -> list[AgentRuntimeSnapshot]:
        await self._ensure_initialized()
        normalized_root = _required_text(root_id, field_name="root_id")
        return await asyncio.to_thread(
            self._load_tree_agents_sync,
            normalized_root,
        )

    async def upsert_agent_with_event(
        self,
        snapshot: AgentRuntimeSnapshot,
        *,
        expected_version: int | None = None,
        event_type: str,
        event_agent_id: str | None = None,
        event_payload: Mapping[str, Any] | None = None,
        event_id: str | None = None,
        event_created_at: float | None = None,
    ) -> tuple[AgentRuntimeSnapshot, AgentLifecycleEvent]:
        """Commit one agent upsert and its lifecycle event in one transaction.

        The separate ``upsert_agent`` + ``append_lifecycle_event`` calls each
        opened their own transaction, so a crash between them left the agent
        row updated with its event missing (or the reverse), producing an
        incomplete audit/replay sequence.
        """

        await self._ensure_initialized()
        incoming = _clone_agent(snapshot)
        expected = _normalize_expected_version(expected_version)
        normalized_event_id = _optional_text(event_id) or _new_event_id()
        normalized_type = _required_text(event_type, field_name="event_type")
        normalized_agent_id = _optional_text(event_agent_id)
        normalized_payload = _payload_copy(event_payload or {})
        normalized_created_at = (
            time.time() if event_created_at is None else float(event_created_at)
        )
        return await asyncio.to_thread(
            self._upsert_agent_with_event_sync,
            incoming,
            expected,
            normalized_event_id,
            normalized_type,
            normalized_agent_id,
            normalized_payload,
            normalized_created_at,
        )

    def _upsert_agent_with_event_sync(
        self,
        incoming: AgentRuntimeSnapshot,
        expected_version: int | None,
        event_id: str,
        event_type: str,
        event_agent_id: str | None,
        event_payload: dict[str, Any],
        event_created_at: float,
    ) -> tuple[AgentRuntimeSnapshot, AgentLifecycleEvent]:
        connection = self._connect()
        try:
            connection.execute("BEGIN IMMEDIATE")
            try:
                row = connection.execute(
                    """
                    SELECT * FROM agent_runtime_agents
                    WHERE root_id = ? AND agent_id = ?
                    """,
                    (incoming.root_id, incoming.agent_id),
                ).fetchone()
                current = None if row is None else _row_to_agent(row)
                resolved, changed = _resolve_upsert(
                    current,
                    incoming,
                    expected_version=expected_version,
                    identity=f"agent {incoming.root_id}/{incoming.agent_id}",
                )
                assert isinstance(resolved, AgentRuntimeSnapshot)
                if changed:
                    previous_version = -1 if current is None else current.version
                    cursor = connection.execute(
                        """
                        INSERT INTO agent_runtime_agents (
                            root_id, agent_id, parent_id, depth, status,
                            metadata, created_at, updated_at, started_at,
                            finished_at, error, version
                        ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
                        ON CONFLICT(root_id, agent_id) DO UPDATE SET
                            parent_id = excluded.parent_id,
                            depth = excluded.depth,
                            status = excluded.status,
                            metadata = excluded.metadata,
                            created_at = excluded.created_at,
                            updated_at = excluded.updated_at,
                            started_at = excluded.started_at,
                            finished_at = excluded.finished_at,
                            error = excluded.error,
                            version = excluded.version
                        WHERE agent_runtime_agents.version = ?
                        """,
                        _agent_values(resolved) + (previous_version,),
                    )
                    if cursor.rowcount != 1:
                        raise AgentRuntimeVersionConflict(
                            "agent "
                            f"{incoming.root_id}/{incoming.agent_id} changed "
                            "during upsert"
                        )
                existing_event = connection.execute(
                    """
                    SELECT * FROM agent_runtime_events
                    WHERE root_id = ? AND event_id = ?
                    """,
                    (incoming.root_id, event_id),
                ).fetchone()
                if existing_event is not None:
                    connection.commit()
                    return _clone_agent(resolved), _row_to_event(existing_event)
                sequence_row = connection.execute(
                    """
                    SELECT COALESCE(MAX(sequence), 0) + 1 AS next_sequence
                    FROM agent_runtime_events
                    WHERE root_id = ?
                    """,
                    (incoming.root_id,),
                ).fetchone()
                sequence = int(sequence_row["next_sequence"])
                connection.execute(
                    """
                    INSERT INTO agent_runtime_events (
                        root_id, sequence, event_id, agent_id, event_type,
                        payload, created_at
                    ) VALUES (?, ?, ?, ?, ?, ?, ?)
                    """,
                    (
                        incoming.root_id,
                        sequence,
                        event_id,
                        event_agent_id,
                        event_type,
                        _json_dumps(event_payload),
                        event_created_at,
                    ),
                )
                connection.commit()
                return _clone_agent(resolved), AgentLifecycleEvent(
                    root_id=incoming.root_id,
                    sequence=sequence,
                    event_id=event_id,
                    event_type=event_type,
                    agent_id=event_agent_id,
                    payload=event_payload,
                    created_at=event_created_at,
                )
            except Exception:
                connection.rollback()
                raise
        finally:
            connection.close()

    def _load_tree_agents_sync(
        self,
        root_id: str,
    ) -> list[AgentRuntimeSnapshot]:
        connection = self._connect()
        try:
            rows = connection.execute(
                """
                SELECT * FROM agent_runtime_agents
                WHERE root_id = ?
                ORDER BY depth ASC, created_at ASC, agent_id ASC
                """,
                (root_id,),
            ).fetchall()
            return [_row_to_agent(row) for row in rows]
        finally:
            connection.close()

    async def append_lifecycle_event(
        self,
        root_id: str,
        *,
        event_type: str,
        agent_id: str | None = None,
        payload: Mapping[str, Any] | None = None,
        event_id: str | None = None,
        created_at: float | None = None,
    ) -> AgentLifecycleEvent:
        await self._ensure_initialized()
        normalized_root = _required_text(root_id, field_name="root_id")
        normalized_event_id = _optional_text(event_id) or _new_event_id()
        normalized_type = _required_text(event_type, field_name="event_type")
        normalized_agent_id = _optional_text(agent_id)
        normalized_payload = _payload_copy(payload or {})
        normalized_created_at = time.time() if created_at is None else float(created_at)
        return await asyncio.to_thread(
            self._append_lifecycle_event_sync,
            normalized_root,
            normalized_event_id,
            normalized_type,
            normalized_agent_id,
            normalized_payload,
            normalized_created_at,
        )

    def _append_lifecycle_event_sync(
        self,
        root_id: str,
        event_id: str,
        event_type: str,
        agent_id: str | None,
        payload: dict[str, Any],
        created_at: float,
    ) -> AgentLifecycleEvent:
        connection = self._connect()
        try:
            connection.execute("BEGIN IMMEDIATE")
            try:
                existing = connection.execute(
                    """
                    SELECT * FROM agent_runtime_events
                    WHERE root_id = ? AND event_id = ?
                    """,
                    (root_id, event_id),
                ).fetchone()
                if existing is not None:
                    connection.commit()
                    return _row_to_event(existing)

                row = connection.execute(
                    """
                    SELECT COALESCE(MAX(sequence), 0) + 1 AS next_sequence
                    FROM agent_runtime_events
                    WHERE root_id = ?
                    """,
                    (root_id,),
                ).fetchone()
                sequence = int(row["next_sequence"])
                connection.execute(
                    """
                    INSERT INTO agent_runtime_events (
                        root_id, sequence, event_id, agent_id, event_type,
                        payload, created_at
                    ) VALUES (?, ?, ?, ?, ?, ?, ?)
                    """,
                    (
                        root_id,
                        sequence,
                        event_id,
                        agent_id,
                        event_type,
                        _json_dumps(payload),
                        created_at,
                    ),
                )
                connection.commit()
                return AgentLifecycleEvent(
                    root_id=root_id,
                    sequence=sequence,
                    event_id=event_id,
                    event_type=event_type,
                    agent_id=agent_id,
                    payload=payload,
                    created_at=created_at,
                )
            except Exception:
                connection.rollback()
                raise
        finally:
            connection.close()

    async def load_lifecycle_events(
        self,
        root_id: str,
        *,
        after_sequence: int = 0,
    ) -> list[AgentLifecycleEvent]:
        await self._ensure_initialized()
        normalized_root = _required_text(root_id, field_name="root_id")
        normalized_after = int(after_sequence)
        if normalized_after < 0:
            raise ValueError("after_sequence must not be negative")
        return await asyncio.to_thread(
            self._load_lifecycle_events_sync,
            normalized_root,
            normalized_after,
        )

    def _load_lifecycle_events_sync(
        self,
        root_id: str,
        after_sequence: int,
    ) -> list[AgentLifecycleEvent]:
        connection = self._connect()
        try:
            rows = connection.execute(
                """
                SELECT * FROM agent_runtime_events
                WHERE root_id = ? AND sequence > ?
                ORDER BY sequence ASC
                """,
                (root_id, after_sequence),
            ).fetchall()
            return [_row_to_event(row) for row in rows]
        finally:
            connection.close()


def _run_values(snapshot: AgentRunSnapshot) -> tuple[Any, ...]:
    return (
        snapshot.root_id,
        snapshot.status,
        _json_dumps(snapshot.metadata),
        snapshot.created_at,
        snapshot.updated_at,
        snapshot.started_at,
        snapshot.finished_at,
        snapshot.error,
        snapshot.version,
    )


def _agent_values(snapshot: AgentRuntimeSnapshot) -> tuple[Any, ...]:
    return (
        snapshot.root_id,
        snapshot.agent_id,
        snapshot.parent_id,
        snapshot.depth,
        snapshot.status,
        _json_dumps(snapshot.metadata),
        snapshot.created_at,
        snapshot.updated_at,
        snapshot.started_at,
        snapshot.finished_at,
        snapshot.error,
        snapshot.version,
    )


def _row_to_run(row: sqlite3.Row) -> AgentRunSnapshot:
    return AgentRunSnapshot(
        root_id=row["root_id"],
        status=row["status"],
        metadata=_json_loads(row["metadata"]),
        created_at=row["created_at"],
        updated_at=row["updated_at"],
        started_at=row["started_at"],
        finished_at=row["finished_at"],
        error=row["error"],
        version=row["version"],
    )


def _row_to_agent(row: sqlite3.Row) -> AgentRuntimeSnapshot:
    return AgentRuntimeSnapshot(
        root_id=row["root_id"],
        agent_id=row["agent_id"],
        parent_id=row["parent_id"],
        depth=row["depth"],
        status=row["status"],
        metadata=_json_loads(row["metadata"]),
        created_at=row["created_at"],
        updated_at=row["updated_at"],
        started_at=row["started_at"],
        finished_at=row["finished_at"],
        error=row["error"],
        version=row["version"],
    )


def _row_to_event(row: sqlite3.Row) -> AgentLifecycleEvent:
    return AgentLifecycleEvent(
        root_id=row["root_id"],
        sequence=row["sequence"],
        event_id=row["event_id"],
        event_type=row["event_type"],
        agent_id=row["agent_id"],
        payload=_json_loads(row["payload"]),
        created_at=row["created_at"],
    )
