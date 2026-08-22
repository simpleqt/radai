"""MCP 目录与适配层契约（拆自 test_agent_platform_standardization.py）。"""

import inspect

from backend.application.mcp.config import McpServiceRegistry
from backend.application.mcp.query_service import build_mcp_agent_request
from backend.application.tools.mcp_adapter import descriptors_for_policy
from backend.domain.agents.events import AgentEvent
from types import SimpleNamespace


def test_mcp_service_config_builds_agent_request(tmp_path):
    registry_path = tmp_path / "mcp_registry.yaml"
    registry_path.write_text(
        """
services:
  default:
    name: Custom MCP
    instructions: Custom instructions
    agent_code: csh
    workflow: research
    user_id: configured-mcp
    use_search: false
    thinking_enabled: true
    tool_name: ask_custom_agent
    tool_description: Ask custom agent
""",
        encoding="utf-8",
    )

    spec = McpServiceRegistry(registry_path).get("default")
    request = build_mcp_agent_request(
        question="hello",
        user_id="runtime-user",
        spec=spec,
        source="test.mcp",
    )

    assert spec.tool_name == "ask_custom_agent"
    assert request.agent_code == "csh"
    assert request.user_id == "runtime-user"
    assert request.requested_workflow == "research"
    assert request.use_search is False
    assert request.thinking_enabled is True
    assert request.metadata == {"source": "test.mcp", "mcp_service": "Custom MCP"}


def test_mcp_api_layer_does_not_hardcode_domain_agent():
    import backend.api.http_server as http_server
    import backend.api.mcp_server as mcp_server
    import backend.api.routers.mcp as mcp_router
    import backend.application.mcp.fastmcp_app as fastmcp_app

    api_sources = "\n".join(
        [
            inspect.getsource(http_server),
            inspect.getsource(mcp_server),
            inspect.getsource(mcp_router),
            inspect.getsource(fastmcp_app),
        ]
    )

    assert 'agent_code="ne"' not in api_sources
    assert '"核电子学智能体"' not in api_sources
    assert "build_mcp_agent_request" in api_sources
    assert "list_available_tools" in api_sources


def test_mcp_tool_catalog_resolves_agent_policy_descriptors():
    from backend.application.mcp.tool_catalog import (
        McpToolCatalogQuery,
        build_mcp_tool_catalog,
    )

    catalog = build_mcp_tool_catalog(
        McpToolCatalogQuery(
            agent_code="ne",
            workflow="full",
            selected_skills={"academic-search"},
        )
    )
    tools = {tool["name"]: tool for tool in catalog.tools}

    assert catalog.agent_code == "ne"
    assert catalog.policy_name == "ne_default"
    assert catalog.domain == "nuclear-electronics"
    assert catalog.workflow == "full"
    assert "read_context_artifact" in tools
    assert tools["web_search"]["annotations"]["side_effect"] == "external_read"
    assert tools["academic_search"]["annotations"]["risk_level"] == "network"


async def test_mcp_tools_endpoint_returns_tool_catalog():
    from backend.api.routers.mcp import list_mcp_tools

    response = await list_mcp_tools(
        agent_code="ne",
        workflow="full",
        selected_skills="academic-search",
        auth=SimpleNamespace(api_key="test", user_id="user-1"),
    )

    tools = {tool["name"]: tool for tool in response.tools}
    assert response.policy_name == "ne_default"
    assert response.selected_skills == ["academic-search"]
    assert "read_context_artifact" in tools
    assert tools["academic_search"]["annotations"]["side_effect"] == "external_read"


def test_mcp_event_adapter_preserves_final_message_replacement():
    from backend.api.routers.mcp import _map_standard_event_to_mcp_edge

    mapped = _map_standard_event_to_mcp_edge(
        AgentEvent(
            type="message.replace",
            request_id="req-1",
            thread_id="thread-1",
            sequence=4,
            payload={"content": "corrected final answer"},
        )
    )

    assert mapped == {"type": "message.replace", "content": "corrected final answer"}


def test_mcp_like_descriptors_export_policy_tool_annotations():
    descriptors = descriptors_for_policy(
        policy_name="ne_default",
        domain="nuclear-electronics",
        workflow="full",
        selected_skills={"academic-search"},
    )

    by_name = {descriptor.name: descriptor for descriptor in descriptors}

    artifact = by_name["read_context_artifact"].to_dict()
    assert artifact["input_schema"]["type"] == "object"
    assert artifact["annotations"]["risk_level"] == "safe"
    assert artifact["annotations"]["timeout_seconds"] == 30.0
    assert artifact["annotations"]["policy_source"] == "registry:read_context_artifact"

    navigator = by_name["context_read"].to_dict()
    assert navigator["annotations"]["timeout_seconds"] == 30.0
    assert navigator["annotations"]["policy_source"] == "registry:context_read"

    web = by_name["web_search"]
    assert web.annotations["risk_level"] == "network"
    assert web.annotations["side_effect"] == "external_read"

    academic = by_name["academic_search"]
    assert academic.annotations["risk_level"] == "network"
    assert academic.annotations["side_effect"] == "external_read"
    assert academic.annotations["required_skills"] == [
        "academic-search",
        "paper-review",
    ]
