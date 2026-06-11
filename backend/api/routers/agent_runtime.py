"""Standardized agent runtime API.

This route exposes the AgentService -> Workflow -> AgentEvent -> SSE boundary.
"""

from __future__ import annotations

from typing import Any

from fastapi import APIRouter, Depends, HTTPException, Request
from fastapi.responses import StreamingResponse
from pydantic import BaseModel, Field

from backend.api.dependencies.auth import ApiKeyAuth, verify_api_key
from backend.api.routers.auth import get_current_user
from backend.application.agent_service import AgentService
from backend.application.agents.info_service import AgentInfoService
from backend.application.agents.profile_loader import AgentProfileLoader
from backend.application.event_stream.sse_encoder import SSEEncoder
from backend.application.skills.manifest_loader import SkillManifestLoader
from backend.application.tools.registry import ToolRegistry
from backend.db.models import User
from backend.domain.agents.request import AgentRequest
from backend.domain.agents.workflows import normalize_workflow_name

router = APIRouter(prefix="/agent-runtime", tags=["agent-runtime-v2"])


class AgentRuntimeQueryRequest(BaseModel):
    question: str = Field(..., min_length=1)
    agent_code: str = Field(default="ne", description="Agent profile code, e.g. ne or csh")
    thread_id: str | None = None
    workflow: str | None = Field(
        default=None,
        description=(
            "lightweight/full/deep_research/plan_solve/"
            "multi_agent_parallel/multi_agent_alternating"
        ),
    )
    kb_ids: list[str] | None = None
    selected_skills: list[str] = Field(default_factory=list)
    model: str | None = None
    use_search: bool = True
    subagent_enabled: bool = False
    thinking: bool = True
    metadata: dict[str, Any] = Field(default_factory=dict)

    def resolve_workflow(self) -> str | None:
        explicit = (self.workflow or "").strip()
        return normalize_workflow_name(explicit) if explicit else None


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


@router.get("/profiles")
async def list_agent_profiles(auth: ApiKeyAuth = Depends(verify_api_key)):
    profiles = AgentProfileLoader().list_profiles()
    return {"profiles": [_profile_to_dict(profile) for profile in profiles]}


@router.get("/profiles/{agent_code}")
async def get_agent_profile(
    agent_code: str,
    auth: ApiKeyAuth = Depends(verify_api_key),
):
    profile = _ensure_agent_profile(agent_code)
    return _profile_to_dict(profile)


@router.get("/profiles/{agent_code}/info")
async def get_agent_runtime_info(agent_code: str):
    """Return frontend-compatible AgentInfo through the standardized runtime namespace."""
    profile = _ensure_agent_profile(agent_code)
    return await AgentInfoService().get_info(profile)


@router.get("/tools")
async def list_tools(auth: ApiKeyAuth = Depends(verify_api_key)):
    tools = ToolRegistry().list_specs()
    return {"tools": [_tool_to_dict(tool) for tool in tools]}


@router.get("/tool-policies/{policy_name}/tools")
async def list_policy_tools(
    policy_name: str,
    agent_code: str,
    workflow: str = "full",
    selected_skills: str = "",
    auth: ApiKeyAuth = Depends(verify_api_key),
):
    profile = AgentProfileLoader().load(agent_code)
    skill_set = {
        item.strip()
        for item in selected_skills.split(",")
        if item.strip()
    }
    tools = ToolRegistry().resolve_policy_tools(
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
async def list_skill_manifests(auth: ApiKeyAuth = Depends(verify_api_key)):
    manifests = SkillManifestLoader().list_manifests()
    return {"skills": [_skill_manifest_to_dict(manifest) for manifest in manifests]}


@router.get("/{agent_code}/task/{task_id}/status")
async def get_agent_runtime_task_status(
    agent_code: str,
    task_id: str,
    thread_id: str = "",
    user: User = Depends(get_current_user),
):
    _ensure_agent_profile(agent_code)
    raise HTTPException(status_code=410, detail="旧版深度研究后台任务已移除")


@router.get("/{agent_code}/task/{task_id}/stream")
async def stream_agent_runtime_task_events(
    agent_code: str,
    task_id: str,
    request: Request,
    thread_id: str = "",
    user: User = Depends(get_current_user),
):
    _ensure_agent_profile(agent_code)
    raise HTTPException(status_code=410, detail="旧版深度研究后台事件流已移除")


@router.post("/{agent_code}/task/{task_id}/cancel")
async def cancel_agent_runtime_task(
    agent_code: str,
    task_id: str,
    thread_id: str = "",
    user: User = Depends(get_current_user),
):
    _ensure_agent_profile(agent_code)
    raise HTTPException(status_code=410, detail="旧版深度研究后台任务已移除")


@router.get("/{agent_code}/thread/{thread_id}/task")
async def get_agent_runtime_thread_active_task(
    agent_code: str,
    thread_id: str,
    user: User = Depends(get_current_user),
):
    _ensure_agent_profile(agent_code)
    return {"has_active_task": False, "task": None}


@router.post("/query")
async def query_agent_runtime(
    payload: AgentRuntimeQueryRequest,
    request: Request,
    auth: ApiKeyAuth = Depends(verify_api_key),
):
    from backend.application.checkpointing.checkpointer import (
        get_checkpointer_from_request,
    )

    agent_request = AgentRequest(
        question=payload.question,
        agent_code=payload.agent_code,
        user_id=auth.user_id,
        conversation_id=payload.thread_id,
        requested_workflow=payload.resolve_workflow(),
        selected_kb_ids=list(payload.kb_ids or []),
        selected_skills=list(payload.selected_skills or []),
        model_name=payload.model,
        use_search=payload.use_search,
        subagent_enabled=payload.subagent_enabled,
        thinking_enabled=payload.thinking,
        metadata=dict(payload.metadata or {}),
    )
    service = AgentService(checkpointer=get_checkpointer_from_request(request))
    encoder = SSEEncoder()
    return StreamingResponse(
        encoder.encode_stream(service.run_stream(agent_request)),
        media_type="text/event-stream",
        headers={
            "Cache-Control": "no-cache, no-transform",
            "Connection": "keep-alive",
            "X-Accel-Buffering": "no",
        },
    )
