"""Profile loader for platform-standard agent execution."""

from __future__ import annotations

import threading
from collections.abc import Iterable
from pathlib import Path
from typing import Any

import yaml

from backend.domain.agents.profile import AgentProfile
from backend.domain.agents.workflows import SUPPORTED_WORKFLOWS

# AgentService(及其内部的 AgentProfileLoader)按请求构造,实例级缓存无法
# 跨请求存活;归一化后的 profile 数据以 (文件路径, 工作流白名单) 为键、
# (mtime_ns, size) 为签名缓存在进程级,文件变更后下一次加载自动重读。
_PROFILE_DATA_CACHE: dict[
    tuple[str, tuple[str, ...]],
    tuple[tuple[int, int], dict[str, Any] | None],
] = {}
_PROFILE_DATA_CACHE_LOCK = threading.Lock()


def invalidate_profile_cache() -> None:
    """Force the next profile load to re-read every YAML file."""

    with _PROFILE_DATA_CACHE_LOCK:
        _PROFILE_DATA_CACHE.clear()


class AgentProfileLoader:
    def __init__(
        self,
        config_dir: Path | None = None,
        *,
        supported_workflows: Iterable[str] | None = None,
    ):
        self._config_dir = (
            config_dir or Path(__file__).resolve().parents[2] / "config" / "agents"
        )
        self._supported_workflows = tuple(
            supported_workflows
            if supported_workflows is not None
            else SUPPORTED_WORKFLOWS
        )

    def load(self, agent_code: str) -> AgentProfile:
        normalized = str(agent_code or "").strip().lower()
        if not normalized:
            raise ValueError("Agent profile code is required")
        configured = self._load_from_yaml(normalized)
        if configured is not None:
            return configured
        raise ValueError(f"Unknown agent profile config: {agent_code}")

    def list_profiles(self) -> list[AgentProfile]:
        profiles: dict[str, AgentProfile] = {}
        for code in self.list_profile_codes():
            loaded = self._load_from_yaml(code)
            if loaded is not None:
                profiles[loaded.code] = loaded
        return [profiles[code] for code in sorted(profiles)]

    def list_profile_codes(self) -> list[str]:
        """List candidate profile files without parsing them.

        Validation uses this discovery method so one malformed YAML document
        becomes one report instead of aborting validation of every profile.
        ``runtime_agents.yaml`` is a separate manifest, not an AgentProfile.
        """
        if not self._config_dir.exists():
            return []
        return sorted(
            path.stem
            for path in self._config_dir.glob("*.yaml")
            if path.stem != "runtime_agents"
        )

    def _load_from_yaml(self, agent_code: str) -> AgentProfile | None:
        path = self._config_dir / f"{agent_code}.yaml"
        try:
            stat = path.stat()
        except FileNotFoundError:
            return None
        signature = (int(stat.st_mtime_ns), int(stat.st_size))
        cache_key = (str(path), self._supported_workflows)
        with _PROFILE_DATA_CACHE_LOCK:
            cached = _PROFILE_DATA_CACHE.get(cache_key)
        if cached is not None and cached[0] == signature:
            return self._profile_from_data(cached[1])
        with path.open("r", encoding="utf-8") as file_obj:
            raw = yaml.safe_load(file_obj) or {}
        if not isinstance(raw, dict):
            raise ValueError(f"Agent profile config must be a mapping: {path}")
        if "code" not in raw and "agents" in raw:
            with _PROFILE_DATA_CACHE_LOCK:
                _PROFILE_DATA_CACHE[cache_key] = (signature, None)
            return None
        data = self._normalize_profile_data(raw)
        if data["code"].strip().lower() != agent_code.strip().lower():
            raise ValueError(
                f"Agent profile code mismatch: file={path.name} code={data['code']}"
            )
        with _PROFILE_DATA_CACHE_LOCK:
            _PROFILE_DATA_CACHE[cache_key] = (signature, data)
        return self._profile_from_data(data)

    @staticmethod
    def _profile_from_data(data: dict[str, Any] | None) -> AgentProfile | None:
        """Build a fresh profile from cached normalized data.

        每次返回新对象并复制可变字段,调用方对 profile 列表/字典的就地修改
        不会污染进程级缓存。
        """

        if data is None:
            return None
        payload = dict(data)
        for key in ("default_kb_ids", "default_skills", "allowed_workflows"):
            payload[key] = list(payload[key])
        if payload.get("stream_timeout_policy") is not None:
            payload["stream_timeout_policy"] = dict(payload["stream_timeout_policy"])
        return AgentProfile(**payload)

    def _normalize_profile_data(self, raw: dict[str, Any]) -> dict[str, Any]:
        code = str(raw.get("code") or "").strip()
        name = str(raw.get("name") or code).strip()
        domain = str(raw.get("domain") or code).strip()
        if not code:
            raise ValueError("Agent profile config missing required field: code")
        default_workflow = str(raw.get("default_workflow") or "lightweight").strip()
        allowed_workflows = self._str_list_field(
            raw,
            "allowed_workflows",
            default=list(self._supported_workflows),
            profile_code=code,
        )
        unknown_workflows = sorted(
            set(allowed_workflows) - set(self._supported_workflows)
        )
        if unknown_workflows:
            raise ValueError(
                f"Agent profile {code} declares unknown workflows: {', '.join(unknown_workflows)}"
            )
        if default_workflow not in allowed_workflows:
            raise ValueError(
                f"Agent profile {code} default_workflow must be allowed: {default_workflow}"
            )

        return {
            "code": code,
            "name": name,
            "domain": domain,
            "description": str(raw.get("description") or ""),
            "default_model": raw.get("default_model"),
            "default_kb_ids": self._str_list_field(
                raw,
                "default_kb_ids",
                profile_code=code,
            ),
            "default_skills": self._str_list_field(
                raw,
                "default_skills",
                profile_code=code,
            ),
            "default_workflow": default_workflow,
            "allowed_workflows": allowed_workflows,
            "prompt_profile": str(raw.get("prompt_profile") or "default"),
            "tool_policy": str(raw.get("tool_policy") or "default"),
            "kb_policy": str(raw.get("kb_policy") or "bound_or_selected"),
            "guardrail_policy": str(raw.get("guardrail_policy") or "default"),
            "ui_profile": raw.get("ui_profile"),
            "domain_context_provider": raw.get("domain_context_provider"),
            "stream_timeout_policy": self._mapping_field(
                raw,
                "stream_timeout_policy",
                profile_code=code,
            ),
        }

    def _mapping_field(
        self,
        raw: dict[str, Any],
        field_name: str,
        *,
        profile_code: str,
    ) -> dict[str, Any] | None:
        value = raw.get(field_name)
        if value is None:
            return None
        if not isinstance(value, dict):
            raise ValueError(
                f"Agent profile {profile_code} field {field_name} must be a mapping"
            )
        return dict(value)

    def _str_list_field(
        self,
        raw: dict[str, Any],
        field_name: str,
        *,
        default: list[str] | None = None,
        profile_code: str,
    ) -> list[str]:
        if field_name not in raw:
            return list(default or [])
        value = raw[field_name]
        if not isinstance(value, list):
            raise ValueError(
                f"Agent profile {profile_code} field {field_name} must be a list"
            )
        return [str(item).strip() for item in value if str(item or "").strip()]
