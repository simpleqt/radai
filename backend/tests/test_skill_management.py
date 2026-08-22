from __future__ import annotations

import io
import zipfile

import pytest
from sqlalchemy.ext.asyncio import async_sessionmaker, create_async_engine

from backend.application.skills.package_validator import (
    SkillPackageValidationError,
    SkillPackageValidator,
)
from backend.application.skills.runtime_resolver import (
    SkillRuntimeError,
    SkillRuntimeResolver,
)
from backend.db.models import User
from backend.db.models.skill import (
    AgentSkillBinding,
    ManagedSkill,
    SkillAcl,
    SkillVersion,
)


def _zip(files: dict[str, str | bytes]) -> bytes:
    buffer = io.BytesIO()
    with zipfile.ZipFile(buffer, "w", zipfile.ZIP_DEFLATED) as archive:
        for path, content in files.items():
            archive.writestr(path, content)
    return buffer.getvalue()


def _skill_md(*, name: str = "uploaded-skill", version: str = "1.2.0") -> str:
    return (
        "---\n"
        f"name: {name}\n"
        f"version: {version}\n"
        "description: Uploaded test skill\n"
        "domain: nuclear-electronics\n"
        "workflows:\n"
        "  allowed: [full]\n"
        "  preferred: full\n"
        "---\n"
        "# Test\n"
    )


def test_skill_package_validator_accepts_single_root_directory():
    result = SkillPackageValidator().validate(
        _zip(
            {
                "uploaded-skill/SKILL.md": _skill_md(),
                "uploaded-skill/references/guide.md": "guide",
            }
        )
    )

    assert result.skill_key == "uploaded-skill"
    assert result.version == "1.2.0"
    assert result.root_prefix == "uploaded-skill/"
    assert {item["path"] for item in result.file_index} == {
        "SKILL.md",
        "references/guide.md",
    }


@pytest.mark.parametrize("name", ["数据分析", "skill-数据分析", "中文-skill-1"])
def test_skill_package_validator_accepts_chinese_names(name):
    result = SkillPackageValidator().validate(
        _zip({f"{name}/SKILL.md": _skill_md(name=name)})
    )

    assert result.skill_key == name
    assert result.root_prefix == f"{name}/"


@pytest.mark.parametrize("name", ["中文", "1数据分析", "-数据分析", "数据_分析", "数据📊分析"])
def test_skill_package_validator_rejects_invalid_names(name):
    with pytest.raises(SkillPackageValidationError, match="Skill name 格式错误"):
        SkillPackageValidator().validate(_zip({"SKILL.md": _skill_md(name=name)}))


@pytest.mark.parametrize(
    "files, expected",
    [
        ({"../SKILL.md": _skill_md()}, "路径"),
        ({"SKILL.md": _skill_md(), "run.py": "print('x')"}, "文件类型"),
        ({"SKILL.md": _skill_md(version="latest")}, "SemVer"),
        ({"a/SKILL.md": _skill_md(), "b/SKILL.md": _skill_md()}, "只能包含一个"),
    ],
)
def test_skill_package_validator_rejects_unsafe_packages(files, expected):
    with pytest.raises(SkillPackageValidationError, match=expected):
        SkillPackageValidator().validate(_zip(files))


async def _session_factory():
    engine = create_async_engine("sqlite+aiosqlite:///:memory:")
    async with engine.begin() as connection:
        for table in (
            User.__table__,
            ManagedSkill.__table__,
            SkillVersion.__table__,
            SkillAcl.__table__,
            AgentSkillBinding.__table__,
        ):
            await connection.run_sync(table.create)
    return engine, async_sessionmaker(engine, expire_on_commit=False)


