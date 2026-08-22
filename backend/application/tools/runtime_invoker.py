"""Request-scoped internal tool invoker built on ToolAssemblyPipeline."""

from __future__ import annotations

import asyncio
import json
import time
from typing import TYPE_CHECKING, Any

from backend.application.agent_trace import AgentTraceRecorder
from backend.application.agent_trace.models import utc_now_iso
from backend.application.tools.assembler import PolicyToolAssembler
from backend.application.tools.execution import (
    invoke_service_call,
    invoke_tool_with_timeout,
    tool_timeout_seconds,
)
from backend.application.tools.result import normalize_tool_error, normalize_tool_result
from backend.application.tools.runtime_gateway import RuntimeToolGateway
from backend.application.tools.tool_assembly_pipeline import ToolAssemblyPipeline
from backend.config.loggers import get_logger
from backend.core.agents.middlewares.runtime_budget import charge_agent_tool_call
from backend.core.agents.middlewares.runtime_policy import (
    RuntimePolicyGuardrailProvider,
)
from backend.core.skills.scoped_skills_provider import ScopedSkillsProvider
from backend.domain.agents.runtime_context import AgentRuntimeContext
from backend.domain.tools.runtime_context import ToolRuntimeContext

logger = get_logger(__name__)

if TYPE_CHECKING:
    from backend.application.agents.definition import ResolvedAgentDefinition


