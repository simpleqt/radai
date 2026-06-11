"""YAML-backed tool registry and policy resolver."""

from __future__ import annotations

from pathlib import Path
from typing import Any

import yaml

from backend.domain.tools.policy import ToolPolicy
from backend.domain.tools.spec import ToolSpec


class ToolRegistry:
    def __init__(self, config_dir: Path | None = None):
        self._config_dir = config_dir or Path(__file__).resolve().parents[2] / "config" / "tools"
        self._specs = self._load_specs()
        self._policies = self._load_policies()

    def get(self, name: str) -> ToolSpec:
        try:
            return self._specs[name]
        except KeyError as exc:
            raise ValueError(f"Unknown tool: {name}") from exc

    def list_specs(self) -> list[ToolSpec]:
        return [self._specs[name] for name in sorted(self._specs)]

    def get_policy(self, name: str) -> ToolPolicy:
        try:
            return self._policies[name]
        except KeyError as exc:
            raise ValueError(f"Unknown tool policy: {name}") from exc

    def resolve_policy_tools(
        self,
        *,
        policy_name: str,
        domain: str,
        workflow: str,
        selected_skills: set[str],
    ) -> list[ToolSpec]:
        policy = self.get_policy(policy_name)
        names = policy.allowed_tool_names(selected_skills=selected_skills)
        specs: list[ToolSpec] = []
        for name in sorted(names):
            spec = self.get(name)
            if spec.supports(domain=domain, workflow=workflow, selected_skills=selected_skills):
                specs.append(spec)
        return specs

    def _load_specs(self) -> dict[str, ToolSpec]:
        path = self._config_dir / "registry.yaml"
        raw = self._load_yaml_mapping(path)
        tools = raw.get("tools") or {}
        if not isinstance(tools, dict):
            raise ValueError(f"tools must be a mapping: {path}")
        return {
            name: self._parse_spec(name, value)
            for name, value in tools.items()
            if isinstance(value, dict)
        }

    def _load_policies(self) -> dict[str, ToolPolicy]:
        path = self._config_dir / "policies.yaml"
        raw = self._load_yaml_mapping(path)
        policies = raw.get("policies") or {}
        if not isinstance(policies, dict):
            raise ValueError(f"policies must be a mapping: {path}")
        return {
            name: self._parse_policy(name, value)
            for name, value in policies.items()
            if isinstance(value, dict)
        }

    def _load_yaml_mapping(self, path: Path) -> dict[str, Any]:
        if not path.exists():
            return {}
        with path.open("r", encoding="utf-8") as file_obj:
            raw = yaml.safe_load(file_obj) or {}
        if not isinstance(raw, dict):
            raise ValueError(f"YAML config must be a mapping: {path}")
        return raw

    def _parse_spec(self, name: str, raw: dict[str, Any]) -> ToolSpec:
        risk_level = str(raw.get("risk_level") or "safe")
        if risk_level not in {"safe", "network", "write", "sandbox"}:
            raise ValueError(f"Invalid risk_level for tool {name}: {risk_level}")
        return ToolSpec(
            name=str(raw.get("name") or name),
            provider=str(raw.get("provider") or ""),
            description=str(raw.get("description") or ""),
            domains=set(self._str_list(raw.get("domains"))),
            workflows=set(self._str_list(raw.get("workflows"))),
            required_skills=set(self._str_list(raw.get("required_skills"))),
            risk_level=risk_level,  # type: ignore[arg-type]
            enabled_by_default=bool(raw.get("enabled_by_default", False)),
        )

    def _parse_policy(self, name: str, raw: dict[str, Any]) -> ToolPolicy:
        skill_map = raw.get("include_when_skill_selected") or {}
        parsed_skill_map: dict[str, set[str]] = {}
        if isinstance(skill_map, dict):
            for skill_name, tool_names in skill_map.items():
                parsed_skill_map[str(skill_name)] = set(self._str_list(tool_names))
        return ToolPolicy(
            name=name,
            include=set(self._str_list(raw.get("include"))),
            exclude=set(self._str_list(raw.get("exclude"))),
            include_when_skill_selected=parsed_skill_map,
        )

    def _str_list(self, value: Any) -> list[str]:
        if not isinstance(value, list):
            return []
        return [str(item).strip() for item in value if str(item or "").strip()]
