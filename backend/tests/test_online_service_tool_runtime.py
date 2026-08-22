from __future__ import annotations

import json
from types import SimpleNamespace

import pytest
from langchain_core.messages import ToolMessage
from langchain_core.tools import tool

from backend.application.tools.assembler import (
    PolicyToolAssembler,
    ToolAssemblyContext,
    tool_spec_metadata,
)
from backend.application.tools.execution import invoke_tool_with_timeout
from backend.application.tools.mcp_adapter import descriptor_from_spec
from backend.application.tools.registry import ToolRegistry
from backend.application.tools.result import normalize_tool_error, normalize_tool_result
from backend.application.tools.runtime_state import tool_runtime_state
from backend.core.agents.middlewares.skill_load_guard import SkillLoadGuardMiddleware
from backend.core.agents.middlewares.tool_error_handling import (
    ToolErrorHandlingMiddleware,
)
from backend.core.skills.skill_runtime_state import (
    has_loaded_skill,
    reset_request_skill_state,
    set_request_skill_state,
)
from backend.core.tools.task_tool import _wrap_tool
from backend.domain.tools.spec import ToolSpec


def _spec(name: str = "service_tool", **kwargs) -> ToolSpec:
    return ToolSpec(
        name=name,
        provider="tests:provider",
        domains={"general"},
        workflows={"full"},
        **kwargs,
    )


@pytest.mark.asyncio
async def test_mcp_tool_call_uses_resolved_workflow_name(monkeypatch):
    from backend.application.mcp import query_service, tool_catalog
    from backend.application.mcp.config import McpServiceSpec

    workflow_object = object()
    context = SimpleNamespace(workflow_name="full")
    captured: dict[str, object] = {}

    class FakeAgentService:
        def __init__(self, **_kwargs):
            pass

        async def resolve_request_skills(self, request):
            return request

        async def resolve_workflow(self, _request):
            return workflow_object, context

    class FakeRuntimeToolInvoker:
        def __init__(
            self,
            *,
            context,
            workflow_name,
            selected_skills,
            resolved_agent_definition=None,
        ):
            captured.update(
                context=context,
                workflow_name=workflow_name,
                selected_skills=selected_skills,
                resolved_agent_definition=resolved_agent_definition,
            )

        async def invoke(self, tool_name, arguments):
            return {"tool_name": tool_name, "arguments": arguments}

    monkeypatch.setattr(query_service, "AgentService", FakeAgentService)
    monkeypatch.setattr(query_service, "RuntimeToolInvoker", FakeRuntimeToolInvoker)
    monkeypatch.setattr(
        tool_catalog,
        "build_mcp_tool_catalog",
        lambda _query: SimpleNamespace(tools=[{"name": "service_tool"}]),
    )

    result = await query_service.invoke_mcp_tool(
        "service_tool",
        {"query": "test"},
        user_id="user-1",
        spec=McpServiceSpec(
            name="test",
            instructions="",
            agent_code="ne",
            workflow="full",
        ),
    )

    assert result == {"tool_name": "service_tool", "arguments": {"query": "test"}}
    assert captured["context"] is context
    assert captured["workflow_name"] == "full"
    assert captured["workflow_name"] is not workflow_object


def test_online_tool_metadata_has_runtime_policy_without_approval_or_sandbox():
    spec = _spec(
        timeout_seconds=12,
        idempotent=True,
        parallel_safe=True,
        max_concurrency=3,
        max_attempts=2,
        retryable_errors={"timeout", "connection"},
        max_output_chars=5000,
        cache_enabled=True,
        cache_ttl_seconds=60,
    )

    metadata = tool_spec_metadata(spec)
    descriptor = descriptor_from_spec(spec).to_dict()

    assert "approval_required" not in metadata
    assert "approval_required" not in descriptor["annotations"]
    assert metadata["max_concurrency"] == 3
    assert metadata["max_attempts"] == 2
    assert descriptor["annotations"]["cache_ttl_seconds"] == 60


