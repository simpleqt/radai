"""Application service for Skill upload, publishing, and lifecycle management."""

from __future__ import annotations

import asyncio
import json
import logging
import uuid
from datetime import datetime

from sqlalchemy import delete, select
from sqlalchemy.ext.asyncio import AsyncSession

from backend.application.skills.access_service import SkillAccessService
from backend.application.skills.materializer import SkillMaterializer
from backend.application.skills.package_validator import (
    SkillPackageValidationError,
    SkillPackageValidator,
    ValidatedSkillPackage,
)
from backend.db.crud.skill_crud import SkillCRUD
from backend.db.models.skill import ManagedSkill, SkillAcl, SkillVersion
from backend.services.skill_package_storage import SkillPackageStorage

logger = logging.getLogger(__name__)


class SkillManagementError(RuntimeError):
    def __init__(self, message: str, *, status_code: int = 400, code: str = "SKILL_ERROR"):
        super().__init__(message)
        self.status_code = status_code
        self.code = code


class SkillManagementService:
    def __init__(
        self,
        *,
        validator: SkillPackageValidator | None = None,
        storage: SkillPackageStorage | None = None,
        materializer: SkillMaterializer | None = None,
        access: SkillAccessService | None = None,
    ) -> None:
        self.validator = validator or SkillPackageValidator()
        self.storage = storage or SkillPackageStorage()
        self.materializer = materializer or SkillMaterializer(validator=self.validator)
        self.access = access or SkillAccessService()

    async def list_visible(
        self,
        session: AsyncSession,
        *,
        user_id: str,
        role: str,
        status: str | None = None,
        owner_user_id: str | None = None,
        limit: int | None = None,
        offset: int = 0,
    ) -> list[tuple[ManagedSkill, SkillVersion | None]]:
        skills = await SkillCRUD.list_visible_candidates(
            session,
            user_id=user_id,
            role=role,
            status=status,
            owner_user_id=owner_user_id,
            limit=limit,
            offset=offset,
        )
        versions_by_id: dict[str, SkillVersion] = {}
        active_ids = [skill.active_version_id for skill in skills if skill.active_version_id]
        if active_ids:
            result = await session.execute(
                select(SkillVersion).where(SkillVersion.version_id.in_(active_ids))
            )
            versions_by_id = {row.version_id: row for row in result.scalars().all()}
        return [
            (skill, versions_by_id.get(skill.active_version_id))
            for skill in skills
        ]

    async def get_visible(
        self,
        session: AsyncSession,
        *,
        skill_id: str,
        user_id: str,
        role: str,
    ) -> tuple[ManagedSkill, SkillVersion | None]:
        skill = await SkillCRUD.get_skill(session, skill_id)
        if skill is None:
            raise SkillManagementError(
                "Skill 不存在",
                status_code=404,
                code="SKILL_NOT_FOUND",
            )
        if not await self.access.has_permission(
            session,
            skill=skill,
            user_id=user_id,
            role=role,
            permission="view",
        ):
            raise SkillManagementError(
                "Skill 不存在",
                status_code=404,
                code="SKILL_NOT_FOUND",
            )
        version = (
            await SkillCRUD.get_version(session, skill.active_version_id)
            if skill.active_version_id
            else None
        )
        return skill, version

    async def list_visible_versions(
        self,
        session: AsyncSession,
        *,
        skill_id: str,
        user_id: str,
        role: str,
        limit: int | None = None,
        offset: int = 0,
    ) -> list[SkillVersion]:
        skill = await SkillCRUD.get_skill(session, skill_id)
        if skill is None:
            raise SkillManagementError(
                "Skill 不存在",
                status_code=404,
                code="SKILL_NOT_FOUND",
            )
        if not await self.access.has_permission(
            session,
            skill=skill,
            user_id=user_id,
            role=role,
            permission="view",
        ):
            raise SkillManagementError(
                "无查看权限",
                status_code=403,
                code="SKILL_ACCESS_DENIED",
            )
        return await SkillCRUD.list_versions(
            session,
            skill_id,
            limit=limit,
            offset=offset,
        )

    async def upload(
        self,
        session: AsyncSession,
        *,
        content: bytes,
        visibility: str,
        user_id: str,
        role: str,
    ) -> tuple[ManagedSkill, SkillVersion, ValidatedSkillPackage]:
        if visibility not in {"public", "private", "restricted"}:
            raise SkillManagementError("visibility 必须是 public/private/restricted")
        if visibility == "public" and role.lower() != "admin":
            raise SkillManagementError(
                "只有管理员可以创建公开 Skill",
                status_code=403,
                code="SKILL_ACCESS_DENIED",
            )
        try:
            package = await asyncio.to_thread(self.validator.validate, content)
        except SkillPackageValidationError as exc:
            raise SkillManagementError(str(exc), code=exc.code) from exc

        skill = await SkillCRUD.get_by_key(session, package.skill_key)
        if skill is None:
            skill = ManagedSkill(
                skill_key=package.skill_key,
                display_name=package.display_name,
                description=package.description,
                display_description=package.description,
                domain=package.domain,
                source_type="uploaded",
                visibility=visibility,
                owner_user_id=user_id,
                status="draft",
                created_by=user_id,
            )
            session.add(skill)
            await session.flush()
        else:
            if skill.source_type == "builtin" and role.lower() != "admin":
                raise SkillManagementError(
                    "内置 Skill 只能由管理员更新",
                    status_code=403,
                    code="SKILL_ACCESS_DENIED",
                )
            if not await self.access.has_permission(
                session,
                skill=skill,
                user_id=user_id,
                role=role,
                permission="edit",
            ):
                raise SkillManagementError(
                    f"Skill key 已存在且无编辑权限: {package.skill_key}",
                    status_code=409,
                    code="SKILL_KEY_ALREADY_EXISTS",
                )
            if skill.domain != package.domain:
                raise SkillManagementError(
                    "新版本 domain 不能改变",
                    status_code=409,
                    code="SKILL_DOMAIN_MISMATCH",
                )
            skill.description = package.description
            skill.display_name = package.display_name
            skill.display_description = package.description
            skill.visibility = visibility

        duplicate = await session.execute(
            select(SkillVersion.version_id).where(
                SkillVersion.skill_id == skill.skill_id,
                (SkillVersion.version == package.version)
                | (SkillVersion.package_sha256 == package.package_sha256),
            )
        )
        if duplicate.first():
            raise SkillManagementError(
                "相同版本或相同内容已经上传",
                status_code=409,
                code="SKILL_VERSION_EXISTS",
            )

        version_id = str(uuid.uuid4())
        object_name = (
            f"skills/packages/{skill.skill_id}/{version_id}/{package.skill_key}-{package.version}.zip"
        )
        # zip 上传 MinIO 是秒级网络 IO：先提交当前状态归还池连接，期间不钉
        # 住 MySQL 连接（expire_on_commit=False，已加载对象仍可用）。
        await session.commit()
        await self.storage.upload(object_name=object_name, content=content)
        manifest = dict(package.manifest)
        manifest["package_root_prefix"] = package.root_prefix
        version = SkillVersion(
            version_id=version_id,
            skill_id=skill.skill_id,
            version=package.version,
            status="validated",
            package_bucket=self.storage.bucket,
            package_object=object_name,
            package_sha256=package.package_sha256,
            package_size=package.package_size,
            manifest_json=manifest,
            file_index_json=package.file_index,
            created_by=user_id,
        )
        session.add(version)
        SkillCRUD.audit(
            session,
            actor_user_id=user_id,
            action="upload",
            skill_id=skill.skill_id,
            version_id=version.version_id,
            detail={"version": package.version, "sha256": package.package_sha256},
        )
        await session.commit()
        await session.refresh(skill)
        await session.refresh(version)
        return skill, version, package

    async def submit(
        self,
        session: AsyncSession,
        *,
        skill: ManagedSkill,
        version: SkillVersion,
        user_id: str,
        role: str,
    ) -> None:
        await self._require(session, skill, user_id=user_id, role=role, permission="edit")
        if version.skill_id != skill.skill_id or version.status not in {"validated", "rejected"}:
            raise SkillManagementError("当前版本不能提交审核", status_code=409)
        version.status = "pending_review"
        version.review_comment = None
        SkillCRUD.audit(
            session,
            actor_user_id=user_id,
            action="submit",
            skill_id=skill.skill_id,
            version_id=version.version_id,
        )
        await session.commit()

    async def publish(
        self,
        session: AsyncSession,
        *,
        skill: ManagedSkill,
        version: SkillVersion,
        user_id: str,
        role: str,
    ) -> None:
        await self._require(session, skill, user_id=user_id, role=role, permission="manage")
        if version.skill_id != skill.skill_id:
            raise SkillManagementError("版本不属于当前 Skill", status_code=404)
        if version.status not in {"validated", "pending_review", "published"}:
            raise SkillManagementError("当前版本不能发布", status_code=409)

        content = await self.storage.download(
            bucket=version.package_bucket, object_name=version.package_object
        )
        package = await asyncio.to_thread(self.validator.validate, content)
        if package.package_sha256 != version.package_sha256:
            raise SkillManagementError("Skill 包摘要不一致", status_code=409)
        await asyncio.to_thread(
            self.materializer.publish,
            skill_key=skill.skill_key,
            content=content,
            root_prefix=str(version.manifest_json.get("package_root_prefix") or ""),
            owner_id=skill.owner_user_id,
        )
        try:
            now = datetime.utcnow()
            version.status = "published"
            version.reviewed_by = user_id
            version.published_at = now
            skill.active_version_id = version.version_id
            skill.status = "published"
            SkillCRUD.audit(
                session,
                actor_user_id=user_id,
                action="publish",
                skill_id=skill.skill_id,
                version_id=version.version_id,
                detail={"version": version.version},
            )
            await session.commit()
        except Exception:
            await asyncio.to_thread(self.materializer.rollback, skill.skill_key)
            raise
        else:
            await asyncio.to_thread(self.materializer.finalize, skill.skill_key)
        await self.notify_change(skill_id=skill.skill_id)

    async def download_url(
        self,
        session: AsyncSession,
        *,
        skill: ManagedSkill,
        version: SkillVersion,
        user_id: str,
        role: str,
    ) -> str:
        if not await self.access.has_permission(
            session,
            skill=skill,
            user_id=user_id,
            role=role,
            permission="view",
        ):
            raise SkillManagementError(
                "无下载权限",
                status_code=403,
                code="SKILL_ACCESS_DENIED",
            )
        if version.skill_id != skill.skill_id:
            raise SkillManagementError("版本不属于当前 Skill", status_code=404)
        url = await self.storage.presigned_download(
            bucket=version.package_bucket,
            object_name=version.package_object,
        )
        if not url:
            raise SkillManagementError(
                "无法生成下载地址",
                status_code=503,
                code="SKILL_STORAGE_UNAVAILABLE",
            )
        return url

    async def reject(
        self,
        session: AsyncSession,
        *,
        skill: ManagedSkill,
        version: SkillVersion,
        user_id: str,
        role: str,
        comment: str,
    ) -> None:
        await self._require(session, skill, user_id=user_id, role=role, permission="manage")
        if version.skill_id != skill.skill_id:
            raise SkillManagementError("版本不属于当前 Skill", status_code=404)
        version.status = "rejected"
        version.reviewed_by = user_id
        version.review_comment = comment
        SkillCRUD.audit(
            session,
            actor_user_id=user_id,
            action="reject",
            skill_id=skill.skill_id,
            version_id=version.version_id,
            detail={"comment": comment},
        )
        await session.commit()

    async def set_enabled(
        self,
        session: AsyncSession,
        *,
        skill: ManagedSkill,
        enabled: bool,
        user_id: str,
        role: str,
    ) -> None:
        await self._require(session, skill, user_id=user_id, role=role, permission="manage")
        if enabled and not skill.active_version_id:
            raise SkillManagementError("没有活动版本，无法启用", status_code=409)
        skill.status = "published" if enabled else "disabled"
        SkillCRUD.audit(
            session,
            actor_user_id=user_id,
            action="enable" if enabled else "disable",
            skill_id=skill.skill_id,
        )
        await session.commit()
        await self.notify_change(skill_id=skill.skill_id)

    async def archive(
        self,
        session: AsyncSession,
        *,
        skill: ManagedSkill,
        user_id: str,
        role: str,
    ) -> None:
        await self._require(session, skill, user_id=user_id, role=role, permission="manage")
        skill.status = "archived"
        SkillCRUD.audit(
            session,
            actor_user_id=user_id,
            action="archive",
            skill_id=skill.skill_id,
        )
        await session.commit()
        await self.notify_change(skill_id=skill.skill_id)

    async def replace_grants(
        self,
        session: AsyncSession,
        *,
        skill: ManagedSkill,
        subject_type: str,
        subject_id: str,
        permissions: list[str],
        effect: str,
        expires_at: datetime | None,
        user_id: str,
        role: str,
    ) -> list[SkillAcl]:
        await self._require(session, skill, user_id=user_id, role=role, permission="manage")
        if subject_type not in {"user", "role"} or effect not in {"allow", "deny"}:
            raise SkillManagementError("无效的授权主体或 effect")
        normalized = sorted(set(permissions))
        if not normalized or set(normalized) - {"view", "use", "edit", "manage"}:
            raise SkillManagementError("permissions 包含无效值")
        await session.execute(
            delete(SkillAcl).where(
                SkillAcl.skill_id == skill.skill_id,
                SkillAcl.subject_type == subject_type,
                SkillAcl.subject_id == subject_id,
            )
        )
        rows = []
        for permission in normalized:
            row = SkillAcl(
                skill_id=skill.skill_id,
                subject_type=subject_type,
                subject_id=subject_id,
                permission=permission,
                effect=effect,
                expires_at=expires_at,
                created_by=user_id,
            )
            session.add(row)
            rows.append(row)
        SkillCRUD.audit(
            session,
            actor_user_id=user_id,
            action="grant",
            skill_id=skill.skill_id,
            target_type=subject_type,
            target_id=subject_id,
            detail={"permissions": normalized, "effect": effect},
        )
        await session.commit()
        await self.notify_change(skill_id=skill.skill_id)
        return rows

    async def _require(
        self,
        session: AsyncSession,
        skill: ManagedSkill,
        *,
        user_id: str,
        role: str,
        permission: str,
    ) -> None:
        allowed = await self.access.has_permission(
            session,
            skill=skill,
            user_id=user_id,
            role=role,
            permission=permission,
        )
        if not allowed:
            raise SkillManagementError(
                f"缺少 Skill {permission} 权限",
                status_code=403,
                code="SKILL_ACCESS_DENIED",
            )

    async def notify_change(self, *, skill_id: str) -> None:
        from backend.core.skills.scoped_skills_provider import invalidate_all_skill_caches

        invalidate_all_skill_caches()
        try:
            from backend.db.redis import get_redis

            client = get_redis()
            try:
                await client.publish(
                    "skill:catalog:changed",
                    json.dumps({"event": "skill.catalog.changed", "skill_id": skill_id}),
                )
            finally:
                await client.aclose()
        except Exception as exc:
            logger.warning("Skill cache invalidation publish failed: %s", exc)
