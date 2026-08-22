"""AgentControl 槽位域:注册/容量预约/等待者管理、owner 清理、槽位释放与中断标记/传播。(拆自 control.py,方法经类属性绑定回 AgentControl)"""

from __future__ import annotations

from backend.config.loggers import get_logger

from typing import Any
import asyncio
import time
from backend.core.agents.control_models import (
    AgentCancellationToken,
    AgentCapacityError,
    AgentDepthError,
    AgentInfo,
    AgentStateError,
    AgentStatus,
    _AgentRecord,
)

logger = get_logger(__name__)


async def register(
    self,
    agent_id: str,
    *,
    parent_id: str | None = None,
    metadata: dict[str, Any] | None = None,
) -> AgentInfo:
    normalized_id = self._normalize_agent_id(agent_id)
    normalized_parent = self._normalize_agent_id(
        parent_id or self.root_agent_id,
        field_name="parent_id",
    )
    await self._ensure_persistent_run()
    await self.check_budget()
    event_type = "agent.registered"
    async with self._condition:
        if self._closed:
            raise AgentStateError("request agent tree is closed")
        if normalized_id == self.root_agent_id:
            raise AgentStateError(f"agent already registered: {normalized_id}")
        existing_record = self._records.get(normalized_id)
        if existing_record is not None:
            if (
                existing_record.restored
                and existing_record.status is AgentStatus.PENDING
                and existing_record.task is None
            ):
                if existing_record.parent_id != normalized_parent:
                    raise AgentStateError(
                        f"restored agent {normalized_id} belongs to parent "
                        f"{existing_record.parent_id}, not {normalized_parent}"
                    )
                existing_record.restored = False
                if metadata is not None:
                    existing_record.metadata = dict(metadata)
                snapshot = existing_record.snapshot()
                record = existing_record
                event_type = "agent.claimed"
            else:
                raise AgentStateError(f"agent already registered: {normalized_id}")
        else:
            record = None
        if record is not None:
            self._condition.notify_all()
        else:
            if len(self._records) >= self.limits.max_spawned_agents:
                raise AgentCapacityError(
                    "subagent spawn limit reached "
                    f"({self.limits.max_spawned_agents})"
                )
            depth = self._depth_for_parent(normalized_parent)
            if depth > self.limits.max_depth:
                raise AgentDepthError(
                    f"agent depth {depth} exceeds limit {self.limits.max_depth}"
                )
            if normalized_parent != self.root_agent_id:
                parent = self._record(normalized_parent)
                if parent.status is not AgentStatus.RUNNING:
                    raise AgentStateError(
                        f"parent agent {normalized_parent} is {parent.status.value}"
                    )
            record = _AgentRecord(
                agent_id=normalized_id,
                parent_id=normalized_parent,
                depth=depth,
                status=AgentStatus.PENDING,
                metadata=dict(metadata or {}),
                created_at=time.time(),
                cancellation=AgentCancellationToken(parent=self.cancellation),
            )
            self._records[normalized_id] = record
            self._condition.notify_all()
            snapshot = record.snapshot()
    try:
        await self._persist_agent_info(
            snapshot,
            event_type=event_type,
        )
    except BaseException:
        async with self._condition:
            current = self._records.get(normalized_id)
            if (
                current is record
                and current.status is AgentStatus.PENDING
                and current.task is None
                and event_type == "agent.registered"
            ):
                self._records.pop(normalized_id, None)
                self._condition.notify_all()
            elif current is record and event_type == "agent.claimed":
                current.restored = True
                self._condition.notify_all()
        raise
    return snapshot


