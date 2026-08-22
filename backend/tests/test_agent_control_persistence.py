from __future__ import annotations

import asyncio
from pathlib import Path

import pytest

from backend.core.agents.control import (
    AgentBudgetExceededError,
    AgentControl,
    AgentStatus,
    RunBudget,
)
from backend.core.agents.runtime_store import (
    AgentRuntimeStore,
    InMemoryAgentRuntimeStore,
    SQLiteAgentRuntimeStore,
)


def _store(kind: str, tmp_path: Path) -> AgentRuntimeStore:
    if kind == "memory":
        return InMemoryAgentRuntimeStore()
    return SQLiteAgentRuntimeStore(tmp_path / "agent-control.sqlite3")


def _manager(store: AgentRuntimeStore) -> AgentControl:
    return AgentControl(
        "root-1",
        budget=RunBudget(
            max_spawned_agents=8,
            max_active_agents=4,
            max_depth=2,
        ),
        store=store,
        run_metadata={"request_id": "req-1"},
    )


@pytest.mark.parametrize("kind", ["memory", "sqlite"])
async def test_agent_lifecycle_and_run_outcome_are_persisted(kind, tmp_path):
    store = _store(kind, tmp_path)
    manager = _manager(store)

    registered = await manager.register(
        "child-1",
        metadata={"role": "researcher"},
    )
    persisted_registered = (await store.load_tree_agents("root-1"))[0]
    running = await manager.reserve("child-1")
    persisted_running = (await store.load_tree_agents("root-1"))[0]
    completed = await manager.complete("child-1")
    persisted_completed = (await store.load_tree_agents("root-1"))[0]
    await manager.finalize_run(AgentStatus.COMPLETED)

    persisted_run = await store.load_run("root-1")
    persisted_agents = await store.load_tree_agents("root-1")
    events = await store.load_lifecycle_events("root-1")

    assert registered.status is AgentStatus.PENDING
    assert running.status is AgentStatus.RUNNING
    assert completed.status is AgentStatus.COMPLETED
    assert (persisted_registered.status, persisted_registered.version) == (
        "pending",
        1,
    )
    assert (persisted_running.status, persisted_running.version) == (
        "running",
        2,
    )
    assert (persisted_completed.status, persisted_completed.version) == (
        "completed",
        3,
    )
    assert persisted_run is not None
    assert persisted_run.status == "completed"
    assert persisted_run.metadata["request_id"] == "req-1"
    assert persisted_run.version == 2
    assert len(persisted_agents) == 1
    assert persisted_agents[0].agent_id == "child-1"
    assert persisted_agents[0].status == "completed"
    assert persisted_agents[0].metadata == {"role": "researcher"}
    assert persisted_agents[0].version == 3
    assert [event.sequence for event in events] == list(range(1, 6))
    assert [event.event_type for event in events] == [
        "agent_tree.started",
        "agent.registered",
        "agent.running",
        "agent.completed",
        "agent_tree.completed",
    ]


async def test_recovery_takeover_grace_rejects_fresh_running_tree(monkeypatch):
    """开启接管护栏时,新鲜的 running 树被拒绝重新认领(共享库多进程护栏)。"""

    from backend.core.agents.control import AgentStateError

    store = InMemoryAgentRuntimeStore()
    first = _manager(store)
    await first.register("child-1")
    await first.reserve("child-1")

    # 第二个"worker"在原持有者仍活跃时尝试认领同一棵树。
    monkeypatch.setenv("AGENT_RUNTIME_RECOVERY_TAKEOVER_GRACE_SECONDS", "300")
    second = _manager(store)
    with pytest.raises(AgentStateError, match="owned by a live worker"):
        await second.register("child-2")

    # 默认(0)保持单 worker 立即恢复语义。
    monkeypatch.setenv("AGENT_RUNTIME_RECOVERY_TAKEOVER_GRACE_SECONDS", "0")
    third = _manager(store)
    await third.register("child-3")
    persisted_ids = {
        snapshot.agent_id for snapshot in await store.load_tree_agents("root-1")
    }
    assert "child-3" in persisted_ids


