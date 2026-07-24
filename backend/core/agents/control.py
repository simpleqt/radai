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
import math
import os
import threading
import time
import uuid
from collections import deque
from collections.abc import AsyncIterator, Callable, Coroutine, Iterator
from contextlib import AbstractAsyncContextManager, asynccontextmanager, contextmanager
from contextvars import ContextVar, Token
from dataclasses import dataclass, field, replace
from enum import Enum
from typing import TYPE_CHECKING, Any, Protocol, runtime_checkable

if TYPE_CHECKING:
    from backend.core.agents.runtime_store import AgentRuntimeStore

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


class AgentStatus(str, Enum):
    PENDING = "pending"
    RUNNING = "running"
    INTERRUPTING = "interrupting"
    COMPLETED = "completed"
    FAILED = "failed"
    TIMED_OUT = "timed_out"
    INTERRUPTED = "interrupted"

    @property
    def terminal(self) -> bool:
        return self in {
            AgentStatus.COMPLETED,
            AgentStatus.FAILED,
            AgentStatus.TIMED_OUT,
            AgentStatus.INTERRUPTED,
        }


class AgentControlError(RuntimeError):
    """Base error for request-tree lifecycle violations."""


class AgentCapacityError(AgentControlError):
    """Raised when a spawn or active-execution limit is exhausted."""


class AgentDepthError(AgentControlError):
    """Raised when a child would exceed the configured tree depth."""


class AgentNotFoundError(AgentControlError):
    """Raised when an unknown agent id is addressed."""


class AgentStateError(AgentControlError):
    """Raised when a lifecycle operation is invalid for the current state."""


@dataclass(frozen=True)
class RunBudget:
    """Immutable budget shared by every agent in one request tree.

    Shape and concurrency limits remain required for backward compatibility.
    Runtime limits are opt-in: ``None`` means unlimited.  ``deadline_at`` is an
    absolute Unix timestamp, while ``max_elapsed_seconds`` is measured from
    manager construction using a monotonic clock.
    """

    max_spawned_agents: int = 16
    max_active_agents: int = 5
    max_depth: int = 1
    max_tokens: int | None = None
    max_tool_calls: int | None = None
    max_elapsed_seconds: float | None = None
    deadline_at: float | None = None

    def __post_init__(self) -> None:
        if self.max_spawned_agents < 1:
            raise ValueError("max_spawned_agents must be at least 1")
        if self.max_active_agents < 1:
            raise ValueError("max_active_agents must be at least 1")
        if self.max_active_agents > self.max_spawned_agents:
            raise ValueError("max_active_agents cannot exceed max_spawned_agents")
        if self.max_depth < 1:
            raise ValueError("max_depth must be at least 1")
        for field_name in ("max_tokens", "max_tool_calls"):
            value = getattr(self, field_name)
            if value is not None and (
                isinstance(value, bool) or not isinstance(value, int) or value < 0
            ):
                raise ValueError(f"{field_name} must be a non-negative integer")
        if self.max_elapsed_seconds is not None and (
            isinstance(self.max_elapsed_seconds, bool)
            or not isinstance(self.max_elapsed_seconds, (int, float))
            or not math.isfinite(self.max_elapsed_seconds)
            or self.max_elapsed_seconds <= 0
        ):
            raise ValueError("max_elapsed_seconds must be a finite positive number")
        if self.deadline_at is not None and (
            isinstance(self.deadline_at, bool)
            or not isinstance(self.deadline_at, (int, float))
            or not math.isfinite(self.deadline_at)
            or self.deadline_at <= 0
        ):
            raise ValueError("deadline_at must be a finite positive Unix timestamp")


@dataclass(frozen=True)
class RunBudgetSnapshot:
    """Point-in-time view of the atomic request-tree budget ledger."""

    started_at: float
    captured_at: float
    elapsed_seconds: float
    deadline_at: float | None
    deadline_remaining_seconds: float | None
    max_elapsed_seconds: float | None
    max_tokens: int | None
    tokens_used: int
    tokens_remaining: int | None
    max_tool_calls: int | None
    tool_calls_used: int
    tool_calls_remaining: int | None
    exceeded: bool = False
    exceeded_dimension: str | None = None
    exceeded_reason: str | None = None


