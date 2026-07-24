"""Skill permission evaluation."""

from __future__ import annotations

from datetime import datetime

from sqlalchemy import or_, select
from sqlalchemy.ext.asyncio import AsyncSession

from backend.db.models.skill import ManagedSkill, SkillAcl


_IMPLIED = {
    "view": {"view", "use", "edit", "manage"},
    "use": {"use", "edit", "manage"},
    "edit": {"edit", "manage"},
    "manage": {"manage"},
}


class SkillAccessService:
    async def allowed_skill_ids(
        self,
        session: AsyncSession,
        *,
        skills: list[ManagedSkill],
        user_id: str,
        role: str,
        permission: str,
    ) -> set[str]:
        """Evaluate one permission for many skills with a single ACL query."""
        if not skills:
            return set()
        normalized_role = str(role or "user").lower()
        if normalized_role == "admin":
            return {skill.skill_id for skill in skills}
        accepted = _IMPLIED.get(permission)
        if accepted is None:
            return set()

        skill_ids = [skill.skill_id for skill in skills]
        result = await session.execute(
            select(SkillAcl).where(
                SkillAcl.skill_id.in_(skill_ids),
                SkillAcl.permission.in_(accepted),
                or_(SkillAcl.expires_at.is_(None), SkillAcl.expires_at > datetime.utcnow()),
                or_(
                    (SkillAcl.subject_type == "user")
                    & (SkillAcl.subject_id == user_id),
                    (SkillAcl.subject_type == "role")
                    & (SkillAcl.subject_id == normalized_role),
                ),
            )
        )
        rows_by_skill: dict[str, list[SkillAcl]] = {
            skill_id: [] for skill_id in skill_ids
        }
        for row in result.scalars().all():
            rows_by_skill.setdefault(row.skill_id, []).append(row)

        allowed: set[str] = set()
        for skill in skills:
            rows = rows_by_skill.get(skill.skill_id, [])
            user_rows = [row for row in rows if row.subject_type == "user"]
            role_rows = [row for row in rows if row.subject_type == "role"]
            if any(row.effect == "deny" for row in user_rows):
                continue
            if any(row.effect == "allow" for row in user_rows):
                allowed.add(skill.skill_id)
                continue
            if any(row.effect == "deny" for row in role_rows):
                continue
            if any(row.effect == "allow" for row in role_rows):
                allowed.add(skill.skill_id)
                continue
            if skill.owner_user_id == user_id and permission in {"view", "use", "edit"}:
                allowed.add(skill.skill_id)
                continue
            if skill.visibility == "public" and permission in {"view", "use"}:
                allowed.add(skill.skill_id)
        return allowed

    async def has_permission(
        self,
        session: AsyncSession,
        *,
        skill: ManagedSkill,
        user_id: str,
        role: str,
        permission: str,
    ) -> bool:
        allowed = await self.allowed_skill_ids(
            session,
            skills=[skill],
            user_id=user_id,
            role=role,
            permission=permission,
        )
        return skill.skill_id in allowed