async def reserve(self, agent_id: str, *, wait: bool = True) -> AgentInfo:
    """Reserve one active slot fairly and transition the agent to running.

    Low-level callers must finish the lease with ``complete``, ``fail``, or
    ``timeout``.  Prefer :meth:`spawn`, which finalizes it automatically.
    If an owner task exits without doing so, the control plane fails or
    interrupts the abandoned record and releases its slot.
    """

    await self._ensure_persistent_run()
    await self.check_budget()
    failed_snapshot: AgentInfo | None = None
    try:
        async with self._condition:
            record = self._record(agent_id)
            normalized_id = record.agent_id
            if record.cancellation.cancelled:
                raise asyncio.CancelledError
            if record.status is not AgentStatus.PENDING:
                raise AgentStateError(
                    f"agent {normalized_id} cannot start from {record.status.value}"
                )
            if record.task is not None:
                raise AgentStateError(
                    f"agent {normalized_id} is already waiting for a slot"
                )
            if normalized_id not in self._waiting:
                self._waiting.append(normalized_id)
            # Bind the coroutine before it obtains capacity so an interrupt can
            # cancel queued work immediately as well as running work.
            owner_task = asyncio.current_task()
            record.task = owner_task
            try:
                while True:
                    record.cancellation.raise_if_cancelled()
                    if record.status is not AgentStatus.PENDING:
                        raise AgentStateError(
                            f"agent {normalized_id} cannot start from "
                            f"{record.status.value}"
                        )
                    is_first = (
                        bool(self._waiting) and self._waiting[0] == normalized_id
                    )
                    has_capacity = (
                        self._active_count < self.limits.max_active_agents
                    )
                    if is_first and has_capacity:
                        self._waiting.popleft()
                        self._transition(record, AgentStatus.RUNNING)
                        record.started_at = time.time()
                        record.slot_reserved = True
                        self._active_count += 1
                        if owner_task is not None:
                            owner_task.add_done_callback(
                                lambda completed, reserved_id=normalized_id: (
                                    self._schedule_owner_task_cleanup(
                                        reserved_id,
                                        completed,
                                    )
                                )
                            )
                        self._condition.notify_all()
                        snapshot = record.snapshot()
                        break
                    if not wait:
                        raise AgentCapacityError(
                            "subagent active limit reached "
                            f"({self.limits.max_active_agents})"
                        )
                    await self._condition.wait()
            except BaseException as exc:
                self._remove_waiter(normalized_id)
                if record.status is AgentStatus.INTERRUPTING:
                    self._release_slot_locked(
                        record,
                        execution_task=owner_task,
                    )
                    self._transition(record, AgentStatus.INTERRUPTED)
                    record.finished_at = time.time()
                    failed_snapshot = record.snapshot()
                elif record.status is AgentStatus.PENDING:
                    record.task = None
                    if isinstance(exc, asyncio.CancelledError):
                        self._transition(record, AgentStatus.INTERRUPTED)
                        record.error = "agent reservation cancelled"
                        record.finished_at = time.time()
                        record.cancellation._cancel("agent reservation cancelled")
                        failed_snapshot = record.snapshot()
                self._condition.notify_all()
                raise
    except BaseException:
        if failed_snapshot is not None:
            persistence_task = asyncio.create_task(
                self._persist_agent_info(
                    failed_snapshot,
                    event_type=f"agent.{failed_snapshot.status.value}",
                )
            )
            while not persistence_task.done():
                try:
                    await asyncio.shield(persistence_task)
                except asyncio.CancelledError:
                    continue
                except Exception:
                    break
            try:
                persistence_task.result()
            except Exception:
                logger.exception(
                    "failed to persist cancelled agent reservation: %s",
                    failed_snapshot.agent_id,
                )
        raise
    try:
        await self._persist_agent_info(
            snapshot,
            event_type="agent.running",
        )
    except BaseException as exc:
        await self._finish(
            normalized_id,
            AgentStatus.FAILED,
            error=f"failed to persist running state: {exc}",
            execution_task=owner_task,
        )
        raise
    return snapshot


def _remove_waiter(self, agent_id: str) -> None:
    try:
        self._waiting.remove(agent_id)
    except ValueError:
        pass


def _schedule_owner_task_cleanup(
    self,
    agent_id: str,
    owner_task: asyncio.Task[Any],
) -> None:
    record = self._records.get(agent_id)
    if record is None or record.task is not owner_task:
        return
    loop = owner_task.get_loop()
    if loop.is_closed():
        # The async finalizer can no longer run. Release the reserved slot
        # synchronously (no coroutine can race us on a closed loop) so the
        # ghost reservation cannot pin max_active_agents for the lifetime of
        # this control instance; full terminal finalization stays undone and
        # is reported for the recovery path.
        if record.slot_reserved:
            self._active_count = max(0, self._active_count - 1)
            record.slot_reserved = False
        if record.task is owner_task:
            record.task = None
        logger.warning(
            "agent %s owner loop already closed; released its slot without "
            "async finalization (status=%s)",
            agent_id,
            record.status.value,
        )
        return
    cleanup_task = loop.create_task(
        self._finalize_abandoned_owner(agent_id, owner_task)
    )
    cleanup_task.add_done_callback(self._consume_cleanup_result)


def _consume_cleanup_result(task: asyncio.Task[Any]) -> None:
    try:
        task.result()
    except asyncio.CancelledError:
        pass
    except Exception:
        logger.exception("failed to finalize abandoned agent execution")


async def _finalize_abandoned_owner(
    self,
    agent_id: str,
    owner_task: asyncio.Task[Any],
) -> None:
    async with self._condition:
        record = self._records.get(agent_id)
        already_finalized = (
            record is None
            or record.task is not owner_task
            or record.status.terminal
        )
    if already_finalized:
        # The in-memory transition is done, but a finalizer-timeout
        # cancellation may have interrupted the persistence awaits after
        # it. Replay any unconfirmed terminal writes so a restart does not
        # resurrect this agent as "running".
        await self._flush_pending_terminal_persists(agent_id)
        return

    if owner_task.cancelled():
        status = AgentStatus.INTERRUPTED
        error = "agent execution task was cancelled"
    else:
        task_error = owner_task.exception()
        status = AgentStatus.FAILED
        error = (
            str(task_error)
            if task_error is not None
            else "agent execution task exited without finalizing its lease"
        )
    await self._finish(
        agent_id,
        status,
        error=error,
        execution_task=owner_task,
    )