def test_skill_guard_honors_any_and_all_modes_per_tool():
    request = SimpleNamespace(
        tool_call={"id": "call-1", "name": "query_excel_data", "args": {}},
        state={"loaded_skills": {"general-table-analysis": "loaded"}},
    )
    any_guard = SkillLoadGuardMiddleware(
        required_skills={"general-table-analysis", "excel-experiment-analysis"},
        required_skill_mode="any",
        guarded_tools={"query_excel_data"},
    )
    all_guard = SkillLoadGuardMiddleware(
        required_skills={"general-table-analysis", "excel-experiment-analysis"},
        required_skill_mode="all",
        guarded_tools={"query_excel_data"},
    )

    assert any_guard.wrap_tool_call(request, lambda _request: "executed") == "executed"
    blocked = all_guard.wrap_tool_call(request, lambda _request: "executed")
    payload = json.loads(blocked.content)
    assert payload["required_skill_mode"] == "all"
    assert payload["required_skills"] == [
        "excel-experiment-analysis",
        "general-table-analysis",
    ]


def test_skill_guard_syncs_persisted_graph_state_to_request_runtime():
    skill_name = "csh-hydrogeology"
    request = SimpleNamespace(
        tool_call={"id": "call-kb", "name": "multi_kb_search", "args": {}},
        state={"loaded_skills": {skill_name: "# loaded in a previous turn"}},
    )
    guard = SkillLoadGuardMiddleware(
        required_skill=skill_name,
        guarded_tools={"multi_kb_search"},
    )
    tokens = set_request_skill_state({skill_name})
    try:
        assert not has_loaded_skill(skill_name)

        result = guard.wrap_tool_call(
            request,
            lambda _request: (
                "executed" if has_loaded_skill(skill_name) else "not-synchronized"
            ),
        )

        assert result == "executed"
        assert has_loaded_skill(skill_name)
    finally:
        reset_request_skill_state(tokens)


def test_registry_parses_service_booleans_strictly():
    registry = object.__new__(ToolRegistry)
    spec = registry._parse_spec(
        "strict_tool",
        {
            "provider": "tests:provider",
            "enabled_by_default": "false",
            "idempotent": "false",
            "parallel_safe": "true",
            "cache_enabled": "false",
        },
    )

    assert spec.enabled_by_default is False
    assert spec.idempotent is False
    assert spec.parallel_safe is True
    assert spec.cache_enabled is False

    with pytest.raises(ValueError, match="Invalid idempotent"):
        registry._parse_spec(
            "invalid_tool",
            {"provider": "tests:provider", "idempotent": "sometimes"},
        )
    with pytest.raises(ValueError, match="Invalid max_concurrency"):
        registry._parse_spec(
            "invalid_concurrency",
            {"provider": "tests:provider", "max_concurrency": 1.5},
        )


def test_normalize_tool_result_preserves_evidence_compatibility_and_truncates():
    result = normalize_tool_result(
        {
            "text": "abcdefghij" * 20,
            "data": {"raw": "x" * 5000},
            "evidence_records": [{"evidence_id": "ev-1"}],
            "stats": {"cache_hit": True},
        },
        tool_name="search_evidence",
        max_output_chars=1200,
    )

    assert result["status"] == "success"
    assert result["tool_name"] == "search_evidence"
    assert result["truncated"] is True
    assert result["evidence"] == result["evidence_records"]
    assert result["metrics"] == result["stats"] == {"cache_hit": True}
    assert len(json.dumps(result, ensure_ascii=False, separators=(",", ":"))) <= 1200


def test_normalize_tool_error_returns_stable_service_error():
    result = normalize_tool_error(
        ConnectionError("upstream unavailable"), tool_name="web_search"
    )

    assert result["status"] == "error"
    assert result["error"]["code"] == "connection"
    assert result["error"]["retryable"] is True


def test_normalize_tool_result_marks_reported_error_as_error():
    result = normalize_tool_result(
        {"error": "unknown_project_table", "message": "missing table"},
        tool_name="query_project_tables",
    )

    assert result["status"] == "error"
    assert result["error"] == "unknown_project_table"


@pytest.mark.asyncio
async def test_idempotent_service_tool_retries_configured_transient_error():
    tool_runtime_state.reset()

    class FlakyTool:
        name = "flaky"
        metadata = {
            "timeout_seconds": 1,
            "idempotent": True,
            "max_attempts": 2,
            "retryable_errors": ["connection"],
            "max_concurrency": 2,
        }

        def __init__(self):
            self.calls = 0

        async def ainvoke(self, args):
            self.calls += 1
            if self.calls == 1:
                raise ConnectionError("temporary")
            return {"ok": args["value"]}

    service_tool = FlakyTool()
    result = await invoke_tool_with_timeout(service_tool, {"value": 7})

    assert result == {"ok": 7}
    assert service_tool.calls == 2
    observed = tool_runtime_state.snapshot()["tools"][0]
    assert observed["name"] == "flaky"
    assert observed["health"]["status"] == "ready"
    assert observed["metrics"]["successes"] == 1
    assert observed["metrics"]["retries"] == 1


