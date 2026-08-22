"""Agent 运行时目录路由:profile、工具与技能清单查询(由 agent_runtime 主 router include)。"""

from __future__ import annotations

import asyncio
from typing import Any

from fastapi import APIRouter, Depends, HTTPException

from backend.api.dependencies.auth import (
    ApiKeyAuth,
    optional_verify_api_key,
    require_admin,
    verify_api_key,
)
from backend.application.agents.definition import (
    AgentDefinitionValidationError,
    AgentDefinitionValidator,
)
from backend.application.agents.info_service import AgentInfoService
from backend.application.agents.profile_loader import AgentProfileLoader
from backend.application.skills.public_catalog import PublicSkillCatalog
from backend.application.tools.assembler import shared_default_tool_registry
from backend.application.tools.runtime_state import tool_runtime_state
from backend.config.loggers import get_logger

logger = get_logger(__name__)
router = APIRouter()


def _profile_to_dict(profile) -> dict[str, Any]:
    return {
        "code": profile.code,
        "name": profile.name,
        "domain": profile.domain,
        "description": profile.description,
        "default_model": profile.default_model,
        "default_kb_ids": profile.default_kb_ids,
        "default_skills": profile.default_skills,
        "default_workflow": profile.default_workflow,
        "allowed_workflows": profile.allowed_workflows,
        "prompt_profile": profile.prompt_profile,
        "tool_policy": profile.tool_policy,
        "kb_policy": profile.kb_policy,
        "guardrail_policy": profile.guardrail_policy,
        "ui_profile": profile.ui_profile,
        "domain_context_provider": profile.domain_context_provider,
    }


async def _profile_to_runtime_dict(
    profile,
    *,
    user_id: str = "guest",
    role: str = "user",
) -> dict[str, Any]:
    from backend.api.dependencies.auth import filter_accessible_kb_ids
    from backend.db.connection import get_async_session_factory
    from backend.db.crud.agent_crud import get_runtime_agent_kb_ids

    payload = _profile_to_dict(profile)
    try:
        factory = get_async_session_factory()
        async with factory() as session:
            bound_ids = await get_runtime_agent_kb_ids(session, profile.code)
            payload["default_kb_ids"] = await filter_accessible_kb_ids(
                session,
                list(bound_ids),
                ApiKeyAuth(api_key="", user_id=user_id, role=role),
            )
    except Exception:
        logger.exception("Failed to load runtime KB bindings for profile %s", profile.code)
        payload["default_kb_ids"] = []
    return payload


def _tool_to_dict(tool) -> dict[str, Any]:
    return {
        "name": tool.name,
        "provider": tool.provider,
        "description": tool.description,
        "domains": sorted(tool.domains),
        "workflows": sorted(tool.workflows),
        "required_skills": sorted(tool.required_skills),
        "risk_level": tool.risk_level,
        "enabled_by_default": tool.enabled_by_default,
        "side_effect": tool.side_effect,
        "timeout_seconds": tool.timeout_seconds,
        "idempotent": tool.idempotent,
        "parallel_safe": tool.parallel_safe,
        "max_concurrency": tool.max_concurrency,
        "max_attempts": tool.max_attempts,
        "retryable_errors": sorted(tool.retryable_errors),
        "max_output_chars": tool.max_output_chars,
        "cache_enabled": tool.cache_enabled,
        "cache_ttl_seconds": tool.cache_ttl_seconds,
    }


def _skill_manifest_to_dict(manifest) -> dict[str, Any]:
    return {
        "name": manifest.name,
        "description": manifest.description,
        "domain": manifest.domain,
        "category": manifest.category,
        "tools": manifest.tools,
        "allowed_workflows": manifest.allowed_workflows,
        "preferred_workflow": manifest.preferred_workflow,
        "kb_required": manifest.kb_required,
        "kb_binding_mode": manifest.kb_binding_mode,
        "guardrails": manifest.guardrails,
    }


def _ensure_agent_profile(agent_code: str):
    try:
        return AgentProfileLoader().load(agent_code)
    except ValueError as exc:
        raise HTTPException(status_code=404, detail=f"未知智能体配置: {agent_code}") from exc


def _ensure_agent_definition(agent_code: str):
    """Resolve the complete runtime contract before advertising capabilities."""
    try:
        return AgentDefinitionValidator().resolve(agent_code)
    except AgentDefinitionValidationError as exc:
        unknown_profile = any(
            issue.component == "profile"
            and issue.code == "load_failed"
            and "Unknown agent profile config" in issue.message
            for issue in exc.report.issues
        )
        raise HTTPException(
            status_code=404 if unknown_profile else 422,
            detail={
                "message": f"智能体运行定义无效: {agent_code}",
                "issues": [issue.to_json_dict() for issue in exc.report.issues],
            },
        ) from exc


def _list_registered_tools():
    return shared_default_tool_registry().list_specs()


def _resolve_policy_tools(
    *,
    policy_name: str,
    domain: str,
    workflow: str,
    selected_skills: set[str],
):
    return shared_default_tool_registry().resolve_policy_tools(
        policy_name=policy_name,
        domain=domain,
        workflow=workflow,
        selected_skills=selected_skills,
    )


