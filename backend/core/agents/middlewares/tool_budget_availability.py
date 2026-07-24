"""Hide only tools whose request-scoped call allowance is exhausted."""

from __future__ import annotations

import json
import logging
from collections.abc import Awaitable, Callable
from typing import Any

from langchain.agents.middleware import AgentMiddleware
from langchain.agents.middleware.types import (
    ModelCallResult,
    ModelRequest,
    ModelResponse,
)
from langchain_core.messages import AIMessage
from typing_extensions import override  # noqa: UP035 (Python 3.10 support)

from backend.core.agents.state import ThreadState
from backend.core.tools.request_limits import get_tool_budget_remaining

logger = logging.getLogger(__name__)


def _tool_name(tool: Any) -> str:
    if isinstance(tool, dict):
        function = tool.get("function")
        if isinstance(function, dict):
            return str(function.get("name") or "")
        return str(tool.get("name") or "")
    return str(getattr(tool, "name", "") or "")


class ToolBudgetAvailabilityMiddleware(AgentMiddleware[ThreadState]):
    """Remove exhausted tools while preserving every other capability."""

    @staticmethod
    def _available_request(request: ModelRequest) -> ModelRequest:
        tools = list(request.tools or [])
        if not tools:
            return request
        available = [
            tool
            for tool in tools
            if get_tool_budget_remaining(_tool_name(tool)) != 0
        ]
        if len(available) == len(tools):
            return request
        return request.override(tools=available, tool_choice=None)

    @staticmethod
    def _call_signature(tool_call: dict[str, Any]) -> str:
        return json.dumps(
            {
                "name": str(tool_call.get("name") or ""),
                "args": tool_call.get("args") or {},
            },
            ensure_ascii=False,
            sort_keys=True,
            default=str,
        )

    @classmethod
    def _bounded_response(cls, response: ModelCallResult) -> ModelCallResult:
        """Drop duplicate or over-budget calls emitted in one model response.

        A model can emit several parallel calls while the tool still has one or
        two calls remaining.  Provider-side accounting must remain the final
        guard, but trimming here prevents predictable excess calls from
        becoming noisy budget-error ToolMessages.
        """

        if isinstance(response, AIMessage):
            messages = [response]
            structured_response = None
            bare_message = True
        elif isinstance(response, ModelResponse):
            messages = list(response.result)
            structured_response = response.structured_response
            bare_message = False
        else:
            return response

        used_by_tool: dict[str, int] = {}
        seen_by_tool: dict[str, set[str]] = {}
        bounded_messages = []
        changed = False

        for message in messages:
            if not isinstance(message, AIMessage) or not message.tool_calls:
                bounded_messages.append(message)
                continue

            kept_calls = []
            for tool_call in message.tool_calls:
                name = str(tool_call.get("name") or "")
                remaining = get_tool_budget_remaining(name)
                if remaining is None:
                    kept_calls.append(tool_call)
                    continue

                signature = cls._call_signature(tool_call)
                seen = seen_by_tool.setdefault(name, set())
                used = used_by_tool.get(name, 0)
                if signature in seen or used >= remaining:
                    changed = True
                    logger.info(
                        "Dropped duplicate or over-budget tool call "
                        "before dispatch: %s",
                        name,
                    )
                    continue

                seen.add(signature)
                used_by_tool[name] = used + 1
                kept_calls.append(tool_call)

            if len(kept_calls) == len(message.tool_calls):
                bounded_messages.append(message)
                continue

            additional_kwargs = dict(message.additional_kwargs)
            # Some providers retain their unparsed tool-call payload here.
            # Leaving it behind would reintroduce calls removed above when the
            # message is serialized again.
            additional_kwargs.pop("tool_calls", None)
            bounded_messages.append(
                message.model_copy(
                    update={
                        "tool_calls": kept_calls,
                        "additional_kwargs": additional_kwargs,
                    }
                )
            )

        if not changed:
            return response
        if bare_message:
            return bounded_messages[0]
        return ModelResponse(
            result=bounded_messages,
            structured_response=structured_response,
        )

    @override
    def wrap_model_call(
        self,
        request: ModelRequest,
        handler: Callable[[ModelRequest], ModelResponse],
    ) -> ModelCallResult:
        return self._bounded_response(handler(self._available_request(request)))

    @override
    async def awrap_model_call(
        self,
        request: ModelRequest,
        handler: Callable[[ModelRequest], Awaitable[ModelResponse]],
    ) -> ModelCallResult:
        response = await handler(self._available_request(request))
        return self._bounded_response(response)
