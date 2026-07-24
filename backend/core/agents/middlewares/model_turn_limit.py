"""Enforce runtime-agent model-turn contracts at the graph boundary."""

from __future__ import annotations

from collections.abc import Awaitable, Callable

from langchain.agents.middleware import AgentMiddleware
from langchain.agents.middleware.types import (
    ModelCallResult,
    ModelRequest,
    ModelResponse,
)
from langchain_core.messages import SystemMessage
from langgraph.runtime import Runtime
from typing_extensions import override  # noqa: UP035 (Python 3.10 support)

from backend.core.agents.state import ThreadState


class ModelTurnLimitExceeded(RuntimeError):
    """Raised before a model call would exceed the role's semantic turn limit."""


class ModelTurnLimitMiddleware(AgentMiddleware[ThreadState]):
    """Count model generations independently of LangGraph supersteps."""

    _FINALIZATION_MARKER = "<runtime_agent_finalization_boundary>"

    def __init__(self, max_turns: int):
        if isinstance(max_turns, bool) or int(max_turns) < 1:
            raise ValueError("max_turns must be at least 1")
        self.max_turns = int(max_turns)

    def _before_model(self, state: ThreadState) -> dict[str, int]:
        completed_turns = int(state.get("runtime_model_turns") or 0)
        if completed_turns >= self.max_turns:
            raise ModelTurnLimitExceeded(
                f"runtime agent exceeded semantic model-turn limit ({self.max_turns})"
            )
        return {"runtime_model_turns": 1}

    def _force_finalization_request(self, request: ModelRequest) -> ModelRequest:
        """Disable tools on the last admitted turn so the agent can conclude.

        ``before_model`` increments the additive semantic-turn counter before
        the model node is entered. Therefore a value equal to ``max_turns`` in
        the request state identifies the final admitted generation.
        """

        admitted_turn = int((request.state or {}).get("runtime_model_turns") or 0)
        if admitted_turn < self.max_turns:
            return request
        system_message = request.system_message
        system_content = str(system_message.content or "") if system_message else ""
        if self._FINALIZATION_MARKER not in system_content:
            finalization_instruction = (
                f"{self._FINALIZATION_MARKER}\n"
                "This is the final admitted model turn for the current runtime agent. "
                "Do not call any tool. Immediately synthesize the best supported answer "
                "from evidence already present, preserve actual citations, state missing "
                "evidence explicitly, and finish the assigned subtask.\n"
                "</runtime_agent_finalization_boundary>"
            )
            system_content = "\n\n".join(
                part for part in (system_content.rstrip(), finalization_instruction) if part
            )
            system_message = (
                system_message.model_copy(update={"content": system_content})
                if system_message is not None
                else SystemMessage(content=system_content)
            )
        return request.override(
            system_message=system_message,
            tools=[],
            tool_choice=None,
        )

    @override
    def before_model(
        self,
        state: ThreadState,
        runtime: Runtime,
    ) -> dict[str, int]:
        del runtime
        return self._before_model(state)

    @override
    async def abefore_model(
        self,
        state: ThreadState,
        runtime: Runtime,
    ) -> dict[str, int]:
        del runtime
        return self._before_model(state)

    @override
    def wrap_model_call(
        self,
        request: ModelRequest,
        handler: Callable[[ModelRequest], ModelResponse],
    ) -> ModelCallResult:
        return handler(self._force_finalization_request(request))

    @override
    async def awrap_model_call(
        self,
        request: ModelRequest,
        handler: Callable[[ModelRequest], Awaitable[ModelResponse]],
    ) -> ModelCallResult:
        return await handler(self._force_finalization_request(request))
