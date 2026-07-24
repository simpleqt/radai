"""Admin APIs for self-ingestion audit, repair, and health surfaces."""

from __future__ import annotations

import asyncio
import hashlib
from datetime import datetime
from typing import Any, Literal

from fastapi import APIRouter, Depends, HTTPException, Query
from pydantic import BaseModel, Field
from sqlalchemy import desc, func, select
from sqlalchemy.ext.asyncio import AsyncSession

from backend.api.dependencies.auth import ApiKeyAuth, get_db_session, require_admin
from backend.config.loggers import get_logger
from backend.db.models import SelfIngestJob
from backend.rag.ingestion import states

router = APIRouter(prefix="/admin/self-ingestion", tags=["自研入库管理"])
logger = get_logger(__name__)

QualityKind = Literal["candidate", "dropped"]
QualityKindOrAll = Literal["candidate", "dropped", "all"]


class QualityFactItem(BaseModel):
    id: int
    kind: QualityKind
    kb_id: str
    job_id: str | None = None
    doc_id: str
    item_kind: str
    item_id: str
    item_type: str | None = None
    confidence: int
    reason: str
    auto_action: str
    requires_manual_review: bool = False
    payload: dict[str, Any] | list[Any] | str | int | float | bool | None = None
    created_at: datetime | None = None


class QualityFactListResponse(BaseModel):
    items: list[QualityFactItem]
    total: int
    limit: int
    offset: int


class QualityFactReasonStat(BaseModel):
    kind: QualityKind
    item_kind: str
    reason: str
    count: int


class QualityFactStatsResponse(BaseModel):
    total_candidates: int
    total_dropped: int
    by_reason: list[QualityFactReasonStat] = Field(default_factory=list)


def _quality_model_for_kind(kind: QualityKind):
    """Validate quality fact kind.

    Quality fact payloads now live in SelfIngestJob.metrics_json/extraction
    instead of the old MySQL fact tables.
    """
    if kind not in {"candidate", "dropped"}:
        raise HTTPException(status_code=400, detail="kind 必须是 candidate 或 dropped")
    return kind


def _fact_synthetic_id(kind: str, job_id: str, index: int, item: dict[str, Any]) -> int:
    raw = "|".join(
        [
            kind,
            job_id,
            str(index),
            str(item.get("item_kind") or ""),
            str(item.get("item_id") or ""),
            str(item.get("reason") or ""),
        ]
    )
    return int(hashlib.sha1(raw.encode("utf-8")).hexdigest()[:15], 16)


def _quality_facts_from_job(job: SelfIngestJob, kind: QualityKind) -> list[QualityFactItem]:
    metrics = dict(getattr(job, "metrics_json", None) or {})
    extraction = dict(metrics.get("extraction") or {})
    raw_items = extraction.get("candidate_facts" if kind == "candidate" else "dropped_facts") or []
    created_at = getattr(job, "finished_at", None) or getattr(job, "updated_at", None) or getattr(job, "created_at", None)
    items: list[QualityFactItem] = []
    for index, raw in enumerate(raw_items):
        if not isinstance(raw, dict):
            continue
        item_kind = str(raw.get("item_kind") or "")
        item_id = str(raw.get("item_id") or "")
        if not item_kind or not item_id:
            continue
        items.append(
            QualityFactItem(
                id=_fact_synthetic_id(kind, job.job_id, index, raw),
                kind=kind,
                kb_id=job.kb_id,
                job_id=job.job_id,
                doc_id=job.doc_id or "",
                item_kind=item_kind,
                item_id=item_id,
                item_type=raw.get("item_type"),
                confidence=int(raw.get("confidence") or 0),
                reason=str(raw.get("reason") or ""),
                auto_action=str(raw.get("auto_action") or ""),
                requires_manual_review=bool(raw.get("requires_manual_review", False)),
                payload=raw.get("payload"),
                created_at=created_at,
            )
        )
    return items


