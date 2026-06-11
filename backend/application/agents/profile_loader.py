"""Profile loader for platform-standard agent execution."""

from __future__ import annotations

from pathlib import Path
from typing import Any

import yaml

from backend.domain.agents.profile import AgentProfile
from backend.domain.agents.workflows import SUPPORTED_WORKFLOWS


class AgentProfileLoader:
    def __init__(self, config_dir: Path | None = None):
        self._config_dir = config_dir or Path(__file__).resolve().parents[2] / "config" / "agents"

    def load(self, agent_code: str) -> AgentProfile:
        normalized = str(agent_code or "ne").strip().lower()
        configured = self._load_from_yaml(normalized)
        if configured is not None:
            return configured
        raise ValueError(f"Unknown agent profile config: {agent_code}")

    def list_profiles(self) -> list[AgentProfile]:
        profiles: dict[str, AgentProfile] = {}
        if self._config_dir.exists():
            for path in sorted(self._config_dir.glob("*.yaml")):
                loaded = self._load_from_yaml(path.stem)
                if loaded is not None:
                    profiles[loaded.code] = loaded
        return [profiles[code] for code in sorted(profiles)]

    def _load_from_yaml(self, agent_code: str) -> AgentProfile | None:
        path = self._config_dir / f"{agent_code}.yaml"
        if not path.exists():
            return None
        with path.open("r", encoding="utf-8") as file_obj:
            raw = yaml.safe_load(file_obj) or {}
        if not isinstance(raw, dict):
            raise ValueError(f"Agent profile config must be a mapping: {path}")
        data = self._normalize_profile_data(raw)
        return AgentProfile(**data)

    def _normalize_profile_data(self, raw: dict[str, Any]) -> dict[str, Any]:
        code = str(raw.get("code") or "").strip()
        name = str(raw.get("name") or code).strip()
        domain = str(raw.get("domain") or code).strip()
        if not code:
            raise ValueError("Agent profile config missing required field: code")
        return {
            "code": code,
            "name": name,
            "domain": domain,
            "description": str(raw.get("description") or ""),
            "default_model": raw.get("default_model"),
            "default_kb_ids": self._str_list(raw.get("default_kb_ids")),
            "default_skills": self._str_list(raw.get("default_skills")),
            "default_workflow": str(raw.get("default_workflow") or "lightweight"),
            "allowed_workflows": self._str_list(
                raw.get("allowed_workflows"),
                default=list(SUPPORTED_WORKFLOWS),
            ),
            "prompt_profile": str(raw.get("prompt_profile") or "default"),
            "tool_policy": str(raw.get("tool_policy") or "default"),
            "kb_policy": str(raw.get("kb_policy") or "bound_or_selected"),
            "guardrail_policy": str(raw.get("guardrail_policy") or "default"),
            "ui_profile": raw.get("ui_profile"),
            "domain_context_provider": raw.get("domain_context_provider"),
        }

    def _str_list(self, value: Any, *, default: list[str] | None = None) -> list[str]:
        if value is None:
            return list(default or [])
        if not isinstance(value, list):
            return list(default or [])
        return [str(item).strip() for item in value if str(item or "").strip()]
