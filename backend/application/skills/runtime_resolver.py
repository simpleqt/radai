"""Resolve effective Skills for a user, agent, and workflow."""

from __future__ import annotations

import logging
from dataclasses import dataclass

from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from backend.application.skills.access_service import SkillAccessService
from backend.application.skills.domain_policy import skill_domain_matches
from backend.db.crud.skill_crud import SkillCRUD
from backend.db.models import User
from backend.db.models.skill import ManagedSkill, SkillVersion

logger = logging.getLogger(__name__)


class SkillRuntimeError(ValueError):
    def __init__(self, message: str, *, code: str, status_code: int = 409):
        super().__init__(message)
        self.code = code
        self.status_code = status_code


@dataclass(frozen=True)
class RuntimeSkillOption:
    skill_id: str
    name: str
    label: str
    description: str
    version: str
    required: bool
    default_selected: bool
    selectable: bool
    allowed_workflows: list[str]
    preferred_workflow: str | None
    guest_selectable: bool


@dataclass(frozen=True)
class RuntimeSkillResolution:
    selected_skills: list[str]
    available_skills: list[RuntimeSkillOption]
    managed: bool


class SkillRuntimeResolver:
    def __init__(self, *, access: SkillAccessService | None = None) -> None:
        self.access = access or SkillAccessService()

    async def resolve(
        self,
        session: AsyncSession,
        *,
        user_id: str,
        agent_code: str,
        agent_domain: str,
        workflow: str | None,
        requested_skills: list[str] | None,
        fallback_skills: list[str],
        role: str | None = None,
    ) -> RuntimeSkillResolution:
        effective_role = role or await self._load_role(session, user_id)
        bindings = await SkillCRUD.list_bindings(session, agent_code)
        if not bindings:
            requested = fallback_skills if requested_skills is None else requested_skills
            managed_requested = await self._managed_keys(session, requested)
            if managed_requested:
                raise SkillRuntimeError(
                    f"Skill 未绑定当前智能体: {', '.join(sorted(managed_requested))}",
                    code="SKILL_NOT_BOUND_TO_AGENT",
                )
            return RuntimeSkillResolution(
                selected_skills=self._dedupe(requested),
                available_skills=[],
                managed=False,
            )

        version_ids = {
            skill.active_version_id
            for binding, skill in bindings
            if binding.is_enabled
            and skill.status == "published"
            and skill.active_version_id
        }
        version_rows = await session.execute(
            select(SkillVersion).where(SkillVersion.version_id.in_(version_ids))
        )
        versions = {
            version.version_id: version for version in version_rows.scalars().all()
        }

        candidates: list[tuple] = []
        for binding, skill in bindings:
            if not binding.is_enabled or skill.status != "published" or not skill.active_version_id:
                continue
            version = versions.get(skill.active_version_id)
            if version is None or version.status != "published":
                continue
            if not self._domain_matches(skill.domain, agent_code, agent_domain):
                continue
            manifest = dict(version.manifest_json or {})
            allowed_workflows = list(manifest.get("allowed_workflows") or [])
            preferred_workflow = (
                str(manifest.get("preferred_workflow") or "").strip() or None
            )
            if workflow and allowed_workflows and workflow not in allowed_workflows:
                continue
            candidates.append((binding, skill, version))

        allowed_skill_ids = await self.access.allowed_skill_ids(
            session,
            skills=[skill for _binding, skill, _version in candidates],
            user_id=user_id,
            role=effective_role,
            permission="use",
        )
        options: list[RuntimeSkillOption] = []
        eligible: dict[str, tuple] = {}
        for binding, skill, version in candidates:
            if skill.skill_id not in allowed_skill_ids:
                continue
            manifest = dict(version.manifest_json or {})
            allowed_workflows = list(manifest.get("allowed_workflows") or [])
            preferred_workflow = (
                str(manifest.get("preferred_workflow") or "").strip() or None
            )
            eligible[skill.skill_key] = (binding, skill, version)
            if binding.is_selectable or binding.is_required:
                options.append(
                    RuntimeSkillOption(
                        skill_id=skill.skill_id,
                        name=skill.skill_key,
                        label=skill.display_name,
                        description=skill.display_description or skill.description,
                        version=version.version,
                        required=bool(binding.is_required),
                        default_selected=bool(binding.default_selected),
                        selectable=bool(binding.is_selectable),
                        allowed_workflows=allowed_workflows,
                        preferred_workflow=preferred_workflow,
                        # Guest requests intentionally cannot activate optional
                        # Skills.  Keep this as response metadata so clients do
                        # not need to know any Skill key to enforce that policy.
                        guest_selectable=False,
                    )
                )

        required = [
            key for key, (binding, _skill, _version) in eligible.items() if binding.is_required
        ]
        if requested_skills is None:
            requested = [
                key
                for key, (binding, _skill, _version) in eligible.items()
                if binding.default_selected
            ]
        else:
            requested = self._dedupe(requested_skills)
            missing = [key for key in requested if key not in eligible]
            if missing:
                await self._raise_unavailable(session, missing, agent_code=agent_code)
        selected = self._dedupe([*required, *requested])
        selected_set = set(selected)
        dependency_errors: dict[str, list[str]] = {}
        conflict_errors: dict[str, list[str]] = {}
        for key in selected:
            _binding, _skill, version = eligible[key]
            manifest = dict(version.manifest_json or {})
            dependencies = {
                str(item).strip()
                for item in (manifest.get("dependencies") or [])
                if str(item or "").strip()
            }
            conflicts = {
                str(item).strip()
                for item in (manifest.get("conflicts") or [])
                if str(item or "").strip()
            }
            missing_dependencies = sorted(dependencies - selected_set)
            active_conflicts = sorted(conflicts & selected_set)
            if missing_dependencies:
                dependency_errors[key] = missing_dependencies
            if active_conflicts:
                conflict_errors[key] = active_conflicts
        if dependency_errors:
            raise SkillRuntimeError(
                f"Skill 依赖未选择: {dependency_errors}",
                code="SKILL_DEPENDENCY_MISSING",
            )
        if conflict_errors:
            raise SkillRuntimeError(
                f"Skill 之间存在冲突: {conflict_errors}",
                code="SKILL_CONFLICT",
            )
        return RuntimeSkillResolution(
            selected_skills=selected,
            available_skills=options,
            managed=True,
        )

    async def _raise_unavailable(
        self, session: AsyncSession, skill_keys: list[str], *, agent_code: str
    ) -> None:
        result = await session.execute(
            select(ManagedSkill).where(ManagedSkill.skill_key.in_(skill_keys))
        )
        found = {row.skill_key: row for row in result.scalars().all()}
        unknown = [key for key in skill_keys if key not in found]
        if unknown:
            raise SkillRuntimeError(
                f"Skill 不存在: {', '.join(unknown)}",
                code="SKILL_NOT_FOUND",
                status_code=404,
            )
        disabled = [key for key in skill_keys if found[key].status != "published"]
        if disabled:
            raise SkillRuntimeError(
                f"Skill 未发布或已禁用: {', '.join(disabled)}",
                code="SKILL_NOT_PUBLISHED",
            )
        raise SkillRuntimeError(
            f"Skill 无权使用、与 workflow 不兼容或未绑定智能体 {agent_code}: {', '.join(skill_keys)}",
            code="SKILL_ACCESS_DENIED",
            status_code=403,
        )

    async def _managed_keys(self, session: AsyncSession, keys: list[str]) -> set[str]:
        if not keys:
            return set()
        result = await session.execute(
            select(ManagedSkill.skill_key).where(ManagedSkill.skill_key.in_(keys))
        )
        return {str(row[0]) for row in result.all()}

    async def _load_role(self, session: AsyncSession, user_id: str) -> str:
        if not user_id:
            return "user"
        result = await session.execute(select(User.role).where(User.user_id == user_id))
        return str(result.scalar_one_or_none() or "user").lower()

    @staticmethod
    def _domain_matches(skill_domain: str, agent_code: str, agent_domain: str) -> bool:
        return skill_domain_matches(
            skill_domain,
            agent_code=agent_code,
            agent_domain=agent_domain,
        )

    @staticmethod
    def _dedupe(items: list[str]) -> list[str]:
        seen: set[str] = set()
        result: list[str] = []
        for item in items:
            value = str(item or "").strip()
            if value and value not in seen:
                seen.add(value)
                result.append(value)
        return result
