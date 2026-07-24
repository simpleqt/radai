"""Standardized agent runtime API.

This route exposes the AgentService -> Workflow -> AgentEvent -> SSE boundary.
"""

from __future__ import annotations

import asyncio
import base64
import binascii
import json
import logging
import re
import uuid
from dataclasses import replace
from typing import Any

from fastapi import APIRouter, Depends, HTTPException, Query, Request
from fastapi.responses import StreamingResponse
from pydantic import BaseModel, Field, model_validator

from backend.api.dependencies.auth import (
    ApiKeyAuth,
    optional_verify_api_key,
    require_admin,
    verify_api_key,
)
from backend.application.agent_service import AgentService
from backend.application.agent_trace import (
    AgentRunReplay,
    AgentTraceStoreProtocol,
    create_agent_trace_store,
)
from backend.application.agent_trace.models import AgentRunRecord
from backend.application.agents.definition import (
    AgentDefinitionValidationError,
    AgentDefinitionValidator,
)
from backend.application.agents.info_service import AgentInfoService
from backend.application.agents.profile_loader import AgentProfileLoader
from backend.application.event_stream.sse_encoder import SSEEncoder
from backend.application.skills.manifest_loader import SkillManifestLoader
from backend.application.skills.public_catalog import PublicSkillCatalog
from backend.application.tools.registry import ToolRegistry
from backend.application.tools.runtime_state import tool_runtime_state
from backend.core.thread_key import validate_external_thread_id
from backend.domain.agents.request import AgentRequest
from backend.domain.agents.workflows import SUPPORTED_WORKFLOWS, normalize_workflow_name

router = APIRouter(prefix="/agent-runtime", tags=["agent-runtime-v2"])
logger = logging.getLogger(__name__)

# Compatibility injection seam retained for existing tests/extensions while
# production selection now goes through the configured trace-store factory.
AgentTraceStore = create_agent_trace_store


class ImageAttachment(BaseModel):
    base64: str | None = Field(default=None, max_length=7_000_000)
    image_url: str | None = Field(default=None, max_length=4096)
    mime_type: str = Field(default="image/png", max_length=64)

    @model_validator(mode="after")
    def validate_image(self):
        if bool(self.base64) == bool(self.image_url):
            raise ValueError("附件必须且只能提供 base64 或 image_url")
        if not re.fullmatch(r"image/(png|jpeg|jpg|webp|gif)", self.mime_type.lower()):
            raise ValueError("附件 MIME 类型不受支持")
        if self.image_url and not self.image_url.startswith(("https://", "http://")):
            raise ValueError("image_url 仅支持 HTTP(S)；内嵌图片请使用 base64")
        if self.base64:
            try:
                decoded = base64.b64decode(self.base64, validate=True)
            except (ValueError, binascii.Error) as exc:
                raise ValueError("附件 base64 格式无效") from exc
            if len(decoded) > 5 * 1024 * 1024:
                raise ValueError("单个附件不能超过 5 MiB")
        return self


class AgentRuntimeQueryRequest(BaseModel):
    question: str = Field(..., min_length=1, max_length=50_000)
    agent_code: str = Field(
        default="ne", min_length=1, max_length=64,
        description="Agent profile code, e.g. ne or csh",
    )
    thread_id: str | None = Field(default=None, min_length=1, max_length=100)
    workflow: str | None = Field(
        default=None,
        description=(
            "lightweight/full/deep_research/plan_solve/"
            "multi_agent_parallel/multi_agent_alternating"
        ),
    )
    kb_ids: list[str] | None = Field(default=None, max_length=64)
    selected_skills: list[str] | None = Field(default=None, max_length=32)
    model: str | None = Field(default=None, max_length=128)
    use_search: bool = True
    subagent_enabled: bool = False
    thinking: bool = True
    attachments: list[ImageAttachment] = Field(default_factory=list, max_length=8)
    metadata: dict[str, Any] = Field(default_factory=dict, max_length=64)

    @model_validator(mode="after")
    def validate_payload_sizes(self):
        if self.thread_id is not None:
            validate_external_thread_id(self.thread_id)
        for label, values, max_item_length in (
            ("kb_ids", self.kb_ids or [], 128),
            ("selected_skills", self.selected_skills or [], 128),
        ):
            if any(len(str(item)) > max_item_length for item in values):
                raise ValueError(f"{label} 包含过长条目")
        if len(json.dumps(self.metadata, ensure_ascii=False, default=str)) > 65_536:
            raise ValueError("metadata 不能超过 64 KiB")
        attachment_payload = [item.model_dump(exclude_none=True) for item in self.attachments]
        if len(json.dumps(attachment_payload, ensure_ascii=False)) > 10_485_760:
            raise ValueError("attachments 不能超过 10 MiB")
        return self

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


