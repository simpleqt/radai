"""Middleware for intercepting clarification requests."""

import logging
from collections.abc import Callable
from time import time
from typing_extensions import override

from langchain.agents import AgentState
from langchain.agents.middleware import AgentMiddleware
from langchain_core.messages import ToolMessage
from langgraph.prebuilt.tool_node import ToolCallRequest
from langgraph.types import Command

from backend.domain.research import ResearchWorkflowState

logger = logging.getLogger(__name__)


class ClarificationMiddlewareState(AgentState):
    pass


class ClarificationMiddleware(AgentMiddleware[ClarificationMiddlewareState]):
    """Intercepts ask_clarification tool calls and records awaiting state."""

    state_schema = ClarificationMiddlewareState

    def _format_clarification_message(self, args: dict) -> str:
        question = args.get("question", "")
        clarification_type = args.get("clarification_type", "missing_info")
        context = args.get("context")
        options = args.get("options", [])

        type_icons = {
            "missing_info": "❓",
            "ambiguous_requirement": "🤔",
            "approach_choice": "🔀",
            "risk_confirmation": "⚠️",
            "suggestion": "💡",
        }
        icon = type_icons.get(clarification_type, "❓")

        parts = []
        if context:
            parts.append(f"{icon} {context}")
            parts.append(f"\n{question}")
        else:
            parts.append(f"{icon} {question}")

        if options:
            parts.append("")
            for i, option in enumerate(options, 1):
                parts.append(f"  {i}. {option}")

        return "\n".join(parts)

    def _handle_clarification(self, request: ToolCallRequest) -> Command:
        args = request.tool_call.get("args", {})
        question = args.get("question", "")
        logger.info("Intercepted clarification request")

        formatted_message = self._format_clarification_message(args)
        tool_call_id = request.tool_call.get("id", "")

        tool_message = ToolMessage(
            content=formatted_message,
            tool_call_id=tool_call_id,
            name="ask_clarification",
        )

        now_ms = int(time() * 1000)
        clarification_payload = {
            "question": question,
            "clarification_type": args.get("clarification_type", "missing_info"),
            "context": args.get("context"),
            "options": args.get("options") if isinstance(args.get("options"), list) else [],
            "tool_call_id": tool_call_id,
            "created_at": now_ms,
        }

        workflow_payload = {
            "state": ResearchWorkflowState.AWAITING_CLARIFICATION.value,
            "updated_at": now_ms,
            "clarification": clarification_payload,
        }

        return Command(
            update={
                "messages": [tool_message],
                "clarification": clarification_payload,
                "research_workflow": workflow_payload,
            },
        )

    @override
    def wrap_tool_call(
        self,
        request: ToolCallRequest,
        handler: Callable[[ToolCallRequest], ToolMessage | Command],
    ) -> ToolMessage | Command:
        if request.tool_call.get("name") != "ask_clarification":
            return handler(request)
        return self._handle_clarification(request)

    @override
    async def awrap_tool_call(
        self,
        request: ToolCallRequest,
        handler: Callable[[ToolCallRequest], ToolMessage | Command],
    ) -> ToolMessage | Command:
        if request.tool_call.get("name") != "ask_clarification":
            return await handler(request)
        return self._handle_clarification(request)