class AgentBudgetExceededError(AgentControlError):
    """Raised after a shared request-tree runtime budget is exceeded."""

    def __init__(self, snapshot: RunBudgetSnapshot) -> None:
        if not snapshot.exceeded or snapshot.exceeded_dimension is None:
            raise ValueError("budget exception requires an exceeded snapshot")
        self.snapshot = snapshot
        self.dimension = snapshot.exceeded_dimension
        super().__init__(snapshot.exceeded_reason or "run budget exceeded")


class _RunBudgetLedger:
    """Concurrency-safe usage ledger owned by exactly one request tree."""

    def __init__(self, budget: RunBudget) -> None:
        self._budget = budget
        self._lock = asyncio.Lock()
        self._started_at = time.time()
        self._started_monotonic = time.monotonic()
        self._tokens_used = 0
        self._tool_calls_used = 0
        self._exceeded_dimension: str | None = None
        self._exceeded_reason: str | None = None

    @staticmethod
    def _validate_increment(value: int, *, field_name: str) -> int:
        if isinstance(value, bool) or not isinstance(value, int) or value < 0:
            raise ValueError(f"{field_name} must be a non-negative integer")
        return value

    def _elapsed(self) -> float:
        return max(0.0, time.monotonic() - self._started_monotonic)

    def _effective_deadline_at(self) -> float | None:
        deadlines: list[float] = []
        if self._budget.max_elapsed_seconds is not None:
            deadlines.append(self._started_at + self._budget.max_elapsed_seconds)
        if self._budget.deadline_at is not None:
            deadlines.append(float(self._budget.deadline_at))
        return min(deadlines) if deadlines else None

    def _latch_time_limit(self, *, captured_at: float, elapsed: float) -> bool:
        if self._exceeded_dimension is not None:
            return False
        candidates: list[tuple[float, str, str]] = []
        if (
            self._budget.max_elapsed_seconds is not None
            and elapsed >= self._budget.max_elapsed_seconds
        ):
            candidates.append(
                (
                    self._started_at + self._budget.max_elapsed_seconds,
                    "elapsed",
                    "run budget exceeded: elapsed time "
                    f"{elapsed:.3f}s reached limit "
                    f"{self._budget.max_elapsed_seconds:.3f}s",
                )
            )
        if (
            self._budget.deadline_at is not None
            and captured_at >= self._budget.deadline_at
        ):
            candidates.append(
                (
                    float(self._budget.deadline_at),
                    "deadline",
                    "run budget exceeded: absolute deadline "
                    f"{self._budget.deadline_at:.6f} was reached",
                )
            )
        if not candidates:
            return False
        _, dimension, reason = min(candidates, key=lambda item: item[0])
        self._exceeded_dimension = dimension
        self._exceeded_reason = reason
        return True

    def _snapshot(self, *, captured_at: float, elapsed: float) -> RunBudgetSnapshot:
        deadline_at = self._effective_deadline_at()
        return RunBudgetSnapshot(
            started_at=self._started_at,
            captured_at=captured_at,
            elapsed_seconds=elapsed,
            deadline_at=deadline_at,
            deadline_remaining_seconds=(
                max(0.0, deadline_at - captured_at) if deadline_at is not None else None
            ),
            max_elapsed_seconds=self._budget.max_elapsed_seconds,
            max_tokens=self._budget.max_tokens,
            tokens_used=self._tokens_used,
            tokens_remaining=(
                max(0, self._budget.max_tokens - self._tokens_used)
                if self._budget.max_tokens is not None
                else None
            ),
            max_tool_calls=self._budget.max_tool_calls,
            tool_calls_used=self._tool_calls_used,
            tool_calls_remaining=(
                max(0, self._budget.max_tool_calls - self._tool_calls_used)
                if self._budget.max_tool_calls is not None
                else None
            ),
            exceeded=self._exceeded_dimension is not None,
            exceeded_dimension=self._exceeded_dimension,
            exceeded_reason=self._exceeded_reason,
        )

    async def consume(
        self,
        *,
        tokens: int = 0,
        tool_calls: int = 0,
    ) -> tuple[RunBudgetSnapshot, bool]:
        """Atomically consume both counters or neither of them."""

        tokens = self._validate_increment(tokens, field_name="tokens")
        tool_calls = self._validate_increment(tool_calls, field_name="tool_calls")
        async with self._lock:
            captured_at = time.time()
            elapsed = self._elapsed()
            newly_exceeded = self._latch_time_limit(
                captured_at=captured_at,
                elapsed=elapsed,
            )
            if self._exceeded_dimension is None:
                attempted_tokens = self._tokens_used + tokens
                attempted_tool_calls = self._tool_calls_used + tool_calls
                if (
                    self._budget.max_tokens is not None
                    and attempted_tokens > self._budget.max_tokens
                ):
                    self._exceeded_dimension = "tokens"
                    self._exceeded_reason = (
                        "run budget exceeded: token usage would reach "
                        f"{attempted_tokens}, limit is {self._budget.max_tokens}"
                    )
                    newly_exceeded = True
                elif (
                    self._budget.max_tool_calls is not None
                    and attempted_tool_calls > self._budget.max_tool_calls
                ):
                    self._exceeded_dimension = "tool_calls"
                    self._exceeded_reason = (
                        "run budget exceeded: tool-call usage would reach "
                        f"{attempted_tool_calls}, limit is "
                        f"{self._budget.max_tool_calls}"
                    )
                    newly_exceeded = True
                else:
                    self._tokens_used = attempted_tokens
                    self._tool_calls_used = attempted_tool_calls
            return self._snapshot(
                captured_at=captured_at, elapsed=elapsed
            ), newly_exceeded

    async def inspect(self) -> tuple[RunBudgetSnapshot, bool]:
        async with self._lock:
            captured_at = time.time()
            elapsed = self._elapsed()
            newly_exceeded = self._latch_time_limit(
                captured_at=captured_at,
                elapsed=elapsed,
            )
            return self._snapshot(
                captured_at=captured_at, elapsed=elapsed
            ), newly_exceeded

    def restore(
        self,
        *,
        started_at: float,
        tokens_used: int = 0,
        tool_calls_used: int = 0,
        exceeded_dimension: str | None = None,
        exceeded_reason: str | None = None,
    ) -> None:
        """Restore counters from a durable run snapshot after restart."""

        tokens_used = self._validate_increment(tokens_used, field_name="tokens_used")
        tool_calls_used = self._validate_increment(
            tool_calls_used,
            field_name="tool_calls_used",
        )
        if self._budget.max_tokens is not None and tokens_used > self._budget.max_tokens:
            raise ValueError("persisted token usage exceeds the configured budget")
        if (
            self._budget.max_tool_calls is not None
            and tool_calls_used > self._budget.max_tool_calls
        ):
            raise ValueError("persisted tool usage exceeds the configured budget")
        self._started_at = float(started_at or time.time())
        self._started_monotonic = time.monotonic() - max(
            0.0,
            time.time() - self._started_at,
        )
        self._tokens_used = tokens_used
        self._tool_calls_used = tool_calls_used
        self._exceeded_dimension = str(exceeded_dimension or "").strip() or None
        self._exceeded_reason = str(exceeded_reason or "").strip() or None
        if self._exceeded_dimension not in {
            None,
            "tokens",
            "tool_calls",
            "elapsed",
            "deadline",
        }:
            raise ValueError("unsupported persisted budget exceeded dimension")
        if self._exceeded_dimension is None:
            self._exceeded_reason = None


