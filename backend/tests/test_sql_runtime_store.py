from __future__ import annotations

import asyncio
from dataclasses import replace

import pytest
from sqlalchemy import create_engine, event, inspect

from backend.core.agents.runtime_store import (
    AgentRunSnapshot,
    AgentRuntimeSnapshot,
    AgentRuntimeStore,
    AgentRuntimeTerminalStateError,
    AgentRuntimeVersionConflict,
)
from backend.core.agents.sql_runtime_store import SQLAlchemyAgentRuntimeStore


def _engine(tmp_path):
    return create_engine(
        f"sqlite:///{tmp_path / 'shared-runtime.sqlite3'}",
        connect_args={"timeout": 30},
    )


def _run(
    *,
    status: str = "running",
    updated_at: float = 1.0,
) -> AgentRunSnapshot:
    return AgentRunSnapshot(
        root_id="root-1",
        status=status,
        metadata={"request_id": "request-1"},
        created_at=1.0,
        updated_at=updated_at,
        started_at=1.0,
    )


def _agent(
    agent_id: str = "agent-1",
    *,
    depth: int = 1,
    created_at: float = 1.0,
    status: str = "running",
    updated_at: float = 2.0,
) -> AgentRuntimeSnapshot:
    return AgentRuntimeSnapshot(
        root_id="root-1",
        agent_id=agent_id,
        parent_id="root-1",
        depth=depth,
        status=status,
        metadata={"role": "researcher"},
        created_at=created_at,
        updated_at=updated_at,
        started_at=2.0,
    )


async def test_sql_runtime_store_constructor_does_not_connect_and_schema_is_explicit(
    tmp_path,
):
    engine = _engine(tmp_path)
    opened_connections: list[object] = []

    @event.listens_for(engine, "connect")
    def on_connect(dbapi_connection, _connection_record):
        opened_connections.append(dbapi_connection)

    store = SQLAlchemyAgentRuntimeStore(engine)

    assert isinstance(store, AgentRuntimeStore)
    assert opened_connections == []

    await store.create_schema()

    assert opened_connections
    inspector = inspect(engine)
    assert {
        "agent_runtime_runs",
        "agent_runtime_agents",
        "agent_runtime_events",
    } <= set(inspector.get_table_names())
    assert {
        column["name"] for column in inspector.get_columns("agent_runtime_runs")
    } == {
        "root_id",
        "status",
        "metadata",
        "created_at",
        "updated_at",
        "started_at",
        "finished_at",
        "error",
        "version",
    }
    engine.dispose()


async def test_sql_runtime_store_run_cas_idempotency_and_terminal_guard(tmp_path):
    engine = _engine(tmp_path)
    store = SQLAlchemyAgentRuntimeStore(engine)
    await store.create_schema()

    first = await store.upsert_tree(_run())
    duplicate = await store.upsert_run(_run(), expected_version=0)
    updated = await store.upsert_run(
        replace(first, metadata={"request_id": "request-2"}, updated_at=2.0),
        expected_version=first.version,
    )

    assert first.version == 1
    assert duplicate == first
    assert updated.version == 2
    with pytest.raises(AgentRuntimeVersionConflict, match="expected version 1"):
        await store.upsert_run(
            replace(updated, metadata={"request_id": "stale"}, updated_at=3.0),
            expected_version=1,
        )

    terminal = await store.upsert_run(
        replace(
            updated,
            status="completed",
            updated_at=4.0,
            finished_at=4.0,
        ),
        expected_version=updated.version,
    )
    assert terminal.version == 3
    with pytest.raises(AgentRuntimeTerminalStateError, match="is terminal"):
        await store.upsert_run(
            replace(terminal, status="running", updated_at=5.0),
        )
    assert await store.load_run("root-1") == terminal
    engine.dispose()


