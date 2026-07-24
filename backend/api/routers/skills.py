"""Managed Skill upload, lifecycle, and authorization APIs."""

from __future__ import annotations

from datetime import datetime
from typing import Any

from fastapi import APIRouter, Depends, File, Form, HTTPException, Query, UploadFile
from pydantic import BaseModel, Field
from sqlalchemy import select

from backend.api.dependencies.auth import ApiKeyAuth, verify_api_key
from backend.api.upload_utils import UploadTooLargeError, read_upload_with_limit
from backend.application.skills.management_service import (
    SkillManagementError,
    SkillManagementService,
)
from backend.application.skills.package_validator import MAX_PACKAGE_SIZE
from backend.db.connection import get_async_session_factory
from backend.db.crud.skill_crud import SkillCRUD
from backend.db.models import User

router = APIRouter(prefix="/skills", tags=["Skills 管理"])


class ReviewRequest(BaseModel):
    comment: str = Field(default="", max_length=2000)


class GrantRequest(BaseModel):
    subject_type: str
    subject_id: str
    permissions: list[str]
    effect: str = "allow"
    expires_at: datetime | None = None


def _error(exc: SkillManagementError) -> HTTPException:
    return HTTPException(
        status_code=exc.status_code,
        detail={"code": exc.code, "message": str(exc)},
    )


def _skill_payload(skill, *, version=None) -> dict[str, Any]:
    payload = {
        "skill_id": skill.skill_id,
        "skill_key": skill.skill_key,
        "display_name": skill.display_name,
        "description": skill.description,
        "display_description": getattr(skill, "display_description", "") or skill.description,
        "domain": skill.domain,
        "source_type": skill.source_type,
        "visibility": skill.visibility,
        "owner_user_id": skill.owner_user_id,
        "status": skill.status,
        "active_version_id": skill.active_version_id,
        "created_at": skill.created_at,
        "updated_at": skill.updated_at,
    }
    if version is not None:
        payload["active_version"] = _version_payload(version)
    return payload


def _version_payload(version) -> dict[str, Any]:
    return {
        "version_id": version.version_id,
        "skill_id": version.skill_id,
        "version": version.version,
        "status": version.status,
        "package_sha256": version.package_sha256,
        "package_size": version.package_size,
        "manifest": version.manifest_json,
        "file_index": version.file_index_json,
        "review_comment": version.review_comment,
        "created_by": version.created_by,
        "reviewed_by": version.reviewed_by,
        "created_at": version.created_at,
        "published_at": version.published_at,
    }


async def _load_skill_or_404(session, skill_id: str):
    skill = await SkillCRUD.get_skill(session, skill_id)
    if skill is None:
        raise HTTPException(status_code=404, detail={"code": "SKILL_NOT_FOUND", "message": "Skill 不存在"})
    return skill


async def _load_version_or_404(session, skill_id: str, version_id: str):
    version = await SkillCRUD.get_version(session, version_id)
    if version is None or version.skill_id != skill_id:
        raise HTTPException(status_code=404, detail={"code": "SKILL_VERSION_NOT_FOUND", "message": "Skill 版本不存在"})
    return version


@router.get("")
async def list_skills(
    status: str | None = None,
    owner_user_id: str | None = None,
    limit: int = Query(default=100, ge=1, le=500),
    offset: int = Query(default=0, ge=0),
    auth: ApiKeyAuth = Depends(verify_api_key),
):
    factory = get_async_session_factory()
    async with factory() as session:
        rows = await SkillManagementService().list_visible(
            session,
            user_id=auth.user_id,
            role=auth.role,
            status=status,
            owner_user_id=owner_user_id,
            limit=limit,
            offset=offset,
        )
        return {
            "total": len(rows),
            "limit": limit,
            "offset": offset,
            "skills": [
                _skill_payload(skill, version=version)
                for skill, version in rows
            ],
        }


