"""Request-tree lifecycle control for delegated agents.

The control object owns the live in-memory invariants that every subagent
execution path needs and can mirror every transition to a durable store:

* a bounded number of spawned and concurrently running agents;
* parent/depth validation;
* observable lifecycle state;
* cancellation propagation to descendants; and
* guaranteed slot release on success, failure, timeout, or cancellation.

One instance should be shared by the root agent and all children created for a
single request tree.  The optional store keeps the execution paths independent
from the selected persistence backend.
"""

from __future__ import annotations

import asyncio
import logging
import os
import threading
import time
from collections import deque
from collections.abc import AsyncIterator, Callable, Coroutine, Iterator
from contextlib import asynccontextmanager, contextmanager
from contextvars import ContextVar, Token
from typing import TYPE_CHECKING, Any

if TYPE_CHECKING:
    from backend.core.agents.runtime_store import AgentRuntimeStore

from backend.core.agents import control_budget as _budget
from backend.core.agents import control_persistence as _persistence
from backend.core.agents import control_slots as _slots
from backend.core.agents.control_models import (  # noqa: F401  (兼容 re-export)
    AgentBudgetExceededError,
    AgentCancellationToken,
    AgentCapacityError,
    AgentControlError,
    AgentControlLimits,
    AgentDepthError,
    AgentInfo,
    AgentNotFoundError,
    AgentRuntimeManager,
    AgentStateError,
    AgentStateMachine,
    AgentStatus,
    RunBudget,
    RunBudgetSnapshot,
    _AgentRecord,
    _RunBudgetLedger,
)

logger = logging.getLogger(__name__)


def _finalizer_timeout_seconds() -> float:
    try:
        return max(
            0.1,
            float(os.getenv("AGENT_FINALIZER_TIMEOUT_SECONDS", "15") or "15"),
        )
    except (TypeError, ValueError):
        return 15.0


def _consume_finalizer_result(task: asyncio.Future[Any]) -> None:
    try:
        task.result()
    except asyncio.CancelledError:
        logger.debug("agent finalizer task was cancelled")
    except Exception:
        logger.exception("agent finalizer task failed")


