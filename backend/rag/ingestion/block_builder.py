"""Block Builder (附录 C) -- convert ParsedDocument blocks to IngestChunks.

Text-only sections are split by the existing token-based chunker. Mixed
text/image/table/formula sections are grouped into semantic windows so a
formula or figure keeps its nearby explanatory context.
"""

from __future__ import annotations

import re
from collections import Counter
from dataclasses import replace
from datetime import date
from typing import Any

from backend.rag.ingestion.chunker import build_chunks as _text_build_chunks, estimate_tokens
from backend.rag.ingestion.ids import sha256_text, stable_chunk_fingerprint, stable_chunk_id
from backend.rag.ingestion.models import IngestBlock, IngestChunk, ParsedDocument
from backend.rag.media_refs import normalize_media_kind, normalize_media_number


_TEXT_SHORT_MERGE_MIN_TOKENS = 80
_TEXT_SHORT_MERGE_MAX_CHARS = 160
_MIN_CHUNK_CHARS = 80
_MULTIMODAL_CONTEXT_MAX_CHARS = 600
_OVERSIZED_CHUNK_RATIO = 1.5
_MIXED_SEGMENT_SOFT_RATIO = 0.65
_TOC_LEADER_RE = re.compile(r".*[\.·•]{4,}\s*\d{1,4}\s*$")
_LOOSE_TOC_LEADER_RE = re.compile(r".*[\.·•．。\s]{4,}\d{1,4}\s*$")
_STANDALONE_NUMBER_RE = re.compile(r"(?:第\s*)?[\d０-９]{1,4}(?:\s*页)?")
_ROMAN_OR_CN_NUMBER_RE = re.compile(r"[ivxlcdmIVXLCDM一二三四五六七八九十]{1,6}")
_JOURNAL_PAGE_RE = re.compile(r"\d{5,}-\d{1,4}")
_PUNCT_ONLY_RE = re.compile(r"[\W_]+", re.UNICODE)
_HEADING_RE = re.compile(
    r"(?:第[一二三四五六七八九十百千万\d]+[章节篇]|"
    r"\d+(?:\.\d+){0,4}\s+\S+|"
    r"[一二三四五六七八九十]+[、.．]\s*\S+)"
)
_CROSS_TYPE_LEAD_RE = re.compile(
    r"(?:"
    r"为|如下|如下[:：]?|描述为|计算公式为|表达式为|反应式为|可写为|可以写出|写出|"
    r"可表示为|表示为|可得|可得出|得出|定义为|定义如下|"
    r"如图所示|如下图|见图|示例如图|如表所示|如下表|见表"
    r")\s*[。:：，,]?$"
)
_MEDIA_REF_RE = re.compile(
    r"(?P<kind>图|表|公式|Fig(?:ure)?|Table|Formula|Eq(?:uation)?)"
    r"\.?\s*(?P<num>\d+(?:\s*[.\-－—–~～￣_]\s*\d+)*)",
    re.I,
)
_MEDIA_BLOCK_TYPES = {"image", "table", "formula"}
_MEDIA_KIND_BY_BLOCK_TYPE = {
    "image": "图",
    "table": "表",
    "formula": "公式",
}
_MEDIA_CONTEXT_BLOCK_WINDOW = 4
_MEDIA_CONTEXT_MAX_CHARS = 1200


def build_chunks_from_blocks(
    parsed: ParsedDocument,
    *,
    file_name: str = "",
    file_path: str | None = None,
    source_hash: str = "",
    document_date: date | None = None,
    category_path: str | None = None,
    commit_version: int = 1,
    chunk_size_tokens: int = 800,
    chunk_overlap_tokens: int = 120,
) -> list[IngestChunk]:
    """Convert all blocks in a ParsedDocument to IngestChunks.

    - Text blocks: split by token-based chunker (preserving paragraph boundaries).
    - Image blocks: single chunk = ``caption + alt_text + surrounding_context + vision_summary``.
    - Table blocks: single chunk (or row-split if very long) = ``caption + markdown_table + table_summary``.
    - Formula blocks: single chunk = ``latex + variable_explanation + surrounding_context``.
    """
    sd = parsed.source_document
    _file_name = file_name or sd.original_file_name
    _file_path = file_path or sd.object_storage_uri
    _source_hash = source_hash or sd.source_hash
    _commit = commit_version or sd.commit_version

    repeated_short_text = _short_text_repetition_counts(parsed.blocks)
    usable_blocks = [
        block for block in parsed.blocks
        if not (block.block_type == "text" and _is_noise_text_block(block, repeated_short_text))
    ]
    usable_blocks = _annotate_media_blocks(usable_blocks, source_doc_name=_file_name)

    chunks: list[IngestChunk] = []
    global_index = 0
    for segment in _mixed_structural_segments(
        usable_blocks,
        chunk_size_tokens=chunk_size_tokens,
    ):
        segment_chunks = _blocks_to_chunks(
            segment,
            sd.doc_id,
            _commit,
            global_index,
            file_name=_file_name,
            file_path=_file_path,
            source_hash=_source_hash,
            document_date=document_date,
            category_path=category_path,
            job_id=sd.job_id,
            kb_id=sd.kb_id,
            kb_code=sd.kb_code,
            schema_version=sd.schema_version,
            parser_version=sd.parser_version,
            chunk_size_tokens=chunk_size_tokens,
            chunk_overlap_tokens=chunk_overlap_tokens,
        )
        for chunk in segment_chunks:
            chunk.chunk_index = global_index
            chunk.chunk_id = stable_chunk_id(sd.doc_id, _commit, global_index, chunk.content_hash)
            chunks.append(chunk)
            global_index += 1

    quality_checked_chunks = _split_oversized_chunks(
        _add_section_context(
            _merge_tiny_chunks(
                _merge_short_text_chunks(chunks, min_tokens=_TEXT_SHORT_MERGE_MIN_TOKENS),
                min_chars=_MIN_CHUNK_CHARS,
            )
        ),
        doc_id=sd.doc_id,
        commit_version=_commit,
        chunk_size_tokens=chunk_size_tokens,
    )

    return _finalize_chunk_sequence(
        quality_checked_chunks,
        doc_id=sd.doc_id,
        commit_version=_commit,
    )


