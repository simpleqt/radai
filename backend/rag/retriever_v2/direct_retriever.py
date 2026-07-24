"""Self-ingestion retriever backed by self chunks plus Milvus/Nebula graph_v2."""

from __future__ import annotations

import asyncio
import html
import json
import math
import os
import re
import threading
from dataclasses import dataclass
from typing import Any

from sqlalchemy import and_, desc, or_, select
from sqlalchemy.ext.asyncio import AsyncSession

from backend.config.loggers import get_logger
from backend.config.settings import settings
from backend.db.models import (
    KnowledgeBase,
    KnowledgeBaseDocument,
    SelfIngestChunk,
)
from backend.db.redis import get_redis
from backend.rag.content_filter.models import RawRetrievalData
from backend.rag.ingestion import states
from backend.rag.ingestion.embedding import embed_media_texts, embed_texts
from backend.rag.ingestion.storage.milvus_writer import (
    chunks_collection_name,
    entities_collection_name,
    media_collection_name,
)
from backend.rag.media_refs import normalize_media_kind, normalize_media_number
from backend.rag.retriever_v2.filters import RetrievalFilter


logger = get_logger(__name__)

_MILVUS_ALIAS = "self_retriever_v2"
_MILVUS_CONNECTION_LOCK = threading.Lock()


def _backend_timeout_seconds() -> float:
    raw = os.getenv("SELF_RETRIEVAL_BACKEND_TIMEOUT_SECONDS", "30")
    try:
        return max(float(raw), 1.0)
    except (TypeError, ValueError):
        return 30.0


def _ensure_milvus_connection() -> str:
    """Return one process-wide Milvus channel instead of reconnecting per query."""

    from pymilvus import connections

    if connections.has_connection(_MILVUS_ALIAS):
        return _MILVUS_ALIAS
    with _MILVUS_CONNECTION_LOCK:
        if connections.has_connection(_MILVUS_ALIAS):
            return _MILVUS_ALIAS
        kwargs: dict[str, Any] = {
            "alias": _MILVUS_ALIAS,
            "uri": settings.milvus_connection_uri,
            "timeout": _backend_timeout_seconds(),
        }
        if settings.milvus_connection_db_name:
            kwargs["db_name"] = settings.milvus_connection_db_name
        if settings.milvus_user:
            kwargs["user"] = settings.milvus_user
        if settings.milvus_password:
            kwargs["password"] = settings.milvus_password
        connections.connect(**kwargs)
    return _MILVUS_ALIAS


_LATIN_RE = re.compile(r"[A-Za-z0-9_\-]{2,}")
_CJK_RE = re.compile(r"[\u4e00-\u9fff]+")
_NUMBER_RE = re.compile(
    r"\d+(?:\.\d+)?(?:\s*(?:kev|mev|gev|ev|ns|us|μs|ms|s|mm|cm|m/d|mg/l|%|℃|°c))?",
    re.IGNORECASE,
)
_FIGURE_REF_RE = re.compile(
    r"(?:图|fig(?:ure)?\.?\s*)"
    r"(\d+(?:\s*[.\-－—–~～￣_]\s*\d+)*)",
    re.IGNORECASE,
)
_MEDIA_REF_RE = re.compile(
    r"(?P<kind>图|表|公式|Fig(?:ure)?|Table|Formula|Eq(?:uation)?)"
    r"\.?\s*(?P<num>\d+(?:\s*[.\-－—–~～￣_]\s*\d+)*)",
    re.IGNORECASE,
)
_MEDIA_BLOCK_TYPES = ("image", "table", "formula")
_MAX_QUERY_TOKENS = 128
_MAX_TOKEN_LEN = 48
_GRAPH_PROFILES = {"legacy", "seed_only", "routed", "filtered", "optimized", "pathrag"}
_HTML_TABLE_RE = re.compile(r"<table\b[^>]*>.*?</table>", re.IGNORECASE | re.DOTALL)
_HTML_ROW_RE = re.compile(r"<tr\b[^>]*>(.*?)</tr>", re.IGNORECASE | re.DOTALL)
_HTML_CELL_RE = re.compile(r"<t[dh]\b[^>]*>(.*?)</t[dh]>", re.IGNORECASE | re.DOTALL)
_HTML_TAG_RE = re.compile(r"<[^>]+>")
_DISPLAY_FORMULA_RE = re.compile(r"(?:公式:\s*)?\$\$.*?\$\$", re.DOTALL)
_ENUM_HEADING_RE = re.compile(
    r"(?<![\d.])(?P<number>\d{1,2})[）)]\s*"
    r"(?P<title>[\u4e00-\u9fffA-Za-z][\u4e00-\u9fffA-Za-z0-9、和与/\-（）() ]{0,48})"
)


def _page_numbers_from_extra(extra: Any) -> tuple[int, ...]:
    """Return normalized one-based source pages from persisted chunk metadata."""

    if not isinstance(extra, dict):
        return ()
    raw_pages = extra.get("page_nums")
    candidates = list(raw_pages) if isinstance(raw_pages, (list, tuple, set)) else []
    candidates.append(extra.get("page_num"))
    pages: set[int] = set()
    for value in candidates:
        if isinstance(value, bool):
            continue
        try:
            page = int(value)
        except (TypeError, ValueError):
            continue
        if page > 0:
            pages.add(page)
    return tuple(sorted(pages))