_RECONNECTABLE_BACKGROUND_WORKFLOWS = {"deep_research", "plan_solve"}


def _uses_deep_research_background(request: AgentRequest, profile) -> bool:
    """Return whether the request should run as a reconnectable background task.

    The historical name is kept for tests and callers.  Plan-Solve has the same
    persistence risk as deep research: if the HTTP SSE connection is cancelled,
    the coordinated workflow can be cancelled before its parent checkpoint is
    written.
    """
    requested = normalize_workflow_name(request.requested_workflow)
    if requested:
        return requested in _RECONNECTABLE_BACKGROUND_WORKFLOWS
    if request.skills_requested_for_workflow:
        loader = SkillManifestLoader()
        for skill_name in request.selected_skills or []:
            if str(skill_name).strip() == "deep-research":
                return True
            try:
                preferred = normalize_workflow_name(loader.load(skill_name).preferred_workflow)
                if preferred in _RECONNECTABLE_BACKGROUND_WORKFLOWS:
                    return True
            except ValueError:
                continue
        return False
    return normalize_workflow_name(profile.default_workflow) in _RECONNECTABLE_BACKGROUND_WORKFLOWS


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
    return ToolRegistry().list_specs()


def _resolve_policy_tools(
    *,
    policy_name: str,
    domain: str,
    workflow: str,
    selected_skills: set[str],
):
    return ToolRegistry().resolve_policy_tools(
        policy_name=policy_name,
        domain=domain,
        workflow=workflow,
        selected_skills=selected_skills,
    )


async def _read_trace_records_for_auth(
    run_id: str,
    *,
    auth: ApiKeyAuth,
    trace_root: str | None = None,
) -> tuple[AgentTraceStoreProtocol, list[AgentRunRecord]]:
    store = AgentTraceStore(trace_root)
    try:
        records = await asyncio.to_thread(store.read_records, run_id)
    except FileNotFoundError as exc:
        raise HTTPException(status_code=404, detail="agent run trace 不存在") from exc
    except ValueError as exc:
        raise HTTPException(status_code=422, detail=str(exc)) from exc
    run_payload = next(
        (record.payload for record in records if record.kind == "run"),
        {},
    )
    owner_id = str(run_payload.get("user_id") or "")
    if not owner_id and (auth.role or "").lower() != "admin":
        raise HTTPException(status_code=404, detail="agent run trace 不存在")
    if owner_id and owner_id != auth.user_id and (auth.role or "").lower() != "admin":
        raise HTTPException(status_code=404, detail="agent run trace 不存在")
    return store, records


def _payloads_for_kind(records: list[AgentRunRecord], kind: str) -> list[dict[str, Any]]:
    return [dict(record.payload) for record in records if record.kind == kind]


