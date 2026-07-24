"""Post-process linker: connect orphan entities via chunk co-occurrence and typed weak edges."""

from __future__ import annotations

from collections import defaultdict

from backend.rag.ingestion import states
from backend.rag.ingestion.extraction.simple_extractor import (
    ExtractedEntity,
    ExtractedRelation,
    ExtractionResult,
)
from backend.rag.ingestion.ids import stable_relation_id
from backend.rag.ingestion.schema.schema_loader import SchemaDefinition


def _linked_entity_ids(extraction: ExtractionResult) -> set[str]:
    linked: set[str] = set()
    for relation in extraction.relations.values():
        linked.add(relation.source_entity_id)
        linked.add(relation.target_entity_id)
    return linked


def _entity_degree(extraction: ExtractionResult) -> dict[str, int]:
    degree: dict[str, int] = defaultdict(int)
    for relation in extraction.relations.values():
        degree[relation.source_entity_id] += 1
        degree[relation.target_entity_id] += 1
    return degree


def _chunk_entity_index(entities: dict[str, ExtractedEntity]) -> dict[str, list[str]]:
    index: dict[str, list[str]] = defaultdict(list)
    for entity_id, entity in entities.items():
        for chunk_id in entity.chunk_ids:
            index[chunk_id].append(entity_id)
    return index


def _relation_allowed(
    schema: SchemaDefinition,
    relation_type: str,
    source: ExtractedEntity,
    target: ExtractedEntity,
) -> bool:
    if relation_type in schema.forbidden_relations:
        return False
    spec = schema.relation_types.get(relation_type)
    if not spec:
        return relation_type == "co_occurs"
    if spec.allowed_source_types and source.entity_type not in spec.allowed_source_types:
        return False
    if spec.allowed_target_types and target.entity_type not in spec.allowed_target_types:
        return False
    return True


def _infer_typed_relation(
    orphan: ExtractedEntity,
    anchor: ExtractedEntity,
    schema: SchemaDefinition,
    *,
    typed_first: bool,
) -> tuple[str, str, str, int] | None:
    if not typed_first:
        return None

    o_type, a_type = orphan.entity_type, anchor.entity_type

    if o_type == "parameter" and a_type in {"circuit", "detector", "method", "model_or_code"}:
        source, target = anchor, orphan
        if _relation_allowed(schema, "has_parameter", source, target):
            return source.entity_id, target.entity_id, "has_parameter", 58

    if o_type == "material" and a_type in {"circuit", "detector", "method"}:
        source, target = anchor, orphan
        if _relation_allowed(schema, "uses_material", source, target):
            return source.entity_id, target.entity_id, "uses_material", 62

    if o_type in {"method", "detector", "term", "term_cn"} and a_type in {
        "circuit", "detector", "method", "model_or_code",
    }:
        source, target = anchor, orphan
        if _relation_allowed(schema, "measures", source, target):
            return source.entity_id, target.entity_id, "measures", 55

    return None


def _rank_anchors(
    orphan_id: str,
    orphan: ExtractedEntity,
    chunk_index: dict[str, list[str]],
    entities: dict[str, ExtractedEntity],
    degree: dict[str, int],
) -> list[str]:
    scores: dict[str, int] = {}
    for chunk_id in orphan.chunk_ids:
        for other_id in chunk_index.get(chunk_id, []):
            if other_id == orphan_id:
                continue
            other = entities.get(other_id)
            if other is None:
                continue
            score = degree.get(other_id, 0) * 10 + len(other.chunk_ids)
            scores[other_id] = max(scores.get(other_id, 0), score)
    return sorted(scores, key=lambda item: scores[item], reverse=True)


def _shared_chunk_ids(a: ExtractedEntity, b: ExtractedEntity) -> set[str]:
    return set(a.chunk_ids) & set(b.chunk_ids)


def link_orphan_entities(
    extraction: ExtractionResult,
    schema: SchemaDefinition,
    *,
    max_links_per_orphan: int = 3,
    typed_first: bool = True,
) -> tuple[ExtractionResult, dict]:
    """Add weak relations so orphan entities connect to chunk co-mentioned anchors."""
    linked = _linked_entity_ids(extraction)
    orphan_ids = [
        entity_id
        for entity_id in extraction.entities
        if entity_id not in linked
    ]
    if not orphan_ids:
        return extraction, {
            "orphan_before": 0,
            "orphan_after": 0,
            "links_added": 0,
            "typed_links": 0,
            "co_occurs_links": 0,
        }

    degree = _entity_degree(extraction)
    chunk_index = _chunk_entity_index(extraction.entities)
    links_added = 0
    typed_links = 0
    co_occurs_links = 0

    for orphan_id in orphan_ids:
        orphan = extraction.entities.get(orphan_id)
        if orphan is None:
            continue
        if orphan_id in _linked_entity_ids(extraction):
            continue

        added_for_orphan = 0
        for anchor_id in _rank_anchors(
            orphan_id, orphan, chunk_index, extraction.entities, degree,
        ):
            if added_for_orphan >= max(1, max_links_per_orphan):
                break
            anchor = extraction.entities.get(anchor_id)
            if anchor is None:
                continue

            typed = _infer_typed_relation(orphan, anchor, schema, typed_first=typed_first)
            if typed:
                source_id, target_id, relation_type, confidence = typed
                status = states.RELATION_WEAK_EVIDENCE
            else:
                left, right = sorted([orphan_id, anchor_id])
                source_id, target_id = left, right
                relation_type = "co_occurs"
                confidence = 52
                status = states.RELATION_ACTIVE
                source = extraction.entities[source_id]
                target = extraction.entities[target_id]
                if not _relation_allowed(schema, relation_type, source, target):
                    continue

            relation_id = stable_relation_id(source_id, relation_type, target_id)
            if relation_id in extraction.relations:
                degree[orphan_id] = degree.get(orphan_id, 0) + 1
                degree[anchor_id] = degree.get(anchor_id, 0) + 1
                linked.add(orphan_id)
                continue

            shared_chunks = _shared_chunk_ids(orphan, anchor)
            evidence = {
                chunk_id: orphan.evidence.get(chunk_id, anchor.evidence.get(chunk_id, ""))[:500]
                for chunk_id in shared_chunks
            }
            extraction.relations[relation_id] = ExtractedRelation(
                relation_id=relation_id,
                source_entity_id=source_id,
                target_entity_id=target_id,
                relation_type=relation_type,
                confidence=confidence,
                status=status,
                chunk_ids=set(shared_chunks),
                evidence=evidence,
            )
            degree[orphan_id] = degree.get(orphan_id, 0) + 1
            degree[anchor_id] = degree.get(anchor_id, 0) + 1
            linked.add(orphan_id)
            links_added += 1
            added_for_orphan += 1
            if typed:
                typed_links += 1
            else:
                co_occurs_links += 1

    orphan_after = sum(
        1 for entity_id in extraction.entities if entity_id not in _linked_entity_ids(extraction)
    )
    return extraction, {
        "orphan_before": len(orphan_ids),
        "orphan_after": orphan_after,
        "links_added": links_added,
        "typed_links": typed_links,
        "co_occurs_links": co_occurs_links,
    }
