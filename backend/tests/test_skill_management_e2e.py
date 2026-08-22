from __future__ import annotations

import io
import zipfile
from unittest.mock import AsyncMock

import httpx
import pytest
from fastapi import FastAPI
from sqlalchemy.ext.asyncio import async_sessionmaker, create_async_engine

from backend.api.dependencies.auth import ApiKeyAuth, require_admin, verify_api_key
from backend.api.routers import agent_skills as agent_skills_router
from backend.api.routers import skills as skills_router
from backend.application.skills.management_service import SkillManagementService
from backend.application.skills.materializer import SkillMaterializer
from backend.application.skills.runtime_resolver import SkillRuntimeResolver
from backend.core.skills.native_skill_tools import create_native_skill_tools
from backend.core.skills.scoped_skills_provider import ScopedSkillsProvider
from backend.db.models import User
from backend.db.models.skill import (
    AgentSkillBinding,
    ManagedSkill,
    SkillAcl,
    SkillAuditLog,
    SkillVersion,
)


class _MemorySkillStorage:
    bucket = "test-skills"

    def __init__(self) -> None:
        self.objects: dict[str, bytes] = {}

    async def upload(self, *, object_name: str, content: bytes) -> None:
        self.objects[object_name] = content

    async def download(self, *, bucket: str, object_name: str) -> bytes:
        assert bucket == self.bucket
        return self.objects[object_name]


def _skill_zip(
    *,
    name: str = "e2e-managed-skill",
    domain: str = "general",
) -> bytes:
    skill_md = (
        "---\n"
        f"name: {name}\n"
        "version: 1.0.0\n"
        "description: End-to-end managed Skill\n"
        f"domain: {domain}\n"
        "workflows:\n"
        "  allowed: [full]\n"
        "  preferred: full\n"
        "---\n"
        "# End-to-end managed Skill\n\n"
        "Always return evidence with the answer.\n"
    )
    buffer = io.BytesIO()
    with zipfile.ZipFile(buffer, "w", zipfile.ZIP_DEFLATED) as archive:
        archive.writestr(f"{name}/SKILL.md", skill_md)
    return buffer.getvalue()


