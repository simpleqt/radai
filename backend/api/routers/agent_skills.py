"""Administrator APIs for Agent to Skill bindings."""

from __future__ import annotations

import asyncio

from fastapi import APIRouter, Depends, HTTPException
from pydantic import BaseModel, Field, model_validator
from sqlalchemy import select

from backend.api.dependencies.auth import ApiKeyAuth, require_admin
from backend.application.agents.code_resolver import AgentCodeResolver
from backend.application.agents.profile_loader import AgentProfileLoader
from backend.application.skills.domain_policy import skill_domain_matches
from backend.db.connection import get_async_session_factory
from backend.db.crud.skill_crud import SkillCRUD
from backend.db.models.skill import ManagedSkill

router = APIRouter()


class AgentSkillBindingItem(BaseModel):
    skill_id: str
    is_enabled: bool = True
    is_required: bool = False
    default_selected: bool = False
    is_selectable: bool = True
    sort_order: int = Field(default=0, ge=0)

    @model_validator(mode="after")
    def validate_flags(self):
        if self.is_required and not self.is_enabled:
            raise ValueError("required Skill 必须启用")
        return self


class SetAgentSkillsRequest(BaseModel):
    skills: list[AgentSkillBindingItem]


async def _load_profile(agent_code: str):
    runtime_code = AgentCodeResolver.to_runtime(agent_code)
    try:
        return await asyncio.to_thread(AgentProfileLoader().load, runtime_code)
    except ValueError as exc:
        raise HTTPException(status_code=404, detail="未知智能体配置") from exc


def _binding_payload(binding, skill) -> dict:
    return {
        "skill_id": skill.skill_id,
        "skill_key": skill.skill_key,
        "display_name": skill.display_name,
        "domain": skill.domain,
        "status": skill.status,
        "is_enabled": binding.is_enabled,
        "is_required": binding.is_required,
        "default_selected": binding.default_selected,
        "is_selectable": binding.is_selectable,
        "sort_order": binding.sort_order,
    }


@router.get("/{agent_code}/skills")
async def list_agent_skills(
    agent_code: str, admin: ApiKeyAuth = Depends(require_admin)
):
    profile = await _load_profile(agent_code)
    runtime_code = profile.code
    factory = get_async_session_factory()
    async with factory() as session:
        rows = await SkillCRUD.list_bindings(session, runtime_code)
        skill_rows = await session.execute(
            select(ManagedSkill.skill_id, ManagedSkill.domain).where(
                ManagedSkill.status == "published",
                ManagedSkill.active_version_id.is_not(None),
            )
        )
        compatible_skill_ids = [
            skill_id
            for skill_id, domain in skill_rows.all()
            if skill_domain_matches(
                domain,
                agent_code=profile.code,
                agent_domain=profile.domain,
            )
        ]
        return {
            "agent_code": runtime_code,
            "skills": [_binding_payload(binding, skill) for binding, skill in rows],
            "compatible_skill_ids": compatible_skill_ids,
        }


@router.put("/{agent_code}/skills")
async def set_agent_skills(
    agent_code: str,
    payload: SetAgentSkillsRequest,
    admin: ApiKeyAuth = Depends(require_admin),
):
    profile = await _load_profile(agent_code)
    runtime_code = profile.code
    ids = [item.skill_id for item in payload.skills]
    if len(ids) != len(set(ids)):
        raise HTTPException(status_code=400, detail="Skill 绑定列表包含重复项")
    factory = get_async_session_factory()
    async with factory() as session:
        for item in payload.skills:
            skill = await SkillCRUD.get_skill(session, item.skill_id)
            if skill is None:
                raise HTTPException(status_code=404, detail=f"Skill 不存在: {item.skill_id}")
            if skill.status != "published" or not skill.active_version_id:
                raise HTTPException(status_code=409, detail=f"Skill 未发布: {skill.skill_key}")
            if not skill_domain_matches(
                skill.domain,
                agent_code=profile.code,
                agent_domain=profile.domain,
            ):
                raise HTTPException(status_code=409, detail=f"Skill domain 与智能体不兼容: {skill.skill_key}")
        await SkillCRUD.replace_bindings(
            session,
            agent_code=runtime_code,
            bindings=[item.model_dump() for item in payload.skills],
            actor_user_id=admin.user_id,
        )
        SkillCRUD.audit(
            session,
            actor_user_id=admin.user_id,
            action="bind_agent",
            target_type="agent",
            target_id=runtime_code,
            detail={"skill_ids": ids},
        )
        await session.commit()
        rows = await SkillCRUD.list_bindings(session, runtime_code)
    return {
        "agent_code": runtime_code,
        "skills": [_binding_payload(binding, skill) for binding, skill in rows],
    }


@router.post("/{agent_code}/skills")
async def upsert_agent_skill(
    agent_code: str,
    payload: AgentSkillBindingItem,
    admin: ApiKeyAuth = Depends(require_admin),
):
    profile = await _load_profile(agent_code)
    runtime_code = profile.code
    factory = get_async_session_factory()
    async with factory() as session:
        current = await SkillCRUD.list_bindings(session, runtime_code)
        items = [
            AgentSkillBindingItem(
                skill_id=skill.skill_id,
                is_enabled=binding.is_enabled,
                is_required=binding.is_required,
                default_selected=binding.default_selected,
                is_selectable=binding.is_selectable,
                sort_order=binding.sort_order,
            )
            for binding, skill in current
            if skill.skill_id != payload.skill_id
        ]
        items.append(payload)
    return await set_agent_skills(
        runtime_code, SetAgentSkillsRequest(skills=items), admin
    )


@router.delete("/{agent_code}/skills/{skill_id}")
async def delete_agent_skill(
    agent_code: str,
    skill_id: str,
    admin: ApiKeyAuth = Depends(require_admin),
):
    profile = await _load_profile(agent_code)
    runtime_code = profile.code
    factory = get_async_session_factory()
    async with factory() as session:
        current = await SkillCRUD.list_bindings(session, runtime_code)
        if not any(skill.skill_id == skill_id for _binding, skill in current):
            raise HTTPException(status_code=404, detail="Skill 绑定不存在")
        items = [
            AgentSkillBindingItem(
                skill_id=skill.skill_id,
                is_enabled=binding.is_enabled,
                is_required=binding.is_required,
                default_selected=binding.default_selected,
                is_selectable=binding.is_selectable,
                sort_order=binding.sort_order,
            )
            for binding, skill in current
            if skill.skill_id != skill_id
        ]
    await set_agent_skills(runtime_code, SetAgentSkillsRequest(skills=items), admin)
    return {"success": True}