def _tokens(text: str) -> list[str]:
    value = text or ""
    protected: list[str] = []
    cjk_candidates: list[str] = []

    def add(target: list[str], token: str) -> None:
        token = token.strip().lower()[:_MAX_TOKEN_LEN]
        if len(token) >= 2 and token not in target:
            target.append(token)

    for match in _LATIN_RE.findall(value):
        add(protected, match)
    for match in _NUMBER_RE.findall(value):
        add(protected, " ".join(match.split()))
    for span in _CJK_RE.findall(value):
        if len(span) <= 6:
            add(cjk_candidates, span)
        for offset in range(max(len(span) - 1, 0)):
            add(cjk_candidates, span[offset : offset + 2])

    tokens = list(dict.fromkeys([*protected, *cjk_candidates]))
    if len(tokens) <= _MAX_QUERY_TOKENS:
        return tokens
    remaining = max(_MAX_QUERY_TOKENS - len(protected), 0)
    if remaining <= 0:
        return protected[:_MAX_QUERY_TOKENS]
    if remaining == 1:
        sampled = [cjk_candidates[len(cjk_candidates) // 2]]
    else:
        sampled = [
            cjk_candidates[round(index * (len(cjk_candidates) - 1) / (remaining - 1))]
            for index in range(remaining)
        ]
    return list(dict.fromkeys([*protected, *sampled]))[:_MAX_QUERY_TOKENS]


def _normalize_space(text: str) -> str:
    return re.sub(r"\s+", " ", str(text or "")).strip()


def _html_cell_text(value: str) -> str:
    return _normalize_space(html.unescape(_HTML_TAG_RE.sub(" ", value or "")))


def _focus_table_evidence(content: str, query: str, *, max_rows: int) -> str:
    """Return table headers plus query-matching rows without changing retrieval scores.

    This is deliberately query-agnostic: matching uses the same lexical tokens as
    the retriever and never checks benchmark-specific words.  If no data row has
    independent lexical support, the original table is returned unchanged.
    """

    if max_rows <= 0 or not query or "<table" not in (content or "").lower():
        return content
    table_match = _HTML_TABLE_RE.search(content or "")
    if table_match is None:
        return content
    parsed_rows: list[tuple[str, list[str]]] = []
    for row_html in _HTML_ROW_RE.findall(table_match.group(0)):
        cells = [_html_cell_text(cell) for cell in _HTML_CELL_RE.findall(row_html)]
        cells = [cell for cell in cells if cell]
        if cells:
            parsed_rows.append((row_html, cells))
    if len(parsed_rows) <= max_rows:
        return content

    terms = [term for term in _tokens(query) if len(term) >= 2]
    if not terms:
        return content
    header_count = min(2, len(parsed_rows))
    ranked: list[tuple[float, int]] = []
    for index, (_row_html, cells) in enumerate(parsed_rows[header_count:], header_count):
        row_text = _normalize_space(" ".join(cells)).lower()
        compact = re.sub(r"\s+", "", row_text)
        score = 0.0
        for term in terms:
            normalized = _normalize_space(term).lower()
            if not normalized:
                continue
            if normalized in row_text or re.sub(r"\s+", "", normalized) in compact:
                numeric_bonus = 1.0 if any(char.isdigit() for char in normalized) else 0.0
                score += 1.0 + min(len(normalized), 8) / 8.0 + numeric_bonus
        if score > 0:
            ranked.append((score, index))
    if not ranked:
        return content

    data_limit = max(max_rows - header_count, 1)
    selected_data = {
        index
        for _score, index in sorted(ranked, key=lambda item: (-item[0], item[1]))[:data_limit]
    }
    selected = [*range(header_count), *sorted(selected_data)]
    if len(selected) >= len(parsed_rows):
        return content

    rendered = [
        f"[表格聚焦证据：保留 {len(selected)}/{len(parsed_rows)} 行；未修改原始数值]"
    ]
    for index in selected:
        label = "表头" if index < header_count else "匹配行"
        rendered.append(f"{label}: " + " | ".join(parsed_rows[index][1]))
    prefix = (content or "")[: table_match.start()].strip()
    suffix = (content or "")[table_match.end() :].strip()
    return "\n".join(part for part in (prefix, *rendered, suffix) if part)


def _compact_text_with_offsets(value: str) -> tuple[str, list[int]]:
    """Remove OCR whitespace while retaining a map back to source offsets."""

    chars: list[str] = []
    offsets: list[int] = []
    for index, char in enumerate(value or ""):
        if char.isspace():
            continue
        chars.append(char.lower())
        offsets.append(index)
    return "".join(chars), offsets


def _query_match_positions(content: str, query: str) -> list[tuple[int, str, float]]:
    """Locate generic lexical/numeric query evidence in OCR-heavy content."""

    if not content or not query:
        return []
    compact, offsets = _compact_text_with_offsets(content)
    if not compact or not offsets:
        return []
    matches: list[tuple[int, str, float]] = []
    seen_terms: set[str] = set()
    for raw_term in _tokens(query):
        term = re.sub(r"\s+", "", raw_term.lower())
        if len(term) < 2 or term in seen_terms:
            continue
        seen_terms.add(term)
        weight = 1.0 + min(len(term), 8) / 8.0
        if any(char.isdigit() for char in term):
            weight += 1.0
        start = 0
        occurrences = 0
        while occurrences < 8:
            position = compact.find(term, start)
            if position < 0:
                break
            matches.append((offsets[position], term, weight))
            occurrences += 1
            start = position + max(len(term), 1)
    return matches


def _best_query_windows(
    content: str,
    query: str,
    *,
    window_chars: int,
    limit: int,
) -> list[tuple[int, int]]:
    """Choose bounded, non-duplicative spans with broad query-clause coverage."""

    if window_chars <= 0 or limit <= 0:
        return []
    matches = _query_match_positions(content, query)
    if not matches:
        return []
    maximum_start = max(len(content) - window_chars, 0)
    candidate_starts = {
        min(max(position - int(window_chars * 0.35), 0), maximum_start)
        for position, _term, _weight in matches
    }
    ranked: list[tuple[float, int, int]] = []
    for start in candidate_starts:
        end = min(start + window_chars, len(content))
        term_weights: dict[str, float] = {}
        occurrence_bonus = 0.0
        for position, term, weight in matches:
            if start <= position < end:
                term_weights[term] = max(term_weights.get(term, 0.0), weight)
                occurrence_bonus += min(weight, 2.0) * 0.04
        score = sum(term_weights.values()) + occurrence_bonus
        # Later spans win only exact score ties.  This helps retain conclusions
        # that follow repeated problem statements without hard-coding words.
        ranked.append((score, start, end))
    ranked.sort(key=lambda item: (-item[0], -item[1]))

    selected: list[tuple[int, int]] = []
    for _score, start, end in ranked:
        if any(
            max(0, min(end, chosen_end) - max(start, chosen_start))
            > window_chars * 0.35
            for chosen_start, chosen_end in selected
        ):
            continue
        selected.append((start, end))
        if len(selected) >= limit:
            break
    return sorted(selected)


def _structured_query_score(content: str, query: str) -> float:
    """Score a neighbor by unique lexical support, including spaced OCR numbers."""

    matches = _query_match_positions(content, query)
    if not matches:
        return 0.0
    unique: dict[str, float] = {}
    for _position, term, weight in matches:
        unique[term] = max(unique.get(term, 0.0), weight)
    return float(sum(unique.values()))


def _enumerated_heading_summary(content: str, *, max_items: int = 10) -> str:
    """Extract one monotonic run of OCR-visible numbered section headings."""

    items: list[tuple[int, str]] = []
    seen: set[tuple[int, str]] = set()
    last_number: int | None = None
    for match in _ENUM_HEADING_RE.finditer(content or ""):
        number = int(match.group("number"))
        title = _normalize_space(match.group("title")).split(" ", 1)[0].strip("、，,；;：:")
        if len(title) < 2:
            continue
        key = (number, title)
        if key in seen:
            continue
        if last_number is not None and number <= last_number and len(items) >= 2:
            break
        seen.add(key)
        items.append(key)
        last_number = number
        if len(items) >= max_items:
            break
    if len(items) < 2:
        return ""
    return "[结构条目索引（原文编号）] " + "；".join(
        f"{number}）{title}" for number, title in items
    )


def _best_novel_formula_window(
    content: str,
    query: str,
    *,
    window_chars: int,
    excluded: list[tuple[int, int]],
) -> tuple[int, int] | None:
    """Select one compact equality span not already covered by query windows."""

    if window_chars <= 0:
        return None
    ranked: list[tuple[float, int, int]] = []
    for match in _DISPLAY_FORMULA_RE.finditer(content or ""):
        formula_start, formula_end = match.span()
        formula_length = max(formula_end - formula_start, 1)
        overlap = max(
            (
                max(0, min(formula_end, end) - max(formula_start, start))
                for start, end in excluded
            ),
            default=0,
        )
        if overlap > min(formula_length, window_chars) * 0.35:
            continue
        formula = match.group(0)
        equality_count = formula.count("=")
        if equality_count <= 0 or not any(char.isdigit() for char in formula):
            continue
        compactness = min(window_chars / formula_length, 2.5)
        score = (
            min(equality_count, 4) * 0.75
            + compactness
            + min(_structured_query_score(formula, query), 8.0) * 0.12
        )
        if formula_length <= window_chars:
            padding = window_chars - formula_length
            start = max(formula_start - padding // 3, 0)
            end = min(start + window_chars, len(content))
            start = max(end - window_chars, 0)
        else:
            # Equality chains conventionally place the evaluated scalar at the
            # end, so retain the terminal part of an oversized display formula.
            end = formula_end
            start = max(end - window_chars, formula_start)
        ranked.append((score, start, end))
    if not ranked:
        return None
    _score, start, end = min(ranked, key=lambda item: (-item[0], -item[1]))
    return start, end


def _bounded_evidence_excerpt(
    content: str,
    *,
    max_chars: int,
    block_type: str,
    query: str = "",
) -> str:
    """Bound one evidence unit while retaining query-relevant structural spans."""

    value = (content or "").strip()
    if len(value) <= max_chars:
        return value
    max_chars = max(int(max_chars), 80)
    if block_type in _MEDIA_BLOCK_TYPES:
        marker = "\n…[结构证据省略]…\n"
        # Long OCR/formula chunks frequently contain the requested calculation
        # in the middle.  Preserve the beginning/end plus up to two generic
        # query-supported spans instead of assuming the answer is at an edge.
        formula_mode = block_type == "formula"
        marker_count = 4 if formula_mode else 3
        usable = max(max_chars - len(marker) * marker_count, 80)
        head_chars = max(int(usable * (0.12 if formula_mode else 0.15)), 20)
        tail_chars = max(int(usable * (0.06 if formula_mode else 0.08)), 20)
        formula_chars = max(int(usable * 0.24), 20) if formula_mode else 0
        focus_budget = max(usable - head_chars - tail_chars - formula_chars, 40)
        focus_chars = max(focus_budget // 2, 20)
        focus_intervals = _best_query_windows(
            value,
            query,
            window_chars=focus_chars,
            limit=2,
        )
        if not focus_intervals:
            head_chars = max(int((max_chars - len(marker)) * 0.56), 20)
            tail_chars = max(max_chars - len(marker) - head_chars, 20)
            return value[:head_chars].rstrip() + marker + value[-tail_chars:].lstrip()
        formula_interval = (
            _best_novel_formula_window(
                value,
                query,
                window_chars=formula_chars,
                excluded=focus_intervals,
            )
            if formula_mode
            else None
        )
        intervals = [
            (0, min(head_chars, len(value))),
            *focus_intervals,
            *([formula_interval] if formula_interval is not None else []),
            (max(len(value) - tail_chars, 0), len(value)),
        ]
        merged: list[list[int]] = []
        for start, end in sorted(intervals):
            if start >= end:
                continue
            if merged and start <= merged[-1][1]:
                merged[-1][1] = max(merged[-1][1], end)
            else:
                merged.append([start, end])
        return marker.join(value[start:end].strip() for start, end in merged)
    return value[:max_chars].rstrip()


def _is_image_query(query: str) -> bool:
    value = _normalize_space(query or "").lower()
    compact = re.sub(r"\s+", "", value)
    if any(
        marker in value
        for marker in (
            "图片",
            "图像",
            "图示",
            "示意",
            "示意图",
            "插图",
            "图表",
            "流程图",
            "关系图",
            "分类图",
            "组成图",
            "结构图",
            "模式图",
            "剖面图",
            "曲线图",
            "分布图",
            "图解",
            "配图",
            "figure",
            "fig.",
        )
    ):
        return True
    if any(
        marker in compact
        for marker in (
            "的图",
            "相关图",
            "对应图",
            "配套图",
            "原图",
            "截图",
        )
    ):
        return True
    if compact.endswith("图") and len(compact) >= 4:
        return True
    return bool(re.search(r"(?:查找|寻找|搜索|找|展示|查看|显示|有没有|是否有).{0,80}图|图\s*\d", value))


def _is_media_query(query: str) -> bool:
    value = (query or "").lower()
    if _is_image_query(query):
        return True
    if any(marker in value for marker in ("表格", "数据表", "公式", "图题", "图号", "figure", "table", "formula", "equation")):
        return True
    return bool(_MEDIA_REF_RE.search(query or ""))


def _figure_refs(query: str) -> set[str]:
    refs: set[str] = set()
    for match in _FIGURE_REF_RE.findall(query or ""):
        compact = re.sub(r"\s+", "", match)
        normalized = re.sub(r"[.\-－—–~～￣_]+", "-", compact)
        dotted = normalized.replace("-", ".")
        variants = {
            compact,
            normalized,
            dotted,
            compact.replace("-", "￣"),
            compact.replace("-", "－"),
            compact.replace("-", "—"),
        }
        for ref in variants:
            if not ref:
                continue
            refs.add(ref)
            refs.add(f"图{ref}")
            refs.add(f"图 {ref}")
    return refs


def _media_refs(query: str) -> set[str]:
    refs: set[str] = set()
    for match in _MEDIA_REF_RE.finditer(query or ""):
        kind = normalize_media_kind(match.group("kind"))
        number = normalize_media_number(match.group("num"))
        if not kind or not number:
            continue
        dotted = number.replace("-", ".")
        for value in {number, dotted}:
            refs.add(f"{kind}{value}")
            refs.add(f"{kind} {value}")
            if kind == "图":
                refs.add(f"Fig. {value}")
                refs.add(f"Figure {value}")
            elif kind == "表":
                refs.add(f"Table {value}")
            elif kind == "公式":
                refs.add(f"Eq. {value}")
                refs.add(f"Equation {value}")
    return refs


def _media_ref_matches(extra: dict[str, Any], refs: set[str]) -> bool:
    if not refs:
        return False
    fields = [
        str(extra.get("media_ref") or ""),
        *[str(item) for item in (extra.get("media_ref_aliases") or []) if item],
    ]
    normalized_fields = {_normalize_space(value).lower() for value in fields if value}
    compact_fields = {re.sub(r"\s+", "", value) for value in normalized_fields}
    for ref in refs:
        ref_norm = _normalize_space(ref).lower()
        ref_compact = re.sub(r"\s+", "", ref_norm)
        if ref_norm in normalized_fields or ref_compact in compact_fields:
            return True
    return False


@dataclass(frozen=True, slots=True)
class PathRAGEdge:
    """Typed graph edge with the source chunks needed for grounded retrieval."""

    source_id: str
    target_id: str
    relation_id: str
    relation_type: str
    description: str
    evidence_chunk_ids: tuple[str, ...]
    weight: float = 1.0

    def other(self, entity_id: str) -> str:
        if entity_id == self.source_id:
            return self.target_id
        if entity_id == self.target_id:
            return self.source_id
        return ""


@dataclass(frozen=True, slots=True)
class PathRAGPath:
    """A pruned seed-to-seed path and its structural support scores."""

    entity_ids: tuple[str, ...]
    edges: tuple[PathRAGEdge, ...]
    score: float
    flow_score: float
    evidence_completeness: float


@dataclass(slots=True)
class RetrievedChunk:
    chunk_id: str
    doc_id: str
    content: str
    content_hash: str
    file_name: str
    file_path: str
    chunk_index: int
    score: float
    block_type: str = "text"
    vec_score: float = 0.0
    entity_score: float = 0.0
    keyword_score: float = 0.0
    graph_score: float = 0.0
    rerank_score: float = 0.0
    context_chunk_ids: tuple[str, ...] = ()
    recall_paths: tuple[str, ...] = ("self_mysql",)
    source_backend: str = "self_ingestion"
    source_index: str = "kb_self_chunks"
    page_num: int | None = None
    page_nums: tuple[int, ...] = ()

    def to_dict(self) -> dict[str, Any]:
        return {
            "id": self.chunk_id,
            "chunk_id": self.chunk_id,
            "doc_id": self.doc_id,
            "content": self.content,
            "content_hash": self.content_hash,
            "file_name": self.file_name,
            "file_path": self.file_path,
            "chunk_index": self.chunk_index,
            "block_type": self.block_type,
            "page_num": self.page_num,
            "page_nums": list(self.page_nums),
            "_chunk_score": self.score,
            "_vec_score": self.vec_score,
            "_entity_score": self.entity_score,
            "_keyword_score": self.keyword_score,
            "_graph_score": self.graph_score,
            "_rerank_score": self.rerank_score,
            "context_chunk_ids": list(self.context_chunk_ids or (self.chunk_id,)),
            "_coverage_score": self.score,
            "_recall_path": "+".join(self.recall_paths),
            "_recall_paths": list(self.recall_paths),
            "source_type": "self_ingestion",
            "source_backend": self.source_backend,
            "source_index": self.source_index,
        }


@dataclass(slots=True)
class SelfRetrievalResult:
    chunks: list[RetrievedChunk]
    entities: list[dict[str, Any]]
    relationships: list[dict[str, Any]]
    query_embedding: list[float] | None = None

    def to_raw_data(self) -> RawRetrievalData:
        return RawRetrievalData(
            entities=list(self.entities),
            relationships=list(self.relationships),
            chunks=[chunk.to_dict() for chunk in self.chunks],
            query_embedding=self.query_embedding,
        )


class SelfDirectRetrieverV2:
    def __init__(
        self,
        session: AsyncSession,
        kb_id: str,
        kb_code: str | None = None,
        retrieval_filter: RetrievalFilter | None = None,
        graph_profile: str = "legacy",
        enable_rerank: bool | None = None,
        enable_parent_expansion: bool | None = None,
    ):
        self._session = session
        self._kb_id = kb_id
        self._kb_code = kb_code
        self._nebula_space: str | None = None
        normalized_profile = str(graph_profile or "legacy").strip().lower()
        if normalized_profile not in _GRAPH_PROFILES:
            raise ValueError(f"unsupported graph retrieval profile: {graph_profile}")
        self._graph_profile = normalized_profile
        self._pathrag_paths_cache: tuple[
            tuple[str, ...], tuple[PathRAGPath, ...]
        ] | None = None
        self._rerank_enabled = (
            normalized_profile in {"optimized", "pathrag"}
            and bool(settings.self_rerank_enabled)
            if enable_rerank is None
            else bool(enable_rerank)
        )
        self._parent_expansion_enabled = (
            bool(settings.self_parent_expansion_enabled)
            if enable_parent_expansion is None
            else bool(enable_parent_expansion)
        )
        self._filter = (retrieval_filter or RetrievalFilter(kb_id=kb_id, kb_code=kb_code or "")).with_defaults(
            kb_id=kb_id,
            kb_code=kb_code or "",
        )

    async def retrieve_chunks(
        self,
        query: str,
        top_k: int = 12,
        retrieval_filter: RetrievalFilter | None = None,
    ) -> list[RetrievedChunk]:
        result = await self.retrieve(query, top_k=top_k, retrieval_filter=retrieval_filter)
        return result.chunks

    async def retrieve(
        self,
        query: str,
        top_k: int = 12,
        retrieval_filter: RetrievalFilter | None = None,
    ) -> SelfRetrievalResult:
        if retrieval_filter is not None:
            self._filter = retrieval_filter.with_defaults(kb_id=self._kb_id, kb_code=self._kb_code or "")
        query_tokens = _tokens(query)
        top_k = max(int(top_k or 12), 1)
        recall_top_k = self._recall_top_k(top_k)
        media_intent = _is_media_query(query)
        image_intent = _is_image_query(query)
        media_refs = _media_refs(query)
        # Entity recall is always a graph-seeding path. Graph candidates are
        # admitted by cross-retriever evidence below, never by query wording.
        graph_requested = True
        entity_requested = True
        # Retrieve enough entity-linked chunks for corroborated reranking, but
        # keep the graph traversal seed frontier independently bounded below.
        entity_top_k = top_k

        query_embedding = await self._query_embedding(query)
        await self._get_kb_code()

        if self._graph_profile == "pathrag" and entity_requested:
            entity_task = asyncio.create_task(
                self._pathrag_entity_recall(query, entity_top_k, query_embedding)
            )
        else:
            entity_task = asyncio.create_task(
                self._call_milvus_entity_recall(query, entity_top_k, query_embedding)
                if entity_requested
                else asyncio.sleep(0, result=({}, []))
            )
        # Redis has a compact query tokenizer of its own.  Pass the original
        # question so its fixed budget covers all clauses instead of a capped
        # intermediate ngram list.
        redis_task = asyncio.create_task(self._redis_chunk_scores([query], recall_top_k))
        vector_task = asyncio.create_task(
            self._call_milvus_chunk_scores(query, recall_top_k, query_embedding)
        )
        media_vector_task = asyncio.create_task(
            self._call_milvus_media_scores(query, recall_top_k)
            if media_intent
            else asyncio.sleep(0, result={})
        )

        redis_scores = await redis_task
        # MySQL content LIKE scans are expensive on large self-ingestion chunks.
        # Prefer Redis BM25 and only fall back to MySQL when the fast index is empty.
        keyword_scores = {}
        if not redis_scores:
            keyword_scores = await self._keyword_scores(query_tokens, recall_top_k)
        (
            (entity_scores, seed_entity_ids),
            vector_scores,
            media_vector_scores,
        ) = await asyncio.gather(
            entity_task,
            vector_task,
            media_vector_task,
        )
        media_scores = {}
        # Semantic image search is handled by media vectors + Redis BM25.
        # Keep MySQL media LIKE only as an exact figure/table-number fallback.
        # A missing/empty media collection must not turn every natural-language
        # image query into an unindexed scan over all media chunks.
        if media_intent and media_refs:
            media_scores = await self._media_scores(
                query,
                query_tokens,
                recall_top_k,
                media_refs=media_refs,
            )
        if not graph_requested:
            graph_seed_ids = []
        elif self._graph_profile == "legacy":
            graph_seed_ids = seed_entity_ids
        elif self._graph_profile == "pathrag":
            graph_seed_ids = seed_entity_ids[
                : max(int(settings.self_pathrag_seed_limit or 1), 1)
            ]
        else:
            graph_seed_ids = seed_entity_ids[: min(top_k, 5)]
        graph_scores = await self._graph_chunk_scores(graph_seed_ids, recall_top_k)
        keyword_component = self._merge_score_maps(keyword_scores, redis_scores)
        media_component = self._merge_score_maps(media_scores, media_vector_scores)
        if (
            self._graph_profile in {"optimized", "pathrag"}
            and not image_intent
            and not media_refs
        ):
            media_component = self._corroborated_scores(
                media_component,
                vector_scores,
                keyword_component,
            )
        admitted_entity_scores: dict[str, float] = {}
        if self._graph_profile in {"routed", "filtered", "optimized", "pathrag"}:
            admitted_entity_scores = self._corroborated_scores(
                entity_scores,
                vector_scores,
                keyword_component,
                media_component,
            )
            graph_scores = self._admit_graph_scores(
                graph_scores,
                vector=vector_scores,
                keyword=keyword_component,
                media=media_component,
                top_k=recall_top_k,
            )
        graph_relationships = await self._fetch_graph_relationships(
            graph_seed_ids,
            recall_top_k,
        )
        graph_entity_ids = self._entity_ids_from_relationships(graph_relationships)
        entities = await self._fetch_entities_by_ids(
            self._dedupe(seed_entity_ids + list(graph_entity_ids)),
            entity_scores=entity_scores,
        )
        relationships = graph_relationships

        component_scores = self._merge_component_scores(
            entity=(
                entity_scores
                if self._graph_profile == "legacy"
                else admitted_entity_scores
            ),
            keyword=keyword_component,
            vector=vector_scores,
            graph=graph_scores,
            media=media_component,
            profile=self._graph_profile,
            image_intent=image_intent,
        )
        chunk_ids = list(component_scores.keys())
        if not chunk_ids:
            return SelfRetrievalResult(
                chunks=await self._fallback_chunks(query_tokens, top_k),
                entities=entities,
                relationships=relationships,
                query_embedding=query_embedding,
            )

        rows = await self._fetch_visible_chunks(chunk_ids)
        figure_refs = _figure_refs(query) | media_refs
        scored = [
            self._row_to_retrieved_chunk(
                row,
                component_scores.get(row.chunk_id, {}),
                query=query,
                query_tokens=query_tokens,
                image_intent=image_intent,
                figure_refs=figure_refs,
            )
            for row in rows
        ]
        scored.sort(key=lambda item: (-item.score, item.file_name, item.chunk_index))
        scored = self._dedupe_scored_chunks(scored, image_intent=image_intent)
        if not image_intent:
            # OCR/caption-rich images can score strongly for ordinary text
            # questions.  Keep a small amount of useful visual evidence, while
            # preventing it from displacing the textual evidence needed for a
            # grounded answer. Explicit image queries remain unrestricted.
            scored = self._limit_non_intent_images(
                scored,
                max_images=max(1, min(4, top_k // 2 or 1)),
            )
        if self._rerank_enabled and not image_intent:
            scored = await self._rerank_scored_chunks(
                query,
                scored,
                graph_requested=graph_requested,
            )
        if self._graph_profile in {"optimized", "pathrag"} and not image_intent:
            scored = self._ensure_vector_floor(scored, vector_scores, top_k=top_k)
        if (
            self._graph_profile in {"optimized", "pathrag"}
            and self._parent_expansion_enabled
            and not image_intent
        ):
            scored = await self._expand_parent_context_windows(
                scored,
                top_k=top_k,
                query=query,
            )
            scored = self._promote_stronger_parent_evidence(
                scored,
                query=query,
                top_k=top_k,
            )
        return SelfRetrievalResult(
            chunks=scored[:top_k],
            entities=entities,
            relationships=relationships,
            query_embedding=query_embedding,
        )

    async def _expand_parent_context_windows(
        self,
        chunks: list[RetrievedChunk],
        *,
        top_k: int,
        query: str = "",
    ) -> list[RetrievedChunk]:
        """Attach bounded adjacent evidence to high-confidence parent anchors.

        The first-stage order and anchor IDs remain unchanged.  A document is
        considered high-confidence when it has repeated frontier hits, or when
        a top-ranked single anchor is structural/multi-path and close to the
        best score.  Expanded candidates remain local to the selected document;
        query-aware table focusing and bounded head+tail excerpts keep complete
        result lines without injecting every neighboring block in full.
        """

        frontier = chunks[: max(int(top_k), 1)]
        if not frontier:
            return chunks

        by_doc: dict[str, list[tuple[int, RetrievedChunk]]] = {}
        for rank, chunk in enumerate(frontier, 1):
            by_doc.setdefault(chunk.doc_id, []).append((rank, chunk))
        best_score = max((float(chunk.score) for chunk in frontier), default=0.0)
        confident: list[tuple[int, str, list[tuple[int, RetrievedChunk]]]] = []
        for doc_id, entries in by_doc.items():
            if not doc_id:
                continue
            if len(entries) >= 2 or self._eligible_single_parent_anchor(
                entries[0][0],
                entries[0][1],
                best_score=best_score,
            ):
                confident.append((entries[0][0], doc_id, entries))
        # Repeated hits from one paper are stronger parent evidence than a
        # single very high-ranked passage.  Break ties by the best rank.
        confident.sort(key=lambda item: (-len(item[2]), item[0], item[1]))
        doc_limit = max(int(settings.self_parent_expansion_doc_limit or 0), 0)
        if doc_limit <= 0 or not confident:
            return chunks

        radius = max(int(settings.self_parent_expansion_radius or 0), 0)
        media_radius = max(int(settings.self_parent_expansion_media_radius or radius), radius)
        anchor_limit = max(int(settings.self_parent_expansion_anchor_limit or 0), 1)
        anchors: list[tuple[RetrievedChunk, int]] = []
        for _best_rank, _doc_id, entries in confident[:doc_limit]:
            structural_entries = [
                entry for entry in entries
                if SelfDirectRetrieverV2._is_structural_media_chunk(entry[1])
            ]
            # Preserve the first-stage relevance order.  The previous
            # farthest-anchor rule could skip a rank-2 calculation continuation
            # in favour of a distant, unrelated formula from the same book.
            selected_entries = (
                structural_entries[:anchor_limit]
                if structural_entries
                else entries[:anchor_limit]
            )
            for _rank, anchor in selected_entries:
                anchor_radius = (
                    media_radius
                    if SelfDirectRetrieverV2._is_structural_media_chunk(anchor)
                    else radius
                )
                if anchor_radius > 0:
                    anchors.append((anchor, anchor_radius))
        if not anchors:
            return chunks

        try:
            rows_by_anchor = await self._fetch_parent_neighbor_windows(anchors)
        except Exception as exc:
            logger.warning(
                "[SelfDirectRetrieverV2] parent context expansion skipped: %s",
                exc,
            )
            return chunks
        max_chars = max(int(settings.self_parent_expansion_max_chars or 0), 1000)
        for anchor, _anchor_radius in anchors:
            rows = rows_by_anchor.get(anchor.chunk_id, [])
            rows = self._select_parent_window_rows(
                rows,
                anchor_chunk_id=anchor.chunk_id,
                query=query,
                max_rows=max(
                    int(settings.self_parent_expansion_render_max_rows or 0),
                    0,
                ),
            )
            if len(rows) <= 1:
                continue
            context_ids = tuple(row.chunk_id for row in rows if row.chunk_id)
            if len(context_ids) <= 1:
                continue
            anchor.content = self._render_parent_window(
                rows,
                anchor_chunk_id=anchor.chunk_id,
                max_chars=max_chars,
                query=query,
            )
            anchor.context_chunk_ids = context_ids
            expanded_pages = sorted({
                page
                for row in rows
                for page in _page_numbers_from_extra(
                    getattr(row, "extra_json", None)
                )
            })
            if expanded_pages:
                anchor.page_nums = tuple(expanded_pages)
                anchor.page_num = expanded_pages[0] if len(expanded_pages) == 1 else None
            anchor.recall_paths = tuple(
                sorted({*anchor.recall_paths, "path_f_parent_window"})
            )
        return chunks

    @staticmethod
    def _eligible_single_parent_anchor(
        rank: int,
        chunk: RetrievedChunk,
        *,
        best_score: float,
    ) -> bool:
        if not bool(settings.self_parent_expansion_single_anchor_enabled):
            return False
        max_rank = max(int(settings.self_parent_expansion_single_anchor_max_rank or 0), 0)
        if max_rank <= 0 or rank > max_rank:
            return False
        ratio = max(
            0.0,
            min(float(settings.self_parent_expansion_single_anchor_score_ratio or 0.0), 1.0),
        )
        if best_score > 0 and float(chunk.score) < best_score * ratio:
            return False
        min_paths = max(int(settings.self_parent_expansion_single_anchor_min_paths or 0), 1)
        independent_paths = {
            path for path in chunk.recall_paths if path != "path_f_parent_window"
        }
        return (
            SelfDirectRetrieverV2._is_structural_media_chunk(chunk)
            or len(independent_paths) >= min_paths
        )

    async def _fetch_parent_neighbor_windows(
        self,
        anchors: list[tuple[RetrievedChunk, int]],
    ) -> dict[str, list[SelfIngestChunk]]:
        if not anchors:
            return {}
        ranges = [
            and_(
                SelfIngestChunk.doc_id == anchor.doc_id,
                SelfIngestChunk.chunk_index.between(
                    max(anchor.chunk_index - radius, 0),
                    anchor.chunk_index + radius,
                ),
            )
            for anchor, radius in anchors
        ]
        stmt = select(SelfIngestChunk).where(*self._chunk_conditions(), or_(*ranges))
        stmt = self._apply_doc_time_filters(stmt)
        rows = list((await self._session.execute(stmt)).scalars().all())
        output: dict[str, list[SelfIngestChunk]] = {}
        for anchor, radius in anchors:
            output[anchor.chunk_id] = sorted(
                (
                    row
                    for row in rows
                    if row.doc_id == anchor.doc_id
                    and abs(row.chunk_index - anchor.chunk_index) <= radius
                ),
                key=lambda row: row.chunk_index,
            )
        return output

    @staticmethod
    def _is_structural_media_chunk(chunk: RetrievedChunk) -> bool:
        if str(chunk.block_type or "").lower() in _MEDIA_BLOCK_TYPES:
            return True
        content = chunk.content or ""
        return any(
            marker in content
            for marker in ("Image Path:", "媒体类型: image", "媒体类型: table", "媒体类型: formula")
        )

    @staticmethod
    def _select_parent_window_rows(
        rows: list[SelfIngestChunk],
        *,
        anchor_chunk_id: str,
        query: str,
        max_rows: int,
    ) -> list[SelfIngestChunk]:
        """Retain the anchor and the neighbors with the strongest query support."""

        if not query or max_rows <= 0 or len(rows) <= max_rows:
            return rows
        anchor = next((row for row in rows if row.chunk_id == anchor_chunk_id), None)
        anchor_index = int(getattr(anchor, "chunk_index", 0) or 0)
        ranked: list[tuple[float, int, SelfIngestChunk]] = []
        for row in rows:
            if row.chunk_id == anchor_chunk_id:
                continue
            relevance = _structured_query_score(row.content or "", query)
            distance = abs(int(getattr(row, "chunk_index", 0) or 0) - anchor_index)
            structural_bonus = (
                0.25
                if str(getattr(row, "block_type", "text") or "text").lower()
                in _MEDIA_BLOCK_TYPES
                else 0.0
            )
            proximity_bonus = 0.15 / max(distance, 1)
            ranked.append((relevance + structural_bonus + proximity_bonus, distance, row))
        keep: list[SelfIngestChunk] = [anchor] if anchor is not None else []
        # Preserve the immediate continuation before relevance pruning.  OCR
        # ingestion commonly separates a worked example's final substitution
        # or table continuation into the chunk directly after its formula/
        # media anchor.  That row may share few query terms because it contains
        # the derived value rather than the inputs, so pure lexical ranking can
        # otherwise discard the answer-bearing continuation.
        if anchor is not None and len(keep) < max_rows:
            successors = sorted(
                (
                    row
                    for row in rows
                    if int(getattr(row, "chunk_index", 0) or 0) > anchor_index
                ),
                key=lambda row: int(getattr(row, "chunk_index", 0) or 0),
            )
            if successors:
                keep.append(successors[0])
        kept_ids = {row.chunk_id for row in keep}
        remaining = max(max_rows - len(keep), 0)
        keep.extend(
            item[2]
            for item in sorted(
                (item for item in ranked if item[2].chunk_id not in kept_ids),
                key=lambda item: (
                    -item[0],
                    item[1],
                    int(getattr(item[2], "chunk_index", 0) or 0),
                ),
            )[:remaining]
        )
        return sorted(keep, key=lambda row: int(getattr(row, "chunk_index", 0) or 0))

    @staticmethod
    def _promote_stronger_parent_evidence(
        chunks: list[RetrievedChunk],
        *,
        query: str,
        top_k: int,
    ) -> list[RetrievedChunk]:
        """Promote at most one dense parent window when its support is decisive.

        This is intentionally narrower than a global reranker: only evidence
        that already passed the parent-confidence gate is eligible, and its
        length-normalized lexical support must exceed the current first item by
        a configured margin.  Candidate membership and scores are unchanged.
        """

        if (
            not bool(settings.self_parent_expansion_promotion_enabled)
            or not query
            or len(chunks) < 2
        ):
            return chunks
        frontier_size = min(
            len(chunks),
            max(int(top_k), 1),
            max(int(settings.self_parent_expansion_promotion_max_rank or 0), 0),
        )
        if frontier_size < 2:
            return chunks

        def evidence_score(chunk: RetrievedChunk) -> float:
            support = _structured_query_score(chunk.content or "", query)
            length_penalty = math.sqrt(max(len(chunk.content or "") / 800.0, 1.0))
            return support / length_penalty

        current_score = evidence_score(chunks[0])
        eligible = [
            (evidence_score(chunk), rank, chunk)
            for rank, chunk in enumerate(chunks[1:frontier_size], 1)
            if "path_f_parent_window" in chunk.recall_paths
        ]
        if not eligible:
            return chunks
        best_score, best_rank, _best_chunk = max(
            eligible,
            key=lambda item: (item[0], -item[1]),
        )
        minimum = max(
            float(settings.self_parent_expansion_promotion_min_score or 0.0),
            0.0,
        )
        margin = max(
            float(settings.self_parent_expansion_promotion_margin or 1.0),
            1.0,
        )
        if best_score < minimum or best_score < current_score * margin:
            return chunks
        promoted = list(chunks)
        promoted.insert(0, promoted.pop(best_rank))
        return promoted

    @staticmethod
    def _render_parent_window(
        rows: list[SelfIngestChunk],
        *,
        anchor_chunk_id: str,
        max_chars: int,
        query: str = "",
    ) -> str:
        """Render the anchor first, then its neighbors, within a fair budget.

        The anchor is the passage the retrieval rank actually selected.  Putting
        an earlier sibling before it can hide the anchor from downstream model
        and evaluator excerpts when the parent window is truncated.
        """

        if not rows:
            return ""
        anchor = next((row for row in rows if row.chunk_id == anchor_chunk_id), None)
        anchor_index = int(getattr(anchor, "chunk_index", 0) or 0)
        ordered_rows = sorted(
            rows,
            key=lambda row: (
                0
                if row.chunk_id == anchor_chunk_id
                else (
                    1
                    if int(getattr(row, "chunk_index", 0) or 0) == anchor_index + 1
                    else 2
                ),
                int(getattr(row, "chunk_index", 0) or 0),
            ),
        )
        header = "[父文档相邻证据窗口]"
        available = max(max_chars - len(header) - 2, 1)
        labels = [
            f"[{'锚点' if row.chunk_id == anchor_chunk_id else '相邻'} "
            f"chunk #{row.chunk_index}; {row.block_type}]\n"
            for row in ordered_rows
        ]
        separators = max(len(ordered_rows) - 1, 0) * 2
        content_budget = max(available - sum(len(label) for label in labels) - separators, 1)
        table_rows = max(int(settings.self_parent_expansion_table_max_rows or 0), 0)
        contents = [
            SelfDirectRetrieverV2._row_content_for_model_context(
                row,
                query=query,
                table_max_rows=table_rows,
            ).strip()
            for row in ordered_rows
        ]

        # Equal per-row budgets are lossy for structural anchors: a complete
        # table can be shorter than the whole parent-window allowance yet still
        # be cut in half merely because three short neighbours are present.
        # Give the selected anchor enough room to remain intact when it fits a
        # bounded majority of the window, while retaining a fair slice for the
        # immediate continuation and other neighbours. Oversized anchors remain
        # capped so a long formula/image OCR block cannot starve its successor.
        row_count = len(ordered_rows)
        fair_share = max(content_budget // row_count, 1)
        if row_count == 1:
            anchor_budget = content_budget
        else:
            reserved_per_neighbor = min(80, max(content_budget // row_count, 1))
            maximum_anchor_budget = max(
                content_budget - reserved_per_neighbor * (row_count - 1),
                1,
            )
            preferred_anchor_cap = max(
                fair_share,
                int(content_budget * 0.55),
            )
            anchor_budget = min(
                len(contents[0]),
                preferred_anchor_cap,
                maximum_anchor_budget,
            )
            if len(contents[0]) > anchor_budget:
                anchor_budget = min(
                    max(fair_share, preferred_anchor_cap),
                    maximum_anchor_budget,
                )

        budgets = [max(anchor_budget, 1)]
        remaining_budget = max(content_budget - budgets[0], 0)
        for index, content in enumerate(contents[1:], start=1):
            remaining_rows = row_count - index
            fair_remaining = max(
                remaining_budget // max(remaining_rows, 1),
                1,
            )
            budget = min(len(content), fair_remaining)
            budgets.append(max(budget, 1))
            remaining_budget = max(remaining_budget - budgets[-1], 0)

        parts = [header]
        for row, label, content, budget in zip(
            ordered_rows,
            labels,
            contents,
            budgets,
            strict=True,
        ):
            excerpt = _bounded_evidence_excerpt(
                content,
                max_chars=budget,
                block_type=str(row.block_type or "text").lower(),
                query=query,
            )
            parts.append(label + excerpt)
        rendered = "\n\n".join(parts)
        return rendered if len(rendered) <= max_chars else rendered[:max_chars]

    def _recall_top_k(self, top_k: int) -> int:
        if not self._rerank_enabled:
            return top_k
        multiplier = max(int(settings.self_rerank_candidate_multiplier or 1), 1)
        maximum = max(int(settings.self_rerank_max_candidates or top_k), top_k)
        return min(max(top_k * multiplier, top_k), maximum)

    async def _rerank_scored_chunks(
        self,
        query: str,
        chunks: list[RetrievedChunk],
        *,
        graph_requested: bool,
    ) -> list[RetrievedChunk]:
        """Fuse cross-encoder relevance with RRF and multi-path evidence."""

        if len(chunks) < 2:
            return chunks
        maximum = max(int(settings.self_rerank_max_candidates or len(chunks)), 1)
        candidates = chunks[:maximum]
        tail = chunks[maximum:]
        max_chars = max(int(settings.self_rerank_max_chars or 6000), 500)
        documents = [
            f"来源: {chunk.file_name}\n{chunk.content[:max_chars]}"
            for chunk in candidates
        ]
        results = await self._call_rerank_api(query, documents)
        rerank_raw = {
            int(item.get("index", -1)): float(item.get("relevance_score") or 0.0)
            for item in results
            if 0 <= int(item.get("index", -1)) < len(candidates)
        }
        if not rerank_raw:
            return chunks

        rerank_normalized = self._normalize_index_scores(rerank_raw)
        base_normalized = self._normalize_index_scores(
            {index: chunk.score for index, chunk in enumerate(candidates)}
        )
        rerank_weight = max(0.0, min(float(settings.self_rerank_weight or 0.72), 1.0))
        for index, chunk in enumerate(candidates):
            raw_score = rerank_raw.get(index, 0.0)
            chunk.rerank_score = raw_score
            evidence_bonus = min(max(len(chunk.recall_paths) - 1, 0) * 0.02, 0.06)
            if (
                graph_requested
                and "path_d_graph" in chunk.recall_paths
                and any(
                    path in chunk.recall_paths
                    for path in ("path_b_keyword", "path_c_chunk_vector")
                )
            ):
                evidence_bonus += 0.04
            chunk.score = (
                rerank_weight * rerank_normalized.get(index, 0.0)
                + (1.0 - rerank_weight) * base_normalized.get(index, 0.0)
                + evidence_bonus
            )
        candidates.sort(key=lambda item: (-item.score, item.file_name, item.chunk_index))
        return candidates + tail

    async def _call_rerank_api(
        self,
        query: str,
        documents: list[str],
    ) -> list[dict[str, Any]]:
        try:
            from backend.rag.content_filter.reranker import call_rerank_api

            return await call_rerank_api(query, documents, len(documents))
        except Exception as exc:
            logger.warning("[SelfDirectRetrieverV2] rerank skipped: %s", exc)
            return []

    @staticmethod
    def _normalize_index_scores(scores: dict[int, float]) -> dict[int, float]:
        if not scores:
            return {}
        values = [float(value) for value in scores.values()]
        low = min(values)
        high = max(values)
        if high - low <= 1e-9:
            return {index: 0.5 for index in scores}
        return {
            index: (float(value) - low) / (high - low)
            for index, value in scores.items()
        }

    async def _keyword_scores(self, query_tokens: list[str], top_k: int) -> dict[str, float]:
        if not query_tokens:
            return {}
        conditions = self._chunk_conditions()
        like_conditions = [
            SelfIngestChunk.content.ilike(f"%{token}%")
            for token in query_tokens[:8]
        ]
        stmt = (
            select(SelfIngestChunk.chunk_id, SelfIngestChunk.content)
            .where(and_(*conditions), or_(*like_conditions))
            .order_by(desc(SelfIngestChunk.visible_at), SelfIngestChunk.chunk_index)
            .limit(max(top_k * 30, 300))
        )
        stmt = self._apply_doc_time_filters(stmt)
        rows = (await self._session.execute(stmt)).all()
        scores = {
            row[0]: self._lexical_score(row[1] or "", query_tokens)
            for row in rows
        }

        return self._normalize_scores(scores)

    async def _media_scores(
        self,
        query: str,
        query_tokens: list[str],
        top_k: int,
        *,
        media_refs: set[str],
    ) -> dict[str, float]:
        conditions = [
            *self._chunk_conditions(),
            SelfIngestChunk.block_type.in_(_MEDIA_BLOCK_TYPES),
        ]
        probes = list(dict.fromkeys([*media_refs, *query_tokens[:10]]))
        if probes:
            like_conditions = [
                SelfIngestChunk.content.ilike(f"%{probe}%")
                for probe in probes
                if probe
            ]
            if like_conditions:
                conditions.append(or_(*like_conditions))
        stmt = (
            select(SelfIngestChunk)
            .where(and_(*conditions))
            .order_by(desc(SelfIngestChunk.visible_at), SelfIngestChunk.chunk_index)
            .limit(max(top_k * 100, 800))
        )
        stmt = self._apply_doc_time_filters(stmt)
        rows = list((await self._session.execute(stmt)).scalars().all())
        scores = {
            row.chunk_id: self._media_rerank_score(
                row,
                query=query,
                query_tokens=query_tokens,
                media_refs=media_refs,
            )
            for row in rows
        }
        return self._normalize_scores(scores)

    async def _call_milvus_entity_recall(
        self,
        query: str,
        top_k: int,
        query_embedding: list[float] | None,
    ) -> tuple[dict[str, float], list[str]]:
        try:
            return await self._milvus_entity_recall(query, top_k, query_embedding=query_embedding)
        except TypeError as exc:
            if "query_embedding" not in str(exc):
                raise
            return await self._milvus_entity_recall(query, top_k)

    @staticmethod
    def _pathrag_query_variants(query: str, limit: int) -> list[str]:
        """Create position-independent semantic seed queries.

        PathRAG combines semantic entity recall with query keywords.  The
        production retriever deliberately avoids an extra LLM call here: it
        embeds the complete question and a bounded set of punctuation-delimited
        clauses.  This is language-agnostic, deterministic, and contains no
        benchmark or domain keyword rules.
        """

        normalized = _normalize_space(query)
        if not normalized or limit <= 0:
            return []
        candidates: list[str] = [normalized]
        sentences = [
            _normalize_space(value)
            for value in re.split(r"[。！？!?；;\n]+", normalized)
            if len(_normalize_space(value)) >= 8
        ]
        clauses = [
            _normalize_space(value)
            for sentence in sentences
            for value in re.split(r"[，,：:]+", sentence)
            if len(_normalize_space(value)) >= 10
        ]
        # Prefer information-rich clauses, then restore source order so ties
        # are deterministic and do not privilege the beginning of a question.
        pool = list(dict.fromkeys([*sentences, *clauses]))
        positions = {value: normalized.find(value) for value in pool}
        pool.sort(key=lambda value: (-len(value), positions[value], value))
        for value in pool:
            if value not in candidates:
                candidates.append(value)
            if len(candidates) >= limit:
                break
        return candidates[:limit]

    async def _pathrag_entity_recall(
        self,
        query: str,
        top_k: int,
        query_embedding: list[float] | None,
    ) -> tuple[dict[str, float], list[str]]:
        """Recall a diverse seed frontier with clause-level dense retrieval."""

        if not (query or "").strip() or not settings.milvus_connection_uri:
            return {}, []
        kb_code = await self._get_kb_code()
        if not kb_code:
            return {}, []
        variant_limit = max(int(settings.self_pathrag_query_variants or 1), 1)
        variants = self._pathrag_query_variants(query, variant_limit)
        if not variants:
            return {}, []
        try:
            vectors: list[list[float]] = []
            remaining = variants
            if query_embedding is not None and variants[0] == _normalize_space(query):
                vectors.append(query_embedding)
                remaining = variants[1:]
            if remaining:
                vectors.extend(await embed_texts(remaining))
            if not vectors:
                return {}, []
            seed_limit = max(
                int(settings.self_pathrag_seed_limit or 1),
                int(top_k or 1),
            )
            rows_by_variant = await asyncio.to_thread(
                self._milvus_entity_multi_search_sync,
                kb_code,
                self._filter,
                vectors,
                max(seed_limit * 2, seed_limit),
            )
        except Exception as exc:
            logger.debug(
                "[SelfDirectRetrieverV2] PathRAG clause entity recall skipped: %s",
                exc,
            )
            return await self._call_milvus_entity_recall(query, top_k, query_embedding)

        fused: dict[str, float] = {}
        chunks_by_entity: dict[str, list[str]] = {}
        for rows in rows_by_variant:
            for rank, (entity_id, chunk_ids, _similarity) in enumerate(rows, 1):
                if not entity_id:
                    continue
                fused[entity_id] = fused.get(entity_id, 0.0) + 1.0 / (60.0 + rank)
                if entity_id not in chunks_by_entity:
                    chunks_by_entity[entity_id] = list(chunk_ids)
                else:
                    chunks_by_entity[entity_id] = self._dedupe(
                        [*chunks_by_entity[entity_id], *chunk_ids]
                    )
        seed_limit = max(int(settings.self_pathrag_seed_limit or 1), 1)
        # RRF alone tends to fill the seed set with near-synonyms of the main
        # topic because those entities rank in every clause.  Interleave the
        # per-clause rankings so secondary constraints and comparison targets
        # receive an equal opportunity to seed a path.
        seed_entity_ids: list[str] = []
        max_variant_rows = max((len(rows) for rows in rows_by_variant), default=0)
        for rank in range(max_variant_rows):
            for rows in rows_by_variant:
                if rank >= len(rows):
                    continue
                entity_id = rows[rank][0]
                if entity_id and entity_id not in seed_entity_ids:
                    seed_entity_ids.append(entity_id)
                    if len(seed_entity_ids) >= seed_limit:
                        break
            if len(seed_entity_ids) >= seed_limit:
                break
        chunk_scores: dict[str, float] = {}
        for entity_id in seed_entity_ids:
            for chunk_id in chunks_by_entity.get(entity_id, []):
                chunk_scores[chunk_id] = max(
                    chunk_scores.get(chunk_id, 0.0),
                    fused[entity_id],
                )
        return self._normalize_scores(chunk_scores), seed_entity_ids

    async def _call_milvus_chunk_scores(
        self,
        query: str,
        top_k: int,
        query_embedding: list[float] | None,
    ) -> dict[str, float]:
        try:
            return await self._milvus_chunk_scores(query, top_k, query_embedding=query_embedding)
        except TypeError as exc:
            if "query_embedding" not in str(exc):
                raise
            return await self._milvus_chunk_scores(query, top_k)

    async def _call_milvus_media_scores(self, query: str, top_k: int) -> dict[str, float]:
        try:
            return await self._milvus_media_scores(query, top_k)
        except Exception as exc:
            logger.debug("[SelfDirectRetrieverV2] Milvus media recall skipped: %s", exc)
            return {}

    async def _nebula_graph_scores(self, seed_entity_ids: list[str], top_k: int) -> dict[str, float]:
        space = await self._get_nebula_space()
        if not space or not seed_entity_ids:
            return {}
        try:
            rows = await asyncio.to_thread(
                self._nebula_graph_search_sync,
                space,
                seed_entity_ids[: max(top_k, 1)],
                max(top_k * 8, 30),
                typed_only=self._graph_profile in {"filtered", "optimized"},
            )
        except Exception as exc:
            logger.debug("[SelfDirectRetrieverV2] Nebula graph recall skipped: %s", exc)
            return {}
        scores: dict[str, float] = {}
        for chunk_ids, score in rows:
            for chunk_id in chunk_ids:
                scores[chunk_id] = max(scores.get(chunk_id, 0.0), score)
        return self._normalize_scores(scores)

    @staticmethod
    def _escape_ngql(value: str) -> str:
        return str(value or "").replace("\\", "\\\\").replace('"', '\\"')

    @staticmethod
    def _nebula_value_to_python(value: Any) -> Any:
        for method in ("as_string", "as_str"):
            if hasattr(value, method):
                try:
                    raw = getattr(value, method)()
                    if isinstance(raw, bytes):
                        return raw.decode("utf-8", errors="ignore")
                    return raw
                except Exception:
                    pass
        for method in ("as_double", "as_float"):
            if hasattr(value, method):
                try:
                    return float(getattr(value, method)())
                except Exception:
                    pass
        if hasattr(value, "as_int"):
            try:
                return int(value.as_int())
            except Exception:
                pass
        return value

    @staticmethod
    def _nebula_graph_search_sync(
        space: str,
        seed_entity_ids: list[str],
        limit: int,
        *,
        typed_only: bool = False,
    ) -> list[tuple[list[str], float]]:
        from nebula3.Config import Config
        from nebula3.gclient.net import ConnectionPool

        config = Config()
        config.max_connection_pool_size = 4
        config.timeout = int(_backend_timeout_seconds() * 1000)
        pool = ConnectionPool()
        ok = pool.init([(settings.nebula_host, settings.nebula_graph_port)], config)
        if not ok:
            return []
        session = None
        try:
            session = pool.get_session(settings.nebula_user, settings.nebula_password)
            result = session.execute(f"USE `{space}`")
            if not result.is_succeeded():
                return []
            query = SelfDirectRetrieverV2._nebula_graph_query(
                seed_entity_ids,
                limit,
                typed_only=typed_only,
            )
            result = session.execute(query)
            if not result.is_succeeded():
                return []
            output: list[tuple[list[str], float]] = []
            for idx in range(result.row_size()):
                row = result.row_values(idx)
                raw_edge_chunk_ids = SelfDirectRetrieverV2._nebula_value_to_python(row[0])
                raw_neighbor_chunk_ids = (
                    SelfDirectRetrieverV2._nebula_value_to_python(row[1])
                    if len(row) > 1
                    else []
                )
                raw_weight = (
                    SelfDirectRetrieverV2._nebula_value_to_python(row[2])
                    if len(row) > 2
                    else 1.0
                )
                weight = float(raw_weight or 1.0)
                edge_chunk_ids = SelfDirectRetrieverV2._as_list(raw_edge_chunk_ids)
                neighbor_chunk_ids = SelfDirectRetrieverV2._as_list(raw_neighbor_chunk_ids)
                if edge_chunk_ids:
                    output.append((edge_chunk_ids, weight))
                if neighbor_chunk_ids:
                    output.append((neighbor_chunk_ids, weight * 0.35))
            return output
        finally:
            if session is not None:
                session.release()
            pool.close()

    @staticmethod
    def _nebula_graph_query(
        seed_entity_ids: list[str],
        limit: int,
        *,
        typed_only: bool = False,
    ) -> str:
        quoted = ", ".join(
            f'"{SelfDirectRetrieverV2._escape_ngql(item)}"'
            for item in seed_entity_ids
            if item
        )
        status_filter = (
            'coalesce(properties(edge).status, "weak_evidence") == "active" '
            'AND coalesce(properties(edge).relation_type, "co_occurs") != "co_occurs" '
            if typed_only
            else '(coalesce(properties(edge).status, "weak_evidence") == "active" '
            'OR coalesce(properties(edge).status, "weak_evidence") == "weak_evidence") '
        )
        return (
            f"GO FROM {quoted} OVER relation_v2 BIDIRECT "
            "WHERE coalesce(properties(edge).is_visible, true) == true "
            f"AND {status_filter}"
            "YIELD "
            "properties(edge).evidence_chunk_ids AS edge_chunk_ids, "
            "$$.entity_v2.chunk_ids AS neighbor_chunk_ids, "
            "properties(edge).weight AS weight "
            f"| LIMIT {int(limit)}"
        )

    async def _pathrag_paths(
        self,
        seed_entity_ids: list[str],
    ) -> tuple[PathRAGPath, ...]:
        seed_key = tuple(self._dedupe(seed_entity_ids))
        if self._pathrag_paths_cache is not None:
            cached_key, cached_paths = self._pathrag_paths_cache
            if cached_key == seed_key:
                return cached_paths
        if len(seed_key) < 2:
            self._pathrag_paths_cache = (seed_key, ())
            return ()
        space = await self._get_nebula_space()
        if not space:
            self._pathrag_paths_cache = (seed_key, ())
            return ()
        try:
            edges = await asyncio.to_thread(
                self._nebula_pathrag_edges_sync,
                space,
                list(seed_key),
                max(int(settings.self_pathrag_max_hops or 1), 1),
                max(int(settings.self_pathrag_edge_limit or 1), 1),
                max(int(settings.self_pathrag_node_limit or 2), 2),
            )
            paths = tuple(
                self._select_pathrag_paths(
                    edges,
                    list(seed_key),
                    max_hops=max(int(settings.self_pathrag_max_hops or 1), 1),
                    max_paths=max(int(settings.self_pathrag_max_paths or 1), 1),
                    alpha=min(max(float(settings.self_pathrag_flow_alpha), 0.0), 1.0),
                    prune_threshold=max(
                        float(settings.self_pathrag_prune_threshold), 0.0
                    ),
                    diversity=min(
                        max(float(settings.self_pathrag_diversity), 0.0), 1.0
                    ),
                )
            )
        except Exception as exc:
            logger.debug("[SelfDirectRetrieverV2] PathRAG traversal skipped: %s", exc)
            paths = ()
        self._pathrag_paths_cache = (seed_key, paths)
        return paths

    @staticmethod
    def _select_pathrag_paths(
        edges: list[PathRAGEdge],
        seed_entity_ids: list[str],
        *,
        max_hops: int,
        max_paths: int,
        alpha: float,
        prune_threshold: float,
        diversity: float,
    ) -> list[PathRAGPath]:
        """Select resource-efficient, evidence-grounded seed-to-seed paths.

        Resource passed over an edge is ``alpha * resource / out_degree``.
        The density threshold prunes hub-heavy branches before expansion.  A
        path is eligible only if every edge maps back to at least one source
        chunk, which keeps graph context auditable by downstream RAGAS checks.
        """

        if max_hops <= 0 or max_paths <= 0:
            return []
        seeds = list(dict.fromkeys(value for value in seed_entity_ids if value))
        if len(seeds) < 2:
            return []
        valid_edges: list[PathRAGEdge] = []
        seen_edges: set[tuple[str, str, str]] = set()
        for edge in edges:
            if (
                not edge.source_id
                or not edge.target_id
                or edge.source_id == edge.target_id
                or not edge.evidence_chunk_ids
                or edge.relation_type == "co_occurs"
            ):
                continue
            key = (
                edge.relation_id,
                min(edge.source_id, edge.target_id),
                max(edge.source_id, edge.target_id),
            )
            if key in seen_edges:
                continue
            seen_edges.add(key)
            valid_edges.append(edge)
        adjacency: dict[str, list[PathRAGEdge]] = {}
        for edge in valid_edges:
            adjacency.setdefault(edge.source_id, []).append(edge)
            adjacency.setdefault(edge.target_id, []).append(edge)
        if not adjacency:
            return []
        for incident in adjacency.values():
            incident.sort(
                key=lambda edge: (-max(float(edge.weight), 0.0), edge.relation_id)
            )

        seed_set = set(seeds)
        seed_rank = {entity_id: rank for rank, entity_id in enumerate(seeds)}
        branch_limit = max(8, min(24, max_paths * 4))
        state_limit = max(2000, max_paths * 1000)
        explored_states = 0
        candidates: list[PathRAGPath] = []
        candidate_keys: set[tuple[tuple[str, ...], tuple[str, ...]]] = set()

        def visit(
            start: str,
            current: str,
            entity_path: tuple[str, ...],
            edge_path: tuple[PathRAGEdge, ...],
            resources: tuple[float, ...],
        ) -> None:
            nonlocal explored_states
            if len(edge_path) >= max_hops or explored_states >= state_limit:
                return
            degree = max(len(adjacency.get(current, ())), 1)
            for edge in adjacency.get(current, ())[:branch_limit]:
                if explored_states >= state_limit:
                    return
                explored_states += 1
                neighbor = edge.other(current)
                if not neighbor or neighbor in entity_path:
                    continue
                next_resource = resources[-1] * alpha / degree
                neighbor_degree = max(len(adjacency.get(neighbor, ())), 1)
                if (
                    neighbor not in seed_set
                    and next_resource / neighbor_degree < prune_threshold
                ):
                    continue
                next_entities = (*entity_path, neighbor)
                next_edges = (*edge_path, edge)
                next_resources = (*resources, next_resource)
                if neighbor in seed_set and neighbor != start:
                    relation_ids = tuple(item.relation_id for item in next_edges)
                    reverse_ids = tuple(reversed(relation_ids))
                    entity_key = min(next_entities, tuple(reversed(next_entities)))
                    path_key = (min(relation_ids, reverse_ids), entity_key)
                    if path_key in candidate_keys:
                        continue
                    candidate_keys.add(path_key)
                    evidence_flags = [
                        bool(item.evidence_chunk_ids) for item in next_edges
                    ]
                    completeness = sum(evidence_flags) / len(evidence_flags)
                    if completeness < 1.0:
                        continue
                    flow_score = sum(next_resources) / len(next_edges)
                    supports = [
                        min(
                            math.log1p(max(float(item.weight), 0.0))
                            / math.log(6.0),
                            1.0,
                        )
                        for item in next_edges
                    ]
                    support_score = 0.5 + 0.5 * (
                        sum(supports) / len(supports)
                    )
                    endpoint_prior = (
                        1.0 / (1.0 + 0.08 * seed_rank[start])
                        + 1.0 / (1.0 + 0.08 * seed_rank[neighbor])
                    ) / 2.0
                    candidates.append(
                        PathRAGPath(
                            entity_ids=next_entities,
                            edges=next_edges,
                            score=flow_score
                            * support_score
                            * endpoint_prior
                            * completeness,
                            flow_score=flow_score,
                            evidence_completeness=completeness,
                        )
                    )
                    continue
                visit(
                    start,
                    neighbor,
                    next_entities,
                    next_edges,
                    next_resources,
                )

        for seed in seeds:
            if seed in adjacency and explored_states < state_limit:
                visit(seed, seed, (seed,), (), (1.0,))
        if not candidates:
            return []

        remaining = sorted(
            candidates,
            key=lambda path: (
                -path.score,
                len(path.edges),
                tuple(edge.relation_id for edge in path.edges),
            ),
        )
        selected: list[PathRAGPath] = []
        selected_evidence: list[set[str]] = []
        while remaining and len(selected) < max_paths:
            best_index = 0
            best_adjusted = float("-inf")
            for index, path in enumerate(remaining):
                evidence = {
                    chunk_id
                    for edge in path.edges
                    for chunk_id in edge.evidence_chunk_ids
                }
                overlap = max(
                    (
                        len(evidence & previous) / max(len(evidence | previous), 1)
                        for previous in selected_evidence
                    ),
                    default=0.0,
                )
                adjusted = path.score * (1.0 - diversity * overlap)
                if adjusted > best_adjusted:
                    best_adjusted = adjusted
                    best_index = index
            chosen = remaining.pop(best_index)
            chosen = PathRAGPath(
                entity_ids=chosen.entity_ids,
                edges=chosen.edges,
                score=max(best_adjusted, 0.0),
                flow_score=chosen.flow_score,
                evidence_completeness=chosen.evidence_completeness,
            )
            selected.append(chosen)
            selected_evidence.append(
                {
                    chunk_id
                    for edge in chosen.edges
                    for chunk_id in edge.evidence_chunk_ids
                }
            )
        return selected

    @staticmethod
    def _nebula_pathrag_edge_query(entity_ids: list[str], limit: int) -> str:
        quoted = ", ".join(
            f'"{SelfDirectRetrieverV2._escape_ngql(item)}"'
            for item in entity_ids
            if item
        )
        return (
            f"GO FROM {quoted} OVER relation_v2 BIDIRECT "
            "WHERE coalesce(properties(edge).is_visible, true) == true "
            'AND coalesce(properties(edge).status, "weak_evidence") == "active" '
            'AND coalesce(properties(edge).relation_type, "co_occurs") != "co_occurs" '
            "YIELD "
            "src(edge) AS source_entity_id, "
            "dst(edge) AS target_entity_id, "
            "properties(edge).relation_id AS relation_id, "
            "properties(edge).relation_type AS relation_type, "
            "properties(edge).description AS description, "
            "properties(edge).evidence_chunk_ids AS evidence_chunk_ids, "
            "properties(edge).weight AS weight "
            "| ORDER BY $-.weight DESC "
            f"| LIMIT {max(int(limit), 1)}"
        )

    @staticmethod
    def _nebula_pathrag_edges_sync(
        space: str,
        seed_entity_ids: list[str],
        max_hops: int,
        edge_limit: int,
        node_limit: int,
    ) -> list[PathRAGEdge]:
        from nebula3.Config import Config
        from nebula3.gclient.net import ConnectionPool

        config = Config()
        config.max_connection_pool_size = 4
        config.timeout = int(_backend_timeout_seconds() * 1000)
        pool = ConnectionPool()
        ok = pool.init([(settings.nebula_host, settings.nebula_graph_port)], config)
        if not ok:
            return []
        session = None
        try:
            session = pool.get_session(settings.nebula_user, settings.nebula_password)
            result = session.execute(f"USE `{space}`")
            if not result.is_succeeded():
                return []
            frontier = set(value for value in seed_entity_ids if value)
            visited_nodes: set[str] = set()
            edges: dict[tuple[str, str, str], PathRAGEdge] = {}
            per_hop_limit = max(edge_limit // max(max_hops, 1), 50)
            for _hop in range(max_hops):
                frontier -= visited_nodes
                if not frontier or len(edges) >= edge_limit:
                    break
                remaining = edge_limit - len(edges)
                query = SelfDirectRetrieverV2._nebula_pathrag_edge_query(
                    sorted(frontier),
                    min(per_hop_limit, remaining),
                )
                result = session.execute(query)
                if not result.is_succeeded():
                    logger.debug(
                        "[SelfDirectRetrieverV2] PathRAG nGQL failed: %s",
                        result.error_msg(),
                    )
                    break
                next_scores: dict[str, float] = {}
                for index in range(result.row_size()):
                    row = result.row_values(index)
                    source_id = str(
                        SelfDirectRetrieverV2._nebula_value_to_python(row[0]) or ""
                    )
                    target_id = str(
                        SelfDirectRetrieverV2._nebula_value_to_python(row[1]) or ""
                    )
                    relation_id = str(
                        SelfDirectRetrieverV2._nebula_value_to_python(row[2]) or ""
                    )
                    relation_type = str(
                        SelfDirectRetrieverV2._nebula_value_to_python(row[3]) or ""
                    )
                    description = str(
                        SelfDirectRetrieverV2._nebula_value_to_python(row[4]) or ""
                    )
                    evidence_chunk_ids = tuple(
                        SelfDirectRetrieverV2._as_list(
                            SelfDirectRetrieverV2._nebula_value_to_python(row[5])
                        )
                    )
                    weight = float(
                        SelfDirectRetrieverV2._nebula_value_to_python(row[6]) or 1.0
                    )
                    if (
                        not source_id
                        or not target_id
                        or not evidence_chunk_ids
                        or relation_type == "co_occurs"
                    ):
                        continue
                    edge = PathRAGEdge(
                        source_id=source_id,
                        target_id=target_id,
                        relation_id=relation_id
                        or f"{source_id}->{relation_type}->{target_id}",
                        relation_type=relation_type,
                        description=description,
                        evidence_chunk_ids=evidence_chunk_ids,
                        weight=weight,
                    )
                    key = (
                        edge.relation_id,
                        min(source_id, target_id),
                        max(source_id, target_id),
                    )
                    edges[key] = edge
                    if source_id not in visited_nodes:
                        next_scores[source_id] = max(
                            next_scores.get(source_id, 0.0), weight
                        )
                    if target_id not in visited_nodes:
                        next_scores[target_id] = max(
                            next_scores.get(target_id, 0.0), weight
                        )
                visited_nodes.update(frontier)
                available = max(node_limit - len(visited_nodes), 0)
                frontier = {
                    entity_id
                    for entity_id, _score in sorted(
                        next_scores.items(),
                        key=lambda item: (-item[1], item[0]),
                    )[:available]
                    if entity_id not in visited_nodes
                }
            return list(edges.values())
        finally:
            if session is not None:
                session.release()
            pool.close()

    _REDIS_BM25_SCAN_LIMIT = 5000

    async def _redis_chunk_scores(self, query_tokens: list[str], top_k: int) -> dict[str, float]:
        if not query_tokens or not settings.redis_host:
            return {}
        kb_code = await self._get_kb_code()
        if not kb_code:
            return {}
        mode = str(getattr(settings, "self_redis_bm25_mode", "inverted") or "inverted").lower()
        client = get_redis()
        corpus_key = f"{kb_code}:v2:bm25:chunk_corpus"
        try:
            if mode != "scan":
                from backend.rag.ingestion.storage.redis_bm25_index import search_chunk_scores

                scores = await search_chunk_scores(kb_code, query_tokens, top_k=top_k)
                if scores:
                    return self._normalize_scores(scores)

            scores = {}
            lexical_tokens = (
                _tokens(query_tokens[0])
                if len(query_tokens) == 1
                else query_tokens
            )
            cursor: int | bytes = 0
            scanned = 0
            while scanned < self._REDIS_BM25_SCAN_LIMIT:
                cursor, batch = await client.hscan(corpus_key, cursor=cursor, count=500)
                for raw_chunk_id, raw_content in batch.items():
                    chunk_id = raw_chunk_id.decode("utf-8") if isinstance(raw_chunk_id, bytes) else str(raw_chunk_id)
                    content = raw_content.decode("utf-8", errors="ignore") if isinstance(raw_content, bytes) else str(raw_content or "")
                    score = self._lexical_score(content, lexical_tokens)
                    if score > 0:
                        scores[chunk_id] = score
                scanned += len(batch)
                if cursor == 0 or cursor == b"0":
                    break
        except Exception as exc:
            logger.debug("[SelfDirectRetrieverV2] Redis v2 BM25 recall skipped: %s", exc)
            return {}
        finally:
            await client.aclose()

        ranked = sorted(scores.items(), key=lambda item: item[1], reverse=True)[: max(top_k * 5, top_k)]
        return self._normalize_scores(dict(ranked))

    async def _entity_recall(self, query_tokens: list[str], top_k: int) -> tuple[dict[str, float], list[str]]:
        """Backward-compatible alias; entity recall is Milvus-only in v2."""
        return {}, []

    async def _graph_chunk_scores(self, seed_entity_ids: list[str], top_k: int) -> dict[str, float]:
        """Path-D: NebulaGraph relation_v2 expansion only."""
        if not seed_entity_ids:
            return {}
        if self._graph_profile == "pathrag":
            scores: dict[str, float] = {}
            for path in await self._pathrag_paths(seed_entity_ids):
                for edge_position, edge in enumerate(path.edges):
                    contribution = path.score / (1.0 + 0.1 * edge_position)
                    for chunk_id in edge.evidence_chunk_ids:
                        scores[chunk_id] = scores.get(chunk_id, 0.0) + contribution
            ranked = self._top_score_map(scores, max(top_k * 2, top_k))
            return self._normalize_scores(ranked)
        return await self._nebula_graph_scores(seed_entity_ids, top_k)

    async def _query_embedding(self, query: str) -> list[float] | None:
        if not (query or "").strip() or not settings.milvus_connection_uri:
            return None
        try:
            vectors = await embed_texts([query])
            return vectors[0] if vectors else None
        except Exception as exc:
            logger.debug("[SelfDirectRetrieverV2] query embedding skipped: %s", exc)
            return None

    async def _milvus_chunk_scores(
        self,
        query: str,
        top_k: int,
        *,
        query_embedding: list[float] | None = None,
    ) -> dict[str, float]:
        if not (query or "").strip() or not settings.milvus_connection_uri:
            return {}
        kb_code = await self._get_kb_code()
        if not kb_code:
            return {}
        try:
            vector = query_embedding or (await embed_texts([query]))[0]
            rows = await asyncio.to_thread(
                self._milvus_chunk_search_sync,
                kb_code,
                self._filter,
                vector,
                max(top_k * 5, top_k),
            )
        except Exception as exc:
            logger.debug("[SelfDirectRetrieverV2] Milvus chunk recall skipped: %s", exc)
            return {}
        return self._normalize_scores(dict(rows))

    async def _milvus_media_scores(self, query: str, top_k: int) -> dict[str, float]:
        if not (query or "").strip() or not settings.milvus_connection_uri:
            return {}
        kb_code = await self._get_kb_code()
        if not kb_code:
            return {}
        try:
            vectors = await embed_media_texts([query])
            if not vectors:
                return {}
            rows = await asyncio.to_thread(
                self._milvus_media_search_sync,
                kb_code,
                self._filter,
                vectors[0],
                max(top_k * 8, top_k),
            )
        except Exception as exc:
            logger.debug("[SelfDirectRetrieverV2] Milvus media vector recall skipped: %s", exc)
            return {}
        return self._normalize_scores(dict(rows))

    async def _milvus_entity_chunk_scores(self, query: str, top_k: int) -> dict[str, float]:
        scores, _ = await self._milvus_entity_recall(query, top_k)
        return scores

    async def _milvus_entity_recall(
        self,
        query: str,
        top_k: int,
        *,
        query_embedding: list[float] | None = None,
    ) -> tuple[dict[str, float], list[str]]:
        if not (query or "").strip() or not settings.milvus_connection_uri:
            return {}, []
        kb_code = await self._get_kb_code()
        if not kb_code:
            return {}, []
        try:
            vector = query_embedding or (await embed_texts([query]))[0]
            rows = await asyncio.to_thread(
                self._milvus_entity_search_sync,
                kb_code,
                self._filter,
                vector,
                max(top_k * 4, top_k),
            )
        except Exception as exc:
            logger.debug("[SelfDirectRetrieverV2] Milvus entity recall skipped: %s", exc)
            return {}, []
        scores: dict[str, float] = {}
        seed_entity_ids: list[str] = []
        for entity_id, chunk_ids, score in rows:
            if entity_id:
                seed_entity_ids.append(entity_id)
            for chunk_id in chunk_ids:
                scores[chunk_id] = max(scores.get(chunk_id, 0.0), score)
        return self._normalize_scores(scores), self._dedupe(seed_entity_ids)[: max(top_k, 1)]

    @staticmethod
    def _milvus_chunk_search_sync(
        kb_code: str,
        retrieval_filter: RetrievalFilter,
        vector: list[float],
        limit: int,
    ) -> list[tuple[str, float]]:
        from pymilvus import Collection, utility

        alias = _ensure_milvus_connection()
        timeout = _backend_timeout_seconds()
        collection_name = chunks_collection_name(kb_code)
        if not utility.has_collection(collection_name, using=alias, timeout=timeout):
            return []
        collection = Collection(collection_name, using=alias)
        collection.load(timeout=timeout)
        results = collection.search(
            data=[vector],
            anns_field="vector",
            param={"metric_type": "COSINE", "params": {}},
            limit=limit,
            expr=SelfDirectRetrieverV2._milvus_chunk_expr(retrieval_filter),
            output_fields=["chunk_id"],
            timeout=timeout,
        )
        output: list[tuple[str, float]] = []
        for hit in results[0] if results else []:
            chunk_id = hit.entity.get("chunk_id")
            if chunk_id:
                output.append((str(chunk_id), float(hit.score or 0.0)))
        return output

    @staticmethod
    def _milvus_media_search_sync(
        kb_code: str,
        retrieval_filter: RetrievalFilter,
        vector: list[float],
        limit: int,
    ) -> list[tuple[str, float]]:
        from pymilvus import Collection, utility

        alias = _ensure_milvus_connection()
        timeout = _backend_timeout_seconds()
        collection_name = media_collection_name(kb_code)
        if not utility.has_collection(collection_name, using=alias, timeout=timeout):
            return []
        collection = Collection(collection_name, using=alias)
        collection.load(timeout=timeout)
        results = collection.search(
            data=[vector],
            anns_field="vector",
            param={"metric_type": "COSINE", "params": {}},
            limit=limit,
            expr=SelfDirectRetrieverV2._milvus_media_expr(retrieval_filter),
            output_fields=["chunk_id"],
            timeout=timeout,
        )
        output: list[tuple[str, float]] = []
        for hit in results[0] if results else []:
            chunk_id = hit.entity.get("chunk_id")
            if chunk_id:
                output.append((str(chunk_id), float(hit.score or 0.0)))
        return output

    @staticmethod
    def _milvus_entity_search_sync(
        kb_code: str,
        retrieval_filter: RetrievalFilter,
        vector: list[float],
        limit: int,
    ) -> list[tuple[str, list[str], float]]:
        from pymilvus import Collection, utility

        alias = _ensure_milvus_connection()
        timeout = _backend_timeout_seconds()
        collection_name = entities_collection_name(kb_code)
        if not utility.has_collection(collection_name, using=alias, timeout=timeout):
            return []
        collection = Collection(collection_name, using=alias)
        collection.load(timeout=timeout)
        results = collection.search(
            data=[vector],
            anns_field="vector",
            param={"metric_type": "COSINE", "params": {}},
            limit=limit,
            expr=SelfDirectRetrieverV2._milvus_entity_expr(retrieval_filter),
            output_fields=["entity_id", "chunk_ids"],
            timeout=timeout,
        )
        output: list[tuple[str, list[str], float]] = []
        for hit in results[0] if results else []:
            raw_entity_id = hit.entity.get("entity_id") or getattr(hit, "id", "")
            raw_chunk_ids = hit.entity.get("chunk_ids")
            chunk_ids = SelfDirectRetrieverV2._as_list(raw_chunk_ids)
            entity_id = str(raw_entity_id or "")
            if entity_id or chunk_ids:
                output.append((entity_id, chunk_ids, float(hit.score or 0.0)))
        return output

    @staticmethod
    def _milvus_entity_multi_search_sync(
        kb_code: str,
        retrieval_filter: RetrievalFilter,
        vectors: list[list[float]],
        limit: int,
    ) -> list[list[tuple[str, list[str], float]]]:
        """Search all PathRAG clause vectors through one Milvus connection."""

        from pymilvus import Collection, utility

        alias = _ensure_milvus_connection()
        timeout = _backend_timeout_seconds()
        collection_name = entities_collection_name(kb_code)
        if not utility.has_collection(collection_name, using=alias, timeout=timeout):
            return []
        collection = Collection(collection_name, using=alias)
        collection.load(timeout=timeout)
        results = collection.search(
            data=vectors,
            anns_field="vector",
            param={"metric_type": "COSINE", "params": {}},
            limit=max(int(limit), 1),
            expr=SelfDirectRetrieverV2._milvus_entity_expr(retrieval_filter),
            output_fields=["entity_id", "chunk_ids"],
            timeout=timeout,
        )
        output: list[list[tuple[str, list[str], float]]] = []
        for hits in results or []:
            per_variant: list[tuple[str, list[str], float]] = []
            for hit in hits:
                raw_entity_id = hit.entity.get("entity_id") or getattr(hit, "id", "")
                entity_id = str(raw_entity_id or "")
                chunk_ids = SelfDirectRetrieverV2._as_list(
                    hit.entity.get("chunk_ids")
                )
                if entity_id or chunk_ids:
                    per_variant.append(
                        (entity_id, chunk_ids, float(hit.score or 0.0))
                    )
            output.append(per_variant)
        return output

    async def _fetch_visible_chunks(self, chunk_ids: list[str]) -> list[SelfIngestChunk]:
        if not chunk_ids:
            return []
        stmt = (
            select(SelfIngestChunk)
            .where(
                *self._chunk_conditions(),
                SelfIngestChunk.chunk_id.in_(chunk_ids),
            )
        )
        stmt = self._apply_doc_time_filters(stmt)
        return list((await self._session.execute(stmt)).scalars().all())

    async def _fetch_entities_by_ids(
        self,
        entity_ids: list[str],
        *,
        entity_scores: dict[str, float] | None = None,
    ) -> list[dict[str, Any]]:
        if not entity_ids:
            return []
        space = await self._get_nebula_space()
        if not space:
            return []
        try:
            rows = await asyncio.to_thread(
                self._nebula_entity_context_sync,
                space,
                entity_ids,
            )
        except Exception as exc:
            logger.debug("[SelfDirectRetrieverV2] Nebula entity context fetch skipped: %s", exc)
            return []
        score_map = entity_scores or {}
        return [self._entity_to_dict(row, score_map=score_map) for row in rows]

    async def _entity_name_map(self, entity_ids: set[str]) -> dict[str, str]:
        if not entity_ids:
            return {}
        entities = await self._fetch_entities_by_ids(list(entity_ids))
        return {
            str(entity.get("entity_id") or ""): str(entity.get("entity_name") or entity.get("entity_id") or "")
            for entity in entities
            if entity.get("entity_id")
        }

    async def _fetch_graph_relationships(self, seed_entity_ids: list[str], top_k: int) -> list[dict[str, Any]]:
        if not seed_entity_ids:
            return []
        if self._graph_profile == "pathrag":
            paths = await self._pathrag_paths(seed_entity_ids)
            entity_ids = {
                entity_id
                for path in paths
                for entity_id in path.entity_ids
                if entity_id
            }
            names = await self._entity_name_map(entity_ids)
            relationships: list[dict[str, Any]] = []
            max_path_score = max((path.score for path in paths), default=1.0) or 1.0
            for path_rank, path in enumerate(paths, start=1):
                path_id = f"pathrag-{path_rank}"
                normalized_path_score = path.score / max_path_score
                for edge_position, edge in enumerate(path.edges, start=1):
                    source_name = names.get(edge.source_id, edge.source_id)
                    target_name = names.get(edge.target_id, edge.target_id)
                    relationships.append(
                        {
                            "relation_id": edge.relation_id,
                            "src_id": source_name,
                            "tgt_id": target_name,
                            "src_tgt": [source_name, target_name],
                            "relation_type": edge.relation_type,
                            "description": edge.description,
                            "weight": min(max(float(edge.weight), 0.0), 10.0) / 10.0,
                            "confidence": 0,
                            "source_id": "<SEP>".join(edge.evidence_chunk_ids),
                            "file_path": "",
                            "_graph_score": normalized_path_score,
                            "_recall_path": "path_d_pathrag",
                            "_source_entity_id": edge.source_id,
                            "_target_entity_id": edge.target_id,
                            "_path_id": path_id,
                            "_path_rank": path_rank,
                            "_path_position": edge_position,
                            "_path_score": normalized_path_score,
                            "_flow_score": path.flow_score,
                            "_evidence_completeness": path.evidence_completeness,
                        }
                    )
            return relationships
        space = await self._get_nebula_space()
        if not space:
            return []
        try:
            rows = await asyncio.to_thread(
                self._nebula_relationships_sync,
                space,
                seed_entity_ids[: max(top_k, 1)],
                max(top_k * 8, 30),
                typed_only=self._graph_profile in {"filtered", "optimized", "pathrag"},
            )
        except Exception as exc:
            logger.debug("[SelfDirectRetrieverV2] Nebula relationship context fetch skipped: %s", exc)
            return []
        entity_ids: set[str] = set()
        for row in rows:
            entity_ids.add(str(row.get("source_entity_id") or ""))
            entity_ids.add(str(row.get("target_entity_id") or ""))
        names = await self._entity_name_map(entity_ids)
        relationships: list[dict[str, Any]] = []
        for rank, row in enumerate(rows, start=1):
            source_id = str(row.get("source_entity_id") or "")
            target_id = str(row.get("target_entity_id") or "")
            source_name = names.get(source_id, source_id)
            target_name = names.get(target_id, target_id)
            evidence_chunk_ids = self._as_list(row.get("evidence_chunk_ids"))
            weight = min(float(row.get("weight") or 1), 10.0) / 10.0
            relationships.append(
                {
                    "relation_id": row.get("relation_id") or f"{source_id}->{target_id}",
                    "src_id": source_name,
                    "tgt_id": target_name,
                    "src_tgt": [source_name, target_name],
                    "relation_type": row.get("relation_type") or "co_occurs",
                    "description": row.get("description") or "",
                    "weight": weight,
                    "confidence": int(row.get("confidence") or 0),
                    "source_id": "<SEP>".join(evidence_chunk_ids),
                    "file_path": "",
                    "_graph_score": max(weight, 1.0 / (rank + 1)),
                    "_recall_path": "path_d_graph",
                    "_source_entity_id": source_id,
                    "_target_entity_id": target_id,
                }
            )
        return relationships

    @staticmethod
    def _nebula_entity_context_sync(space: str, entity_ids: list[str]) -> list[dict[str, Any]]:
        from nebula3.Config import Config
        from nebula3.gclient.net import ConnectionPool

        quoted_ids = [
            f'"{SelfDirectRetrieverV2._escape_ngql(entity_id)}"'
            for entity_id in SelfDirectRetrieverV2._dedupe(entity_ids)
            if entity_id
        ]
        if not quoted_ids:
            return []
        config = Config()
        config.max_connection_pool_size = 4
        config.timeout = int(_backend_timeout_seconds() * 1000)
        pool = ConnectionPool()
        ok = pool.init([(settings.nebula_host, settings.nebula_graph_port)], config)
        if not ok:
            return []
        session = None
        try:
            session = pool.get_session(settings.nebula_user, settings.nebula_password)
            use_result = session.execute(f"USE `{space}`")
            if not use_result.is_succeeded():
                return []
            output: list[dict[str, Any]] = []
            for offset in range(0, len(quoted_ids), 200):
                vids = ", ".join(quoted_ids[offset : offset + 200])
                query = (
                    "FETCH PROP ON entity_v2 "
                    f"{vids} "
                    "YIELD properties(vertex).entity_id, properties(vertex).entity_name, "
                    "properties(vertex).entity_type, properties(vertex).description, "
                    "properties(vertex).source_id, properties(vertex).chunk_ids"
                )
                result = session.execute(query)
                if not result.is_succeeded():
                    continue
                for idx in range(result.row_size()):
                    row = result.row_values(idx)
                    entity_id = SelfDirectRetrieverV2._nebula_value_to_python(row[0])
                    if not entity_id:
                        continue
                    output.append(
                        {
                            "entity_id": str(entity_id),
                            "entity_name": str(SelfDirectRetrieverV2._nebula_value_to_python(row[1]) or entity_id),
                            "entity_type": str(SelfDirectRetrieverV2._nebula_value_to_python(row[2]) or "term"),
                            "description": str(SelfDirectRetrieverV2._nebula_value_to_python(row[3]) or ""),
                            "source_id": str(SelfDirectRetrieverV2._nebula_value_to_python(row[4]) or ""),
                            "chunk_ids": SelfDirectRetrieverV2._nebula_value_to_python(row[5]),
                        }
                    )
            return output
        finally:
            if session is not None:
                session.release()
            pool.close()

    @staticmethod
    def _nebula_relationships_sync(
        space: str,
        seed_entity_ids: list[str],
        limit: int,
        *,
        typed_only: bool = False,
    ) -> list[dict[str, Any]]:
        from nebula3.Config import Config
        from nebula3.gclient.net import ConnectionPool

        quoted = ", ".join(
            f'"{SelfDirectRetrieverV2._escape_ngql(item)}"'
            for item in seed_entity_ids
            if item
        )
        if not quoted:
            return []
        config = Config()
        config.max_connection_pool_size = 4
        config.timeout = int(_backend_timeout_seconds() * 1000)
        pool = ConnectionPool()
        ok = pool.init([(settings.nebula_host, settings.nebula_graph_port)], config)
        if not ok:
            return []
        session = None
        try:
            session = pool.get_session(settings.nebula_user, settings.nebula_password)
            result = session.execute(f"USE `{space}`")
            if not result.is_succeeded():
                return []
            relation_filter = (
                'properties(edge).status == "active" '
                'AND properties(edge).relation_type != "co_occurs" '
                if typed_only
                else '(properties(edge).status == "active" '
                'OR properties(edge).status == "weak_evidence") '
            )
            query = (
                f"GO FROM {quoted} OVER relation_v2 "
                "WHERE properties(edge).is_visible == true "
                f"AND {relation_filter}"
                "YIELD "
                "src(edge) AS source_entity_id, "
                "dst(edge) AS target_entity_id, "
                "properties(edge).relation_id AS relation_id, "
                "properties(edge).relation_type AS relation_type, "
                "properties(edge).description AS description, "
                "properties(edge).evidence_chunk_ids AS evidence_chunk_ids, "
                "properties(edge).weight AS weight "
                f"| LIMIT {int(limit)}"
            )
            result = session.execute(query)
            if not result.is_succeeded():
                return []
            output: list[dict[str, Any]] = []
            for idx in range(result.row_size()):
                row = result.row_values(idx)
                output.append(
                    {
                        "source_entity_id": str(SelfDirectRetrieverV2._nebula_value_to_python(row[0]) or ""),
                        "target_entity_id": str(SelfDirectRetrieverV2._nebula_value_to_python(row[1]) or ""),
                        "relation_id": str(SelfDirectRetrieverV2._nebula_value_to_python(row[2]) or ""),
                        "relation_type": str(SelfDirectRetrieverV2._nebula_value_to_python(row[3]) or ""),
                        "description": str(SelfDirectRetrieverV2._nebula_value_to_python(row[4]) or ""),
                        "evidence_chunk_ids": SelfDirectRetrieverV2._nebula_value_to_python(row[5]),
                        "weight": float(SelfDirectRetrieverV2._nebula_value_to_python(row[6]) or 1.0),
                    }
                )
            return output
        finally:
            if session is not None:
                session.release()
            pool.close()

    @staticmethod
    def _entity_ids_from_relationships(relationships: list[dict[str, Any]]) -> set[str]:
        entity_ids: set[str] = set()
        for rel in relationships:
            for key in ("_source_entity_id", "_target_entity_id"):
                value = str(rel.get(key) or "")
                if value:
                    entity_ids.add(value)
        return entity_ids

    async def _fallback_chunks(self, query_tokens: list[str], top_k: int) -> list[RetrievedChunk]:
        conditions = self._chunk_conditions()
        stmt = (
            select(SelfIngestChunk)
            .where(and_(*conditions))
            .order_by(desc(SelfIngestChunk.visible_at), SelfIngestChunk.chunk_index)
            .limit(max(top_k, 1))
        )
        stmt = self._apply_doc_time_filters(stmt)
        rows = list((await self._session.execute(stmt)).scalars().all())
        return [
            self._row_to_retrieved_chunk(
                row,
                {
                    "keyword": self._lexical_score(row.content or "", query_tokens) if query_tokens else 0.1,
                    "paths": {"fallback"},
                },
            )
            for row in rows
        ]

    def build_context(self, chunks: list[RetrievedChunk]) -> str:
        parts: list[str] = []
        for idx, chunk in enumerate(chunks, start=1):
            parts.append(
                f"[{idx}] {chunk.file_name}#{chunk.chunk_index}\n{chunk.content.strip()}"
            )
        return "\n\n".join(parts)

    def _chunk_conditions(self) -> list[Any]:
        conditions: list[Any] = [SelfIngestChunk.kb_id == self._filter.kb_id]
        if self._filter.is_visible is not None:
            conditions.append(SelfIngestChunk.is_visible == bool(self._filter.is_visible))
        if self._filter.is_active is not None:
            conditions.append(SelfIngestChunk.is_active == bool(self._filter.is_active))
        if self._filter.doc_statuses:
            conditions.append(SelfIngestChunk.doc_status.in_(tuple(self._filter.doc_statuses)))
        if self._filter.visible_at_gte is not None:
            conditions.append(SelfIngestChunk.visible_at >= self._filter.visible_at_gte)
        if self._filter.visible_at_lte is not None:
            conditions.append(SelfIngestChunk.visible_at <= self._filter.visible_at_lte)
        return conditions

    def _apply_doc_time_filters(self, stmt):
        if not self._filter.has_doc_time_filters:
            return stmt
        stmt = stmt.join(
            KnowledgeBaseDocument,
            and_(
                KnowledgeBaseDocument.kb_id == SelfIngestChunk.kb_id,
                KnowledgeBaseDocument.doc_id == SelfIngestChunk.doc_id,
            ),
        ).where(KnowledgeBaseDocument.ingestion_backend == self._filter.source_backend)
        if self._filter.uploaded_at_gte is not None:
            stmt = stmt.where(KnowledgeBaseDocument.uploaded_at >= self._filter.uploaded_at_gte)
        if self._filter.uploaded_at_lte is not None:
            stmt = stmt.where(KnowledgeBaseDocument.uploaded_at <= self._filter.uploaded_at_lte)
        if self._filter.committed_at_gte is not None:
            stmt = stmt.where(KnowledgeBaseDocument.committed_at >= self._filter.committed_at_gte)
        if self._filter.committed_at_lte is not None:
            stmt = stmt.where(KnowledgeBaseDocument.committed_at <= self._filter.committed_at_lte)
        return stmt

    @staticmethod
    def _milvus_quote(value: str) -> str:
        return str(value or "").replace("\\", "\\\\").replace('"', '\\"')

    @staticmethod
    def _milvus_in(field: str, values: tuple[str, ...]) -> str:
        quoted = ", ".join(f'"{SelfDirectRetrieverV2._milvus_quote(value)}"' for value in values)
        return f"{field} in [{quoted}]"

    @staticmethod
    def _milvus_chunk_expr(retrieval_filter: RetrievalFilter) -> str:
        parts = [
            f'kb_id == "{SelfDirectRetrieverV2._milvus_quote(retrieval_filter.kb_id)}"',
            f"is_active == {'true' if retrieval_filter.is_active else 'false'}",
            f"is_visible == {'true' if retrieval_filter.is_visible else 'false'}",
        ]
        if retrieval_filter.doc_statuses:
            parts.append(SelfDirectRetrieverV2._milvus_in("doc_status", tuple(retrieval_filter.doc_statuses)))
        if retrieval_filter.commit_status:
            parts.append(
                f'commit_status == "{SelfDirectRetrieverV2._milvus_quote(retrieval_filter.commit_status)}"'
            )
        if retrieval_filter.compatible_schema_versions:
            parts.append(
                SelfDirectRetrieverV2._milvus_in(
                    "schema_version",
                    tuple(retrieval_filter.compatible_schema_versions),
                )
            )
        return " && ".join(parts)

    @staticmethod
    def _milvus_media_expr(retrieval_filter: RetrievalFilter) -> str:
        parts = [
            f'kb_id == "{SelfDirectRetrieverV2._milvus_quote(retrieval_filter.kb_id)}"',
            f"is_active == {'true' if retrieval_filter.is_active else 'false'}",
            f"is_visible == {'true' if retrieval_filter.is_visible else 'false'}",
        ]
        if retrieval_filter.doc_statuses:
            parts.append(SelfDirectRetrieverV2._milvus_in("doc_status", tuple(retrieval_filter.doc_statuses)))
        if retrieval_filter.commit_status:
            parts.append(
                f'commit_status == "{SelfDirectRetrieverV2._milvus_quote(retrieval_filter.commit_status)}"'
            )
        return " && ".join(parts)

    @staticmethod
    def _milvus_entity_expr(retrieval_filter: RetrievalFilter) -> str:
        parts = [
            f'kb_id == "{SelfDirectRetrieverV2._milvus_quote(retrieval_filter.kb_id)}"',
            f"is_visible == {'true' if retrieval_filter.is_visible else 'false'}",
            SelfDirectRetrieverV2._milvus_in("status", states.RETRIEVAL_ENTITY_STATUSES),
        ]
        return " && ".join(parts)

    @staticmethod
    def _lexical_score(content: str, query_tokens: list[str]) -> float:
        content_lower = (content or "").lower()
        if not query_tokens:
            return 0.0
        hits = sum(1 for token in query_tokens if token in content_lower)
        if hits <= 0:
            return 0.0
        density = hits / max(len(query_tokens), 1)
        length_penalty = 1.0 / math.sqrt(max(len(content_lower) / 800, 1.0))
        return float(density * 0.85 + length_penalty * 0.15)

    @staticmethod
    def _clause_coverage_score(content: str, query: str) -> float:
        """Measure balanced lexical coverage across punctuation-delimited clauses."""
        clauses = [
            clause.strip()
            for clause in re.split(r"[,，;；。.!！?？]+", query or "")
            if clause.strip()
        ]
        token_groups = [tokens for clause in clauses if len(tokens := _tokens(clause)) >= 2]
        if len(token_groups) < 2:
            return 0.0
        content_lower = (content or "").lower()
        coverages = [
            sum(1 for token in tokens if token in content_lower) / len(tokens)
            for tokens in token_groups
        ]
        if any(value <= 0.0 for value in coverages):
            return 0.0
        return float(len(coverages) / sum(1.0 / value for value in coverages))

    @staticmethod
    def _media_rerank_score(
        row: SelfIngestChunk,
        *,
        query: str,
        query_tokens: list[str],
        media_refs: set[str],
    ) -> float:
        extra = getattr(row, "extra_json", None) or {}
        if not isinstance(extra, dict):
            extra = {}
        content = row.content or ""
        media_fields = [
            content,
            str(extra.get("media_ref") or ""),
            " ".join(str(item) for item in (extra.get("media_ref_aliases") or []) if item),
            str(extra.get("media_title") or ""),
            str(extra.get("media_caption") or extra.get("caption") or ""),
            str(extra.get("ocr_text") or ""),
            str(extra.get("visual_summary") or extra.get("vision_summary") or ""),
            str(extra.get("nearby_text_before") or ""),
            str(extra.get("nearby_text_after") or ""),
            str(extra.get("source_doc_name") or ""),
        ]
        haystack = "\n".join(item for item in media_fields if item)
        haystack_lower = haystack.lower()
        score = SelfDirectRetrieverV2._lexical_score(haystack, query_tokens)
        if _media_ref_matches(extra, media_refs):
            score += 3.0
        else:
            for ref in media_refs:
                if ref and ref.lower() in haystack_lower:
                    score += 0.45
        title = _normalize_space(str(extra.get("media_title") or extra.get("media_caption") or ""))
        if title and title in query:
            score += 1.0
        if row.block_type == "image":
            score += 0.12
        elif row.block_type in {"table", "formula"}:
            score += 0.08
        return max(score, 0.0)

    @staticmethod
    def _title_context_score(
        row: SelfIngestChunk,
        *,
        query: str = "",
        query_tokens: list[str] | None = None,
    ) -> float:
        extra = getattr(row, "extra_json", None) or {}
        if not isinstance(extra, dict):
            extra = {}

        section_path = extra.get("section_path") or []
        if isinstance(section_path, str):
            section_path = [section_path]
        title_fields = [
            " ".join(str(item) for item in section_path[-4:] if item),
            str(extra.get("media_ref") or ""),
            " ".join(str(item) for item in (extra.get("media_ref_aliases") or []) if item),
            str(extra.get("media_title") or ""),
            str(extra.get("media_caption") or extra.get("caption") or ""),
            str(extra.get("alt_text") or ""),
            str(extra.get("source_doc_name") or ""),
        ]

        content = row.content or ""
        heading_lines: list[str] = []
        for line in content.splitlines()[:6]:
            normalized = _normalize_space(line)
            if not normalized:
                continue
            if len(normalized) <= 120 or normalized.startswith(("章节:", "图", "表", "Fig", "Table")):
                heading_lines.append(normalized)
        title_text = "\n".join(item for item in [*title_fields, *heading_lines] if item)
        if not title_text:
            return 0.0

        tokens = query_tokens or []
        score = SelfDirectRetrieverV2._lexical_score(title_text, tokens)
        query_norm = _normalize_space(query).lower()
        title_norm = _normalize_space(title_text).lower()
        if query_norm and len(query_norm) >= 4:
            if query_norm in title_norm:
                score += 1.0
            else:
                compact_query = re.sub(r"\s+", "", query_norm)
                compact_title = re.sub(r"\s+", "", title_norm)
                if len(compact_query) >= 4 and compact_query in compact_title:
                    score += 0.8
        return max(score, 0.0)

    @staticmethod
    def _source_doc_score(
        row: SelfIngestChunk,
        *,
        query: str = "",
        query_tokens: list[str] | None = None,
    ) -> float:
        extra = getattr(row, "extra_json", None) or {}
        if not isinstance(extra, dict):
            extra = {}
        source_text = "\n".join(
            item
            for item in [
                str(getattr(row, "file_name", "") or ""),
                str(getattr(row, "file_path", "") or ""),
                str(extra.get("source_doc_name") or ""),
            ]
            if item
        )
        if not source_text:
            return 0.0
        tokens = query_tokens or []
        score = SelfDirectRetrieverV2._lexical_score(source_text, tokens)
        query_compact = re.sub(r"\s+", "", _normalize_space(query).lower())
        source_compact = re.sub(r"\s+", "", _normalize_space(source_text).lower())
        source_stem = re.sub(r"\.(?:pdf|docx?|md|txt)$", "", source_compact, flags=re.IGNORECASE)
        if source_stem and len(source_stem) >= 4 and source_stem in query_compact:
            score += 1.0
        return max(score, 0.0)

    @staticmethod
    def _dedupe_scored_chunks(
        chunks: list[RetrievedChunk],
        *,
        image_intent: bool = False,
    ) -> list[RetrievedChunk]:
        if not chunks:
            return []
        seen: set[tuple[str, str]] = set()
        seen_image_signatures: list[set[str]] = []
        output: list[RetrievedChunk] = []
        for chunk in chunks:
            key: tuple[str, str] | None = None
            if image_intent and "Image Path:" in (chunk.content or ""):
                match = re.search(r"Image Path:\s*(\S+)", chunk.content or "")
                if match:
                    key = ("image_path", match.group(1))
            if key is None and "媒体编号:" in (chunk.content or ""):
                match = re.search(r"媒体编号:\s*([^\n ]+)", chunk.content or "")
                if match:
                    key = ("media_ref", f"{chunk.doc_id}:{match.group(1)}")
            if key is not None:
                if key in seen:
                    continue
                seen.add(key)
            if image_intent and "Image Path:" in (chunk.content or ""):
                signature = SelfDirectRetrieverV2._image_description_signature(chunk.content)
                if signature and any(
                    len(signature & previous) / max(len(signature | previous), 1) >= 0.82
                    for previous in seen_image_signatures
                ):
                    continue
                if signature:
                    seen_image_signatures.append(signature)
            output.append(chunk)
        return output

    @staticmethod
    def _image_description_signature(content: str) -> set[str]:
        """Return character-bigram evidence for cross-document image dedupe."""
        descriptions = re.findall(
            r"图片描述:\s*(.*?)(?=\n(?:上下文|图片描述|公式|变量说明|章节|媒体编号):|\Z)",
            content or "",
            flags=re.DOTALL,
        )
        normalized = re.sub(
            r"[^a-z0-9\u4e00-\u9fff]+",
            "",
            " ".join(descriptions).lower(),
        )
        if len(normalized) < 20:
            return set()
        return {normalized[index : index + 2] for index in range(len(normalized) - 1)}

    @staticmethod
    def _limit_non_intent_images(
        chunks: list[RetrievedChunk],
        *,
        max_images: int,
    ) -> list[RetrievedChunk]:
        """Limit image blocks for text intent while preserving score order."""
        output: list[RetrievedChunk] = []
        image_count = 0
        for chunk in chunks:
            is_image = "Image Path:" in (chunk.content or "")
            if is_image:
                if image_count >= max(max_images, 0):
                    continue
                image_count += 1
            output.append(chunk)
        return output

    @staticmethod
    def _ensure_vector_floor(
        chunks: list[RetrievedChunk],
        vector_scores: dict[str, float],
        *,
        top_k: int,
        ratio: float = 0.6,
    ) -> list[RetrievedChunk]:
        """Reserve part of the final frontier for the strongest vector baseline."""
        if not chunks or not vector_scores or top_k <= 0:
            return chunks
        limit = min(int(top_k), len(chunks))
        floor = min(max(math.ceil(limit * ratio), 1), limit)
        by_id = {chunk.chunk_id: chunk for chunk in chunks}
        vector_order = [
            chunk_id
            for chunk_id, _ in sorted(
                vector_scores.items(),
                key=lambda item: (-float(item[1]), item[0]),
            )
            if chunk_id in by_id
        ]
        required_ids = set(vector_order[:floor])
        if not required_ids:
            return chunks

        head = list(chunks[:limit])
        head_ids = {chunk.chunk_id for chunk in head}
        for chunk_id in vector_order[:floor]:
            if chunk_id in head_ids:
                continue
            replacement = next(
                (
                    index
                    for index in range(len(head) - 1, -1, -1)
                    if head[index].chunk_id not in required_ids
                ),
                None,
            )
            if replacement is None:
                break
            head_ids.discard(head[replacement].chunk_id)
            head[replacement] = by_id[chunk_id]
            head_ids.add(chunk_id)

        original_position = {chunk.chunk_id: index for index, chunk in enumerate(chunks)}
        head.sort(key=lambda chunk: original_position[chunk.chunk_id])
        selected = {chunk.chunk_id for chunk in head}
        return [*head, *(chunk for chunk in chunks if chunk.chunk_id not in selected)]

    @staticmethod
    def _normalize_scores(scores: dict[str, float]) -> dict[str, float]:
        if not scores:
            return {}
        max_score = max(float(score) for score in scores.values()) or 1.0
        return {
            key: max(0.0, min(float(score) / max_score, 1.0))
            for key, score in scores.items()
            if score > 0
        }

    @staticmethod
    def _top_score_map(scores: dict[str, float], limit: int) -> dict[str, float]:
        if not scores or limit <= 0:
            return {}
        return dict(
            sorted(scores.items(), key=lambda item: (-float(item[1]), item[0]))[:limit]
        )

    @staticmethod
    def _admit_graph_scores(
        graph: dict[str, float],
        *,
        vector: dict[str, float],
        keyword: dict[str, float],
        media: dict[str, float] | None = None,
        top_k: int,
    ) -> dict[str, float]:
        """Keep corroborated graph evidence plus a small graph-only frontier.

        This is deliberately independent of query language and wording. Chunks
        supported by another retriever are retained first; a bounded novel
        frontier preserves multi-hop recall without allowing graph expansion to
        dominate the final candidate set.
        """

        if not graph or top_k <= 0:
            return {}
        base_ids = set(vector) | set(keyword) | set(media or {})
        ranked = sorted(graph.items(), key=lambda item: (-float(item[1]), item[0]))
        corroborated = [(chunk_id, score) for chunk_id, score in ranked if chunk_id in base_ids]
        novel_limit = max(2, min(5, (top_k + 2) // 3))
        novel = [
            (chunk_id, score)
            for chunk_id, score in ranked
            if chunk_id not in base_ids
        ][:novel_limit]
        admitted = (corroborated + novel)[: max(top_k * 2, novel_limit)]
        return dict(admitted)

    @staticmethod
    def _corroborated_scores(
        primary: dict[str, float],
        *independent: dict[str, float],
    ) -> dict[str, float]:
        """Retain a score only when an independent retriever found the chunk.

        Entity-to-chunk recall is useful as a ranking signal but is too broad
        to admit chunks on its own. Requiring independent evidence avoids the
        noisy direct-entity path without relying on query wording or domains.
        """

        if not primary:
            return {}
        supported_ids: set[str] = set()
        for scores in independent:
            supported_ids.update(scores or {})
        return {
            chunk_id: float(score)
            for chunk_id, score in primary.items()
            if chunk_id in supported_ids
        }

    @staticmethod
    def _merge_score_maps(*score_maps: dict[str, float]) -> dict[str, float]:
        merged: dict[str, float] = {}
        for scores in score_maps:
            for chunk_id, score in (scores or {}).items():
                merged[chunk_id] = max(float(score), merged.get(chunk_id, 0.0))
        return merged

    @staticmethod
    def _dedupe(values: list[str]) -> list[str]:
        seen: set[str] = set()
        output: list[str] = []
        for value in values:
            if value and value not in seen:
                seen.add(value)
                output.append(value)
        return output

    @staticmethod
    def _merge_component_scores(
        *,
        entity: dict[str, float],
        keyword: dict[str, float],
        vector: dict[str, float],
        graph: dict[str, float],
        media: dict[str, float] | None = None,
        profile: str = "legacy",
        image_intent: bool = False,
    ) -> dict[str, dict[str, Any]]:
        merged: dict[str, dict[str, Any]] = {}
        weights = {
            "entity": 0.20,
            "keyword": 0.22,
            "vector": 0.28,
            "graph": 0.12,
            "media": 0.18,
        }
        path_names = {
            "entity": "path_a_entity",
            "keyword": "path_b_keyword",
            "vector": "path_c_chunk_vector",
            "graph": (
                "path_d_pathrag" if profile == "pathrag" else "path_d_graph"
            ),
            "media": "path_e_media",
        }
        component_maps = {
            "entity": entity,
            "keyword": keyword,
            "vector": vector,
            "graph": graph,
            "media": media or {},
        }
        for component, scores in component_maps.items():
            for chunk_id, score in scores.items():
                item = merged.setdefault(
                    chunk_id,
                    {
                        "entity": 0.0,
                        "keyword": 0.0,
                        "vector": 0.0,
                        "graph": 0.0,
                        "media": 0.0,
                        "score": 0.0,
                        "paths": set(),
                    },
                )
                value = max(float(score), float(item[component]))
                item[component] = value
                item["paths"].add(path_names[component])
        if profile in {"optimized", "pathrag"}:
            # Rank fusion avoids treating independently normalized cosine,
            # lexical and graph scores as if they shared one calibrated scale.
            rrf_weights = (
                {
                    "entity": 0.15,
                    "keyword": 1.0,
                    "vector": 0.5,
                    "graph": 0.15,
                    "media": 2.0,
                }
                if image_intent
                else {
                    "entity": 0.35,
                    "keyword": 0.75,
                    "vector": 1.0,
                    "graph": 0.35,
                    "media": 0.75,
                }
            )
            for component, scores in component_maps.items():
                if not scores or rrf_weights[component] <= 0:
                    continue
                ranked = sorted(scores.items(), key=lambda item: (-float(item[1]), item[0]))
                for rank, (chunk_id, _score) in enumerate(ranked, 1):
                    merged[chunk_id]["score"] += rrf_weights[component] / (60.0 + rank)
            max_fused = max((float(item["score"]) for item in merged.values()), default=1.0) or 1.0
            for item in merged.values():
                item["score"] = float(item["score"]) / max_fused
        else:
            for item in merged.values():
                item["score"] = sum(float(item[key]) * weight for key, weight in weights.items())
        for item in merged.values():
            if not item["paths"]:
                item["paths"].add("self_mysql")
        return merged

    @staticmethod
    def _block_type_weights() -> dict[str, float]:
        """Load block_type weight multipliers from settings (§8.2 multi-modal weighting)."""
        raw = getattr(settings, "self_multimodal_block_type_weights", "") or ""
        if raw:
            try:
                return json.loads(raw)
            except Exception:
                pass
        return {"text": 1.0, "image": 1.2, "table": 1.3, "formula": 1.1}

    @staticmethod
    def _entity_to_dict(row: dict[str, Any], *, score_map: dict[str, float]) -> dict[str, Any]:
        chunk_ids = SelfDirectRetrieverV2._as_list(row.get("chunk_ids"))
        doc_ids = [item for item in str(row.get("source_id") or "").split(",") if item]
        entity_score = max((score_map.get(chunk_id, 0.0) for chunk_id in chunk_ids), default=0.0)
        entity_id = str(row.get("entity_id") or "")
        entity_name = str(row.get("entity_name") or entity_id)
        return {
            "entity_id": entity_id,
            "entity_name": entity_name,
            "entity_type": row.get("entity_type") or "term",
            "description": row.get("description") or "",
            "source_id": "<SEP>".join(chunk_ids),
            "file_path": "",
            "doc_ids": doc_ids,
            "chunk_ids": chunk_ids,
            "support_count": len(chunk_ids),
            "_fused_score": entity_score,
            "_entity_score": entity_score,
            "_recall_path": "path_a_entity",
        }

    @staticmethod
    def _row_to_retrieved_chunk(
        row: SelfIngestChunk,
        scores: dict[str, Any],
        *,
        query: str = "",
        query_tokens: list[str] | None = None,
        image_intent: bool = False,
        figure_refs: set[str] | None = None,
    ) -> RetrievedChunk:
        score = float(scores.get("score") or scores.get("keyword") or 0.1)
        block_type = getattr(row, "block_type", "text") or "text"
        type_weight = SelfDirectRetrieverV2._block_type_weights().get(block_type, 1.0)
        if block_type == "image" and not image_intent:
            # Do not grant the image-intent boost to ordinary text queries.
            # Keep neutral weight because many PDF "image" blocks also carry
            # the only OCR/context text for the surrounding page.
            type_weight = min(type_weight, 1.0)
        raw_content = row.content or ""
        keyword_score = float(scores.get("keyword") or 0.0)
        vec_score = float(scores.get("vector") or 0.0)
        media_score = float(scores.get("media") or 0.0)
        title_score = 0.0
        source_score = 0.0
        if query_tokens:
            keyword_score = max(keyword_score, SelfDirectRetrieverV2._lexical_score(raw_content, query_tokens))
            title_score = SelfDirectRetrieverV2._title_context_score(
                row,
                query=query,
                query_tokens=query_tokens,
            )
            source_score = SelfDirectRetrieverV2._source_doc_score(
                row,
                query=query,
                query_tokens=query_tokens,
            )
        if keyword_score >= 0.95 and vec_score >= 0.90:
            score += 0.18
        elif keyword_score >= 0.80 and vec_score >= 0.85:
            score += 0.08
        if title_score >= 1.20:
            score += 0.30
        elif title_score >= 0.85:
            score += 0.18
        elif title_score >= 0.55:
            score += 0.08
        if source_score >= 1.20:
            score += 0.22
        elif source_score >= 0.75:
            score += 0.12
        elif source_score >= 0.45:
            score += 0.05
        refs = figure_refs or set()
        if refs and any(ref and ref in raw_content for ref in refs):
            score += 0.25
        extra = getattr(row, "extra_json", None) or {}
        if not isinstance(extra, dict):
            extra = {}
        if refs and _media_ref_matches(extra, refs):
            score += 0.45
        if media_score:
            score += media_score * (0.40 if image_intent else 0.22)
        if image_intent:
            score += SelfDirectRetrieverV2._clause_coverage_score(raw_content, query) * 1.2
            if block_type == "image":
                score += 0.55
                if keyword_score >= 0.65:
                    score += 0.18
                if vec_score >= 0.85:
                    score += 0.12
                if source_score >= 0.45:
                    score += 0.12
            elif block_type == "text":
                score *= 0.62 if title_score < 1.20 else 0.72
            elif block_type in {"table", "formula"}:
                score *= 0.62
        score *= type_weight
        paths = scores.get("paths") or {"self_mysql"}
        if not isinstance(paths, set):
            paths = set(paths)
        content = SelfDirectRetrieverV2._row_content_for_model_context(
            row,
            query=query,
            table_max_rows=max(int(settings.self_parent_expansion_table_max_rows or 0), 0),
        )
        page_nums = _page_numbers_from_extra(extra)
        return RetrievedChunk(
            chunk_id=row.chunk_id,
            doc_id=row.doc_id,
            content=content,
            content_hash=row.content_hash or "",
            file_name=row.file_name,
            file_path=row.file_path or "",
            chunk_index=row.chunk_index,
            score=score,
            block_type=str(row.block_type or "text").lower(),
            page_num=page_nums[0] if len(page_nums) == 1 else None,
            page_nums=page_nums,
            vec_score=vec_score,
            entity_score=float(scores.get("entity") or 0.0),
            keyword_score=keyword_score,
            graph_score=float(scores.get("graph") or 0.0),
            recall_paths=tuple(sorted(paths)),
        )

    @staticmethod
    def _row_content_for_model_context(
        row: SelfIngestChunk,
        *,
        query: str = "",
        table_max_rows: int = 0,
    ) -> str:
        """Expose images and focus large tables into model-ready evidence."""

        content = row.content or ""
        block_type = str(getattr(row, "block_type", "") or "").lower()
        if block_type == "table":
            return _focus_table_evidence(content, query, max_rows=table_max_rows)
        outline = _enumerated_heading_summary(content)
        if outline:
            content = outline + "\n" + content
        if block_type != "image":
            return content
        if "Image Path:" in content:
            return content

        extra = getattr(row, "extra_json", None) or {}
        if not isinstance(extra, dict):
            return content
        image_path = str(
            extra.get("persistent_object_key")
            or extra.get("img_path")
            or extra.get("image_path")
            or ""
        ).strip()
        if not image_path:
            return content

        caption = str(
            extra.get("caption")
            or extra.get("alt_text")
            or ""
        ).strip()
        visual = str(extra.get("vision_summary") or content or "").strip()
        media_ref = str(extra.get("media_ref") or "").strip()
        media_title = str(extra.get("media_title") or "").strip()
        ocr_text = str(extra.get("ocr_text") or "").strip()
        nearby_before = str(extra.get("nearby_text_before") or "").strip()
        nearby_after = str(extra.get("nearby_text_after") or "").strip()
        if not caption:
            caption = SelfDirectRetrieverV2._fallback_image_caption(content, visual)
        details = [
            "Image Content Analysis:",
            f"Image Path: {image_path}",
            f"Captions: {caption}",
        ]
        if media_ref:
            details.append(f"Media Ref: {media_ref}")
        if media_title:
            details.append(f"Media Title: {media_title}")
        if ocr_text:
            details.append(f"OCR Text: {ocr_text}")
        if nearby_before or nearby_after:
            details.append("Nearby Text:")
            if nearby_before:
                details.append(f"Before: {nearby_before}")
            if nearby_after:
                details.append(f"After: {nearby_after}")
        details.extend([
            "Footnotes:",
            f"Visual Analysis: {visual}",
        ])
        return "\n".join(details).strip()

    @staticmethod
    def _fallback_image_caption(content: str, visual: str) -> str:
        for pattern in (
            r"(?:图片标题|图注|标题|表格标题)\s*[:：]\s*([^\r\n]{4,160})",
            r"(图\s*\d+(?:\s*[.\-－—–~～￣_]\s*\d+)*[^\r\n]{0,120})",
        ):
            match = re.search(pattern, content or "")
            if match:
                return _normalize_space(match.group(1))[:160]
        first_sentence = re.split(r"[。.!！?？]\s*", visual or "", maxsplit=1)[0].strip()
        return _normalize_space(first_sentence)[:120] if len(first_sentence) >= 8 else ""

    async def _get_kb_code(self) -> str:
        if self._kb_code:
            return self._kb_code
        row = (
            await self._session.execute(
                select(KnowledgeBase.kb_code).where(KnowledgeBase.kb_id == self._kb_id)
            )
        ).scalar_one_or_none()
        self._kb_code = row or ""
        return self._kb_code

    async def _get_nebula_space(self) -> str:
        if self._nebula_space is not None:
            return self._nebula_space
        row = (
            await self._session.execute(
                select(KnowledgeBase.nebula_space).where(KnowledgeBase.kb_id == self._kb_id)
            )
        ).scalar_one_or_none()
        self._nebula_space = (row or "").strip()
        return self._nebula_space

    @staticmethod
    def _as_list(value: Any) -> list[str]:
        if value is None:
            return []
        if isinstance(value, str):
            try:
                value = json.loads(value)
            except json.JSONDecodeError:
                return []
        if not isinstance(value, list):
            return []
        return [str(item) for item in value if item]