async def _list_quality_rows(
    session: AsyncSession,
    *,
    kind: QualityKind,
    limit: int,
    offset: int,
    kb_id: str | None = None,
    job_id: str | None = None,
    doc_id: str | None = None,
    item_kind: str | None = None,
    reason: str | None = None,
) -> tuple[list[QualityFactItem], int]:
    _quality_model_for_kind(kind)
    conditions = []
    if kb_id:
        conditions.append(SelfIngestJob.kb_id == kb_id)
    if job_id:
        conditions.append(SelfIngestJob.job_id == job_id)
    if doc_id:
        conditions.append(SelfIngestJob.doc_id == doc_id)

    stmt = select(SelfIngestJob)
    if conditions:
        stmt = stmt.where(*conditions)
    stmt = stmt.order_by(desc(SelfIngestJob.created_at)).limit(max(limit + offset, 1) * 20)
    jobs = (await session.execute(stmt)).scalars().all()

    items: list[QualityFactItem] = []
    for job in jobs:
        for fact in _quality_facts_from_job(job, kind):
            if item_kind and fact.item_kind != item_kind:
                continue
            if reason and fact.reason != reason:
                continue
            items.append(fact)

    items.sort(key=lambda item: (item.created_at or datetime.min, item.id), reverse=True)
    total = len(items)
    return items[offset : offset + limit], total


@router.get("/quality-facts", response_model=QualityFactListResponse)
async def list_quality_facts(
    kind: QualityKindOrAll = Query("all", description="candidate/dropped/all"),
    kb_id: str | None = Query(None),
    job_id: str | None = Query(None),
    doc_id: str | None = Query(None),
    item_kind: str | None = Query(None, description="entity/relation"),
    reason: str | None = Query(None),
    limit: int = Query(50, ge=1, le=200),
    offset: int = Query(0, ge=0),
    session: AsyncSession = Depends(get_db_session),
    _auth: ApiKeyAuth = Depends(require_admin),
) -> QualityFactListResponse:
    if kind != "all":
        items, total = await _list_quality_rows(
            session,
            kind=kind,
            limit=limit,
            offset=offset,
            kb_id=kb_id,
            job_id=job_id,
            doc_id=doc_id,
            item_kind=item_kind,
            reason=reason,
        )
        return QualityFactListResponse(items=items, total=total, limit=limit, offset=offset)

    candidate_items, candidate_total = await _list_quality_rows(
        session,
        kind="candidate",
        limit=limit + offset,
        offset=0,
        kb_id=kb_id,
        job_id=job_id,
        doc_id=doc_id,
        item_kind=item_kind,
        reason=reason,
    )
    dropped_items, dropped_total = await _list_quality_rows(
        session,
        kind="dropped",
        limit=limit + offset,
        offset=0,
        kb_id=kb_id,
        job_id=job_id,
        doc_id=doc_id,
        item_kind=item_kind,
        reason=reason,
    )
    merged = sorted(
        candidate_items + dropped_items,
        key=lambda item: (item.created_at or datetime.min, item.id),
        reverse=True,
    )
    return QualityFactListResponse(
        items=merged[offset : offset + limit],
        total=candidate_total + dropped_total,
        limit=limit,
        offset=offset,
    )


@router.get("/quality-facts/{kind}/{fact_id}", response_model=QualityFactItem)
async def get_quality_fact(
    kind: QualityKind,
    fact_id: int,
    session: AsyncSession = Depends(get_db_session),
    _auth: ApiKeyAuth = Depends(require_admin),
) -> QualityFactItem:
    _quality_model_for_kind(kind)
    items, _ = await _list_quality_rows(
        session,
        kind=kind,
        limit=10000,
        offset=0,
    )
    for item in items:
        if item.id == fact_id:
            return item
    raise HTTPException(status_code=404, detail="质量事实不存在")