class RuntimeToolInvoker:
    """Invoke assembled tools from orchestration code with runtime policy checks."""

    def __init__(
        self,
        *,
        context: AgentRuntimeContext,
        workflow_name: str | None = None,
        selected_skills: set[str] | None = None,
        enforce_skill_load: bool = False,
        resolved_agent_definition: ResolvedAgentDefinition | None = None,
    ) -> None:
        if (
            resolved_agent_definition is not None
            and resolved_agent_definition.profile.code != context.agent_profile.code
        ):
            raise ValueError(
                "Resolved definition agent_code does not match runtime context: "
                f"{resolved_agent_definition.profile.code} != "
                f"{context.agent_profile.code}"
            )
        effective_workflow = workflow_name or context.workflow_name
        effective_skills = set(
            context.selected_skills if selected_skills is None else selected_skills
        )
        if effective_workflow == "deep_research":
            effective_skills.add("deep-research")
        self._context = context
        self._workflow_name = effective_workflow
        self._selected_skills = effective_skills
        tool_policy = self._resolve_tool_policy(
            context,
            resolved_agent_definition=resolved_agent_definition,
        )
        registry_scope = (
            resolved_agent_definition.runtime_scope
            if resolved_agent_definition is not None
            else None
        )
        self._runtime_context = ToolRuntimeContext(
            request_id=context.request_id,
            user_id=context.user_id,
            agent_code=context.agent_profile.code,
            domain=context.agent_profile.domain,
            thread_id=context.internal_thread_id,
            role=str(context.permissions.get("auth_role") or "user").strip().lower()
            or "user",
            permissions=dict(context.permissions),
            time_context=context.time_context,
            request_metadata=dict(context.request_metadata),
            domain_context=dict(context.domain_context),
            context_scope_id=(
                f"{context.request_id}::{context.internal_thread_id}"
                if (context.request_id or context.internal_thread_id)
                else ""
            ),
        )
        self._tools = {
            str(getattr(tool, "name", "") or ""): tool
            for tool in ToolAssemblyPipeline(
                subagent_enabled=False,
                use_search=context.use_search,
                deep_retrieval=getattr(context, "deep_retrieval", False),
                kb_ids=list(context.kb_ids),
                selected_skills=effective_skills,
                skills_provider=ScopedSkillsProvider(
                    skills_base_path=(
                        registry_scope.skills_base_path
                        if registry_scope is not None
                        else None
                    ),
                    user_id=context.user_id,
                    authorized_skill_names=(
                        effective_skills
                        if registry_scope is not None
                        or context.permissions.get("managed_skills_resolved")
                        else None
                    ),
                ),
                task_type=effective_workflow,
                lightweight_mode=False,
                thread_id=context.internal_thread_id,
                domain=context.agent_profile.domain,
                workflow_name=effective_workflow,
                tool_policy=tool_policy,
                policy_tool_assembler=PolicyToolAssembler(
                    registry=(
                        registry_scope.tool_registry
                        if registry_scope is not None
                        else None
                    )
                ),
                tool_runtime_context=self._runtime_context,
                enforce_skill_load=enforce_skill_load,
            ).assemble()
            if str(getattr(tool, "name", "") or "")
        }
        self._guardrail = RuntimePolicyGuardrailProvider(
            allowed_tools=sorted(self._tools),
            guardrail_policy=context.agent_profile.guardrail_policy,
            permissions=dict(context.permissions),
        )
        self._tool_gateway = RuntimeToolGateway(
            tools=self._tools,
            guardrail=self._guardrail,
            cache_scope=context.request_id,
            agent_id=context.agent_profile.guardrail_policy,
            thread_id=context.internal_thread_id,
        )
        self.invoked_tools: list[str] = []
        self._trace = AgentTraceRecorder.from_context(
            context,
            workflow_name=effective_workflow,
        )
        self._tool_call_sequence = 0

    @staticmethod
    def _resolve_tool_policy(
        context: AgentRuntimeContext,
        *,
        resolved_agent_definition: ResolvedAgentDefinition | None = None,
    ) -> str:
        policy = str(
            resolved_agent_definition.tool_policy.name
            if resolved_agent_definition is not None
            else context.agent_profile.tool_policy or ""
        ).strip()
        if policy and policy != "default":
            return policy
        raise ValueError(
            f"Agent profile '{context.agent_profile.code}' must declare an "
            "explicit registered tool_policy"
        )

    def available_tool_names(self) -> set[str]:
        return self._tool_gateway.available_tool_names()

    @staticmethod
    def _timeout_seconds(tool_name: str, tool_obj: Any | None = None) -> float:
        return tool_timeout_seconds(tool_name, tool_obj)

    async def _await_tool(self, tool_name: str, awaitable, tool_obj: Any | None = None):
        timeout = self._timeout_seconds(tool_name, tool_obj)
        try:
            return await asyncio.wait_for(awaitable, timeout=timeout)
        except (
            asyncio.TimeoutError,
            TimeoutError,
        ) as exc:  # noqa: UP041 -- Python 3.10
            raise TimeoutError(
                f"Tool '{tool_name}' timed out after {timeout:.0f}s"
            ) from exc

    async def invoke(self, tool_name: str, args: dict[str, Any]) -> Any:
        async def call_tool() -> Any:
            value = await self._tool_gateway.invoke(tool_name, args)
            self.invoked_tools.append(tool_name)
            return value

        return await self._run_traced_tool_call(tool_name, args, call_tool)

    async def invoke_structured(
        self,
        tool_name: str,
        args: dict[str, Any],
    ) -> dict[str, Any]:
        async def call_tool() -> dict[str, Any]:
            tool_obj = await self._authorize(tool_name, args)
            await self._charge_tool_budget()
            self.invoked_tools.append(tool_name)
            if tool_name == "multi_kb_search":
                from backend.application.tools.providers.multi_kb_search_tool import (
                    execute_multi_kb_search_structured,
                )

                deep_retrieval = bool(
                    getattr(self._context, "deep_retrieval", False)
                )
                value = await invoke_service_call(
                    tool_obj,
                    lambda: execute_multi_kb_search_structured(
                        **dict(args),
                        kb_ids=list(self._context.kb_ids),
                        deep_retrieval=deep_retrieval,
                    ),
                    tool_name=tool_name,
                    cache_key_payload={
                        "scope": self._context.request_id,
                        "args": dict(args),
                        "kb_ids": list(self._context.kb_ids),
                        "deep_retrieval": deep_retrieval,
                    },
                    # The structured normalizer below owns compaction so that
                    # evidence and image metadata are not flattened first.
                    enforce_output_limit=False,
                )
                return normalize_tool_result(
                    value,
                    tool_name=tool_name,
                    max_output_chars=_metadata_int(tool_obj, "max_output_chars"),
                )
            value = await invoke_tool_with_timeout(
                tool_obj,
                args,
                tool_name=tool_name,
                cache_scope=self._context.request_id,
            )
            return normalize_tool_result(
                value,
                tool_name=tool_name,
                max_output_chars=_metadata_int(tool_obj, "max_output_chars"),
            )

        try:
            return await self._run_traced_tool_call(tool_name, args, call_tool)
        except Exception as exc:
            return normalize_tool_error(exc, tool_name=tool_name)

    async def _authorize(
        self,
        tool_name: str,
        args: dict[str, Any],
    ):
        return await self._tool_gateway.authorize(tool_name, args)

    @staticmethod
    async def _charge_tool_budget() -> None:
        """Charge direct orchestration calls that bypass graph middleware."""

        await charge_agent_tool_call()

    async def _run_traced_tool_call(self, tool_name: str, args: dict[str, Any], call):
        self._tool_call_sequence += 1
        tool_call_id = (
            f"{self._context.request_id}:tool:{self._tool_call_sequence}:{tool_name}"
        )
        started_at = utc_now_iso()
        started_monotonic = time.monotonic()
        tool_metadata = self._tool_trace_metadata(tool_name)
        try:
            result = await call()
        except Exception as exc:
            self._trace.record_tool_call(
                tool_call_id=tool_call_id,
                tool_name=tool_name,
                status="failed",
                started_at=started_at,
                ended_at=utc_now_iso(),
                elapsed_ms=round((time.monotonic() - started_monotonic) * 1000, 3),
                args_preview=_preview_mapping(args),
                error=str(exc)[:1000],
                metadata=tool_metadata,
            )
            raise
        self._trace.record_tool_call(
            tool_call_id=tool_call_id,
            tool_name=tool_name,
            status="completed",
            started_at=started_at,
            ended_at=utc_now_iso(),
            elapsed_ms=round((time.monotonic() - started_monotonic) * 1000, 3),
            args_preview=_preview_mapping(args),
            result_preview=_preview_value(result),
            metadata=tool_metadata,
        )
        return result

    def _tool_trace_metadata(self, tool_name: str) -> dict[str, Any]:
        metadata = getattr(self._tools.get(tool_name), "metadata", None)
        if not isinstance(metadata, dict):
            metadata = {}
        return {
            "risk_level": metadata.get("risk_level"),
            "policy_source": metadata.get("policy_source"),
            "side_effect": metadata.get("side_effect") or "unknown",
            "timeout_seconds": self._timeout_seconds(
                tool_name,
                self._tools.get(tool_name),
            ),
            "idempotent": bool(metadata.get("idempotent", True)),
            "parallel_safe": bool(metadata.get("parallel_safe", True)),
            "max_concurrency": metadata.get("max_concurrency"),
            "max_attempts": metadata.get("max_attempts", 1),
            "cache_enabled": bool(metadata.get("cache_enabled", False)),
        }


