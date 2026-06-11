"""Tool-level GuardrailMiddleware — evaluates tool calls before execution.

Works as a LangGraph AgentMiddleware that wraps each tool call.
"""

from __future__ import annotations

import logging
from collections.abc import Awaitable, Callable
from datetime import datetime, timezone
UTC = timezone.utc
from typing_extensions import override

from langchain.agents import AgentState
from langchain.agents.middleware import AgentMiddleware
from langchain_core.messages import ToolMessage
from langgraph.errors import GraphBubbleUp
from langgraph.prebuilt.tool_node import ToolCallRequest
from langgraph.types import Command

from backend.core.agents.middlewares.guardrail_provider import (
    GuardrailDecision,
    GuardrailProvider,
    GuardrailReason,
    GuardrailRequest,
)

logger = logging.getLogger(__name__)


class ToolGuardrailMiddleware(AgentMiddleware[AgentState]):
    """Evaluate tool calls against a GuardrailProvider before execution.

    Denied calls return an error ToolMessage so the agent can adapt.
    """

    def __init__(
        self,
        provider: GuardrailProvider,
        *,
        fail_closed: bool = True,
        passport: str | None = None,
    ):
        self.provider = provider
        self.fail_closed = fail_closed
        self.passport = passport

    def _build_request(self, request: ToolCallRequest) -> GuardrailRequest:
        return GuardrailRequest(
            tool_name=str(request.tool_call.get("name", "")),
            tool_input=request.tool_call.get("args", {}),
            agent_id=self.passport,
            timestamp=datetime.now(UTC).isoformat(),
        )

    def _build_denied_message(
        self, request: ToolCallRequest, decision: GuardrailDecision
    ) -> ToolMessage:
        tool_name = str(request.tool_call.get("name", "unknown_tool"))
        tool_call_id = str(request.tool_call.get("id", "missing_id"))
        reason_text = (
            decision.reasons[0].message
            if decision.reasons
            else "blocked by guardrail policy"
        )
        reason_code = (
            decision.reasons[0].code if decision.reasons else "denied"
        )
        return ToolMessage(
            content=(
                f"Guardrail denied: tool '{tool_name}' was blocked ({reason_code}). "
                f"Reason: {reason_text}. Choose an alternative approach."
            ),
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
        gr = self._build_request(request)
        try:
            decision = self.provider.evaluate(gr)
        except GraphBubbleUp:
            raise
        except Exception:
            logger.exception("Guardrail provider error (sync)")
            if self.fail_closed:
                decision = GuardrailDecision(
                    allow=False,
                    reasons=[
                        GuardrailReason(
                            code="evaluator_error",
                            message="guardrail provider error (fail-closed)",
                        )
                    ],
                )
            else:
                return handler(request)
        if not decision.allow:
            logger.warning(
                "Guardrail denied: tool=%s code=%s",
                gr.tool_name,
                decision.reasons[0].code if decision.reasons else "unknown",
            )
            return self._build_denied_message(request, decision)
        return handler(request)

    @override
    async def awrap_tool_call(
        self,
        request: ToolCallRequest,
        handler: Callable[[ToolCallRequest], Awaitable[ToolMessage | Command]],
    ) -> ToolMessage | Command:
        gr = self._build_request(request)
        try:
            decision = await self.provider.aevaluate(gr)
        except GraphBubbleUp:
            raise
        except Exception:
            logger.exception("Guardrail provider error (async)")
            if self.fail_closed:
                decision = GuardrailDecision(
                    allow=False,
                    reasons=[
                        GuardrailReason(
                            code="evaluator_error",
                            message="guardrail provider error (fail-closed)",
                        )
                    ],
                )
            else:
                return await handler(request)
        if not decision.allow:
            logger.warning(
                "Guardrail denied: tool=%s code=%s",
                gr.tool_name,
                decision.reasons[0].code if decision.reasons else "unknown",
            )
            return self._build_denied_message(request, decision)
        return await handler(request)