@router.get("/quality-facts/stats", response_model=QualityFactStatsResponse)
async def get_quality_fact_stats(
    kb_id: str | None = Query(None),
    job_id: str | None = Query(None),
    doc_id: str | None = Query(None),
    session: AsyncSession = Depends(get_db_session),
    _auth: ApiKeyAuth = Depends(require_admin),
) -> QualityFactStatsResponse:
    candidate_items, total_candidates = await _list_quality_rows(
        session,
        kind="candidate",
        limit=10000,
        offset=0,
        kb_id=kb_id,
        job_id=job_id,
        doc_id=doc_id,
    )
    dropped_items, total_dropped = await _list_quality_rows(
        session,
        kind="dropped",
        limit=10000,
        offset=0,
        kb_id=kb_id,
        job_id=job_id,
        doc_id=doc_id,
    )
    counts: dict[tuple[str, str, str], int] = {}
    for fact in [*candidate_items, *dropped_items]:
        key = (fact.kind, fact.item_kind, fact.reason)
        counts[key] = counts.get(key, 0) + 1
    return QualityFactStatsResponse(
        total_candidates=total_candidates,
        total_dropped=total_dropped,
        by_reason=[
            QualityFactReasonStat(kind=kind, item_kind=item_kind, reason=reason, count=count)
            for (kind, item_kind, reason), count in sorted(counts.items())
        ],
    )


# ---- Health Check (§25.4) ----

class HealthComponentStatus(BaseModel):
    name: str
    status: str
    detail: str | None = None


class SelfIngestionHealthResponse(BaseModel):
    status: str
    queue_depth: int = 0
    components: list[HealthComponentStatus] = Field(default_factory=list)


@router.get("/health", response_model=SelfIngestionHealthResponse)
async def self_ingestion_health(
    session: AsyncSession = Depends(get_db_session),
    _auth: ApiKeyAuth = Depends(require_admin),
) -> SelfIngestionHealthResponse:
    """Health check for self-ingestion subsystem (§25.4)."""
    from backend.config import settings

    components: list[HealthComponentStatus] = []
    overall = "healthy"

    # MySQL
    try:
        await session.execute(select(func.count()).select_from(SelfIngestJob))
        components.append(HealthComponentStatus(name="mysql", status="ok"))
    except Exception as exc:
        components.append(HealthComponentStatus(name="mysql", status="error", detail=str(exc)[:200]))
        overall = "degraded"

    # Queue depth
    queue_depth = 0
    try:
        from backend.db.models import PendingFile
        queue_depth = int(
            (await session.execute(
                select(func.count()).select_from(PendingFile).where(PendingFile.status == 1)
            )).scalar() or 0
        )
    except Exception:
        pass

    # Milvus
    try:
        if settings.milvus_connection_uri:
            from backend.rag.retriever_v2.direct_retriever import (
                _ensure_milvus_connection,
            )

            await asyncio.wait_for(
                asyncio.to_thread(_ensure_milvus_connection),
                timeout=5.0,
            )
            components.append(HealthComponentStatus(name="milvus", status="ok"))
        else:
            components.append(HealthComponentStatus(name="milvus", status="skipped", detail="not configured"))
    except Exception as exc:
        components.append(HealthComponentStatus(name="milvus", status="error", detail=str(exc)[:200]))
        overall = "degraded"

    # Redis
    try:
        if settings.redis_host:
            from backend.db.redis import get_redis
            client = get_redis()
            await client.ping()
            await client.aclose()
            components.append(HealthComponentStatus(name="redis", status="ok"))
        else:
            components.append(HealthComponentStatus(name="redis", status="skipped", detail="not configured"))
    except Exception as exc:
        components.append(HealthComponentStatus(name="redis", status="error", detail=str(exc)[:200]))
        overall = "degraded"

    # Nebula
    try:
        if settings.nebula_host:
            components.append(HealthComponentStatus(name="nebula", status="configured"))
        else:
            components.append(HealthComponentStatus(name="nebula", status="skipped", detail="not configured"))
    except Exception as exc:
        components.append(HealthComponentStatus(name="nebula", status="error", detail=str(exc)[:200]))

    # Embedding
    try:
        api_base = settings.embedding_config.get("api_base", "")
        model = settings.embedding_config.get("model", "")
        if api_base and model:
            components.append(HealthComponentStatus(name="embedding", status="configured", detail=model))
        else:
            components.append(HealthComponentStatus(name="embedding", status="mock", detail="using mock embeddings"))
    except Exception as exc:
        components.append(HealthComponentStatus(name="embedding", status="error", detail=str(exc)[:200]))

    from backend.rag.ingestion.extraction.llm_extractor import _resolve_llm_model_name
    from backend.rag.ingestion.extraction.vision_extractor import _resolve_vision_model_name

    llm_model = _resolve_llm_model_name()
    vision_model = _resolve_vision_model_name()
    require_llm = bool(getattr(settings, "self_extraction_require_llm", True))
    if require_llm and not llm_model:
        components.append(
            HealthComponentStatus(
                name="extraction_llm",
                status="error",
                detail="SELF_EXTRACTION_LLM_MODEL 未配置",
            )
        )
        overall = "degraded"
    elif llm_model:
        components.append(HealthComponentStatus(name="extraction_llm", status="configured", detail=llm_model))
    else:
        components.append(HealthComponentStatus(name="extraction_llm", status="optional", detail="not required"))

    if vision_model:
        components.append(HealthComponentStatus(name="extraction_vision", status="configured", detail=vision_model))
    else:
        components.append(
            HealthComponentStatus(
                name="extraction_vision",
                status="warning",
                detail="SELF_VISION_MODEL 未配置，图片块将回退文本 LLM",
            )
        )

    return SelfIngestionHealthResponse(
        status=overall,
        queue_depth=queue_depth,
        components=components,
    )