async def test_sql_runtime_store_persists_and_orders_tree_agents(tmp_path):
    engine = _engine(tmp_path)
    store = SQLAlchemyAgentRuntimeStore(engine)
    await store.create_schema()

    later = await store.upsert_agent(_agent("agent-later", depth=1, created_at=3.0))
    deeper = await store.upsert_agent(_agent("agent-deeper", depth=2, created_at=1.0))
    earlier = await store.upsert_agent(_agent("agent-earlier", depth=1, created_at=1.0))

    reopened = SQLAlchemyAgentRuntimeStore(engine)

    assert await reopened.load_tree_agents("root-1") == [earlier, later, deeper]
    assert await reopened.load_tree_agents("missing-root") == []
    engine.dispose()


async def test_sql_runtime_store_transactional_agent_cas_has_one_winner(tmp_path):
    engine = _engine(tmp_path)
    store = SQLAlchemyAgentRuntimeStore(engine)
    await store.create_schema()
    first = await store.upsert_agent(_agent())

    results = await asyncio.gather(
        store.upsert_agent(
            replace(first, metadata={"writer": "a"}, updated_at=3.0),
            expected_version=first.version,
        ),
        store.upsert_agent(
            replace(first, metadata={"writer": "b"}, updated_at=4.0),
            expected_version=first.version,
        ),
        return_exceptions=True,
    )

    winners = [item for item in results if isinstance(item, AgentRuntimeSnapshot)]
    conflicts = [
        item for item in results if isinstance(item, AgentRuntimeVersionConflict)
    ]
    assert len(winners) == 1
    assert winners[0].version == 2
    assert len(conflicts) == 1
    assert await store.load_tree_agents("root-1") == winners
    engine.dispose()


async def test_sql_runtime_store_terminal_agent_cannot_regress_under_concurrency(
    tmp_path,
):
    engine = _engine(tmp_path)
    store = SQLAlchemyAgentRuntimeStore(engine)
    await store.create_schema()
    running = await store.upsert_agent(_agent())

    async def complete_agent():
        return await store.upsert_agent(
            replace(
                running,
                status="completed",
                updated_at=4.0,
                finished_at=4.0,
            )
        )

    async def write_running_snapshot():
        try:
            return await store.upsert_agent(replace(running, updated_at=3.0))
        except AgentRuntimeTerminalStateError:
            return None

    await asyncio.gather(complete_agent(), write_running_snapshot())

    persisted = (await store.load_tree_agents("root-1"))[0]
    assert persisted.status == "completed"
    assert persisted.finished_at == 4.0
    with pytest.raises(AgentRuntimeTerminalStateError):
        await store.upsert_agent(replace(running, updated_at=5.0))
    engine.dispose()


async def test_sql_runtime_store_event_ids_and_sequences_are_root_scoped(tmp_path):
    engine = _engine(tmp_path)
    store = SQLAlchemyAgentRuntimeStore(engine)
    await store.create_schema()

    root_one_events, root_two_events = await asyncio.gather(
        asyncio.gather(
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
        ),
        asyncio.gather(
            *(
                store.append_lifecycle_event(
                    "root-2",
                    event_type="agent.progress",
                    event_id=f"event-{index}",
                    created_at=float(index),
                )
                for index in range(5)
            )
        ),
    )

    assert sorted(event.sequence for event in root_one_events) == list(range(1, 21))
    assert sorted(event.sequence for event in root_two_events) == list(range(1, 6))

    loaded = await store.load_lifecycle_events("root-1")
    retried = await store.append_lifecycle_event(
        "root-1",
        event_type="different-on-retry",
        event_id="event-7",
    )

    assert [event.sequence for event in loaded] == list(range(1, 21))
    assert retried == next(event for event in loaded if event.event_id == "event-7")
    assert len(await store.load_lifecycle_events("root-1")) == 20
    assert [
        event.sequence
        for event in await store.load_lifecycle_events("root-1", after_sequence=17)
    ] == [18, 19, 20]
    engine.dispose()