def build_chunk_quality_audit(
    chunks: list[IngestChunk],
    *,
    blocks: list[IngestBlock] | None = None,
    chunk_size_tokens: int = 800,
) -> dict[str, Any]:
    """Summarize chunk quality signals for ingestion metrics."""
    lengths = [len(chunk.content or "") for chunk in chunks]
    tokens = [int(chunk.token_count or estimate_tokens(chunk.content)) for chunk in chunks]
    type_counts: dict[str, int] = {}
    oversized_by_type: dict[str, int] = {}
    hard_oversized_by_type: dict[str, int] = {}
    strategy_counts: dict[str, int] = {}
    text_tokens: list[int] = []
    hard_limit = int(chunk_size_tokens * _OVERSIZED_CHUNK_RATIO)
    for chunk in chunks:
        block_type = chunk.block_type or "text"
        token_count = int(chunk.token_count or estimate_tokens(chunk.content))
        type_counts[block_type] = type_counts.get(block_type, 0) + 1
        if token_count > chunk_size_tokens:
            oversized_by_type[block_type] = oversized_by_type.get(block_type, 0) + 1
        if token_count > hard_limit:
            hard_oversized_by_type[block_type] = hard_oversized_by_type.get(block_type, 0) + 1
        if block_type == "text":
            text_tokens.append(token_count)
        strategy = str((chunk.extra or {}).get("merge_strategy") or "single")
        strategy_counts[strategy] = strategy_counts.get(strategy, 0) + 1

    known_text_block_ids: set[str] = set()
    if blocks is not None:
        known_text_block_ids = {
            str(block.block_id)
            for block in blocks
            if block.block_type == "text" and block.block_id
        }

    source_text_block_ids: set[str] = set()
    for chunk in chunks:
        ids = list(_chunk_source_block_ids(chunk))
        if isinstance(ids, list):
            if known_text_block_ids:
                source_text_block_ids.update(str(item) for item in ids if item in known_text_block_ids)
            elif chunk.block_type == "text":
                source_text_block_ids.update(str(item) for item in ids if item)

    text_block_count = 0
    if blocks is not None:
        text_block_count = sum(1 for block in blocks if block.block_type == "text")

    return {
        "chunk_count": len(chunks),
        "type_counts": type_counts,
        "strategy_counts": strategy_counts,
        "char_len_le_10": sum(1 for value in lengths if value <= 10),
        "char_len_le_30": sum(1 for value in lengths if value <= 30),
        "char_len_lt_50": sum(1 for value in lengths if value < _MIN_CHUNK_CHARS),
        "char_len_le_100": sum(1 for value in lengths if value <= 100),
        "token_lt_40": sum(1 for value in tokens if value < 40),
        "text_token_lt_40": sum(1 for value in text_tokens if value < 40),
        "token_gt_chunk_size": sum(1 for value in tokens if value > chunk_size_tokens),
        "token_gt_1_5x_chunk_size": sum(1 for value in tokens if value > hard_limit),
        "oversized_by_type": oversized_by_type,
        "hard_oversized_by_type": hard_oversized_by_type,
        "token_min": min(tokens) if tokens else 0,
        "token_avg": round(sum(tokens) / len(tokens), 2) if tokens else 0,
        "token_p50": _percentile(tokens, 0.5),
        "token_p90": _percentile(tokens, 0.9),
        "token_max": max(tokens) if tokens else 0,
        "text_block_count": text_block_count,
        "text_block_used_count": len(source_text_block_ids),
        "text_block_filtered_count": max(text_block_count - len(source_text_block_ids), 0)
        if blocks is not None else 0,
    }


def _percentile(values: list[int], ratio: float) -> int:
    if not values:
        return 0
    ordered = sorted(values)
    index = min(max(int(round((len(ordered) - 1) * ratio)), 0), len(ordered) - 1)
    return ordered[index]


def _finalize_chunk_sequence(
    chunks: list[IngestChunk],
    *,
    doc_id: str,
    commit_version: int,
) -> list[IngestChunk]:
    for index, chunk in enumerate(chunks):
        chunk.chunk_index = index
        chunk.content_hash = sha256_text(chunk.content)
        chunk.stable_chunk_fingerprint = stable_chunk_fingerprint(chunk.block_type, chunk.content)
        chunk.token_count = estimate_tokens(chunk.content)
        chunk.chunk_id = stable_chunk_id(doc_id, commit_version, index, chunk.content_hash)
    return chunks


def _add_section_context(chunks: list[IngestChunk]) -> list[IngestChunk]:
    for chunk in chunks:
        updated = _with_section_context(chunk.content, chunk.section_path)
        if updated == chunk.content:
            continue
        chunk.content = updated
        chunk.char_start = None
        chunk.char_end = None
        extra = dict(chunk.extra or {})
        extra["section_context_injected"] = True
        chunk.extra = extra
    return chunks


def _with_section_context(content: str, section_path: list[str] | None) -> str:
    content = (content or "").strip()
    sections = [str(item).strip() for item in (section_path or []) if str(item).strip()]
    if not content or not sections:
        return content

    normalized_content = _normalize_text(content)
    last_section = _normalize_text(sections[-1])
    if last_section and last_section in normalized_content:
        return content

    section_text = " > ".join(sections[-3:])
    prefix = f"章节: {section_text}"
    if normalized_content.startswith(_normalize_text(prefix)):
        return content
    return f"{prefix}\n\n{content}"


def _merge_short_text_chunks(
    chunks: list[IngestChunk],
    *,
    min_tokens: int,
) -> list[IngestChunk]:
    merged: list[IngestChunk] = []
    pending_short_text: list[IngestChunk] = []

    for chunk in chunks:
        if _should_merge_short_text_chunk(chunk, min_tokens=min_tokens):
            pending_short_text.append(chunk)
            continue

        if pending_short_text and chunk.block_type != "text":
            if _should_attach_short_text_to_non_text(pending_short_text, chunk):
                chunk = _merge_chunk_sequence([*pending_short_text, chunk], base_index=len(pending_short_text))
                pending_short_text = []
            else:
                _append_pending_short_text(merged, pending_short_text)
                pending_short_text = []
        elif pending_short_text:
            chunk = _merge_chunk_sequence([*pending_short_text, chunk], base_index=len(pending_short_text))
            pending_short_text = []

        merged.append(chunk)

    if pending_short_text:
        _append_pending_short_text(merged, pending_short_text)
    return merged


def _append_pending_short_text(merged: list[IngestChunk], pending: list[IngestChunk]) -> None:
    if not pending:
        return
    if len(pending) == 1:
        merged.append(pending[0])
        return
    merged.append(_merge_chunk_sequence(pending, base_index=0))


def _merge_tiny_chunks(
    chunks: list[IngestChunk],
    *,
    min_chars: int,
) -> list[IngestChunk]:
    """Fold tiny text chunks into a neighbor while preserving multimodal chunks."""
    if len(chunks) <= 1:
        return chunks

    merged: list[IngestChunk] = []
    pending_tiny_text: list[IngestChunk] = []

    for chunk in chunks:
        if _is_tiny_text_chunk(chunk, min_chars=min_chars):
            pending_tiny_text.append(chunk)
            continue

        if pending_tiny_text:
            if chunk.block_type != "text":
                _append_pending_short_text(merged, pending_tiny_text)
            else:
                chunk = _merge_chunk_sequence([*pending_tiny_text, chunk], base_index=len(pending_tiny_text))
            pending_tiny_text = []
        merged.append(chunk)

    if pending_tiny_text:
        if merged and merged[-1].block_type == "text":
            previous = merged.pop()
            merged.append(_merge_chunk_sequence([previous, *pending_tiny_text], base_index=0))
        else:
            merged.append(_merge_chunk_sequence(pending_tiny_text, base_index=0))
    return merged


