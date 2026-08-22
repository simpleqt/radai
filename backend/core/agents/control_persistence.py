"""AgentControl 持久化域:持久 run 保障、生命周期事件、agent 信息落库、run 终结与孤儿清理。(拆自 control.py,方法经类属性绑定回 AgentControl)"""

from __future__ import annotations

from backend.config.loggers import get_logger

from dataclasses import replace
from typing import Any
import os
import time
import uuid
from backend.core.agents.control_models import (
    AgentCancellationToken,
    AgentInfo,
    AgentStateError,
    AgentStatus,
    _AgentRecord,
)

logger = get_logger(__name__)


def _recovery_takeover_grace_seconds() -> float:
    """Freshness guard for adopting a persisted 'running' tree.

    Version CAS only rejects *simultaneous* claims; a second worker arriving
    later reads the new version and would re-claim a tree whose original
    owner is still alive (shared-SQLite multi-process deployments). With a
    positive grace, a running tree whose row was updated more recently than
    the grace window is treated as still owned and recovery fails closed.
    Default 0 keeps the single-worker behavior (immediate restart recovery)
    unchanged.
    """

    try:
        return max(
            0.0,
            float(os.getenv("AGENT_RUNTIME_RECOVERY_TAKEOVER_GRACE_SECONDS", "0")),
        )
    except (TypeError, ValueError):
        return 0.0


async def _ensure_persistent_run(self) -> None:
    if self._store is None or self._persistence_ready:
        return
    from backend.core.agents.runtime_store import AgentRunSnapshot

    async with self._persistence_lock:
        if self._persistence_ready:
            return
        existing = await self._store.load_run(self.root_agent_id)
        if existing is not None and existing.status in {
            "completed",
            "failed",
            "timed_out",
            "interrupted",
            "cancelled",
        }:
            raise AgentStateError(
                f"persisted agent tree {self.root_agent_id} is already "
                f"terminal ({existing.status})"
            )
        takeover_grace = _recovery_takeover_grace_seconds()
        if (
            existing is not None
            and existing.status == "running"
            and takeover_grace > 0
            and (time.time() - existing.updated_at) < takeover_grace
        ):
            # Shared-store deployments: a freshly updated running row means
            # another worker probably still owns this tree. Re-claiming it
            # would reset RUNNING children to PENDING and double-execute
            # side-effecting agents.
            raise AgentStateError(
                f"persisted agent tree {self.root_agent_id} appears to be "
                "owned by a live worker (running row updated "
                f"{time.time() - existing.updated_at:.1f}s ago, grace "
                f"{takeover_grace:g}s)"
            )
        if existing is not None:
            self._restore_persisted_budget(existing.metadata, existing.started_at)
        now = time.time()
        run_metadata = {
            **(existing.metadata if existing is not None else {}),
            **self._run_metadata,
            "budget": self._budget_config_metadata(),
            # Diagnostic claim marker for shared-store deployments; contains
            # no secrets and is overwritten by each legitimate owner.
            "runtime_owner": {
                "pid": os.getpid(),
                "claimed_at": now,
            },
        }
        snapshot = AgentRunSnapshot(
            root_id=self.root_agent_id,
            status="running",
            metadata=run_metadata,
            created_at=(
                existing.created_at if existing is not None else self._created_at
            ),
            updated_at=now,
            started_at=(existing.started_at if existing is not None else now),
            version=(existing.version if existing is not None else 0),
        )
        persisted = await self._store.upsert_run(
            snapshot,
            expected_version=(existing.version if existing is not None else 0),
        )
        self._created_at = persisted.created_at
        self._run_started_at = persisted.started_at
        self._run_version = persisted.version
        self._run_metadata = dict(persisted.metadata)
        event_type = (
            "agent_tree.resumed" if existing is not None else "agent_tree.started"
        )
        event_timestamp = now if existing is not None else persisted.started_at
        await self._store.append_lifecycle_event(
            self.root_agent_id,
            event_type=event_type,
            payload={"status": "running", **persisted.metadata},
            event_id=self._lifecycle_event_id(
                event_type,
                timestamp=event_timestamp,
            ),
            created_at=event_timestamp,
        )
        persisted_agents = await self._store.load_tree_agents(self.root_agent_id)
        recovered_agents: dict[str, Any] = {}
        for item in persisted_agents:
            recovered = item
            if item.status in {
                AgentStatus.RUNNING.value,
                AgentStatus.INTERRUPTING.value,
            }:
                recovered_at = time.time()
                recovered = await self._store.upsert_agent(
                    replace(
                        item,
                        status=AgentStatus.PENDING.value,
                        updated_at=recovered_at,
                        started_at=None,
                        finished_at=None,
                        error=None,
                    ),
                    expected_version=item.version,
                )
                await self._store.append_lifecycle_event(
                    self.root_agent_id,
                    event_type="agent.recovered",
                    agent_id=item.agent_id,
                    payload={
                        "from_status": item.status,
                        "status": AgentStatus.PENDING.value,
                        "version": recovered.version,
                    },
                    event_id=self._lifecycle_event_id(
                        "agent.recovered",
                        agent_id=item.agent_id,
                        timestamp=recovered_at,
                    ),
                    created_at=recovered_at,
                )
            recovered_agents[recovered.agent_id] = recovered
            if recovered.agent_id in self._records:
                continue
            try:
                recovered_status = AgentStatus(recovered.status)
            except ValueError:
                if recovered.status == "cancelled":
                    recovered_status = AgentStatus.INTERRUPTED
                else:
                    raise AgentStateError(
                        "persisted agent has unsupported status: "
                        f"{recovered.agent_id}={recovered.status}"
                    ) from None
            recovered_record = _AgentRecord(
                agent_id=recovered.agent_id,
                parent_id=recovered.parent_id or self.root_agent_id,
                depth=recovered.depth,
                status=recovered_status,
                metadata=dict(recovered.metadata),
                created_at=recovered.created_at,
                cancellation=AgentCancellationToken(parent=self.cancellation),
                started_at=recovered.started_at,
                finished_at=recovered.finished_at,
                error=recovered.error,
                restored=True,
            )
            if recovered_status in {
                AgentStatus.INTERRUPTED,
                AgentStatus.TIMED_OUT,
            }:
                recovered_record.cancellation._cancel(
                    recovered.error or recovered_status.value
                )
            self._records[recovered.agent_id] = recovered_record
        self._persisted_agents = recovered_agents
        self._agent_versions.update(
            {agent_id: item.version for agent_id, item in recovered_agents.items()}
        )
        self._persistence_ready = True