def _trace_counts(records: list[AgentRunRecord]) -> dict[str, int]:
    """Return stable counters used by both detail and admin inventory views."""
    return {
        "events": sum(1 for record in records if record.kind == "event"),
        "units": sum(1 for record in records if record.kind == "unit"),
        "tool_calls": sum(1 for record in records if record.kind == "tool_call"),
        "context_packs": sum(
            1 for record in records if record.kind == "context_pack"
        ),
        "compactions": sum(
            1
            for record in records
            if record.kind == "compaction"
            and str(record.payload.get("status") or "completed") == "completed"
        ),
        "compaction_attempts": sum(
            1 for record in records if record.kind == "compaction"
        ),
        "compaction_skipped": sum(
            1
            for record in records
            if record.kind == "compaction"
            and str(record.payload.get("status") or "completed") == "skipped"
        ),
        "handoffs": sum(1 for record in records if record.kind == "handoff"),
    }


def _trace_summary(run_id: str, records: list[AgentRunRecord]) -> dict[str, Any] | None:
    run_payloads = _payloads_for_kind(records, "run")
    if not run_payloads:
        return None
    summary = dict(run_payloads[-1])
    summary["run_id"] = run_id
    summary["record_count"] = len(records)
    summary["counts"] = _trace_counts(records)
    return summary


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


@router.get("/admin/runs")
async def list_admin_agent_runs(
    limit: int = Query(default=50, ge=1, le=200),
    offset: int = Query(default=0, ge=0),
    status: str | None = Query(default=None, max_length=32),
    agent_code: str | None = Query(default=None, max_length=64),
    user_id: str | None = Query(default=None, max_length=128),
    q: str | None = Query(default=None, max_length=256),
    auth: ApiKeyAuth = Depends(require_admin),
):
    """List run summaries for the Admin runtime console.

    Trace stores intentionally expose only a minimal ``list_run_ids`` contract.
    The inventory therefore reads one compact run record per id and never
    returns event/tool payloads.  Detailed records remain behind the existing
    per-run authorization checks.
    """
    del auth  # The dependency performs the authorization check.
    try:
        store = AgentTraceStore()
        run_ids = store.list_run_ids()
    except Exception as exc:  # noqa: BLE001
        logger.exception("Failed to list agent trace runs")
        raise HTTPException(status_code=503, detail="运行记录暂时不可用") from exc

    normalized_status = str(status or "").strip().lower()
    normalized_agent = str(agent_code or "").strip().lower()
    normalized_user = str(user_id or "").strip().lower()
    normalized_query = str(q or "").strip().lower()
    ordered_run_ids = list(reversed(run_ids))

    # The common unfiltered view can page by IDs without opening every trace
    # file/blob.  This keeps the Admin page cheap even when the installation
    # has accumulated a large history; filters intentionally trade some I/O
    # for exact matching.
    if not any((normalized_status, normalized_agent, normalized_user, normalized_query)):
        items: list[dict[str, Any]] = []
        for run_id in ordered_run_ids[offset:offset + limit]:
            try:
                records = await asyncio.to_thread(store.read_records, run_id)
            except (FileNotFoundError, ValueError):
                continue
            summary = _trace_summary(run_id, records)
            if summary is not None:
                items.append(summary)
        return {
            "items": items,
            "total": len(run_ids),
            "limit": limit,
            "offset": offset,
        }

    filtered_items: list[dict[str, Any]] = []
    for run_id in ordered_run_ids:
        try:
            records = await asyncio.to_thread(store.read_records, run_id)
        except (FileNotFoundError, ValueError):
            # A concurrently rotating/deleting trace should not make the
            # entire Admin page fail.
            continue
        summary = _trace_summary(run_id, records)
        if summary is None:
            continue
        if normalized_status and str(summary.get("status") or "").lower() != normalized_status:
            continue
        if normalized_agent and str(summary.get("agent_code") or "").lower() != normalized_agent:
            continue
        if normalized_user and str(summary.get("user_id") or "").lower() != normalized_user:
            continue
        if normalized_query:
            searchable = " ".join(
                str(summary.get(field) or "")
                for field in ("run_id", "request_id", "thread_id", "user_id", "agent_code", "workflow", "model_name")
            ).lower()
            if normalized_query not in searchable:
                continue
        filtered_items.append(summary)

    total = len(filtered_items)
    return {
        "items": filtered_items[offset:offset + limit],
        "total": total,
        "limit": limit,
        "offset": offset,
    }


