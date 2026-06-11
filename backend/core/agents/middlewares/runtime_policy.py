"""Guardrail provider enforcing resolved runtime permissions."""

from __future__ import annotations

from typing import Any

from backend.core.agents.middlewares.allowlist import AllowlistProvider
from backend.core.agents.middlewares.guardrail_provider import (
    GuardrailDecision,
    GuardrailReason,
    GuardrailRequest,
)


class RuntimePolicyGuardrailProvider:
    name = "runtime-profile-policy"

    def __init__(
        self,
        *,
        allowed_tools: list[str],
        guardrail_policy: str,
        permissions: dict[str, Any],
    ):
        self._allowlist = AllowlistProvider(allowed_tools=allowed_tools)
        self._policy = guardrail_policy
        self._permissions = permissions

    def evaluate(self, request: GuardrailRequest) -> GuardrailDecision:
        allowlist_decision = self._allowlist.evaluate(request)
        if not allowlist_decision.allow:
            return allowlist_decision

        if request.tool_name == "multi_kb_search":
            allowed_ids = (
                self._permissions.get("knowledge_base", {}).get("allowed_ids", [])
            )
            if not allowed_ids:
                return self._deny("knowledge_base_not_authorized")

        if request.tool_name.startswith("groundwater_"):
            authorized = (
                self._permissions.get("domain_context", {}).get("authorized", False)
            )
            if self._policy != "groundwater_project_scope" or not authorized:
                return self._deny("groundwater_project_not_authorized")

        return GuardrailDecision(
            allow=True,
            reasons=[GuardrailReason(code="allowed")],
            policy_id=self._policy,
        )

    async def aevaluate(self, request: GuardrailRequest) -> GuardrailDecision:
        return self.evaluate(request)

    def _deny(self, code: str) -> GuardrailDecision:
        return GuardrailDecision(
            allow=False,
            reasons=[GuardrailReason(code=code, message="runtime policy denied tool call")],
            policy_id=self._policy,
        )
