"""MCP-facing tool catalog helpers."""

from __future__ import annotations

from dataclasses import dataclass, field
from typing import Any

from backend.application.agents.code_resolver import AgentCodeResolver
from backend.application.agents.profile_loader import AgentProfileLoader
from backend.application.tools.mcp_adapter import descriptors_for_policy
from backend.application.tools.registry import ToolRegistry
from backend.domain.agents.profile import AgentProfile


@dataclass(frozen=True)
class McpToolCatalogQuery:
    agent_code: str | None = None
    policy_name: str | None = None
    domain: str | None = None
    workflow: str | None = None
    selected_skills: set[str] = field(default_factory=set)


@dataclass(frozen=True)
class McpToolCatalog:
    agent_code: str | None
    policy_name: str
    domain: str
    workflow: str
    selected_skills: list[str]
    tools: list[dict[str, Any]]

    def to_dict(self) -> dict[str, Any]:
        return {
            "agent_code": self.agent_code,
            "policy_name": self.policy_name,
            "domain": self.domain,
            "workflow": self.workflow,
            "selected_skills": list(self.selected_skills),
            "tools": [dict(tool) for tool in self.tools],
        }


def build_mcp_tool_catalog(
    query: McpToolCatalogQuery | None = None,
    *,
    profile_loader: AgentProfileLoader | None = None,
    registry: ToolRegistry | None = None,
) -> McpToolCatalog:
    query = query or McpToolCatalogQuery()
    loader = profile_loader or AgentProfileLoader()
    profile = _load_profile(query.agent_code, loader)
    domain = _resolve_domain(query, profile)
    workflow = _resolve_workflow(query, profile)
    policy_name = _resolve_policy_name(query, profile)
    selected_skills = _resolve_selected_skills(query, profile)
    descriptors = descriptors_for_policy(
        registry=registry,
        policy_name=policy_name,
        domain=domain,
        workflow=workflow,
        selected_skills=selected_skills,
    )
    return McpToolCatalog(
        agent_code=profile.code if profile else None,
        policy_name=policy_name,
        domain=domain,
        workflow=workflow,
        selected_skills=sorted(selected_skills),
        tools=[descriptor.to_dict() for descriptor in descriptors],
    )


def _load_profile(
    agent_code: str | None,
    loader: AgentProfileLoader,
) -> AgentProfile | None:
    normalized = str(agent_code or "").strip()
    if not normalized:
        return None
    return loader.load(AgentCodeResolver.to_runtime(normalized))


def _resolve_domain(
    query: McpToolCatalogQuery,
    profile: AgentProfile | None,
) -> str:
    domain = str(query.domain or "").strip()
    if domain:
        return domain
    if profile and str(profile.domain or "").strip():
        return str(profile.domain).strip()
    raise ValueError("MCP tool catalog requires either agent_code or domain")


def _resolve_workflow(
    query: McpToolCatalogQuery,
    profile: AgentProfile | None,
) -> str:
    workflow = str(query.workflow or "").strip()
    if workflow:
        return workflow
    if profile and profile.default_workflow:
        return profile.default_workflow
    return "full"


def _resolve_policy_name(
    query: McpToolCatalogQuery,
    profile: AgentProfile | None,
) -> str:
    policy_name = str(query.policy_name or "").strip()
    if policy_name:
        return policy_name
    if profile and profile.tool_policy and profile.tool_policy != "default":
        return profile.tool_policy
    if profile:
        raise ValueError(
            f"Agent profile '{profile.code}' must declare an explicit "
            "registered tool_policy"
        )
    raise ValueError(
        "MCP tool catalog requires policy_name when no agent profile is supplied"
    )


def _resolve_selected_skills(
    query: McpToolCatalogQuery,
    profile: AgentProfile | None,
) -> set[str]:
    if query.selected_skills:
        return {skill for skill in query.selected_skills if skill}
    if profile:
        return {skill for skill in profile.default_skills if skill}
    return set()
