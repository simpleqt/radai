"""Skill manifest and runtime resolution facade.

Manifest validation is intentionally dependency-light.  Runtime resolution
loads database models, so expose both groups lazily instead of making a caller
that only reads ``SKILL.md`` initialize the database and application settings.
"""

from importlib import import_module
from typing import Any

__all__ = [
    "RuntimeSkillOption",
    "RuntimeSkillResolution",
    "SkillRuntimeError",
    "SkillRuntimeResolver",
    "SkillManifestLoader",
]

_EXPORT_MODULES = {
    "SkillManifestLoader": "backend.application.skills.manifest_loader",
    "RuntimeSkillOption": "backend.application.skills.runtime_resolver",
    "RuntimeSkillResolution": "backend.application.skills.runtime_resolver",
    "SkillRuntimeError": "backend.application.skills.runtime_resolver",
    "SkillRuntimeResolver": "backend.application.skills.runtime_resolver",
}


def __getattr__(name: str) -> Any:
    """Load database-backed runtime exports only when explicitly requested."""
    module_name = _EXPORT_MODULES.get(name)
    if module_name is None:
        raise AttributeError(name)
    value = getattr(import_module(module_name), name)
    globals()[name] = value
    return value


def __dir__() -> list[str]:
    return sorted(set(globals()) | set(__all__))
