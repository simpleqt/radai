"""Middleware to fix dangling tool calls in message history."""

import json
import logging
from collections.abc import Awaitable, Callable
from typing import Any
from typing_extensions import override

from langchain.agents import AgentState
from langchain.agents.middleware import AgentMiddleware
from langchain.agents.middleware.types import ModelCallResult, ModelRequest, ModelResponse
from langchain_core.messages import AIMessage, ToolMessage
from langgraph.runtime import Runtime

from backend.core.skills.native_skill_tools import normalize_skill_tool_name

logger = logging.getLogger(__name__)

SKILL_NAME_TOOL_ARGS = {
    "read_local_skill_file",
    "read_skill_reference",
}

HTML_PPT_IDENTIFIER_ARGS = {
    "template",
    "theme",
}


class DanglingToolCallMiddleware(AgentMiddleware[AgentState]):
    """Inserts placeholder ToolMessages for dangling tool calls before model invocation."""

    def _normalize_tool_args(self, tool_name: str, args: Any) -> Any:
        if isinstance(args, dict):
            normalized = dict(args)
            if tool_name in SKILL_NAME_TOOL_ARGS and "skill_name" in normalized:
                normalized["skill_name"] = normalize_skill_tool_name(normalized.get("skill_name"))
            if tool_name == "generate_html_ppt":
                for key in HTML_PPT_IDENTIFIER_ARGS:
                    if key in normalized:
                        normalized[key] = normalize_skill_tool_name(normalized.get(key))
            return normalized
        if tool_name not in SKILL_NAME_TOOL_ARGS:
            return args
        if isinstance(args, str):
            try:
                decoded = json.loads(args)
                if isinstance(decoded, dict):
                    return self._normalize_tool_args(tool_name, decoded)
            except Exception:
                pass
            return {"skill_name": normalize_skill_tool_name(args)}
        return args

    def _sanitize_ai_message(self, msg: AIMessage) -> AIMessage:
        tool_calls = getattr(msg, "tool_calls", None) or []
        if not tool_calls:
            return msg

        changed = False
        sanitized_tool_calls: list[dict[str, Any]] = []
        for tc in tool_calls:
            if not isinstance(tc, dict):
                sanitized_tool_calls.append(tc)
                continue
            sanitized = dict(tc)
            tool_name = str(sanitized.get("name") or "")
            old_args = sanitized.get("args")
            new_args = self._normalize_tool_args(tool_name, old_args)
            if new_args != old_args:
                sanitized["args"] = new_args
                changed = True
            sanitized_tool_calls.append(sanitized)

        additional_kwargs = dict(getattr(msg, "additional_kwargs", {}) or {})
        for key in ("tool_calls", "invalid_tool_calls"):
            if key in additional_kwargs:
                additional_kwargs.pop(key, None)
                changed = True

        if not changed:
            return msg

        return msg.model_copy(
            update={
                "tool_calls": sanitized_tool_calls,
                "invalid_tool_calls": [],
                "additional_kwargs": additional_kwargs,
            }
        )

    def _sanitize_messages(self, messages: list) -> tuple[list, bool]:
        sanitized: list = []
        changed = False
        for msg in messages:
            if isinstance(msg, AIMessage):
                new_msg = self._sanitize_ai_message(msg)
                sanitized.append(new_msg)
                changed = changed or new_msg is not msg
            else:
                sanitized.append(msg)
        return sanitized, changed

    def _build_patched_messages(self, messages: list) -> list | None:
        messages, sanitized = self._sanitize_messages(messages)
        existing_tool_msg_ids: set[str] = set()
        for msg in messages:
            if isinstance(msg, ToolMessage):
                existing_tool_msg_ids.add(msg.tool_call_id)

        needs_patch = False
        for msg in messages:
            if getattr(msg, "type", None) != "ai":
                continue
            for tc in getattr(msg, "tool_calls", None) or []:
                tc_id = tc.get("id")
                if tc_id and tc_id not in existing_tool_msg_ids:
                    needs_patch = True
                    break
            if needs_patch:
                break

        if not needs_patch:
            if sanitized:
                return messages
            return None

        patched: list = []
        patched_ids: set[str] = set()
        patch_count = 0
        for msg in messages:
            patched.append(msg)
            if getattr(msg, "type", None) != "ai":
                continue
            for tc in getattr(msg, "tool_calls", None) or []:
                tc_id = tc.get("id")
                if tc_id and tc_id not in existing_tool_msg_ids and tc_id not in patched_ids:
                    patched.append(
                        ToolMessage(
                            content="[Tool call was interrupted and did not return a result.]",
                            tool_call_id=tc_id,
                            name=tc.get("name", "unknown"),
                            status="error",
                        )
                    )
                    patched_ids.add(tc_id)
                    patch_count += 1

        logger.info(f"Injected {patch_count} placeholder ToolMessage(s) for dangling tool calls")
        return patched

    def _patch_state_messages(self, state: AgentState) -> None:
        messages = state.get("messages") or []
        patched = self._build_patched_messages(messages)
        if patched is None:
            return
        state["messages"] = patched

    @override
    def before_model(self, state: AgentState, runtime: Runtime) -> dict | None:
        self._patch_state_messages(state)
        return None

    @override
    async def abefore_model(self, state: AgentState, runtime: Runtime) -> dict | None:
        self._patch_state_messages(state)
        return None

    @override
    def wrap_model_call(
        self,
        request: ModelRequest,
        handler: Callable[[ModelRequest], ModelResponse],
    ) -> ModelCallResult:
        patched = self._build_patched_messages(request.messages)
        if patched is not None:
            request = request.override(messages=patched)
        return handler(request)

    @override
    async def awrap_model_call(
        self,
        request: ModelRequest,
        handler: Callable[[ModelRequest], Awaitable[ModelResponse]],
    ) -> ModelCallResult:
        patched = self._build_patched_messages(request.messages)
        if patched is not None:
            request = request.override(messages=patched)
        return await handler(request)
