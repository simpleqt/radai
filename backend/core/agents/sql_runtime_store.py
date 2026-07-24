"""Shared SQLAlchemy-backed persistence for agent runtime state.

This module is an opt-in adapter for deployments where multiple processes need
to share one runtime tree.  It accepts an already configured synchronous
SQLAlchemy :class:`~sqlalchemy.engine.Engine`; importing or constructing the
store never opens a database connection.  All blocking SQL work is dispatched
with :func:`asyncio.to_thread`.

Schema creation is deliberately explicit.  Applications should manage these
tables through their normal migration system, or call ``await create_schema()``
during controlled startup.  The Core-only schema uses portable SQLAlchemy
types and statements supported by SQLite (for tests) and MySQL/InnoDB.
"""

from __future__ import annotations

import asyncio
import json
import time
import uuid
from collections.abc import Mapping
from dataclasses import replace
from typing import Any, TypeVar, cast

from sqlalchemy import (
    JSON,
    CheckConstraint,
    Column,
    Float,
    Index,
    Integer,
    MetaData,
    String,
    Table,
    Text,
    UniqueConstraint,
    and_,
    func,
    insert,
    select,
    update,
)
from sqlalchemy.engine import Connection, Engine, RowMapping
from sqlalchemy.exc import IntegrityError, OperationalError

from backend.core.agents.runtime_store import (
    TERMINAL_AGENT_STATUSES,
    AgentLifecycleEvent,
    AgentRunSnapshot,
    AgentRuntimeSnapshot,
    AgentRuntimeStoreError,
    AgentRuntimeTerminalStateError,
    AgentRuntimeVersionConflict,
)

_METADATA = MetaData()

_RUNS = Table(
    "agent_runtime_runs",
    _METADATA,
    Column("root_id", String(191), primary_key=True),
    Column("status", String(64), nullable=False),
    Column("metadata", JSON, nullable=False),
    Column("created_at", Float, nullable=False),
    Column("updated_at", Float, nullable=False),
    Column("started_at", Float),
    Column("finished_at", Float),
    Column("error", Text),
    Column("version", Integer, nullable=False),
    CheckConstraint("version >= 1", name="ck_agent_runtime_runs_version"),
    mysql_engine="InnoDB",
    mysql_charset="utf8mb4",
)

_AGENTS = Table(
    "agent_runtime_agents",
    _METADATA,
    Column("root_id", String(191), primary_key=True),
    Column("agent_id", String(191), primary_key=True),
    Column("parent_id", String(191)),
    Column("depth", Integer, nullable=False),
    Column("status", String(64), nullable=False),
    Column("metadata", JSON, nullable=False),
    Column("created_at", Float, nullable=False),
    Column("updated_at", Float, nullable=False),
    Column("started_at", Float),
    Column("finished_at", Float),
    Column("error", Text),
    Column("version", Integer, nullable=False),
    CheckConstraint("depth >= 0", name="ck_agent_runtime_agents_depth"),
    CheckConstraint("version >= 1", name="ck_agent_runtime_agents_version"),
    mysql_engine="InnoDB",
    mysql_charset="utf8mb4",
)
Index(
    "ix_agent_runtime_agents_tree",
    _AGENTS.c.root_id,
    _AGENTS.c.depth,
    _AGENTS.c.created_at,
    _AGENTS.c.agent_id,
)

_EVENTS = Table(
    "agent_runtime_events",
    _METADATA,
    Column("root_id", String(191), primary_key=True),
    Column("sequence", Integer, primary_key=True),
    Column("event_id", String(191), nullable=False),
    Column("agent_id", String(191)),
    Column("event_type", String(191), nullable=False),
    Column("payload", JSON, nullable=False),
    Column("created_at", Float, nullable=False),
    CheckConstraint("sequence >= 1", name="ck_agent_runtime_events_sequence"),
    UniqueConstraint(
        "root_id",
        "event_id",
        name="uq_agent_runtime_events_root_event",
    ),
    mysql_engine="InnoDB",
    mysql_charset="utf8mb4",
)
Index(
    "ix_agent_runtime_events_agent",
    _EVENTS.c.root_id,
    _EVENTS.c.agent_id,
    _EVENTS.c.sequence,
)

_SnapshotT = TypeVar(
    "_SnapshotT",
    AgentRunSnapshot,
    AgentRuntimeSnapshot,
)


class _ConcurrentWrite(RuntimeError):
    """Internal signal used to retry a transaction without hiding CAS errors."""


