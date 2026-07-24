"""Confidence and schema quality gate for extracted graph facts."""

from __future__ import annotations

from copy import deepcopy
from dataclasses import asdict, is_dataclass
from typing import Any

from backend.rag.ingestion import states
from backend.rag.ingestion.extraction.simple_extractor import ExtractionResult
from backend.rag.ingestion.schema.schema_loader import SchemaDefinition
from backend.rag.ingestion.schema.schema_validator import validate_extraction


def _fact_payload(item: Any) -> dict[str, Any]:
    if is_dataclass(item):
        payload = asdict(item)
    else:
        payload = dict(getattr(item, "__dict__", {}) or {})
    for key in ("doc_ids", "chunk_ids"):
        if isinstance(payload.get(key), set):
            payload[key] = sorted(payload[key])
    if isinstance(payload.get("evidence"), dict):
        payload["evidence"] = {
            str(k): str(v)[:500]
            for k, v in payload["evidence"].items()
        }
    return payload


def _has_chunk_evidence(item: Any) -> tuple[bool, str | None]:
    chunk_ids = {str(value) for value in getattr(item, "chunk_ids", set()) or [] if str(value)}
    if not chunk_ids:
        return False, "missing_chunk_ids"
    evidence = getattr(item, "evidence", None) or {}
    if not any(str(key) in chunk_ids and str(value).strip() for key, value in evidence.items()):
        return False, "missing_evidence"
    return True, None


