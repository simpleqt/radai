"""GuardrailProvider protocol and data structures for pre-tool-call authorization."""

from __future__ import annotations

from dataclasses import dataclass, field
from typing import Any, Protocol, runtime_checkable


@dataclass
class GuardrailRequest:
    tool_name: str
    tool_input: dict[str, Any]
    agent_id: str | None = None
    thread_id: str | None = None
    is_subagent: bool = False
    timestamp: str = ""


@dataclass
class GuardrailReason:
    code: str
    message: str = ""


@dataclass
class GuardrailDecision:
    allow: bool
    reasons: list[GuardrailReason] = field(default_factory=list)
    policy_id: str | None = None
    metadata: dict[str, Any] = field(default_factory=dict)


@runtime_checkable
class GuardrailProvider(Protocol):
    name: str

    def evaluate(self, request: GuardrailRequest) -> GuardrailDecision: ...

    async def aevaluate(self, request: GuardrailRequest) -> GuardrailDecision: ...