async def test_budget_persist_retries_once_on_version_conflict():
    """预算落盘遇到陈旧版本时重读重试一次而不是静默放弃。"""

    from backend.core.agents.runtime_store import AgentRuntimeVersionConflict

    store = InMemoryAgentRuntimeStore()
    manager = _manager(store)
    await manager.register("child-1")

    original_upsert = store.upsert_run
    conflicts = {"raised": False}

    async def _conflict_once(snapshot, **kwargs):
        if not conflicts["raised"]:
            conflicts["raised"] = True
            raise AgentRuntimeVersionConflict("stale version (injected)")
        return await original_upsert(snapshot, **kwargs)

    store.upsert_run = _conflict_once
    await manager.consume_budget(tokens=123)

    run = await store.load_run("root-1")
    assert run is not None
    assert conflicts["raised"] is True
    assert run.metadata["budget_usage"]["tokens_used"] == 123


async def test_interrupted_terminal_persist_is_replayed_by_abandoned_cleanup():
    """finalizer 超时取消打断落库后,补写路径必须把终态写进存储。

    _finish 在锁内完成内存转换、锁外 await 持久化;取消落在持久化期间时
    内存已终态而存储仍是 running。修复前 abandoned-owner 清理看到终态直接
    return,存储永远停留在 running,重启后会被当作未完成任务恢复重跑。
    """

    store = InMemoryAgentRuntimeStore()
    manager = _manager(store)
    await manager.register("child-1")
    await manager.reserve("child-1")

    original_persist = manager._persist_agent_info
    interrupted_once = {"done": False}

    async def _cancel_first_persist(info, **kwargs):
        if not interrupted_once["done"]:
            interrupted_once["done"] = True
            raise asyncio.CancelledError
        return await original_persist(info, **kwargs)

    manager._persist_agent_info = _cancel_first_persist
    with pytest.raises(asyncio.CancelledError):
        await manager.complete("child-1")

    # 内存已终态,存储仍是 running —— 正是分叉窗口。
    in_memory = await manager.get("child-1")
    assert in_memory.status is AgentStatus.COMPLETED
    persisted = (await store.load_tree_agents("root-1"))[0]
    assert persisted.status == "running"

    # 终态转换已完成的 abandoned 清理路径必须补写遗留快照。
    await manager._flush_pending_terminal_persists("child-1")
    persisted_after = (await store.load_tree_agents("root-1"))[0]
    assert persisted_after.status == "completed"
    assert manager._pending_terminal_persists == {}


@pytest.mark.parametrize("kind", ["memory", "sqlite"])
async def test_repeated_and_conflicting_terminal_finishes_are_idempotent(
    kind,
    tmp_path,
):
    store = _store(kind, tmp_path)
    manager = _manager(store)
    await manager.register("child-1")
    await manager.reserve("child-1")

    first = await manager.complete("child-1")
    persisted_before = (await store.load_tree_agents("root-1"))[0]
    events_before = await store.load_lifecycle_events("root-1")
    repeated = await manager.complete("child-1")
    conflicting = await manager.fail("child-1", "late failure")

    assert first.status is AgentStatus.COMPLETED
    assert repeated == first
    assert conflicting == first
    assert (await store.load_tree_agents("root-1"))[0] == persisted_before
    assert await store.load_lifecycle_events("root-1") == events_before


@pytest.mark.parametrize("kind", ["memory", "sqlite"])
async def test_interrupt_persists_descendant_outcomes_and_ordered_events(
    kind,
    tmp_path,
):
    store = _store(kind, tmp_path)
    manager = _manager(store)
    await manager.register("parent")
    await manager.reserve("parent")
    await manager.register("child", parent_id="parent")

    interrupted = await manager.interrupt("parent", reason="operator stop")
    final_parent = await manager.complete("parent")

    assert {
        info.agent_id: info.status
        for info in interrupted
    } == {
        "parent": AgentStatus.INTERRUPTING,
        "child": AgentStatus.INTERRUPTED,
    }
    assert final_parent.status is AgentStatus.INTERRUPTED

    persisted = {
        snapshot.agent_id: snapshot
        for snapshot in await store.load_tree_agents("root-1")
    }
    assert persisted["parent"].status == "interrupted"
    assert persisted["parent"].error == "operator stop"
    assert persisted["child"].status == "interrupted"
    assert persisted["child"].error == "operator stop"

    events = await store.load_lifecycle_events("root-1")
    assert [event.sequence for event in events] == list(range(1, 8))
    assert [event.event_type for event in events] == [
        "agent_tree.started",
        "agent.registered",
        "agent.running",
        "agent.registered",
        "agent.interrupted",
        "agent.interrupting",
        "agent.interrupted",
    ]


