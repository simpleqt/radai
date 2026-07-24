"""Enforce the small, deterministic bootstrap phase for groundwater SQL."""

from __future__ import annotations

import json
import re
from collections.abc import Awaitable, Callable
from typing import Any

from langchain.agents import AgentState
from langchain.agents.middleware import AgentMiddleware
from langchain_core.messages import AIMessage, ToolMessage
from langgraph.prebuilt.tool_node import ToolCallRequest
from langgraph.types import Command
from typing_extensions import override


class GroundwaterProjectWorkflowMiddleware(AgentMiddleware[AgentState]):
    """Require project Skill and bootstrap before a free-form SQL tool call.

    Prompt rules are helpful but not sufficient: this middleware reads completed
    tool calls from graph state and returns a structured recovery instruction
    before an arbitrary table name reaches DuckDB.
    """

    _SQL_TOOLS = {"query_project_tables", "summarize_project_table"}
    _WATER_QUALITY_TABLES = re.compile(
        r"\b(water_quality_measurements|water_samples)\b",
        re.IGNORECASE,
    )

    @staticmethod
    def _tool_arguments_by_id(messages: list[Any]) -> dict[str, dict[str, Any]]:
        calls: dict[str, dict[str, Any]] = {}
        for message in messages:
            if not isinstance(message, AIMessage):
                continue
            for call in getattr(message, "tool_calls", None) or []:
                call_id = str(call.get("id") or "")
                args = call.get("args") or {}
                if call_id and isinstance(args, dict):
                    calls[call_id] = args
        return calls

    @classmethod
    def _completed_tools(cls, state: Any) -> list[tuple[str, dict[str, Any]]]:
        messages = list((state or {}).get("messages") or []) if isinstance(state, dict) else []
        args_by_id = cls._tool_arguments_by_id(messages)
        completed: list[tuple[str, dict[str, Any]]] = []
        for message in messages:
            if not isinstance(message, ToolMessage) or getattr(message, "status", None) == "error":
                continue
            content = str(message.content or "")
            try:
                payload = json.loads(content)
            except (TypeError, json.JSONDecodeError):
                payload = {}
            if isinstance(payload, dict) and payload.get("error"):
                continue
            completed.append(
                (
                    str(message.name or ""),
                    args_by_id.get(str(message.tool_call_id or ""), {}),
                )
            )
        return completed

    @classmethod
    def _successful_quality_check_points(cls, state: Any) -> set[str]:
        messages = list((state or {}).get("messages") or []) if isinstance(state, dict) else []
        points: set[str] = set()
        if isinstance(state, dict):
            for record in state.get("context_records") or []:
                if not isinstance(record, dict):
                    continue
                if str(record.get("source_tool") or "") != "groundwater_quality_check":
                    continue
                content = str(record.get("claim") or record.get("content") or "")
                for match in re.finditer(r"\bpoint_code\s*=\s*([A-Za-z0-9_-]+)", content):
                    points.add(match.group(1).strip().upper())
                for match in re.finditer(r'"point_code"\s*:\s*"([^"]+)"', content):
                    points.add(match.group(1).strip().upper())
        for message in messages:
            if (
                not isinstance(message, ToolMessage)
                or str(message.name or "") != "groundwater_quality_check"
                or getattr(message, "status", None) == "error"
            ):
                continue
            content = str(message.content or "")
            if content.startswith("[Context Isolation:"):
                # The result has already been elevated; do not block follow-up
                # based only on the placeholder because the point code is no
                # longer visible in the ToolMessage itself.
                continue
            try:
                payload = json.loads(content)
            except (TypeError, json.JSONDecodeError):
                continue
            if not isinstance(payload, dict) or payload.get("error"):
                continue
            point_code = str(payload.get("point_code") or "").strip().upper()
            if point_code and payload.get("standard_class") == "IV":
                points.add(point_code)
        return points

    @classmethod
    def _redundant_water_quality_query(cls, request: ToolCallRequest) -> ToolMessage | None:
        tool_name = str(request.tool_call.get("name") or "")
        if tool_name != "query_project_tables":
            return None
        args = request.tool_call.get("args") or {}
        sql = str(args.get("sql") or "")
        if not sql or not cls._WATER_QUALITY_TABLES.search(sql):
            return None
        checked_points = cls._successful_quality_check_points(getattr(request, "state", None))
        if not checked_points:
            return None
        upper_sql = sql.upper()
        matched = sorted(point for point in checked_points if point in upper_sql)
        if not matched:
            return None
        return ToolMessage(
            content=json.dumps(
                {
                    "error": "redundant_groundwater_quality_query",
                    "blocked_tool": tool_name,
                    "point_code": matched[0],
                    "message": (
                        "本轮已通过 groundwater_quality_check 获得该点位相对 IV 类水的"
                        "分层超标判断、限值、差值和倍数；请直接基于该工具结果回答，"
                        "不要重复查询完整水质明细。"
                    ),
                    "counts_toward_query_budget": False,
                },
                ensure_ascii=False,
            ),
            tool_call_id=str(request.tool_call.get("id") or "missing_tool_call_id"),
            name=tool_name,
            status="error",
        )

    @classmethod
    def _blocked_message(cls, request: ToolCallRequest) -> ToolMessage | None:
        tool_name = str(request.tool_call.get("name") or "")
        redundant = cls._redundant_water_quality_query(request)
        if redundant is not None:
            return redundant
        if tool_name not in cls._SQL_TOOLS:
            return None

        completed = cls._completed_tools(getattr(request, "state", None))
        state = getattr(request, "state", None)
        preloaded_project_skill = bool(
            (state or {}).get("project_skill")
            if isinstance(state, dict)
            else False
        )
        preloaded_bootstrap = bool(
            (state or {}).get("project_bootstrap")
            if isinstance(state, dict)
            else False
        )
        project_skill_loaded = any(
            name == "groundwater_project_skill_read"
            and str(args.get("resource") or "manifest").strip().casefold() == "skill"
            for name, args in completed
        ) or preloaded_project_skill
        bootstrap_loaded = (
            any(name == "groundwater_project_bootstrap" for name, _ in completed)
            or preloaded_bootstrap
        )
        if project_skill_loaded and bootstrap_loaded:
            return None

        missing_steps = []
        if not project_skill_loaded:
            missing_steps.append('groundwater_project_skill_read(resource="skill")')
        if not bootstrap_loaded:
            missing_steps.append("groundwater_project_bootstrap()")
        return ToolMessage(
            content=json.dumps(
                {
                    "error": "groundwater_project_workflow_not_ready",
                    "blocked_tool": tool_name,
                    "required_steps": missing_steps,
                    "message": "执行项目 SQL 前必须先加载项目 Skill 和项目 SQL 合同。",
                    "counts_toward_query_budget": False,
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