@router.post("/uploads", status_code=201)
async def upload_skill(
    file: UploadFile = File(...),
    visibility: str = Form("private"),
    auth: ApiKeyAuth = Depends(verify_api_key),
):
    if not (file.filename or "").lower().endswith(".zip"):
        raise HTTPException(status_code=400, detail={"code": "INVALID_SKILL_PACKAGE", "message": "仅支持 ZIP 文件"})
    try:
        content = await read_upload_with_limit(file, MAX_PACKAGE_SIZE)
    except UploadTooLargeError as exc:
        raise HTTPException(
            status_code=413,
            detail={"code": "INVALID_SKILL_PACKAGE", "message": "Skill ZIP 超过 20MB 限制"},
        ) from exc
    factory = get_async_session_factory()
    async with factory() as session:
        try:
            skill, version, package = await SkillManagementService().upload(
                session,
                content=content,
                visibility=visibility,
                user_id=auth.user_id,
                role=auth.role,
            )
        except SkillManagementError as exc:
            raise _error(exc) from exc
    return {
        **_skill_payload(skill),
        "version": _version_payload(version),
        "validation": {
            "valid": True,
            "warnings": package.warnings,
            "tools": package.manifest.get("tools", []),
            "allowed_workflows": package.manifest.get("allowed_workflows", []),
        },
    }


@router.get("/{skill_id}")
async def get_skill(skill_id: str, auth: ApiKeyAuth = Depends(verify_api_key)):
    factory = get_async_session_factory()
    async with factory() as session:
        try:
            skill, version = await SkillManagementService().get_visible(
                session,
                skill_id=skill_id,
                user_id=auth.user_id,
                role=auth.role,
            )
        except SkillManagementError as exc:
            raise _error(exc) from exc
        return _skill_payload(skill, version=version)


@router.get("/{skill_id}/versions")
async def list_versions(
    skill_id: str,
    limit: int = Query(default=100, ge=1, le=500),
    offset: int = Query(default=0, ge=0),
    auth: ApiKeyAuth = Depends(verify_api_key),
):
    factory = get_async_session_factory()
    async with factory() as session:
        try:
            versions = await SkillManagementService().list_visible_versions(
                session,
                skill_id=skill_id,
                user_id=auth.user_id,
                role=auth.role,
                limit=limit,
                offset=offset,
            )
        except SkillManagementError as exc:
            raise _error(exc) from exc
        return {
            "total": len(versions),
            "limit": limit,
            "offset": offset,
            "versions": [_version_payload(row) for row in versions],
        }


@router.post("/{skill_id}/versions/{version_id}/submit")
async def submit_version(skill_id: str, version_id: str, auth: ApiKeyAuth = Depends(verify_api_key)):
    factory = get_async_session_factory()
    async with factory() as session:
        skill = await _load_skill_or_404(session, skill_id)
        version = await _load_version_or_404(session, skill_id, version_id)
        try:
            await SkillManagementService().submit(
                session, skill=skill, version=version, user_id=auth.user_id, role=auth.role
            )
        except SkillManagementError as exc:
            raise _error(exc) from exc
    return {"success": True, "status": "pending_review"}


@router.post("/{skill_id}/versions/{version_id}/publish")
@router.post("/{skill_id}/versions/{version_id}/activate")
async def publish_version(skill_id: str, version_id: str, auth: ApiKeyAuth = Depends(verify_api_key)):
    factory = get_async_session_factory()
    async with factory() as session:
        skill = await _load_skill_or_404(session, skill_id)
        version = await _load_version_or_404(session, skill_id, version_id)
        try:
            await SkillManagementService().publish(
                session, skill=skill, version=version, user_id=auth.user_id, role=auth.role
            )
        except SkillManagementError as exc:
            raise _error(exc) from exc
    return {"success": True, "status": "published", "active_version_id": version_id}


@router.post("/{skill_id}/versions/{version_id}/reject")
async def reject_version(
    skill_id: str,
    version_id: str,
    payload: ReviewRequest,
    auth: ApiKeyAuth = Depends(verify_api_key),
):
    factory = get_async_session_factory()
    async with factory() as session:
        skill = await _load_skill_or_404(session, skill_id)
        version = await _load_version_or_404(session, skill_id, version_id)
        try:
            await SkillManagementService().reject(
                session,
                skill=skill,
                version=version,
                user_id=auth.user_id,
                role=auth.role,
                comment=payload.comment,
            )
        except SkillManagementError as exc:
            raise _error(exc) from exc
    return {"success": True, "status": "rejected"}


@router.post("/{skill_id}/enable")
async def enable_skill(skill_id: str, auth: ApiKeyAuth = Depends(verify_api_key)):
    return await _set_enabled(skill_id, True, auth)


@router.post("/{skill_id}/disable")
async def disable_skill(skill_id: str, auth: ApiKeyAuth = Depends(verify_api_key)):
    return await _set_enabled(skill_id, False, auth)


