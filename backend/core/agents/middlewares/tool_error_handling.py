"""Tool error handling middleware — converts tool exceptions into error ToolMessages."""

import json
import logging
from collections.abc import Awaitable, Callable
from typing import Any

from langchain.agents import AgentState
from langchain.agents.middleware import AgentMiddleware
from langchain_core.messages import ToolMessage
from langchain_core.tools.base import ToolException
from langgraph.errors import GraphBubbleUp
from langgraph.prebuilt.tool_node import ToolCallRequest
from langgraph.types import Command
from typing_extensions import override  # noqa: UP035 -- runtime supports Python 3.10

from backend.application.tools.execution import (
    ToolReportedError,
    invoke_service_call,
    invoke_sync_with_timeout,
    raise_for_tool_reported_error,
    tool_timeout_seconds,
)

logger = logging.getLogger(__name__)

_MISSING_TOOL_CALL_ID = "missing_tool_call_id"


class ToolErrorHandlingMiddleware(AgentMiddleware[AgentState]):
    """Convert tool exceptions into error ToolMessages so the run can continue."""

    def __init__(self) -> None:
        self._tools: dict[str, Any] = {}

    def bind_tools(self, tools: list[Any]) -> None:
        """Bind materialized tools after feature tools have been assembled."""

        self._tools = {
            str(getattr(tool, "name", "") or ""): tool
            for tool in tools
            if str(getattr(tool, "name", "") or "")
        }

    def _tool(self, request: ToolCallRequest) -> Any:
        name = str(request.tool_call.get("name") or "unknown_tool")
        return getattr(request, "tool", None) or self._tools.get(name) or {"name": name}

    @staticmethod
    def _cache_scope(request: ToolCallRequest) -> str | None:
        runtime = getattr(request, "runtime", None)
        config = getattr(runtime, "config", None)
        configurable = config.get("configurable", {}) if isinstance(config, dict) else {}
        if not isinstance(configurable, dict):
            return None
        request_id = str(configurable.get("request_id") or "").strip()
        if request_id:
            return request_id
        user_id = str(configurable.get("user_id") or "").strip()
        thread_id = str(configurable.get("thread_id") or "").strip()
        if user_id or thread_id:
            return f"{user_id}:{thread_id}"
        return None

    @staticmethod
    def _rebind_cached_result(request: ToolCallRequest, value: Any) -> Any:
        """Cached ToolMessages must belong to the current model tool call."""

        if not isinstance(value, ToolMessage):
            return value
        tool_call_id = str(request.tool_call.get("id") or _MISSING_TOOL_CALL_ID)
        tool_name = str(request.tool_call.get("name") or "unknown_tool")
        return value.model_copy(
            update={"tool_call_id": tool_call_id, "name": tool_name}
        )

    def _build_error_message(self, request: ToolCallRequest, exc: Exception) -> ToolMessage:
        tool_name = str(request.tool_call.get("name") or "unknown_tool")
        tool_call_id = str(request.tool_call.get("id") or _MISSING_TOOL_CALL_ID)
        detail = str(exc).strip() or exc.__class__.__name__
        if len(detail) > 500:
            detail = detail[:497] + "..."
        if isinstance(exc, ToolReportedError) and exc.details:
            recovery = json.dumps(
                exc.details,
                ensure_ascii=False,
                default=str,
                separators=(",", ":"),
            )
            if len(recovery) > 1500:
                recovery = recovery[:1497] + "..."
            content = (
                f"Error: Tool '{tool_name}' reported {exc.error_code}: {detail}. "
                f"Recovery context: {recovery}"
            )
        else:
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
            tool_name = str(request.tool_call.get("name") or "unknown_tool")
            tool_obj = self._tool(request)
            result = invoke_sync_with_timeout(
                lambda: handler(request),
                timeout=tool_timeout_seconds(tool_name, tool_obj),
                label=tool_name,
            )
            raise_for_tool_reported_error(result)
            return result
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
            tool_name = str(request.tool_call.get("name") or "unknown_tool")
            tool_obj = self._tool(request)
            cache_scope = self._cache_scope(request)
            return await invoke_service_call(
                tool_obj,
                lambda: handler(request),
                tool_name=tool_name,
                cache_key_payload=(
                    {
                        "scope": cache_scope,
                        "args": request.tool_call.get("args", {}),
                    }
                    if cache_scope is not None
                    else None
                ),
                cache_hit_transform=lambda value: self._rebind_cached_result(
                    request,
                    value,
                ),
            )
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
