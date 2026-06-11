"""MCP query execution helpers."""

from __future__ import annotations

from backend.application.agent_runtime.visible_answer import collect_visible_answer_standard
from backend.application.mcp.config import McpServiceSpec, get_default_mcp_service_spec
from backend.domain.agents.request import AgentRequest


async def collect_mcp_visible_answer(
    query: str,
    *,
    spec: McpServiceSpec | None = None,
    source: str,
) -> str:
    spec = spec or get_default_mcp_service_spec()
    return await collect_visible_answer_standard(
        question=query,
        agent_code=spec.agent_code,
        user_id=spec.user_id,
        workflow=spec.workflow,
        use_search=spec.use_search,
        thinking_enabled=spec.thinking_enabled,
        metadata={"source": source, "mcp_service": spec.name},
    )


def build_mcp_agent_request(
    *,
    question: str,
    user_id: str,
    spec: McpServiceSpec | None = None,
    source: str,
) -> AgentRequest:
    spec = spec or get_default_mcp_service_spec()
    return AgentRequest(
        question=question,
        agent_code=spec.agent_code,
        user_id=user_id,
        requested_workflow=spec.workflow,
        use_search=spec.use_search,
        thinking_enabled=spec.thinking_enabled,
        metadata={"source": source, "mcp_service": spec.name},
    )