class AgentControl:
    """Live :class:`AgentRuntimeManager` for one root request tree.

    ``AgentControl`` remains as a compatibility name while callers migrate to
    the manager interface.  Supplying ``store`` durably mirrors tree and child
    lifecycle transitions while keeping scheduling local to this process.
    """

    def __init__(
        self,
        root_agent_id: str,
        *,
        limits: AgentControlLimits | None = None,
        budget: RunBudget | None = None,
        cancellation: AgentCancellationToken | None = None,
        store: AgentRuntimeStore | None = None,
        run_metadata: dict[str, Any] | None = None,
    ) -> None:
        root = str(root_agent_id or "").strip()
        if not root:
            raise ValueError("root_agent_id must not be empty")
        if limits is not None and budget is not None:
            raise ValueError("provide either budget or limits, not both")
        self.root_agent_id = root
        self.budget = budget or limits or RunBudget()
        self._budget_ledger = _RunBudgetLedger(self.budget)
        # Compatibility for existing code and configuration.  New consumers
        # should read ``budget`` from the AgentRuntimeManager contract.
        self.limits = self.budget
        self.cancellation = cancellation or AgentCancellationToken()
        self._store = store
        self._run_metadata = dict(run_metadata or {})
        self._created_at = time.time()
        self._run_started_at: float | None = None
        self._run_version = 0
        self._agent_versions: dict[str, int] = {}
        self._persisted_agents: dict[str, Any] = {}
        try:
            self._owner_loop: asyncio.AbstractEventLoop | None = (
                asyncio.get_running_loop()
            )
        except RuntimeError:
            self._owner_loop = None
        self._owner_loop_lock = threading.Lock()
        self._persistence_lock = asyncio.Lock()
        self._persistence_ready = False
        self._records: dict[str, _AgentRecord] = {}
        self._waiting: deque[str] = deque()
        self._active_count = 0
        self._closed = False
        self._condition = asyncio.Condition()
        # Terminal snapshots whose persistence has not been confirmed yet.
        # _finish transitions in-memory state under the lock but persists
        # outside it; a finalizer-timeout cancellation between the two left
        # the store on "running" forever because abandoned-owner cleanup
        # saw a terminal record and returned. These snapshots let any later
        # cleanup replay the missing writes (persistence is an idempotent
        # upsert).
        self._pending_terminal_persists: dict[str, list[AgentInfo]] = {}

    @property
    def active_count(self) -> int:
        return self._active_count

    @property
    def spawned_count(self) -> int:
        return len(self._records)

    @property
    def closed(self) -> bool:
        return self._closed

    @property
    def persistent(self) -> bool:
        return self._store is not None

    @property
    def run_metadata(self) -> dict[str, Any]:
        """Return non-secret metadata used to authorize runtime controls."""

        return dict(self._run_metadata)

    def _foreign_owner_loop(self) -> asyncio.AbstractEventLoop | None:
        """Return the owning loop when the current call must be forwarded.

        Blocking tool bridges copy the request context and run async tool code
        with ``asyncio.run`` in a worker thread.  That copied context includes
        this manager, but its asyncio locks and live tasks still belong to the
        request loop.  Bind lazily for managers constructed outside async code
        and keep all cross-thread budget work on that one owning loop.
        """

        current_loop = asyncio.get_running_loop()
        with self._owner_loop_lock:
            if self._owner_loop is None:
                self._owner_loop = current_loop
            owner_loop = self._owner_loop
        return None if owner_loop is current_loop else owner_loop

    async def _await_on_owner_loop(
        self,
        owner_loop: asyncio.AbstractEventLoop,
        coroutine_factory: Callable[[], Coroutine[Any, Any, Any]],
        *,
        operation: str,
    ) -> Any:
        """Submit one copied-context call to the manager's request loop."""

        if owner_loop.is_closed() or not owner_loop.is_running():
            raise AgentStateError(
                f"cannot {operation}: agent runtime owner event loop is not running"
            )
        coroutine = coroutine_factory()
        try:
            submitted = asyncio.run_coroutine_threadsafe(coroutine, owner_loop)
        except BaseException:
            coroutine.close()
            raise
        try:
            return await asyncio.wrap_future(submitted)
        except asyncio.CancelledError:
            submitted.cancel()
            raise

    @staticmethod
    def _normalize_agent_id(agent_id: str, *, field_name: str = "agent_id") -> str:
        normalized = str(agent_id or "").strip()
        if not normalized:
            raise ValueError(f"{field_name} must not be empty")
        return normalized

    def _record(self, agent_id: str) -> _AgentRecord:
        normalized_id = self._normalize_agent_id(agent_id)
        try:
            return self._records[normalized_id]
        except KeyError as exc:
            raise AgentNotFoundError(f"unknown agent: {normalized_id}") from exc

    def _depth_for_parent(self, parent_id: str) -> int:
        if parent_id == self.root_agent_id:
            return 1
        return self._record(parent_id).depth + 1

    @staticmethod
    def _transition(record: _AgentRecord, target: AgentStatus) -> None:
        AgentStateMachine.validate(
            record.status,
            target,
            agent_id=record.agent_id,
        )
        record.status = target

    async def _finish(
        self,
        agent_id: str,
        status: AgentStatus,
        *,
        error: str | None = None,
        allowed_from: set[AgentStatus] | None = None,
        execution_task: asyncio.Task[Any] | None = None,
    ) -> AgentInfo:
        if not status.terminal:
            raise ValueError("finish status must be terminal")
        await self._ensure_persistent_run()
        owner_task = execution_task or asyncio.current_task()
        tasks_to_cancel: list[asyncio.Task[Any]] = []
        descendant_snapshots: list[AgentInfo] = []
        async with self._condition:
            record = self._record(agent_id)
            normalized_id = record.agent_id
            if record.status.terminal:
                self._release_slot_locked(record, execution_task=owner_task)
                self._condition.notify_all()
                snapshot = record.snapshot()
            else:
                was_interrupting = record.status is AgentStatus.INTERRUPTING
                if (
                    allowed_from is not None
                    and record.status not in allowed_from
                    and not was_interrupting
                ):
                    raise AgentStateError(
                        f"agent {normalized_id} cannot transition from "
                        f"{record.status.value} to {status.value}"
                    )
                self._release_slot_locked(record, execution_task=owner_task)
                self._remove_waiter(normalized_id)
                self._transition(
                    record,
                    AgentStatus.INTERRUPTED if was_interrupting else status,
                )
                if not was_interrupting:
                    record.error = error
                record.finished_at = time.time()
                if record.status in {
                    AgentStatus.INTERRUPTED,
                    AgentStatus.TIMED_OUT,
                }:
                    record.cancellation._cancel(error or record.status.value)

                descendant_ids = self._descendant_ids(normalized_id)
                if descendant_ids:
                    (
                        descendant_snapshots,
                        tasks_to_cancel,
                    ) = self._mark_interrupted_locked(
                        descendant_ids,
                        reason=f"parent agent {normalized_id} finished",
                        current_task=owner_task,
                    )
                self._condition.notify_all()
                snapshot = record.snapshot()
        for task in tasks_to_cancel:
            task.cancel()
        # Track the terminal writes until each one is confirmed: a
        # finalizer-timeout cancellation inside a persist await must leave a
        # replayable record instead of a store stuck on "running". Merge into
        # any snapshots an earlier, cancelled persist loop has not confirmed
        # yet — an unconditional overwrite dropped them permanently.
        pending = self._pending_terminal_persists.setdefault(normalized_id, [])
        pending.extend([snapshot, *descendant_snapshots])
        await self._drain_pending_terminal_persists(normalized_id, pending)
        return snapshot

    async def _drain_pending_terminal_persists(
        self,
        normalized_id: str,
        pending: list,
    ) -> None:
        """Persist queued terminal snapshots with identity-guarded pops.

        The finalizer's own loop and the abandoned-owner flush can drain the
        same list concurrently (a cancelled ``to_thread`` persist only
        delivers its cancellation once the worker thread returns). Persisting
        the same snapshot twice is an idempotent upsert; popping an element
        the other drain has not persisted would silently skip it, so each
        drain removes only the exact snapshot it just confirmed.
        """

        while pending:
            info = pending[0]
            await self._persist_agent_info(
                info,
                event_type=f"agent.{info.status.value}",
            )
            if pending and pending[0] is info:
                pending.pop(0)
        if (
            not pending
            and self._pending_terminal_persists.get(normalized_id) is pending
        ):
            self._pending_terminal_persists.pop(normalized_id, None)

    async def _flush_pending_terminal_persists(self, agent_id: str) -> None:
        """Replay terminal persistence that a cancelled finalizer left behind."""

        normalized_id = self._normalize_agent_id(agent_id)
        pending = self._pending_terminal_persists.get(normalized_id)
        if not pending:
            self._pending_terminal_persists.pop(normalized_id, None)
            return
        await self._drain_pending_terminal_persists(normalized_id, pending)

    async def complete(self, agent_id: str) -> AgentInfo:
        return await self._finish(
            agent_id,
            AgentStatus.COMPLETED,
            allowed_from={AgentStatus.RUNNING},
        )

    async def fail(self, agent_id: str, error: str | BaseException) -> AgentInfo:
        return await self._finish(agent_id, AgentStatus.FAILED, error=str(error))

    async def timeout(self, agent_id: str, error: str | None = None) -> AgentInfo:
        return await self._finish(
            agent_id,
            AgentStatus.TIMED_OUT,
            error=error or "agent execution timed out",
        )

    async def cancel(
        self, agent_id: str, *, reason: str = "agent cancelled"
    ) -> AgentInfo:
        normalized_id = self._normalize_agent_id(agent_id)
        if normalized_id == self.root_agent_id:
            raise AgentStateError(
                "the root agent is not a registered child; use interrupt(root_agent_id)"
            )
        results = await self.interrupt(normalized_id, reason=reason)
        return next(info for info in results if info.agent_id == normalized_id)

    async def get(self, agent_id: str) -> AgentInfo:
        await self._ensure_persistent_run()
        async with self._condition:
            return self._record(agent_id).snapshot()

    async def list(self, *, parent_id: str | None = None) -> list[AgentInfo]:
        await self._ensure_persistent_run()
        async with self._condition:
            records = self._records.values()
            if parent_id is not None:
                normalized_parent = self._normalize_agent_id(
                    parent_id,
                    field_name="parent_id",
                )
                records = (
                    record
                    for record in records
                    if record.parent_id == normalized_parent
                )
            return [record.snapshot() for record in records]

    async def wait(
        self,
        agent_ids: list[str] | tuple[str, ...] | None = None,
        *,
        timeout: float | None = None,
    ) -> list[AgentInfo]:
        """Wait until all selected agents are terminal and return snapshots."""

        # A cleanup interrupt can close a newly-created manager locally after
        # discovering that its durable root id is already terminal.  In that
        # recovery path there is no live persistent run to initialize; waiting
        # on the in-memory tree is the only meaningful operation.
        if not (self._closed and self._store is not None and not self._persistence_ready):
            await self._ensure_persistent_run()

        async def wait_inner() -> list[AgentInfo]:
            async with self._condition:
                selected = (
                    [self._normalize_agent_id(agent_id) for agent_id in agent_ids]
                    if agent_ids is not None
                    else list(self._records)
                )
                for selected_id in selected:
                    self._record(selected_id)
                await self._condition.wait_for(
                    lambda: all(
                        self._record(selected_id).status.terminal
                        for selected_id in selected
                    )
                )
                return [
                    self._record(selected_id).snapshot() for selected_id in selected
                ]

        if timeout is None:
            return await wait_inner()
        return await asyncio.wait_for(wait_inner(), timeout=timeout)

    async def token_for(self, agent_id: str) -> AgentCancellationToken:
        await self._ensure_persistent_run()
        async with self._condition:
            return self._record(agent_id).cancellation

    @asynccontextmanager
    async def spawn(
        self,
        agent_id: str,
        *,
        parent_id: str | None = None,
        metadata: dict[str, Any] | None = None,
        wait: bool = True,
    ) -> AsyncIterator[AgentInfo]:
        """Register, reserve, and reliably finalize one child execution."""

        normalized_id = self._normalize_agent_id(agent_id)
        await self.register(normalized_id, parent_id=parent_id, metadata=metadata)
        execution_task = asyncio.current_task()
        final_status = AgentStatus.COMPLETED
        final_error: str | None = None
        normal_exit = False
        current_agent_token: Token[str | None] | None = None
        try:
            info = await self.reserve(normalized_id, wait=wait)
            current_agent_token = _CURRENT_AGENT_ID.set(normalized_id)
            yield info
            normal_exit = True
        except (asyncio.TimeoutError, TimeoutError) as exc:  # noqa: UP041
            final_status = AgentStatus.TIMED_OUT
            final_error = str(exc) or "agent execution timed out"
            raise
        except asyncio.CancelledError:
            final_status = AgentStatus.INTERRUPTED
            final_error = "agent cancelled"
            raise
        except BaseException as exc:
            final_status = AgentStatus.FAILED
            final_error = str(exc)
            raise
        finally:
            if current_agent_token is not None:
                _CURRENT_AGENT_ID.reset(current_agent_token)
            final_info = await self._finish_uninterruptibly(
                normalized_id,
                final_status,
                error=final_error,
                allowed_from=(
                    {AgentStatus.RUNNING}
                    if final_status is AgentStatus.COMPLETED
                    else None
                ),
                execution_task=execution_task,
            )
            if normal_exit and final_info.status is AgentStatus.INTERRUPTED:
                raise asyncio.CancelledError

    async def _finish_uninterruptibly(
        self,
        agent_id: str,
        status: AgentStatus,
        *,
        error: str | None,
        allowed_from: set[AgentStatus] | None,
        execution_task: asyncio.Task[Any] | None,
    ) -> AgentInfo:
        """Commit a terminal state even if the owner receives another cancel."""

        finalizer = asyncio.create_task(
            self._finish(
                agent_id,
                status,
                error=error,
                allowed_from=allowed_from,
                execution_task=execution_task,
            )
        )
        deferred_cancel: asyncio.CancelledError | None = None
        deadline = asyncio.get_running_loop().time() + _finalizer_timeout_seconds()
        while True:
            remaining = deadline - asyncio.get_running_loop().time()
            if remaining <= 0:
                finalizer.cancel()
                finalizer.add_done_callback(_consume_finalizer_result)
                if deferred_cancel is not None:
                    raise deferred_cancel from None
                raise TimeoutError(
                    f"agent terminal finalizer exceeded {_finalizer_timeout_seconds():g}s"
                )
            try:
                result = await asyncio.wait_for(
                    asyncio.shield(finalizer),
                    timeout=remaining,
                )
                break
            except asyncio.CancelledError as exc:
                deferred_cancel = exc
                if finalizer.done():
                    try:
                        result = finalizer.result()
                    except BaseException as finalizer_error:
                        # 取消契约优先:递延的取消必须投递,finalizer 的异常
                        # 作为原因链保留,否则任务以 failed 而非 cancelled
                        # 收场,外层等待方丢失取消信号。
                        raise deferred_cancel from finalizer_error
                    break
            except asyncio.TimeoutError:  # noqa: UP041 -- Python 3.10 compatibility
                finalizer.cancel()
                finalizer.add_done_callback(_consume_finalizer_result)
                if deferred_cancel is not None:
                    raise deferred_cancel from None
                raise TimeoutError(
                    f"agent terminal finalizer exceeded {_finalizer_timeout_seconds():g}s"
                ) from None
            except BaseException as finalizer_error:
                # finalizer 真异常(落库失败等):若期间收到过取消,取消优先
                # 重投,异常保留在原因链;否则照常向上传播。
                if deferred_cancel is not None:
                    raise deferred_cancel from finalizer_error
                raise
        if deferred_cancel is not None:
            raise deferred_cancel
        return result

    # ── 拆分方法绑定(descriptor 协议)────────────────────────────
    _apply_budget_decision = _budget._apply_budget_decision
    consume_budget = _budget.consume_budget
    check_budget = _budget.check_budget
    budget_snapshot = _budget.budget_snapshot
    _budget_config_metadata = _budget._budget_config_metadata
    _budget_usage_metadata = staticmethod(_budget._budget_usage_metadata)
    _restore_persisted_budget = _budget._restore_persisted_budget
    _persist_budget_snapshot = _budget._persist_budget_snapshot
    _ensure_persistent_run = _persistence._ensure_persistent_run
    _lifecycle_event_id = _persistence._lifecycle_event_id
    _persist_agent_info = _persistence._persist_agent_info
    finalize_run = _persistence.finalize_run
    _finalize_orphaned_persisted_agents = _persistence._finalize_orphaned_persisted_agents
    register = _slots.register
    reserve = _slots.reserve
    _remove_waiter = _slots._remove_waiter
    _schedule_owner_task_cleanup = _slots._schedule_owner_task_cleanup
    _consume_cleanup_result = staticmethod(_slots._consume_cleanup_result)
    _finalize_abandoned_owner = _slots._finalize_abandoned_owner
    _release_slot_locked = _slots._release_slot_locked
    _mark_interrupted_locked = _slots._mark_interrupted_locked
    _descendant_ids = _slots._descendant_ids
    interrupt = _slots.interrupt


