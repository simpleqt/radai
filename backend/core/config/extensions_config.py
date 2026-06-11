"""Extensions configuration for skills and optional MCP servers."""

from __future__ import annotations

import json
import os
from pathlib import Path
from typing import Any, Literal

from pydantic import BaseModel, ConfigDict, Field


class McpOAuthConfig(BaseModel):
    enabled: bool = Field(default=True)
    token_url: str = Field(...)
    grant_type: Literal["client_credentials", "refresh_token"] = Field(default="client_credentials")
    client_id: str | None = Field(default=None)
    client_secret: str | None = Field(default=None)
    refresh_token: str | None = Field(default=None)
    scope: str | None = Field(default=None)
    audience: str | None = Field(default=None)
    token_field: str = Field(default="access_token")
    token_type_field: str = Field(default="token_type")
    expires_in_field: str = Field(default="expires_in")
    default_token_type: str = Field(default="Bearer")
    refresh_skew_seconds: int = Field(default=60)
    extra_token_params: dict[str, str] = Field(default_factory=dict)
    model_config = ConfigDict(extra="allow")


class McpServerConfig(BaseModel):
    enabled: bool = Field(default=True)
    type: str = Field(default="stdio")
    command: str | None = Field(default=None)
    args: list[str] = Field(default_factory=list)
    env: dict[str, str] = Field(default_factory=dict)
    url: str | None = Field(default=None)
    headers: dict[str, str] = Field(default_factory=dict)
    oauth: McpOAuthConfig | None = Field(default=None)
    description: str = Field(default="")
    model_config = ConfigDict(extra="allow")


class SkillStateConfig(BaseModel):
    enabled: bool = Field(default=True)


class ExtensionsConfig(BaseModel):
    mcp_servers: dict[str, McpServerConfig] = Field(default_factory=dict, alias="mcpServers")
    skills: dict[str, SkillStateConfig] = Field(default_factory=dict)
    model_config = ConfigDict(extra="allow", populate_by_name=True)

    @classmethod
    def resolve_config_path(cls, config_path: str | None = None) -> Path | None:
        if config_path:
            path = Path(config_path)
            if not path.exists():
                raise FileNotFoundError(f"Extensions config file specified by param `config_path` not found at {path}")
            return path
        env_path = os.getenv("BACKEND_EXTENSIONS_CONFIG_PATH")
        if env_path:
            path = Path(env_path)
            if not path.exists():
                raise FileNotFoundError(
                    "Extensions config file specified by environment variable "
                    f"`BACKEND_EXTENSIONS_CONFIG_PATH` not found at {path}"
                )
            return path
        backend_dir = Path(__file__).resolve().parents[2]
        repo_root = backend_dir.parent
        for path in (
            backend_dir / 'extensions_config.json',
            repo_root / 'extensions_config.json',
            backend_dir / 'mcp_config.json',
            repo_root / 'mcp_config.json',
        ):
            if path.exists():
                return path
        return None

    @classmethod
    def from_file(cls, config_path: str | None = None) -> 'ExtensionsConfig':
        resolved_path = cls.resolve_config_path(config_path)
        if resolved_path is None:
            return cls(mcp_servers={}, skills={})
        with open(resolved_path, encoding='utf-8') as f:
            config_data = json.load(f)
        cls.resolve_env_variables(config_data)
        return cls.model_validate(config_data)

    @classmethod
    def resolve_env_variables(cls, config: dict[str, Any]) -> dict[str, Any]:
        for key, value in config.items():
            if isinstance(value, str):
                if value.startswith('$'):
                    config[key] = os.getenv(value[1:], '')
            elif isinstance(value, dict):
                config[key] = cls.resolve_env_variables(value)
            elif isinstance(value, list):
                config[key] = [cls.resolve_env_variables(item) if isinstance(item, dict) else item for item in value]
        return config

    def is_skill_enabled(self, skill_name: str, skill_category: str) -> bool:
        skill_config = self.skills.get(skill_name)
        if skill_config is None:
            return skill_category in ('public', 'custom')
        return skill_config.enabled
