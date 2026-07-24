"""Charge model and tool usage to the request-tree runtime budget."""

from __future__ import annotations

from collections.abc import Awaitable, Callable, Mapping
from typing import Any

from langchain.agents import AgentState
from langchain.agents.middleware import AgentMiddleware
from langchain.agents.middleware.types import (
    ModelCallResult,
    ModelRequest,
    ModelResponse,
)
from langchain_core.messages import BaseMessage
from langgraph.prebuilt.tool_node import ToolCallRequest
from typing_extensions import override  # noqa: UP035 (Python 3.10 support)

from backend.core.agents.control import get_agent_runtime_manager


def _usage_int(value: Any) -> int | None:
    """Normalize provider usage values without accepting lossy numbers."""

    if isinstance(value, bool) or value is None:
        return None
    if isinstance(value, int):
        return max(0, value)
    if isinstance(value, float) and value.is_integer():
        return max(0, int(value))
    if isinstance(value, str):
        normalized = value.strip()
        if normalized.isdigit():
            return int(normalized)
    return None


def _mapping_total_tokens(usage: Mapping[str, Any] | None) -> int | None:
    if not usage:
        return None
    for key in ("total_tokens", "total_token_count"):
        total = _usage_int(usage.get(key))
        if total is not None:
            return total

    input_tokens = None
    for key in ("input_tokens", "prompt_tokens"):
        input_tokens = _usage_int(usage.get(key))
        if input_tokens is not None:
            break
    output_tokens = None
    for key in ("output_tokens", "completion_tokens"):
        output_tokens = _usage_int(usage.get(key))
        if output_tokens is not None:
            break
    if input_tokens is None and output_tokens is None:
        return None
    return (input_tokens or 0) + (output_tokens or 0)


def message_token_usage(message: BaseMessage) -> int:
    """Extract one message's total usage across common provider schemas."""

    usage_metadata = getattr(message, "usage_metadata", None)
    if isinstance(usage_metadata, Mapping):
        total = _mapping_total_tokens(usage_metadata)
        if total is not None:
            return total

    response_metadata = getattr(message, "response_metadata", None)
    if isinstance(response_metadata, Mapping):
        for key in ("token_usage", "usage"):
            nested = response_metadata.get(key)
            if isinstance(nested, Mapping):
                total = _mapping_total_tokens(nested)
                if total is not None:
                    return total
        total = _mapping_total_tokens(response_metadata)
        if total is not None:
            return total

    additional_kwargs = getattr(message, "additional_kwargs", None)
    if isinstance(additional_kwargs, Mapping):
        for key in ("token_usage", "usage"):
            nested = additional_kwargs.get(key)
            if isinstance(nested, Mapping):
                total = _mapping_total_tokens(nested)
                if total is not None:
                    return total
    return 0


def model_response_token_usage(response: ModelResponse) -> int:
    """Return the total usage reported by all messages in a model response."""

    result = getattr(response, "result", None)
    if isinstance(result, list):
        return sum(message_token_usage(message) for message in result)
    if isinstance(response, list):
        return sum(message_token_usage(message) for message in response)
    return message_token_usage(response)


async def check_agent_runtime_budget() -> None:
    """Check cancellation and time limits before a direct model call."""

    manager = get_agent_runtime_manager()
    if manager is None:
        return
    manager.cancellation.raise_if_cancelled()
    await manager.check_budget()


async def charge_agent_tool_call() -> None:
    """Charge one tool call for direct runtimes outside LangGraph middleware."""

    manager = get_agent_runtime_manager()
    if manager is None:
        return
    manager.cancellation.raise_if_cancelled()
    await manager.consume_budget(tool_calls=1)


async def charge_agent_model_response(response: Any) -> None:
    """Charge direct model responses and enforce elapsed/deadline limits."""

    manager = get_agent_runtime_manager()
    if manager is None:
        return
    manager.cancellation.raise_if_cancelled()
    tokens = model_response_token_usage(response)
    if tokens:
        await manager.consume_budget(tokens=tokens)
    else:
        await manager.check_budget()


class RuntimeBudgetMiddleware(AgentMiddleware[AgentState]):
    """Enforce the shared tree budget at async model/tool boundaries.

    Production graph execution uses LangGraph's async path.  Direct orchestration
    tools are charged separately by ``RuntimeToolInvoker`` because they do not
    traverse the graph middleware chain.
    """

    @override
    async def awrap_model_call(
        self,
        request: ModelRequest,
        handler: Callable[[ModelRequest], Awaitable[ModelResponse]],
    ) -> ModelCallResult:
        manager = get_agent_runtime_manager()
        if manager is None:
            return await handler(request)

        await check_agent_runtime_budget()
        response = await handler(request)
        await charge_agent_model_response(response)
        return response

    @override
    async def awrap_tool_call(
        self,
        request: ToolCallRequest,
        handler: Callable[[ToolCallRequest], Awaitable[Any]],
    ) -> Any:
        manager = get_agent_runtime_manager()
        if manager is not None:
            await charge_agent_tool_call()
        return await handler(request)
