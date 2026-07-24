"""探索式学习 API 路由（/api/v2/explore）。

提供发现态主题、思维导图生成/展开、知识点详解（SSE 流式）、关联资源、
习题练习（生成/评分）能力。登录可选：携带有效 token 时记录用户，否则按访客处理
（本期不做服务端持久化，由前端 localStorage 留存）。
"""

from __future__ import annotations

import json
import logging
import uuid
from pathlib import Path
from typing import Any

from fastapi import APIRouter, Depends, HTTPException, Query, Request
from fastapi.responses import FileResponse, Response, StreamingResponse
from pydantic import BaseModel, Field, field_validator
from sqlalchemy.ext.asyncio import AsyncSession

from backend.api.dependencies.auth import (
    ApiKeyAuth,
    optional_verify_api_key,
    require_kb_access,
)
from backend.api.routers.auth import get_current_user
from backend.application.checkpointing.checkpointer import get_checkpointer_from_request
from backend.application.event_stream.sse_encoder import SSEEncoder
from backend.application.explore import service as svc
from backend.db.connection import get_async_session
from backend.db.models import User
from backend.storage.streaming import iter_minio_response

logger = logging.getLogger(__name__)

router = APIRouter(prefix="/explore", tags=["explore"])


# ---------------------------------------------------------------------------
# 请求模型
# ---------------------------------------------------------------------------

class ExploreDomainRequest(BaseModel):
    """Explore 请求的领域边界：空值默认，显式未知值返回 422。"""

    domain: str | None = Field(
        default="ne",
        description="领域标识：ne=核电子学 / csh=场地污染水文地质 / fmca=基础力学",
    )

    @field_validator("domain", mode="before")
    @classmethod
    def validate_domain(cls, value: object) -> str:
        return svc.resolve_domain(None if value is None else str(value))


class TopicsRequest(ExploreDomainRequest):
    kb_ids: list[str] | None = Field(default=None, description="知识库 ID 列表（取首个）")
    count: int = Field(default=20, ge=1, le=50, description="主题数量")
    seed: str = Field(default="", description="聚焦方向（可选）")
    model: str | None = Field(default=None, description="模型名称（可选）")


class MapRequest(ExploreDomainRequest):
    topic: str = Field(..., description="探索主题")
    kb_ids: list[str] | None = None
    max_children: int = Field(default=6, ge=1, le=20, description="一级子节点数量上限")
    model: str | None = None


class ExpandRequest(ExploreDomainRequest):
    node_title: str = Field(..., description="待展开节点标题")
    path: list[str] = Field(default_factory=list, description="从根到该节点的路径")
    kb_ids: list[str] | None = None
    max_children: int = Field(default=5, ge=1, le=20, description="子节点数量上限")
    model: str | None = None


class DetailRequest(ExploreDomainRequest):
    node_title: str
    path: list[str] = Field(default_factory=list)
    kb_ids: list[str] | None = None
    model: str | None = None


class DetailChatMessage(BaseModel):
    role: str = Field(default="user")
    content: str = Field(default="")


class DetailChatRequest(ExploreDomainRequest):
    node_title: str
    question: str
    detail_content: str = Field(default="")
    path: list[str] = Field(default_factory=list)
    kb_ids: list[str] | None = None
    model: str | None = None
    history: list[DetailChatMessage] = Field(default_factory=list)


class ResourcesRequest(ExploreDomainRequest):
    node_title: str
    kb_ids: list[str] | None = None
    top_k: int = Field(default=8, ge=1, le=50)


class QuizRequest(ExploreDomainRequest):
    node_title: str
    kb_ids: list[str] | None = None
    types: list[str] = Field(default_factory=lambda: ["choice", "judge", "short"])
    difficulty: str = Field(default="all")
    count: int = Field(default=6, ge=1, le=30)
    model: str | None = None


class GradeRequest(ExploreDomainRequest):
    question: dict[str, Any]
    user_answer: str = ""
    model: str | None = None


# ---------------------------------------------------------------------------
# 工具
# ---------------------------------------------------------------------------

def _first_kb(kb_ids: list[str] | None) -> str | None:
    if not kb_ids:
        return None
    for kid in kb_ids:
        if str(kid or "").strip():
            return str(kid).strip()
    return None


async def _authorized_first_kb(
    kb_ids: list[str] | None,
    auth: ApiKeyAuth,
    session: AsyncSession,
) -> str | None:
    kb_id = _first_kb(kb_ids)
    if kb_id:
        await require_kb_access(session, kb_id, auth, access="read")
    return kb_id


# ---------------------------------------------------------------------------
# 路由
# ---------------------------------------------------------------------------

