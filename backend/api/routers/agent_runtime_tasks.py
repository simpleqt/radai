"""Agent 运行时任务路由:query 执行入口、任务状态/事件流/取消/线程活跃任务(由主 router include)。"""

from __future__ import annotations

import asyncio
import uuid
from dataclasses import replace
from typing import Any

from fastapi import APIRouter, Depends, HTTPException, Query, Request
from fastapi.responses import StreamingResponse

from backend.api.dependencies.auth import ApiKeyAuth, verify_api_key
from backend.api.routers.agent_runtime_catalog import (
    _ensure_agent_profile,
)
from backend.api.schemas.agent_runtime import (
    AgentRuntimeQueryRequest,
)
from backend.application.agent_service import AgentService
from backend.application.event_stream.sse_encoder import SSEEncoder
from backend.application.skills.manifest_loader import SkillManifestLoader
from backend.config.loggers import get_logger
from backend.domain.agents.request import AgentRequest
from backend.domain.agents.workflows import SUPPORTED_WORKFLOWS, normalize_workflow_name

logger = get_logger(__name__)
router = APIRouter()


def _request_file_ids(metadata: dict[str, Any]) -> list[str]:
    raw = metadata.get("file_ids")
    if isinstance(raw, (list, tuple, set)):
        return [str(item).strip() for item in raw if str(item or "").strip()]
    value = str(raw or "").strip()
    return [value] if value else []


