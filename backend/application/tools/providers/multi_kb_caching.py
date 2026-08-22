"""多库检索:请求级缓存 key、命中复用、轮次预留与输出预算截断。(拆自 multi_kb_search_tool.py)"""

from __future__ import annotations

import logging

import json
import re

from backend.application.tools.providers.multi_kb_types import FilterMode
from typing import Any

# 原 multi_kb_references 中的常量(结果文本块分隔符)。
_CHUNK_SEPARATOR = "\n\n---\n\n"


logger = logging.getLogger(__name__)


def _structured_text(value: Any) -> str:
    if isinstance(value, dict):
        return str(value.get("text") or "")
    return str(value or "")


def _structured_result(
    text: str,
    *,
    evidence_records: list[dict[str, Any]] | None = None,
    stats: dict[str, Any] | None = None,
) -> dict[str, Any]:
    records = list(evidence_records or [])
    return {
        "text": str(text or ""),
        "evidence_records": records,
        "stats": {
            "evidence_count": len(records),
            "cache_scope": "request",
            **dict(stats or {}),
        },
    }


def _ensure_structured_result(value: Any) -> dict[str, Any]:
    if isinstance(value, dict) and "text" in value:
        return dict(value)
    return _structured_result(str(value or ""))


def _mark_structured_cache_hit(value: Any, hit: str) -> dict[str, Any]:
    result = _ensure_structured_result(value)
    result["stats"] = {**dict(result.get("stats") or {}), "cache_hit": str(hit)}
    return result


def _build_cached_reference_block(
    evidence_records: list[dict[str, Any]],
) -> str | None:
    """Render the compact literature list for a cache-hit tool message."""

    titles: list[str] = []
    seen: set[str] = set()
    for record in evidence_records or []:
        if not isinstance(record, dict):
            continue
        title = str(record.get("doc_title") or "").strip()
        if not title or title in seen:
            continue
        seen.add(title)
        titles.append(title)
    if not titles:
        return None
    lines = "\n".join(f"- [{idx}] {title}" for idx, title in enumerate(titles, start=1))
    return (
        "## 可引用文献\n\n"
        "以下为本轮检索片段的实际来源文档；正文引用 [n] 与末尾「参考文档」"
        "必须使用这些具体文章/文献名称。若条目带页码，必须原样保留，禁止遗漏或臆造；"
        "禁止使用知识库容器名称：\n\n"
        f"{lines}"
    )


def _cached_reuse_result(value: Any, hit: str) -> dict[str, Any]:
    """Reuse structured evidence without injecting the same large text twice."""

    result = _mark_structured_cache_hit(value, hit)
    reused = dict(result)
    message = (
        "本轮已存在等价知识库证据，已复用结构化证据记录，" "未再次占用检索上下文预算。"
    )
    reference_block = _build_cached_reference_block(
        result.get("evidence_records") or []
    )
    reused["text"] = (
        message + ("\n\n" + reference_block) if reference_block else message
    )
    reused["stats"] = {
        **dict(result.get("stats") or {}),
        "cache_reused": True,
        "injected": False,
    }
    return reused


_PARTIAL_CONTEXT_MIN_CHARS = 256


_CONTEXT_BUDGET_TRUNCATION_NOTE = (
    "\n\n> **上下文预算截断**：本轮剩余检索上下文有限，"
    "已注入当前可容纳的证据；如需更多内容，请缩小检索范围。"
)