class AgentControlLimits(RunBudget):
    """Backward-compatible name for the original tree budget type."""


class AgentStateMachine:
    """Single source of truth for child-agent lifecycle transitions."""

    _TRANSITIONS: dict[AgentStatus, frozenset[AgentStatus]] = {
        AgentStatus.PENDING: frozenset(
            {
                AgentStatus.RUNNING,
                AgentStatus.INTERRUPTING,
                AgentStatus.INTERRUPTED,
                AgentStatus.FAILED,
                AgentStatus.TIMED_OUT,
            }
        ),
        AgentStatus.RUNNING: frozenset(
            {
                AgentStatus.INTERRUPTING,
                AgentStatus.COMPLETED,
                AgentStatus.FAILED,
                AgentStatus.TIMED_OUT,
                AgentStatus.INTERRUPTED,
            }
        ),
        AgentStatus.INTERRUPTING: frozenset({AgentStatus.INTERRUPTED}),
        AgentStatus.COMPLETED: frozenset(),
        AgentStatus.FAILED: frozenset(),
        AgentStatus.TIMED_OUT: frozenset(),
        AgentStatus.INTERRUPTED: frozenset(),
    }

    @classmethod
    def can_transition(cls, current: AgentStatus, target: AgentStatus) -> bool:
        return target in cls._TRANSITIONS[current]

    @classmethod
    def validate(
        cls,
        current: AgentStatus,
        target: AgentStatus,
        *,
        agent_id: str,
    ) -> None:
        if not cls.can_transition(current, target):
            raise AgentStateError(
                f"agent {agent_id} cannot transition from "
                f"{current.value} to {target.value}"
            )


