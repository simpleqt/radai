"""Policy-filtered fallback catalog for repository-owned public Skills."""

from __future__ import annotations

from backend.application.skills.domain_policy import skill_domain_matches
from backend.application.skills.manifest_loader import SkillManifestLoader
from backend.application.tools.assembler import shared_default_tool_registry
from backend.application.tools.registry import ToolRegistry
from backend.domain.agents.profile import AgentProfile
from backend.domain.agents.workflows import normalize_workflow_name
from backend.domain.skills.manifest import SkillManifest


class PublicSkillCatalog:
    """Expose only public Skills that can execute under one Agent Profile.

    Managed database bindings remain authoritative at the API boundary. This
    catalog is the explicit compatibility path for installations where the
    resolver reports ``managed=False``.
    """

    def __init__(
        self,
        *,
        skill_loader: SkillManifestLoader | None = None,
        tool_registry: ToolRegistry | None = None,
    ) -> None:
        self._tool_registry = (
            tool_registry or shared_default_tool_registry()
        )
        self._skill_loader = skill_loader or SkillManifestLoader(
            tool_registry=self._tool_registry,
            validate_tools=False,
        )

    def list_compatible(
        self,
        profile: AgentProfile,
        *,
        workflow: str | None = None,
    ) -> list[SkillManifest]:
        manifests = {
            manifest.name: manifest
            for manifest in self._skill_loader.list_manifests()
            if manifest.category == "public"
        }
        requested_workflow = normalize_workflow_name(workflow)
        profile_workflows = [
            normalize_workflow_name(item) for item in profile.allowed_workflows
        ]
        if requested_workflow:
            if requested_workflow not in profile_workflows:
                return []
            candidate_workflows = [requested_workflow]
        else:
            candidate_workflows = profile_workflows

        compatible: list[SkillManifest] = []
        for manifest in manifests.values():
            closure = self._dependency_closure(manifest, manifests)
            if closure is None or not all(
                self._domain_matches(item, profile) for item in closure
            ):
                continue
            closure_names = {item.name for item in closure}
            selected_skills = set(profile.default_skills) | closure_names
            if self._has_conflict(closure, selected_skills, manifests):
                continue
            declared_tools = {
                tool_name for item in closure for tool_name in item.tools
            }
            viable_workflows = [
                item
                for item in candidate_workflows
                if all(
                    not skill.allowed_workflows
                    or item in {
                        normalize_workflow_name(name)
                        for name in skill.allowed_workflows
                    }
                    for skill in closure
                )
                and self._tools_reachable(
                    profile=profile,
                    workflow=item,
                    selected_skills=selected_skills,
                    declared_tools=declared_tools,
                )
            ]
            if viable_workflows:
                compatible.append(manifest)
        return compatible

    def to_info_options(
        self,
        profile: AgentProfile,
        *,
        workflow: str | None = None,
    ) -> list[dict[str, object]]:
        defaults = set(profile.default_skills)
        options: list[dict[str, object]] = []
        for manifest in self.list_compatible(profile, workflow=workflow):
            allowed = [
                item
                for item in profile.allowed_workflows
                if not manifest.allowed_workflows
                or normalize_workflow_name(item)
                in {
                    normalize_workflow_name(name)
                    for name in manifest.allowed_workflows
                }
            ]
            normalized_allowed = {
                normalize_workflow_name(item) for item in allowed
            }
            preferred_workflow = (
                manifest.preferred_workflow
                if normalize_workflow_name(manifest.preferred_workflow)
                in normalized_allowed
                else None
            )
            is_default = manifest.name in defaults
            options.append(
                {
                    "id": manifest.name,
                    "name": manifest.name,
                    "label": manifest.name,
                    "description": manifest.description,
                    "version": manifest.version,
                    "required": is_default,
                    "default_selected": is_default,
                    "selectable": not is_default,
                    "allowed_workflows": allowed,
                    "preferred_workflow": preferred_workflow,
                    # Guest execution currently rejects all optional Skills.
                    # Advertising the policy lets every frontend stay generic.
                    "guest_selectable": False,
                }
            )
        return options

    @staticmethod
    def _domain_matches(manifest: SkillManifest, profile: AgentProfile) -> bool:
        return skill_domain_matches(
            manifest.domain,
            agent_code=profile.code,
            agent_domain=profile.domain,
        )

    @staticmethod
    def _dependency_closure(
        root: SkillManifest,
        manifests: dict[str, SkillManifest],
    ) -> tuple[SkillManifest, ...] | None:
        resolved: dict[str, SkillManifest] = {}
        visiting: set[str] = set()

        def visit(manifest: SkillManifest) -> bool:
            if manifest.name in visiting:
                return False
            if manifest.name in resolved:
                return True
            visiting.add(manifest.name)
            for dependency_name in manifest.dependencies:
                dependency = manifests.get(dependency_name)
                if dependency is None or not visit(dependency):
                    return False
            visiting.remove(manifest.name)
            resolved[manifest.name] = manifest
            return True

        return tuple(resolved.values()) if visit(root) else None

    @staticmethod
    def _has_conflict(
        closure: tuple[SkillManifest, ...],
        selected_skills: set[str],
        manifests: dict[str, SkillManifest],
    ) -> bool:
        if any(set(manifest.conflicts) & selected_skills for manifest in closure):
            return True
        closure_names = {manifest.name for manifest in closure}
        return any(
            bool(set(manifests[name].conflicts) & closure_names)
            for name in selected_skills
            if name in manifests
        )

    def _tools_reachable(
        self,
        *,
        profile: AgentProfile,
        workflow: str,
        selected_skills: set[str],
        declared_tools: set[str],
    ) -> bool:
        try:
            resolved = self._tool_registry.resolve_policy_tools(
                policy_name=profile.tool_policy,
                domain=profile.domain,
                workflow=workflow,
                selected_skills=selected_skills,
            )
        except (OSError, ValueError):
            return False
        return declared_tools <= {spec.name for spec in resolved}


__all__ = ["PublicSkillCatalog"]