@router.get("/runs/{run_id}")
async def get_agent_run_trace(
    run_id: str,
    auth: ApiKeyAuth = Depends(verify_api_key),
):
    _, records = await _read_trace_records_for_auth(
        run_id,
        auth=auth,
    )
    run_payloads = _payloads_for_kind(records, "run")
    return {
        "run_id": run_id,
        "run": run_payloads[-1] if run_payloads else None,
        "record_count": len(records),
        "counts": _trace_counts(records),
    }


@router.post("/runs/{run_id}/cancel")
async def cancel_live_agent_run(
    run_id: str,
    auth: ApiKeyAuth = Depends(verify_api_key),
):
    """Cooperatively cancel a live request tree in this API worker."""

    from backend.core.agents.runtime_manager import get_live_agent_runtime_manager

    manager = get_live_agent_runtime_manager(run_id)
    if manager is None:
        raise HTTPException(
            status_code=409,
            detail="agent run is not active in this API worker",
        )
    metadata = dict(getattr(manager, "run_metadata", {}) or {})
    owner_id = str(metadata.get("user_id") or "")
    if (
        (auth.role or "").lower() != "admin"
        and (not owner_id or owner_id != auth.user_id)
    ):
        raise HTTPException(status_code=404, detail="agent run 不存在")
    try:
        await manager.interrupt(
            manager.root_agent_id,
            reason="cancelled by user",
        )
    except (asyncio.CancelledError, GeneratorExit):
        raise
    except Exception as exc:  # noqa: BLE001
        logger.exception("Failed to cancel live agent run %s", run_id)
        raise HTTPException(status_code=503, detail="agent run 暂时无法取消") from exc
    return {
        "run_id": run_id,
        "status": "cancelling",
        "scope": "current_worker",
    }


@router.get("/runs/{run_id}/events")
async def list_agent_run_events(
    run_id: str,
    auth: ApiKeyAuth = Depends(verify_api_key),
):
    _, records = await _read_trace_records_for_auth(
        run_id,
        auth=auth,
    )
    return {"run_id": run_id, "events": _payloads_for_kind(records, "event")}


@router.get("/runs/{run_id}/units")
async def list_agent_run_units(
    run_id: str,
    auth: ApiKeyAuth = Depends(verify_api_key),
):
    _, records = await _read_trace_records_for_auth(
        run_id,
        auth=auth,
    )
    return {"run_id": run_id, "units": _payloads_for_kind(records, "unit")}


@router.get("/runs/{run_id}/tool-calls")
async def list_agent_run_tool_calls(
    run_id: str,
    auth: ApiKeyAuth = Depends(verify_api_key),
):
    _, records = await _read_trace_records_for_auth(
        run_id,
        auth=auth,
    )
    return {"run_id": run_id, "tool_calls": _payloads_for_kind(records, "tool_call")}


@router.get("/runs/{run_id}/context-packs")
async def list_agent_run_context_packs(
    run_id: str,
    auth: ApiKeyAuth = Depends(verify_api_key),
):
    _, records = await _read_trace_records_for_auth(
        run_id,
        auth=auth,
    )
    return {
        "run_id": run_id,
        "context_packs": _payloads_for_kind(records, "context_pack"),
    }


@router.get("/runs/{run_id}/compactions")
async def list_agent_run_compactions(
    run_id: str,
    auth: ApiKeyAuth = Depends(verify_api_key),
):
    _, records = await _read_trace_records_for_auth(
        run_id,
        auth=auth,
    )
    return {
        "run_id": run_id,
        "compactions": _payloads_for_kind(records, "compaction"),
    }


@router.get("/runs/{run_id}/handoffs")
async def list_agent_run_handoffs(
    run_id: str,
    auth: ApiKeyAuth = Depends(verify_api_key),
):
    _, records = await _read_trace_records_for_auth(
        run_id,
        auth=auth,
    )
    return {"run_id": run_id, "handoffs": _payloads_for_kind(records, "handoff")}


