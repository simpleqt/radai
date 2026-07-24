"""Validation helpers for schema-aware extraction output."""

from __future__ import annotations

from dataclasses import dataclass, field

from backend.rag.ingestion.extraction.simple_extractor import ExtractionResult
from backend.rag.ingestion.schema.schema_loader import SchemaDefinition


@dataclass(slots=True)
class SchemaValidationResult:
    errors: list[str] = field(default_factory=list)
    warnings: list[str] = field(default_factory=list)

    @property
    def ok(self) -> bool:
        return not self.errors


def validate_extraction(
    extraction: ExtractionResult,
    schema: SchemaDefinition,
) -> SchemaValidationResult:
    errors: list[str] = []
    warnings: list[str] = []
    entity_types = schema.entity_type_names()
    relation_types = schema.relation_type_names()

    for entity in extraction.entities.values():
        if entity_types and entity.entity_type not in entity_types:
            warnings.append(f"unknown_entity_type:{entity.entity_id}:{entity.entity_type}")

    for relation in extraction.relations.values():
        if relation.relation_type in schema.forbidden_relations:
            errors.append(f"forbidden_relation:{relation.relation_id}:{relation.relation_type}")
            continue
        if relation_types and relation.relation_type not in relation_types:
            warnings.append(f"unknown_relation_type:{relation.relation_id}:{relation.relation_type}")
            continue
        spec = schema.relation_types.get(relation.relation_type)
        if not spec:
            continue
        src = extraction.entities.get(relation.source_entity_id)
        dst = extraction.entities.get(relation.target_entity_id)
        if not src or not dst:
            errors.append(f"relation_missing_entity:{relation.relation_id}")
            continue
        if spec.allowed_source_types and src.entity_type not in spec.allowed_source_types:
            errors.append(f"relation_bad_source_type:{relation.relation_id}:{src.entity_type}")
        if spec.allowed_target_types and dst.entity_type not in spec.allowed_target_types:
            errors.append(f"relation_bad_target_type:{relation.relation_id}:{dst.entity_type}")

    return SchemaValidationResult(errors=errors, warnings=warnings)
