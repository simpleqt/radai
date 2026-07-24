"""Map LLM/VLM JSON payloads to ExtractionResult."""

from __future__ import annotations

from typing import Any

from backend.rag.ingestion.extraction.simple_extractor import (
    ExtractedEntity,
    ExtractedRelation,
    ExtractionResult,
)
from backend.rag.ingestion.ids import stable_entity_id, stable_relation_id
from backend.rag.ingestion.models import IngestChunk
from backend.rag.ingestion.normalization.entity_normalizer import canonical_name
from backend.rag.ingestion.schema.schema_loader import (
    SchemaDefinition,
    canonical_entity_type,
    canonical_relation_type,
)


def _name_keys(value: Any) -> set[str]:
    raw = str(value or "").strip()
    if not raw:
        return set()
    compact = " ".join(raw.split()).lower()
    canonical = canonical_name(raw)
    return {key for key in (compact, canonical) if key}


def _iter_aliases(value: Any) -> list[Any]:
    if value is None:
        return []
    if isinstance(value, (list, tuple, set)):
        return list(value)
    return [value]


def payload_to_extraction(
    kb_id: str,
    kb_code: str,
    chunks: list[IngestChunk],
    payload: dict[str, Any],
    schema: SchemaDefinition | None = None,
) -> ExtractionResult:
    chunk_by_id = {chunk.chunk_id: chunk for chunk in chunks}
    entities: dict[str, ExtractedEntity] = {}
    name_to_id: dict[str, str] = {}

    for item in payload.get("entities") or []:
        if not isinstance(item, dict):
            continue
        name = str(item.get("name") or "").strip()
        entity_type = canonical_entity_type(schema, item.get("entity_type"))
        if not name or not entity_type:
            continue
        normalized = canonical_name(name)
        if not normalized:
            continue
        entity_id = stable_entity_id(kb_code or kb_id, entity_type, normalized)
        entity = entities.setdefault(
            entity_id,
            ExtractedEntity(
                entity_id=entity_id,
                name=name,
                normalized_name=normalized,
                entity_type=entity_type,
                confidence=max(0, min(100, int(item.get("confidence") or 60))),
            ),
        )
        entity.confidence = max(entity.confidence, max(0, min(100, int(item.get("confidence") or 60))))
        for chunk_id in item.get("chunk_ids") or []:
            chunk = chunk_by_id.get(str(chunk_id))
            if chunk:
                entity.doc_ids.add(chunk.doc_id)
                entity.chunk_ids.add(chunk.chunk_id)
                entity.evidence.setdefault(chunk.chunk_id, chunk.content[:500])
        for key in _name_keys(name):
            name_to_id[key] = entity_id
        for alias in _iter_aliases(item.get("aliases") or item.get("alias")):
            for key in _name_keys(alias):
                name_to_id[key] = entity_id

    relations: dict[str, ExtractedRelation] = {}
    for item in payload.get("relations") or []:
        if not isinstance(item, dict):
            continue
        relation_type = canonical_relation_type(schema, item.get("relation_type"))
        source_id = str(item.get("source_entity_id") or "").strip()
        target_id = str(item.get("target_entity_id") or "").strip()
        if source_id not in entities:
            source_id = ""
        if target_id not in entities:
            target_id = ""
        if not source_id:
            for key in _name_keys(item.get("source")):
                source_id = name_to_id.get(key, "")
                if source_id:
                    break
        if not target_id:
            for key in _name_keys(item.get("target")):
                target_id = name_to_id.get(key, "")
                if target_id:
                    break
        if not source_id or not target_id or not relation_type:
            continue
        relation_id = stable_relation_id(source_id, relation_type, target_id)
        relation = relations.setdefault(
            relation_id,
            ExtractedRelation(
                relation_id=relation_id,
                source_entity_id=source_id,
                target_entity_id=target_id,
                relation_type=relation_type,
                confidence=max(0, min(100, int(item.get("confidence") or 55))),
            ),
        )
        relation.confidence = max(
            relation.confidence,
            max(0, min(100, int(item.get("confidence") or 55))),
        )
        for chunk_id in item.get("chunk_ids") or []:
            chunk = chunk_by_id.get(str(chunk_id))
            if chunk:
                relation.chunk_ids.add(chunk.chunk_id)
                relation.evidence.setdefault(chunk.chunk_id, chunk.content[:500])

    return ExtractionResult(entities=entities, relations=relations)
