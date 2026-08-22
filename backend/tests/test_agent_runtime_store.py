from __future__ import annotations

import asyncio
import sqlite3
from dataclasses import replace

import pytest

from backend.core.agents.runtime_store import (
    AgentRunSnapshot,
    AgentRuntimeSnapshot,
    AgentRuntimeStore,
    AgentRuntimeTerminalStateError,
    AgentRuntimeVersionConflict,
    InMemoryAgentRuntimeStore,
    SQLiteAgentRuntimeStore,
)


def _run(*, status: str = "running", updated_at: float = 1.0) -> AgentRunSnapshot:
    return AgentRunSnapshot(
        root_id="root-1",
        status=status,
        metadata={"request_id": "req-1"},
        created_at=1.0,
        updated_at=updated_at,
        started_at=1.0,
    )


def _agent(
    *,
    status: str = "running",
    updated_at: float = 2.0,
) -> AgentRuntimeSnapshot:
    return AgentRuntimeSnapshot(
        root_id="root-1",
        agent_id="agent-1",
        parent_id="root-1",
        depth=1,
        status=status,
        metadata={"role": "researcher"},
        created_at=1.0,
        updated_at=updated_at,
        started_at=2.0,
    )


def _store(kind: str, tmp_path) -> AgentRuntimeStore:
    if kind == "memory":
        return InMemoryAgentRuntimeStore()
    return SQLiteAgentRuntimeStore(tmp_path / f"runtime-{kind}.sqlite3")


def test_in_memory_store_implements_async_protocol():
    assert isinstance(InMemoryAgentRuntimeStore(), AgentRuntimeStore)


@pytest.mark.parametrize("kind", ["memory", "sqlite"])
async def test_upserts_are_idempotent_and_support_optimistic_cas(kind, tmp_path):
    store = _store(kind, tmp_path)

    first = await store.upsert_agent(_agent())
    duplicate = await store.upsert_agent(_agent(), expected_version=0)
    updated = await store.upsert_agent(
        replace(first, metadata={"role": "writer"}, updated_at=3.0),
        expected_version=first.version,
    )

    assert first.version == 1
    assert duplicate == first
    assert updated.version == 2
    with pytest.raises(AgentRuntimeVersionConflict, match="expected version 1"):
        await store.upsert_agent(
            replace(updated, metadata={"role": "stale"}, updated_at=4.0),
            expected_version=1,
        )


async def test_sqlite_store_recovers_run_agents_and_events_after_reopen(tmp_path):
    path = tmp_path / "runtime-state.sqlite3"
    first_store = SQLiteAgentRuntimeStore(path)
    stored_run = await first_store.upsert_tree(_run())
    stored_agent = await first_store.upsert_agent(_agent())
    stored_event = await first_store.append_lifecycle_event(
        "root-1",
        event_type="agent.started",
        agent_id="agent-1",
        payload={"status": "running"},
        event_id="event-1",
        created_at=3.0,
    )

    reopened = SQLiteAgentRuntimeStore(path)
    resumed_event = await reopened.append_lifecycle_event(
        "root-1",
        event_type="agent.completed",
        agent_id="agent-1",
        payload={"status": "completed"},
        event_id="event-2",
        created_at=4.0,
    )

    assert await reopened.load_run("root-1") == stored_run
    assert await reopened.load_tree_agents("root-1") == [stored_agent]
    assert resumed_event.sequence == 2
    assert await reopened.load_lifecycle_events("root-1") == [
        stored_event,
        resumed_event,
    ]
    with sqlite3.connect(path) as connection:
        assert connection.execute("PRAGMA journal_mode").fetchone()[0] == "wal"
        agent_columns = {
            row[1]
            for row in connection.execute(
                "PRAGMA table_info(agent_runtime_agents)"
            )
        }
        assert {
            "root_id",
            "agent_id",
            "parent_id",
            "depth",
            "status",
            "metadata",
            "created_at",
            "updated_at",
            "started_at",
            "finished_at",
            "error",
            "version",
        } <= agent_columns


