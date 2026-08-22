"""MCP-like descriptors for policy-managed tools.

This module does not start an external MCP server. It provides a stable
descriptor boundary so platform tools can be exposed, audited, or bridged to a
real MCP transport without coupling callers to LangChain tool objects.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from typing import Any

from langchain_core.tools import BaseTool

from backend.application.tools.assembler import shared_default_tool_registry
from backend.application.tools.registry import ToolRegistry
from backend.config.loggers import get_logger
from backend.domain.tools.spec import ToolSpec

logger = get_logger(__name__)


@dataclass(frozen=True)
class McpToolDescriptor:
    name: str
    description: str
    provider: str
    input_schema: dict[str, Any] = field(default_factory=dict)
    annotations: dict[str, Any] = field(default_factory=dict)

    def to_dict(self) -> dict[str, Any]:
        return {
            "name": self.name,
            "description": self.description,
            "provider": self.provider,
            "input_schema": dict(self.input_schema),
            "annotations": dict(self.annotations),
        }


def descriptor_from_spec(spec: ToolSpec) -> McpToolDescriptor:
    """Build a transport-neutral descriptor from registry metadata."""

    return McpToolDescriptor(
        name=spec.name,
        description=spec.description,
        provider=spec.provider,
        input_schema={"type": "object", "properties": {}},
        annotations=_annotations_from_spec(spec),
    )


def descriptor_from_tool(
    tool: BaseTool,
    spec: ToolSpec | None = None,
) -> McpToolDescriptor:
    """Build a descriptor from a materialized LangChain tool."""

    metadata = getattr(tool, "metadata", None)
    if not isinstance(metadata, dict):
        metadata = {}
    return McpToolDescriptor(
        name=str(getattr(tool, "name", "") or (spec.name if spec else "")),
        description=str(
            getattr(tool, "description", "") or (spec.description if spec else "")
        ),
        provider=spec.provider if spec else str(metadata.get("provider") or ""),
        input_schema=_input_schema_from_tool(tool),
        annotations=(
            _annotations_from_spec(spec, metadata=metadata)
            if spec
            else _annotations_from_metadata(metadata)
        ),
    )


def descriptors_for_policy(
    *,
    registry: ToolRegistry | None = None,
    policy_name: str,
    domain: str,
    workflow: str,
    selected_skills: set[str] | None = None,
) -> list[McpToolDescriptor]:
    """Resolve policy-visible tools as MCP-like descriptors."""

    tool_registry = registry or shared_default_tool_registry()
    specs = tool_registry.resolve_policy_tools(
        policy_name=policy_name,
        domain=domain,
        workflow=workflow,
        selected_skills=selected_skills or set(),
    )
    # Prefer the materialized LangChain schema when the provider can be
    # constructed without request scope.  Scope-bound factories (for example
    # chat-file search) still receive a safe registry descriptor instead of
    # being silently omitted from the catalog.
    try:
        from backend.application.tools.assembler import PolicyToolAssembler

        assembler = PolicyToolAssembler(registry=tool_registry)
    except Exception:
        logger.warning(
            "[McpAdapter] 工具组装器初始化失败，使用静态描述符",
            exc_info=True,
        )
        assembler = None
    descriptors: list[McpToolDescriptor] = []
    for spec in specs:
        descriptor: McpToolDescriptor | None = None
        if assembler is not None:
            try:
                tools = assembler.materialize_spec(spec)
                matching = next(
                    (item for item in tools if item.name == spec.name), None
                )
                if matching is not None:
                    descriptor = descriptor_from_tool(matching, spec)
            except Exception:
                logger.warning(
                    "[McpAdapter] 工具实例化失败，使用静态描述符: tool=%s",
                    spec.name,
                    exc_info=True,
                )
        descriptors.append(descriptor or descriptor_from_spec(spec))
    return descriptors


def _annotations_from_spec(
    spec: ToolSpec,
    *,
    metadata: dict[str, Any] | None = None,
) -> dict[str, Any]:
    metadata = metadata or {}
    annotations = {
        "risk_level": metadata.get("risk_level", spec.risk_level),
        "side_effect": metadata.get("side_effect", spec.side_effect),
        "timeout_seconds": metadata.get("timeout_seconds", spec.timeout_seconds),
        "policy_source": metadata.get("policy_source", spec.policy_source),
        "idempotent": metadata.get("idempotent", spec.idempotent),
        "parallel_safe": metadata.get("parallel_safe", spec.parallel_safe),
        "max_concurrency": metadata.get("max_concurrency", spec.max_concurrency),
        "max_attempts": metadata.get("max_attempts", spec.max_attempts),
        "retryable_errors": metadata.get(
            "retryable_errors", sorted(spec.retryable_errors)
        ),
        "max_output_chars": metadata.get("max_output_chars", spec.max_output_chars),
        "cache_enabled": metadata.get("cache_enabled", spec.cache_enabled),
        "cache_ttl_seconds": metadata.get("cache_ttl_seconds", spec.cache_ttl_seconds),
        "enabled_by_default": spec.enabled_by_default,
        "domains": sorted(spec.domains),
        "workflows": sorted(spec.workflows),
        "required_skills": sorted(spec.required_skills),
    }
    if spec.instructions or metadata.get("instructions"):
        annotations["instructions"] = metadata.get("instructions", spec.instructions)
    return annotations


def _annotations_from_metadata(metadata: dict[str, Any]) -> dict[str, Any]:
    keys = {
        "risk_level",
        "side_effect",
        "timeout_seconds",
        "policy_source",
        "instructions",
        "idempotent",
        "parallel_safe",
        "max_concurrency",
        "max_attempts",
        "retryable_errors",
        "max_output_chars",
        "cache_enabled",
        "cache_ttl_seconds",
    }
    return {key: metadata.get(key) for key in sorted(keys) if key in metadata}


def _input_schema_from_tool(tool: BaseTool) -> dict[str, Any]:
    args_schema = getattr(tool, "args_schema", None)
    if args_schema is not None:
        schema = _schema_from_model(args_schema)
        if schema:
            return schema
    args = getattr(tool, "args", None)
    if isinstance(args, dict):
        return {"type": "object", "properties": dict(args)}
    return {"type": "object", "properties": {}}


def _schema_from_model(model: Any) -> dict[str, Any]:
    try:
        if hasattr(model, "model_json_schema"):
            schema = model.model_json_schema()
        elif hasattr(model, "schema"):
            schema = model.schema()
        else:
            schema = {}
    except Exception:
        logger.warning(
            "[McpAdapter] 工具输入 schema 读取失败: model=%s",
            getattr(model, "__name__", type(model).__name__),
            exc_info=True,
        )
        schema = {}
    return schema if isinstance(schema, dict) else {}
