"""Retrieval-only MCP use cases with image content removed."""

from __future__ import annotations

import re
from typing import Any

from backend.application.retrieval.query_service import retrieve_context
from backend.rag.media_blocks import IMAGE_BLOCK_RE

NUCLEAR_ELECTRONICS_KB_ID = "ca8f3763-f956-492f-a373-e2dfc3e89d60"
NUCLEAR_ELECTRONICS_KB_CODE = "NuclearElectronicsPaperRepositorySelf"
NUCLEAR_ELECTRONICS_KB_NAME = "核电子学期刊知识库"
NUCLEAR_ELECTRONICS_TEXT_TOOL = "query_nuclear_electronics_text"

_MARKDOWN_IMAGE_RE = re.compile(r"!\[[^\]]*\]\([^\r\n)]*\)")
_HTML_IMAGE_RE = re.compile(r"<img\b[^>]*>", re.IGNORECASE | re.DOTALL)
_IMAGE_TOKEN_RE = re.compile(
    r"\[(?:图片[^\]\r\n]*?)?\[?IMAGE\s*:\s*[^\]\r\n]+\]?[^\]\r\n]*\]"
    r"|\[?IMAGE\s*:\s*[A-Za-z0-9_.:-]+\]?",
    re.IGNORECASE,
)
_IMAGE_URL_RE = re.compile(
    r"(?:https?://|/)(?:[^\s<>\]\[()'\"]+?)(?:"
    r"\.(?:avif|bmp|gif|ico|jpe?g|png|svg|tiff?|webp)(?:\?[^\s<>\]\[()'\"]*)?"
    r"|api/v\d+/files/kb-image(?:\?[^\s<>\]\[()'\"]*)?"
    r")",
    re.IGNORECASE,
)
_DATA_IMAGE_RE = re.compile(r"data:image/[^;\s]+;base64,[A-Za-z0-9+/=\s]+", re.I)
_EXCESS_BLANK_LINES_RE = re.compile(r"\n{3,}")


def strip_all_image_content(value: str) -> str:
    """Remove stored image blocks, markup, tokens, URLs and base64 payloads."""

    text = str(value or "")
    text = IMAGE_BLOCK_RE.sub("", text)
    text = _HTML_IMAGE_RE.sub("", text)
    text = _MARKDOWN_IMAGE_RE.sub("", text)
    text = _DATA_IMAGE_RE.sub("", text)
    text = _IMAGE_TOKEN_RE.sub("", text)
    text = _IMAGE_URL_RE.sub("", text)
    lines = [line.rstrip() for line in text.splitlines()]
    text = "\n".join(lines).strip()
    return _EXCESS_BLANK_LINES_RE.sub("\n\n", text)


def _chunk_value(chunk: Any, key: str, default: Any = None) -> Any:
    if isinstance(chunk, dict):
        return chunk.get(key, default)
    return getattr(chunk, key, default)


def _chunk_score(chunk: Any) -> float | None:
    for key in ("_rerank_score", "_coverage_score", "_chunk_score", "score"):
        value = _chunk_value(chunk, key)
        if value is None:
            continue
        try:
            return round(float(value), 6)
        except (TypeError, ValueError):
            continue
    return None


async def query_nuclear_electronics_text(
    query: str,
    *,
    limit: int = 10,
) -> dict[str, Any]:
    """Return raw text evidence from the fixed KB without answer generation."""

    normalized_query = str(query or "").strip()
    if not normalized_query:
        raise ValueError("query 不能为空")
    if len(normalized_query) > 4000:
        raise ValueError("query 不能超过 4000 个字符")

    effective_limit = max(1, min(int(limit or 10), 20))
    filtered_data, _prompt, _images, _stats = await retrieve_context(
        query=normalized_query,
        kb_id=NUCLEAR_ELECTRONICS_KB_ID,
        top_k=max(30, effective_limit * 3),
        chunk_top_k=min(20, max(effective_limit * 2, effective_limit)),
        external_hl_keywords=[],
        external_ll_keywords=[],
        external_query_type="general",
    )

    results: list[dict[str, Any]] = []
    for chunk in list(getattr(filtered_data, "chunks", []) or []):
        block_type = str(_chunk_value(chunk, "block_type", "text") or "text").lower()
        if block_type == "image":
            continue
        content = strip_all_image_content(str(_chunk_value(chunk, "content", "") or ""))
        if not content:
            continue
        item: dict[str, Any] = {
            "content": content,
            "document_name": str(_chunk_value(chunk, "file_name", "") or ""),
            "chunk_id": str(
                _chunk_value(chunk, "chunk_id", _chunk_value(chunk, "id", "")) or ""
            ),
            "chunk_index": _chunk_value(chunk, "chunk_index"),
        }
        score = _chunk_score(chunk)
        if score is not None:
            item["score"] = score
        results.append(item)
        if len(results) >= effective_limit:
            break

    return {
        "knowledge_base_id": NUCLEAR_ELECTRONICS_KB_ID,
        "knowledge_base_name": NUCLEAR_ELECTRONICS_KB_NAME,
        "query": normalized_query,
        "results": results,
    }


__all__ = [
    "NUCLEAR_ELECTRONICS_KB_CODE",
    "NUCLEAR_ELECTRONICS_KB_ID",
    "NUCLEAR_ELECTRONICS_KB_NAME",
    "NUCLEAR_ELECTRONICS_TEXT_TOOL",
    "query_nuclear_electronics_text",
    "strip_all_image_content",
]
