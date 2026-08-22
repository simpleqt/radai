"""Agent 控制模型层:状态/异常族、运行预算账本、状态机、取消令牌、协议与记录(拆自 control.py)。"""

from __future__ import annotations

from backend.config.loggers import get_logger

from contextlib import AbstractAsyncContextManager
from dataclasses import dataclass
from dataclasses import field
from enum import Enum
from typing import Any
from typing import Protocol
from typing import runtime_checkable
import asyncio
import math
import time

logger = get_logger(__name__)


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