class SQLAlchemyAgentRuntimeStore:
    """Async runtime-store adapter over an injected synchronous SQLAlchemy Engine.

    The store performs no implicit schema initialization and owns neither the
    Engine nor its pool.  Callers remain responsible for Engine disposal and
    should use MySQL/InnoDB (or another transactional backend) in production.
    ``max_transaction_retries`` applies only to transient lock/deadlock and
    uniqueness races; semantic version conflicts are returned immediately.
    """

    def __init__(
        self,
        engine: Engine,
        *,
        max_transaction_retries: int = 32,
        retry_base_delay_seconds: float = 0.002,
    ) -> None:
        if not isinstance(engine, Engine):
            raise TypeError("engine must be a synchronous SQLAlchemy Engine")
        retries = int(max_transaction_retries)
        if retries < 1:
            raise ValueError("max_transaction_retries must be at least 1")
        delay = float(retry_base_delay_seconds)
        if delay < 0:
            raise ValueError("retry_base_delay_seconds must not be negative")
        self.engine = engine
        self.max_transaction_retries = retries
        self.retry_base_delay_seconds = delay

    async def create_schema(self) -> None:
        """Explicitly create the portable runtime tables and indexes."""

        await asyncio.to_thread(_METADATA.create_all, self.engine)

    async def upsert_run(
        self,
        snapshot: AgentRunSnapshot,
        *,
        expected_version: int | None = None,
    ) -> AgentRunSnapshot:
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
        identity = f"run {incoming.root_id}"

        def operation(connection: Connection) -> AgentRunSnapshot:
            row = (
                connection.execute(
                    select(_RUNS)
                    .where(_RUNS.c.root_id == incoming.root_id)
                    .with_for_update()
                )
                .mappings()
                .first()
            )
            current = None if row is None else _row_to_run(row)
            resolved, changed = _resolve_upsert(
                current,
                incoming,
                expected_version=expected_version,
                identity=identity,
            )
            if not changed:
                return _clone_run(resolved)
            if current is None:
                connection.execute(insert(_RUNS).values(_run_values(resolved)))
            else:
                result = connection.execute(
                    update(_RUNS)
                    .where(
                        and_(
                            _RUNS.c.root_id == incoming.root_id,
                            _RUNS.c.version == current.version,
                        )
                    )
                    .values(_run_values(resolved, include_identity=False))
                )
                if result.rowcount != 1:
                    raise _ConcurrentWrite(f"{identity} changed during upsert")
            return _clone_run(resolved)

        return self._run_upsert_transaction(
            operation,
            identity=identity,
            expected_version=expected_version,
        )

    async def load_run(self, root_id: str) -> AgentRunSnapshot | None:
        normalized_root = _required_text(root_id, field_name="root_id")
        return await asyncio.to_thread(self._load_run_sync, normalized_root)

    def _load_run_sync(self, root_id: str) -> AgentRunSnapshot | None:
        with self.engine.connect() as connection:
            row = (
                connection.execute(select(_RUNS).where(_RUNS.c.root_id == root_id))
                .mappings()
                .first()
            )
        return None if row is None else _row_to_run(row)

    async def upsert_agent(
        self,
        snapshot: AgentRuntimeSnapshot,
        *,
        expected_version: int | None = None,
    ) -> AgentRuntimeSnapshot:
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
        identity = f"agent {incoming.root_id}/{incoming.agent_id}"

        def operation(connection: Connection) -> AgentRuntimeSnapshot:
            row = (
                connection.execute(
                    select(_AGENTS)
                    .where(
                        and_(
                            _AGENTS.c.root_id == incoming.root_id,
                            _AGENTS.c.agent_id == incoming.agent_id,
                        )
                    )
                    .with_for_update()
                )
                .mappings()
                .first()
            )
            current = None if row is None else _row_to_agent(row)
            resolved, changed = _resolve_upsert(
                current,
                incoming,
                expected_version=expected_version,
                identity=identity,
            )
            if not changed:
                return _clone_agent(resolved)
            if current is None:
                connection.execute(insert(_AGENTS).values(_agent_values(resolved)))
            else:
                result = connection.execute(
                    update(_AGENTS)
                    .where(
                        and_(
                            _AGENTS.c.root_id == incoming.root_id,
                            _AGENTS.c.agent_id == incoming.agent_id,
                            _AGENTS.c.version == current.version,
                        )
                    )
                    .values(_agent_values(resolved, include_identity=False))
                )
                if result.rowcount != 1:
                    raise _ConcurrentWrite(f"{identity} changed during upsert")
            return _clone_agent(resolved)

        return self._run_upsert_transaction(
            operation,
            identity=identity,
            expected_version=expected_version,
        )

    async def load_tree_agents(
        self,
        root_id: str,
    ) -> list[AgentRuntimeSnapshot]:
        normalized_root = _required_text(root_id, field_name="root_id")
        return await asyncio.to_thread(
            self._load_tree_agents_sync,
            normalized_root,
        )

    def _load_tree_agents_sync(
        self,
        root_id: str,
    ) -> list[AgentRuntimeSnapshot]:
        with self.engine.connect() as connection:
            rows = (
                connection.execute(
                    select(_AGENTS)
                    .where(_AGENTS.c.root_id == root_id)
                    .order_by(
                        _AGENTS.c.depth.asc(),
                        _AGENTS.c.created_at.asc(),
                        _AGENTS.c.agent_id.asc(),
                    )
                )
                .mappings()
                .all()
            )
        return [_row_to_agent(row) for row in rows]

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
        def operation(connection: Connection) -> AgentLifecycleEvent:
            existing = (
                connection.execute(
                    select(_EVENTS)
                    .where(
                        and_(
                            _EVENTS.c.root_id == root_id,
                            _EVENTS.c.event_id == event_id,
                        )
                    )
                    .with_for_update()
                )
                .mappings()
                .first()
            )
            if existing is not None:
                return _row_to_event(existing)

            next_sequence = connection.execute(
                select(func.coalesce(func.max(_EVENTS.c.sequence), 0) + 1).where(
                    _EVENTS.c.root_id == root_id
                )
            ).scalar_one()
            sequence = int(next_sequence)
            connection.execute(
                insert(_EVENTS).values(
                    root_id=root_id,
                    sequence=sequence,
                    event_id=event_id,
                    agent_id=agent_id,
                    event_type=event_type,
                    payload=payload,
                    created_at=created_at,
                )
            )
            return AgentLifecycleEvent(
                root_id=root_id,
                sequence=sequence,
                event_id=event_id,
                event_type=event_type,
                agent_id=agent_id,
                payload=dict(payload),
                created_at=created_at,
            )

        return self._run_retrying_transaction(
            operation,
            operation_name=f"append event {root_id}/{event_id}",
        )

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
        with self.engine.connect() as connection:
            rows = (
                connection.execute(
                    select(_EVENTS)
                    .where(
                        and_(
                            _EVENTS.c.root_id == root_id,
                            _EVENTS.c.sequence > after_sequence,
                        )
                    )
                    .order_by(_EVENTS.c.sequence.asc())
                )
                .mappings()
                .all()
            )
        return [_row_to_event(row) for row in rows]

    def _run_upsert_transaction(
        self,
        operation: Any,
        *,
        identity: str,
        expected_version: int | None,
    ) -> Any:
        for attempt in range(self.max_transaction_retries):
            try:
                with self.engine.begin() as connection:
                    return operation(connection)
            except _ConcurrentWrite as exc:
                if expected_version is not None:
                    raise AgentRuntimeVersionConflict(str(exc)) from exc
                last_error: Exception = exc
            except IntegrityError as exc:
                last_error = exc
            except OperationalError as exc:
                if not _is_retryable_operational_error(exc):
                    raise
                last_error = exc
            self._retry_delay(attempt)
        raise AgentRuntimeStoreError(
            f"{identity} could not be persisted after "
            f"{self.max_transaction_retries} transaction attempts"
        ) from last_error

    def _run_retrying_transaction(
        self,
        operation: Any,
        *,
        operation_name: str,
    ) -> Any:
        for attempt in range(self.max_transaction_retries):
            try:
                with self.engine.begin() as connection:
                    return operation(connection)
            except IntegrityError as exc:
                last_error: Exception = exc
            except OperationalError as exc:
                if not _is_retryable_operational_error(exc):
                    raise
                last_error = exc
            self._retry_delay(attempt)
        raise AgentRuntimeStoreError(
            f"{operation_name} failed after "
            f"{self.max_transaction_retries} transaction attempts"
        ) from last_error

    def _retry_delay(self, attempt: int) -> None:
        if self.retry_base_delay_seconds <= 0:
            return
        time.sleep(min(self.retry_base_delay_seconds * (2**attempt), 0.25))


