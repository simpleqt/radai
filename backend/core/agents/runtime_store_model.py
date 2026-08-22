"""Agent 运行时存储模型层:快照/事件数据类、协议基类、异常与版本校验/克隆 helper。(拆自 runtime_store.py)"""

from __future__ import annotations

import copy
from collections.abc import Mapping
from dataclasses import dataclass
from dataclasses import field
from dataclasses import replace
from enum import Enum
from typing import Any
from typing import Protocol
from typing import runtime_checkable
import uuid


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


# Clones must be deep: a shallow dict() copy still shares nested dicts/lists
# (e.g. metadata["budget_usage"]) between the caller and the InMemory store's
# internal snapshot, so an in-place caller mutation would corrupt the stored
# state and break CAS equality checks. The SQLite backend was already isolated
# by its JSON round trip.


def _clone_run(snapshot: AgentRunSnapshot) -> AgentRunSnapshot:
    return replace(snapshot, metadata=copy.deepcopy(snapshot.metadata))


def _clone_agent(snapshot: AgentRuntimeSnapshot) -> AgentRuntimeSnapshot:
    return replace(snapshot, metadata=copy.deepcopy(snapshot.metadata))


def _clone_event(event: AgentLifecycleEvent) -> AgentLifecycleEvent:
    return replace(event, payload=copy.deepcopy(event.payload))


def _new_event_id() -> str:
    return f"evt_{uuid.uuid4().hex}"
