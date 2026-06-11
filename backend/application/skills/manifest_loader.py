"""Load platform-level SkillManifest data from SKILL.md front matter."""

from __future__ import annotations

import re
from pathlib import Path
from typing import Any

import yaml

from backend.application.tools.registry import ToolRegistry
from backend.domain.agents.workflows import SUPPORTED_WORKFLOWS
from backend.domain.skills.manifest import SkillManifest


class SkillManifestLoader:
    def __init__(
        self,
        *,
        skills_root: Path | None = None,
        tool_registry: ToolRegistry | None = None,
        validate_tools: bool = True,
    ):
        self._skills_root = skills_root or Path(__file__).resolve().parents[2] / "skills"
        self._tool_registry = tool_registry or ToolRegistry()
        self._validate_tools = validate_tools

    def list_manifests(self) -> list[SkillManifest]:
        manifests_by_name: dict[str, SkillManifest] = {}
        for category in ("public", "custom"):
            category_dir = self._skills_root / category
            if not category_dir.exists():
                continue
            for skill_file in sorted(category_dir.glob("*/SKILL.md")):
                manifest = self.load_file(skill_file, category=category)
                if manifest is not None:
                    existing = manifests_by_name.get(manifest.name)
                    manifests_by_name[manifest.name] = (
                        self._merge_manifest(existing, manifest)
                        if existing is not None
                        else manifest
                    )
        return sorted(manifests_by_name.values(), key=lambda item: item.name)

    def load(self, skill_name: str) -> SkillManifest:
        normalized = str(skill_name or "").strip()
        for manifest in self.list_manifests():
            if manifest.name == normalized:
                return manifest
        raise ValueError(f"Unknown skill: {skill_name}")

    def _merge_manifest(
        self,
        base: SkillManifest,
        override: SkillManifest,
    ) -> SkillManifest:
        return SkillManifest(
            name=override.name,
            description=override.description or base.description,
            domain=override.domain or base.domain,
            skill_file=override.skill_file,
            category=override.category,
            tools=override.tools or base.tools,
            allowed_workflows=override.allowed_workflows or base.allowed_workflows,
            preferred_workflow=override.preferred_workflow or base.preferred_workflow,
            kb_required=override.kb_required or base.kb_required,
            kb_binding_mode=override.kb_binding_mode or base.kb_binding_mode,
            guardrails=override.guardrails or base.guardrails,
        )

    def load_file(self, skill_file: Path, *, category: str) -> SkillManifest | None:
        if not skill_file.exists() or skill_file.name != "SKILL.md":
            return None
        content = skill_file.read_text(encoding="utf-8")
        match = re.match(r"^---\s*\n(.*?)\n---\s*\n", content, re.DOTALL)
        if not match:
            return None
        raw = yaml.safe_load(match.group(1)) or {}
        if not isinstance(raw, dict):
            return None
        name = str(raw.get("name") or "").strip()
        description = str(raw.get("description") or "").strip()
        if not name or not description:
            return None
        tools = self._parse_tools(raw.get("tools"))
        if self._validate_tools:
            for tool_name in tools:
                self._tool_registry.get(tool_name)
        workflows = raw.get("workflows") or {}
        allowed_workflows: list[str]
        preferred_workflow: str | None
        if isinstance(workflows, dict):
            allowed_workflows = self._str_list(workflows.get("allowed"))
            preferred_workflow = str(workflows.get("preferred") or "").strip() or None
        else:
            allowed_workflows = self._str_list(workflows)
            preferred_workflow = None
        kb_policy = raw.get("kb_policy") or {}
        if not isinstance(kb_policy, dict):
            kb_policy = {}
        return SkillManifest(
            name=name,
            description=description,
            domain=str(raw.get("domain") or name),
            skill_file=skill_file,
            category=category,
            tools=tools,
            allowed_workflows=allowed_workflows or list(SUPPORTED_WORKFLOWS),
            preferred_workflow=preferred_workflow,
            kb_required=bool(kb_policy.get("required", False)),
            kb_binding_mode=str(kb_policy.get("binding_mode") or "agent_or_user_selected"),
            guardrails=self._str_list(raw.get("guardrails")),
        )

    def _parse_tools(self, value: Any) -> list[str]:
        if isinstance(value, str):
            return [value.strip()] if value.strip() else []
        if not isinstance(value, list):
            return []
        names: list[str] = []
        seen: set[str] = set()
        for item in value:
            if isinstance(item, dict):
                name = str(item.get("name") or "").strip()
            else:
                name = str(item or "").strip()
            if name and name not in seen:
                names.append(name)
                seen.add(name)
        return names

    def _str_list(self, value: Any) -> list[str]:
        if isinstance(value, str):
            return [value.strip()] if value.strip() else []
        if not isinstance(value, list):
            return []
        return [str(item).strip() for item in value if str(item or "").strip()]
