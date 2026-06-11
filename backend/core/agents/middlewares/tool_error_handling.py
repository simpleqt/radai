"""Tool error handling middleware — converts tool exceptions into error ToolMessages."""

import logging
from collections.abc import Awaitable, Callable
from typing_extensions import override

from langchain.agents import AgentState
from langchain.agents.middleware import AgentMiddleware
from langchain_core.messages import ToolMessage
from langchain_core.tools.base import ToolException
from langgraph.errors import GraphBubbleUp
from langgraph.prebuilt.tool_node import ToolCallRequest
from langgraph.types import Command

logger = logging.getLogger(__name__)

_MISSING_TOOL_CALL_ID = "missing_tool_call_id"


class ToolErrorHandlingMiddleware(AgentMiddleware[AgentState]):
    """Convert tool exceptions into error ToolMessages so the run can continue."""

    def _build_error_message(self, request: ToolCallRequest, exc: Exception) -> ToolMessage:
        tool_name = str(request.tool_call.get("name") or "unknown_tool")
        tool_call_id = str(request.tool_call.get("id") or _MISSING_TOOL_CALL_ID)
        detail = str(exc).strip() or exc.__class__.__name__
        if len(detail) > 500:
            detail = detail[:497] + "..."
        content = (
            f"Error: Tool '{tool_name}' failed with {exc.__class__.__name__}: {detail}. "
            "Continue with available context, or choose an alternative tool."
        )
        return ToolMessage(
            content=content,
            tool_call_id=tool_call_id,
            name=tool_name,
            status="error",
        )

    @override
    def wrap_tool_call(
        self,
        request: ToolCallRequest,
        handler: Callable[[ToolCallRequest], ToolMessage | Command],
    ) -> ToolMessage | Command:
        try:
            return handler(request)
        except GraphBubbleUp:
            raise
        except ToolException as exc:
            logger.info(
                "Tool execution raised ToolException (expected): name=%s id=%s msg=%s",
                request.tool_call.get("name"),
                request.tool_call.get("id"),
                str(exc)[:200],
            )
            return self._build_error_message(request, exc)
        except Exception as exc:
            logger.exception(
                "Tool execution failed (sync): name=%s id=%s",
                request.tool_call.get("name"),
                request.tool_call.get("id"),
            )
            return self._build_error_message(request, exc)

    @override
    async def awrap_tool_call(
        self,
        request: ToolCallRequest,
        handler: Callable[[ToolCallRequest], Awaitable[ToolMessage | Command]],
    ) -> ToolMessage | Command:
        try:
            return await handler(request)
        except GraphBubbleUp:
            raise
        except ToolException as exc:
            logger.info(
                "Tool execution raised ToolException (expected): name=%s id=%s msg=%s",
                request.tool_call.get("name"),
                request.tool_call.get("id"),
                str(exc)[:200],
            )
            return self._build_error_message(request, exc)
        except Exception as exc:
            logger.exception(
                "Tool execution failed (async): name=%s id=%s",
                request.tool_call.get("name"),
                request.tool_call.get("id"),
            )
            return self._build_error_message(request, exc)
