"""Conservative entity normalization for self-ingestion extraction results."""

from __future__ import annotations

import re
import unicodedata
from dataclasses import replace

from backend.rag.ingestion.extraction.simple_extractor import (
    ExtractedEntity,
    ExtractedRelation,
    ExtractionResult,
)
from backend.rag.ingestion.ids import stable_entity_id, stable_relation_id


_SPACE_RE = re.compile(r"\s+")
_UNIT_SLASH_RE = re.compile(r"\s*/\s*")
_ASSIGN_RE = re.compile(r"\s*([=:：])\s*")


def canonical_name(name: str) -> str:
    value = unicodedata.normalize("NFKC", name or "")
    value = value.strip(" \t\r\n,.;:()[]{}<>，。；：（）【】")
    value = _SPACE_RE.sub(" ", value)
    value = _UNIT_SLASH_RE.sub("/", value)
    value = _ASSIGN_RE.sub(r"\1", value)
    return value.strip().lower()


def normalize_extraction(
    extraction: ExtractionResult,
    *,
    kb_id: str,
    kb_code: str = "",
) -> tuple[ExtractionResult, dict]:
    """Merge entities with the same `(entity_type, canonical_name)`.

    This is intentionally narrow: it avoids cross-type merging and does not use
    external synonym dictionaries, so it cannot pull legacy KB data into self
    indexes or change old retrieval behavior.
    """
    canonical_to_entity: dict[tuple[str, str], ExtractedEntity] = {}
    old_to_new: dict[str, str] = {}
    merged_count = 0

    for entity in extraction.entities.values():
        normalized = canonical_name(entity.normalized_name or entity.name)
        if not normalized:
            continue
        entity_id = stable_entity_id(kb_code or kb_id, entity.entity_type, normalized)
        key = (entity.entity_type, normalized)
        existing = canonical_to_entity.get(key)
        old_to_new[entity.entity_id] = entity_id
        if existing is None:
            canonical_to_entity[key] = replace(
                entity,
                entity_id=entity_id,
                normalized_name=normalized,
            )
            continue

        merged_count += 1
        existing.doc_ids.update(entity.doc_ids)
        existing.chunk_ids.update(entity.chunk_ids)
        existing.evidence.update(entity.evidence)
        existing.confidence = max(existing.confidence, entity.confidence)
        if len(entity.name) < len(existing.name):
            existing.name = entity.name

    relations: dict[str, ExtractedRelation] = {}
    dropped_relation_count = 0
    for relation in extraction.relations.values():
        src = old_to_new.get(relation.source_entity_id)
        dst = old_to_new.get(relation.target_entity_id)
        if not src or not dst or src == dst:
            dropped_relation_count += 1
            continue
        if relation.relation_type == "co_occurs":
            src, dst = sorted([src, dst])
        relation_id = stable_relation_id(src, relation.relation_type, dst)
        existing = relations.get(relation_id)
        if existing is None:
            relations[relation_id] = replace(
                relation,
                relation_id=relation_id,
                source_entity_id=src,
                target_entity_id=dst,
            )
            continue
        existing.chunk_ids.update(relation.chunk_ids)
        existing.evidence.update(relation.evidence)
        existing.confidence = max(existing.confidence, relation.confidence)

    audit = {
        "normalized_entity_count": len(canonical_to_entity),
        "merged_entity_count": merged_count,
        "dropped_self_relation_count": dropped_relation_count,
    }
    return ExtractionResult(entities={e.entity_id: e for e in canonical_to_entity.values()}, relations=relations), audit
