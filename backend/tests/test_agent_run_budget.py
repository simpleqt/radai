from __future__ import annotations

import asyncio
import time

import pytest

from backend.core.agents.control import (
    AgentBudgetExceededError,
    AgentControl,
    AgentStatus,
    RunBudget,
)


@pytest.mark.parametrize(
    ("kwargs", "message"),
    [
        ({"max_tokens": -1}, "max_tokens"),
        ({"max_tool_calls": 1.5}, "max_tool_calls"),
        ({"max_elapsed_seconds": 0}, "max_elapsed_seconds"),
        ({"deadline_at": float("inf")}, "deadline_at"),
    ],
)
def test_run_budget_rejects_invalid_runtime_limits(kwargs, message):
    with pytest.raises(ValueError, match=message):
        RunBudget(**kwargs)


async def test_concurrent_usage_is_atomic_across_the_whole_tree():
    manager = AgentControl(
        "root",
        budget=RunBudget(
            max_spawned_agents=2,
            max_active_agents=2,
            max_tokens=100,
            max_tool_calls=50,
        ),
    )

    snapshots = await asyncio.gather(
        *(manager.consume_budget(tokens=2, tool_calls=1) for _ in range(50))
    )

    assert max(snapshot.tokens_used for snapshot in snapshots) == 100
    snapshot = await manager.budget_snapshot()
    assert snapshot.tokens_used == 100
    assert snapshot.tokens_remaining == 0
    assert snapshot.tool_calls_used == 50
    assert snapshot.tool_calls_remaining == 0
    assert snapshot.exceeded is False


async def test_rejected_multi_counter_charge_does_not_partially_consume():
    manager = AgentControl(
        "root",
        budget=RunBudget(max_tokens=10, max_tool_calls=1),
    )
    await manager.consume_budget(tokens=2)

    with pytest.raises(AgentBudgetExceededError) as raised:
        await manager.consume_budget(tokens=3, tool_calls=2)

    assert raised.value.dimension == "tool_calls"
    assert "tool-call usage" in str(raised.value)
    snapshot = await manager.budget_snapshot()
    assert snapshot.tokens_used == 2
    assert snapshot.tool_calls_used == 0
    assert snapshot.exceeded is True
    assert manager.closed is True
    assert manager.cancellation.cancelled is True
    assert manager.cancellation.reason == snapshot.exceeded_reason


async def test_children_charge_one_shared_tree_ledger():
    manager = AgentControl(
        "root",
        budget=RunBudget(
            max_spawned_agents=2,
            max_active_agents=2,
            max_tokens=10,
            max_tool_calls=2,
        ),
    )

    async def run(agent_id: str):
        async with manager.spawn(agent_id):
            await manager.consume_budget(tokens=5, tool_calls=1)

    await asyncio.gather(run("first"), run("second"))

    snapshot = await manager.budget_snapshot()
    assert snapshot.tokens_used == 10
    assert snapshot.tool_calls_used == 2
    assert {info.status for info in await manager.list()} == {AgentStatus.COMPLETED}


async def test_token_overrun_raises_clear_error_and_cancels_running_tree():
    manager = AgentControl(
        "root",
        budget=RunBudget(
            max_spawned_agents=2,
            max_active_agents=2,
            max_tokens=1,
        ),
    )
    sibling_started = asyncio.Event()

    async def sibling():
        async with manager.spawn("sibling"):
            sibling_started.set()
            await asyncio.Event().wait()

    sibling_task = asyncio.create_task(sibling())
    await sibling_started.wait()

    with pytest.raises(AgentBudgetExceededError, match="token usage") as raised:
        await manager.consume_budget(tokens=2)

    with pytest.raises(asyncio.CancelledError):
        await sibling_task
    assert raised.value.snapshot.tokens_used == 0
    assert (await manager.get("sibling")).status is AgentStatus.INTERRUPTED
    assert manager.closed is True


async def test_absolute_deadline_check_latches_and_cancels_tree():
    manager = AgentControl(
        "root",
        budget=RunBudget(deadline_at=time.time() - 1),
    )

    with pytest.raises(AgentBudgetExceededError) as raised:
        await manager.check_budget()

    assert raised.value.dimension == "deadline"
    assert raised.value.snapshot.deadline_remaining_seconds == 0
    assert manager.cancellation.cancelled is True
    assert manager.closed is True


async def test_elapsed_limit_is_visible_in_snapshot_and_subsequent_check():
    manager = AgentControl(
        "root",
        budget=RunBudget(max_elapsed_seconds=0.005),
    )
    await asyncio.sleep(0.02)

    snapshot = await manager.budget_snapshot()

    assert snapshot.exceeded is True
    assert snapshot.exceeded_dimension == "elapsed"
    assert snapshot.elapsed_seconds >= 0.005
    assert manager.cancellation.cancelled is True
    with pytest.raises(AgentBudgetExceededError) as raised:
        await manager.check_budget()
    assert raised.value.dimension == "elapsed"


async def test_unlimited_runtime_budget_preserves_legacy_behavior():
    manager = AgentControl("root")

    snapshot = await manager.consume_budget(tokens=123, tool_calls=7)

    assert snapshot.max_tokens is None
    assert snapshot.tokens_used == 123
    assert snapshot.tokens_remaining is None
    assert snapshot.max_tool_calls is None
    assert snapshot.tool_calls_used == 7
    assert snapshot.exceeded is False
    assert manager.closed is False