@pytest.mark.parametrize("kind", ["memory", "sqlite"])
async def test_concurrent_writes_cannot_regress_a_terminal_agent(kind, tmp_path):
    store = _store(kind, tmp_path)
    running = await store.upsert_agent(_agent())

    async def write_terminal():
        return await store.upsert_agent(
            replace(
                running,
                status="completed",
                updated_at=4.0,
                finished_at=4.0,
            )
        )

    async def write_stale_running():
        try:
            return await store.upsert_agent(
                replace(running, updated_at=3.0),
            )
        except AgentRuntimeTerminalStateError:
            return None

    await asyncio.gather(write_terminal(), write_stale_running())

    persisted = (await store.load_tree_agents("root-1"))[0]
    assert persisted.status == "completed"
    assert persisted.finished_at == 4.0
    with pytest.raises(AgentRuntimeTerminalStateError, match="is terminal"):
        await store.upsert_agent(replace(running, updated_at=5.0))


@pytest.mark.parametrize("kind", ["memory", "sqlite"])
async def test_lifecycle_event_sequences_are_contiguous_under_concurrency(
    kind,
    tmp_path,
):
    store = _store(kind, tmp_path)

    events = await asyncio.gather(
        *(
            store.append_lifecycle_event(
                "root-1",
                event_type="agent.progress",
                agent_id="agent-1",
                payload={"index": index},
                event_id=f"event-{index}",
                created_at=float(index),
            )
            for index in range(20)
        )
    )
    loaded = await store.load_lifecycle_events("root-1")
    retried = await store.append_lifecycle_event(
        "root-1",
        event_type="ignored-on-idempotent-retry",
        event_id="event-7",
    )

    assert sorted(event.sequence for event in events) == list(range(1, 21))
    assert [event.sequence for event in loaded] == list(range(1, 21))
    assert retried == next(event for event in loaded if event.event_id == "event-7")
    assert len(await store.load_lifecycle_events("root-1")) == 20


@pytest.mark.parametrize("kind", ["memory", "sqlite"])
async def test_upsert_agent_with_event_commits_both_atomically(kind, tmp_path):
    """组合写:快照与事件一起提交,事件按 event_id 幂等。"""

    store = _store(kind, tmp_path)

    persisted, event = await store.upsert_agent_with_event(
        _agent(),
        expected_version=0,
        event_type="agent.running",
        event_agent_id="agent-1",
        event_payload={"status": "running"},
        event_id="evt-run-1",
        event_created_at=2.0,
    )
    assert persisted.version == 1
    assert event.sequence == 1
    assert event.event_type == "agent.running"

    # 幂等重试:同 event_id 不追加新事件,快照 upsert 也是幂等的。
    retried, retried_event = await store.upsert_agent_with_event(
        _agent(),
        expected_version=0,
        event_type="agent.running",
        event_agent_id="agent-1",
        event_payload={"status": "running"},
        event_id="evt-run-1",
        event_created_at=2.0,
    )
    assert retried == persisted
    assert retried_event.sequence == 1
    assert len(await store.load_lifecycle_events("root-1")) == 1

    # CAS 冲突:快照与事件都不落库。
    with pytest.raises(AgentRuntimeVersionConflict):
        await store.upsert_agent_with_event(
            replace(persisted, metadata={"role": "stale"}, updated_at=9.0),
            expected_version=0,
            event_type="agent.failed",
            event_agent_id="agent-1",
            event_payload={},
            event_id="evt-should-not-exist",
        )
    assert (await store.load_tree_agents("root-1"))[0] == persisted
    assert len(await store.load_lifecycle_events("root-1")) == 1


async def test_in_memory_store_isolates_nested_metadata_mutations():
    """clone 深拷贝:调用方原地改嵌套 metadata 不得污染 store 内部快照。"""

    store = InMemoryAgentRuntimeStore()
    snapshot = AgentRuntimeSnapshot(
        root_id="root-1",
        agent_id="agent-1",
        parent_id="root-1",
        depth=1,
        status="running",
        metadata={"budget_usage": {"tokens_used": 1}, "tags": ["a"]},
        created_at=1.0,
        updated_at=2.0,
    )
    persisted = await store.upsert_agent(snapshot)

    # 写入后改调用方对象的嵌套结构。
    snapshot.metadata["budget_usage"]["tokens_used"] = 999
    snapshot.metadata["tags"].append("b")
    # 读出后改返回值的嵌套结构。
    persisted.metadata["budget_usage"]["tokens_used"] = 777

    reloaded = (await store.load_tree_agents("root-1"))[0]
    assert reloaded.metadata["budget_usage"]["tokens_used"] == 1
    assert reloaded.metadata["tags"] == ["a"]