def _lifecycle_event_id(
    self,
    event_type: str,
    *,
    agent_id: str | None = None,
    timestamp: float | None = None,
) -> str:
    identity = (
        f"{self.root_agent_id}:{agent_id or self.root_agent_id}:"
        f"{event_type}:{timestamp or 0.0:.9f}"
    )
    return f"evt_{uuid.uuid5(uuid.NAMESPACE_URL, identity).hex}"


async def _persist_agent_info(
    self,
    info: AgentInfo,
    *,
    event_type: str,
) -> None:
    if self._store is None:
        return
    from backend.core.agents.runtime_store import AgentRuntimeSnapshot

    await self._ensure_persistent_run()
    async with self._persistence_lock:
        existing = self._persisted_agents.get(info.agent_id)
        # _ensure_persistent_run() loads the tree once and populates both
        # caches. A miss here is a genuinely new agent; reloading the whole
        # tree for every registration creates O(N²) persistence traffic.
        version = (
            existing.version
            if existing is not None
            else self._agent_versions.get(info.agent_id, 0)
        )
        incoming = AgentRuntimeSnapshot(
            root_id=self.root_agent_id,
            agent_id=info.agent_id,
            parent_id=info.parent_id,
            depth=info.depth,
            status=info.status.value,
            metadata=info.metadata,
            created_at=(
                existing.created_at if existing is not None else info.created_at
            ),
            updated_at=time.time(),
            started_at=info.started_at,
            finished_at=info.finished_at,
            error=info.error,
            version=version,
        )
        # Store implementations intentionally reject any mutation after a
        # terminal state.  Ignore only the volatile update timestamp when
        # deciding whether a retry is the exact same logical transition.
        same_logical_state = bool(
            existing is not None
            and AgentRuntimeSnapshot(
                root_id=incoming.root_id,
                agent_id=incoming.agent_id,
                parent_id=incoming.parent_id,
                depth=incoming.depth,
                status=incoming.status,
                metadata=incoming.metadata,
                created_at=incoming.created_at,
                updated_at=existing.updated_at,
                started_at=incoming.started_at,
                finished_at=incoming.finished_at,
                error=incoming.error,
                version=existing.version,
            )
            == existing
        )
        used_combined_write = False
        if same_logical_state:
            persisted = existing
        else:
            from backend.core.agents.runtime_store import (
                AgentRuntimeTerminalStateError,
            )

            # Prefer the atomic snapshot+event write: separate commits let a
            # crash between them leave the agent row updated with its event
            # missing (or the reverse). CAS semantics make the persisted
            # values predictable, so the event payload/timestamp can be
            # computed up front from the incoming snapshot.
            combined_writer = getattr(
                self._store, "upsert_agent_with_event", None
            )
            try:
                if callable(combined_writer):
                    event_timestamp = _agent_event_timestamp(
                        event_type,
                        incoming,
                        has_existing=existing is not None,
                    )
                    persisted, _event = await combined_writer(
                        incoming,
                        expected_version=version,
                        event_type=event_type,
                        event_agent_id=info.agent_id,
                        event_payload={
                            "parent_id": info.parent_id,
                            "depth": info.depth,
                            "status": incoming.status,
                            "metadata": incoming.metadata,
                            "error": incoming.error,
                            "version": version + 1,
                        },
                        event_id=self._lifecycle_event_id(
                            event_type,
                            agent_id=info.agent_id,
                            timestamp=event_timestamp,
                        ),
                        event_created_at=event_timestamp,
                    )
                    used_combined_write = True
                else:
                    persisted = await self._store.upsert_agent(
                        incoming,
                        expected_version=version,
                    )
            except AgentRuntimeTerminalStateError:
                # 锁序竞争:被取消的 owner 已抢先把自己的终态落库(store 的
                # 终态不可变规则拒绝一切后续写)。首个终态胜出,本次写入按
                # 陈旧数据跳过——向上抛会打断 interrupt/finalize 的批量落库,
                # 剩余 agent 快照全部丢失且 TerminalStateError 泄给调用方。
                logger.info(
                    "agent %s already terminal in store; skipping stale %s "
                    "persist (incoming status=%s)",
                    info.agent_id,
                    event_type,
                    incoming.status,
                )
                try:
                    for snapshot in await self._store.load_tree_agents(
                        self.root_agent_id
                    ):
                        if snapshot.agent_id == info.agent_id:
                            self._agent_versions[info.agent_id] = snapshot.version
                            self._persisted_agents[info.agent_id] = snapshot
                            break
                except Exception:
                    logger.debug(
                        "agent %s terminal snapshot refresh skipped",
                        info.agent_id,
                        exc_info=True,
                    )
                return
        self._agent_versions[info.agent_id] = persisted.version
        self._persisted_agents[info.agent_id] = persisted
        if used_combined_write:
            return
        event_timestamp = _agent_event_timestamp(
            event_type,
            persisted,
            has_existing=existing is not None,
        )
        await self._store.append_lifecycle_event(
            self.root_agent_id,
            event_type=event_type,
            agent_id=info.agent_id,
            payload={
                "parent_id": info.parent_id,
                "depth": info.depth,
                "status": persisted.status,
                "metadata": persisted.metadata,
                "error": persisted.error,
                "version": persisted.version,
            },
            event_id=self._lifecycle_event_id(
                event_type,
                agent_id=info.agent_id,
                timestamp=event_timestamp,
            ),
            created_at=event_timestamp,
        )