async def _set_enabled(skill_id: str, enabled: bool, auth: ApiKeyAuth):
    factory = get_async_session_factory()
    async with factory() as session:
        skill = await _load_skill_or_404(session, skill_id)
        try:
            await SkillManagementService().set_enabled(
                session, skill=skill, enabled=enabled, user_id=auth.user_id, role=auth.role
            )
        except SkillManagementError as exc:
            raise _error(exc) from exc
    return {"success": True, "status": "published" if enabled else "disabled"}


@router.delete("/{skill_id}")
async def archive_skill(skill_id: str, auth: ApiKeyAuth = Depends(verify_api_key)):
    factory = get_async_session_factory()
    async with factory() as session:
        skill = await _load_skill_or_404(session, skill_id)
        try:
            await SkillManagementService().archive(
                session, skill=skill, user_id=auth.user_id, role=auth.role
            )
        except SkillManagementError as exc:
            raise _error(exc) from exc
    return {"success": True, "status": "archived"}


@router.get("/{skill_id}/grants")
async def list_grants(skill_id: str, auth: ApiKeyAuth = Depends(verify_api_key)):
    factory = get_async_session_factory()
    async with factory() as session:
        skill = await _load_skill_or_404(session, skill_id)
        service = SkillManagementService()
        try:
            await service._require(session, skill, user_id=auth.user_id, role=auth.role, permission="manage")
        except SkillManagementError as exc:
            raise _error(exc) from exc
        rows = await SkillCRUD.list_acl(session, skill_id)
        return {
            "grants": [
                {
                    "id": row.id,
                    "subject_type": row.subject_type,
                    "subject_id": row.subject_id,
                    "permission": row.permission,
                    "effect": row.effect,
                    "expires_at": row.expires_at,
                }
                for row in rows
            ]
        }


@router.post("/{skill_id}/grants")
async def replace_grants(
    skill_id: str,
    payload: GrantRequest,
    auth: ApiKeyAuth = Depends(verify_api_key),
):
    factory = get_async_session_factory()
    async with factory() as session:
        skill = await _load_skill_or_404(session, skill_id)
        if payload.subject_type == "user":
            exists = await session.execute(select(User.user_id).where(User.user_id == payload.subject_id))
            if exists.scalar_one_or_none() is None:
                raise HTTPException(status_code=404, detail={"code": "USER_NOT_FOUND", "message": "授权用户不存在"})
        elif payload.subject_type == "role" and payload.subject_id not in {"user", "admin"}:
            raise HTTPException(status_code=400, detail={"code": "INVALID_ROLE", "message": "未知角色"})
        try:
            rows = await SkillManagementService().replace_grants(
                session,
                skill=skill,
                subject_type=payload.subject_type,
                subject_id=payload.subject_id,
                permissions=payload.permissions,
                effect=payload.effect,
                expires_at=payload.expires_at,
                user_id=auth.user_id,
                role=auth.role,
            )
        except SkillManagementError as exc:
            raise _error(exc) from exc
    return {"success": True, "grant_ids": [row.id for row in rows]}


@router.delete("/{skill_id}/grants/{grant_id}")
async def delete_grant(skill_id: str, grant_id: int, auth: ApiKeyAuth = Depends(verify_api_key)):
    factory = get_async_session_factory()
    async with factory() as session:
        skill = await _load_skill_or_404(session, skill_id)
        service = SkillManagementService()
        try:
            await service._require(session, skill, user_id=auth.user_id, role=auth.role, permission="manage")
        except SkillManagementError as exc:
            raise _error(exc) from exc
        deleted = await SkillCRUD.delete_acl(session, skill_id, grant_id)
        if not deleted:
            raise HTTPException(status_code=404, detail={"code": "GRANT_NOT_FOUND", "message": "授权记录不存在"})
        SkillCRUD.audit(
            session,
            actor_user_id=auth.user_id,
            action="revoke",
            skill_id=skill_id,
            target_type="grant",
            target_id=str(grant_id),
        )
        await session.commit()
        await service.notify_change(skill_id=skill_id)
    return {"success": True}


@router.get("/{skill_id}/versions/{version_id}/download")
async def download_version(skill_id: str, version_id: str, auth: ApiKeyAuth = Depends(verify_api_key)):
    factory = get_async_session_factory()
    async with factory() as session:
        skill = await _load_skill_or_404(session, skill_id)
        version = await _load_version_or_404(session, skill_id, version_id)
        try:
            url = await SkillManagementService().download_url(
                session,
                skill=skill,
                version=version,
                user_id=auth.user_id,
                role=auth.role,
            )
        except SkillManagementError as exc:
            raise _error(exc) from exc
        return {"url": url, "expires_in": 600}
