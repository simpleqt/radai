"""YAML-backed MCP service configuration."""

from __future__ import annotations

from dataclasses import dataclass
from pathlib import Path
from typing import Any

import yaml


@dataclass(frozen=True)
class McpServiceSpec:
    name: str
    instructions: str
    agent_code: str
    workflow: str | None = "full"
    user_id: str = "mcp"
    use_search: bool = True
    thinking_enabled: bool = False
    tool_name: str = "query_knowledge_base"
    tool_description: str = "查询配置绑定智能体的知识库并返回回答"


class McpServiceRegistry:
    def __init__(self, config_path: Path | None = None):
        self._config_path = config_path or (
            Path(__file__).resolve().parents[2] / "config" / "mcp" / "registry.yaml"
        )
        self._specs = self._load_specs()

    def get(self, name: str = "default") -> McpServiceSpec:
        try:
            return self._specs[name]
        except KeyError as exc:
            raise ValueError(f"Unknown MCP service config: {name}") from exc

    def _load_specs(self) -> dict[str, McpServiceSpec]:
        if not self._config_path.exists():
            raise FileNotFoundError(f"MCP registry config not found: {self._config_path}")
        raw = yaml.safe_load(self._config_path.read_text(encoding="utf-8")) or {}
        services = raw.get("services") if isinstance(raw, dict) else None
        if not isinstance(services, dict):
            raise ValueError(f"MCP registry must define a services mapping: {self._config_path}")

        specs: dict[str, McpServiceSpec] = {}
        for name, value in services.items():
            if not isinstance(value, dict):
                continue
            service_name = str(name or "").strip()
            agent_code = str(value.get("agent_code") or "").strip()
            if not service_name or not agent_code:
                continue
            specs[service_name] = McpServiceSpec(
                name=str(value.get("name") or service_name),
                instructions=str(value.get("instructions") or ""),
                agent_code=agent_code,
                workflow=self._optional_str(value.get("workflow")),
                user_id=str(value.get("user_id") or "mcp"),
                use_search=bool(value.get("use_search", True)),
                thinking_enabled=bool(value.get("thinking_enabled", False)),
                tool_name=str(value.get("tool_name") or "query_knowledge_base"),
                tool_description=str(
                    value.get("tool_description") or "查询配置绑定智能体的知识库并返回回答"
                ),
            )
        if not specs:
            raise ValueError(f"MCP registry contains no valid services: {self._config_path}")
        return specs

    def _optional_str(self, value: Any) -> str | None:
        normalized = str(value or "").strip()
        return normalized or None


def get_default_mcp_service_spec() -> McpServiceSpec:
    return McpServiceRegistry().get("default")
