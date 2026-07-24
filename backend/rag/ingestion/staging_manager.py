"""Staging manager (§13) -- serialize/store/load StagingPayload via MinIO.

StagingPayload is the sole input to CommitWriter. It captures the full
intermediate state (blocks, chunks, extraction, metrics) so that:
1. Parsing and committing are decoupled.
2. A failed commit can be replayed from the stored payload.
3. Repair worker can load the payload and retry specific storage stages.
"""

from __future__ import annotations

import asyncio
import json
from dataclasses import asdict, is_dataclass
from datetime import date, datetime
from io import BytesIO
from typing import Any

from backend.config import settings
from backend.config.loggers import get_logger
from backend.rag.ingestion.extraction.simple_extractor import (
    ExtractedEntity,
    ExtractedRelation,
    ExtractionResult,
)
from backend.rag.ingestion.models import IngestBlock, IngestChunk, SourceDocument

logger = get_logger(__name__)


class StagingPayload:
    """Immutable staging snapshot for CommitWriter (§13.2)."""

    __slots__ = (
        "job_id", "kb_id", "kb_code", "doc_id", "commit_version",
        "source_document", "blocks", "chunks", "vectors", "entity_vectors", "media_vectors",
        "extraction", "extraction_audit", "quality_audit",
        "candidates", "dropped_logs",
        "metrics", "created_at",
    )

    def __init__(
        self,
        *,
        job_id: str,
        kb_id: str,
        kb_code: str,
        doc_id: str,
        commit_version: int,
        source_document: SourceDocument | None = None,
        blocks: list[IngestBlock] | None = None,
        chunks: list[IngestChunk] | None = None,
        vectors: list[list[float]] | None = None,
        entity_vectors: list[list[float]] | None = None,
        media_vectors: list[list[float]] | None = None,
        extraction: ExtractionResult | None = None,
        extraction_audit: dict | None = None,
        quality_audit: dict | None = None,
        candidates: list[dict] | None = None,
        dropped_logs: list[dict] | None = None,
        metrics: dict | None = None,
        created_at: datetime | None = None,
    ):
        self.job_id = job_id
        self.kb_id = kb_id
        self.kb_code = kb_code
        self.doc_id = doc_id
        self.commit_version = commit_version
        self.source_document = source_document
        self.blocks = blocks or []
        self.chunks = chunks or []
        self.vectors = vectors or []
        self.entity_vectors = entity_vectors or []
        self.media_vectors = media_vectors or []
        self.extraction = extraction
        self.extraction_audit = extraction_audit or {}
        self.quality_audit = quality_audit or {}
        self.candidates = candidates or []
        self.dropped_logs = dropped_logs or []
        self.metrics = metrics or {}
        self.created_at = created_at or datetime.now()


def staging_uri(kb_id: str, job_id: str) -> str:
    return f"staging/{kb_id}/{job_id}/payload.json"


def _json_default(obj: Any) -> Any:
    if isinstance(obj, datetime):
        return obj.isoformat()
    if isinstance(obj, date):
        return obj.isoformat()
    if isinstance(obj, set):
        return sorted(obj)
    if is_dataclass(obj) and not isinstance(obj, type):
        return asdict(obj)
    if hasattr(obj, "__dict__"):
        return {k: v for k, v in obj.__dict__.items() if not k.startswith("_")}
    return str(obj)


def _serialize_extraction(extraction: ExtractionResult | None) -> dict:
    if extraction is None:
        return {}
    entities = {}
    for eid, entity in extraction.entities.items():
        entities[eid] = {
            "entity_id": entity.entity_id,
            "name": entity.name,
            "normalized_name": entity.normalized_name,
            "entity_type": entity.entity_type,
            "confidence": entity.confidence,
            "status": getattr(entity, "status", "active"),
            "doc_ids": sorted(entity.doc_ids),
            "chunk_ids": sorted(entity.chunk_ids),
            "evidence": dict(entity.evidence),
        }
    relations = {}
    for rid, relation in extraction.relations.items():
        relations[rid] = {
            "relation_id": relation.relation_id,
            "source_entity_id": relation.source_entity_id,
            "target_entity_id": relation.target_entity_id,
            "relation_type": relation.relation_type,
            "confidence": relation.confidence,
            "status": getattr(relation, "status", "weak_evidence"),
            "chunk_ids": sorted(relation.chunk_ids),
            "evidence": dict(relation.evidence),
        }
    return {"entities": entities, "relations": relations}


def _deserialize_extraction(data: dict) -> ExtractionResult:
    entities: dict[str, ExtractedEntity] = {}
    for eid, edata in (data.get("entities") or {}).items():
        entity = ExtractedEntity(
            entity_id=edata["entity_id"],
            name=edata["name"],
            normalized_name=edata.get("normalized_name", edata["name"]),
            entity_type=edata.get("entity_type", "term"),
            confidence=int(edata.get("confidence", 60)),
        )
        entity.status = edata.get("status", "active")
        entity.doc_ids = set(edata.get("doc_ids", []))
        entity.chunk_ids = set(edata.get("chunk_ids", []))
        entity.evidence = dict(edata.get("evidence", {}))
        entities[eid] = entity
    relations: dict[str, ExtractedRelation] = {}
    for rid, rdata in (data.get("relations") or {}).items():
        relation = ExtractedRelation(
            relation_id=rdata["relation_id"],
            source_entity_id=rdata["source_entity_id"],
            target_entity_id=rdata["target_entity_id"],
            relation_type=rdata.get("relation_type", "co_occurs"),
            confidence=int(rdata.get("confidence", 55)),
        )
        relation.status = rdata.get("status", "weak_evidence")
        relation.chunk_ids = set(rdata.get("chunk_ids", []))
        relation.evidence = dict(rdata.get("evidence", {}))
        relations[rid] = relation
    return ExtractionResult(entities=entities, relations=relations)