@pytest.mark.asyncio
async def test_upload_publish_bind_grant_request_and_read_skill(
    monkeypatch,
    tmp_path,
):
    engine = create_async_engine("sqlite+aiosqlite:///:memory:")
    async with engine.begin() as connection:
        for table in (
            User.__table__,
            ManagedSkill.__table__,
            SkillVersion.__table__,
            SkillAcl.__table__,
            AgentSkillBinding.__table__,
            SkillAuditLog.__table__,
        ):
            await connection.run_sync(table.create)
    session_factory = async_sessionmaker(engine, expire_on_commit=False)

    admin = ApiKeyAuth(api_key="admin-key", user_id="admin-user", role="admin")
    async with session_factory() as session:
        session.add_all(
            [
                User(
                    user_id=admin.user_id,
                    username="skill-admin",
                    api_key=admin.api_key,
                    role=admin.role,
                    is_active=True,
                ),
                User(
                    user_id="runtime-user",
                    username="runtime-user",
                    api_key="runtime-key",
                    role="user",
                    is_active=True,
                ),
            ]
        )
        await session.commit()

    storage = _MemorySkillStorage()
    service = SkillManagementService(
        storage=storage,
        materializer=SkillMaterializer(root=tmp_path),
    )
    service.notify_change = AsyncMock()
    monkeypatch.setattr(skills_router, "SkillManagementService", lambda: service)
    monkeypatch.setattr(
        skills_router, "get_async_session_factory", lambda: session_factory
    )
    monkeypatch.setattr(
        agent_skills_router, "get_async_session_factory", lambda: session_factory
    )

    app = FastAPI()
    app.include_router(skills_router.router, prefix="/api/v1")
    app.include_router(agent_skills_router.router, prefix="/api/v1/agents")
    app.dependency_overrides[verify_api_key] = lambda: admin
    app.dependency_overrides[require_admin] = lambda: admin

    transport = httpx.ASGITransport(app=app)
    try:
        async with httpx.AsyncClient(
            transport=transport,
            base_url="http://test",
        ) as client:
            upload = await client.post(
                "/api/v1/skills/uploads",
                data={"visibility": "private"},
                files={
                    "file": (
                        "e2e-managed-skill.zip",
                        _skill_zip(),
                        "application/zip",
                    )
                },
            )
            assert upload.status_code == 201, upload.text
            uploaded = upload.json()
            skill_id = uploaded["skill_id"]
            version_id = uploaded["version"]["version_id"]

            publish = await client.post(
                f"/api/v1/skills/{skill_id}/versions/{version_id}/publish"
            )
            assert publish.status_code == 200, publish.text

            grant = await client.post(
                f"/api/v1/skills/{skill_id}/grants",
                json={
                    "subject_type": "user",
                    "subject_id": "runtime-user",
                    "permissions": ["view", "use"],
                    "effect": "allow",
                },
            )
            assert grant.status_code == 200, grant.text
            assert len(grant.json()["grant_ids"]) == 2

            binding_payload = {
                "skills": [
                    {
                        "skill_id": skill_id,
                        "is_enabled": True,
                        "is_required": False,
                        "default_selected": True,
                        "is_selectable": True,
                        "sort_order": 0,
                    }
                ]
            }
            binding = await client.put(
                "/api/v1/agents/ne/skills",
                json=binding_payload,
            )
            assert binding.status_code == 200, binding.text
            assert binding.json()["skills"][0]["skill_key"] == "e2e-managed-skill"

            listed = await client.get("/api/v1/agents/ne/skills")
            assert listed.status_code == 200, listed.text
            assert listed.json()["compatible_skill_ids"] == [skill_id]

            duplicate = await client.put(
                "/api/v1/agents/ne/skills",
                json={"skills": binding_payload["skills"] * 2},
            )
            assert duplicate.status_code == 400
            assert "重复" in duplicate.json()["detail"]

        async with session_factory() as session:
            resolution = await SkillRuntimeResolver().resolve(
                session,
                user_id="runtime-user",
                role="user",
                agent_code="ne",
                agent_domain="nuclear-electronics",
                workflow="full",
                requested_skills=["e2e-managed-skill"],
                fallback_skills=[],
            )

        assert resolution.selected_skills == ["e2e-managed-skill"]
        provider = ScopedSkillsProvider(
            skills_base_path=tmp_path,
            user_id="runtime-user",
            authorized_skill_names=set(resolution.selected_skills),
        )
        read_skill = next(
            tool
            for tool in create_native_skill_tools(
                provider,
                selected_skills=set(resolution.selected_skills),
            )
            if tool.name == "read_local_skill_file"
        )
        content = await read_skill.ainvoke({"skill_name": "e2e-managed-skill"})
        assert "# End-to-end managed Skill" in content
        assert "Always return evidence" in content
    finally:
        await engine.dispose()


@pytest.mark.asyncio
async def test_binding_api_rejects_incompatible_skill_domain(monkeypatch):
    engine = create_async_engine("sqlite+aiosqlite:///:memory:")
    async with engine.begin() as connection:
        for table in (
            ManagedSkill.__table__,
            AgentSkillBinding.__table__,
            SkillAuditLog.__table__,
        ):
            await connection.run_sync(table.create)
    session_factory = async_sessionmaker(engine, expire_on_commit=False)
    async with session_factory() as session:
        session.add(
            ManagedSkill(
                skill_id="foreign-domain-skill",
                skill_key="foreign-domain-skill",
                display_name="Foreign",
                description="Incompatible Skill",
                domain="groundwater",
                visibility="public",
                status="published",
                active_version_id="published-version",
                created_by="admin-user",
            )
        )
        await session.commit()

    admin = ApiKeyAuth(api_key="admin-key", user_id="admin-user", role="admin")
    monkeypatch.setattr(
        agent_skills_router, "get_async_session_factory", lambda: session_factory
    )
    app = FastAPI()
    app.include_router(agent_skills_router.router, prefix="/api/v1/agents")
    app.dependency_overrides[require_admin] = lambda: admin
    try:
        async with httpx.AsyncClient(
            transport=httpx.ASGITransport(app=app),
            base_url="http://test",
        ) as client:
            response = await client.put(
                "/api/v1/agents/ne/skills",
                json={"skills": [{"skill_id": "foreign-domain-skill"}]},
            )
        assert response.status_code == 409
        assert "domain" in response.json()["detail"]
    finally:
        await engine.dispose()