class AgentCancellationToken:
    """Read-only cooperative cancellation signal with optional tree parent."""

    def __init__(self, parent: AgentCancellationToken | None = None) -> None:
        self._event = asyncio.Event()
        self._parent = parent
        self._reason: str | None = None

    @property
    def cancelled(self) -> bool:
        return self._event.is_set() or bool(
            self._parent is not None and self._parent.cancelled
        )

    @property
    def reason(self) -> str | None:
        if self._event.is_set():
            return self._reason
        if self._parent is not None:
            return self._parent.reason
        return None

    def _cancel(self, reason: str | None = None) -> None:
        """Set the signal; lifecycle cancellation must go through AgentControl."""

        if not self._event.is_set():
            self._reason = str(reason or "agent cancelled")
        self._event.set()

    async def wait(self) -> None:
        if self.cancelled:
            return
        if self._parent is None:
            await self._event.wait()
            return
        local_wait = asyncio.create_task(self._event.wait())
        parent_wait = asyncio.create_task(self._parent.wait())
        try:
            await asyncio.wait(
                {local_wait, parent_wait},
                return_when=asyncio.FIRST_COMPLETED,
            )
        finally:
            for pending in (local_wait, parent_wait):
                if not pending.done():
                    pending.cancel()

    def raise_if_cancelled(self) -> None:
        if self.cancelled:
            raise asyncio.CancelledError


@dataclass(frozen=True)
class AgentInfo:
    agent_id: str
    parent_id: str
    depth: int
    status: AgentStatus
    metadata: dict[str, Any] = field(default_factory=dict)
    created_at: float = 0.0
    started_at: float | None = None
    finished_at: float | None = None
    error: str | None = None