@pytest.mark.parametrize("kind", ["memory", "sqlite"])
async def test_same_root_restart_restores_nonterminal_agents(kind, tmp_path):
    first_store = _store(kind, tmp_path)
    first_manager = _manager(first_store)
    await first_manager.register(
        "before-restart",
        metadata={"checkpoint": "persisted"},
    )

    restarted_store = (
        first_store
        if kind == "memory"
        else SQLiteAgentRuntimeStore(tmp_path / "agent-control.sqlite3")
    )
    restarted_manager = _manager(restarted_store)
    await restarted_manager.register("after-restart")

    restored = {
        info.agent_id: info
        for info in await restarted_manager.list()
    }
    assert set(restored) == {"before-restart", "after-restart"}
    assert restored["before-restart"].status is AgentStatus.PENDING
    assert restored["before-restart"].metadata == {
        "checkpoint": "persisted"
    }


async def test_same_root_restart_restores_budget_usage_and_limits(tmp_path):
    path = tmp_path / "budget-recovery.sqlite3"
    first = AgentControl(
        "budget-root",
        budget=RunBudget(
            max_spawned_agents=4,
            max_active_agents=2,
            max_depth=1,
            max_tokens=10,
            max_tool_calls=3,
        ),
        store=SQLiteAgentRuntimeStore(path),
    )
    await first.consume_budget(tokens=4, tool_calls=1)

    restarted = AgentControl(
        "budget-root",
        store=SQLiteAgentRuntimeStore(path),
    )
    snapshot = await restarted.budget_snapshot()

    assert snapshot.max_tokens == 10
    assert snapshot.max_tool_calls == 3
    assert snapshot.tokens_used == 4
    assert snapshot.tool_calls_used == 1

    await restarted.consume_budget(tokens=6, tool_calls=2)
    with pytest.raises(AgentBudgetExceededError) as exc_info:
        await restarted.consume_budget(tokens=1)
    assert exc_info.value.dimension == "tokens"


async def test_blocking_tool_loop_persists_budget_on_manager_owner_loop():
    store = InMemoryAgentRuntimeStore()
    manager = AgentControl(
        "cross-loop-budget-root",
        budget=RunBudget(max_tool_calls=2),
        store=store,
    )
    await manager.budget_snapshot()

    # Bind the persistence lock to this request loop, then hold it while a
    # copied-context blocking tool loop charges the shared tree budget.  This
    # reproduces the production contention that previously raised "bound to a
    # different event loop" and silently skipped the durable usage update.
    lock = manager._persistence_lock
    await lock.acquire()
    waiter = asyncio.create_task(lock.acquire())
    await asyncio.sleep(0)
    assert waiter.done() is False
    lock.release()
    await waiter
    lock.release()

    await lock.acquire()
    worker = asyncio.create_task(
        asyncio.to_thread(lambda: asyncio.run(manager.consume_budget(tool_calls=1)))
    )
    try:
        await asyncio.sleep(0.02)
    finally:
        lock.release()

    snapshot = await asyncio.wait_for(worker, timeout=1)
    persisted = await store.load_run(manager.root_agent_id)

    assert snapshot.tool_calls_used == 1
    assert persisted is not None
    assert persisted.metadata["budget_usage"]["tool_calls_used"] == 1


@pytest.mark.parametrize("kind", ["memory", "sqlite"])
async def test_direct_queued_reservation_cancellation_is_persisted(kind, tmp_path):
    store = _store(kind, tmp_path)
    manager = AgentControl(
        "root-1",
        budget=RunBudget(
            max_spawned_agents=4,
            max_active_agents=1,
            max_depth=1,
        ),
        store=store,
    )
    blocker_started = asyncio.Event()
    release_blocker = asyncio.Event()

    async def hold_capacity():
        async with manager.spawn("blocker"):
            blocker_started.set()
            await release_blocker.wait()

    blocker = asyncio.create_task(hold_capacity())
    await blocker_started.wait()
    await manager.register("queued")
    reservation = asyncio.create_task(manager.reserve("queued"))
    await asyncio.sleep(0)

    reservation.cancel()
    with pytest.raises(asyncio.CancelledError):
        await reservation

    persisted = {
        item.agent_id: item
        for item in await store.load_tree_agents("root-1")
    }
    assert persisted["queued"].status == "interrupted"
    assert persisted["queued"].error == "agent reservation cancelled"

    release_blocker.set()
    await blocker
