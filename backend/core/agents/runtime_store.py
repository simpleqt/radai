"""Persistence boundary for agent-runtime trees and lifecycle events.

The runtime manager mirrors live transitions into these stores when a backend
is explicitly configured. The in-memory scheduler remains the source of live
task ownership; the SQL/SQLite stores provide recovery and inspection state.
"""

from backend.core.agents.runtime_store_model import (  # noqa: F401
    AgentLifecycleEvent,
    AgentRunSnapshot,
    AgentRuntimeSnapshot,
    AgentRuntimeStore,
    AgentRuntimeStoreError,
    AgentRuntimeTerminalStateError,
    AgentRuntimeVersionConflict,
    TERMINAL_AGENT_STATUSES,
    _clone_agent,
    _clone_event,
    _clone_run,
    _metadata_copy,
    _new_event_id,
    _normalize_expected_version,
    _optional_text,
    _payload_copy,
    _required_text,
    _resolve_upsert,
    _status_text,
)
from backend.core.agents.runtime_store_memory import (  # noqa: F401
    InMemoryAgentRuntimeStore,
)
from backend.core.agents.runtime_store_sqlite import (  # noqa: F401
    SQLiteAgentRuntimeStore,
    _SCHEMA_STATEMENTS,
    _agent_values,
    _json_dumps,
    _json_loads,
    _row_to_agent,
    _row_to_event,
    _row_to_run,
    _run_values,
)

__all__ = [
    "AgentLifecycleEvent",
    "AgentRunSnapshot",
    "AgentRuntimeSnapshot",
    "AgentRuntimeStore",
    "AgentRuntimeStoreError",
    "AgentRuntimeTerminalStateError",
    "AgentRuntimeVersionConflict",
    "InMemoryAgentRuntimeStore",
    "SQLiteAgentRuntimeStore",
    "TERMINAL_AGENT_STATUSES",
]
