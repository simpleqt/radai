"""Public runtime-manager contract, factory, and live local scheduler.

The implementation currently lives beside the legacy ``AgentControl`` name so
existing imports remain valid.  New execution paths should import from this
module and depend on ``AgentRuntimeManager`` rather than the concrete backend.
"""

from __future__ import annotations

import os
import threading
import weakref
from pathlib import Path
from typing import TYPE_CHECKING, Any

if TYPE_CHECKING:
    from sqlalchemy.engine import Engine

from backend.core.agents.control import (
    AgentBudgetExceededError,
    AgentCancellationToken,
    AgentInfo,
    AgentRuntimeManager,
    AgentStateMachine,
    AgentStatus,
    InMemoryAgentRuntimeManager,
    RunBudget,
    RunBudgetSnapshot,
    agent_runtime_manager_scope,
    get_agent_runtime_manager,
    get_current_agent_id,
    reset_agent_runtime_manager,
    set_agent_runtime_manager,
)
from backend.core.agents.runtime_store import (
    AgentRuntimeStore,
    InMemoryAgentRuntimeStore,
    SQLiteAgentRuntimeStore,
)

_IN_MEMORY_RUNTIME_STORE: InMemoryAgentRuntimeStore | None = None
_IN_MEMORY_RUNTIME_STORE_LOCK = threading.Lock()
_LIVE_RUNTIME_MANAGERS: weakref.WeakValueDictionary[str, AgentRuntimeManager] = (
    weakref.WeakValueDictionary()
)
_LIVE_RUNTIME_MANAGERS_LOCK = threading.RLock()


def register_agent_runtime_manager(manager: AgentRuntimeManager) -> None:
    """Make a live request tree addressable by its root request id.

    This registry is intentionally process-local. It gives the HTTP control
    route a safe way to reach the asyncio task tree that owns a request while
    keeping the runtime manager independent from FastAPI. Multi-worker
    deployments still need a shared control plane or sticky routing for
    cross-worker cancellation.
    """

    root_agent_id = str(getattr(manager, "root_agent_id", "")).strip()
    if not root_agent_id:
        raise ValueError("runtime manager root_agent_id must not be empty")
    with _LIVE_RUNTIME_MANAGERS_LOCK:
        _LIVE_RUNTIME_MANAGERS[root_agent_id] = manager


def unregister_agent_runtime_manager(
    root_agent_id: str,
    manager: AgentRuntimeManager | None = None,
) -> None:
    """Remove a live manager, without deleting a newer replacement."""

    normalized_id = str(root_agent_id or "").strip()
    if not normalized_id:
        return
    with _LIVE_RUNTIME_MANAGERS_LOCK:
        current = _LIVE_RUNTIME_MANAGERS.get(normalized_id)
        if current is not None and (manager is None or current is manager):
            _LIVE_RUNTIME_MANAGERS.pop(normalized_id, None)


def get_live_agent_runtime_manager(
    root_agent_id: str,
) -> AgentRuntimeManager | None:
    """Return a live manager in this worker, if the request is still running."""

    normalized_id = str(root_agent_id or "").strip()
    if not normalized_id:
        return None
    with _LIVE_RUNTIME_MANAGERS_LOCK:
        manager = _LIVE_RUNTIME_MANAGERS.get(normalized_id)
        if manager is None or bool(getattr(manager, "closed", False)):
            return None
        return manager


def create_agent_runtime_store_from_env(
    *,
    sql_engine: Engine | None = None,
) -> AgentRuntimeStore | None:
    """Build the explicitly configured durable store without network I/O.

    Persistence is opt-in so upgrades do not silently create files in an
    existing deployment.  ``sqlite`` is suitable for one host or a shared
    volume; ``sql`` reuses the application's shared pooled SQLAlchemy Engine
    and never creates a per-manager connection pool.
    """

    backend = os.getenv("AGENT_RUNTIME_STORE_BACKEND", "disabled").strip().lower()
    if backend in {"", "disabled", "none", "off"}:
        return None
    if backend == "memory":
        global _IN_MEMORY_RUNTIME_STORE
        # 每个流式请求都会走到这里;加锁防止并发首建两个 store 导致运行时
        # 快照分裂(输家线程建的 store 只被自己那次请求使用后丢弃)。
        with _IN_MEMORY_RUNTIME_STORE_LOCK:
            if _IN_MEMORY_RUNTIME_STORE is None:
                _IN_MEMORY_RUNTIME_STORE = InMemoryAgentRuntimeStore()
            return _IN_MEMORY_RUNTIME_STORE
    if backend == "sqlite":
        default_path = (
            Path(__file__).resolve().parents[2]
            / ".runtime"
            / "agent_runtime"
            / "agent_runtime.sqlite3"
        )
        path = Path(os.getenv("AGENT_RUNTIME_STORE_PATH", str(default_path)))
        path.parent.mkdir(parents=True, exist_ok=True)
        return SQLiteAgentRuntimeStore(path)
    if backend in {"sql", "mysql", "sqlalchemy"}:
        from backend.core.agents.sql_runtime_store import (
            SQLAlchemyAgentRuntimeStore,
        )

        if sql_engine is None:
            from backend.db.connection import get_engine

            sql_engine = get_engine()
        return SQLAlchemyAgentRuntimeStore(sql_engine)
    raise ValueError(
        "AGENT_RUNTIME_STORE_BACKEND must be disabled, memory, sqlite, or sql"
    )


def create_agent_runtime_manager(
    root_agent_id: str,
    *,
    budget: RunBudget | None = None,
    cancellation: AgentCancellationToken | None = None,
    run_metadata: dict[str, Any] | None = None,
    store: AgentRuntimeStore | None = None,
) -> InMemoryAgentRuntimeManager:
    """Create one tree manager with the configured durable mirror."""

    manager = InMemoryAgentRuntimeManager(
        root_agent_id=root_agent_id,
        budget=budget,
        cancellation=cancellation,
        store=(store if store is not None else create_agent_runtime_store_from_env()),
        run_metadata=run_metadata,
    )
    register_agent_runtime_manager(manager)
    return manager


__all__ = [
    "AgentBudgetExceededError",
    "AgentCancellationToken",
    "AgentInfo",
    "AgentRuntimeManager",
    "AgentStateMachine",
    "AgentStatus",
    "InMemoryAgentRuntimeManager",
    "RunBudget",
    "RunBudgetSnapshot",
    "agent_runtime_manager_scope",
    "create_agent_runtime_manager",
    "create_agent_runtime_store_from_env",
    "get_live_agent_runtime_manager",
    "get_current_agent_id",
    "get_agent_runtime_manager",
    "reset_agent_runtime_manager",
    "set_agent_runtime_manager",
    "register_agent_runtime_manager",
    "unregister_agent_runtime_manager",
]