async def _uploaded_file_manifest(
    file_ids: list[str], user_id: str
) -> dict[str, dict[str, Any]]:
    """解析对话上传文件的展示名清单（file_id → display_name/mime/size）。

    供 prompt 注入使用，让模型知道每个 file_id 对应哪个文件（如成绩单/试卷/大纲）。
    权限复用下载解析：仅返回 owner 或有读授权的 active 文件；解析失败跳过，
    绝不因清单失败阻断主流程。
    """
    if not file_ids:
        return {}
    from backend.db.connection import get_async_session_factory
    from backend.services.user_file_service import resolve_user_file_for_download

    manifest: dict[str, dict[str, Any]] = {}
    factory = get_async_session_factory()
    async with factory() as session:
        for fid in file_ids:
            try:
                row, allowed, _reason = await resolve_user_file_for_download(
                    session=session, file_id=fid, current_user_id=user_id
                )
            except Exception:  # noqa: BLE001
                logger.exception("[AgentRuntime] 上传文件清单解析失败 file_id=%s", fid)
                continue
            if not allowed or row is None:
                continue
            manifest[fid] = {
                "display_name": row.display_name,
                "mime_type": row.mime_type or "",
                "size_bytes": row.size_bytes,
            }
    return manifest


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

    from backend.services.chat_image_analysis import (
        ChatImageAnalysisTool,
        ImageAnalysisUnavailable,
        UploadedImageAccessError,
        UploadedImageStorageError,
    )
    from backend.services.chat_model_catalog import (
        ChatModelCatalogUnavailable,
        ChatModelNotAvailable,
        resolve_active_chat_model,
    )

    file_ids = _request_file_ids(public_metadata)
    if file_ids:
        # 供 prompt 注入：file_id → 文件名清单（模型据此知道哪个文件是成绩单/试卷/大纲）
        public_metadata["_uploaded_file_manifest"] = await _uploaded_file_manifest(
            file_ids, auth.user_id
        )
    requested_model = str(payload.model or profile.default_model or "").strip() or None
    selected_model = None
    if requested_model or payload.attachments or file_ids:
        try:
            selected_model = await resolve_active_chat_model(requested_model)
        except ChatModelNotAvailable as exc:
            raise HTTPException(status_code=422, detail=str(exc)) from exc
        except ChatModelCatalogUnavailable as exc:
            raise HTTPException(status_code=503, detail=str(exc)) from exc

    prepared_attachments = [
        item.model_dump(exclude_none=True) for item in payload.attachments
    ]
    if payload.attachments or file_ids:
        assert selected_model is not None
        try:
            prepared = await ChatImageAnalysisTool().prepare(
                question=payload.question,
                selected_model=selected_model,
                direct_attachments=prepared_attachments,
                file_ids=file_ids,
                user_id=auth.user_id,
                thread_id=background_thread_id,
            )
        except UploadedImageAccessError as exc:
            raise HTTPException(status_code=422, detail=str(exc)) from exc
        except (UploadedImageStorageError, ImageAnalysisUnavailable) as exc:
            raise HTTPException(status_code=503, detail=str(exc)) from exc
        except Exception as exc:  # noqa: BLE001
            logger.exception("Chat image preprocessing failed")
            raise HTTPException(status_code=503, detail="图片预处理暂不可用") from exc
        prepared_attachments = prepared.attachments
        if prepared.analysis_context:
            public_metadata["_uploaded_image_analysis_context"] = (
                prepared.analysis_context
            )

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
        model_name=selected_model.name if selected_model is not None else None,
        use_search=payload.use_search,
        subagent_enabled=payload.subagent_enabled,
        thinking_enabled=payload.thinking,
        deep_retrieval=payload.deep_retrieval,
        attachments=prepared_attachments,
        metadata=public_metadata,
        auth_role=auth.role,
    )
    from backend.application.question_analytics import schedule_question_collection

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
    from backend.application.conversations.thread_guard import (
        ConversationThreadBusy,
        acquire_conversation_thread,
        conversation_lease_key,
    )

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
        except ConversationThreadBusy as exc:
            # A normal-mode stream currently owns this thread. Report the
            # caller's conversation id, not the internal user-scoped key.
            raise HTTPException(
                status_code=409,
                detail={
                    "code": "conversation_thread_busy",
                    "message": str(exc),
                    "thread_id": agent_request.conversation_id,
                },
            ) from exc
        # 采集只在请求被接受后进行：被 429/409 拒绝的提问不灌学情统计。
        schedule_question_collection(
            question=payload.question,
            user_id=auth.user_id,
            agent_code=payload.agent_code,
            role=auth.role,
            thread_id=agent_request.conversation_id,
            workflow=requested_workflow,
            model_name=selected_model.name if selected_model is not None else None,
        )
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
    # Two live streams on one LangGraph thread would fork the same parent
    # checkpoint and interleave conversation state; reject the second request
    # with the same conflict semantics as the background-task path.
    try:
        thread_lease = acquire_conversation_thread(
            conversation_lease_key(auth.user_id, agent_request.conversation_id)
        )
    except ConversationThreadBusy as exc:
        raise HTTPException(
            status_code=409,
            detail={
                "code": "conversation_thread_busy",
                "message": str(exc),
                "thread_id": agent_request.conversation_id,
            },
        ) from exc

    # 采集只在请求被接受后进行：被 409 拒绝的并发提问不灌学情统计。
    schedule_question_collection(
        question=payload.question,
        user_id=auth.user_id,
        agent_code=payload.agent_code,
        role=auth.role,
        thread_id=agent_request.conversation_id,
        workflow=requested_workflow,
        model_name=selected_model.name if selected_model is not None else None,
    )

    async def _guarded_stream():
        from backend.application.conversations.thread_guard import (
            start_lease_heartbeat,
            stop_lease_heartbeat,
        )

        # Keep the lease fresh for the whole live stream so the stale-lease
        # takeover bound only preempts genuinely dead holders.
        heartbeat = start_lease_heartbeat(thread_lease)
        try:
            async for item in service.run_stream(agent_request):
                yield item
        finally:
            stop_lease_heartbeat(heartbeat)
            if thread_lease is not None:
                thread_lease.release()

    try:
        stream = _guarded_stream()
    except Exception:
        if thread_lease is not None:
            thread_lease.release()
        raise
    return StreamingResponse(
        encoder.encode_stream(stream),
        media_type="text/event-stream",
        headers={
            "Cache-Control": "no-cache, no-transform",
            "Connection": "keep-alive",
            "X-Accel-Buffering": "no",
        },
    )