def apply_quality_gate(
    extraction: ExtractionResult,
    schema: SchemaDefinition,
) -> tuple[ExtractionResult, dict]:
    """Apply automatic quality decisions and return accepted facts plus audit.

    No human review is required in the ingest flow. Facts below the candidate
    threshold are dropped with audit logs. Mid-confidence entities are stored as
    audit-only candidates and are not visible to retrieval; mid-confidence
    relations are stored as weak evidence and remain visible with lower weight.
    """
    result = deepcopy(extraction)
    validation = validate_extraction(result, schema)
    entity_types = schema.entity_type_names()
    relation_types = schema.relation_type_names()
    dropped_entities: set[str] = set()
    dropped_relations: set[str] = set()
    candidate_facts: list[dict[str, Any]] = []
    dropped_facts: list[dict[str, Any]] = []

    for entity_id, entity in list(result.entities.items()):
        confidence = int(getattr(entity, "confidence", 0) or 0)
        unknown_type = bool(entity_types and entity.entity_type not in entity_types)
        has_evidence, evidence_reason = _has_chunk_evidence(entity)
        if unknown_type or not has_evidence or confidence < schema.candidate_confidence:
            dropped_entities.add(entity_id)
            if unknown_type:
                reason = "unknown_entity_type"
            elif not has_evidence:
                reason = evidence_reason or "missing_evidence"
            else:
                reason = "below_candidate_confidence"
            dropped_facts.append(
                {
                    "item_kind": "entity",
                    "item_id": entity_id,
                    "item_type": entity.entity_type,
                    "name": entity.name,
                    "confidence": confidence,
                    "reason": reason,
                    "auto_action": "auto_dropped_not_indexed",
                    "requires_manual_review": False,
                    "payload": _fact_payload(entity),
                }
            )
            result.entities.pop(entity_id, None)
        elif confidence < schema.min_entity_confidence:
            entity.status = states.ENTITY_CANDIDATE
            candidate_facts.append(
                {
                    "item_kind": "entity",
                    "item_id": entity_id,
                    "item_type": entity.entity_type,
                    "name": entity.name,
                    "confidence": confidence,
                    "reason": "below_entity_confidence",
                    "auto_action": "auto_audit_only_not_visible",
                    "requires_manual_review": False,
                    "payload": _fact_payload(entity),
                }
            )
        else:
            entity.status = states.ENTITY_ACTIVE

    for relation_id, relation in list(result.relations.items()):
        confidence = int(getattr(relation, "confidence", 0) or 0)
        has_evidence, evidence_reason = _has_chunk_evidence(relation)
        missing_endpoint = (
            relation.source_entity_id not in result.entities
            or relation.target_entity_id not in result.entities
        )
        non_visible_endpoint = False
        if not missing_endpoint:
            source = result.entities[relation.source_entity_id]
            target = result.entities[relation.target_entity_id]
            non_visible_endpoint = (
                getattr(source, "status", states.ENTITY_ACTIVE) not in states.RETRIEVAL_ENTITY_STATUSES
                or getattr(target, "status", states.ENTITY_ACTIVE) not in states.RETRIEVAL_ENTITY_STATUSES
            )
        forbidden = relation.relation_type in schema.forbidden_relations
        spec = schema.relation_types.get(relation.relation_type)
        unknown_type = bool(relation_types and relation.relation_type not in relation_types)
        min_conf = spec.min_confidence if spec else schema.min_relation_confidence
        if (
            missing_endpoint
            or non_visible_endpoint
            or forbidden
            or unknown_type
            or not has_evidence
            or confidence < schema.candidate_confidence
        ):
            dropped_relations.add(relation_id)
            if missing_endpoint:
                reason = "missing_endpoint"
            elif non_visible_endpoint:
                reason = "non_visible_endpoint"
            elif forbidden:
                reason = "forbidden_relation"
            elif unknown_type:
                reason = "unknown_relation_type"
            elif not has_evidence:
                reason = evidence_reason or "missing_evidence"
            else:
                reason = "below_candidate_confidence"
            dropped_facts.append(
                {
                    "item_kind": "relation",
                    "item_id": relation_id,
                    "item_type": relation.relation_type,
                    "source_entity_id": relation.source_entity_id,
                    "target_entity_id": relation.target_entity_id,
                    "confidence": confidence,
                    "reason": reason,
                    "auto_action": "auto_dropped_not_indexed",
                    "requires_manual_review": False,
                    "payload": _fact_payload(relation),
                }
            )
            result.relations.pop(relation_id, None)
        elif confidence < min_conf:
            relation.status = states.RELATION_WEAK_EVIDENCE
            candidate_facts.append(
                {
                    "item_kind": "relation",
                    "item_id": relation_id,
                    "item_type": relation.relation_type,
                    "source_entity_id": relation.source_entity_id,
                    "target_entity_id": relation.target_entity_id,
                    "confidence": confidence,
                    "reason": "below_relation_confidence",
                    "auto_action": "auto_weak_evidence_visible",
                    "requires_manual_review": False,
                    "payload": _fact_payload(relation),
                }
            )
        else:
            relation.status = states.RELATION_ACTIVE

    audit = {
        "schema_version": schema.schema_version,
        "validation_errors": validation.errors,
        "validation_warnings": validation.warnings,
        "dropped_entity_count": len(dropped_entities),
        "dropped_relation_count": len(dropped_relations),
        "candidate_entity_count": sum(1 for item in result.entities.values() if item.status == states.ENTITY_CANDIDATE),
        "weak_relation_count": sum(1 for item in result.relations.values() if item.status == states.RELATION_WEAK_EVIDENCE),
        "candidate_facts": candidate_facts,
        "dropped_facts": dropped_facts,
        "manual_review_required": False,
        "auto_quality_policy": {
            "entity_below_candidate": "auto_dropped_not_indexed",
            "entity_below_min_confidence": "auto_audit_only_not_visible",
            "entity_unknown_type": "auto_dropped_not_indexed",
            "relation_below_candidate_or_forbidden": "auto_dropped_not_indexed",
            "relation_unknown_type": "auto_dropped_not_indexed",
            "fact_missing_chunk_or_evidence": "auto_dropped_not_indexed",
            "relation_below_min_confidence": "auto_weak_evidence_visible",
        },
    }
    return result, audit
