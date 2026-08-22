"""Request-scoped context available while materializing tools."""

from __future__ import annotations

from dataclasses import dataclass, field
from typing import Any


@dataclass(frozen=True)
class ToolRuntimeContext:
    request_id: str
    user_id: str
    agent_code: str
    domain: str
    thread_id: str
    # Authenticated role/claims are injected by the application service.  Tool
    # arguments and public metadata must never be used to derive this value.
    role: str = "user"
    permissions: dict[str, Any] = field(default_factory=dict)
    time_context: str = ""
    request_metadata: dict[str, Any] = field(default_factory=dict)
    domain_context: dict[str, Any] = field(default_factory=dict)
    # Capability-ledger scope key. One request fans out into parallel
    # coordination units and task sub-agents that each own a request-scoped
    # CapabilityLedger; keying the ledger registry by request_id alone made
    # those executions overwrite each other. The scope id is derived from
    # (request_id, execution thread id) at assembly time so the middleware
    # that installs a ledger and the scoped tools that look it up always
    # agree. Empty means "fall back to request_id" (legacy behavior).
    context_scope_id: str = ""
