"""Text chunking for phase-1 self ingestion."""

from __future__ import annotations

import re

from backend.rag.ingestion.ids import (
    sha256_text,
    stable_chunk_fingerprint,
    stable_chunk_id,
)
from backend.rag.ingestion.models import IngestChunk


_TOKEN_RE = re.compile(r"[\u4e00-\u9fff]|[A-Za-z0-9_]+")
_SPLIT_BOUNDARY_RE = re.compile(r"[\n。！？；.!?;]\s*|\s+")


def estimate_tokens(text: str) -> int:
    tokens = _TOKEN_RE.findall(text or "")
    if tokens:
        return len(tokens)
    return max(1, len(text or "") // 4)


def _safe_split_end(text: str, start: int, desired_end: int, hard_end: int) -> int:
    """Prefer paragraph/sentence/word boundaries while keeping forward progress."""

    desired_end = min(max(desired_end, start + 1), hard_end)
    if desired_end >= hard_end:
        return hard_end

    min_end = min(hard_end, start + max(120, int((desired_end - start) * 0.55)))
    window = text[min_end:desired_end]
    best = -1
    for match in _SPLIT_BOUNDARY_RE.finditer(window):
        best = match.end()
    if best > 0:
        return min_end + best

    return desired_end


def _safe_overlap(previous: str, overlap_chars: int) -> str:
    if overlap_chars <= 0 or not previous:
        return ""

    previous = previous.rstrip()
    if len(previous) <= overlap_chars:
        return previous

    tail_start = max(0, len(previous) - overlap_chars)
    search_start = max(0, len(previous) - int(overlap_chars * 1.8))
    window = previous[search_start:]

    best = -1
    for match in re.finditer(r"\n\s*\n|[。！？；.!?;]\s+|\s+", window):
        absolute = search_start + match.end()
        if absolute >= tail_start:
            best = absolute
            break

    if best < 0:
        best = tail_start
        while best < len(previous) and re.match(r"[A-Za-z0-9_\-]", previous[best]):
            best += 1

    return previous[best:].lstrip()


def _safe_overlap_start(text: str, chunk_start: int, chunk_end: int, overlap_chars: int) -> int:
    if overlap_chars <= 0:
        return chunk_end
    desired = max(chunk_start + 1, chunk_end - overlap_chars)
    search_start = max(chunk_start + 1, chunk_end - int(overlap_chars * 1.8))
    window = text[search_start:chunk_end]
    best = -1
    for match in re.finditer(r"\n\s*\n|[。！？；.!?;]\s*|\s+", window):
        absolute = search_start + match.end()
        if absolute <= desired:
            best = absolute
        elif best >= 0:
            break
    if best >= 0:
        return best
    pos = desired
    while pos < chunk_end and re.match(r"[A-Za-z0-9_\-]", text[pos]):
        pos += 1
    return min(max(pos, chunk_start + 1), chunk_end)


def split_text(
    text: str,
    *,
    chunk_size_tokens: int = 800,
    chunk_overlap_tokens: int = 120,
) -> list[tuple[str, int, int]]:
    """Split text into approximate-token chunks with paragraph preference."""

    normalized = (text or "").replace("\r\n", "\n").replace("\r", "\n")
    paragraphs = [
        match
        for match in re.finditer(r".+?(?:\n\s*\n|$)", normalized, flags=re.S)
        if match.group(0).strip()
    ]
    if not paragraphs:
        return []

    target_chars = max(200, int(chunk_size_tokens) * 3)
    overlap_chars = max(0, int(chunk_overlap_tokens) * 3)
    chunks: list[tuple[str, int, int]] = []
    current_parts: list[str] = []
    current_start: int | None = None
    current_end = 0

    def flush() -> None:
        nonlocal current_parts, current_start, current_end
        if current_start is None or not current_parts:
            return
        body = "".join(current_parts).strip()
        if body:
            chunks.append((body, current_start, current_end))
        current_parts = []
        current_start = None
        current_end = 0

    for para in paragraphs:
        raw = para.group(0)
        start, end = para.span()
        if len(raw) > target_chars:
            flush()
            pos = start
            while pos < end:
                sub_end = _safe_split_end(normalized, pos, pos + target_chars, end)
                body = normalized[pos:sub_end].strip()
                if body:
                    chunks.append((body, pos, sub_end))
                if sub_end >= end:
                    break
                pos = min(max(_safe_overlap_start(normalized, pos, sub_end, overlap_chars), pos + 1), end)
            continue

        current_len = sum(len(part) for part in current_parts)
        if current_parts and current_len + len(raw) > target_chars:
            previous = "".join(current_parts)
            flush()
            if overlap_chars > 0 and previous:
                overlap = _safe_overlap(previous, overlap_chars)
                current_parts = [overlap]
                current_start = max(start - len(overlap), 0)
                current_end = start

        if current_start is None:
            current_start = start
        current_parts.append(raw)
        current_end = end

    flush()
    return chunks


def build_chunks(
    text: str,
    *,
    doc_id: str,
    file_name: str,
    file_path: str | None,
    source_hash: str,
    document_date,
    category_path: str | None,
    commit_version: int,
    chunk_size_tokens: int = 800,
    chunk_overlap_tokens: int = 120,
) -> list[IngestChunk]:
    chunks: list[IngestChunk] = []
    for index, (content, start, end) in enumerate(
        split_text(
            text,
            chunk_size_tokens=chunk_size_tokens,
            chunk_overlap_tokens=chunk_overlap_tokens,
        )
    ):
        content_hash = sha256_text(content)
        fingerprint = stable_chunk_fingerprint("text", content)
        chunks.append(
            IngestChunk(
                chunk_id=stable_chunk_id(doc_id, commit_version, index, content_hash),
                doc_id=doc_id,
                chunk_index=index,
                content=content,
                content_hash=content_hash,
                stable_chunk_fingerprint=fingerprint,
                token_count=estimate_tokens(content),
                block_type="text",
                char_start=start,
                char_end=end,
                file_name=file_name,
                file_path=file_path,
                source_hash=source_hash,
                document_date=document_date,
                category_path=category_path,
                commit_version=commit_version,
            )
        )
    return chunks
