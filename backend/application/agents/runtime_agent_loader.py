"""YAML loader for executable runtime agent roles."""

from __future__ import annotations

import threading
from pathlib import Path
from typing import Any

import yaml

from backend.domain.agents.runtime_agent import RuntimeAgentSpec

# Loader 实例随 AgentService 按请求构造,实例缓存跨请求无效;签名命中的
# 解析结果按配置路径共享在进程级(specs 视为不可变,返回时浅拷贝外层 dict)。
_RUNTIME_AGENT_CACHE: dict[
    str, tuple[tuple[int, int], dict[str, RuntimeAgentSpec]]
] = {}
_RUNTIME_AGENT_CACHE_LOCK = threading.RLock()


class RuntimeAgentLoader:
    _SPEC_FIELDS = {
        "name",
        "description",
        "system_prompt",
        "allowed_tools",
        "required_tools",
        "disallowed_tools",
        "max_turns",
        "timeout_seconds",
        "model",
        "reasoning_effort",
        "tool_access",
        "output_schema",
        "can_use_network",
        "can_read_kb",
        "planner_selectable",
        "aliases",
        "title",
        "planning_description",
    }
    _OUTPUT_SCHEMAS = {
        "ResearchTaskPlan",
        "SubagentResult",
        "MarkdownReport",
        "MarkdownAnswer",
    }

    def __init__(self, config_path: Path | None = None):
        self._config_path = (
            config_path
            or Path(__file__).resolve().parents[2]
            / "config"
            / "agents"
            / "runtime_agents.yaml"
        )
        self._cache_key = str(self._config_path)
        self._cache_lock = _RUNTIME_AGENT_CACHE_LOCK

    def get(self, name: str) -> RuntimeAgentSpec:
        normalized = self._normalize_name(name)
        specs = self.load_all()
        try:
            return specs[normalized]
        except KeyError as exc:
            raise ValueError(f"Unknown runtime agent: {name}") from exc

    def available_names(self) -> list[str]:
        return sorted(self.load_all())

    def load_all(self) -> dict[str, RuntimeAgentSpec]:
        with self._cache_lock:
            return self._load_all_locked()

    def _load_all_locked(self) -> dict[str, RuntimeAgentSpec]:
        try:
            stat = self._config_path.stat()
        except FileNotFoundError:
            raise ValueError(
                f"Runtime agent config not found: {self._config_path}"
            ) from None
        signature = (int(stat.st_mtime_ns), int(stat.st_size))
        cached = _RUNTIME_AGENT_CACHE.get(self._cache_key)
        if cached is not None and cached[0] == signature:
            return dict(cached[1])
        raw = yaml.safe_load(self._config_path.read_text(encoding="utf-8")) or {}
        if not isinstance(raw, dict):
            raise ValueError(
                f"Runtime agent config must be a mapping: {self._config_path}"
            )
        raw_agents = raw.get("agents") or {}
        if not isinstance(raw_agents, dict):
            raise ValueError(
                f"Runtime agent config field 'agents' must be a mapping: {self._config_path}"
            )
        specs: dict[str, RuntimeAgentSpec] = {}
        for name, value in raw_agents.items():
            manifest_name = self._normalize_name(name)
            if not manifest_name:
                raise ValueError("Runtime agent config has an agent with an empty name")
            if not isinstance(value, dict):
                raise ValueError(
                    f"Runtime agent {manifest_name} definition must be a mapping"
                )
            spec = self._parse_spec(manifest_name, value)
            if spec.name in specs:
                raise ValueError(
                    f"Runtime agent config declares duplicate canonical name: {spec.name}"
                )
            specs[spec.name] = spec
        if not specs:
            raise ValueError(
                f"Runtime agent config declares no agents: {self._config_path}"
            )
        self._validate_planner_aliases(specs)
        _RUNTIME_AGENT_CACHE[self._cache_key] = (signature, specs)
        return dict(specs)

    def invalidate_cache(self) -> None:
        """Force the next request to reload the manifest."""

        with self._cache_lock:
            _RUNTIME_AGENT_CACHE.pop(self._cache_key, None)

    def _parse_spec(self, name: str, raw: dict[str, Any]) -> RuntimeAgentSpec:
        unknown_fields = set(raw) - self._SPEC_FIELDS
        if unknown_fields:
            fields = ", ".join(sorted(str(field) for field in unknown_fields))
            raise ValueError(f"Runtime agent {name} has unknown fields: {fields}")
        normalized = self._normalize_name(raw.get("name") or name)
        description = str(raw.get("description") or "").strip()
        system_prompt = str(raw.get("system_prompt") or "").strip()
        if not normalized:
            raise ValueError("Runtime agent config has an agent with an empty name")
        if not description:
            raise ValueError(
                f"Runtime agent {normalized} missing required field: description"
            )
        if not system_prompt:
            raise ValueError(
                f"Runtime agent {normalized} missing required field: system_prompt"
            )
        max_turns = self._positive_int(
            raw.get("max_turns"), default=12, field_name="max_turns", agent=normalized
        )
        timeout_seconds = self._positive_int(
            raw.get("timeout_seconds"),
            default=300,
            field_name="timeout_seconds",
            agent=normalized,
        )
        allowed_tools = self._optional_tool_set(
            raw.get("allowed_tools"), field_name="allowed_tools", agent=normalized
        )
        required_tools = set(
            self._str_list(
                raw.get("required_tools"),
                field_name="required_tools",
                agent=normalized,
            )
        )
        disallowed_tools = set(
            self._str_list(
                raw.get("disallowed_tools"),
                field_name="disallowed_tools",
                agent=normalized,
            )
        )
        if allowed_tools is not None and not required_tools.issubset(allowed_tools):
            missing = ", ".join(sorted(required_tools - allowed_tools))
            raise ValueError(
                f"Runtime agent {normalized} required_tools must be included in allowed_tools: {missing}"
            )
        conflicting_tools = required_tools & disallowed_tools
        if conflicting_tools:
            conflicts = ", ".join(sorted(conflicting_tools))
            raise ValueError(
                f"Runtime agent {normalized} required_tools cannot be disallowed: {conflicts}"
            )
        output_schema = self._optional_str(raw.get("output_schema"))
        if output_schema is not None and output_schema not in self._OUTPUT_SCHEMAS:
            raise ValueError(
                f"Runtime agent {normalized} field output_schema is unknown: "
                f"{output_schema}"
            )
        planner_selectable = self._bool(
            raw.get("planner_selectable"),
            default=False,
            field_name="planner_selectable",
            agent=normalized,
        )
        aliases = tuple(
            self._str_list(
                raw.get("aliases"),
                field_name="aliases",
                agent=normalized,
            )
        )
        title = self._optional_str(raw.get("title"))
        planning_description = self._optional_str(raw.get("planning_description"))
        if planner_selectable and not title:
            raise ValueError(
                f"Runtime agent {normalized} missing required planner field: title"
            )
        if planner_selectable and not planning_description:
            raise ValueError(
                f"Runtime agent {normalized} missing required planner field: "
                "planning_description"
            )
        return RuntimeAgentSpec(
            name=normalized,
            description=description,
            system_prompt=system_prompt,
            allowed_tools=allowed_tools,
            required_tools=required_tools,
            disallowed_tools=disallowed_tools,
            max_turns=max_turns,
            timeout_seconds=timeout_seconds,
            model=self._optional_str(raw.get("model")),
            reasoning_effort=self._optional_str(raw.get("reasoning_effort")),
            tool_access=self._bool(
                raw.get("tool_access"),
                default=True,
                field_name="tool_access",
                agent=normalized,
            ),
            output_schema=output_schema,
            can_use_network=self._bool(
                raw.get("can_use_network"),
                default=True,
                field_name="can_use_network",
                agent=normalized,
            ),
            can_read_kb=self._bool(
                raw.get("can_read_kb"),
                default=True,
                field_name="can_read_kb",
                agent=normalized,
            ),
            planner_selectable=planner_selectable,
            aliases=aliases,
            title=title,
            planning_description=planning_description,
        )

    @staticmethod
    def _validate_planner_aliases(specs: dict[str, RuntimeAgentSpec]) -> None:
        """Reject ambiguous names in the planner-visible role namespace."""

        selectable = {
            name: spec for name, spec in specs.items() if spec.planner_selectable
        }
        aliases: dict[str, str] = {}
        for name, spec in selectable.items():
            for alias in spec.aliases:
                if alias == name:
                    raise ValueError(
                        f"Runtime agent {name} planner alias duplicates its canonical name: "
                        f"{alias}"
                    )
                if alias in selectable:
                    raise ValueError(
                        f"Runtime agent {name} planner alias conflicts with canonical "
                        f"planner role: {alias}"
                    )
                owner = aliases.get(alias)
                if owner is not None:
                    raise ValueError(
                        f"Runtime agent planner alias {alias} is declared by both "
                        f"{owner} and {name}"
                    )
                aliases[alias] = name

    @staticmethod
    def _normalize_name(value: Any) -> str:
        return str(value or "").strip()

    @staticmethod
    def _optional_str(value: Any) -> str | None:
        text = str(value or "").strip()
        return text or None

    @staticmethod
    def _str_list(value: Any, *, field_name: str, agent: str) -> list[str]:
        if value is None:
            return []
        if not isinstance(value, list):
            raise ValueError(
                f"Runtime agent {agent} field {field_name} must be a list of strings"
            )
        values: list[str] = []
        for item in value:
            if not isinstance(item, str) or not item.strip():
                raise ValueError(
                    f"Runtime agent {agent} field {field_name} must contain only non-empty strings"
                )
            values.append(item.strip())
        if len(values) != len(set(values)):
            raise ValueError(
                f"Runtime agent {agent} field {field_name} contains duplicate values"
            )
        return values

    def _optional_tool_set(
        self, value: Any, *, field_name: str, agent: str
    ) -> set[str] | None:
        if value is None:
            return None
        return set(self._str_list(value, field_name=field_name, agent=agent))

    @staticmethod
    def _bool(value: Any, *, default: bool, field_name: str, agent: str) -> bool:
        if value is None:
            return default
        if isinstance(value, bool):
            return value
        text = str(value).strip().lower()
        if text in {"1", "true", "yes", "on"}:
            return True
        if text in {"0", "false", "no", "off"}:
            return False
        raise ValueError(f"Runtime agent {agent} field {field_name} must be a boolean")

    @staticmethod
    def _positive_int(value: Any, *, default: int, field_name: str, agent: str) -> int:
        if value is None:
            return default
        try:
            parsed = int(value)
        except (TypeError, ValueError) as exc:
            raise ValueError(
                f"Runtime agent {agent} field {field_name} must be an integer"
            ) from exc
        if parsed <= 0:
            raise ValueError(
                f"Runtime agent {agent} field {field_name} must be positive"
            )
        return parsed