@router.post("/runs/{run_id}/replay")
async def replay_agent_run_trace(
    run_id: str,
    mode: str = Query(default="dry-run", pattern="^(dry-run|tool-cache|full)$"),
    from_unit: str | None = None,
    auth: ApiKeyAuth = Depends(verify_api_key),
):
    store, _ = await _read_trace_records_for_auth(
        run_id,
        auth=auth,
    )
    try:
        summary = await asyncio.to_thread(
            AgentRunReplay(store).replay,
            run_id,
            mode=mode,
            from_unit=from_unit,
        )
    except ValueError as exc:
        raise HTTPException(status_code=422, detail=str(exc)) from exc
    return summary.to_json_dict()


@router.get("/{agent_code}/task/{task_id}/status")
async def get_agent_runtime_task_status(
    agent_code: str,
    task_id: str,
    thread_id: str = "",
    auth: ApiKeyAuth = Depends(verify_api_key),
):
    await asyncio.to_thread(_ensure_agent_profile, agent_code)
    from backend.application.agent_tasks import agent_task_registry

    record = await agent_task_registry.get(
        task_id, user_id=auth.user_id, agent_code=agent_code
    )
    if record is None or (thread_id and record.thread_id != thread_id):
        raise HTTPException(status_code=404, detail="后台任务不存在")
    return record.public_info()


@router.get("/{agent_code}/task/{task_id}/stream")
async def stream_agent_runtime_task_events(
    agent_code: str,
    task_id: str,
    request: Request,
    thread_id: str = "",
    after_event_id: int | None = Query(default=None, ge=-1),
    auth: ApiKeyAuth = Depends(verify_api_key),
):
    await asyncio.to_thread(_ensure_agent_profile, agent_code)
    from backend.application.agent_tasks import agent_task_registry

    record = await agent_task_registry.get(
        task_id, user_id=auth.user_id, agent_code=agent_code
    )
    if record is None or (thread_id and record.thread_id != thread_id):
        raise HTTPException(status_code=404, detail="后台任务不存在")
    return StreamingResponse(
        SSEEncoder().encode_stream(
            agent_task_registry.subscribe(
                record,
                replay=True,
                after_event_id=after_event_id,
            )
        ),
        media_type="text/event-stream",
        headers={
            "Cache-Control": "no-cache, no-transform",
            "Connection": "keep-alive",
            "X-Accel-Buffering": "no",
        },
    )


@router.post("/{agent_code}/task/{task_id}/cancel")
async def cancel_agent_runtime_task(
    agent_code: str,
    task_id: str,
    thread_id: str = "",
    auth: ApiKeyAuth = Depends(verify_api_key),
):
    await asyncio.to_thread(_ensure_agent_profile, agent_code)
    from backend.application.agent_tasks import agent_task_registry

    record = await agent_task_registry.get(
        task_id, user_id=auth.user_id, agent_code=agent_code
    )
    if record is None or (thread_id and record.thread_id != thread_id):
        raise HTTPException(status_code=404, detail="后台任务不存在")
    await agent_task_registry.cancel(record)
    return {"status": "cancelled", "task_status": record.status}


@router.get("/{agent_code}/thread/{thread_id}/task")
async def get_agent_runtime_thread_active_task(
    agent_code: str,
    thread_id: str,
    auth: ApiKeyAuth = Depends(verify_api_key),
):
    await asyncio.to_thread(_ensure_agent_profile, agent_code)
    from backend.application.agent_tasks import agent_task_registry

    record = await agent_task_registry.active_for_thread(
        user_id=auth.user_id,
        agent_code=agent_code,
        thread_id=thread_id,
    )
    return {
        "has_active_task": record is not None,
        "task": record.public_info() if record is not None else None,
    }


