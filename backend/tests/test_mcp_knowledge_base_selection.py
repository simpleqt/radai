"""MCP knowledge-base name selection regressions."""

from __future__ import annotations

from contextlib import asynccontextmanager
from types import SimpleNamespace

import pytest

from backend.application.mcp.config import McpServiceSpec
from backend.application.mcp.query_service import (
    collect_mcp_visible_answer,
    normalize_mcp_knowledge_base_names,
    resolve_mcp_knowledge_base_ids,
)


def _spec() -> McpServiceSpec:
    return McpServiceSpec(
        name="test-mcp",
        instructions="",
        agent_code="ne",
        user_id="mcp",
    )


def test_normalize_mcp_knowledge_base_names_supports_single_and_multiple_names():
    assert normalize_mcp_knowledge_base_names(None) == []
    assert normalize_mcp_knowledge_base_names(" 核电子学知识库 ") == [
        "核电子学知识库"
    ]
    assert normalize_mcp_knowledge_base_names(
        ["核电子学期刊", "核电子学知识库", "核电子学期刊"]
    ) == ["核电子学期刊", "核电子学知识库"]


def test_normalize_mcp_knowledge_base_names_rejects_blank_names():
    with pytest.raises(ValueError, match="不能包含空名称"):
        normalize_mcp_knowledge_base_names([""])


@pytest.mark.asyncio
async def test_resolve_mcp_knowledge_base_ids_preserves_requested_name_order(
    monkeypatch,
):
    from backend.db import connection
    from backend.db.crud import agent_crud

    class Result:
        @staticmethod
        def all():
            return [
                SimpleNamespace(kb_id="kb-main", kb_name="核电子学知识库"),
                SimpleNamespace(kb_id="kb-journal", kb_name="核电子学期刊"),
            ]

    class Session:
        async def execute(self, _statement):
            return Result()

    @asynccontextmanager
    async def session_context():
        yield Session()

    async def bound_ids(_session, agent_code):
        assert agent_code == "ne"
        return ["kb-main", "kb-journal"]

    monkeypatch.setattr(connection, "read_only_session_context", session_context)
    monkeypatch.setattr(agent_crud, "get_runtime_agent_kb_ids", bound_ids)

    resolved = await resolve_mcp_knowledge_base_ids(
        ["核电子学期刊", "核电子学知识库"],
        spec=_spec(),
    )

    assert resolved == ["kb-journal", "kb-main"]


@pytest.mark.asyncio
async def test_resolve_mcp_knowledge_base_ids_rejects_unbound_name(monkeypatch):
    from backend.db import connection
    from backend.db.crud import agent_crud

    class Result:
        @staticmethod
        def all():
            return [SimpleNamespace(kb_id="kb-main", kb_name="核电子学知识库")]

    class Session:
        async def execute(self, _statement):
            return Result()

    @asynccontextmanager
    async def session_context():
        yield Session()

    async def bound_ids(_session, _agent_code):
        return ["kb-main"]

    monkeypatch.setattr(connection, "read_only_session_context", session_context)
    monkeypatch.setattr(agent_crud, "get_runtime_agent_kb_ids", bound_ids)

    with pytest.raises(ValueError, match="知识库未绑定或不可用"):
        await resolve_mcp_knowledge_base_ids(["其他知识库"], spec=_spec())


@pytest.mark.asyncio
async def test_collect_mcp_visible_answer_forwards_selected_ids(monkeypatch):
    from backend.application.mcp import query_service

    captured = {}

    async def resolve(names, *, spec):
        assert names == ["核电子学期刊"]
        assert spec.agent_code == "ne"
        return ["kb-journal"]

    async def collect(**kwargs):
        captured.update(kwargs)
        return "answer"

    monkeypatch.setattr(query_service, "resolve_mcp_knowledge_base_ids", resolve)
    monkeypatch.setattr(query_service, "collect_visible_answer_standard", collect)

    answer = await collect_mcp_visible_answer(
        "question",
        spec=_spec(),
        source="test",
        knowledge_bases="核电子学期刊",
    )

    assert answer == "answer"
    assert captured["selected_kb_ids"] == ["kb-journal"]
    assert captured["metadata"]["selected_knowledge_bases"] == ["核电子学期刊"]


@pytest.mark.asyncio
async def test_query_knowledge_base_schema_accepts_name_or_name_list():
    from backend.application.mcp.fastmcp_app import create_platform_mcp_app

    app = create_platform_mcp_app()
    list_tools = getattr(app, "list_tools", None)
    tools = await (list_tools() if list_tools else app._list_tools_mcp())
    query_tool = next(tool for tool in tools if tool.name == "query_knowledge_base")
    parameters = getattr(query_tool, "parameters", None) or query_tool.inputSchema
    selection_schema = parameters["properties"]["knowledge_bases"]
    accepted_types = {
        option.get("type") for option in selection_schema["anyOf"] if option.get("type")
    }

    assert accepted_types == {"string", "array", "null"}
    assert selection_schema["default"] is None
