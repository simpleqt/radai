from __future__ import annotations

from sqlalchemy import create_engine

from backend.core.agents.control import AgentStatus, RunBudget
from backend.core.agents.runtime_manager import (
    create_agent_runtime_manager,
    create_agent_runtime_store_from_env,
)
from backend.core.agents.runtime_store import (
    AgentRunSnapshot,
    AgentRuntimeSnapshot,
    SQLiteAgentRuntimeStore,
)
from backend.core.agents.sql_runtime_store import SQLAlchemyAgentRuntimeStore
from backend.db.migrate import MIGRATIONS, _split_sql


def test_runtime_manager_factory_keeps_persistence_opt_in(monkeypatch):
    monkeypatch.delenv("AGENT_RUNTIME_STORE_BACKEND", raising=False)

    manager = create_agent_runtime_manager("root-disabled")

    assert manager.persistent is False


def test_runtime_store_factory_reuses_injected_sqlalchemy_engine(monkeypatch):
    engine = create_engine("sqlite+pysqlite:///:memory:")
    monkeypatch.setenv("AGENT_RUNTIME_STORE_BACKEND", "sql")

    store = create_agent_runtime_store_from_env(sql_engine=engine)

    assert isinstance(store, SQLAlchemyAgentRuntimeStore)
    assert store.engine is engine
    engine.dispose()


def test_shared_runtime_store_schema_is_a_tracked_mysql_migration():
    migration = next(
        item
        for item in MIGRATIONS
        if item.version == "202607110001_add_agent_runtime_tables"
    )
    statements = _split_sql(migration.path.read_text(encoding="utf-8"))

    assert len(statements) == 3
    assert [
        table
        for table in (
            "agent_runtime_runs",
            "agent_runtime_agents",
            "agent_runtime_events",
        )
        if any(f"CREATE TABLE IF NOT EXISTS {table}" in sql for sql in statements)
    ] == [
        "agent_runtime_runs",
        "agent_runtime_agents",
        "agent_runtime_events",
    ]
    assert all("ENGINE=InnoDB" in statement for statement in statements)
    assert all("CHARSET=utf8mb4" in statement for statement in statements)


async def test_runtime_manager_factory_persists_sqlite_lifecycle(
    tmp_path,
    monkeypatch,
):
    path = tmp_path / "runtime.sqlite3"
    monkeypatch.setenv("AGENT_RUNTIME_STORE_BACKEND", "sqlite")
    monkeypatch.setenv("AGENT_RUNTIME_STORE_PATH", str(path))
    manager = create_agent_runtime_manager(
        "root-sqlite",
        budget=RunBudget(max_spawned_agents=4, max_active_agents=2),
        run_metadata={"request_id": "request-1"},
    )

    async with manager.spawn("child-1", metadata={"role": "researcher"}):
        pass
    await manager.interrupt(manager.root_agent_id, reason="request closed")
    await manager.finalize_run("completed")

    reopened = SQLiteAgentRuntimeStore(path)
    run = await reopened.load_run("root-sqlite")
    agents = await reopened.load_tree_agents("root-sqlite")

    assert run is not None
    assert run.status == "completed"
    assert run.metadata["request_id"] == "request-1"
    assert [(item.agent_id, item.status) for item in agents] == [
        ("child-1", "completed")
    ]


async def test_runtime_manager_recovers_crashed_running_agent_as_pending(
    tmp_path,
    monkeypatch,
):
    path = tmp_path / "recovery.sqlite3"
    store = SQLiteAgentRuntimeStore(path)
    await store.upsert_run(
        AgentRunSnapshot(
            root_id="root-recovery",
            status="running",
            metadata={"request_id": "request-recovery"},
            created_at=1.0,
            updated_at=1.0,
            started_at=1.0,
        )
    )
    await store.upsert_agent(
        AgentRuntimeSnapshot(
            root_id="root-recovery",
            agent_id="stale-running",
            parent_id="root-recovery",
            depth=1,
            status="running",
            metadata={"checkpoint_thread_id": "thread-child"},
            created_at=1.0,
            updated_at=2.0,
            started_at=2.0,
        )
    )
    monkeypatch.setenv("AGENT_RUNTIME_STORE_BACKEND", "sqlite")
    monkeypatch.setenv("AGENT_RUNTIME_STORE_PATH", str(path))

    recovered = create_agent_runtime_manager("root-recovery")
    agents = await recovered.list()
    events = await store.load_lifecycle_events("root-recovery")

    assert len(agents) == 1
    assert agents[0].agent_id == "stale-running"
    assert agents[0].status is AgentStatus.PENDING
    assert agents[0].started_at is None
    assert (await store.load_tree_agents("root-recovery"))[0].status == "pending"
    assert [event.event_type for event in events] == [
        "agent_tree.resumed",
        "agent.recovered",
    ]
