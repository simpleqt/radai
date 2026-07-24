"""Persistence boundary for agent-runtime trees and lifecycle events.

The runtime manager mirrors live transitions into these stores when a backend
is explicitly configured. The in-memory scheduler remains the source of live
task ownership; the SQL/SQLite stores provide recovery and inspection state.
"""

from __future__ import annotations

import asyncio
import json
import os
import sqlite3
import time
import uuid
from collections.abc import Mapping
from dataclasses import dataclass, field, replace
from enum import Enum
from pathlib import Path
from typing import Any, Protocol, runtime_checkable

TERMINAL_AGENT_STATUSES = frozenset(
    {"completed", "failed", "timed_out", "interrupted", "cancelled"}
)


class AgentRuntimeStoreError(RuntimeError):
    """Base error for durable runtime-state operations."""


class AgentRuntimeVersionConflict(AgentRuntimeStoreError):
    """Raised when an optimistic compare-and-swap version is stale."""


class AgentRuntimeTerminalStateError(AgentRuntimeStoreError):
    """Raised when a write would mutate an already-terminal snapshot."""


def _required_text(value: object, *, field_name: str) -> str:
    normalized = str(value or "").strip()
    if not normalized:
        raise ValueError(f"{field_name} must not be empty")
    return normalized


def _optional_text(value: object) -> str | None:
    if value is None:
        return None
    normalized = str(value).strip()
    return normalized or None


def _status_text(value: object) -> str:
    if isinstance(value, Enum):
        value = value.value
    return _required_text(value, field_name="status")


def _metadata_copy(value: Mapping[str, Any]) -> dict[str, Any]:
    if not isinstance(value, Mapping):
        raise TypeError("metadata must be a mapping")
    return dict(value)


def _payload_copy(value: Mapping[str, Any]) -> dict[str, Any]:
    if not isinstance(value, Mapping):
        raise TypeError("payload must be a mapping")
    return dict(value)


@dataclass(frozen=True, kw_only=True)
class AgentRunSnapshot:
    """Persisted root/tree state for one agent run."""

    root_id: str
    status: str
    metadata: dict[str, Any] = field(default_factory=dict)
    created_at: float = 0.0
    updated_at: float = 0.0
    started_at: float | None = None
    finished_at: float | None = None
    error: str | None = None
    version: int = 0

    def __post_init__(self) -> None:
        version = int(self.version)
        if version < 0:
            raise ValueError("version must not be negative")
        object.__setattr__(
            self,
            "root_id",
            _required_text(self.root_id, field_name="root_id"),
        )
        object.__setattr__(self, "status", _status_text(self.status))
        object.__setattr__(self, "metadata", _metadata_copy(self.metadata))
        object.__setattr__(self, "created_at", float(self.created_at))
        object.__setattr__(self, "updated_at", float(self.updated_at))
        object.__setattr__(
            self,
            "started_at",
            None if self.started_at is None else float(self.started_at),
        )
        object.__setattr__(
            self,
            "finished_at",
            None if self.finished_at is None else float(self.finished_at),
        )
        object.__setattr__(self, "error", _optional_text(self.error))
        object.__setattr__(self, "version", version)


@dataclass(frozen=True, kw_only=True)
class AgentRuntimeSnapshot:
    """Persisted snapshot for one child agent in a root request tree."""

    root_id: str
    agent_id: str
    parent_id: str | None
    depth: int
    status: str
    metadata: dict[str, Any] = field(default_factory=dict)
    created_at: float = 0.0
    updated_at: float = 0.0
    started_at: float | None = None
    finished_at: float | None = None
    error: str | None = None
    version: int = 0

    def __post_init__(self) -> None:
        depth = int(self.depth)
        version = int(self.version)
        if depth < 0:
            raise ValueError("depth must not be negative")
        if version < 0:
            raise ValueError("version must not be negative")
        object.__setattr__(
            self,
            "root_id",
            _required_text(self.root_id, field_name="root_id"),
        )
        object.__setattr__(
            self,
            "agent_id",
            _required_text(self.agent_id, field_name="agent_id"),
        )
        object.__setattr__(self, "parent_id", _optional_text(self.parent_id))
        object.__setattr__(self, "depth", depth)
        object.__setattr__(self, "status", _status_text(self.status))
        object.__setattr__(self, "metadata", _metadata_copy(self.metadata))
        object.__setattr__(self, "created_at", float(self.created_at))
        object.__setattr__(self, "updated_at", float(self.updated_at))
        object.__setattr__(
            self,
            "started_at",
            None if self.started_at is None else float(self.started_at),
        )
        object.__setattr__(
            self,
            "finished_at",
            None if self.finished_at is None else float(self.finished_at),
        )
        object.__setattr__(self, "error", _optional_text(self.error))
        object.__setattr__(self, "version", version)


