"""YAML-backed prompt template provider registry."""

from __future__ import annotations

import importlib
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any, Callable

import yaml


PromptProvider = Callable[..., str]


@dataclass(frozen=True)
class PromptProfileSpec:
    name: str
    provider: str
    aliases: set[str] = field(default_factory=set)


class PromptTemplateRegistry:
    def __init__(self, config_path: Path | None = None):
        self._config_path = config_path or (
            Path(__file__).resolve().parents[2] / "config" / "prompts" / "registry.yaml"
        )
        self._specs = self._load_specs()
        self._aliases = {
            alias.strip().lower(): name
            for name, spec in self._specs.items()
            for alias in spec.aliases | {name}
            if alias.strip()
        }

    def resolve_provider(self, prompt_profile: str | None, *, fallback_key: str | None = None) -> PromptProvider:
        profile_name = self.resolve_name(prompt_profile)
        if not profile_name and fallback_key:
            profile_name = self.resolve_name(fallback_key)
        if not profile_name or profile_name not in self._specs:
            raise ValueError(f"Unknown prompt profile: {prompt_profile or fallback_key}")
        return self._load_provider(self._specs[profile_name].provider)

    def resolve_name(self, prompt_profile: str | None) -> str | None:
        normalized = str(prompt_profile or "").strip().lower()
        if not normalized:
            return None
        return self._aliases.get(normalized, normalized if normalized in self._specs else None)

    def list_specs(self) -> list[PromptProfileSpec]:
        return [self._specs[name] for name in sorted(self._specs)]

    def _load_specs(self) -> dict[str, PromptProfileSpec]:
        if not self._config_path.exists():
            raise FileNotFoundError(f"Prompt registry config not found: {self._config_path}")
        raw = yaml.safe_load(self._config_path.read_text(encoding="utf-8")) or {}
        profiles = raw.get("prompt_profiles") if isinstance(raw, dict) else None
        if not isinstance(profiles, dict):
            raise ValueError(
                f"Prompt registry must define a prompt_profiles mapping: {self._config_path}"
            )

        specs: dict[str, PromptProfileSpec] = {}
        for name, value in profiles.items():
            if not isinstance(value, dict):
                continue
            normalized_name = str(name or "").strip()
            provider = str(value.get("provider") or "").strip()
            if not normalized_name or not provider:
                continue
            specs[normalized_name] = PromptProfileSpec(
                name=normalized_name,
                provider=provider,
                aliases=self._str_set(value.get("aliases")) | {normalized_name},
            )
        if not specs:
            raise ValueError(f"Prompt registry contains no valid prompt profiles: {self._config_path}")
        return specs

    def _load_provider(self, provider_path: str) -> PromptProvider:
        module_name, sep, attr_name = provider_path.partition(":")
        if not sep or not module_name or not attr_name:
            raise ValueError(f"Invalid prompt provider: {provider_path}")
        module = importlib.import_module(module_name)
        provider = getattr(module, attr_name)
        if not callable(provider):
            raise ValueError(f"Prompt provider is not callable: {provider_path}")
        return provider

    def _str_set(self, value: Any) -> set[str]:
        if isinstance(value, str):
            return {value.strip()} if value.strip() else set()
        if not isinstance(value, list):
            return set()
        return {str(item).strip() for item in value if str(item or "").strip()}
