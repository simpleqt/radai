from __future__ import annotations

import asyncio

import pytest

from backend.core.agents.control import (
    AgentCapacityError,
    AgentControl,
    AgentControlLimits,
    AgentDepthError,
    AgentStateError,
    AgentStatus,
    get_agent_control,
    reset_agent_control,
    set_agent_control,
)


def test_limits_reject_more_active_slots_than_total_spawns():
    with pytest.raises(ValueError, match="cannot exceed"):
        AgentControlLimits(max_spawned_agents=1, max_active_agents=2)


async def test_spawn_context_records_success_and_releases_slot():
    control = AgentControl(
        "root",
        limits=AgentControlLimits(max_spawned_agents=3, max_active_agents=1),
    )

    async with control.spawn("child", metadata={"role": "researcher"}) as running:
        assert running.status is AgentStatus.RUNNING
        assert running.depth == 1
        assert control.active_count == 1

    completed = await control.get("child")
    assert completed.status is AgentStatus.COMPLETED
    assert completed.metadata == {"role": "researcher"}
    assert completed.started_at is not None
    assert completed.finished_at is not None
    assert control.active_count == 0


async def test_active_limit_queues_children_in_registration_order():
    control = AgentControl(
        "root",
        limits=AgentControlLimits(max_spawned_agents=3, max_active_agents=1),
    )
    first_started = asyncio.Event()
    release_first = asyncio.Event()
    execution_order: list[str] = []

    async def first():
        async with control.spawn("first"):
            execution_order.append("first")
            first_started.set()
            await release_first.wait()

    async def second():
        await first_started.wait()
        async with control.spawn("second"):
            execution_order.append("second")

    first_task = asyncio.create_task(first())
    second_task = asyncio.create_task(second())
    await first_started.wait()
    await asyncio.sleep(0)

    assert (await control.get("second")).status is AgentStatus.PENDING
    release_first.set()
    await asyncio.gather(first_task, second_task)

    assert execution_order == ["first", "second"]
    assert control.active_count == 0


async def test_root_interrupt_cancels_running_and_capacity_queued_tasks():
    control = AgentControl(
        "root",
        limits=AgentControlLimits(max_spawned_agents=3, max_active_agents=1),
    )
    first_started = asyncio.Event()

    async def run(agent_id: str):
        async with control.spawn(agent_id):
            if agent_id == "first":
                first_started.set()
            await asyncio.Event().wait()

    first = asyncio.create_task(run("first"))
    await first_started.wait()
    second = asyncio.create_task(run("second"))
    await asyncio.sleep(0)
    assert (await control.get("second")).status is AgentStatus.PENDING

    await control.interrupt("root", reason="request cancelled")

    results = await asyncio.gather(first, second, return_exceptions=True)
    assert all(isinstance(result, asyncio.CancelledError) for result in results)
    assert (await control.get("first")).status is AgentStatus.INTERRUPTED
    assert (await control.get("second")).status is AgentStatus.INTERRUPTED
    assert control.active_count == 0


async def test_root_interrupt_latches_tree_closed():
    control = AgentControl("root")

    await control.interrupt("root", reason="request closed")

    assert control.closed is True
    with pytest.raises(AgentStateError, match="tree is closed"):
        await control.register("late-child")


async def test_interrupted_task_keeps_slot_until_execution_really_exits():
    control = AgentControl(
        "root",
        limits=AgentControlLimits(max_spawned_agents=3, max_active_agents=1),
    )
    first_started = asyncio.Event()
    cancellation_swallowed = asyncio.Event()
    allow_first_exit = asyncio.Event()
    second_started = asyncio.Event()

    async def first_runner():
        async with control.spawn("first"):
            first_started.set()
            try:
                await asyncio.Event().wait()
            except asyncio.CancelledError:
                cancellation_swallowed.set()
                await allow_first_exit.wait()

    async def second_runner():
        async with control.spawn("second"):
            second_started.set()

    first = asyncio.create_task(first_runner())
    await first_started.wait()
    second = asyncio.create_task(second_runner())
    await asyncio.sleep(0)

    interrupted = await control.interrupt("first", reason="stop first")
    await cancellation_swallowed.wait()
    repeated = await control.interrupt("first", reason="stop first again")

    assert interrupted[0].status is AgentStatus.INTERRUPTING
    assert repeated[0].status is AgentStatus.INTERRUPTING
    assert control.active_count == 1
    assert second_started.is_set() is False

    allow_first_exit.set()
    with pytest.raises(asyncio.CancelledError):
        await first
    await second

    assert (await control.get("first")).status is AgentStatus.INTERRUPTED
    assert (await control.get("second")).status is AgentStatus.COMPLETED
    assert control.active_count == 0


async def test_duplicate_pending_reserve_is_rejected_instead_of_hanging():
    control = AgentControl(
        "root",
        limits=AgentControlLimits(max_spawned_agents=3, max_active_agents=1),
    )
    blocker_started = asyncio.Event()
    release_blocker = asyncio.Event()

    async def block_capacity():
        async with control.spawn("blocker"):
            blocker_started.set()
            await release_blocker.wait()

    blocker = asyncio.create_task(block_capacity())
    await blocker_started.wait()
    await control.register("queued")
    first_reservation = asyncio.create_task(control.reserve("queued"))
    await asyncio.sleep(0)

    with pytest.raises(AgentStateError, match="already waiting"):
        await control.reserve("queued")

    await control.interrupt("queued")
    with pytest.raises(asyncio.CancelledError):
        await first_reservation
    assert (await control.get("queued")).status is AgentStatus.INTERRUPTED
    assert [info.agent_id for info in await control.wait(["queued"], timeout=1)] == [
        "queued"
    ]
    release_blocker.set()
    await blocker


