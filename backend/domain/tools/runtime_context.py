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