def _release_slot_locked(
    self,
    record: _AgentRecord,
    *,
    execution_task: asyncio.Task[Any] | None,
) -> None:
    if record.task is not None and record.task is not execution_task:
        raise AgentStateError(
            f"agent {record.agent_id} can only be finalized by its execution task"
        )
    if record.slot_reserved:
        self._active_count = max(0, self._active_count - 1)
        record.slot_reserved = False
    if record.task is execution_task:
        record.task = None


def _mark_interrupted_locked(
    self,
    target_ids: list[str],
    *,
    reason: str,
    current_task: asyncio.Task[Any] | None,
) -> tuple[list[AgentInfo], list[asyncio.Task[Any]]]:
    snapshots: list[AgentInfo] = []
    tasks_to_cancel: list[asyncio.Task[Any]] = []
    for target_id in reversed(target_ids):
        record = self._record(target_id)
        if record.status.terminal:
            snapshots.append(record.snapshot())
            continue
        self._remove_waiter(target_id)
        record.cancellation._cancel(reason)
        record.error = reason
        if record.status is AgentStatus.INTERRUPTING:
            if record.task is not None and record.task is not current_task:
                tasks_to_cancel.append(record.task)
            snapshots.append(record.snapshot())
            continue
        if record.task is None:
            self._transition(record, AgentStatus.INTERRUPTED)
            record.finished_at = time.time()
        else:
            # Keep the execution lease until the coroutine has actually
            # exited.  This prevents cancellation-swallowing work from
            # exceeding the configured concurrency limit.
            self._transition(record, AgentStatus.INTERRUPTING)
            if record.task is not current_task:
                tasks_to_cancel.append(record.task)
        snapshots.append(record.snapshot())
    return snapshots, tasks_to_cancel


def _descendant_ids(self, agent_id: str) -> list[str]:
    descendants: list[str] = []
    frontier = [self._normalize_agent_id(agent_id)]
    while frontier:
        parent = frontier.pop()
        children = [
            record.agent_id
            for record in self._records.values()
            if record.parent_id == parent
        ]
        descendants.extend(children)
        frontier.extend(children)
    return descendants


async def interrupt(
    self,
    agent_id: str,
    *,
    reason: str = "agent interrupted",
) -> list[AgentInfo]:
    """Interrupt an agent and every registered descendant."""

    try:
        await self._ensure_persistent_run()
    except AgentStateError as exc:
        # A cleanup call may race with a terminal record left by an
        # earlier attempt that reused the same root id (for example after
        # a worker restart).  The live manager still owns its in-process
        # tasks, so close and cancel that tree locally without trying to
        # mutate the already-terminal durable record.
        if "already terminal" not in str(exc):
            raise
        tasks_to_cancel: list[asyncio.Task[Any]] = []
        snapshots: list[AgentInfo] = []
        async with self._condition:
            self._closed = True
            self.cancellation._cancel(reason)
            target_ids = list(self._records)
            if target_ids:
                snapshots, tasks_to_cancel = self._mark_interrupted_locked(
                    target_ids,
                    reason=reason,
                    current_task=asyncio.current_task(),
                )
            self._condition.notify_all()
        for task in tasks_to_cancel:
            task.cancel()
        return snapshots
    normalized_id = self._normalize_agent_id(agent_id)
    async with self._condition:
        if normalized_id == self.root_agent_id:
            self._closed = True
            self.cancellation._cancel(reason)
            target_ids = list(self._records)
        else:
            self._record(normalized_id)
            target_ids = [
                normalized_id,
                *self._descendant_ids(normalized_id),
            ]
        current_task = asyncio.current_task()
        snapshots, tasks_to_cancel = self._mark_interrupted_locked(
            target_ids,
            reason=reason,
            current_task=current_task,
        )
        self._condition.notify_all()
    for task in tasks_to_cancel:
        task.cancel()
    # 与 _finish 同保障:先按 agent 键登记补写队列再逐条确认。此循环常在流
    # 拆卸的 finally 里执行,双重取消可把它打断在半途;无登记时剩余快照永久
    # 停留在 pending/running,且这些 agent 仍在 self._records 中,孤儿修复
    # 也不会兜底。登记后由后续 _finish / abandoned-owner flush 重放。
    for snapshot in snapshots:
        key = self._normalize_agent_id(snapshot.agent_id)
        self._pending_terminal_persists.setdefault(key, []).append(snapshot)
    for key in dict.fromkeys(
        self._normalize_agent_id(snapshot.agent_id) for snapshot in snapshots
    ):
        pending = self._pending_terminal_persists.get(key)
        if pending:
            await self._drain_pending_terminal_persists(key, pending)
    return snapshots
