"""Simple allowlist/denylist guardrail provider."""

from __future__ import annotations

from backend.core.agents.middlewares.guardrail_provider import (
    GuardrailDecision,
    GuardrailReason,
    GuardrailRequest,
)


class AllowlistProvider:
    name = "allowlist"

    def __init__(
        self,
        *,
        allowed_tools: list[str] | None = None,
        denied_tools: list[str] | None = None,
    ):
        self._allowed = set(allowed_tools) if allowed_tools is not None else None
        self._denied = set(denied_tools) if denied_tools else set()

    def evaluate(self, request: GuardrailRequest) -> GuardrailDecision:
        if self._allowed is not None and request.tool_name not in self._allowed:
            return GuardrailDecision(
                allow=False,
                reasons=[
                    GuardrailReason(
                        code="tool_not_allowed",
                        message=f"tool '{request.tool_name}' not in allowlist",
                    )
                ],
            )
        if request.tool_name in self._denied:
            return GuardrailDecision(
                allow=False,
                reasons=[
                    GuardrailReason(
                        code="tool_denied",
                        message=f"tool '{request.tool_name}' is denied",
                    )
                ],
            )
        return GuardrailDecision(
            allow=True,
            reasons=[GuardrailReason(code="allowed")],
        )

    async def aevaluate(self, request: GuardrailRequest) -> GuardrailDecision:
        return self.evaluate(request)
