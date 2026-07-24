"""Request-scoped dependency closure for repository-owned public Skills."""

from __future__ import annotations

from collections.abc import Iterable

from backend.application.skills.manifest_loader import SkillManifestLoader
from backend.application.skills.runtime_resolver import SkillRuntimeError
from backend.application.tools.registry import ToolRegistry
from backend.domain.agents.profile import AgentProfile
from backend.domain.skills.manifest import SkillManifest
from backend.domain.tools.policy import ToolPolicy


class UnmanagedSkillClosureResolver:
    """Resolve public Skill selections without widening their trust boundary.

    Database-managed selections are deliberately not handled here. Their
    bindings, dependencies, and permissions are authoritative in
    :class:`SkillRuntimeResolver`. This resolver is the compatibility path for
    repository-owned public Skills when no managed bindings exist.
    """

    def __init__(
        self,
        *,
        skill_loader: SkillManifestLoader,
        tool_registry: ToolRegistry,
    ) -> None:
        self._skill_loader = skill_loader
        self._tool_registry = tool_registry

    def resolve(
        self,
        *,
        profile: AgentProfile,
        tool_policy: ToolPolicy,
        workflow: str,
        selected_skills: Iterable[str],
        trusted_manifests: Iterable[SkillManifest] = (),
    ) -> list[str]:
        """Return a root-first, recursively expanded, validated selection."""

        selected = self._dedupe(selected_skills)
        if not selected:
            return []

        trusted = {manifest.name: manifest for manifest in trusted_manifests}
        manifests = {
            manifest.name: manifest for manifest in self._skill_loader.list_manifests()
        }
        # The definition's immutable snapshot wins for its explicitly trusted
        # defaults. Dynamic names still have to come from this exact loader.
        manifests.update(trusted)

        expanded = list(selected)
        expanded_names = set(expanded)
        resolved: dict[str, SkillManifest] = {}
        visiting: list[str] = []

        def visit(skill_name: str, *, required_by: str | None = None) -> None:
            if skill_name in visiting:
                cycle_start = visiting.index(skill_name)
                cycle = " -> ".join([*visiting[cycle_start:], skill_name])
                raise SkillRuntimeError(
                    f"Skill 依赖存在循环: {cycle}",
                    code="SKILL_DEPENDENCY_CYCLE",
                )
            if skill_name in resolved:
                return

            manifest = manifests.get(skill_name)
            if manifest is None:
                if required_by is not None:
                    raise SkillRuntimeError(
                        f"Skill 依赖不存在: {required_by} -> {skill_name}",
                        code="SKILL_DEPENDENCY_MISSING",
                    )
                raise SkillRuntimeError(
                    f"Skill 不存在: {skill_name}",
                    code="SKILL_NOT_FOUND",
                    status_code=404,
                )

            # Only definition-resolved Skills may be custom in unmanaged mode.
            # A dynamically selected public Skill cannot pull an arbitrary
            # repository/managed custom package into its dependency closure.
            if skill_name not in trusted and manifest.category != "public":
                relation = f"{required_by} -> " if required_by else ""
                raise SkillRuntimeError(
                    f"未托管请求不能加载自定义 Skill: {relation}{skill_name}",
                    code="SKILL_ACCESS_DENIED",
                    status_code=403,
                )
            if not self._domain_matches(manifest, profile):
                raise SkillRuntimeError(
                    f"Skill 与智能体领域不兼容: {skill_name}",
                    code="SKILL_ACCESS_DENIED",
                    status_code=403,
                )
            if manifest.allowed_workflows and workflow not in {
                str(item or "").strip().lower()
                for item in manifest.allowed_workflows
                if str(item or "").strip()
            }:
                raise SkillRuntimeError(
                    f"Skill 与 workflow 不兼容: {skill_name}/{workflow}",
                    code="SKILL_ACCESS_DENIED",
                    status_code=403,
                )

            visiting.append(skill_name)
            for raw_dependency in manifest.dependencies:
                dependency = str(raw_dependency or "").strip()
                if not dependency:
                    continue
                if dependency not in expanded_names:
                    expanded_names.add(dependency)
                    expanded.append(dependency)
                visit(dependency, required_by=skill_name)
            visiting.pop()
            resolved[skill_name] = manifest

        for skill_name in tuple(selected):
            visit(skill_name)

        conflicts: dict[str, list[str]] = {}
        for skill_name in expanded:
            active = sorted(set(resolved[skill_name].conflicts) & expanded_names)
            if active:
                conflicts[skill_name] = active
        if conflicts:
            raise SkillRuntimeError(
                f"Skill 之间存在冲突: {conflicts}",
                code="SKILL_CONFLICT",
            )

        allowed_tools = tool_policy.allowed_tool_names(
            selected_skills=expanded_names,
        )
        for skill_name in expanded:
            manifest = resolved[skill_name]
            for tool_name in manifest.tools:
                if tool_name not in allowed_tools:
                    raise SkillRuntimeError(
                        f"Skill 工具未被策略允许: {skill_name}/{tool_name}",
                        code="SKILL_TOOL_NOT_ALLOWED",
                    )
                try:
                    tool_spec = self._tool_registry.get(tool_name)
                except ValueError as exc:
                    raise SkillRuntimeError(
                        f"Skill 工具不存在: {skill_name}/{tool_name}",
                        code="SKILL_TOOL_NOT_FOUND",
                    ) from exc
                if not tool_spec.supports(
                    domain=profile.domain,
                    workflow=workflow,
                    selected_skills=expanded_names,
                ):
                    raise SkillRuntimeError(
                        f"Skill 工具在当前请求不可达: {skill_name}/{tool_name}",
                        code="SKILL_TOOL_UNREACHABLE",
                    )

        return expanded

    @staticmethod
    def _domain_matches(manifest: SkillManifest, profile: AgentProfile) -> bool:
        domain = str(manifest.domain or "").strip().lower()
        return domain in {
            "*",
            "general",
            profile.code.strip().lower(),
            profile.domain.strip().lower(),
        }

    @staticmethod
    def _dedupe(items: Iterable[str]) -> list[str]:
        result: list[str] = []
        seen: set[str] = set()
        for raw_item in items:
            item = str(raw_item or "").strip()
            if item and item not in seen:
                seen.add(item)
                result.append(item)
        return result


__all__ = ["UnmanagedSkillClosureResolver"]