@router.post("/topics")
async def explore_topics(
    payload: TopicsRequest,
    auth: ApiKeyAuth = Depends(optional_verify_api_key),
    session: AsyncSession = Depends(get_async_session),
):
    kb_id = await _authorized_first_kb(payload.kb_ids, auth, session)
    return await svc.generate_topics(
        kb_id=kb_id,
        count=payload.count,
        seed=payload.seed,
        subject=svc.resolve_subject(payload.domain),
        model_name=payload.model,
    )


@router.post("/map")
async def explore_map(
    payload: MapRequest,
    auth: ApiKeyAuth = Depends(optional_verify_api_key),
    session: AsyncSession = Depends(get_async_session),
):
    kb_id = await _authorized_first_kb(payload.kb_ids, auth, session)
    return await svc.generate_map(
        topic=payload.topic,
        kb_id=kb_id,
        max_children=payload.max_children,
        subject=svc.resolve_subject(payload.domain),
        model_name=payload.model,
    )


@router.post("/expand")
async def explore_expand(
    payload: ExpandRequest,
    auth: ApiKeyAuth = Depends(optional_verify_api_key),
    session: AsyncSession = Depends(get_async_session),
):
    kb_id = await _authorized_first_kb(payload.kb_ids, auth, session)
    return await svc.expand_node(
        node_title=payload.node_title,
        path=payload.path,
        kb_id=kb_id,
        max_children=payload.max_children,
        subject=svc.resolve_subject(payload.domain),
        model_name=payload.model,
    )


@router.post("/resources")
async def explore_resources(
    payload: ResourcesRequest,
    auth: ApiKeyAuth = Depends(optional_verify_api_key),
    session: AsyncSession = Depends(get_async_session),
):
    kb_id = await _authorized_first_kb(payload.kb_ids, auth, session)
    return await svc.gather_resources(
        node_title=payload.node_title,
        kb_id=kb_id,
        top_k=payload.top_k,
    )


@router.get("/resources/download")
async def explore_resource_download(
    kb_id: str = Query(..., description="知识库 ID"),
    doc_id: str = Query(..., description="知识库文档 doc_id"),
    user: User = Depends(get_current_user),
    session: AsyncSession = Depends(get_async_session),
):
    """下载探索式学习关联的知识库原始文档。"""
    kb = str(kb_id or "").strip()
    doc = str(doc_id or "").strip()
    if not kb or not doc:
        raise HTTPException(status_code=400, detail="缺少 kb_id 或 doc_id")

    try:
        await require_kb_access(
            session,
            kb,
            ApiKeyAuth(
                api_key="",
                user_id=str(getattr(user, "user_id", "") or ""),
                role=str(getattr(user, "role", "user") or "user"),
            ),
            access="read",
        )

        payload, filename, media_type, source_kind = await svc.download_kb_resource(
            session,
            kb,
            doc,
            stream=True,
        )
    except HTTPException:
        raise
    except ValueError as exc:
        raise HTTPException(status_code=404, detail=str(exc)) from exc
    except Exception as exc:  # noqa: BLE001
        logger.exception("[Explore] 资源下载失败: kb=%s doc=%s", kb, doc)
        raise HTTPException(status_code=500, detail="资源下载失败，请稍后重试") from exc

    disposition = svc._build_content_disposition(filename)
    headers = {"Content-Disposition": disposition}

    if source_kind == "local" and isinstance(payload, Path):
        return FileResponse(path=str(payload), media_type=media_type, filename=filename, headers=headers)

    if isinstance(payload, (bytes, bytearray)):
        return Response(content=bytes(payload), media_type=media_type, headers=headers)
    if source_kind == "minio":
        return StreamingResponse(
            iter_minio_response(payload),
            media_type=media_type,
            headers=headers,
        )

    raise HTTPException(status_code=404, detail="找不到文档原始文件")


@router.post("/quiz")
async def explore_quiz(
    payload: QuizRequest,
    auth: ApiKeyAuth = Depends(optional_verify_api_key),
    session: AsyncSession = Depends(get_async_session),
):
    kb_id = await _authorized_first_kb(payload.kb_ids, auth, session)
    return await svc.generate_quiz(
        node_title=payload.node_title,
        kb_id=kb_id,
        types=payload.types,
        difficulty=payload.difficulty,
        count=payload.count,
        subject=svc.resolve_subject(payload.domain),
        model_name=payload.model,
    )


@router.post("/quiz/grade")
async def explore_quiz_grade(payload: GradeRequest):
    return await svc.grade_answer(
        question=payload.question,
        user_answer=payload.user_answer,
        subject=svc.resolve_subject(payload.domain),
        model_name=payload.model,
    )


