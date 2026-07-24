"""Conservative entity/relation extraction for self-ingestion phase 2."""

from __future__ import annotations

import re
from collections import Counter
from dataclasses import dataclass, field
from typing import Iterable

from backend.rag.ingestion.ids import (
    stable_entity_id,
    stable_relation_id,
)
from backend.rag.ingestion.models import IngestChunk


_CJK_RE = re.compile(r"[\u4e00-\u9fff]{2,12}")
_LATIN_RE = re.compile(r"[A-Za-z][A-Za-z0-9_\-/.]{1,48}")
_PARAM_RE = re.compile(r"[A-Za-z\u4e00-\u9fff]{1,24}\s*[=:：]\s*[-+]?\d+(?:\.\d+)?\s*[A-Za-z%℃°/]*")

_STOPWORDS = {
    "the",
    "and",
    "for",
    "with",
    "from",
    "this",
    "that",
    "have",
    "were",
    "are",
    "was",
    "本文",
    "研究",
    "结果",
    "方法",
    "进行",
    "可以",
    "通过",
    "使用",
    "其中",
    "以及",
    "相关",
    "如下",
}


@dataclass(slots=True)
class ExtractedEntity:
    entity_id: str
    name: str
    normalized_name: str
    entity_type: str
    confidence: int = 70
    status: str = "active"
    attributes: dict[str, str] = field(default_factory=dict)
    doc_ids: set[str] = field(default_factory=set)
    chunk_ids: set[str] = field(default_factory=set)
    evidence: dict[str, str] = field(default_factory=dict)


@dataclass(slots=True)
class ExtractedRelation:
    relation_id: str
    source_entity_id: str
    target_entity_id: str
    relation_type: str = "co_occurs"
    confidence: int = 60
    status: str = "weak_evidence"
    attributes: dict[str, str] = field(default_factory=dict)
    chunk_ids: set[str] = field(default_factory=set)
    evidence: dict[str, str] = field(default_factory=dict)


@dataclass(slots=True)
class ExtractionResult:
    entities: dict[str, ExtractedEntity]
    relations: dict[str, ExtractedRelation]


def _make_entity_id(kb_code: str, entity_type: str, normalized_name: str) -> str:
    """按方案 5.4 生成稳定实体 ID：ent-{sha1(kb_code:entity_type:canonical_name)[:32]}."""
    return stable_entity_id(kb_code, entity_type, normalized_name)


def _make_relation_id(source_entity_id: str, relation_type: str, target_entity_id: str) -> str:
    """按方案 5.4 生成稳定关系 ID：rel-{sha1(src:relation_type:dst)[:32]}.

    对 co_occurs 这类无向关系，调用方传入排序后的 (left, right) 以保证幂等。
    """
    return stable_relation_id(source_entity_id, relation_type, target_entity_id)


def _normalize_name(name: str) -> str:
    return " ".join((name or "").strip().split()).lower()


def _entity_type(name: str) -> str:
    if _PARAM_RE.fullmatch(name.strip()):
        return "parameter"
    if re.search(r"\d", name) and re.search(r"[A-Za-z]", name):
        return "model_or_code"
    if re.search(r"[\u4e00-\u9fff]", name):
        return "term_cn"
    return "term"


def _candidate_terms(text: str) -> Iterable[str]:
    seen: set[str] = set()
    for pattern in (_PARAM_RE, _LATIN_RE, _CJK_RE):
        for match in pattern.findall(text or ""):
            term = " ".join(str(match).strip(" ,.;:()[]{}<>，。；：（）【】").split())
            normalized = _normalize_name(term)
            if not normalized or normalized in _STOPWORDS:
                continue
            if len(normalized) < 2 or len(normalized) > 80:
                continue
            if normalized in seen:
                continue
            seen.add(normalized)
            yield term


def extract_chunks(kb_id: str, chunks: list[IngestChunk], kb_code: str = "") -> ExtractionResult:
    entities: dict[str, ExtractedEntity] = {}
    relations: dict[str, ExtractedRelation] = {}

    for chunk in chunks:
        terms = list(_candidate_terms(chunk.content))
        if not terms:
            continue
        counts = Counter(_normalize_name(term) for term in terms)
        ranked = sorted(terms, key=lambda term: (-counts[_normalize_name(term)], len(term)))[:12]
        chunk_entity_ids: list[str] = []
        for term in ranked:
            normalized = _normalize_name(term)
            entity_type = _entity_type(term)
            entity_id = _make_entity_id(kb_code or kb_id, entity_type, normalized)
            entity = entities.get(entity_id)
            if entity is None:
                entity = ExtractedEntity(
                    entity_id=entity_id,
                    name=term.strip(),
                    normalized_name=normalized,
                    entity_type=entity_type,
                )
                entities[entity_id] = entity
            entity.doc_ids.add(chunk.doc_id)
            entity.chunk_ids.add(chunk.chunk_id)
            entity.evidence.setdefault(chunk.chunk_id, chunk.content[:500])
            chunk_entity_ids.append(entity_id)

        for index, src in enumerate(chunk_entity_ids[:8]):
            for dst in chunk_entity_ids[index + 1 : 8]:
                if src == dst:
                    continue
                # co_occurs 无向：排序后生成稳定 relation_id，避免 A-B / B-A 重复
                left, right = sorted([src, dst])
                relation_id = _make_relation_id(left, "co_occurs", right)
                relation = relations.get(relation_id)
                if relation is None:
                    relation = ExtractedRelation(
                        relation_id=relation_id,
                        source_entity_id=left,
                        target_entity_id=right,
                    )
                    relations[relation_id] = relation
                relation.chunk_ids.add(chunk.chunk_id)
                relation.evidence.setdefault(chunk.chunk_id, chunk.content[:500])

    return ExtractionResult(entities=entities, relations=relations)
