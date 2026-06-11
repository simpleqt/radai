"""Shared workflow naming and capability helpers.

Workflow names and capabilities are loaded from ``backend/config/workflows`` so
new workflows can be added without editing this module. The constants below are
kept as stable names for built-in callers.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from pathlib import Path
from typing import Any, Final

import yaml

LIGHTWEIGHT_WORKFLOW: Final[str] = "lightweight"
FULL_WORKFLOW: Final[str] = "full"
DEEP_RESEARCH_WORKFLOW: Final[str] = "deep_research"
PLAN_SOLVE_WORKFLOW: Final[str] = "plan_solve"
MULTI_AGENT_PARALLEL_WORKFLOW: Final[str] = "multi_agent_parallel"
MULTI_AGENT_ALTERNATING_WORKFLOW: Final[str] = "multi_agent_alternating"


@dataclass(frozen=True)
class WorkflowSpec:
    name: str
    provider: str
    aliases: set[str] = field(default_factory=set)
    capabilities: set[str] = field(default_factory=set)
    full_capability: bool = False
    subagent_enabled: bool = False


def _config_path() -> Path:
    return Path(__file__).resolve().parents[2] / "config" / "workflows" / "registry.yaml"


def _str_set(value: Any) -> set[str]:
    if isinstance(value, str):
        return {value.strip()} if value.strip() else set()
    if not isinstance(value, list):
        return set()
    return {str(item).strip() for item in value if str(item or "").strip()}


def _load_workflow_specs(path: Path | None = None) -> dict[str, WorkflowSpec]:
    path = path or _config_path()
    if not path.exists():
        raise FileNotFoundError(f"Workflow registry config not found: {path}")
    raw = yaml.safe_load(path.read_text(encoding="utf-8")) or {}
    workflows = raw.get("workflows") if isinstance(raw, dict) else None
    if not isinstance(workflows, dict):
        raise ValueError(f"Workflow registry must define a workflows mapping: {path}")

    specs: dict[str, WorkflowSpec] = {}
    for name, value in workflows.items():
        if not isinstance(value, dict):
            continue
        normalized_name = str(name or "").strip()
        provider = str(value.get("provider") or "").strip()
        if not normalized_name or not provider:
            continue
        aliases = _str_set(value.get("aliases")) | {normalized_name}
        capabilities = _str_set(value.get("capabilities")) or {normalized_name}
        if bool(value.get("full_capability", False)):
            capabilities.add(FULL_WORKFLOW)
        specs[normalized_name] = WorkflowSpec(
            name=normalized_name,
            provider=provider,
            aliases=aliases,
            capabilities=capabilities,
            full_capability=bool(value.get("full_capability", False)),
            subagent_enabled=bool(value.get("subagent_enabled", False)),
        )
    if not specs:
        raise ValueError(f"Workflow registry contains no valid workflows: {path}")
    return specs


WORKFLOW_SPECS: Final[dict[str, WorkflowSpec]] = _load_workflow_specs()
SUPPORTED_WORKFLOWS: Final[tuple[str, ...]] = tuple(WORKFLOW_SPECS)
WORKFLOW_ALIASES: Final[dict[str, str]] = {
    alias.strip().lower(): name
    for name, spec in WORKFLOW_SPECS.items()
    for alias in spec.aliases
    if alias.strip()
}
COORDINATED_WORKFLOWS: Final[set[str]] = {
    name
    for name, spec in WORKFLOW_SPECS.items()
    if spec.provider.endswith(":create_coordinated_workflow")
}
FULL_CAPABILITY_WORKFLOWS: Final[set[str]] = {
    name for name, spec in WORKFLOW_SPECS.items() if FULL_WORKFLOW in spec.capabilities
}
SUBAGENT_WORKFLOWS: Final[set[str]] = {
    name for name, spec in WORKFLOW_SPECS.items() if spec.subagent_enabled
}


def normalize_workflow_name(workflow_name: str | None) -> str:
    normalized = str(workflow_name or "").strip().lower()
    return WORKFLOW_ALIASES.get(normalized, normalized)


def resolve_workflow_capabilities(workflow_name: str | None) -> set[str]:
    normalized = normalize_workflow_name(workflow_name)
    spec = WORKFLOW_SPECS.get(normalized)
    if spec is not None:
        return set(spec.capabilities)
    if normalized:
        return {normalized}
    return set()


def get_workflow_spec(workflow_name: str | None) -> WorkflowSpec | None:
    return WORKFLOW_SPECS.get(normalize_workflow_name(workflow_name))