# ---- Job Management (§28.2) ----

class JobListItem(BaseModel):
    job_id: str
    kb_id: str
    file_id: str
    doc_id: str | None = None
    status: str
    stage: str | None = None
    error_message: str | None = None
    chunk_count: int = 0
    vector_count: int = 0
    retry_count: int = 0
    staging_uri: str | None = None
    started_at: datetime | None = None
    finished_at: datetime | None = None
    created_at: datetime | None = None


class JobListResponse(BaseModel):
    items: list[JobListItem]
    total: int
    limit: int
    offset: int


class JobRepairResponse(BaseModel):
    job_id: str
    result: dict[str, Any]


class JobStatsResponse(BaseModel):
    total: int
    by_status: dict[str, int]
    failed_count: int
    partial_count: int
    completed_count: int
    processing_count: int


@router.get("/jobs", response_model=JobListResponse)
async def list_jobs(
    kb_id: str | None = Query(None),
    status: str | None = Query(None),
    limit: int = Query(50, ge=1, le=200),
    offset: int = Query(0, ge=0),
    session: AsyncSession = Depends(get_db_session),
    _auth: ApiKeyAuth = Depends(require_admin),
) -> JobListResponse:
    conditions = []
    if kb_id:
        conditions.append(SelfIngestJob.kb_id == kb_id)
    if status:
        conditions.append(SelfIngestJob.status == status)
    stmt = select(SelfIngestJob)
    if conditions:
        stmt = stmt.where(*conditions)
    stmt = stmt.order_by(desc(SelfIngestJob.created_at)).limit(limit).offset(offset)
    rows = (await session.execute(stmt)).scalars().all()

    count_stmt = select(func.count()).select_from(SelfIngestJob)
    if conditions:
        count_stmt = count_stmt.where(*conditions)
    total = int((await session.execute(count_stmt)).scalar() or 0)

    return JobListResponse(
        items=[
            JobListItem(
                job_id=j.job_id, kb_id=j.kb_id, file_id=j.file_id,
                doc_id=j.doc_id, status=j.status, stage=j.stage,
                error_message=j.error_message,
                chunk_count=j.chunk_count or 0, vector_count=j.vector_count or 0,
                retry_count=j.retry_count or 0,
                staging_uri=getattr(j, "staging_uri", None),
                started_at=j.started_at, finished_at=j.finished_at,
                created_at=j.created_at,
            )
            for j in rows
        ],
        total=total, limit=limit, offset=offset,
    )


@router.get("/jobs/stats", response_model=JobStatsResponse)
async def get_job_stats(
    kb_id: str | None = Query(None),
    session: AsyncSession = Depends(get_db_session),
    _auth: ApiKeyAuth = Depends(require_admin),
) -> JobStatsResponse:
    base = select(SelfIngestJob.status, func.count()).select_from(SelfIngestJob)
    if kb_id:
        base = base.where(SelfIngestJob.kb_id == kb_id)
    base = base.group_by(SelfIngestJob.status)
    rows = (await session.execute(base)).all()
    by_status = {str(status): int(count) for status, count in rows}
    total = sum(by_status.values())
    return JobStatsResponse(
        total=total,
        by_status=by_status,
        failed_count=by_status.get(states.JOB_FAILED, 0),
        partial_count=by_status.get(states.JOB_PARTIAL_SUCCESS, 0),
        completed_count=by_status.get(states.JOB_COMPLETED, 0),
        processing_count=by_status.get(states.JOB_PROCESSING, 0),
    )


