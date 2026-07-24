"""YAML-backed tool registry and policy resolver."""

from __future__ import annotations

import math
from pathlib import Path
from typing import Any

import yaml

from backend.domain.tools.policy import ToolPolicy
from backend.domain.tools.spec import ToolSpec

_RETRYABLE_ERROR_CATEGORIES = {
    "timeout",
    "connection",
    "rate_limited",
    "unavailable",
}


class ToolRegistry:
    def __init__(self, config_dir: Path | None = None):
        self._config_dir = (
            config_dir or Path(__file__).resolve().parents[2] / "config" / "tools"
        )
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
            if spec.supports(
                domain=domain, workflow=workflow, selected_skills=selected_skills
            ):
                specs.append(spec)
        return specs

    def _load_specs(self) -> dict[str, ToolSpec]:
        path = self._config_dir / "registry.yaml"
        raw = self._load_yaml_mapping(path)
        tools = raw["tools"] if "tools" in raw else {}
        if not isinstance(tools, dict):
            raise ValueError(f"tools must be a mapping: {path}")
        specs: dict[str, ToolSpec] = {}
        for name, value in tools.items():
            if not isinstance(value, dict):
                raise ValueError(f"Tool spec must be a mapping: {name}")
            specs[str(name)] = self._parse_spec(str(name), value)
        return specs

    def _load_policies(self) -> dict[str, ToolPolicy]:
        path = self._config_dir / "policies.yaml"
        raw = self._load_yaml_mapping(path)
        policies = raw["policies"] if "policies" in raw else {}
        if not isinstance(policies, dict):
            raise ValueError(f"policies must be a mapping: {path}")
        parsed: dict[str, ToolPolicy] = {}
        for name, value in policies.items():
            if not isinstance(value, dict):
                raise ValueError(f"Tool policy must be a mapping: {name}")
            parsed[str(name)] = self._parse_policy(str(name), value)
        return parsed

    def _load_yaml_mapping(self, path: Path) -> dict[str, Any]:
        if not path.exists():
            return {}
        with path.open("r", encoding="utf-8") as file_obj:
            raw = yaml.safe_load(file_obj) or {}
        if not isinstance(raw, dict):
            raise ValueError(f"YAML config must be a mapping: {path}")
        return raw

    def _parse_spec(self, name: str, raw: dict[str, Any]) -> ToolSpec:
        declared_name = str(raw.get("name") or name).strip()
        if declared_name != name:
            raise ValueError(f"Tool spec name does not match registry key: {name}")
        provider = str(raw.get("provider") or "").strip()
        if ":" not in provider:
            raise ValueError(f"Tool provider is required for {name}")
        risk_level = str(raw.get("risk_level") or "safe")
        if risk_level not in {"safe", "network", "write"}:
            raise ValueError(f"Invalid risk_level for tool {name}: {risk_level}")
        required_skill_mode = (
            str(raw.get("required_skill_mode") or "any").strip().lower()
        )
        if required_skill_mode not in {"any", "all"}:
            raise ValueError(
                f"Invalid required_skill_mode for tool {name}: {required_skill_mode}"
            )
        timeout_seconds = self._optional_float(raw.get("timeout_seconds"))
        if raw.get("timeout_seconds") not in (None, "") and timeout_seconds is None:
            raise ValueError(f"Invalid timeout_seconds for tool {name}")
        if timeout_seconds is not None and (
            not math.isfinite(timeout_seconds) or timeout_seconds <= 0
        ):
            raise ValueError(f"Invalid timeout_seconds for tool {name}")
        max_concurrency = self._optional_int(raw.get("max_concurrency"))
        if raw.get("max_concurrency") not in (None, "") and max_concurrency is None:
            raise ValueError(f"Invalid max_concurrency for tool {name}")
        if max_concurrency is not None and max_concurrency <= 0:
            raise ValueError(f"Invalid max_concurrency for tool {name}")
        if max_concurrency is not None and max_concurrency > 256:
            raise ValueError(f"max_concurrency exceeds service limit for tool {name}")
        parsed_max_attempts = self._optional_int(raw.get("max_attempts"))
        if raw.get("max_attempts") not in (None, "") and parsed_max_attempts is None:
            raise ValueError(f"Invalid max_attempts for tool {name}")
        max_attempts = parsed_max_attempts or 1
        if max_attempts < 1 or max_attempts > 5:
            raise ValueError(f"Invalid max_attempts for tool {name}")
        max_output_chars = self._optional_int(raw.get("max_output_chars"))
        if raw.get("max_output_chars") not in (None, "") and max_output_chars is None:
            raise ValueError(f"Invalid max_output_chars for tool {name}")
        if max_output_chars is not None and max_output_chars < 512:
            raise ValueError(f"Invalid max_output_chars for tool {name}")
        cache_ttl_seconds = self._optional_int(raw.get("cache_ttl_seconds"))
        if raw.get("cache_ttl_seconds") not in (None, "") and cache_ttl_seconds is None:
            raise ValueError(f"Invalid cache_ttl_seconds for tool {name}")
        if cache_ttl_seconds is not None and cache_ttl_seconds <= 0:
            raise ValueError(f"Invalid cache_ttl_seconds for tool {name}")
        retryable_errors = set(
            self._str_list_field(raw, "retryable_errors", owner=f"Tool spec {name}")
        )
        unknown_retry_errors = retryable_errors - _RETRYABLE_ERROR_CATEGORIES
        if unknown_retry_errors:
            raise ValueError(
                f"Invalid retryable_errors for tool {name}: "
                f"{sorted(unknown_retry_errors)}"
            )
        return ToolSpec(
            name=declared_name,
            provider=provider,
            description=str(raw.get("description") or ""),
            domains=set(
                self._str_list_field(raw, "domains", owner=f"Tool spec {name}")
            ),
            workflows=set(
                self._str_list_field(raw, "workflows", owner=f"Tool spec {name}")
            ),
            required_skills=set(
                self._str_list_field(
                    raw,
                    "required_skills",
                    owner=f"Tool spec {name}",
                )
            ),
            required_skill_mode=required_skill_mode,  # type: ignore[arg-type]
            risk_level=risk_level,  # type: ignore[arg-type]
            enabled_by_default=self._boolean(
                raw.get("enabled_by_default", False),
                field="enabled_by_default",
                tool_name=name,
            ),
            instructions=str(raw.get("instructions") or ""),
            timeout_seconds=timeout_seconds,
            side_effect=str(raw.get("side_effect") or "none"),
            policy_source=str(raw.get("policy_source") or f"registry:{name}"),
            idempotent=self._boolean(
                raw.get("idempotent", True), field="idempotent", tool_name=name
            ),
            parallel_safe=self._boolean(
                raw.get("parallel_safe", True),
                field="parallel_safe",
                tool_name=name,
            ),
            max_concurrency=max_concurrency,
            max_attempts=max_attempts,
            retryable_errors=retryable_errors,
            max_output_chars=max_output_chars,
            cache_enabled=self._boolean(
                raw.get("cache_enabled", False),
                field="cache_enabled",
                tool_name=name,
            ),
            cache_ttl_seconds=cache_ttl_seconds,
        )

    def _parse_policy(self, name: str, raw: dict[str, Any]) -> ToolPolicy:
        if "include_when_skill_selected" not in raw:
            skill_map: dict[Any, Any] = {}
        else:
            configured_skill_map = raw["include_when_skill_selected"]
            if not isinstance(configured_skill_map, dict):
                raise ValueError(
                    "Tool policy "
                    f"{name} field include_when_skill_selected must be a mapping"
                )
            skill_map = configured_skill_map
        parsed_skill_map: dict[str, set[str]] = {}
        for skill_name, tool_names in skill_map.items():
            parsed_skill_map[str(skill_name)] = set(
                self._require_str_list(
                    tool_names,
                    field_name=f"include_when_skill_selected.{skill_name}",
                    owner=f"Tool policy {name}",
                )
            )
        return ToolPolicy(
            name=name,
            include=set(
                self._str_list_field(raw, "include", owner=f"Tool policy {name}")
            ),
            exclude=set(
                self._str_list_field(raw, "exclude", owner=f"Tool policy {name}")
            ),
            include_when_skill_selected=parsed_skill_map,
        )

    def _str_list_field(
        self,
        raw: dict[str, Any],
        field_name: str,
        *,
        owner: str,
    ) -> list[str]:
        if field_name not in raw:
            return []
        return self._require_str_list(
            raw[field_name],
            field_name=field_name,
            owner=owner,
        )

    @staticmethod
    def _require_str_list(
        value: Any,
        *,
        field_name: str,
        owner: str,
    ) -> list[str]:
        if not isinstance(value, list):
            raise ValueError(f"{owner} field {field_name} must be a list")
        return [str(item).strip() for item in value if str(item or "").strip()]

    def _optional_float(self, value: Any) -> float | None:
        if value is None or value == "":
            return None
        if isinstance(value, bool):
            return None
        try:
            return float(value)
        except (TypeError, ValueError):
            return None

    def _optional_int(self, value: Any) -> int | None:
        if value is None or value == "":
            return None
        if isinstance(value, bool):
            return None
        if isinstance(value, float) and not value.is_integer():
            return None
        try:
            parsed = int(value)
        except (TypeError, ValueError):
            return None
        return parsed

    @staticmethod
    def _boolean(value: Any, *, field: str, tool_name: str) -> bool:
        if isinstance(value, bool):
            return value
        if value in (0, 1):
            return bool(value)
        if isinstance(value, str):
            normalized = value.strip().lower()
            if normalized in {"true", "yes", "on", "1"}:
                return True
            if normalized in {"false", "no", "off", "0"}:
                return False
        raise ValueError(f"Invalid {field} for tool {tool_name}: {value!r}")