class InMemoryAgentRuntimeManager(AgentControl):
    """Explicit implementation name for dependency injection and factories."""


_CURRENT_AGENT_ID: ContextVar[str | None] = ContextVar(
    "current_agent_runtime_id",
    default=None,
)
_CURRENT_AGENT_RUNTIME_MANAGER: ContextVar[AgentRuntimeManager | None] = ContextVar(
    "current_agent_runtime_manager",
    default=None,
)
# Private compatibility alias for code that imported the old ContextVar.
_CURRENT_AGENT_CONTROL = _CURRENT_AGENT_RUNTIME_MANAGER


def set_agent_runtime_manager(
    manager: AgentRuntimeManager,
) -> Token[AgentRuntimeManager | None]:
    if not isinstance(manager, AgentRuntimeManager):
        raise TypeError("manager must implement AgentRuntimeManager")
    return _CURRENT_AGENT_RUNTIME_MANAGER.set(manager)


def reset_agent_runtime_manager(
    token: Token[AgentRuntimeManager | None],
) -> None:
    if (
        getattr(token, "var", _CURRENT_AGENT_RUNTIME_MANAGER)
        is not _CURRENT_AGENT_RUNTIME_MANAGER
    ):
        raise ValueError(
            "agent runtime manager token belongs to a different ContextVar"
        )
    try:
        _CURRENT_AGENT_RUNTIME_MANAGER.reset(token)
    except ValueError as exc:
        # 流式中继收尾在被 GC/外来 Context 补做时,reset(token) 会抛
        # "Token was created in a different Context"。与 reset_tool_budgets /
        # reset_request_skill_state 的兜底一致:退化为 set(None),避免从
        # asyncgen finalizer 里冒出 unretrieved-task 噪声。
        if "different Context" not in str(exc):
            raise
        _CURRENT_AGENT_RUNTIME_MANAGER.set(None)


def get_agent_runtime_manager() -> AgentRuntimeManager | None:
    return _CURRENT_AGENT_RUNTIME_MANAGER.get()


def get_current_agent_id() -> str | None:
    """Return the unit currently holding a manager execution lease."""

    return _CURRENT_AGENT_ID.get()


def set_agent_control(
    control: AgentRuntimeManager,
) -> Token[AgentRuntimeManager | None]:
    """Compatibility wrapper for :func:`set_agent_runtime_manager`."""

    return set_agent_runtime_manager(control)


def reset_agent_control(token: Token[AgentRuntimeManager | None]) -> None:
    """Compatibility wrapper for :func:`reset_agent_runtime_manager`."""

    reset_agent_runtime_manager(token)


def get_agent_control() -> AgentRuntimeManager | None:
    """Compatibility wrapper for :func:`get_agent_runtime_manager`."""

    return get_agent_runtime_manager()


@contextmanager
def agent_runtime_manager_scope(
    manager: AgentRuntimeManager,
) -> Iterator[AgentRuntimeManager]:
    token = set_agent_runtime_manager(manager)
    try:
        yield manager
    finally:
        reset_agent_runtime_manager(token)