@router.post("/query")
async def query_agent_runtime(
    payload: AgentRuntimeQueryRequest,
    request: Request,
    auth: ApiKeyAuth = Depends(verify_api_key),
):
    from backend.application.checkpointing.checkpointer import (
        get_checkpointer_from_request,
    )

    profile = await asyncio.to_thread(_ensure_agent_profile, payload.agent_code)
    requested_workflow = payload.resolve_workflow()
    if requested_workflow and requested_workflow not in SUPPORTED_WORKFLOWS:
        raise HTTPException(status_code=422, detail=f"不支持的工作流: {payload.workflow}")
    if requested_workflow and not profile.allows_workflow(requested_workflow):
        raise HTTPException(
            status_code=422,
            detail=f"智能体 {profile.code} 不允许工作流 {requested_workflow}",
        )

    public_metadata = {
        str(key): value
        for key, value in dict(payload.metadata or {}).items()
        if not str(key).startswith("_")
    }
    background_thread_id = payload.thread_id
    if requested_workflow == "deep_research" and not background_thread_id:
        background_thread_id = str(uuid.uuid4())
    agent_request = AgentRequest(
        question=payload.question,
        agent_code=payload.agent_code,
        user_id=auth.user_id,
        conversation_id=background_thread_id,
        requested_workflow=requested_workflow,
        selected_kb_ids=list(payload.kb_ids or []),
        selected_skills=(
            list(payload.selected_skills) if payload.selected_skills is not None else None
        ),
        model_name=payload.model,
        use_search=payload.use_search,
        subagent_enabled=payload.subagent_enabled,
        thinking_enabled=payload.thinking,
        attachments=[item.model_dump(exclude_none=True) for item in payload.attachments],
        metadata=public_metadata,
        auth_role=auth.role,
    )
    service = AgentService(checkpointer=get_checkpointer_from_request(request))
    try:
        agent_request = await service.resolve_request_skills(agent_request)
    except Exception as exc:
        from backend.application.skills.runtime_resolver import SkillRuntimeError

        if isinstance(exc, SkillRuntimeError):
            raise HTTPException(
                status_code=exc.status_code,
                detail={"code": exc.code, "message": str(exc)},
            ) from exc
        raise HTTPException(status_code=503, detail="Skill catalog 暂不可用") from exc
    uses_background_task = await asyncio.to_thread(
        _uses_deep_research_background,
        agent_request,
        profile,
    )
    if uses_background_task and not agent_request.conversation_id:
        agent_request = replace(agent_request, conversation_id=str(uuid.uuid4()))
    encoder = SSEEncoder()
    if uses_background_task:
        from backend.application.agent_tasks import (
            AgentTaskCapacityError,
            AgentTaskConflictError,
            agent_task_registry,
        )

        assert agent_request.conversation_id is not None
        try:
            record = await agent_task_registry.start(
                user_id=auth.user_id,
                agent_code=profile.code,
                thread_id=agent_request.conversation_id,
                question=agent_request.question,
                stream_factory=lambda: service.run_stream(agent_request),
            )
        except AgentTaskCapacityError as exc:
            raise HTTPException(
                status_code=429,
                detail=str(exc),
                headers={"Retry-After": "5"},
            ) from exc
        except AgentTaskConflictError as exc:
            raise HTTPException(
                status_code=409,
                detail={
                    "code": "agent_background_task_conflict",
                    "message": str(exc),
                    "task": exc.record.public_info(),
                },
            ) from exc
        return StreamingResponse(
            # The POST only acknowledges creation. The reconnectable task SSE
            # endpoint is the single owner of progress and final-answer delivery.
            encoder.encode_stream(agent_task_registry.startup_ack(record)),
            media_type="text/event-stream",
            headers={
                "Cache-Control": "no-cache, no-transform",
                "Connection": "keep-alive",
                "X-Accel-Buffering": "no",
            },
        )
    return StreamingResponse(
        encoder.encode_stream(service.run_stream(agent_request)),
        media_type="text/event-stream",
        headers={
            "Cache-Control": "no-cache, no-transform",
            "Connection": "keep-alive",
            "X-Accel-Buffering": "no",
        },
    )