def parse_tool_json_result(value: Any) -> dict[str, Any]:
    if isinstance(value, dict):
        return dict(value)
    if isinstance(value, str):
        try:
            parsed = json.loads(value)
            if isinstance(parsed, dict):
                return parsed
        except Exception:
            logger.debug("工具返回值不是有效 JSON 对象，按文本处理", exc_info=True)
            return {"content": value}
    return {"content": value}


def _preview_mapping(value: dict[str, Any], *, max_chars: int = 500) -> dict[str, Any]:
    preview: dict[str, Any] = {}
    for key, item in dict(value or {}).items():
        text = _preview_value(item, max_chars=max_chars)
        preview[str(key)] = text
    return preview


def _preview_value(value: Any, *, max_chars: int = 1000) -> str:
    if isinstance(value, str):
        text = value
    else:
        try:
            text = json.dumps(value, ensure_ascii=False, sort_keys=True)
        except TypeError:
            text = str(value)
    if len(text) <= max_chars:
        return text
    return text[:max_chars] + "...[truncated]"


def _metadata_int(tool_obj: Any, key: str) -> int | None:
    metadata = getattr(tool_obj, "metadata", None)
    if not isinstance(metadata, dict):
        return None
    try:
        value = int(metadata.get(key))
    except (TypeError, ValueError):
        return None
    return value if value > 0 else None
