"""Async CRUD helpers for the managed Skill catalog."""

from __future__ import annotations

from datetime import datetime

from sqlalchemy import delete, or_, select
from sqlalchemy.ext.asyncio import AsyncSession

from backend.db.models.skill import (
    AgentSkillBinding,
    ManagedSkill,
    SkillAcl,
    SkillAuditLog,
    SkillVersion,
)


class SkillCRUD:
    @staticmethod
    async def get_skill(session: AsyncSession, skill_id: str) -> ManagedSkill | None:
        return await session.get(ManagedSkill, skill_id)

    @staticmethod
    async def get_by_key(session: AsyncSession, skill_key: str) -> ManagedSkill | None:
        result = await session.execute(
            select(ManagedSkill).where(ManagedSkill.skill_key == skill_key)
        )
        return result.scalar_one_or_none()

    @staticmethod
    async def list_visible_candidates(
        session: AsyncSession,
        *,
        user_id: str,
        role: str,
        status: str | None = None,
        owner_user_id: str | None = None,
        limit: int | None = None,
        offset: int = 0,
    ) -> list[ManagedSkill]:
        stmt = select(ManagedSkill).order_by(
            ManagedSkill.updated_at.desc(), ManagedSkill.skill_key
        )
        if status:
            stmt = stmt.where(ManagedSkill.status == status)
        if owner_user_id:
            stmt = stmt.where(ManagedSkill.owner_user_id == owner_user_id)
        if role.lower() != "admin":
            acl_exists = select(SkillAcl.id).where(
                SkillAcl.skill_id == ManagedSkill.skill_id,
                SkillAcl.effect == "allow",
                SkillAcl.permission.in_(["view", "use", "edit", "manage"]),
                or_(
                    (SkillAcl.subject_type == "user")
                    & (SkillAcl.subject_id == user_id),
                    (SkillAcl.subject_type == "role")
                    & (SkillAcl.subject_id == role.lower()),
                ),
                or_(SkillAcl.expires_at.is_(None), SkillAcl.expires_at > datetime.utcnow()),
            )
            deny_exists = select(SkillAcl.id).where(
                SkillAcl.skill_id == ManagedSkill.skill_id,
                SkillAcl.effect == "deny",
                SkillAcl.permission.in_(["view", "use", "edit", "manage"]),
                or_(
                    (SkillAcl.subject_type == "user")
                    & (SkillAcl.subject_id == user_id),
                    (SkillAcl.subject_type == "role")
                    & (SkillAcl.subject_id == role.lower()),
                ),
                or_(SkillAcl.expires_at.is_(None), SkillAcl.expires_at > datetime.utcnow()),
            )
            stmt = stmt.where(
                or_(
                    ManagedSkill.visibility == "public",
                    ManagedSkill.owner_user_id == user_id,
                    acl_exists.exists(),
                ),
                ~deny_exists.exists(),
            )
        if offset > 0:
            stmt = stmt.offset(offset)
        if limit is not None:
            stmt = stmt.limit(max(int(limit), 1))
        result = await session.execute(stmt)
        return list(result.scalars().all())

    @staticmethod
    async def get_version(
        session: AsyncSession, version_id: str
    ) -> SkillVersion | None:
        return await session.get(SkillVersion, version_id)

    @staticmethod
    async def list_versions(
        session: AsyncSession,
        skill_id: str,
        *,
        limit: int | None = None,
        offset: int = 0,
    ) -> list[SkillVersion]:
        stmt = (
            select(SkillVersion)
            .where(SkillVersion.skill_id == skill_id)
            .order_by(SkillVersion.created_at.desc())
        )
        if offset > 0:
            stmt = stmt.offset(offset)
        if limit is not None:
            stmt = stmt.limit(max(int(limit), 1))
        result = await session.execute(stmt)
        return list(result.scalars().all())

    @staticmethod
    async def list_acl(session: AsyncSession, skill_id: str) -> list[SkillAcl]:
        result = await session.execute(
            select(SkillAcl)
            .where(SkillAcl.skill_id == skill_id)
            .order_by(SkillAcl.subject_type, SkillAcl.subject_id, SkillAcl.permission)
        )
        return list(result.scalars().all())

    @staticmethod
    async def delete_acl(session: AsyncSession, skill_id: str, grant_id: int) -> int:
        result = await session.execute(
            delete(SkillAcl).where(
                SkillAcl.skill_id == skill_id, SkillAcl.id == grant_id
            )
        )
        return int(result.rowcount or 0)

    @staticmethod
    async def list_bindings(
        session: AsyncSession, agent_code: str
    ) -> list[tuple[AgentSkillBinding, ManagedSkill]]:
        result = await session.execute(
            select(AgentSkillBinding, ManagedSkill)
            .join(ManagedSkill, ManagedSkill.skill_id == AgentSkillBinding.skill_id)
            .where(AgentSkillBinding.agent_code == agent_code)
            .order_by(AgentSkillBinding.sort_order, AgentSkillBinding.id)
        )
        return list(result.all())

    @staticmethod
    async def replace_bindings(
        session: AsyncSession,
        *,
        agent_code: str,
        bindings: list[dict],
        actor_user_id: str,
    ) -> None:
        await session.execute(
            delete(AgentSkillBinding).where(
                AgentSkillBinding.agent_code == agent_code
            )
        )
        for item in bindings:
            session.add(
                AgentSkillBinding(
                    agent_code=agent_code,
                    skill_id=item["skill_id"],
                    is_enabled=item.get("is_enabled", True),
                    is_required=item.get("is_required", False),
                    default_selected=item.get("default_selected", False),
                    is_selectable=item.get("is_selectable", True),
                    sort_order=item.get("sort_order", 0),
                    created_by=actor_user_id,
                )
            )
        await session.flush()

    @staticmethod
    def audit(
        session: AsyncSession,
        *,
        actor_user_id: str,
        action: str,
        skill_id: str | None = None,
        version_id: str | None = None,
        target_type: str | None = None,
        target_id: str | None = None,
        detail: dict | None = None,
    ) -> None:
        session.add(
            SkillAuditLog(
                skill_id=skill_id,
                version_id=version_id,
                actor_user_id=actor_user_id,
                action=action,
                target_type=target_type,
                target_id=target_id,
                detail_json=detail,
            )
        )