def serialize_payload(payload: StagingPayload) -> bytes:
    data = {
        "job_id": payload.job_id,
        "kb_id": payload.kb_id,
        "kb_code": payload.kb_code,
        "doc_id": payload.doc_id,
        "commit_version": payload.commit_version,
        "source_document": asdict(payload.source_document) if payload.source_document else None,
        "blocks": [asdict(b) for b in payload.blocks],
        "chunks": [asdict(c) for c in payload.chunks],
        "vectors": payload.vectors,
        "entity_vectors": payload.entity_vectors,
        "media_vectors": payload.media_vectors,
        "extraction": _serialize_extraction(payload.extraction),
        "extraction_audit": payload.extraction_audit,
        "quality_audit": payload.quality_audit,
        "candidates": payload.candidates,
        "dropped_logs": payload.dropped_logs,
        "metrics": payload.metrics,
        "created_at": payload.created_at.isoformat() if payload.created_at else None,
    }
    return json.dumps(data, ensure_ascii=False, default=_json_default).encode("utf-8")


def deserialize_payload(raw: bytes) -> StagingPayload:
    data = json.loads(raw.decode("utf-8"))
    sd = None
    if data.get("source_document"):
        sd_data = data["source_document"]
        sd = SourceDocument(**{
            k: v for k, v in sd_data.items()
            if k in SourceDocument.__dataclass_fields__
        })
    blocks = [
        IngestBlock(**{k: v for k, v in b.items() if k in IngestBlock.__dataclass_fields__})
        for b in (data.get("blocks") or [])
    ]
    chunks = [
        IngestChunk(**{k: v for k, v in c.items() if k in IngestChunk.__dataclass_fields__})
        for c in (data.get("chunks") or [])
    ]
    extraction = _deserialize_extraction(data.get("extraction") or {})
    created_at = None
    if data.get("created_at"):
        try:
            created_at = datetime.fromisoformat(data["created_at"])
        except (ValueError, TypeError):
            created_at = datetime.now()
    return StagingPayload(
        job_id=data["job_id"],
        kb_id=data["kb_id"],
        kb_code=data["kb_code"],
        doc_id=data["doc_id"],
        commit_version=int(data.get("commit_version", 1)),
        source_document=sd,
        blocks=blocks,
        chunks=chunks,
        vectors=data.get("vectors") or [],
        entity_vectors=data.get("entity_vectors") or [],
        media_vectors=data.get("media_vectors") or [],
        extraction=extraction,
        extraction_audit=data.get("extraction_audit") or {},
        quality_audit=data.get("quality_audit") or {},
        candidates=data.get("candidates") or [],
        dropped_logs=data.get("dropped_logs") or [],
        metrics=data.get("metrics") or {},
        created_at=created_at,
    )


async def save_staging_payload(payload: StagingPayload) -> str:
    """Serialize and upload StagingPayload to MinIO. Returns the object URI."""
    from backend.storage import _get_minio_client
    uri = staging_uri(payload.kb_id, payload.job_id)
    raw = await asyncio.to_thread(serialize_payload, payload)
    client = _get_minio_client()
    bucket = settings.minio_bucket_name
    try:
        await asyncio.to_thread(
            client.put_object,
            bucket,
            uri,
            BytesIO(raw),
            length=len(raw),
            content_type="application/json",
        )
        logger.info("[staging] saved payload: %s (%d bytes)", uri, len(raw))
    except Exception as exc:
        logger.error("[staging] failed to save payload: %s error=%s", uri, exc)
        raise
    return uri


async def load_staging_payload(uri: str) -> StagingPayload:
    """Download and deserialize StagingPayload from MinIO."""
    from backend.storage import _get_minio_client
    client = _get_minio_client()
    bucket = settings.minio_bucket_name

    def _download() -> bytes:
        response = client.get_object(bucket, uri)
        try:
            return response.read()
        finally:
            response.close()
            response.release_conn()

    raw = await asyncio.to_thread(_download)
    return await asyncio.to_thread(deserialize_payload, raw)


async def delete_staging_payload(uri: str) -> bool:
    """Remove staging payload from MinIO after successful commit."""
    from backend.storage import _get_minio_client
    client = _get_minio_client()
    bucket = settings.minio_bucket_name
    try:
        await asyncio.to_thread(client.remove_object, bucket, uri)
        logger.info("[staging] deleted payload: %s", uri)
        return True
    except Exception as exc:
        logger.debug("[staging] failed to delete payload: %s error=%s", uri, exc)
        return False
