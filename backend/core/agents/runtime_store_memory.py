"""Agent 运行时内存存储:带终态自动裁剪的 InMemoryAgentRuntimeStore。(拆自 runtime_store.py)"""

from __future__ import annotations

from collections.abc import Mapping
from typing import Any
import asyncio
import os
import time
from backend.core.agents.runtime_store_model import (
    AgentLifecycleEvent,
    AgentRunSnapshot,
    AgentRuntimeSnapshot,
    TERMINAL_AGENT_STATUSES,
    _clone_agent,
    _clone_event,
    _clone_run,
    _new_event_id,
    _optional_text,
    _payload_copy,
    _required_text,
    _resolve_upsert,
)


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
        """Apply one agent upsert and its lifecycle event atomically.

        A crash between the two separate calls used to leave the agent row
        updated with its event missing (or vice versa); both mutations now
        commit under one lock acquisition (one transaction on SQLite).
        """

        incoming = _clone_agent(snapshot)
        key = (incoming.root_id, incoming.agent_id)
        normalized_event_id = _optional_text(event_id) or _new_event_id()
        normalized_type = _required_text(event_type, field_name="event_type")
        normalized_payload = _payload_copy(event_payload or {})
        normalized_created_at = (
            time.time() if event_created_at is None else float(event_created_at)
        )
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
            existing_event = self._events_by_id.get(
                (incoming.root_id, normalized_event_id)
            )
            if existing_event is not None:
                return _clone_agent(resolved), _clone_event(existing_event)
            events = self._events.setdefault(incoming.root_id, [])
            event = AgentLifecycleEvent(
                root_id=incoming.root_id,
                sequence=len(events) + 1,
                event_id=normalized_event_id,
                event_type=normalized_type,
                agent_id=event_agent_id,
                payload=normalized_payload,
                created_at=normalized_created_at,
            )
            events.append(event)
            self._events_by_id[(incoming.root_id, normalized_event_id)] = event
            return _clone_agent(resolved), _clone_event(event)

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
