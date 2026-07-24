"""Require selected skill instructions to be loaded before business tools run."""

from __future__ import annotations

import json
import logging
from collections.abc import Awaitable, Callable

from langchain.agents import AgentState
from langchain.agents.middleware import AgentMiddleware
from langchain_core.messages import ToolMessage
from langgraph.prebuilt.tool_node import ToolCallRequest
from langgraph.types import Command
from typing_extensions import override  # noqa: UP035 -- runtime supports Python 3.10

from backend.core.skills.skill_runtime_state import has_loaded_skill

logger = logging.getLogger(__name__)


class SkillLoadGuardMiddleware(AgentMiddleware[AgentState]):
    """Block configured tools until their governing skill file is loaded."""

    def __init__(
        self,
        *,
        required_skill: str | None = None,
        required_skills: set[str] | None = None,
        required_skill_mode: str = "all",
        guarded_tools: set[str],
    ):
        normalized = {
            str(item).strip()
            for item in (set(required_skills or set()) | ({required_skill} if required_skill else set()))
            if str(item or "").strip()
        }
        if not normalized:
            raise ValueError("at least one required skill must be configured")
        mode = str(required_skill_mode or "all").strip().lower()
        if mode not in {"any", "all"}:
            raise ValueError("required_skill_mode must be 'any' or 'all'")
        self.required_skills = normalized
        self.required_skill_mode = mode
        self.required_skill = sorted(normalized)[0]
        self.guarded_tools = set(guarded_tools)

    @property
    def name(self) -> str:
        # LangChain create_agent requires unique middleware names; multiple
        # selected skills each install one guard instance.
        suffix = "+".join(sorted(self.required_skills))
        return f"SkillLoadGuard:{self.required_skill_mode}:{suffix}"

    def _blocked_message(self, request: ToolCallRequest) -> ToolMessage | None:
        tool_name = str(request.tool_call.get("name") or "unknown_tool")
        state = getattr(request, "state", None)
        graph_loaded_skills = (
            (state or {}).get("loaded_skills") or {}
            if isinstance(state, dict)
            else {}
        )
        loaded_skills = {
            skill_name
            for skill_name in self.required_skills
            if skill_name in graph_loaded_skills or has_loaded_skill(skill_name)
        }
        skill_loaded = (
            bool(loaded_skills)
            if self.required_skill_mode == "any"
            else loaded_skills == self.required_skills
        )
        if tool_name not in self.guarded_tools or skill_loaded:
            return None

        logger.warning(
            "[SkillLoadGuard] 工具调用被拦截: tool=%s required_skill=%s",
            tool_name,
            ",".join(sorted(self.required_skills)),
        )
        required_skills = sorted(self.required_skills)
        required_text = (
            "其中任一技能"
            if self.required_skill_mode == "any"
            else "全部技能"
        )
        return ToolMessage(
            content=json.dumps(
                {
                    "error": "skill_not_loaded",
                    "blocked_tool": tool_name,
                    "required_skill": self.required_skill if len(required_skills) == 1 else None,
                    "required_skills": required_skills,
                    "required_skill_mode": self.required_skill_mode,
                    "message": (
                        f"必须先加载{required_text}：{', '.join(required_skills)}，"
                        "再重试当前工具。"
                    ),
                },
                ensure_ascii=False,
            ),
            tool_call_id=str(request.tool_call.get("id") or "missing_tool_call_id"),
            name=tool_name,
            status="error",
        )

    @override
    def wrap_tool_call(
        self,
        request: ToolCallRequest,
        handler: Callable[[ToolCallRequest], ToolMessage | Command],
    ) -> ToolMessage | Command:
        blocked = self._blocked_message(request)
        return blocked if blocked is not None else handler(request)

    @override
    async def awrap_tool_call(
        self,
        request: ToolCallRequest,
        handler: Callable[[ToolCallRequest], Awaitable[ToolMessage | Command]],
    ) -> ToolMessage | Command:
        blocked = self._blocked_message(request)
        return blocked if blocked is not None else await handler(request)