def _agent_event_timestamp(
    event_type: str,
    snapshot: Any,
    *,
    has_existing: bool,
) -> float:
    """Shared event-timestamp rule for the combined and two-step writes.

    Under CAS the persisted snapshot keeps the incoming snapshot's
    status/timestamps, so computing from ``incoming`` before the write is
    equivalent to computing from ``persisted`` after it.
    """

    if event_type == "agent.registered":
        return snapshot.updated_at if has_existing else snapshot.created_at
    if snapshot.finished_at is not None:
        return snapshot.finished_at
    if snapshot.status == AgentStatus.RUNNING.value:
        return snapshot.started_at or snapshot.updated_at
    return snapshot.updated_at


async def finalize_run(
    self,
    status: AgentStatus | str,
    *,
    error: str | None = None,
    metadata: dict[str, Any] | None = None,
) -> None:
    """Persist the root outcome before closing/cancelling the live tree."""

    normalized_status = (
        status.value if isinstance(status, AgentStatus) else str(status).strip()
    )
    normalized_error = (str(error).strip() or None) if error is not None else None
    completion_metadata = dict(metadata or {})
    if normalized_status not in {
        "completed",
        "failed",
        "timed_out",
        "interrupted",
        "cancelled",
    }:
        raise ValueError("run status must be terminal")
    if self._store is None:
        return
    from backend.core.agents.runtime_store import AgentRunSnapshot

    if not self._persistence_ready:
        existing_run = await self._store.load_run(self.root_agent_id)
        if existing_run is not None and existing_run.status in {
            "completed",
            "failed",
            "timed_out",
            "interrupted",
            "cancelled",
        }:
            existing_error = str(existing_run.error or "").strip() or None
            if (
                existing_run.status == normalized_status
                and existing_error == normalized_error
            ):
                return
            raise AgentStateError(
                f"persisted agent tree {self.root_agent_id} is already "
                f"terminal ({existing_run.status})"
            )
    await self._ensure_persistent_run()
    await self._finalize_orphaned_persisted_agents()
    async with self._persistence_lock:
        existing_run = await self._store.load_run(self.root_agent_id)
        if existing_run is None:
            raise AgentStateError(
                f"persisted agent tree {self.root_agent_id} disappeared"
            )
        if existing_run.status in {
            "completed",
            "failed",
            "timed_out",
            "interrupted",
            "cancelled",
        }:
            existing_error = str(existing_run.error or "").strip() or None
            if (
                existing_run.status == normalized_status
                and existing_error == normalized_error
            ):
                self._run_version = existing_run.version
                return
            raise AgentStateError(
                f"persisted agent tree {self.root_agent_id} is already "
                f"terminal ({existing_run.status})"
            )
        now = time.time()
        persisted = await self._store.upsert_run(
            AgentRunSnapshot(
                root_id=self.root_agent_id,
                status=normalized_status,
                metadata={
                    **existing_run.metadata,
                    **self._run_metadata,
                    **completion_metadata,
                    "budget": self._budget_config_metadata(),
                },
                created_at=existing_run.created_at,
                updated_at=now,
                started_at=(
                    existing_run.started_at
                    or self._run_started_at
                    or self._created_at
                ),
                finished_at=now,
                error=normalized_error,
                version=existing_run.version,
            ),
            expected_version=existing_run.version,
        )
        self._run_version = persisted.version
        await self._store.append_lifecycle_event(
            self.root_agent_id,
            event_type=f"agent_tree.{normalized_status}",
            payload={"status": normalized_status, "error": normalized_error},
            event_id=self._lifecycle_event_id(
                f"agent_tree.{normalized_status}",
                timestamp=persisted.finished_at,
            ),
            created_at=persisted.finished_at,
        )


async def _finalize_orphaned_persisted_agents(self) -> None:
    """Close crash-left child rows that were not resumed by this worker."""

    if self._store is None:
        return
    terminal_statuses = {
        AgentStatus.COMPLETED.value,
        AgentStatus.FAILED.value,
        AgentStatus.TIMED_OUT.value,
        AgentStatus.INTERRUPTED.value,
    }
    async with self._condition:
        live_agent_ids = {
            agent_id
            for agent_id, record in self._records.items()
            if not record.restored
        }
    orphaned = [
        snapshot
        for agent_id, snapshot in self._persisted_agents.items()
        if agent_id not in live_agent_ids
        and snapshot.status not in terminal_statuses
    ]
    for snapshot in orphaned:
        await self._persist_agent_info(
            AgentInfo(
                agent_id=snapshot.agent_id,
                parent_id=snapshot.parent_id or self.root_agent_id,
                depth=snapshot.depth,
                status=AgentStatus.INTERRUPTED,
                metadata=snapshot.metadata,
                created_at=snapshot.created_at,
                started_at=snapshot.started_at,
                finished_at=time.time(),
                error="agent was not resumed before tree finalization",
            ),
            event_type="agent.interrupted",
        )
