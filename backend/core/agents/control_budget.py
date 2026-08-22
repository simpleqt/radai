"""AgentControl 预算域:预算判定/消费/巡检/快照与配置/用量元数据、持久预算恢复与落盘。(拆自 control.py,方法经类属性绑定回 AgentControl)"""

from __future__ import annotations

from backend.config.loggers import get_logger

from typing import Any
import time
from backend.core.agents.control_models import (
    AgentBudgetExceededError,
    AgentStateError,
    RunBudget,
    RunBudgetSnapshot,
    _RunBudgetLedger,
)

logger = get_logger(__name__)


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
    """Best-effort CAS update of durable usage for worker recovery.

    A stale-version conflict (another writer bumped the run row between our
    read and write) is retried once with a fresh read; other failures are
    swallowed so a store outage cannot break a valid tool/model call, at the
    cost that a crash right after would recover with under-counted usage.
    """

    if self._store is None:
        return
    from backend.core.agents.runtime_store import (
        AgentRunSnapshot,
        AgentRuntimeVersionConflict,
    )

    try:
        await self._ensure_persistent_run()
        async with self._persistence_lock:
            for attempt in range(2):
                existing = await self._store.load_run(self.root_agent_id)
                if existing is None or existing.status in {
                    "completed",
                    "failed",
                    "timed_out",
                    "interrupted",
                    "cancelled",
                }:
                    return
                try:
                    persisted = await self._store.upsert_run(
                        AgentRunSnapshot(
                            root_id=self.root_agent_id,
                            status=existing.status,
                            metadata={
                                **existing.metadata,
                                **self._run_metadata,
                                "budget": self._budget_config_metadata(),
                                "budget_usage": self._budget_usage_metadata(
                                    snapshot
                                ),
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
                except AgentRuntimeVersionConflict:
                    if attempt == 0:
                        continue
                    raise
                self._run_version = persisted.version
                self._run_metadata = dict(persisted.metadata)
                return
    except Exception:
        # The in-memory ledger remains authoritative for the live tree;
        # a transient store failure must not break a valid tool/model call.
        # Recovery after a crash in this window restores usage from the last
        # successful snapshot, i.e. a lower bound of true consumption.
        logger.exception("failed to persist request-tree budget usage")