@runtime_checkable
class AgentRuntimeManager(Protocol):
    """Transport-independent control plane used by all agent execution paths.

    Callers deliberately depend only on this interface so persistent or
    distributed implementations can replace the live in-memory controller
    without changing tools or workflow output contracts.
    """

    root_agent_id: str
    budget: RunBudget
    cancellation: AgentCancellationToken

    @property
    def active_count(self) -> int: ...

    @property
    def spawned_count(self) -> int: ...

    @property
    def closed(self) -> bool: ...

    def spawn(
        self,
        agent_id: str,
        *,
        parent_id: str | None = None,
        metadata: dict[str, Any] | None = None,
        wait: bool = True,
    ) -> AbstractAsyncContextManager[AgentInfo]: ...

    async def wait(
        self,
        agent_ids: list[str] | tuple[str, ...] | None = None,
        *,
        timeout: float | None = None,
    ) -> list[AgentInfo]: ...

    async def interrupt(
        self,
        agent_id: str,
        *,
        reason: str = "agent interrupted",
    ) -> list[AgentInfo]: ...

    async def list(self, *, parent_id: str | None = None) -> list[AgentInfo]: ...

    async def get(self, agent_id: str) -> AgentInfo: ...

    async def token_for(self, agent_id: str) -> AgentCancellationToken: ...

    async def consume_budget(
        self,
        *,
        tokens: int = 0,
        tool_calls: int = 0,
    ) -> RunBudgetSnapshot: ...

    async def check_budget(self) -> RunBudgetSnapshot: ...

    async def budget_snapshot(self) -> RunBudgetSnapshot: ...


