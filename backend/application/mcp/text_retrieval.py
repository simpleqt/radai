"""Retrieval-only MCP use cases with image content removed."""

from __future__ import annotations

import asyncio
import re
from typing import Any

from backend.application.knowledge_bases.catalog_service import (
    load_knowledge_base_catalog,
)
from backend.application.knowledge.internal_client import internal_retrieve
from backend.domain.knowledge_base.catalog import KnowledgeBaseCatalogEntry
from backend.core.text.media_blocks import IMAGE_BLOCK_RE

NUCLEAR_ELECTRONICS_KB_CODE = "rag_nuclear_electronics"
NUCLEAR_ELECTRONICS_KB_NAME = "核电子学知识库"
NUCLEAR_ELECTRONICS_JOURNAL_KB_CODE = "NuclearElectronicsPaperRepositorySelf"
NUCLEAR_ELECTRONICS_JOURNAL_KB_NAME = "核电子学期刊知识库"
NUCLEAR_ELECTRONICS_TEXT_TOOL = "query_nuclear_electronics_text"

KnowledgeBaseSelection = str

_CONFIGURED_KNOWLEDGE_BASES = (
    (NUCLEAR_ELECTRONICS_KB_CODE, NUCLEAR_ELECTRONICS_KB_NAME),
    (NUCLEAR_ELECTRONICS_JOURNAL_KB_CODE, NUCLEAR_ELECTRONICS_JOURNAL_KB_NAME),
)

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


async def _load_configured_knowledge_bases() -> dict[str, dict[str, str]]:
    """Resolve stable KB codes to environment-specific IDs from MySQL."""

    catalog = await load_knowledge_base_catalog()
    by_code = {entry.kb_code.casefold(): entry for entry in catalog}
    configured: dict[str, dict[str, str]] = {}
    missing: list[str] = []
    for code, fallback_name in _CONFIGURED_KNOWLEDGE_BASES:
        entry: KnowledgeBaseCatalogEntry | None = by_code.get(code.casefold())
        if entry is None:
            missing.append(code)
            continue
        configured[entry.kb_id] = {
            "id": entry.kb_id,
            "code": entry.kb_code,
            "name": entry.kb_name or fallback_name,
        }
    if missing:
        raise ValueError(f"未找到已配置的知识库编码: {', '.join(missing)}")
    return configured


def _resolve_knowledge_base_ids(
    knowledge_bases: list[str] | None,
    configured: dict[str, dict[str, str]],
) -> list[str]:
    """Resolve the controlled MCP selection; an omitted selection uses one KB."""

    values = [
        str(value or "").strip()
        for value in (knowledge_bases or [])
        if str(value or "").strip()
    ]
    if not values:
        return [next(iter(configured))]
    if any(value.casefold() in {"all", "全部", "*"} for value in values):
        return list(configured)

    aliases = {
        alias.casefold(): kb_id
        for kb_id, metadata in configured.items()
        for alias in (kb_id, metadata["code"], metadata["name"])
    }

    resolved: list[str] = []
    invalid: list[str] = []
    for value in values:
        kb_id = aliases.get(value.casefold())
        if kb_id is None:
            invalid.append(value)
        elif kb_id not in resolved:
            resolved.append(kb_id)
    if invalid:
        allowed = ", ".join(
            f"{item['name']} ({item['id']})" for item in configured.values()
        )
        raise ValueError(f"不支持的知识库: {', '.join(invalid)}；可选: {allowed}, all")
    return resolved


async def _retrieve_knowledge_base(
    *,
    query: str,
    kb_id: str,
    limit: int,
) -> tuple[str, dict[str, Any]]:
    # 检索在 omnigraph 知识服务内执行;此处只取文本块元数据。
    response = await internal_retrieve(
        query=query,
        kb_id=kb_id,
        top_k=max(30, limit * 3),
        chunk_top_k=min(20, max(limit * 2, limit)),
        hl_keywords=[],
        ll_keywords=[],
        query_type="general",
    )
    return kb_id, response


async def query_nuclear_electronics_text(
    query: str,
    *,
    limit: int = 10,
    knowledge_bases: list[str] | None = None,
) -> dict[str, Any]:
    """Return raw text evidence from one or all configured nuclear-electronics KBs."""

    normalized_query = str(query or "").strip()
    if not normalized_query:
        raise ValueError("query 不能为空")
    if len(normalized_query) > 4000:
        raise ValueError("query 不能超过 4000 个字符")

    effective_limit = max(1, min(int(limit or 10), 20))
    configured = await _load_configured_knowledge_bases()
    selected_ids = _resolve_knowledge_base_ids(knowledge_bases, configured)
    retrieved = await asyncio.gather(
        *(
            _retrieve_knowledge_base(
                query=normalized_query,
                kb_id=kb_id,
                limit=effective_limit,
            )
            for kb_id in selected_ids
        )
    )

    results: list[dict[str, Any]] = []
    for kb_id, response in retrieved:
        kb = configured[kb_id]
        for chunk in list(response.get("chunks") or []):
            block_type = str(
                _chunk_value(chunk, "block_type", "text") or "text"
            ).lower()
            if block_type == "image":
                continue
            content = strip_all_image_content(
                str(_chunk_value(chunk, "content", "") or "")
            )
            if not content:
                continue
            item: dict[str, Any] = {
                "content": content,
                "document_name": str(_chunk_value(chunk, "file_name", "") or ""),
                "chunk_id": str(
                    _chunk_value(
                        chunk, "chunk_id", _chunk_value(chunk, "id", "")
                    )
                    or ""
                ),
                "chunk_index": _chunk_value(chunk, "chunk_index"),
                "knowledge_base_id": kb_id,
                "knowledge_base_name": kb["name"],
            }
            score = _chunk_score(chunk)
            if score is not None:
                item["score"] = score
            results.append(item)

    results.sort(
        key=lambda item: (
            item.get("score") is not None,
            item.get("score") if item.get("score") is not None else float("-inf"),
        ),
        reverse=True,
    )
    results = results[:effective_limit]

    selected_metadata = [configured[kb_id] for kb_id in selected_ids]
    return {
        # Keep the legacy singular fields for default/single selection clients.
        "knowledge_base_id": selected_ids[0] if len(selected_ids) == 1 else None,
        "knowledge_base_name": (
            selected_metadata[0]["name"] if len(selected_metadata) == 1 else None
        ),
        "knowledge_bases": selected_metadata,
        "query": normalized_query,
        "results": results,
    }


__all__ = [
    "NUCLEAR_ELECTRONICS_KB_CODE",
    "NUCLEAR_ELECTRONICS_KB_NAME",
    "NUCLEAR_ELECTRONICS_JOURNAL_KB_CODE",
    "NUCLEAR_ELECTRONICS_JOURNAL_KB_NAME",
    "NUCLEAR_ELECTRONICS_TEXT_TOOL",
    "KnowledgeBaseSelection",
    "query_nuclear_electronics_text",
    "strip_all_image_content",
]
