"""Import built-in Skills and profile defaults into the managed Skill catalog."""

from __future__ import annotations

import asyncio
import hashlib
import io
import uuid
import zipfile
from datetime import datetime
from pathlib import Path

from sqlalchemy import delete, select

from backend.application.agents.profile_loader import AgentProfileLoader
from backend.application.skills.manifest_loader import SkillManifestLoader
from backend.db.connection import get_async_session_factory
from backend.db.models import User
from backend.db.models.skill import AgentSkillBinding, ManagedSkill, SkillVersion
from backend.services.skill_package_storage import SkillPackageStorage


SKILL_DISPLAY_METADATA = {
    "academic-search": {
        "display_name": "学术文献检索",
        "display_description": "检索论文、DOI、arXiv、PubMed、OpenAlex、Crossref 等学术资源，适合综述和相关工作梳理。",
    },
    "csh-hydrogeology": {
        "display_name": "场地污染水文地质",
        "display_description": "面向地下水、土壤污染、污染场地调查评估、污染运移模拟和修复方案的专业问答技能。",
    },
    "deep-research": {
        "display_name": "深度研究",
        "display_description": "进入深度研究流程，进行多角度资料检索、分析和结构化研究报告生成。",
    },
    "excel-experiment-analysis": {
        "display_name": "实验数据分析",
        "display_description": "分析 Excel/CSV 实验数据，支持表结构探查、SQL 查询、统计分析和科研图表生成。",
    },
    "general-table-analysis": {
        "display_name": "通用表格分析",
        "display_description": "通用表格分析能力，支持 Excel/CSV 的查询、统计摘要、缺失值分析、分组聚合和可视化。",
    },
    "groundwater-project-analysis": {
        "display_name": "地下水项目分析",
        "display_description": "读取地下水项目资料、钻井数据和水文地质背景，支持质量检查与新井预测分析。",
    },
    "html-ppt": {
        "display_name": "PPT 创建",
        "display_description": "根据内容生成专业静态 HTML 演示文稿，适合 PPT、幻灯片、汇报和分享材料。",
    },
    "nuclear-electronics": {
        "display_name": "核电子学专业问答",
        "display_description": "面向核辐射探测、粒子探测器、数据获取和电子学读出的专业知识检索与问答技能。",
    },
}


def _display_metadata(skill_key: str, fallback_description: str) -> dict[str, str]:
    metadata = SKILL_DISPLAY_METADATA.get(skill_key, {})
    return {
        "display_name": metadata.get("display_name") or skill_key,
        "display_description": metadata.get("display_description") or fallback_description,
    }


def _zip_directory(directory: Path) -> bytes:
    buffer = io.BytesIO()
    with zipfile.ZipFile(buffer, "w", compression=zipfile.ZIP_DEFLATED) as archive:
        for path in sorted(directory.rglob("*")):
            if path.is_file():
                archive.write(path, path.relative_to(directory).as_posix())
    return buffer.getvalue()


def manifest_file_index(manifest) -> list[dict[str, object]]:
    root = manifest.skill_file.parent
    items: list[dict[str, object]] = []
    for path in sorted(root.rglob("*")):
        if not path.is_file():
            continue
        data = path.read_bytes()
        items.append(
            {
                "path": path.relative_to(root).as_posix(),
                "size": len(data),
                "sha256": hashlib.sha256(data).hexdigest(),
            }
        )
    return items