@pytest.mark.asyncio
async def test_reported_json_error_uses_retry_and_health_policy():
    tool_runtime_state.reset()

    class ReportedErrorTool:
        name = "reported_error"
        metadata = {
            "timeout_seconds": 1,
            "idempotent": True,
            "max_attempts": 2,
            "retryable_errors": ["connection"],
        }

        def __init__(self):
            self.calls = 0

        async def ainvoke(self, _args):
            self.calls += 1
            if self.calls == 1:
                return json.dumps(
                    {"status": "error", "error": {"code": "connection", "message": "temporary"}}
                )
            return json.dumps({"status": "success", "data": {"ok": True}})

    service_tool = ReportedErrorTool()
    result = await invoke_tool_with_timeout(service_tool, {})

    assert json.loads(result)["data"] == {"ok": True}
    assert service_tool.calls == 2
    observed = tool_runtime_state.snapshot()["tools"][0]
    assert observed["metrics"]["retries"] == 1


@pytest.mark.asyncio
async def test_service_tool_cache_is_scoped_to_materialized_tool_instance():
    class CachedTool:
        name = "cached"
        metadata = {
            "timeout_seconds": 1,
            "cache_enabled": True,
            "cache_ttl_seconds": 60,
        }

        def __init__(self):
            self.calls = 0

        async def ainvoke(self, args):
            self.calls += 1
            return {"value": args["value"], "call": self.calls}

    first = CachedTool()
    second = CachedTool()

    assert await invoke_tool_with_timeout(first, {"value": 3}) == {"value": 3, "call": 1}
    assert await invoke_tool_with_timeout(first, {"value": 3}) == {"value": 3, "call": 1}
    assert await invoke_tool_with_timeout(second, {"value": 3}) == {"value": 3, "call": 1}
    assert first.calls == 1
    assert second.calls == 1


@pytest.mark.asyncio
async def test_service_tool_applies_max_output_chars_to_raw_invocation():
    class LargeTool:
        name = "large"
        metadata = {"timeout_seconds": 1, "max_output_chars": 64}

        async def ainvoke(self, _args):
            return "x" * 500

    result = await invoke_tool_with_timeout(LargeTool(), {})

    assert len(result) == 64
    assert result.endswith("...[truncated]")


@pytest.mark.asyncio
async def test_service_tool_keeps_truncated_json_valid():
    class LargeJsonTool:
        name = "large_json"
        metadata = {"timeout_seconds": 1, "max_output_chars": 160}

        async def ainvoke(self, _args):
            return json.dumps(
                {"status": "success", "data": {"content": "证据" * 500}},
                ensure_ascii=False,
            )

    result = await invoke_tool_with_timeout(LargeJsonTool(), {})
    parsed = json.loads(result)

    assert len(result) <= 160
    assert parsed["status"] == "success"
    assert parsed["truncated"] is True


@pytest.mark.asyncio
async def test_graph_tool_error_middleware_applies_service_retry_contract():
    calls = 0

    @tool("graph_service")
    async def graph_service() -> str:
        """Graph service used for middleware policy binding."""
        return "unused"

    graph_service.metadata = {
        "timeout_seconds": 1,
        "max_attempts": 2,
        "retryable_errors": ["connection"],
    }
    middleware = ToolErrorHandlingMiddleware()
    middleware.bind_tools([graph_service])
    request = SimpleNamespace(
        tool_call={"id": "call-graph", "name": "graph_service", "args": {}}
    )

    async def handler(_request):
        nonlocal calls
        calls += 1
        content = (
            json.dumps({"error": {"code": "connection", "message": "temporary"}})
            if calls == 1
            else json.dumps({"status": "success", "data": {"ok": True}})
        )
        return ToolMessage(
            content=content,
            tool_call_id="call-graph",
            name="graph_service",
        )

    result = await middleware.awrap_tool_call(request, handler)

    assert result.status == "success"
    assert calls == 2