@dataclass
class _AgentRecord:
    agent_id: str
    parent_id: str
    depth: int
    status: AgentStatus
    metadata: dict[str, Any]
    created_at: float
    cancellation: AgentCancellationToken = field(default_factory=AgentCancellationToken)
    started_at: float | None = None
    finished_at: float | None = None
    error: str | None = None
    task: asyncio.Task[Any] | None = None
    slot_reserved: bool = False
    restored: bool = False

    def snapshot(self) -> AgentInfo:
        return AgentInfo(
            agent_id=self.agent_id,
            parent_id=self.parent_id,
            depth=self.depth,
            status=self.status,
            metadata=dict(self.metadata),
            created_at=self.created_at,
            started_at=self.started_at,
            finished_at=self.finished_at,
            error=self.error,
        )


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

    async def _apply_budget_decision(
        self,
        snapshot: RunBudgetSnapshot,
        *,
        newly_exceeded: bool,
    ) -> None:
        if not newly_exceeded:
            return
        reason = snapshot.exceeded_reason or "run budget exceeded"
        # Latch the cooperative signal before the first await so concurrent
        # children observe cancellation as soon as the ledger rejects usage.
        self.cancellation._cancel(reason)
        try:
            await self.interrupt(self.root_agent_id, reason=reason)
        except Exception:
            # A persistence outage must not turn an explicit budget violation
            # into an unrelated store error or leave the cancellation unset.
            logger.exception("failed to persist request-tree budget cancellation")

    async def consume_budget(
        self,
        *,
        tokens: int = 0,
        tool_calls: int = 0,
    ) -> RunBudgetSnapshot:
        """Atomically charge usage to the shared tree ledger.

        The two counters form one transaction: if either requested increment
        would exceed its limit, neither counter is changed.  The first
        violation closes and cooperatively cancels the whole request tree.
        """

        owner_loop = self._foreign_owner_loop()
        if owner_loop is not None:
            return await self._await_on_owner_loop(
                owner_loop,
                lambda: self.consume_budget(tokens=tokens, tool_calls=tool_calls),
                operation="consume budget",
            )

        await self._ensure_persistent_run()
        snapshot, newly_exceeded = await self._budget_ledger.consume(
            tokens=tokens,
            tool_calls=tool_calls,
        )
        if tokens or tool_calls or newly_exceeded:
            await self._persist_budget_snapshot(snapshot)
        await self._apply_budget_decision(
            snapshot,
            newly_exceeded=newly_exceeded,
        )
        if snapshot.exceeded:
            raise AgentBudgetExceededError(snapshot)
        return snapshot

    async def check_budget(self) -> RunBudgetSnapshot:
        """Check elapsed/absolute deadlines and raise on a latched violation."""

        owner_loop = self._foreign_owner_loop()
        if owner_loop is not None:
            return await self._await_on_owner_loop(
                owner_loop,
                self.check_budget,
                operation="check budget",
            )

        await self._ensure_persistent_run()
        snapshot, newly_exceeded = await self._budget_ledger.inspect()
        if newly_exceeded:
            await self._persist_budget_snapshot(snapshot)
        await self._apply_budget_decision(
            snapshot,
            newly_exceeded=newly_exceeded,
        )
        if snapshot.exceeded:
            raise AgentBudgetExceededError(snapshot)
        return snapshot

    async def budget_snapshot(self) -> RunBudgetSnapshot:
        """Return current usage, remaining allowance, and deadline state.

        Introspection does not raise, but discovering an expired deadline still
        propagates tree cancellation just like :meth:`check_budget`.
        """

        owner_loop = self._foreign_owner_loop()
        if owner_loop is not None:
            return await self._await_on_owner_loop(
                owner_loop,
                self.budget_snapshot,
                operation="capture budget snapshot",
            )

        await self._ensure_persistent_run()
        snapshot, newly_exceeded = await self._budget_ledger.inspect()
        if newly_exceeded:
            await self._persist_budget_snapshot(snapshot)
        await self._apply_budget_decision(
            snapshot,
            newly_exceeded=newly_exceeded,
        )
        return snapshot

    def _budget_config_metadata(self) -> dict[str, int | float | None]:
        return {
            "max_spawned_agents": self.budget.max_spawned_agents,
            "max_active_agents": self.budget.max_active_agents,
            "max_depth": self.budget.max_depth,
            "max_tokens": self.budget.max_tokens,
            "max_tool_calls": self.budget.max_tool_calls,
            "max_elapsed_seconds": self.budget.max_elapsed_seconds,
            "deadline_at": self.budget.deadline_at,
        }

    @staticmethod
    def _budget_usage_metadata(snapshot: RunBudgetSnapshot) -> dict[str, Any]:
        return {
            "started_at": snapshot.started_at,
            "captured_at": snapshot.captured_at,
            "tokens_used": snapshot.tokens_used,
            "tool_calls_used": snapshot.tool_calls_used,
            "exceeded_dimension": snapshot.exceeded_dimension,
            "exceeded_reason": snapshot.exceeded_reason,
        }

    def _restore_persisted_budget(
        self,
        metadata: dict[str, Any],
        started_at: float | None,
    ) -> None:
        """Adopt durable budget configuration and usage after a restart."""

        raw_budget = metadata.get("budget")
        if isinstance(raw_budget, dict):
            fields = {
                field_name: raw_budget.get(field_name, getattr(self.budget, field_name))
                for field_name in (
                    "max_spawned_agents",
                    "max_active_agents",
                    "max_depth",
                    "max_tokens",
                    "max_tool_calls",
                    "max_elapsed_seconds",
                    "deadline_at",
                )
            }
            try:
                persisted_budget = RunBudget(**fields)
            except (TypeError, ValueError) as exc:
                raise AgentStateError(
                    f"persisted agent tree {self.root_agent_id} has invalid budget"
                ) from exc
            if self.budget == RunBudget() or self.budget == persisted_budget:
                if self.budget != persisted_budget:
                    self.budget = persisted_budget
                    self.limits = persisted_budget
                    self._budget_ledger = _RunBudgetLedger(persisted_budget)
            else:
                raise AgentStateError(
                    f"persisted agent tree {self.root_agent_id} budget differs from "
                    "the requested recovery budget"
                )

        usage = metadata.get("budget_usage")
        if not isinstance(usage, dict):
            return
        try:
            self._budget_ledger.restore(
                started_at=float(
                    usage.get("started_at") or started_at or self._created_at
                ),
                tokens_used=int(usage.get("tokens_used") or 0),
                tool_calls_used=int(usage.get("tool_calls_used") or 0),
                exceeded_dimension=usage.get("exceeded_dimension"),
                exceeded_reason=usage.get("exceeded_reason"),
            )
        except (TypeError, ValueError) as exc:
            raise AgentStateError(
                f"persisted agent tree {self.root_agent_id} has invalid budget usage"
            ) from exc

    async def _persist_budget_snapshot(self, snapshot: RunBudgetSnapshot) -> None:
        """Best-effort CAS update of durable usage for worker recovery."""

        if self._store is None:
            return
        from backend.core.agents.runtime_store import AgentRunSnapshot

        try:
            await self._ensure_persistent_run()
            async with self._persistence_lock:
                existing = await self._store.load_run(self.root_agent_id)
                if existing is None or existing.status in {
                    "completed",
                    "failed",
                    "timed_out",
                    "interrupted",
                    "cancelled",
                }:
                    return
                persisted = await self._store.upsert_run(
                    AgentRunSnapshot(
                        root_id=self.root_agent_id,
                        status=existing.status,
                        metadata={
                            **existing.metadata,
                            **self._run_metadata,
                            "budget": self._budget_config_metadata(),
                            "budget_usage": self._budget_usage_metadata(snapshot),
                        },
                        created_at=existing.created_at,
                        updated_at=time.time(),
                        started_at=existing.started_at,
                        finished_at=existing.finished_at,
                        error=existing.error,
                        version=existing.version,
                    ),
                    expected_version=existing.version,
                )
                self._run_version = persisted.version
                self._run_metadata = dict(persisted.metadata)
        except Exception:
            # The in-memory ledger remains authoritative for the live tree;
            # a transient store failure must not break a valid tool/model call.
            logger.exception("failed to persist request-tree budget usage")

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
            if existing is not None:
                self._restore_persisted_budget(existing.metadata, existing.started_at)
            now = time.time()
            run_metadata = {
                **(existing.metadata if existing is not None else {}),
                **self._run_metadata,
                "budget": self._budget_config_metadata(),
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
            if same_logical_state:
                persisted = existing
            else:
                persisted = await self._store.upsert_agent(
                    incoming,
                    expected_version=version,
                )
            self._agent_versions[info.agent_id] = persisted.version
            self._persisted_agents[info.agent_id] = persisted
            if event_type == "agent.registered":
                event_timestamp = (
                    persisted.updated_at
                    if existing is not None
                    else persisted.created_at
                )
            elif persisted.finished_at is not None:
                event_timestamp = persisted.finished_at
            elif persisted.status == AgentStatus.RUNNING.value:
                event_timestamp = persisted.started_at or persisted.updated_at
            else:
                event_timestamp = persisted.updated_at
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
            return
        cleanup_task = loop.create_task(
            self._finalize_abandoned_owner(agent_id, owner_task)
        )
        cleanup_task.add_done_callback(self._consume_cleanup_result)

    @staticmethod
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
            if (
                record is None
                or record.task is not owner_task
                or record.status.terminal
            ):
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
        await self._persist_agent_info(
            snapshot,
            event_type=f"agent.{snapshot.status.value}",
        )
        for descendant in descendant_snapshots:
            await self._persist_agent_info(
                descendant,
                event_type=f"agent.{descendant.status.value}",
            )
        return snapshot

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
        for snapshot in snapshots:
            await self._persist_agent_info(
                snapshot,
                event_type=f"agent.{snapshot.status.value}",
            )
        return snapshots

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
                    result = finalizer.result()
                    break
            except asyncio.TimeoutError:  # noqa: UP041 -- Python 3.10 compatibility
                finalizer.cancel()
                finalizer.add_done_callback(_consume_finalizer_result)
                if deferred_cancel is not None:
                    raise deferred_cancel from None
                raise TimeoutError(
                    f"agent terminal finalizer exceeded {_finalizer_timeout_seconds():g}s"
                ) from None
        if deferred_cancel is not None:
            raise deferred_cancel
        return result


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
    _CURRENT_AGENT_RUNTIME_MANAGER.reset(token)


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


@contextmanager
def agent_control_scope(
    control: AgentRuntimeManager,
) -> Iterator[AgentRuntimeManager]:
    """Compatibility wrapper for :func:`agent_runtime_manager_scope`."""

    with agent_runtime_manager_scope(control) as manager:
        yield manager