async def import_catalog(*, force: bool = False) -> None:
    storage = SkillPackageStorage()
    manifests = SkillManifestLoader().list_manifests()
    factory = get_async_session_factory()
    async with factory() as session:
        admin_result = await session.execute(
            select(User.user_id).where(User.role == "admin").limit(1)
        )
        actor = str(admin_result.scalar_one_or_none() or "system")
        key_to_skill: dict[str, ManagedSkill] = {}
        existing_skills = {
            skill.skill_key: skill
            for skill in (
                await session.execute(
                    select(ManagedSkill).where(
                        ManagedSkill.skill_key.in_(
                            [manifest.name for manifest in manifests]
                        )
                    )
                )
            ).scalars().all()
        }
        for manifest in manifests:
            skill = existing_skills.get(manifest.name)
            display = _display_metadata(manifest.name, manifest.description)
            if skill is not None and not force:
                skill.display_name = display["display_name"]
                skill.description = manifest.description
                skill.display_description = display["display_description"]
                skill.domain = manifest.domain
                key_to_skill[skill.skill_key] = skill
                continue
            if skill is not None and force:
                await session.execute(
                    delete(SkillVersion).where(SkillVersion.skill_id == skill.skill_id)
                )

            package, file_index = await asyncio.gather(
                asyncio.to_thread(_zip_directory, manifest.skill_file.parent),
                asyncio.to_thread(manifest_file_index, manifest),
            )
            sha256 = hashlib.sha256(package).hexdigest()
            if skill is None:
                skill = ManagedSkill(
                    skill_id=str(uuid.uuid4()),
                    skill_key=manifest.name,
                    display_name=display["display_name"],
                    description=manifest.description,
                    display_description=display["display_description"],
                    domain=manifest.domain,
                    source_type="builtin",
                    visibility="public",
                    status="published",
                    created_by=actor,
                )
                session.add(skill)
                await session.flush()
            version_id = str(uuid.uuid4())
            imported_version = (
                manifest.version
                if manifest.version != "0.0.0"
                else "1.0.0+imported"
            )
            object_name = (
                f"skills/packages/{skill.skill_id}/{version_id}/"
                f"{manifest.name}-{imported_version}.zip"
            )
            await storage.upload(object_name=object_name, content=package)
            version = SkillVersion(
                version_id=version_id,
                skill_id=skill.skill_id,
                version=imported_version,
                status="published",
                package_bucket=storage.bucket,
                package_object=object_name,
                package_sha256=sha256,
                package_size=len(package),
                manifest_json={
                    "name": manifest.name,
                    "version": imported_version,
                    "description": manifest.description,
                    "domain": manifest.domain,
                    "tools": manifest.tools,
                    "allowed_workflows": manifest.allowed_workflows,
                    "preferred_workflow": manifest.preferred_workflow,
                    "kb_required": manifest.kb_required,
                    "kb_binding_mode": manifest.kb_binding_mode,
                    "guardrails": manifest.guardrails,
                    "dependencies": manifest.dependencies,
                    "conflicts": manifest.conflicts,
                    "capabilities": manifest.capabilities,
                    "resource_budget": manifest.resource_budget,
                    "package_root_prefix": "",
                },
                file_index_json=file_index,
                created_by=actor,
                reviewed_by=actor,
                published_at=datetime.utcnow(),
            )
            session.add(version)
            skill.active_version_id = version_id
            skill.display_name = display["display_name"]
            skill.description = manifest.description
            skill.display_description = display["display_description"]
            skill.status = "published"
            key_to_skill[skill.skill_key] = skill

        profiles = AgentProfileLoader().list_profiles()
        existing_bindings = {
            (binding.agent_code, binding.skill_id)
            for binding in (
                await session.execute(
                    select(AgentSkillBinding).where(
                        AgentSkillBinding.agent_code.in_(
                            [profile.code for profile in profiles]
                        )
                    )
                )
            ).scalars().all()
        }
        for profile in profiles:
            for order, skill_key in enumerate(profile.default_skills):
                skill = key_to_skill.get(skill_key)
                if skill is None:
                    continue
                if (profile.code, skill.skill_id) not in existing_bindings:
                    session.add(
                        AgentSkillBinding(
                            agent_code=profile.code,
                            skill_id=skill.skill_id,
                            is_enabled=True,
                            is_required=False,
                            default_selected=True,
                            is_selectable=True,
                            sort_order=order,
                            created_by=actor,
                        )
                    )
                    existing_bindings.add((profile.code, skill.skill_id))
        await session.commit()
    print(f"Imported {len(key_to_skill)} Skills")
