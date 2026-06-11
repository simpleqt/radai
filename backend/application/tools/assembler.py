"""Policy-driven tool assembly helpers."""

from __future__ import annotations

from dataclasses import dataclass, field
from typing import Any

from langchain_core.tools import BaseTool

from backend.application.tools.provider_loader import ToolProviderLoader
from backend.application.tools.registry import ToolRegistry
from backend.domain.tools.spec import ToolSpec
from backend.domain.tools.runtime_context import ToolRuntimeContext


@dataclass(frozen=True)
class ToolAssemblyContext:
    domain: str
    workflow: str
    policy_name: str
    selected_skills: set[str] = field(default_factory=set)


@dataclass(frozen=True)
class ToolPolicyAudit:
    policy_name: str
    domain: str
    workflow: str
    expected: set[str]
    present: set[str]
    missing: set[str]
    unexpected: set[str]


class PolicyToolAssembler:
    """Resolve and materialize tools from ToolPolicy/ToolSpec contracts."""

    def __init__(
        self,
        *,
        registry: ToolRegistry | None = None,
        provider_loader: ToolProviderLoader | None = None,
    ):
        self._registry = registry or ToolRegistry()
        self._provider_loader = provider_loader or ToolProviderLoader()

    def resolve_specs(self, context: ToolAssemblyContext) -> list[ToolSpec]:
        return self._registry.resolve_policy_tools(
            policy_name=context.policy_name,
            domain=context.domain,
            workflow=context.workflow,
            selected_skills=context.selected_skills,
        )

    def materialize_tool_name(
        self,
        tool_name: str,
        *,
        runtime_context: ToolRuntimeContext | None = None,
    ) -> list[BaseTool]:
        return self.materialize_spec(
            self._registry.get(tool_name),
            runtime_context=runtime_context,
        )

    def materialize_spec(
        self,
        spec: ToolSpec,
        *,
        runtime_context: ToolRuntimeContext | None = None,
    ) -> list[BaseTool]:
        provider = self._provider_loader.load(spec)
        if callable(provider) and not isinstance(provider, BaseTool):
            raw_tools = self._provider_loader.invoke(
                provider,
                runtime_context=runtime_context,
            )
        else:
            raw_tools = provider
        if isinstance(raw_tools, BaseTool):
            return [raw_tools]
        if isinstance(raw_tools, list):
            return [tool_item for tool_item in raw_tools if isinstance(tool_item, BaseTool)]
        return []

    def materialize_policy_tools(
        self,
        context: ToolAssemblyContext,
        *,
        skip_tool_names: set[str] | None = None,
        runtime_context: ToolRuntimeContext | None = None,
    ) -> dict[str, BaseTool]:
        skip_tool_names = skip_tool_names or set()
        tools_by_name: dict[str, BaseTool] = {}
        for spec in self.resolve_specs(context):
            if spec.name in skip_tool_names:
                continue
            for tool_item in self.materialize_spec(
                spec,
                runtime_context=runtime_context,
            ):
                tools_by_name[tool_item.name] = tool_item
        return tools_by_name

    def audit(
        self,
        context: ToolAssemblyContext,
        *,
        actual_tool_names: set[str],
    ) -> ToolPolicyAudit:
        expected = {spec.name for spec in self.resolve_specs(context)}
        return ToolPolicyAudit(
            policy_name=context.policy_name,
            domain=context.domain,
            workflow=context.workflow,
            expected=expected,
            present=expected & actual_tool_names,
            missing=expected - actual_tool_names,
            unexpected=actual_tool_names - expected,
        )