async def test_interrupted_low_level_running_reservation_releases_on_owner_exit():
    control = AgentControl("root")
    await control.register("child")
    started = asyncio.Event()

    async def run_without_spawn():
        await control.reserve("child")
        started.set()
        await asyncio.Event().wait()

    owner = asyncio.create_task(run_without_spawn())
    await started.wait()
    await control.interrupt(" child ", reason="request stopped")

    with pytest.raises(asyncio.CancelledError):
        await owner
    terminal = await control.wait([" child "], timeout=1)

    assert terminal[0].status is AgentStatus.INTERRUPTED
    assert control.active_count == 0


async def test_terminal_parent_cannot_register_new_descendant():
    control = AgentControl(
        "root",
        limits=AgentControlLimits(
            max_spawned_agents=3,
            max_active_agents=2,
            max_depth=2,
        ),
    )
    async with control.spawn("parent"):
        pass

    with pytest.raises(AgentStateError, match="parent agent parent is completed"):
        await control.register("late-grandchild", parent_id="parent")


async def test_spawn_uses_normalized_agent_id_for_the_whole_lifecycle():
    control = AgentControl("root")

    async with control.spawn(" child "):
        pass

    assert (await control.get(" child ")).status is AgentStatus.COMPLETED
    assert [info.agent_id for info in await control.list(parent_id=" root ")] == [
        "child"
    ]
    assert control.spawned_count == 1


async def test_cancellation_token_is_read_only_to_consumers():
    control = AgentControl("root")
    await control.register("child")

    token = await control.token_for("child")

    assert token.cancelled is False
    assert not hasattr(token, "cancel")


async def test_spawn_limit_counts_the_whole_request_tree():
    control = AgentControl(
        "root",
        limits=AgentControlLimits(max_spawned_agents=1, max_active_agents=1),
    )
    async with control.spawn("first"):
        pass

    with pytest.raises(AgentCapacityError, match="spawn limit"):
        await control.register("second")


async def test_depth_limit_rejects_recursive_delegation():
    control = AgentControl(
        "root",
        limits=AgentControlLimits(
            max_spawned_agents=3,
            max_active_agents=2,
            max_depth=1,
        ),
    )
    await control.register("child")

    with pytest.raises(AgentDepthError, match="depth 2"):
        await control.register("grandchild", parent_id="child")


async def test_failures_and_timeouts_are_terminal_and_release_slots():
    control = AgentControl(
        "root",
        limits=AgentControlLimits(max_spawned_agents=3, max_active_agents=1),
    )

    with pytest.raises(RuntimeError, match="boom"):
        async with control.spawn("failed"):
            raise RuntimeError("boom")
    assert (await control.get("failed")).status is AgentStatus.FAILED
    assert control.active_count == 0

    with pytest.raises((asyncio.TimeoutError, TimeoutError)):
        async with control.spawn("timed-out"):
            await asyncio.wait_for(asyncio.Event().wait(), timeout=0.001)
    timed_out = await control.get("timed-out")
    assert timed_out.status is AgentStatus.TIMED_OUT
    assert (await control.token_for("timed-out")).cancelled is True
    assert control.active_count == 0


async def test_interrupt_cancels_running_task_and_descendants():
    control = AgentControl(
        "root",
        limits=AgentControlLimits(
            max_spawned_agents=3,
            max_active_agents=2,
            max_depth=2,
        ),
    )
    child_started = asyncio.Event()
    descendant_registered = asyncio.Event()

    async def run_child():
        async with control.spawn("child"):
            child_started.set()
            await control.register("grandchild", parent_id="child")
            descendant_registered.set()
            await asyncio.Event().wait()

    task = asyncio.create_task(run_child())
    await child_started.wait()
    await descendant_registered.wait()

    interrupted = await control.interrupt("child", reason="parent stopped")
    with pytest.raises(asyncio.CancelledError):
        await task

    assert {info.agent_id for info in interrupted} == {"child", "grandchild"}
    assert (await control.get("child")).status is AgentStatus.INTERRUPTED
    assert (await control.get("grandchild")).status is AgentStatus.INTERRUPTED
    assert control.active_count == 0


async def test_cancel_rejects_root_but_root_interrupts_the_tree():
    control = AgentControl("root")
    await control.register("child")

    with pytest.raises(AgentStateError, match="root agent"):
        await control.cancel("root")

    interrupted = await control.interrupt("root", reason="request closed")

    assert [info.agent_id for info in interrupted] == ["child"]
    assert interrupted[0].status is AgentStatus.INTERRUPTED


async def test_wait_returns_when_selected_agents_are_terminal():
    control = AgentControl("root")
    await control.register("one")
    await control.register("two")
    await control.reserve("one")
    await control.complete("one")

    waiter = asyncio.create_task(control.wait(["one", "two"], timeout=1))
    await asyncio.sleep(0)
    assert waiter.done() is False
    await control.fail("two", "failed")

    statuses = {info.agent_id: info.status for info in await waiter}
    assert statuses == {
        "one": AgentStatus.COMPLETED,
        "two": AgentStatus.FAILED,
    }


def test_request_context_holds_one_shared_control():
    control = AgentControl("root")
    token = set_agent_control(control)
    try:
        assert get_agent_control() is control
    finally:
        reset_agent_control(token)
    assert get_agent_control() is None
