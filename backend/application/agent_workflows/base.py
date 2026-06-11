"""Workflow protocol for agent execution."""

from __future__ import annotations

from typing import AsyncIterator, Protocol

from backend.domain.agents.events import AgentEvent
from backend.domain.agents.request import AgentRequest
from backend.domain.agents.runtime_context import AgentRuntimeContext


class AgentWorkflow(Protocol):
    name: str

    async def run(
        self,
        request: AgentRequest,
        context: AgentRuntimeContext,
    ) -> AsyncIterator[AgentEvent]:
        ...