def _is_tiny_text_chunk(chunk: IngestChunk, *, min_chars: int) -> bool:
    return chunk.block_type == "text" and len(_normalize_text(chunk.content)) < min_chars


def _should_merge_short_text_chunk(chunk: IngestChunk, *, min_tokens: int) -> bool:
    if chunk.block_type != "text":
        return False
    text = _normalize_text(chunk.content)
    if not text:
        return True
    return estimate_tokens(text) < min_tokens and len(text) <= _TEXT_SHORT_MERGE_MAX_CHARS


def _should_attach_short_text_to_non_text(pending: list[IngestChunk], target: IngestChunk) -> bool:
    if target.block_type != "formula":
        return False
    text = _normalize_text(" ".join(part.content or "" for part in pending))
    if not text:
        return False
    if len(text) <= 10:
        return True
    return bool(_CROSS_TYPE_LEAD_RE.search(text))


def _merge_chunk_sequence(parts: list[IngestChunk], *, base_index: int) -> IngestChunk:
    if len(parts) == 1:
        return parts[0]
    base = parts[base_index]
    content = "\n\n".join(_merge_content_part(part) for part in parts if _merge_content_part(part))
    base.content = content
    base.content_hash = sha256_text(content)
    base.stable_chunk_fingerprint = stable_chunk_fingerprint(base.block_type, content)
    base.token_count = estimate_tokens(content)
    base.char_start = None
    base.char_end = None
    base.bbox = None

    page_nums = [part.page_num for part in parts if isinstance(part.page_num, int)]
    unique_pages = list(dict.fromkeys(page_nums))
    base.page_num = unique_pages[0] if len(unique_pages) == 1 else None
    base.extra = _merge_chunk_extra(parts, base_index=base_index, base_block_type=base.block_type)
    return base


def _merge_content_part(chunk: IngestChunk) -> str:
    if chunk.block_type == "text":
        return _normalize_text(chunk.content)
    return (chunk.content or "").strip()


def _chunk_source_block_ids(chunk: IngestChunk) -> list[str]:
    ids = (chunk.extra or {}).get("source_block_ids")
    result: list[str] = []
    if isinstance(ids, list):
        result.extend(str(item) for item in ids if item)
    if chunk.block_id:
        result.append(str(chunk.block_id))
    return list(dict.fromkeys(result))


def _chunk_page_nums(chunk: IngestChunk) -> list[int]:
    result: list[int] = []
    pages = (chunk.extra or {}).get("page_nums")
    if isinstance(pages, list):
        result.extend(int(page) for page in pages if isinstance(page, int))
    if isinstance(chunk.page_num, int):
        result.append(chunk.page_num)
    return list(dict.fromkeys(result))


def _merge_chunk_extra(parts: list[IngestChunk], *, base_index: int, base_block_type: str) -> dict[str, Any]:
    source_block_ids: list[str] = []
    page_nums: list[int] = []
    source_block_types: list[str] = []
    short_merge_count = 0
    for part in parts:
        source_block_ids.extend(_chunk_source_block_ids(part))
        page_nums.extend(_chunk_page_nums(part))
        source_block_types.append(str(part.block_type or "text"))
        short_merge_count += int((part.extra or {}).get("short_merge_count") or 0)

    unique_block_ids = list(dict.fromkeys(source_block_ids))
    unique_page_nums = list(dict.fromkeys(page_nums))
    unique_block_types = list(dict.fromkeys(source_block_types))
    base_source_count = max(len(_chunk_source_block_ids(parts[base_index])), 1)
    merged = dict(parts[0].extra or {})
    for part in parts[1:]:
        for key, value in (part.extra or {}).items():
            if key not in merged:
                merged[key] = value
    if len(unique_block_types) == 1 and base_block_type == "text":
        merged["merge_strategy"] = "structure_semantic+short_merge"
    else:
        merged["merge_strategy"] = "structure_semantic+cross_type_short_merge"
        merged["primary_block_type"] = base_block_type
        merged["source_block_types"] = unique_block_types
    if unique_block_ids:
        merged["source_block_ids"] = unique_block_ids
        merged["source_block_count"] = len(unique_block_ids)
    if unique_page_nums:
        merged["page_nums"] = unique_page_nums
    merged["short_merge_count"] = short_merge_count + max(len(unique_block_ids) - base_source_count, len(parts) - 1, 0)
    return merged


def _normalize_text(text: str) -> str:
    return re.sub(r"\s+", " ", (text or "").strip())


def _short_text_repetition_counts(blocks: list[IngestBlock]) -> Counter[str]:
    values: list[str] = []
    for block in blocks:
        if block.block_type != "text":
            continue
        normalized = _normalize_text(block.content)
        if normalized and len(normalized) <= 80:
            values.append(normalized)
    return Counter(values)


def _is_noise_text_block(block: IngestBlock, repeated_short_text: Counter[str] | None = None) -> bool:
    text = _normalize_text(block.content)
    if not text:
        return True
    if _is_toc_text(block.content):
        return True
    if _JOURNAL_PAGE_RE.fullmatch(text):
        return True
    if _STANDALONE_NUMBER_RE.fullmatch(text) or _ROMAN_OR_CN_NUMBER_RE.fullmatch(text):
        return True
    if len(text) <= 2 and estimate_tokens(text) <= 2:
        return True
    if _PUNCT_ONLY_RE.fullmatch(text):
        return True
    if repeated_short_text and len(text) <= 80 and repeated_short_text.get(text, 0) >= 3:
        return True
    return False


def _is_toc_text(text: str) -> bool:
    normalized = _normalize_text(text)
    if not normalized:
        return False
    if _TOC_LEADER_RE.fullmatch(normalized) or _LOOSE_TOC_LEADER_RE.fullmatch(normalized):
        return True
    lines = [line.strip() for line in (text or "").splitlines() if line.strip()]
    if len(lines) >= 3:
        toc_lines = [
            line for line in lines
            if _TOC_LEADER_RE.fullmatch(_normalize_text(line))
            or _LOOSE_TOC_LEADER_RE.fullmatch(_normalize_text(line))
        ]
        if len(toc_lines) >= 3 and len(toc_lines) / max(len(lines), 1) >= 0.35:
            return True
    leader_chars = sum(1 for ch in normalized if ch in ".·•．。")
    if len(normalized) >= 200 and leader_chars / max(len(normalized), 1) >= 0.04:
        has_section_number = bool(re.search(r"\d+(?:\.\d+){0,4}\s+\S+", normalized))
        has_page_leader = bool(re.search(r"[\.·•．。]{2,}[\s\.·•．。]*\d{1,4}(?:\s|$)", normalized))
        return has_section_number and has_page_leader
    return False