@pytest.mark.asyncio
async def test_graph_cached_tool_message_is_rebound_to_current_call_id():
    calls = 0

    @tool("cached_graph_service")
    async def cached_graph_service() -> str:
        """Graph service used for cache identity verification."""
        return "unused"

    cached_graph_service.metadata = {
        "timeout_seconds": 1,
        "cache_enabled": True,
        "cache_ttl_seconds": 60,
    }
    middleware = ToolErrorHandlingMiddleware()
    middleware.bind_tools([cached_graph_service])
    runtime = SimpleNamespace(
        config={"configurable": {"request_id": "request-1"}}
    )

    async def handler(request):
        nonlocal calls
        calls += 1
        return ToolMessage(
            content=json.dumps({"status": "success", "call": calls}),
            tool_call_id=request.tool_call["id"],
            name="cached_graph_service",
        )

    first_request = SimpleNamespace(
        tool_call={"id": "call-1", "name": "cached_graph_service", "args": {}},
        tool=cached_graph_service,
        runtime=runtime,
    )
    second_request = SimpleNamespace(
        tool_call={"id": "call-2", "name": "cached_graph_service", "args": {}},
        tool=cached_graph_service,
        runtime=runtime,
    )

    first = await middleware.awrap_tool_call(first_request, handler)
    second = await middleware.awrap_tool_call(second_request, handler)

    assert first.tool_call_id == "call-1"
    assert second.tool_call_id == "call-2"
    assert calls == 1


@pytest.mark.asyncio
async def test_subagent_required_tool_tracking_excludes_reported_errors():
    @tool("required_service")
    async def required_service() -> str:
        """Service that reports failure in its payload."""
        return json.dumps(
            {"status": "error", "error": {"code": "connection", "message": "down"}}
        )

    invoked_tools: set[str] = set()
    wrapped = _wrap_tool(
        required_service,
        writer=lambda _event: None,
        task_id="task-1",
        subagent_type="test-agent",
        description="test",
        trace_id="trace-1",
        invoked_tools=invoked_tools,
    )

    with pytest.raises(RuntimeError, match="down"):
        await wrapped.ainvoke({})
    assert invoked_tools == set()


@pytest.mark.asyncio
async def test_non_idempotent_service_tool_never_retries():
    class WriteTool:
        name = "write_artifact"
        metadata = {
            "timeout_seconds": 1,
            "idempotent": False,
            "max_attempts": 3,
            "retryable_errors": ["connection"],
        }

        def __init__(self):
            self.calls = 0

        async def ainvoke(self, args):
            self.calls += 1
            raise ConnectionError("failed after write may have started")

    service_tool = WriteTool()
    with pytest.raises(ConnectionError):
        await invoke_tool_with_timeout(service_tool, {})
    assert service_tool.calls == 1


def test_policy_materialization_isolates_unavailable_optional_tool():
    tool_runtime_state.reset()

    @tool("ready_tool")
    def ready_tool() -> str:
        """A ready online service tool."""
        return "ok"

    class Registry:
        def resolve_policy_tools(self, **kwargs):
            return [_spec("missing_tool"), _spec("ready_tool")]

    class Loader:
        def load(self, spec):
            if spec.name == "missing_tool":
                raise ModuleNotFoundError("optional_service_sdk")
            return ready_tool

    assembler = PolicyToolAssembler(registry=Registry(), provider_loader=Loader())
    tools = assembler.materialize_policy_tools(
        ToolAssemblyContext(
            domain="general",
            workflow="full",
            policy_name="default",
        )
    )

    assert set(tools) == {"ready_tool"}
    health = {item.name: item for item in assembler.health_report()}
    assert health["ready_tool"].status == "ready"
    assert health["missing_tool"].status == "unavailable"
    assert health["missing_tool"].reason == "missing_dependency"
    observed = {item["name"]: item for item in tool_runtime_state.snapshot()["tools"]}
    assert observed["ready_tool"]["health"]["status"] == "ready"
    assert observed["missing_tool"]["health"]["reason"] == "missing_dependency"
    assert observed["missing_tool"]["health"]["detail"] == "ModuleNotFoundError"
    assert "optional_service_sdk" not in observed["missing_tool"]["health"]["detail"]
