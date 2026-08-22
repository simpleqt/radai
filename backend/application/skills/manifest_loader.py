"""Load platform-level SkillManifest data from SKILL.md front matter."""

from __future__ import annotations

import re
from pathlib import Path
from typing import Any

import yaml

from backend.application.tools.assembler import shared_default_tool_registry
from backend.application.tools.registry import ToolRegistry
from backend.core.utils.normalization import normalize_string_list
from backend.domain.agents.workflows import SUPPORTED_WORKFLOWS
from backend.domain.skills.manifest import SkillManifest

MAX_MANIFEST_FILE_SIZE = 1 * 1024 * 1024


class SkillManifestLoader:
    def __init__(
        self,
        *,
        skills_root: Path | None = None,
        tool_registry: ToolRegistry | None = None,
        validate_tools: bool = True,
    ):
        default_root = Path(__file__).resolve().parents[2] / "skills"
        self._skills_roots = (
            [skills_root]
            if skills_root is not None
            else [default_root, default_root.parent / "data" / "managed_skills"]
        )
        # 共享进程级只读注册表：默认构造会每次读盘解析两份 YAML。
        self._tool_registry = (
            tool_registry or shared_default_tool_registry()
        )
        self._validate_tools = validate_tools

    @property
    def primary_skills_root(self) -> Path | None:
        """Return the repository Skill root bound to this loader.

        Runtime composition uses the same root after definition validation;
        managed overlays remain the responsibility of ``ScopedSkillsProvider``.
        """

        return self._skills_roots[0] if self._skills_roots else None

    def list_manifests(self) -> list[SkillManifest]:
        manifests_by_name: dict[str, SkillManifest] = {}
        for skills_root in self._skills_roots:
            for category in ("public", "custom"):
                category_dir = skills_root / category
                if not category_dir.exists():
                    continue
                # Packages may contain nested category paths.  Use the same
                # recursive discovery rule as the core loader and keep the
                # root/category iteration order as the explicit precedence:
                # managed custom > repository custom > public.
                for skill_file in sorted(category_dir.rglob("SKILL.md")):
                    if (
                        skill_file.is_symlink()
                        or skill_file.stat().st_size > MAX_MANIFEST_FILE_SIZE
                    ):
                        continue
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
            version=(
                override.version
                if override.version and override.version != "0.0.0"
                else base.version
            ),
            tools=override.tools or base.tools,
            allowed_workflows=override.allowed_workflows or base.allowed_workflows,
            preferred_workflow=override.preferred_workflow or base.preferred_workflow,
            kb_required=override.kb_required or base.kb_required,
            kb_binding_mode=override.kb_binding_mode or base.kb_binding_mode,
            guardrails=override.guardrails or base.guardrails,
            dependencies=override.dependencies or base.dependencies,
            conflicts=override.conflicts or base.conflicts,
            capabilities=override.capabilities or base.capabilities,
            resource_budget=override.resource_budget or base.resource_budget,
        )

    def load_file(self, skill_file: Path, *, category: str) -> SkillManifest | None:
        if not skill_file.exists() or skill_file.name != "SKILL.md":
            return None
        if (
            skill_file.is_symlink()
            or skill_file.stat().st_size > MAX_MANIFEST_FILE_SIZE
        ):
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
            allowed_workflows = normalize_string_list(workflows.get("allowed"))
            preferred_workflow = str(workflows.get("preferred") or "").strip() or None
        else:
            allowed_workflows = normalize_string_list(workflows)
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
            version=str(raw.get("version") or "0.0.0"),
            tools=tools,
            allowed_workflows=allowed_workflows or list(SUPPORTED_WORKFLOWS),
            preferred_workflow=preferred_workflow,
            kb_required=bool(kb_policy.get("required", False)),
            kb_binding_mode=str(
                kb_policy.get("binding_mode") or "agent_or_user_selected"
            ),
            guardrails=normalize_string_list(raw.get("guardrails")),
            dependencies=normalize_string_list(raw.get("dependencies")),
            conflicts=normalize_string_list(raw.get("conflicts")),
            capabilities=normalize_string_list(raw.get("capabilities")),
            resource_budget=(
                {
                    str(key): value
                    for key, value in (raw.get("resource_budget") or {}).items()
                }
                if isinstance(raw.get("resource_budget"), dict)
                else {}
            ),
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
