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

        risk_level = str(request.risk_level or "safe").strip() or "safe"
        side_effect = str(request.side_effect or "none").strip() or "none"
        metadata = {
            "risk_level": risk_level,
            "side_effect": side_effect,
            "policy_source": request.policy_source,
        }

        if risk_level in self._denied_risk_levels():
            return self._deny("tool_risk_denied", metadata=metadata)

        auth_role = str(self._permissions.get("auth_role") or "user").strip().lower()
        if risk_level == "write" and auth_role in {
            "anonymous",
            "guest",
            "unauthenticated",
        }:
            return self._deny(
                "tool_write_requires_authenticated_user",
                metadata=metadata,
            )

        if not self._side_effect_allowed(side_effect):
            return self._deny("tool_side_effect_denied", metadata=metadata)

        if request.tool_name == "multi_kb_search":
            allowed_ids = (
                self._permissions.get("knowledge_base", {}).get("allowed_ids", [])
            )
            if not allowed_ids:
                return self._deny("knowledge_base_not_authorized", metadata=metadata)

        if request.tool_name.startswith("groundwater_"):
            authorized = (
                self._permissions.get("domain_context", {}).get("authorized", False)
            )
            if self._policy != "groundwater_project_scope" or not authorized:
                return self._deny(
                    "groundwater_project_not_authorized",
                    metadata=metadata,
                )

        return GuardrailDecision(
            allow=True,
            reasons=[GuardrailReason(code="allowed")],
            policy_id=self._policy,
            metadata=metadata,
        )

    async def aevaluate(self, request: GuardrailRequest) -> GuardrailDecision:
        return self.evaluate(request)

    def _deny(
        self,
        code: str,
        *,
        metadata: dict[str, Any] | None = None,
    ) -> GuardrailDecision:
        return GuardrailDecision(
            allow=False,
            reasons=[
                GuardrailReason(
                    code=code,
                    message="runtime policy denied tool call",
                )
            ],
            policy_id=self._policy,
            metadata=dict(metadata or {}),
        )

    def _denied_risk_levels(self) -> set[str]:
        risk_policy = self._mapping("tool_risk")
        values = risk_policy.get("denied_levels")
        if values is None:
            values = self._permissions.get("denied_tool_risk_levels")
        return _string_set(values)

    def _side_effect_allowed(self, side_effect: str) -> bool:
        if side_effect in {"", "none"}:
            return True
        policy = self._mapping("side_effects")
        allowed = policy.get("allowed")
        if allowed is None:
            allowed = self._permissions.get("allowed_tool_side_effects")
        allowed_set = _string_set(allowed)
        if not allowed_set:
            return True
        return side_effect in allowed_set or "*" in allowed_set

    def _mapping(self, key: str) -> dict[str, Any]:
        value = self._permissions.get(key)
        if isinstance(value, dict):
            return value
        return {}


def _string_set(value: Any) -> set[str]:
    if isinstance(value, str):
        return {value} if value else set()
    if not isinstance(value, (list, tuple, set)):
        return set()
    return {str(item) for item in value if str(item or "").strip()}