async def test_runtime_resolver_applies_required_default_and_explicit_empty():
    engine, factory = await _session_factory()
    try:
        async with factory() as session:
            user = User(
                user_id="user-1",
                username="skill-user",
                api_key="skill-key",
                role="user",
                is_active=True,
            )
            required = ManagedSkill(
                skill_id="skill-required",
                skill_key="required-skill",
                display_name="Required",
                description="required",
                domain="nuclear-electronics",
                visibility="public",
                status="published",
                active_version_id="version-required",
                created_by="admin",
            )
            optional = ManagedSkill(
                skill_id="skill-optional",
                skill_key="optional-skill",
                display_name="Optional",
                description="optional",
                domain="nuclear-electronics",
                visibility="public",
                status="published",
                active_version_id="version-optional",
                created_by="admin",
            )
            session.add_all(
                [
                    user,
                    required,
                    optional,
                    SkillVersion(
                        version_id="version-required",
                        skill_id=required.skill_id,
                        version="1.0.0",
                        status="published",
                        package_bucket="bucket",
                        package_object="required.zip",
                        package_sha256="a" * 64,
                        package_size=1,
                        manifest_json={
                            "allowed_workflows": ["full"],
                            "preferred_workflow": "full",
                        },
                        file_index_json=[],
                        created_by="admin",
                    ),
                    SkillVersion(
                        version_id="version-optional",
                        skill_id=optional.skill_id,
                        version="1.0.0",
                        status="published",
                        package_bucket="bucket",
                        package_object="optional.zip",
                        package_sha256="b" * 64,
                        package_size=1,
                        manifest_json={
                            "allowed_workflows": ["full"],
                            "preferred_workflow": "full",
                        },
                        file_index_json=[],
                        created_by="admin",
                    ),
                    AgentSkillBinding(
                        id=1,
                        agent_code="ne",
                        skill_id=required.skill_id,
                        is_enabled=True,
                        is_required=True,
                        default_selected=True,
                        is_selectable=False,
                        sort_order=0,
                        created_by="admin",
                    ),
                    AgentSkillBinding(
                        id=2,
                        agent_code="ne",
                        skill_id=optional.skill_id,
                        is_enabled=True,
                        is_required=False,
                        default_selected=True,
                        is_selectable=True,
                        sort_order=1,
                        created_by="admin",
                    ),
                ]
            )
            await session.commit()

            resolver = SkillRuntimeResolver()
            default_result = await resolver.resolve(
                session,
                user_id=user.user_id,
                role=user.role,
                agent_code="ne",
                agent_domain="nuclear-electronics",
                workflow="full",
                requested_skills=None,
                fallback_skills=[],
            )
            empty_result = await resolver.resolve(
                session,
                user_id=user.user_id,
                role=user.role,
                agent_code="ne",
                agent_domain="nuclear-electronics",
                workflow="full",
                requested_skills=[],
                fallback_skills=[],
            )

            assert default_result.selected_skills == ["required-skill", "optional-skill"]
            assert empty_result.selected_skills == ["required-skill"]
            assert [item.name for item in default_result.available_skills] == [
                "required-skill",
                "optional-skill",
            ]
            optional_option = next(
                item
                for item in default_result.available_skills
                if item.name == "optional-skill"
            )
            assert optional_option.allowed_workflows == ["full"]
            assert optional_option.preferred_workflow == "full"
            assert optional_option.guest_selectable is False
    finally:
        await engine.dispose()


async def test_runtime_resolver_rejects_managed_skill_without_agent_binding():
    engine, factory = await _session_factory()
    try:
        async with factory() as session:
            session.add(
                ManagedSkill(
                    skill_id="managed-skill",
                    skill_key="managed-skill",
                    display_name="Managed",
                    description="managed",
                    domain="nuclear-electronics",
                    visibility="public",
                    status="published",
                    active_version_id="managed-version",
                    created_by="admin",
                )
            )
            await session.commit()

            with pytest.raises(SkillRuntimeError) as exc_info:
                await SkillRuntimeResolver().resolve(
                    session,
                    user_id="user-1",
                    role="user",
                    agent_code="ne",
                    agent_domain="nuclear-electronics",
                    workflow="full",
                    requested_skills=["managed-skill"],
                    fallback_skills=[],
                )

            assert exc_info.value.code == "SKILL_NOT_BOUND_TO_AGENT"
    finally:
        await engine.dispose()