def _looks_like_heading(block: IngestBlock) -> bool:
    text = _normalize_text(block.content)
    if not text or len(text) > 80:
        return False
    return bool(_HEADING_RE.fullmatch(text))


def _same_section(left: IngestBlock, right: IngestBlock) -> bool:
    return list(left.section_path or []) == list(right.section_path or [])


def _segment_page_nums(blocks: list[IngestBlock]) -> list[int]:
    seen: list[int] = []
    for block in blocks:
        if isinstance(block.page_num, int) and block.page_num not in seen:
            seen.append(block.page_num)
    return seen


def _text_structural_segments(
    blocks: list[IngestBlock],
    *,
    chunk_size_tokens: int,
) -> list[list[IngestBlock]]:
    segments: list[list[IngestBlock]] = []
    current: list[IngestBlock] = []
    current_tokens = 0
    soft_limit = max(int(chunk_size_tokens * 0.9), 120)

    def flush() -> None:
        nonlocal current, current_tokens
        if current:
            segments.append(current)
        current = []
        current_tokens = 0

    for block in blocks:
        block_tokens = estimate_tokens(block.content)
        current_is_short_heading = (
            len(current) == 1
            and _looks_like_heading(current[0])
            and current_tokens < max(30, chunk_size_tokens // 10)
        )
        starts_new_section = bool(current) and (
            not _same_section(current[-1], block) or _looks_like_heading(block)
        )
        crosses_soft_limit = bool(current) and current_tokens + block_tokens > soft_limit
        if (starts_new_section and not current_is_short_heading) or crosses_soft_limit:
            flush()
        current.append(block)
        current_tokens += block_tokens

    flush()
    return segments


def _mixed_structural_segments(
    blocks: list[IngestBlock],
    *,
    chunk_size_tokens: int,
) -> list[list[IngestBlock]]:
    """Group adjacent blocks into section-aware semantic windows.

    Mixed text/formula/image/table windows use a lower token budget than
    text-only chunks so derivations stay coherent without becoming too broad.
    """
    segments: list[list[IngestBlock]] = []
    current: list[IngestBlock] = []
    current_tokens = 0
    text_soft_limit = max(int(chunk_size_tokens * 0.9), 120)
    mixed_soft_limit = max(int(chunk_size_tokens * _MIXED_SEGMENT_SOFT_RATIO), 180)

    def current_limit(next_block: IngestBlock | None = None) -> int:
        has_non_text = any(block.block_type != "text" for block in current)
        if next_block is not None and next_block.block_type != "text":
            has_non_text = True
        return mixed_soft_limit if has_non_text else text_soft_limit

    def flush() -> None:
        nonlocal current, current_tokens
        if current:
            segments.append(current)
        current = []
        current_tokens = 0

    for block in blocks:
        block_tokens = estimate_tokens(_block_search_content(block))
        current_non_text_types = {
            item.block_type for item in current if item.block_type != "text"
        }
        changes_non_text_type = (
            bool(current)
            and block.block_type != "text"
            and bool(current_non_text_types)
            and block.block_type not in current_non_text_types
        )
        current_is_short_heading = (
            len(current) == 1
            and current[0].block_type == "text"
            and _looks_like_heading(current[0])
            and current_tokens < max(30, chunk_size_tokens // 10)
        )
        starts_new_section = bool(current) and (
            not _same_section(current[-1], block)
            or (block.block_type == "text" and _looks_like_heading(block))
        )
        crosses_soft_limit = (
            bool(current)
            and current_tokens + block_tokens > current_limit(block)
        )
        if (
            changes_non_text_type
            or (starts_new_section and not current_is_short_heading)
            or crosses_soft_limit
        ):
            flush()
        current.append(block)
        current_tokens += block_tokens

    flush()
    return segments


def _blocks_to_chunks(
    blocks: list[IngestBlock],
    doc_id: str,
    commit_version: int,
    base_index: int,
    *,
    file_name: str,
    file_path: str | None,
    source_hash: str,
    document_date: date | None,
    category_path: str | None,
    job_id: str | None,
    kb_id: str | None,
    kb_code: str | None,
    schema_version: str,
    parser_version: str,
    chunk_size_tokens: int,
    chunk_overlap_tokens: int,
) -> list[IngestChunk]:
    if not blocks:
        return []
    if all(block.block_type == "text" for block in blocks):
        return _text_blocks_to_chunks(
            blocks,
            doc_id,
            commit_version,
            base_index,
            file_name=file_name,
            file_path=file_path,
            source_hash=source_hash,
            document_date=document_date,
            category_path=category_path,
            job_id=job_id,
            kb_id=kb_id,
            kb_code=kb_code,
            schema_version=schema_version,
            parser_version=parser_version,
            chunk_size_tokens=chunk_size_tokens,
            chunk_overlap_tokens=chunk_overlap_tokens,
        )
    if len(blocks) == 1 and blocks[0].block_type != "text":
        return _multimodal_block_to_chunks(
            blocks[0],
            doc_id,
            commit_version,
            base_index,
            file_name=file_name,
            file_path=file_path,
            source_hash=source_hash,
            document_date=document_date,
            category_path=category_path,
            job_id=job_id,
            kb_id=kb_id,
            kb_code=kb_code,
            schema_version=schema_version,
            parser_version=parser_version,
            chunk_size_tokens=chunk_size_tokens,
        )
    if any(block.block_type in {"image", "table"} for block in blocks):
        return _mixed_blocks_to_independent_chunks(
            blocks,
            doc_id,
            commit_version,
            base_index,
            file_name=file_name,
            file_path=file_path,
            source_hash=source_hash,
            document_date=document_date,
            category_path=category_path,
            job_id=job_id,
            kb_id=kb_id,
            kb_code=kb_code,
            schema_version=schema_version,
            parser_version=parser_version,
            chunk_size_tokens=chunk_size_tokens,
            chunk_overlap_tokens=chunk_overlap_tokens,
        )

    mixed = _mixed_blocks_to_chunk(
        blocks,
        doc_id,
        commit_version,
        base_index,
        file_name=file_name,
        file_path=file_path,
        source_hash=source_hash,
        document_date=document_date,
        category_path=category_path,
        job_id=job_id,
        kb_id=kb_id,
        kb_code=kb_code,
        schema_version=schema_version,
        parser_version=parser_version,
    )
    return _split_oversized_chunk(
        mixed,
        doc_id=doc_id,
        commit_version=commit_version,
        base_index=base_index,
        chunk_size_tokens=chunk_size_tokens,
    )


def _mixed_blocks_to_independent_chunks(
    blocks: list[IngestBlock],
    doc_id: str,
    commit_version: int,
    base_index: int,
    *,
    file_name: str,
    file_path: str | None,
    source_hash: str,
    document_date: date | None,
    category_path: str | None,
    job_id: str | None,
    kb_id: str | None,
    kb_code: str | None,
    schema_version: str,
    parser_version: str,
    chunk_size_tokens: int,
    chunk_overlap_tokens: int,
) -> list[IngestChunk]:
    result: list[IngestChunk] = []
    text_run: list[IngestBlock] = []
    pending_media_refs: list[tuple[str, str]] = []

    def next_index() -> int:
        return base_index + len(result)

    def flush_text_run() -> None:
        nonlocal text_run, pending_media_refs
        if not text_run:
            return
        result.extend(
            _text_blocks_to_chunks(
                text_run,
                doc_id,
                commit_version,
                next_index(),
                file_name=file_name,
                file_path=file_path,
                source_hash=source_hash,
                document_date=document_date,
                category_path=category_path,
                job_id=job_id,
                kb_id=kb_id,
                kb_code=kb_code,
                schema_version=schema_version,
                parser_version=parser_version,
                chunk_size_tokens=chunk_size_tokens,
                chunk_overlap_tokens=chunk_overlap_tokens,
            )
        )
        pending_media_refs = _media_refs_from_text(
            "\n".join(block.content or "" for block in text_run[-3:])
        )
        text_run = []

    for block in blocks:
        if block.block_type == "text":
            text_run.append(block)
            continue
        flush_text_run()
        block = _with_pending_media_ref_caption(block, pending_media_refs)
        result.extend(
            _multimodal_block_to_chunks(
                block,
                doc_id,
                commit_version,
                next_index(),
                file_name=file_name,
                file_path=file_path,
                source_hash=source_hash,
                document_date=document_date,
                category_path=category_path,
                job_id=job_id,
                kb_id=kb_id,
                kb_code=kb_code,
                schema_version=schema_version,
                parser_version=parser_version,
                chunk_size_tokens=chunk_size_tokens,
            )
        )

    flush_text_run()
    return result


def _with_pending_media_ref_caption(
    block: IngestBlock,
    pending_media_refs: list[tuple[str, str]],
) -> IngestBlock:
    if block.block_type not in {"image", "table"}:
        return block
    meta = dict(block.metadata or {})
    if str(meta.get("caption") or "").strip():
        return block
    expected_kind = "图" if block.block_type == "image" else "表"
    for idx, (kind, ref) in enumerate(list(pending_media_refs)):
        if kind != expected_kind:
            continue
        pending_media_refs.pop(idx)
        meta["caption"] = ref
        meta["caption_inferred_from_context"] = True
        return replace(block, metadata=meta)
    return block


def _media_refs_from_text(text: str) -> list[tuple[str, str]]:
    refs: list[tuple[str, str]] = []
    for candidate in _media_candidates_from_text(text):
        kind = candidate["kind"]
        ref = candidate["ref"]
        if (kind, ref) not in refs:
            refs.append((kind, ref))
    return refs[-8:]


def _annotate_media_blocks(blocks: list[IngestBlock], *, source_doc_name: str) -> list[IngestBlock]:
    if not blocks:
        return blocks
    annotated: list[IngestBlock] = []
    for index, block in enumerate(blocks):
        if block.block_type not in _MEDIA_BLOCK_TYPES:
            annotated.append(block)
            continue
        before = _nearby_text(blocks, index, direction=-1)
        after = _nearby_text(blocks, index, direction=1)
        meta = _media_metadata(
            block,
            before_text=before,
            after_text=after,
            source_doc_name=source_doc_name,
        )
        annotated.append(replace(block, metadata=meta))
    return annotated


def _media_metadata(
    block: IngestBlock,
    *,
    before_text: str,
    after_text: str,
    source_doc_name: str,
) -> dict[str, Any]:
    meta = dict(block.metadata or {})
    media_type = block.block_type
    expected_kind = _MEDIA_KIND_BY_BLOCK_TYPE.get(media_type, "")
    caption = _first_text(meta, "caption", "title", "fig_caption", "table_caption")
    candidates = _media_candidates_for_block(
        block,
        expected_kind=expected_kind,
        caption=caption,
        before_text=before_text,
        after_text=after_text,
    )
    best = candidates[0] if candidates else {}

    media_ref = str(meta.get("media_ref") or best.get("ref") or "").strip()
    media_title = str(meta.get("media_title") or best.get("title") or "").strip()
    media_caption = str(meta.get("media_caption") or best.get("caption") or caption or "").strip()

    if media_caption and not caption:
        meta["caption"] = media_caption
        meta["caption_inferred_from_context"] = True
    if media_ref:
        meta["media_ref"] = media_ref
    if media_title:
        meta["media_title"] = media_title
    if media_caption:
        meta["media_caption"] = media_caption
    aliases = _media_ref_aliases(media_ref)
    if aliases:
        meta["media_ref_aliases"] = aliases
    meta["media_type"] = media_type
    if isinstance(block.page_num, int):
        meta["page_num"] = block.page_num
    if source_doc_name:
        meta["source_doc_name"] = source_doc_name
    if before_text:
        meta["nearby_text_before"] = _truncate_context_text(before_text, max_chars=_MEDIA_CONTEXT_MAX_CHARS)
    if after_text:
        meta["nearby_text_after"] = _truncate_context_text(after_text, max_chars=_MEDIA_CONTEXT_MAX_CHARS)

    ocr_text = _first_text(meta, "ocr_text", "image_ocr", "ocr", "detected_text")
    if not ocr_text and media_type == "image" and block.content:
        ocr_text = block.content
    if ocr_text:
        meta["ocr_text"] = _truncate_context_text(ocr_text, max_chars=_MEDIA_CONTEXT_MAX_CHARS)

    visual_summary = _first_text(
        meta,
        "visual_summary",
        "vision_summary",
        "image_summary",
        "table_summary",
        "formula_summary",
        "summary",
    )
    if visual_summary:
        meta["visual_summary"] = _truncate_context_text(
            visual_summary,
            max_chars=_MEDIA_CONTEXT_MAX_CHARS,
        )
    return meta


def _media_candidates_for_block(
    block: IngestBlock,
    *,
    expected_kind: str,
    caption: str,
    before_text: str,
    after_text: str,
) -> list[dict[str, Any]]:
    weighted_sources = [
        (caption, 100),
        (block.content or "", 90),
        (before_text, 70),
        (after_text, 68),
    ]
    candidates: list[dict[str, Any]] = []
    for text, score in weighted_sources:
        for candidate in _media_candidates_from_text(text, expected_kind=expected_kind):
            item = dict(candidate)
            item["score"] = score
            candidates.append(item)
    candidates.sort(key=lambda item: (-int(item.get("score") or 0), len(str(item.get("caption") or ""))))
    deduped: list[dict[str, Any]] = []
    seen: set[tuple[str, str]] = set()
    for item in candidates:
        key = (str(item.get("kind") or ""), str(item.get("ref") or ""))
        if key in seen:
            continue
        seen.add(key)
        deduped.append(item)
    return deduped


def _media_candidates_from_text(text: str, *, expected_kind: str = "") -> list[dict[str, str]]:
    value = text or ""
    if not value:
        return []
    candidates: list[dict[str, str]] = []
    for match in _MEDIA_REF_RE.finditer(value):
        kind = normalize_media_kind(match.group("kind"))
        if expected_kind and kind != expected_kind:
            continue
        number = normalize_media_number(match.group("num"))
        if not kind or not number:
            continue
        ref = f"{kind}{number}"
        line = _line_around(value, match.start(), match.end())
        caption = _normalize_text(line)
        title = _normalize_text(line[match.end() - _line_start_offset(value, match.start()):])
        title = re.sub(r"^[\s:：,，.．、\-—–]+", "", title).strip()
        if not title:
            title = caption
        candidates.append({
            "kind": kind,
            "ref": ref,
            "title": title[:240],
            "caption": caption[:320],
        })
    return candidates


def _nearby_text(blocks: list[IngestBlock], index: int, *, direction: int) -> str:
    if direction not in {-1, 1}:
        return ""
    origin = blocks[index]
    selected: list[str] = []
    steps = range(index - 1, -1, -1) if direction < 0 else range(index + 1, len(blocks))
    for pos in steps:
        block = blocks[pos]
        if len(selected) >= _MEDIA_CONTEXT_BLOCK_WINDOW:
            break
        if block.block_type != "text":
            if selected:
                break
            continue
        text = _normalize_text(block.content)
        if not text:
            continue
        same_page = (
            isinstance(origin.page_num, int)
            and isinstance(block.page_num, int)
            and origin.page_num == block.page_num
        )
        if not same_page and selected:
            break
        selected.append(text)
    if direction < 0:
        selected.reverse()
    return "\n".join(selected)


def _first_text(meta: dict[str, Any], *keys: str) -> str:
    for key in keys:
        value = meta.get(key)
        if value is None:
            continue
        text = _normalize_text(str(value))
        if text:
            return text
    return ""


def _media_ref_aliases(ref: str) -> list[str]:
    if not ref:
        return []
    aliases = {ref}
    match = re.match(r"^(图|表|公式)(.+)$", ref)
    if match:
        kind, number = match.groups()
        dotted = number.replace("-", ".")
        aliases.add(f"{kind} {number}")
        aliases.add(f"{kind}{dotted}")
        if kind == "图":
            aliases.update({f"Fig. {number}", f"Figure {number}", f"Fig. {dotted}"})
        elif kind == "表":
            aliases.update({f"Table {number}", f"Table {dotted}"})
        elif kind == "公式":
            aliases.update({f"Eq. {number}", f"Equation {number}"})
    return sorted(alias for alias in aliases if alias)


def _line_start_offset(text: str, pos: int) -> int:
    return max(text.rfind("\n", 0, pos) + 1, 0)


def _line_around(text: str, start: int, end: int) -> str:
    line_start = _line_start_offset(text, start)
    line_end = text.find("\n", end)
    if line_end < 0:
        line_end = len(text)
    return text[line_start:line_end]


def _mixed_blocks_to_chunk(
    blocks: list[IngestBlock],
    doc_id: str,
    commit_version: int,
    chunk_index: int,
    *,
    file_name: str,
    file_path: str | None,
    source_hash: str,
    document_date: date | None,
    category_path: str | None,
    job_id: str | None,
    kb_id: str | None,
    kb_code: str | None,
    schema_version: str,
    parser_version: str,
) -> IngestChunk:
    part_chunks: list[IngestChunk] = []
    for offset, block in enumerate(blocks):
        content = _block_search_content(block)
        if not content:
            continue
        part_chunks.append(
            _make_chunk(
                content,
                block,
                doc_id,
                commit_version,
                chunk_index + offset,
                file_name=file_name,
                file_path=file_path,
                source_hash=source_hash,
                document_date=document_date,
                category_path=category_path,
                job_id=job_id,
                kb_id=kb_id,
                kb_code=kb_code,
                schema_version=schema_version,
                parser_version=parser_version,
            )
        )
    if not part_chunks:
        return _make_chunk(
            "",
            blocks[0],
            doc_id,
            commit_version,
            chunk_index,
            file_name=file_name,
            file_path=file_path,
            source_hash=source_hash,
            document_date=document_date,
            category_path=category_path,
            job_id=job_id,
            kb_id=kb_id,
            kb_code=kb_code,
            schema_version=schema_version,
            parser_version=parser_version,
        )

    primary_index = _primary_mixed_chunk_index(part_chunks)
    merged = _merge_chunk_sequence(part_chunks, base_index=primary_index)
    if not _is_short_cross_type_merge_window(blocks):
        merged.extra["merge_strategy"] = "structure_semantic+cross_type_semantic_window"
        merged.extra["semantic_window_count"] = max(len(part_chunks) - 1, 0)
    return merged


def _primary_mixed_chunk_index(chunks: list[IngestChunk]) -> int:
    for index, chunk in enumerate(chunks):
        if chunk.block_type != "text":
            return index
    return 0


def _is_short_cross_type_merge_window(blocks: list[IngestBlock]) -> bool:
    non_text = [block for block in blocks if block.block_type != "text"]
    text_blocks = [block for block in blocks if block.block_type == "text"]
    if len(non_text) != 1 or not text_blocks:
        return False
    text = _normalize_text(" ".join(block.content or "" for block in text_blocks))
    if not text:
        return False
    if len(text) <= 10:
        return True
    return (
        estimate_tokens(text) < _TEXT_SHORT_MERGE_MIN_TOKENS
        and len(text) <= _TEXT_SHORT_MERGE_MAX_CHARS
    )


def _block_search_content(block: IngestBlock) -> str:
    if block.block_type == "text":
        return _normalize_text(block.content)
    meta = block.metadata or {}
    if block.block_type == "image":
        return _build_image_chunk_content(block, meta)
    if block.block_type == "table":
        return _build_table_chunk_content(block, meta)
    if block.block_type == "formula":
        return _build_formula_chunk_content(block, meta)
    return (block.content or "").strip()


def _text_blocks_to_chunks(
    blocks: list[IngestBlock],
    doc_id: str,
    commit_version: int,
    base_index: int,
    *,
    file_name: str,
    file_path: str | None,
    source_hash: str,
    document_date: date | None,
    category_path: str | None,
    job_id: str | None,
    kb_id: str | None,
    kb_code: str | None,
    schema_version: str,
    parser_version: str,
    chunk_size_tokens: int,
    chunk_overlap_tokens: int,
) -> list[IngestChunk]:
    chunks: list[IngestChunk] = []
    local_index = 0
    for segment in _text_structural_segments(blocks, chunk_size_tokens=chunk_size_tokens):
        content = "\n\n".join(_normalize_text(block.content) for block in segment if _normalize_text(block.content))
        if not content:
            continue
        first = segment[0]
        page_nums = _segment_page_nums(segment)
        text_chunks = _text_build_chunks(
            content,
            doc_id=doc_id,
            file_name=file_name,
            file_path=file_path,
            source_hash=source_hash,
            document_date=document_date,
            category_path=category_path,
            commit_version=commit_version,
            chunk_size_tokens=chunk_size_tokens,
            chunk_overlap_tokens=chunk_overlap_tokens,
        )
        for tc in text_chunks:
            chunk_index = base_index + local_index
            tc.chunk_index = chunk_index
            tc.chunk_id = stable_chunk_id(doc_id, commit_version, chunk_index, tc.content_hash)
            tc.block_id = first.block_id
            tc.block_type = "text"
            tc.job_id = job_id
            tc.kb_id = kb_id
            tc.kb_code = kb_code
            tc.page_num = page_nums[0] if len(page_nums) == 1 else None
            tc.bbox = first.bbox if len(segment) == 1 else None
            tc.section_path = list(first.section_path)
            tc.schema_version = schema_version
            tc.parser_version = parser_version
            tc.extra = {
                **(tc.extra or {}),
                "merge_strategy": "structure_semantic",
                "source_block_ids": [block.block_id for block in segment],
                "source_block_count": len(segment),
                "page_nums": page_nums,
            }
            chunks.append(tc)
            local_index += 1
    return chunks


def _multimodal_block_to_chunks(
    block: IngestBlock,
    doc_id: str,
    commit_version: int,
    base_index: int,
    *,
    file_name: str,
    file_path: str | None,
    source_hash: str,
    document_date: date | None,
    category_path: str | None,
    job_id: str | None,
    kb_id: str | None,
    kb_code: str | None,
    schema_version: str,
    parser_version: str,
    chunk_size_tokens: int = 800,
) -> list[IngestChunk]:
    """Convert a non-text block into one or more IngestChunks."""
    block_type = block.block_type
    meta = block.metadata or {}

    if block_type == "image":
        content = _build_image_chunk_content(block, meta)
    elif block_type == "table":
        content = _build_table_chunk_content(block, meta)
    elif block_type == "formula":
        content = _build_formula_chunk_content(block, meta)
    else:
        content = block.content

    result_chunks: list[IngestChunk] = []
    tokens = estimate_tokens(content)

    if tokens <= chunk_size_tokens * _OVERSIZED_CHUNK_RATIO:
        result_chunks.append(_make_chunk(
            content, block, doc_id, commit_version, base_index,
            file_name=file_name, file_path=file_path,
            source_hash=source_hash, document_date=document_date,
            category_path=category_path, job_id=job_id,
            kb_id=kb_id, kb_code=kb_code,
            schema_version=schema_version, parser_version=parser_version,
        ))
    else:
        parts = _split_content_by_char_budget(content, chunk_size_tokens=chunk_size_tokens)
        for idx, part in enumerate(parts):
            chunk = _make_chunk(
                part, block, doc_id, commit_version, base_index + idx,
                file_name=file_name, file_path=file_path,
                source_hash=source_hash, document_date=document_date,
                category_path=category_path, job_id=job_id,
                kb_id=kb_id, kb_code=kb_code,
                schema_version=schema_version, parser_version=parser_version,
            )
            if len(parts) > 1:
                chunk.extra = {
                    **dict(chunk.extra or {}),
                    "oversized_split": True,
                    "oversized_split_part": idx + 1,
                    "oversized_split_total": len(parts),
                }
            result_chunks.append(chunk)

    return result_chunks


def _split_oversized_chunk(
    chunk: IngestChunk,
    *,
    doc_id: str,
    commit_version: int,
    base_index: int,
    chunk_size_tokens: int,
) -> list[IngestChunk]:
    hard_limit = int(chunk_size_tokens * _OVERSIZED_CHUNK_RATIO)
    if estimate_tokens(chunk.content) <= hard_limit:
        return [chunk]

    parts = _split_content_by_char_budget(chunk.content, chunk_size_tokens=chunk_size_tokens)
    if len(parts) <= 1:
        return [chunk]

    result: list[IngestChunk] = []
    total = len(parts)
    for offset, content in enumerate(parts):
        extra = dict(chunk.extra or {})
        extra.update({
            "oversized_split": True,
            "oversized_split_part": offset + 1,
            "oversized_split_total": total,
        })
        content_hash = sha256_text(content)
        result.append(replace(
            chunk,
            chunk_id=stable_chunk_id(doc_id, commit_version, base_index + offset, content_hash),
            chunk_index=base_index + offset,
            content=content,
            content_hash=content_hash,
            stable_chunk_fingerprint=stable_chunk_fingerprint(chunk.block_type, content),
            token_count=estimate_tokens(content),
            char_start=None,
            char_end=None,
            extra=extra,
        ))
    return result


def _split_oversized_chunks(
    chunks: list[IngestChunk],
    *,
    doc_id: str,
    commit_version: int,
    chunk_size_tokens: int,
) -> list[IngestChunk]:
    result: list[IngestChunk] = []
    for chunk in chunks:
        result.extend(
            _split_oversized_chunk(
                chunk,
                doc_id=doc_id,
                commit_version=commit_version,
                base_index=len(result),
                chunk_size_tokens=chunk_size_tokens,
            )
        )
    return result


def _split_content_by_char_budget(content: str, *, chunk_size_tokens: int) -> list[str]:
    target_chars = max(200, int(int(chunk_size_tokens) * 1.8))
    hard_limit = int(int(chunk_size_tokens) * _OVERSIZED_CHUNK_RATIO)
    normalized = re.sub(r"(</tr>)", "\\1\n", content or "", flags=re.I)
    lines = normalized.split("\n")
    parts: list[str] = []
    buf: list[str] = []
    buf_len = 0
    buf_tokens = 0

    def flush() -> None:
        nonlocal buf, buf_len, buf_tokens
        body = "\n".join(item for item in buf if item).strip()
        if body:
            parts.append(body)
        buf = []
        buf_len = 0
        buf_tokens = 0

    for raw_line in lines:
        line_segments = _split_line_by_token_budget(
            raw_line,
            target_chars=target_chars,
            hard_limit=hard_limit,
        )
        for line in line_segments:
            line_len = len(line) + 1
            line_tokens = estimate_tokens(line)
            if buf and (buf_len + line_len > target_chars or buf_tokens + line_tokens > hard_limit):
                flush()
            if line.strip():
                buf.append(line)
                buf_len += line_len
                buf_tokens += line_tokens
    flush()
    return parts


def _split_line_by_token_budget(
    raw_line: str,
    *,
    target_chars: int,
    hard_limit: int,
) -> list[str]:
    if not raw_line:
        return [raw_line]

    segments: list[str] = []
    pos = 0
    while pos < len(raw_line):
        desired_end = min(len(raw_line), pos + target_chars)
        end = _safe_line_split_end(raw_line, pos, desired_end)
        segment = raw_line[pos:end]
        tokens = estimate_tokens(segment)
        if tokens > hard_limit and len(segment) > 1:
            safe_chars = max(120, int(len(segment) * hard_limit / max(tokens, 1) * 0.9))
            end = _safe_line_split_end(raw_line, pos, min(len(raw_line), pos + min(safe_chars, target_chars)))
            segment = raw_line[pos:end]
        segments.append(segment)
        pos = max(pos + len(segment), pos + 1)
    return segments


def _safe_line_split_end(raw_line: str, start: int, desired_end: int) -> int:
    desired_end = min(max(desired_end, start + 1), len(raw_line))
    if desired_end >= len(raw_line):
        return len(raw_line)
    min_end = min(len(raw_line), start + max(80, int((desired_end - start) * 0.55)))
    window = raw_line[min_end:desired_end]
    best = -1
    for match in re.finditer(r"[。！？；.!?;]\s*|\s+", window):
        best = match.end()
    if best > 0:
        return min_end + best
    return desired_end


def _build_image_chunk_content(block: IngestBlock, meta: dict) -> str:
    """§8.2: image chunk = caption + alt_text + surrounding_context + vision_summary"""
    parts: list[str] = []
    parts.extend(_media_header_parts(meta))
    caption = meta.get("caption", "")
    alt_text = meta.get("alt_text", "")
    vision_summary = meta.get("visual_summary") or meta.get("vision_summary", "")
    ocr_text = meta.get("ocr_text", "")
    surrounding = _truncate_context_text(meta.get("surrounding_context", ""))
    nearby_before = _truncate_context_text(meta.get("nearby_text_before", ""))
    nearby_after = _truncate_context_text(meta.get("nearby_text_after", ""))

    if caption:
        parts.append(f"图片标题: {caption}")
    if alt_text and alt_text != caption:
        parts.append(f"替代文本: {alt_text}")
    if ocr_text:
        parts.append(f"图片OCR: {ocr_text}")
    if surrounding:
        parts.append(f"上下文: {surrounding}")
    if nearby_before:
        parts.append(f"前文: {nearby_before}")
    if nearby_after:
        parts.append(f"后文: {nearby_after}")
    if vision_summary:
        parts.append(f"图片描述: {vision_summary}")

    if not parts:
        parts.append(block.content or f"[图片] {block.original_file_name}")
    return "\n".join(parts)


def _build_table_chunk_content(block: IngestBlock, meta: dict) -> str:
    """§8.2: table chunk = caption + markdown_table + table_summary + units"""
    parts: list[str] = []
    parts.extend(_media_header_parts(meta))
    caption = meta.get("caption", "")
    table_summary = meta.get("visual_summary") or meta.get("table_summary", "")
    units = meta.get("units", "")
    nearby_before = _truncate_context_text(meta.get("nearby_text_before", ""))
    nearby_after = _truncate_context_text(meta.get("nearby_text_after", ""))

    if caption:
        parts.append(f"表格标题: {caption}")
    parts.append(block.content)
    if table_summary:
        parts.append(f"表格摘要: {table_summary}")
    if units:
        parts.append(f"单位信息: {units}")
    if nearby_before:
        parts.append(f"前文: {nearby_before}")
    if nearby_after:
        parts.append(f"后文: {nearby_after}")
    return "\n\n".join(parts)


def _build_formula_chunk_content(block: IngestBlock, meta: dict) -> str:
    """§8.2: formula chunk = latex + variable_explanation + surrounding_context"""
    parts: list[str] = []
    parts.extend(_media_header_parts(meta))
    latex = meta.get("latex", block.content)
    variable_explanation = meta.get("variable_explanation", "")
    surrounding = _truncate_context_text(meta.get("surrounding_context", ""))
    nearby_before = _truncate_context_text(meta.get("nearby_text_before", ""))
    nearby_after = _truncate_context_text(meta.get("nearby_text_after", ""))

    parts.append(f"公式: {latex}")
    if variable_explanation:
        parts.append(f"变量说明: {variable_explanation}")
    if surrounding:
        parts.append(f"上下文: {surrounding}")
    if nearby_before:
        parts.append(f"前文: {nearby_before}")
    if nearby_after:
        parts.append(f"后文: {nearby_after}")
    return "\n".join(parts)


def _media_header_parts(meta: dict[str, Any]) -> list[str]:
    parts: list[str] = []
    media_type = str(meta.get("media_type") or "").strip()
    media_ref = str(meta.get("media_ref") or "").strip()
    media_title = str(meta.get("media_title") or "").strip()
    aliases = meta.get("media_ref_aliases") or []
    source_doc_name = str(meta.get("source_doc_name") or "").strip()
    page_num = meta.get("page_num")
    if media_type:
        parts.append(f"媒体类型: {media_type}")
    if media_ref:
        parts.append(f"媒体编号: {media_ref}")
    if aliases:
        parts.append("媒体编号别名: " + " / ".join(str(item) for item in aliases if item))
    if media_title:
        parts.append(f"媒体标题: {media_title}")
    if isinstance(page_num, int):
        parts.append(f"页码: {page_num}")
    if source_doc_name:
        parts.append(f"来源文档: {source_doc_name}")
    return parts


def _truncate_context_text(text: str, *, max_chars: int = _MULTIMODAL_CONTEXT_MAX_CHARS) -> str:
    normalized = _normalize_text(text)
    if len(normalized) <= max_chars:
        return normalized
    return normalized[:max_chars].rstrip() + "..."


def _make_chunk(
    content: str,
    block: IngestBlock,
    doc_id: str,
    commit_version: int,
    chunk_index: int,
    *,
    file_name: str,
    file_path: str | None,
    source_hash: str,
    document_date: date | None,
    category_path: str | None,
    job_id: str | None,
    kb_id: str | None,
    kb_code: str | None,
    schema_version: str,
    parser_version: str,
) -> IngestChunk:
    content_hash = sha256_text(content)
    fingerprint = stable_chunk_fingerprint(block.block_type, content)
    return IngestChunk(
        chunk_id=stable_chunk_id(doc_id, commit_version, chunk_index, content_hash),
        doc_id=doc_id,
        chunk_index=chunk_index,
        content=content,
        content_hash=content_hash,
        stable_chunk_fingerprint=fingerprint,
        token_count=estimate_tokens(content),
        block_type=block.block_type,
        file_name=file_name,
        file_path=file_path,
        source_hash=source_hash,
        document_date=document_date,
        category_path=category_path,
        commit_version=commit_version,
        block_id=block.block_id,
        job_id=job_id,
        kb_id=kb_id,
        kb_code=kb_code,
        page_num=block.page_num,
        bbox=block.bbox,
        section_path=list(block.section_path),
        schema_version=schema_version,
        parser_version=parser_version,
        extra=dict(block.metadata) if block.metadata else {},
    )