@router.post("/jobs/{job_id}/repair", response_model=JobRepairResponse)
async def repair_job(
    job_id: str,
    _auth: ApiKeyAuth = Depends(require_admin),
) -> JobRepairResponse:
    from backend.rag.ingestion.repair_worker import SelfIngestionRepairWorker
    worker = SelfIngestionRepairWorker()
    result = await worker.repair_job(job_id)
    return JobRepairResponse(job_id=job_id, result=result)


@router.post("/repair/scan")
async def scan_and_repair(
    batch_size: int = Query(10, ge=1, le=100),
    _auth: ApiKeyAuth = Depends(require_admin),
) -> dict[str, Any]:
    from backend.rag.ingestion.repair_worker import SelfIngestionRepairWorker
    worker = SelfIngestionRepairWorker()
    return await worker.scan_and_repair(batch_size=batch_size)


class SwitchBackendRequest(BaseModel):
    ingestion_backend: Literal["legacy_lightrag", "self_ingestion"] | None = None
    retrieval_backend: Literal["legacy_lightrag", "self_ingestion"] | None = None
    self_ingestion_enabled: bool | None = None
    self_retriever_enabled: bool | None = None
    reason: str = Field("", max_length=500, description="切换原因（审计）")


class SwitchBackendResponse(BaseModel):
    kb_id: str
    kb_code: str
    ingestion_backend: str
    retrieval_backend: str
    self_ingestion_enabled: bool
    self_retriever_enabled: bool
    audit_note: str


@router.post("/kb/{kb_id}/switch-backend", response_model=SwitchBackendResponse)
async def switch_kb_backend(
    kb_id: str,
    body: SwitchBackendRequest,
    session: AsyncSession = Depends(get_db_session),
    _auth: ApiKeyAuth = Depends(require_admin),
) -> SwitchBackendResponse:
    """管理员显式切换知识库入库/查询后端（§28.2）。"""
    from backend.config import settings as app_settings
    from backend.db.models import KnowledgeBase
    from backend.domain.knowledge_base.backend_policy import (
        LEGACY_BACKEND,
        SELF_BACKEND,
        validate_backend_isolation,
    )

    kb = (
        await session.execute(select(KnowledgeBase).where(KnowledgeBase.kb_id == kb_id))
    ).scalar_one_or_none()
    if not kb:
        raise HTTPException(status_code=404, detail="知识库不存在")

    new_ingestion = body.ingestion_backend or kb.ingestion_backend or LEGACY_BACKEND
    new_retrieval = body.retrieval_backend or kb.retrieval_backend or LEGACY_BACKEND
    new_self_ingest = (
        body.self_ingestion_enabled
        if body.self_ingestion_enabled is not None
        else bool(kb.self_ingestion_enabled)
    )
    new_self_retriever = (
        body.self_retriever_enabled
        if body.self_retriever_enabled is not None
        else bool(kb.self_retriever_enabled)
    )

    if new_ingestion == SELF_BACKEND:
        new_self_ingest = True
    if new_retrieval == SELF_BACKEND:
        new_self_retriever = True

    validate_backend_isolation(
        ingestion_backend=new_ingestion,
        retrieval_backend=new_retrieval,
        self_ingestion_enabled=bool(app_settings.self_ingestion_enabled) and new_self_ingest,
        self_retriever_enabled=bool(app_settings.self_retriever_enabled) and new_self_retriever,
    )

    kb.ingestion_backend = new_ingestion
    kb.retrieval_backend = new_retrieval
    kb.self_ingestion_enabled = new_self_ingest
    kb.self_retriever_enabled = new_self_retriever
    kb.updated_at = datetime.now()
    await session.commit()

    audit = body.reason.strip() or "admin switch-backend"
    logger.info(
        "[self-ingestion] switch-backend kb=%s ingestion=%s retrieval=%s reason=%s",
        kb.kb_code, new_ingestion, new_retrieval, audit,
    )
    return SwitchBackendResponse(
        kb_id=kb.kb_id,
        kb_code=kb.kb_code,
        ingestion_backend=new_ingestion,
        retrieval_backend=new_retrieval,
        self_ingestion_enabled=new_self_ingest,
        self_retriever_enabled=new_self_retriever,
        audit_note=audit,
    )