@dataclass(frozen=True, kw_only=True)
class AgentLifecycleEvent:
    """Append-only lifecycle event with a tree-local durable sequence."""

    root_id: str
    sequence: int
    event_id: str
    event_type: str
    agent_id: str | None = None
    payload: dict[str, Any] = field(default_factory=dict)
    created_at: float = 0.0

    def __post_init__(self) -> None:
        sequence = int(self.sequence)
        if sequence < 1:
            raise ValueError("sequence must be at least 1")
        object.__setattr__(
            self,
            "root_id",
            _required_text(self.root_id, field_name="root_id"),
        )
        object.__setattr__(self, "sequence", sequence)
        object.__setattr__(
            self,
            "event_id",
            _required_text(self.event_id, field_name="event_id"),
        )
        object.__setattr__(
            self,
            "event_type",
            _required_text(self.event_type, field_name="event_type"),
        )
        object.__setattr__(self, "agent_id", _optional_text(self.agent_id))
        object.__setattr__(self, "payload", _payload_copy(self.payload))
        object.__setattr__(self, "created_at", float(self.created_at))


@runtime_checkable
class AgentRuntimeStore(Protocol):
    """Async persistence contract for runtime trees, agents, and events."""

    async def upsert_run(
        self,
        snapshot: AgentRunSnapshot,
        *,
        expected_version: int | None = None,
    ) -> AgentRunSnapshot: ...

    async def upsert_tree(
        self,
        snapshot: AgentRunSnapshot,
        *,
        expected_version: int | None = None,
    ) -> AgentRunSnapshot: ...

    async def load_run(self, root_id: str) -> AgentRunSnapshot | None: ...

    async def upsert_agent(
        self,
        snapshot: AgentRuntimeSnapshot,
        *,
        expected_version: int | None = None,
    ) -> AgentRuntimeSnapshot: ...

    async def load_tree_agents(
        self,
        root_id: str,
    ) -> list[AgentRuntimeSnapshot]: ...

    async def append_lifecycle_event(
        self,
        root_id: str,
        *,
        event_type: str,
        agent_id: str | None = None,
        payload: Mapping[str, Any] | None = None,
        event_id: str | None = None,
        created_at: float | None = None,
    ) -> AgentLifecycleEvent: ...

    async def load_lifecycle_events(
        self,
        root_id: str,
        *,
        after_sequence: int = 0,
    ) -> list[AgentLifecycleEvent]: ...


def _normalize_expected_version(expected_version: int | None) -> int | None:
    if expected_version is None:
        return None
    normalized = int(expected_version)
    if normalized < 0:
        raise ValueError("expected_version must not be negative")
    return normalized


def _resolve_upsert(
    current: AgentRunSnapshot | AgentRuntimeSnapshot | None,
    incoming: AgentRunSnapshot | AgentRuntimeSnapshot,
    *,
    expected_version: int | None,
    identity: str,
) -> tuple[AgentRunSnapshot | AgentRuntimeSnapshot, bool]:
    """Apply idempotency, CAS, and immutable-terminal rules."""

    expected = _normalize_expected_version(expected_version)
    if current is None:
        if expected not in (None, 0):
            raise AgentRuntimeVersionConflict(
                f"{identity} does not exist at expected version {expected}"
            )
        return replace(incoming, version=1), True

    comparable = replace(incoming, version=current.version)
    if comparable == current:
        return current, False

    if current.status in TERMINAL_AGENT_STATUSES:
        raise AgentRuntimeTerminalStateError(
            f"{identity} is terminal at status {current.status} and version "
            f"{current.version}"
        )

    if expected is not None and expected != current.version:
        raise AgentRuntimeVersionConflict(
            f"{identity} expected version {expected}, found {current.version}"
        )
    return replace(incoming, version=current.version + 1), True


def _clone_run(snapshot: AgentRunSnapshot) -> AgentRunSnapshot:
    return replace(snapshot, metadata=dict(snapshot.metadata))


def _clone_agent(snapshot: AgentRuntimeSnapshot) -> AgentRuntimeSnapshot:
    return replace(snapshot, metadata=dict(snapshot.metadata))


def _clone_event(event: AgentLifecycleEvent) -> AgentLifecycleEvent:
    return replace(event, payload=dict(event.payload))