def _resolve_upsert(
    current: _SnapshotT | None,
    incoming: _SnapshotT,
    *,
    expected_version: int | None,
    identity: str,
) -> tuple[_SnapshotT, bool]:
    if current is None:
        if expected_version not in (None, 0):
            raise AgentRuntimeVersionConflict(
                f"{identity} does not exist at expected version {expected_version}"
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
    if expected_version is not None and expected_version != current.version:
        raise AgentRuntimeVersionConflict(
            f"{identity} expected version {expected_version}, found {current.version}"
        )
    return replace(incoming, version=current.version + 1), True


def _run_values(
    snapshot: AgentRunSnapshot,
    *,
    include_identity: bool = True,
) -> dict[str, Any]:
    values: dict[str, Any] = {
        "status": snapshot.status,
        "metadata": dict(snapshot.metadata),
        "created_at": snapshot.created_at,
        "updated_at": snapshot.updated_at,
        "started_at": snapshot.started_at,
        "finished_at": snapshot.finished_at,
        "error": snapshot.error,
        "version": snapshot.version,
    }
    if include_identity:
        values["root_id"] = snapshot.root_id
    return values


def _agent_values(
    snapshot: AgentRuntimeSnapshot,
    *,
    include_identity: bool = True,
) -> dict[str, Any]:
    values: dict[str, Any] = {
        "parent_id": snapshot.parent_id,
        "depth": snapshot.depth,
        "status": snapshot.status,
        "metadata": dict(snapshot.metadata),
        "created_at": snapshot.created_at,
        "updated_at": snapshot.updated_at,
        "started_at": snapshot.started_at,
        "finished_at": snapshot.finished_at,
        "error": snapshot.error,
        "version": snapshot.version,
    }
    if include_identity:
        values.update(root_id=snapshot.root_id, agent_id=snapshot.agent_id)
    return values


def _row_to_run(row: RowMapping) -> AgentRunSnapshot:
    return AgentRunSnapshot(
        root_id=cast(str, row["root_id"]),
        status=cast(str, row["status"]),
        metadata=_json_object(row["metadata"]),
        created_at=cast(float, row["created_at"]),
        updated_at=cast(float, row["updated_at"]),
        started_at=cast(float | None, row["started_at"]),
        finished_at=cast(float | None, row["finished_at"]),
        error=cast(str | None, row["error"]),
        version=cast(int, row["version"]),
    )


def _row_to_agent(row: RowMapping) -> AgentRuntimeSnapshot:
    return AgentRuntimeSnapshot(
        root_id=cast(str, row["root_id"]),
        agent_id=cast(str, row["agent_id"]),
        parent_id=cast(str | None, row["parent_id"]),
        depth=cast(int, row["depth"]),
        status=cast(str, row["status"]),
        metadata=_json_object(row["metadata"]),
        created_at=cast(float, row["created_at"]),
        updated_at=cast(float, row["updated_at"]),
        started_at=cast(float | None, row["started_at"]),
        finished_at=cast(float | None, row["finished_at"]),
        error=cast(str | None, row["error"]),
        version=cast(int, row["version"]),
    )


def _row_to_event(row: RowMapping) -> AgentLifecycleEvent:
    return AgentLifecycleEvent(
        root_id=cast(str, row["root_id"]),
        sequence=cast(int, row["sequence"]),
        event_id=cast(str, row["event_id"]),
        event_type=cast(str, row["event_type"]),
        agent_id=cast(str | None, row["agent_id"]),
        payload=_json_object(row["payload"]),
        created_at=cast(float, row["created_at"]),
    )


def _clone_run(snapshot: AgentRunSnapshot) -> AgentRunSnapshot:
    return replace(snapshot, metadata=dict(snapshot.metadata))


def _clone_agent(snapshot: AgentRuntimeSnapshot) -> AgentRuntimeSnapshot:
    return replace(snapshot, metadata=dict(snapshot.metadata))


def _json_object(value: object) -> dict[str, Any]:
    parsed = json.loads(value) if isinstance(value, str) else value
    if not isinstance(parsed, Mapping):
        raise AgentRuntimeStoreError("stored JSON value is not an object")
    return dict(parsed)


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


def _payload_copy(value: Mapping[str, Any]) -> dict[str, Any]:
    if not isinstance(value, Mapping):
        raise TypeError("payload must be a mapping")
    return dict(value)


def _normalize_expected_version(expected_version: int | None) -> int | None:
    if expected_version is None:
        return None
    normalized = int(expected_version)
    if normalized < 0:
        raise ValueError("expected_version must not be negative")
    return normalized


def _new_event_id() -> str:
    return f"evt_{uuid.uuid4().hex}"


def _is_retryable_operational_error(error: OperationalError) -> bool:
    original = error.orig
    code = getattr(original, "sqlite_errorcode", None)
    if code in {5, 6}:
        return True
    arguments = getattr(original, "args", ())
    if arguments and arguments[0] in {1205, 1213}:
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


__all__ = ["SQLAlchemyAgentRuntimeStore"]
