"""Shared policy boundary for direct tool calls outside LangGraph."""

from __future__ import annotations

import asyncio
from collections.abc import Mapping
from typing import Any

from backend.application.tools.execution import (
    invoke_tool_with_timeout,
    tool_metadata,
)
from backend.core.agents.middlewares.guardrail_provider import GuardrailRequest
from backend.core.agents.middlewares.runtime_budget import charge_agent_tool_call


class RuntimeToolGateway:
    """Authorize, budget, and execute a preassembled request-scoped tool.

    Both coordinated orchestration and the lightweight executor make direct
    tool calls that do not traverse LangGraph middleware. Keeping that boundary
    here prevents the two execution modes from drifting on guardrails, budget
    charging, timeout handling, and cache scoping.
    """

    def __init__(
        self,
        *,
        tools: Mapping[str, Any],
        guardrail: Any,
        cache_scope: str,
        agent_id: str = "",
        thread_id: str = "",
        missing_tool_message: str = "Tool '{tool_name}' was not assembled for runtime invocation",
        denied_tool_message: str = "runtime policy denied tool '{tool_name}'",
        include_all_denial_reasons: bool = False,
        require_assembled_tool_for_authorization: bool = True,
    ) -> None:
        self._tools = dict(tools)
        self._guardrail = guardrail
        self._cache_scope = str(cache_scope or "")
        self._agent_id = str(agent_id or "")
        self._thread_id = str(thread_id or "")
        self._missing_tool_message = missing_tool_message
        self._denied_tool_message = denied_tool_message
        self._include_all_denial_reasons = include_all_denial_reasons
        self._require_assembled_tool_for_authorization = (
            require_assembled_tool_for_authorization
        )

    def available_tool_names(self) -> set[str]:
        return set(self._tools)

    async def authorize(
        self,
        tool_name: str,
        args: dict[str, Any],
        *,
        tool_obj: Any | None = None,
    ) -> Any:
        resolved_tool = tool_obj or self._tools.get(tool_name)
        if resolved_tool is None and self._require_assembled_tool_for_authorization:
            raise RuntimeError(self._missing_tool_message.format(tool_name=tool_name))

        metadata = tool_metadata(resolved_tool)
        decision = await asyncio.wait_for(
            self._guardrail.aevaluate(
                GuardrailRequest(
                    tool_name=tool_name,
                    tool_input=dict(args),
                    agent_id=self._agent_id,
                    thread_id=self._thread_id,
                    risk_level=str(metadata.get("risk_level") or "safe"),
                    side_effect=str(metadata.get("side_effect") or "none"),
                    policy_source=str(metadata.get("policy_source") or ""),
                )
            ),
            timeout=5.0,
        )
        if decision.allow:
            return resolved_tool

        reasons = [reason.code for reason in decision.reasons]
        if self._include_all_denial_reasons:
            reason_text = ", ".join(reasons) or "denied"
        else:
            reason_text = reasons[0] if reasons else "denied"
        prefix = self._denied_tool_message.format(tool_name=tool_name)
        raise PermissionError(f"{prefix}: {reason_text}")

    async def invoke(self, tool_name: str, args: dict[str, Any]) -> Any:
        tool_obj = await self.authorize(tool_name, args)
        if tool_obj is None:
            raise RuntimeError(self._missing_tool_message.format(tool_name=tool_name))
        await charge_agent_tool_call()
        return await invoke_tool_with_timeout(
            tool_obj,
            args,
            tool_name=tool_name,
            cache_scope=self._cache_scope,
        )