def _truncate_kb_result_text(text: str, max_chars: int) -> str:
    """Fit retrieval text to a hard character ceiling without dropping it all."""

    value = str(text or "")
    limit = max(0, int(max_chars))
    if len(value) <= limit:
        return value
    if limit <= len(_CONTEXT_BUDGET_TRUNCATION_NOTE) + 64:
        return value[:limit]

    prefix_limit = limit - len(_CONTEXT_BUDGET_TRUNCATION_NOTE)
    prefix = value[:prefix_limit].rstrip()
    boundary = prefix.rfind(_CHUNK_SEPARATOR)
    if boundary >= max(128, prefix_limit // 2):
        prefix = prefix[:boundary].rstrip()
    image_token_start = prefix.rfind("[IMAGE:")
    if image_token_start >= 0 and "]" not in prefix[image_token_start:]:
        prefix = prefix[:image_token_start].rstrip()
    return (prefix + _CONTEXT_BUDGET_TRUNCATION_NOTE)[:limit]


def _fit_result_to_output_limit(
    value: Any,
    max_chars: int | None,
) -> dict[str, Any]:
    """Apply the configured result ceiling to local and remote providers alike."""

    result = _ensure_structured_result(value)
    text = _structured_text(result)
    if not max_chars or max_chars <= 0 or len(text) <= max_chars:
        return result
    fitted = dict(result)
    fitted["text"] = _truncate_kb_result_text(text, max_chars)
    fitted["stats"] = {
        **dict(result.get("stats") or {}),
        "output_budget_truncated": True,
        "original_text_chars": len(text),
        "output_text_chars": len(fitted["text"]),
    }
    return fitted


def _reserve_result_for_turn(
    value: Any,
    turn_max: int,
) -> dict[str, Any] | None:
    """Atomically reserve context and compact a result to the granted slice."""

    from backend.core.tools.request_limits import reserve_turn_kb_chars

    result = _ensure_structured_result(value)
    text = _structured_text(result)
    if not text:
        return result
    reserved = reserve_turn_kb_chars(
        len(text),
        turn_max=turn_max,
        minimum_chars=min(len(text), _PARTIAL_CONTEXT_MIN_CHARS),
    )
    if reserved <= 0:
        return None
    if reserved >= len(text):
        return result

    compacted = dict(result)
    compacted["text"] = _truncate_kb_result_text(text, reserved)
    compacted["stats"] = {
        **dict(result.get("stats") or {}),
        "context_budget_truncated": True,
        "original_text_chars": len(text),
        "injected_text_chars": len(compacted["text"]),
    }
    return compacted


def _context_budget_exhausted_result(
    text: str,
    *,
    cache_hit: str | None = None,
) -> dict[str, Any]:
    stats: dict[str, Any] = {
        "context_budget_exhausted": True,
        "injected": False,
    }
    if cache_hit:
        stats["cache_hit"] = cache_hit
    return _structured_result(text, stats=stats)


def _build_request_cache_key(
    *,
    query: str,
    kb_ids: list[str],
    top_k: int,
    chunk_top_k: int,
    merge_strategy: str,
    external_hl_keywords: list[str] | None,
    external_ll_keywords: list[str] | None,
    external_query_type: str | None,
    user_provided_keywords: bool,
    content_filter_mode: FilterMode | None,
    max_result_chars: int | None,
    include_graph: bool = True,
    deep_retrieval: bool = False,
) -> str:
    normalized_query = re.sub(r"\s+", " ", str(query or "").strip().lower())
    return json.dumps(
        {
            "query": normalized_query,
            "parameters": {
                "kb_ids": sorted(kb_ids or []),
                "top_k": top_k,
                "chunk_top_k": chunk_top_k,
                "merge_strategy": str(merge_strategy or "rrf"),
                "high_level_keywords": external_hl_keywords or [],
                "low_level_keywords": external_ll_keywords or [],
                "query_type": str(external_query_type or ""),
                "keyword_mode": "external" if user_provided_keywords else "shared",
                "content_filter_mode": (
                    content_filter_mode.value if content_filter_mode else "profile"
                ),
                "max_result_chars": int(max_result_chars or 0),
                "include_graph": bool(include_graph),
                "deep_retrieval": bool(deep_retrieval),
            },
        },
        ensure_ascii=False,
        sort_keys=True,
        separators=(",", ":"),
    )