@router.post("/detail")
async def explore_detail(
    payload: DetailRequest,
    request: Request,
    auth: ApiKeyAuth = Depends(optional_verify_api_key),
    session: AsyncSession = Depends(get_async_session),
):
    """知识点详解（SSE 流式）。

    委托统一 AgentRuntime 执行 lightweight 工作流。路由层只负责探索式学习参数转换，
    不再直接实例化具体智能体执行类。
    """
    kb_id = await _authorized_first_kb(payload.kb_ids, auth, session)
    question = svc.build_detail_question(payload.node_title, payload.path)
    agent_code = svc.resolve_agent_code(payload.domain)

    async def stream_generator():
        try:
            from backend.application.agent_service import AgentService
            from backend.domain.agents.request import AgentRequest

            checkpointer = get_checkpointer_from_request(request)
            thread_id = f"explore__{uuid.uuid4().hex}"
            state_user = getattr(request.state, "user", None)
            user_id = str(
                auth.user_id
                or getattr(request.state, "user_id", None)
                or getattr(state_user, "user_id", None)
                or "guest"
            )
            service = AgentService(checkpointer=checkpointer)
            agent_request = AgentRequest(
                question=question,
                agent_code=agent_code,
                user_id=user_id,
                conversation_id=thread_id,
                requested_workflow="lightweight",
                selected_kb_ids=[kb_id] if kb_id else [],
                selected_skills=[],
                model_name=svc.resolve_learning_fast_model_name(payload.model),
                use_search=False,  # 探索式学习详解仅使用知识库，不启用联网搜索。
                subagent_enabled=False,
                thinking_enabled=False,
                metadata={"source": "explore.detail"},
            )

            from backend.core.utils.async_tasks import iterate_until_disconnected

            encoder = SSEEncoder()
            upstream = service.run_stream(agent_request)
            async for event in iterate_until_disconnected(upstream, request):
                yield encoder.encode(event)
        except Exception as exc:  # noqa: BLE001
            logger.exception("[Explore] 详解流式失败: %s", exc)
            yield f"data: {json.dumps({'error': str(exc)}, ensure_ascii=False)}\n\n"
            yield "data: [DONE]\n\n"

    return StreamingResponse(
        stream_generator(),
        media_type="text/event-stream",
        headers={
            "Cache-Control": "no-cache",
            "Connection": "keep-alive",
            "X-Accel-Buffering": "no",
        },
    )


@router.post("/detail/chat")
async def explore_detail_chat(
    payload: DetailChatRequest,
    request: Request,
    auth: ApiKeyAuth = Depends(optional_verify_api_key),
    session: AsyncSession = Depends(get_async_session),
):
    """围绕当前知识点详解输出进行多轮追问（SSE 流式）。"""
    kb_id = await _authorized_first_kb(payload.kb_ids, auth, session)
    subject = svc.resolve_subject(payload.domain)

    async def stream_generator():
        try:
            async for kind, data in svc.stream_detail_followup_answer(
                node_title=payload.node_title,
                path=payload.path,
                user_question=payload.question,
                detail_content=payload.detail_content,
                kb_id=kb_id,
                subject=subject,
                history=[item.model_dump() for item in payload.history],
                model_name=payload.model,
            ):
                if await request.is_disconnected():
                    break
                if kind == "chunk":
                    yield (
                        "data: "
                        + json.dumps(
                            {"type": "message.delta", "payload": {"content": data}},
                            ensure_ascii=False,
                        )
                        + "\n\n"
                    )
                elif kind == "sources":
                    yield (
                        "data: "
                        + json.dumps(
                            {
                                "type": "event",
                                "payload": {
                                    "event": {
                                        "kind": "sources",
                                        "metadata": {"sources": data},
                                    }
                                },
                            },
                            ensure_ascii=False,
                        )
                        + "\n\n"
                    )
                elif kind == "error":
                    yield (
                        "data: "
                        + json.dumps(
                            {"type": "error", "payload": {"message": data}},
                            ensure_ascii=False,
                        )
                        + "\n\n"
                    )
            yield "data: [DONE]\n\n"
        except Exception as exc:  # noqa: BLE001
            logger.exception("[Explore] 详解追问流式失败: %s", exc)
            yield (
                "data: "
                + json.dumps(
                    {"type": "error", "payload": {"message": "追问回答生成失败，请稍后重试"}},
                    ensure_ascii=False,
                )
                + "\n\n"
            )
            yield "data: [DONE]\n\n"

    return StreamingResponse(
        stream_generator(),
        media_type="text/event-stream",
        headers={
            "Cache-Control": "no-cache",
            "Connection": "keep-alive",
            "X-Accel-Buffering": "no",
        },
    )