class InMemoryAgentRuntimeStore:
    """Concurrency-safe in-process implementation of ``AgentRuntimeStore``."""

    def __init__(self, *, finished_retention_seconds: float | None = None) -> None:
        self._runs: dict[str, AgentRunSnapshot] = {}
        self._agents: dict[tuple[str, str], AgentRuntimeSnapshot] = {}
        self._events: dict[str, list[AgentLifecycleEvent]] = {}
        self._events_by_id: dict[tuple[str, str], AgentLifecycleEvent] = {}
        self._lock = asyncio.Lock()
        if finished_retention_seconds is None:
            try:
                finished_retention_seconds = float(
                    os.getenv("AGENT_RUNTIME_MEMORY_RETENTION_SECONDS", "3600")
                )
            except (TypeError, ValueError):
                finished_retention_seconds = 3600.0
        self._finished_retention_seconds = max(
            1.0,
            float(finished_retention_seconds),
        )
        self._last_automatic_prune = 0.0

    def _prune_finished_locked(self, before: float) -> int:
        expired_roots = {
            root_id
            for root_id, snapshot in self._runs.items()
            if snapshot.status in TERMINAL_AGENT_STATUSES
            and (snapshot.finished_at or snapshot.updated_at) < before
        }
        if not expired_roots:
            return 0
        for root_id in expired_roots:
            self._runs.pop(root_id, None)
            self._events.pop(root_id, None)
        self._agents = {
            key: value
            for key, value in self._agents.items()
            if key[0] not in expired_roots
        }
        self._events_by_id = {
            key: value
            for key, value in self._events_by_id.items()
            if key[0] not in expired_roots
        }
        return len(expired_roots)

    def _automatic_prune_locked(self) -> None:
        now_monotonic = time.monotonic()
        if now_monotonic - self._last_automatic_prune < 60.0:
            return
        self._last_automatic_prune = now_monotonic
        self._prune_finished_locked(
            time.time() - self._finished_retention_seconds
        )

    async def prune_finished(self, before: float) -> int:
        """Remove terminal trees older than ``before`` and all related records."""
        cutoff = float(before)
        async with self._lock:
            return self._prune_finished_locked(cutoff)

    async def upsert_run(
        self,
        snapshot: AgentRunSnapshot,
        *,
        expected_version: int | None = None,
    ) -> AgentRunSnapshot:
        incoming = _clone_run(snapshot)
        async with self._lock:
            self._automatic_prune_locked()
            resolved, changed = _resolve_upsert(
                self._runs.get(incoming.root_id),
                incoming,
                expected_version=expected_version,
                identity=f"run {incoming.root_id}",
            )
            assert isinstance(resolved, AgentRunSnapshot)
            if changed:
                self._runs[incoming.root_id] = _clone_run(resolved)
            return _clone_run(resolved)

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

    async def load_run(self, root_id: str) -> AgentRunSnapshot | None:
        normalized_root = _required_text(root_id, field_name="root_id")
        async with self._lock:
            snapshot = self._runs.get(normalized_root)
            return None if snapshot is None else _clone_run(snapshot)

    async def upsert_agent(
        self,
        snapshot: AgentRuntimeSnapshot,
        *,
        expected_version: int | None = None,
    ) -> AgentRuntimeSnapshot:
        incoming = _clone_agent(snapshot)
        key = (incoming.root_id, incoming.agent_id)
        async with self._lock:
            self._automatic_prune_locked()
            resolved, changed = _resolve_upsert(
                self._agents.get(key),
                incoming,
                expected_version=expected_version,
                identity=f"agent {incoming.root_id}/{incoming.agent_id}",
            )
            assert isinstance(resolved, AgentRuntimeSnapshot)
            if changed:
                self._agents[key] = _clone_agent(resolved)
            return _clone_agent(resolved)

    async def load_tree_agents(
        self,
        root_id: str,
    ) -> list[AgentRuntimeSnapshot]:
        normalized_root = _required_text(root_id, field_name="root_id")
        async with self._lock:
            snapshots = [
                _clone_agent(snapshot)
                for (stored_root, _), snapshot in self._agents.items()
                if stored_root == normalized_root
            ]
        return sorted(
            snapshots,
            key=lambda item: (item.depth, item.created_at, item.agent_id),
        )

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
        normalized_root = _required_text(root_id, field_name="root_id")
        normalized_event_id = _optional_text(event_id) or _new_event_id()
        normalized_type = _required_text(event_type, field_name="event_type")
        normalized_payload = _payload_copy(payload or {})
        normalized_created_at = time.time() if created_at is None else float(created_at)

        async with self._lock:
            self._automatic_prune_locked()
            existing = self._events_by_id.get(
                (normalized_root, normalized_event_id)
            )
            if existing is not None:
                return _clone_event(existing)
            events = self._events.setdefault(normalized_root, [])
            event = AgentLifecycleEvent(
                root_id=normalized_root,
                sequence=len(events) + 1,
                event_id=normalized_event_id,
                event_type=normalized_type,
                agent_id=agent_id,
                payload=normalized_payload,
                created_at=normalized_created_at,
            )
            events.append(event)
            self._events_by_id[(normalized_root, normalized_event_id)] = event
            return _clone_event(event)

    async def load_lifecycle_events(
        self,
        root_id: str,
        *,
        after_sequence: int = 0,
    ) -> list[AgentLifecycleEvent]:
        normalized_root = _required_text(root_id, field_name="root_id")
        normalized_after = int(after_sequence)
        if normalized_after < 0:
            raise ValueError("after_sequence must not be negative")
        async with self._lock:
            return [
                _clone_event(event)
                for event in self._events.get(normalized_root, [])
                if event.sequence > normalized_after
            ]


def _new_event_id() -> str:
    return f"evt_{uuid.uuid4().hex}"


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


__all__ = [
    "AgentLifecycleEvent",
    "AgentRunSnapshot",
    "AgentRuntimeSnapshot",
    "AgentRuntimeStore",
    "AgentRuntimeStoreError",
    "AgentRuntimeTerminalStateError",
    "AgentRuntimeVersionConflict",
    "InMemoryAgentRuntimeStore",
    "SQLiteAgentRuntimeStore",
    "TERMINAL_AGENT_STATUSES",
]