class ShadowRebuildRequest(BaseModel):
    kb_id: str | None = None
    kb_code: str | None = None
    limit: int = Field(20, ge=1, le=500)
    dry_run: bool = True
    reason: str = Field("", max_length=500)


class ShadowRebuildResponse(BaseModel):
    kb_id: str
    kb_code: str
    dry_run: bool
    queued: int
    items: list[dict[str, Any]] = Field(default_factory=list)
    note: str


@router.post("/shadow-rebuild", response_model=ShadowRebuildResponse)
async def shadow_rebuild_kb(
    body: ShadowRebuildRequest,
    _auth: ApiKeyAuth = Depends(require_admin),
) -> ShadowRebuildResponse:
    """将 legacy 文档 shadow 入队到 self_ingestion，不修改 legacy 索引（§24.4）。"""
    import uuid

    from backend.db.connection import async_session_context
    from backend.db.models import KnowledgeBase, KnowledgeBaseDocument, PendingFile
    from backend.domain.knowledge_base.backend_policy import LEGACY_BACKEND, SELF_BACKEND

    if not body.kb_id and not body.kb_code:
        raise HTTPException(status_code=400, detail="kb_id 或 kb_code 必填其一")

    async with async_session_context() as session:
        stmt = select(KnowledgeBase)
        if body.kb_id:
            stmt = stmt.where(KnowledgeBase.kb_id == body.kb_id)
        else:
            stmt = stmt.where(KnowledgeBase.kb_code == body.kb_code)
        kb = (await session.execute(stmt)).scalar_one_or_none()
        if not kb:
            raise HTTPException(status_code=404, detail="知识库不存在")

        docs = (
            await session.execute(
                select(KnowledgeBaseDocument)
                .where(
                    KnowledgeBaseDocument.kb_id == kb.kb_id,
                    KnowledgeBaseDocument.ingestion_backend == LEGACY_BACKEND,
                    KnowledgeBaseDocument.storage_status == 1,
                )
                .order_by(KnowledgeBaseDocument.updated_at.desc())
                .limit(body.limit)
            )
        ).scalars().all()

        queued_items: list[dict[str, Any]] = []
        for doc in docs:
            file_id = str(uuid.uuid4())
            item = {
                "file_id": file_id,
                "doc_id": doc.doc_id,
                "file_name": doc.file_name,
                "minio_path": doc.file_path or doc.object_storage_uri,
            }
            queued_items.append(item)
            if body.dry_run:
                continue
            session.add(
                PendingFile(
                    file_id=file_id,
                    kb_id=kb.kb_id,
                    file_name=doc.file_name or f"{doc.doc_id}.md",
                    file_type=doc.mime_type or "text/plain",
                    file_ext=doc.file_ext or "",
                    minio_path=doc.file_path or doc.object_storage_uri or "",
                    source_hash=doc.source_hash,
                    mime_type=doc.mime_type,
                    ingestion_backend=SELF_BACKEND,
                    status=1,
                    created_at=datetime.now(),
                    updated_at=datetime.now(),
                )
            )
        if not body.dry_run:
            await session.commit()

    audit = body.reason.strip() or "admin shadow-rebuild"
    logger.info(
        "[self-ingestion] shadow-rebuild kb=%s queued=%d dry_run=%s reason=%s",
        kb.kb_code, len(queued_items), body.dry_run, audit,
    )
    return ShadowRebuildResponse(
        kb_id=kb.kb_id,
        kb_code=kb.kb_code,
        dry_run=body.dry_run,
        queued=len(queued_items),
        items=queued_items[:20],
        note="legacy 索引未修改；worker 仅写入 v2 命名空间",
    )