@router.get("/profiles")
async def list_agent_profiles(auth: ApiKeyAuth = Depends(verify_api_key)):
    profiles = await asyncio.to_thread(AgentProfileLoader().list_profiles)
    return {
        "profiles": await asyncio.gather(
            *[
                _profile_to_runtime_dict(
                    profile,
                    user_id=auth.user_id,
                    role=auth.role,
                )
                for profile in profiles
            ]
        )
    }


@router.get("/profiles/{agent_code}")
async def get_agent_profile(
    agent_code: str,
    auth: ApiKeyAuth = Depends(verify_api_key),
):
    profile = await asyncio.to_thread(_ensure_agent_profile, agent_code)
    return await _profile_to_runtime_dict(
        profile,
        user_id=auth.user_id,
        role=auth.role,
    )


@router.get("/profiles/{agent_code}/info")
async def get_agent_runtime_info(
    agent_code: str,
    auth: ApiKeyAuth = Depends(optional_verify_api_key),
):
    """Return frontend-compatible AgentInfo through the standardized runtime namespace."""
    definition = await asyncio.to_thread(_ensure_agent_definition, agent_code)
    profile = definition.profile
    return await AgentInfoService().get_info(
        profile, user_id=auth.user_id, role=auth.role
    )


@router.get("/tools")
async def list_tools(auth: ApiKeyAuth = Depends(verify_api_key)):
    tools = await asyncio.to_thread(_list_registered_tools)
    return {"tools": [_tool_to_dict(tool) for tool in tools]}


@router.get("/admin/tool-runtime")
async def get_tool_runtime_state(auth: ApiKeyAuth = Depends(require_admin)):
    """Return this worker's observed tool health and execution statistics."""

    del auth
    snapshot = tool_runtime_state.snapshot()
    tools = await asyncio.to_thread(_list_registered_tools)
    snapshot["registered_tools"] = [_tool_to_dict(tool) for tool in tools]
    return snapshot


@router.get("/tool-policies/{policy_name}/tools")
async def list_policy_tools(
    policy_name: str,
    agent_code: str,
    workflow: str = "full",
    selected_skills: str = "",
    auth: ApiKeyAuth = Depends(verify_api_key),
):
    profile = await asyncio.to_thread(_ensure_agent_profile, agent_code)
    skill_set = {
        item.strip()
        for item in selected_skills.split(",")
        if item.strip()
    }
    tools = await asyncio.to_thread(
        _resolve_policy_tools,
        policy_name=policy_name,
        domain=profile.domain,
        workflow=workflow,
        selected_skills=skill_set,
    )
    return {
        "policy": policy_name,
        "agent_code": profile.code,
        "workflow": workflow,
        "selected_skills": sorted(skill_set),
        "tools": [_tool_to_dict(tool) for tool in tools],
    }


@router.get("/skill-manifests")
async def list_skill_manifests(
    agent_code: str = "ne",
    workflow: str | None = None,
    auth: ApiKeyAuth = Depends(verify_api_key),
):
    profile = await asyncio.to_thread(_ensure_agent_profile, agent_code)
    from backend.application.skills.runtime_resolver import (
        SkillRuntimeError,
        SkillRuntimeResolver,
    )
    from backend.db.connection import get_async_session_factory

    try:
        factory = get_async_session_factory()
        async with factory() as session:
            resolution = await SkillRuntimeResolver().resolve(
                session,
                user_id=auth.user_id,
                role=auth.role,
                agent_code=profile.code,
                agent_domain=profile.domain,
                workflow=workflow,
                requested_skills=None,
                fallback_skills=list(profile.default_skills),
            )
        if resolution.managed:
            skills = [
                {
                    "id": item.skill_id,
                    "name": item.name,
                    "description": item.description,
                    "version": item.version,
                    "required": item.required,
                    "default_selected": item.default_selected,
                    "selectable": item.selectable,
                    "allowed_workflows": item.allowed_workflows,
                    "preferred_workflow": item.preferred_workflow,
                    "guest_selectable": item.guest_selectable,
                }
                for item in resolution.available_skills
            ]
            # Managed mode is authoritative: do not advertise local manifests
            # that the current user cannot actually execute.
            return {"skills": skills}
    except SkillRuntimeError as exc:
        raise HTTPException(
            status_code=exc.status_code,
            detail={"code": exc.code, "message": str(exc)},
        ) from exc
    except Exception as exc:  # noqa: BLE001
        logger.warning(
            "Managed Skill catalog unavailable; returning manifest fallback catalog",
            exc_info=exc,
        )
        return {
            "skills": [
                _skill_manifest_to_dict(manifest)
                for manifest in PublicSkillCatalog().list_compatible(
                    profile,
                    workflow=workflow,
                )
            ]
        }
    return {
        "skills": [
            _skill_manifest_to_dict(manifest)
            for manifest in PublicSkillCatalog().list_compatible(
                profile,
                workflow=workflow,
            )
        ]
    }
