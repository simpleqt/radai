"""多知识库检索工具 - 优化版

同时检索多个知识库，使用倒数排名融合算法合并结果。
用于核电子学全功能智能体的 RAG 检索。

优化：
  - 并发查询多个知识库
  - 单库与 retrieve_context 一致：阶段1召回 + 内容过滤（PPR/Rerank/MMR）
  - 超时保护（每个知识库独立超时）
  - 性能统计和详细日志
  - 错误隔离（单个知识库失败不影响其他）

排障说明（与日志「向量搜索」打印次数相关）：
  - 实体召回 Path-A 会对「整句向量」打 1 次 keyword_index，再对高阶关键词 hl 的前 3 个各打 1 次，
    故有非空 hl 时同一知识库下 ``per_kb_index`` 的「返回 N 个结果」日志通常出现 4 次（N 由
    ``entity_vec_top_k`` 等配置决定，与 multi_kb 的 top_k 不是同一参数）。
  - **默认开启** ``MULTI_KB_PATH_A_QUERY_ONLY``：共享单库关键词后，各 KB 的 Path-A 仅整句 1 路
    （双库合计约 2 次 keyword_index，而非 8 次）。需要更高召回可设 ``MULTI_KB_PATH_A_QUERY_ONLY=0``。
  - 未显式传入高/低词时，仅在首个 KB 提取一次关键词并复用，避免双库各 ~2.5s 的重复提取。
  - 降低 ``MULTI_KB_SEARCH_DEFAULT_TOP_K`` / ``MULTI_KB_SEARCH_MAX_TOP_K`` 可减轻阶段1与过滤开销。
    把上限提高到 **10** 会扩大候选池与 Milvus 调用量，**通常更慢**；要提速请**调低**而非调高。
"""

import asyncio
import json
import logging
import os
import re
import threading
import time
from typing import Annotated, Any
from urllib.parse import unquote

from langchain_core.tools import InjectedToolArg, tool
from pydantic import BaseModel, Field

from backend.rag.content_filter import FilterMode
from backend.application.agent_executors.image_url_registry import ImageURLRegistry
from langchain_core.runnables import RunnableConfig

logger = logging.getLogger(__name__)

def _normalize_doc_display_name(raw: str) -> str:
    """从路径或文件名提取可展示的文献名（去目录、去后缀）。"""
    cleaned = str(raw or "").strip().strip('"\'')
    if not cleaned:
        return ""
    # 部分 chunk 的 source 形如 "doc_id::filename"
    if "::" in cleaned:
        cleaned = cleaned.rsplit("::", 1)[-1].strip()
    name = os.path.basename(cleaned.replace("\\", "/"))
    decoded_name = name
    for _ in range(2):
        next_name = unquote(decoded_name)
        if next_name == decoded_name:
            break
        decoded_name = next_name
    stem, _ = os.path.splitext(name)
    decoded_stem, _ = os.path.splitext(decoded_name)
    display = (decoded_stem or decoded_name or stem or name).strip()
    return display


def _extract_doc_name_from_chunk(chunk: dict | Any, *, kb_label: str = "") -> str:
    """从 chunk 元数据提取具体文献/文章名；无法识别时回退 kb_label。"""
    if not isinstance(chunk, dict):
        return kb_label

    for key in ("file_path", "source", "source_id", "file_name", "source_file_path"):
        value = chunk.get(key)
        if not value:
            continue
        display = _normalize_doc_display_name(str(value))
        if display:
            return display

    metadata = chunk.get("metadata")
    if isinstance(metadata, dict):
        for key in ("file_path", "source", "file_name", "title"):
            value = metadata.get(key)
            if not value:
                continue
            display = _normalize_doc_display_name(str(value))
            if display:
                return display

    return kb_label


def _extract_page_nums_from_chunk(chunk: dict | Any) -> tuple[int, ...]:
    """Extract normalized one-based page numbers emitted by DirectRetriever v2."""

    if not isinstance(chunk, dict):
        return ()
    candidates: list[Any] = []
    for container in (
        chunk,
        chunk.get("extra_json"),
        chunk.get("metadata"),
    ):
        if not isinstance(container, dict):
            continue
        raw_pages = container.get("page_nums")
        if isinstance(raw_pages, (list, tuple, set)):
            candidates.extend(raw_pages)
        candidates.append(container.get("page_num"))

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


def _format_page_locator(page_nums: tuple[int, ...] | list[int]) -> str:
    pages = sorted(set(page_nums))
    if not pages:
        return ""

    ranges: list[str] = []
    start = previous = pages[0]
    for page in pages[1:]:
        if page == previous + 1:
            previous = page
            continue
        ranges.append(str(start) if start == previous else f"{start}–{previous}")
        start = previous = page
    ranges.append(str(start) if start == previous else f"{start}–{previous}")
    return f"第 {'、'.join(ranges)} 页"


def _format_doc_reference(doc_name: str, page_nums: tuple[int, ...] | list[int]) -> str:
    locator = _format_page_locator(page_nums)
    return f"{doc_name}，{locator}" if locator else doc_name


def _build_reference_block(
    selected: list[tuple[str, float]],
    *,
    chunk_contents: dict[str, dict],
    kb_display_names: set[str],
) -> str | None:
    doc_pages: dict[str, set[int]] = {}
    for chunk_key, _score in selected:
        info = chunk_contents[chunk_key]
        doc_name = info.get("doc_name") or ""
        if not doc_name or doc_name in kb_display_names:
            continue
        doc_pages.setdefault(doc_name, set()).update(info.get("page_nums") or ())
    if not doc_pages:
        return None

    ref_lines = "\n".join(
        f"- [{idx}] {_format_doc_reference(name, sorted(pages))}"
        for idx, (name, pages) in enumerate(doc_pages.items(), start=1)
    )
    return (
        "## 可引用文献\n\n"
        "以下为本轮检索片段的实际来源文档；正文引用 [n] 与末尾「参考文档」"
        "必须使用这些具体文章/文献名称。若条目带页码，必须原样保留，禁止遗漏或臆造；"
        "禁止使用知识库容器名称：\n\n"
        f"{ref_lines}"
    )


KB_SEARCH_TIMEOUT = 60  # 单个知识库查询超时（秒）
TOTAL_SEARCH_TIMEOUT = 90  # 总查询超时（秒）
SUBAGENT_KB_SEARCH_TIMEOUT = int(os.getenv("SUBAGENT_KB_SEARCH_TIMEOUT", "90"))
SUBAGENT_TOTAL_SEARCH_TIMEOUT = int(os.getenv("SUBAGENT_TOTAL_SEARCH_TIMEOUT", "120"))
if SUBAGENT_TOTAL_SEARCH_TIMEOUT <= SUBAGENT_KB_SEARCH_TIMEOUT:
    SUBAGENT_TOTAL_SEARCH_TIMEOUT = SUBAGENT_KB_SEARCH_TIMEOUT + 10
# 默认偏延迟优化（3/2）；可用环境变量或 tool 参数调高，但 MAX 仍硬封顶。
MAX_TOP_K = max(1, int(os.getenv("MULTI_KB_SEARCH_MAX_TOP_K", "12")))
MAX_CHUNK_TOP_K = max(1, int(os.getenv("MULTI_KB_SEARCH_MAX_CHUNK_TOP_K", "6")))
_TOOL_DEFAULT_TOP_K = max(1, min(MAX_TOP_K, int(os.getenv("MULTI_KB_SEARCH_DEFAULT_TOP_K", "10"))))
_TOOL_DEFAULT_CHUNK_TOP_K = max(1, min(MAX_CHUNK_TOP_K, int(os.getenv("MULTI_KB_SEARCH_DEFAULT_CHUNK_TOP_K", "6"))))

# 默认开启：Path-A 仅整句向量（每 KB 1 次 keyword_index，而非 1+3 次）。
# 关闭：MULTI_KB_PATH_A_QUERY_ONLY=0 / false / no / off
_PAO_RAW = os.getenv("MULTI_KB_PATH_A_QUERY_ONLY", "1").strip().lower()
_MULTI_KB_PATH_A_QUERY_ONLY = _PAO_RAW not in {"0", "false", "no", "off"}

# 多 KB 工具关闭 retrieve_context 的阶段1过采样（不将 chunk_top_k×5 放大），减轻 PPR/重排/MMR 负载
_MULTI_KB_DISABLE_STAGE1_OVERSAMPLE = True

# 非轻量全功能 Agent：单次工具返回字符上限（0 表示不截断）
MAX_RESULT_CHARS_FULL_AGENT = max(
    0, int(os.getenv("MULTI_KB_SEARCH_MAX_RESULT_CHARS", "8000"))
)
# 非轻量普通模式每轮 multi_kb_search 最大调用次数
NORMAL_MAX_CALLS_PER_TURN = max(
    1, int(os.getenv("MULTI_KB_SEARCH_NORMAL_MAX_CALLS", "2"))
)
# 单次检索 chunk 正文的字符预算（不含图片白名单等元数据）
_EVIDENCE_BODY_CHAR_BUDGET = max(
    500, int(os.getenv("MULTI_KB_SEARCH_EVIDENCE_BODY_CHARS", "7200"))
)

_CONTEXT_SECTION_HEADER = "## 检索到的上下文"
_STATS_SECTION_HEADER = "## 检索统计"
_CHUNK_SEPARATOR = "\n\n---\n\n"
_FRAGMENT_HEADING_PREFIX = "### 片段 "
_TRUNCATION_NOTE = (
    "> **内容截断**：本轮检索结果已超过 {limit} 字上限，已按完整片段（chunk）保留前 {kept} 段，"
    "其余 {dropped} 段未返回。请换用更具体的关键词、拆分检索角度，再次调用 `multi_kb_search` 获取更多证据。"
)
_TARGET_DOCUMENT_MATCH_TRUE = "<target_document_match>true</target_document_match>"
_TARGET_DOCUMENT_MATCH_FALSE = "<target_document_match>false</target_document_match>"


def _rag_service_base_url() -> str:
    """Return the optional dedicated RAG service endpoint."""
    return os.getenv("RAG_SERVICE_BASE_URL", "").strip().rstrip("/")


async def _execute_remote_multi_kb_search(
    *,
    base_url: str,
    query: str,
    kb_ids: list[str],
    top_k: int,
    chunk_top_k: int,
    merge_strategy: str,
    external_hl_keywords: list[str] | None,
    external_ll_keywords: list[str] | None,
    external_query_type: str | None,
    content_filter_mode: FilterMode | None,
    max_result_chars: int | None,
    _registry: Any = None,
) -> dict[str, Any]:
    """Delegate retrieval to the long-lived knowledge service."""
    import httpx

    from backend.config.settings import settings

    secret = str(settings.internal_api_secret or "").strip()
    if not secret:
        raise RuntimeError(
            "RAG_SERVICE_BASE_URL requires INTERNAL_API_SECRET to protect internal retrieval"
        )
    timeout_seconds = max(
        1.0,
        float(os.getenv("RAG_SERVICE_REQUEST_TIMEOUT_SECONDS", "150") or "150"),
    )
    payload = {
        "query": query,
        "kb_ids": kb_ids,
        "top_k": top_k,
        "chunk_top_k": chunk_top_k,
        "merge_strategy": merge_strategy,
        "external_hl_keywords": external_hl_keywords,
        "external_ll_keywords": external_ll_keywords,
        "external_query_type": external_query_type,
        "content_filter_mode": (
            content_filter_mode.value if isinstance(content_filter_mode, FilterMode) else None
        ),
        "max_result_chars": max_result_chars,
    }
    try:
        async with httpx.AsyncClient(trust_env=False, timeout=timeout_seconds) as client:
            response = await client.post(
                f"{base_url}/api/v1/rag/internal/multi-kb-search",
                json=payload,
                headers={"X-Internal-Secret": secret},
            )
        response.raise_for_status()
        result = response.json()
    except httpx.HTTPError as exc:
        raise RuntimeError(f"知识库服务请求失败: {exc}") from exc
    if not isinstance(result, dict):
        raise RuntimeError("知识库服务返回了无效响应")

    _remap_remote_registered_images(result, _registry=_registry)

    return result


def _remap_remote_registered_images(
    result: dict[str, Any],
    *,
    _registry: Any = None,
) -> dict[str, Any]:
    """Map knowledge-service image tokens into the request-local registry.

    Every internal HTTP retrieval owns a fresh registry, so separate Agentic
    RAG rounds may both return ``[IMAGE:001]`` for different URLs.  Blindly
    inserting those entries overwrites the first image.  Registering by URL in
    the agent request and rewriting all returned textual fields gives every
    image a stable, collision-free token across retrieval rounds.
    """

    registered_images = result.get("registered_images") or []
    if not isinstance(registered_images, list) or not registered_images:
        return result
    if _registry is None:
        from backend.application.agent_executors.image_url_registry import get_registry

        _registry = get_registry()

    token_mapping: dict[str, str] = {}
    local_images: list[dict[str, str]] = []
    seen_local_tokens: set[str] = set()
    for image in registered_images:
        if not isinstance(image, dict):
            continue
        remote_token = str(image.get("token") or "").strip()
        url = str(image.get("url") or "").strip()
        caption = str(image.get("caption") or "").strip()
        if not remote_token or not url:
            continue
        local_token = str(_registry.register(url, caption) or "").strip()
        if not local_token:
            continue
        token_mapping[remote_token] = local_token
        if local_token in seen_local_tokens:
            continue
        seen_local_tokens.add(local_token)
        local_images.append(
            {"token": local_token, "url": url, "caption": caption}
        )

    if token_mapping:
        token_pattern = re.compile(
            "|".join(
                re.escape(token)
                for token in sorted(token_mapping, key=len, reverse=True)
            )
        )

        def replace_tokens(value: Any) -> Any:
            if isinstance(value, str):
                return token_pattern.sub(
                    lambda match: token_mapping.get(match.group(0), match.group(0)),
                    value,
                )
            if isinstance(value, list):
                return [replace_tokens(item) for item in value]
            if isinstance(value, dict):
                return {
                    key: replace_tokens(item)
                    for key, item in value.items()
                    if key != "registered_images"
                }
            return value

        remapped = replace_tokens(result)
        if isinstance(remapped, dict):
            result.clear()
            result.update(remapped)
    result["registered_images"] = local_images
    logger.info(
        "[_execute_remote_multi_kb_search] remapped %s image token(s) into registry id=%s",
        len(token_mapping),
        id(_registry),
    )
    return result


def _normalize_document_code(value: str) -> str:
    # Keep Unicode letters so Chinese book/paper titles can use the same exact
    # source-prioritization path as GB/HJ codes.
    return re.sub(r"[\W_]+", "", str(value or "").upper(), flags=re.UNICODE)


def _extract_explicit_document_codes(query: str) -> list[tuple[str, str]]:
    """Extract explicit standard codes and Chinese quoted document titles."""
    pattern = re.compile(
        r"\b(?:GB(?:\s*/\s*T)?|HJ|DB\d{2,}\s*/\s*T|T\s*/\s*[A-Z0-9]+)"
        r"\s*[A-Z0-9.]+(?:\s*-\s*\d{4})?\b",
        re.IGNORECASE,
    )
    found: list[tuple[str, str]] = []
    seen: set[str] = set()
    for match in pattern.finditer(query or ""):
        display = re.sub(r"\s+", " ", match.group(0)).strip()
        normalized = _normalize_document_code(display)
        if normalized and normalized not in seen:
            seen.add(normalized)
            found.append((display, normalized))
    for match in re.finditer(r"《\s*([^《》\n]{2,160}?)\s*》", query or ""):
        display = re.sub(r"\s+", " ", match.group(1)).strip()
        normalized = _normalize_document_code(display)
        if normalized and normalized not in seen:
            seen.add(normalized)
            found.append((display, normalized))
    return found


def _joined_parts_len(parts: list[str]) -> int:
    if not parts:
        return 0
    return sum(len(part) for part in parts) + len(_CHUNK_SEPARATOR) * (len(parts) - 1)


_OVERSIZED_FRAGMENT_NOTE = "\n\n...[片段因上下文预算截断]"


def _fit_oversized_block(value: str, max_chars: int) -> str:
    limit = max(0, int(max_chars))
    text = str(value or "")
    if len(text) <= limit:
        return text
    if limit <= len(_OVERSIZED_FRAGMENT_NOTE):
        return text[:limit]
    prefix = text[: limit - len(_OVERSIZED_FRAGMENT_NOTE)].rstrip()
    return prefix + _OVERSIZED_FRAGMENT_NOTE


def _fit_whole_parts(parts: list[str], max_chars: int | None) -> tuple[list[str], bool]:
    """按完整块累加，不在块内截断字符。"""
    if max_chars is None or max_chars <= 0:
        return parts, False
    if not parts:
        return [], False

    kept: list[str] = []
    truncated = False
    for part in parts:
        trial = kept + [part]
        if _joined_parts_len(trial) <= max_chars:
            kept.append(part)
            continue
        if not kept:
            kept.append(_fit_oversized_block(part, max_chars))
            truncated = True
            break
        truncated = True
        break

    if len(kept) < len(parts) and not truncated:
        truncated = True
    return kept, truncated


def _format_fragment(idx: int, doc_name: str, content: str) -> str:
    return f"{_FRAGMENT_HEADING_PREFIX}{idx}（来自：{doc_name}）\n\n{content}"


def _select_fragment_entries(
    ref_block: str | None,
    entries: list[tuple[str, str]],
    max_context_chars: int | None,
) -> tuple[list[tuple[str, str]], bool]:
    """按完整 chunk 片段选择，保留 RRF 顺序。"""
    if max_context_chars is None or max_context_chars <= 0:
        return entries, False
    if not entries:
        return [], False

    ref_parts = [ref_block] if ref_block else []
    kept: list[tuple[str, str]] = []
    truncated = False

    for doc_name, content in entries:
        trial_fragments = [
            _format_fragment(idx, dn, c)
            for idx, (dn, c) in enumerate(kept + [(doc_name, content)], start=1)
        ]
        if _joined_parts_len(ref_parts + trial_fragments) <= max_context_chars:
            kept.append((doc_name, content))
            continue
        if not kept:
            empty_fragment = _format_fragment(1, doc_name, "")
            content_budget = max(0, max_context_chars - len(empty_fragment))
            kept.append(
                (
                    doc_name,
                    _fit_oversized_block(content, content_budget),
                )
            )
            truncated = True
            break
        truncated = True
        break

    if len(kept) < len(entries) and not truncated:
        truncated = True
    return kept, truncated


def _prioritize_explicit_document_evidence(
    items: list[tuple[str, float]],
    *,
    chunk_contents: dict[str, dict[str, Any]],
    query: str,
) -> list[tuple[str, float]]:
    """Order passages inside an explicitly requested source by query support.

    Source matching first isolates the document the user actually named.  A
    document can still contribute several recalled chunks, however, and their
    original graph/vector rank may put a large nearby image before the exact
    numeric or formula passage.  Under a bounded context budget that can hide
    the answer-bearing chunk completely.  Reusing the retriever's generic
    lexical/OCR-number scorer resolves that ordering without query-specific
    markers or benchmark-specific rules.
    """

    if not query or len(items) <= 1:
        return items
    try:
        from backend.rag.retriever_v2.direct_retriever import (
            _structured_query_score,
        )
    except Exception as exc:  # pragma: no cover - defensive import boundary
        logger.debug("显式来源证据重排不可用，保留融合顺序: %s", exc)
        return items

    scored = []
    for original_rank, item in enumerate(items):
        content = str(chunk_contents.get(item[0], {}).get("content") or "")
        scored.append(
            (
                _structured_query_score(content, query),
                original_rank,
                item,
            )
        )
    if not any(support > 0 for support, _rank, _item in scored):
        return items
    return [
        item
        for _support, _rank, item in sorted(
            scored,
            key=lambda value: (-value[0], value[1]),
        )
    ]


_QUERY_EVIDENCE_PROMOTION_MIN_SUPPORT = 4.0
_QUERY_EVIDENCE_PROMOTION_MARGIN = 1.05


def _promote_query_supported_evidence(
    items: list[tuple[str, float]],
    *,
    chunk_contents: dict[str, dict[str, Any]],
    query: str,
) -> list[tuple[str, float]]:
    """Promote one decisively better answer-bearing candidate.

    Cross-KB RRF/weighted ranks express recall confidence, but the first large
    chunk is not always the passage that best covers the literal question. A
    bounded output budget can therefore hide a lower-ranked exact table,
    formula, or numeric row. Candidate membership and retrieval scores remain
    untouched; this only moves the strongest lexical/OCR-supported passage to
    the front when it clears a generic minimum and margin.
    """

    if not query or len(items) <= 1:
        return items
    try:
        from backend.rag.retriever_v2.direct_retriever import (
            _structured_query_score,
        )
    except Exception as exc:  # pragma: no cover - defensive import boundary
        logger.debug("查询支持证据前置不可用，保留融合顺序: %s", exc)
        return items

    supports = [
        _structured_query_score(
            str(chunk_contents.get(item[0], {}).get("content") or ""),
            query,
        )
        for item in items
    ]
    best_rank = max(range(len(items)), key=lambda rank: (supports[rank], -rank))
    if best_rank == 0:
        return items
    best_support = supports[best_rank]
    first_support = supports[0]
    if (
        best_support < _QUERY_EVIDENCE_PROMOTION_MIN_SUPPORT
        or best_support
        < max(first_support, 0.0) * _QUERY_EVIDENCE_PROMOTION_MARGIN
    ):
        return items

    promoted = list(items)
    promoted.insert(0, promoted.pop(best_rank))
    return promoted


def _estimate_context_char_budget(
    max_result_chars: int | None,
    *,
    active_kbs: int,
) -> int | None:
    """估算 chunk 正文可用字符预算（图片白名单/统计不计入，避免只剩 1 段 chunk）。"""
    if not max_result_chars or max_result_chars <= 0:
        return None
    if max_result_chars >= _EVIDENCE_BODY_CHAR_BUDGET:
        return _EVIDENCE_BODY_CHAR_BUDGET
    # 显式传入更小上限时，预留约 350 字给 header/统计/截断提示
    return max(400, max_result_chars - 350)

_INFLIGHT_MULTI_KB_TASKS: dict[str, asyncio.Task[str]] = {}
_INFLIGHT_MULTI_KB_LOCK = threading.Lock()


class MultiKBSearchInput(BaseModel):
    query: str = Field(description="搜索查询内容")
    kb_ids: list[str] | None = Field(
        default=None, description="知识库ID列表，默认使用全部"
    )
    top_k: int = Field(default=_TOOL_DEFAULT_TOP_K, description="每个知识库召回文档数量")
    chunk_top_k: int = Field(default=_TOOL_DEFAULT_CHUNK_TOP_K, description="返回文本块数量")
    merge_strategy: str = Field(
        default="rrf", description="合并策略: rrf(倒数排名融合), weighted(加权)"
    )


def _strip_internal_urls_from_caption(caption: str) -> str:
    from backend.application.image_pipeline import strip_internal_urls_from_caption

    return strip_internal_urls_from_caption(caption)


def _make_browser_accessible_image_url(url: str) -> str:
    from backend.application.image_pipeline import normalize_image_url

    return normalize_image_url(url)


def _normalize_keywords_value(raw: str | list[str] | None) -> list[str]:
    """Normalize model-generated keyword inputs to list[str]."""
    if raw is None:
        return []
    if isinstance(raw, list):
        return [str(x).strip() for x in raw if str(x).strip()]

    text = str(raw).strip()
    if not text:
        return []

    if text.startswith("[") and text.endswith("]"):
        try:
            parsed = json.loads(text)
            if isinstance(parsed, list):
                return [str(x).strip() for x in parsed if str(x).strip()]
        except Exception:
            pass

    parts = re.split(r"[\n,，;；|]+", text)
    return [p.strip() for p in parts if p.strip()]


async def _search_single_kb_with_timeout(
    query: str,
    kb_id: str,
    top_k: int,
    chunk_top_k: int,
    timeout: int = KB_SEARCH_TIMEOUT,
    external_hl_keywords: list[str] | None = None,
    external_ll_keywords: list[str] | None = None,
    external_query_type: str | None = None,
    precomputed_query_embedding: Any | None = None,
    seed_weights: dict[str, float] | None = None,
    content_filter_mode: FilterMode | None = None,
    kb_label: str | None = None,
) -> dict:
    """搜索单个知识库（带超时保护）"""
    kb_label = kb_label or kb_id
    start_time = time.time()
    
    logger.info(f"[KB搜索] 开始搜索 {kb_label}: {query[:50]}...")
    
    try:
        result = await asyncio.wait_for(
            _search_single_kb(
                query,
                kb_id,
                top_k,
                chunk_top_k,
                external_hl_keywords=external_hl_keywords,
                external_ll_keywords=external_ll_keywords,
                external_query_type=external_query_type,
                precomputed_query_embedding=precomputed_query_embedding,
                seed_weights=seed_weights,
                content_filter_mode=content_filter_mode,
                kb_label=kb_label,
            ),
            timeout=timeout,
        )

        elapsed = time.time() - start_time
        logger.info(f"[KB搜索] {kb_label} 完成，耗时 {elapsed:.2f}s")
        
        result["elapsed_time"] = elapsed
        result["kb_label"] = kb_label
        return result
        
    except asyncio.TimeoutError:
        elapsed = time.time() - start_time
        logger.warning(f"[KB搜索] {kb_label} 超时 (>{timeout}s)，已跳过")
        return {
            "kb_id": kb_id,
            "kb_label": kb_label,
            "error": f"Timeout after {timeout}s",
            "success": False,
            "elapsed_time": elapsed,
        }
    except Exception as e:
        elapsed = time.time() - start_time
        logger.error(f"[KB搜索] {kb_label} 异常: {e}")
        return {
            "kb_id": kb_id,
            "kb_label": kb_label,
            "error": str(e),
            "success": False,
            "elapsed_time": elapsed,
        }


async def _search_single_kb(
    query: str,
    kb_id: str,
    top_k: int,
    chunk_top_k: int,
    external_hl_keywords: list[str] | None = None,
    external_ll_keywords: list[str] | None = None,
    external_query_type: str | None = None,
    precomputed_query_embedding: Any | None = None,
    seed_weights: dict[str, float] | None = None,
    content_filter_mode: FilterMode | None = None,
    kb_label: str | None = None,
) -> dict:
    """搜索单个知识库：走 retrieve_context（含内容过滤与上下文拼装）。"""
    kb_label = kb_label or kb_id
    total_started = time.time()
    try:
        from backend.rag.retrieval_pipeline import retrieve_context

        filtered_data, context_prompt, images, stats = await retrieve_context(
            query=query,
            kb_id=kb_id,
            top_k=top_k,
            chunk_top_k=chunk_top_k,
            external_hl_keywords=external_hl_keywords,
            external_ll_keywords=external_ll_keywords,
            external_query_type=external_query_type,
            precomputed_query_embedding=precomputed_query_embedding,
            seed_weights=seed_weights,
            disable_stage1_oversample=_MULTI_KB_DISABLE_STAGE1_OVERSAMPLE,
            content_filter_mode=content_filter_mode,
        )
        total_elapsed = time.time() - total_started
        chunks = list(filtered_data.chunks or []) if filtered_data else []

        logger.info(
            "[KB搜索] retrieve_context 完成 %s | total=%.2fs | chunks=%s",
            kb_label,
            total_elapsed,
            stats.get("chunks", len(chunks)),
        )

        return {
            "kb_id": kb_id,
            "kb_label": kb_label,
            "context": context_prompt or "",
            "images": images,
            "stats": stats,
            "chunks": chunks,
            "success": True,
            "elapsed_time": total_elapsed,
            "stage_timings": {
                "retrieve_context": round(total_elapsed, 3),
                "total": round(total_elapsed, 3),
            },
        }
    except Exception as e:
        total_elapsed = time.time() - total_started
        logger.exception("知识库 %s 检索失败: %s", kb_id, e)
        return {
            "kb_id": kb_id,
            "kb_label": kb_label,
            "error": str(e),
            "success": False,
            "elapsed_time": total_elapsed,
        }


async def _compute_query_embedding_for_kb(kb_id: str, query: str) -> Any | None:
    from backend.rag.service import get_rag_service

    rag_service = get_rag_service()
    rag = await rag_service._get_rag_instance(kb_id)
    if not rag or not rag.lightrag or not hasattr(rag.lightrag, "embedding_func"):
        return None

    embedding_func = rag.lightrag.embedding_func
    embedding_result = embedding_func([query])
    if asyncio.iscoroutine(embedding_result):
        embedding_result = await embedding_result

    if isinstance(embedding_result, list) and embedding_result:
        return embedding_result[0]
    if hasattr(embedding_result, "ndim"):
        return embedding_result[0] if getattr(embedding_result, "ndim", 1) > 1 else embedding_result
    return None


async def _extract_shared_keywords_once(
    kb_id: str,
    query: str,
    external_query_type: str | None = None,
    precomputed_query_embedding: Any | None = None,
) -> tuple[list[str], list[str], str | None, dict[str, float]]:
    """仅在首个 KB 上提取一次关键词与 seed_weights，供多库并发复用。"""
    from backend.rag.retrieval_pipeline import extract_keywords

    hl, ll, query_type, seed_weights = await extract_keywords(
        kb_id,
        query,
        external_query_type=external_query_type,
        precomputed_query_embedding=precomputed_query_embedding,
    )
    qt_value = query_type.value if hasattr(query_type, "value") else str(query_type)
    return hl, ll, qt_value, seed_weights


async def _prepare_shared_query_context(
    query: str,
    effective_kb_ids: list[str],
    external_query_type: str | None = None,
) -> tuple[list[str] | None, list[str] | None, str | None, dict[str, float] | None, Any | None]:
    """预计算意图、embedding 与关键词（单库一次），避免各 KB 重复提取。"""
    from backend.rag.keywords.extractor import detect_query_type

    query_type_task = None
    if external_query_type is None:
        query_type_task = asyncio.create_task(asyncio.to_thread(detect_query_type, query))

    embedding_task = None
    if effective_kb_ids:
        embedding_task = asyncio.create_task(
            _compute_query_embedding_for_kb(effective_kb_ids[0], query)
        )

    shared_query_type = external_query_type
    shared_query_embedding = None

    if query_type_task:
        detected = await query_type_task
        shared_query_type = detected.value

    if embedding_task:
        shared_query_embedding = await embedding_task

    shared_hl: list[str] | None = None
    shared_ll: list[str] | None = None
    shared_seed_weights: dict[str, float] | None = None
    if effective_kb_ids:
        shared_hl, shared_ll, extracted_qt, shared_seed_weights = await _extract_shared_keywords_once(
            effective_kb_ids[0],
            query,
            external_query_type=shared_query_type,
            precomputed_query_embedding=shared_query_embedding,
        )
        if shared_query_type is None:
            shared_query_type = extracted_qt

    return shared_hl, shared_ll, shared_query_type, shared_seed_weights, shared_query_embedding


def _apply_path_a_fast_keywords(
    hl_keywords: list[str] | None,
    ll_keywords: list[str] | None,
) -> tuple[list[str], list[str]]:
    """Path-A 加速：保留低级词供 Path-B/图扩展，高级词置空以减少 keyword_index 多路检索。"""
    if not _MULTI_KB_PATH_A_QUERY_ONLY:
        return list(hl_keywords or []), list(ll_keywords or [])
    return [], list(ll_keywords or [])


async def _prepare_shared_embedding_only(
    query: str,
    effective_kb_ids: list[str],
    external_query_type: str | None = None,
) -> tuple[str | None, Any | None]:
    """仅预计算意图与 query embedding（不重复关键词提取）。"""
    from backend.rag.keywords.extractor import detect_query_type

    shared_query_type = external_query_type
    if shared_query_type is None:
        detected = await asyncio.to_thread(detect_query_type, query)
        shared_query_type = detected.value

    shared_query_embedding = None
    if effective_kb_ids:
        shared_query_embedding = await _compute_query_embedding_for_kb(
            effective_kb_ids[0], query
        )

    return shared_query_type, shared_query_embedding


def _clean_image_blocks_in_context(context: str, registry: ImageURLRegistry) -> str:
    """将 context 中内嵌的 Image Content Analysis 块替换为 [IMAGE:xxx] token。

    chunk 内容以 `image_chunk` 模板存储：
        Image Content Analysis:
        Image Path: http://internal-minio/...
        Captions: 图1: ...
        Footnotes: ...
        Visual Analysis: ...

    这类块如果直接传给模型，模型会把 MinIO 内网 URL 当成可访问的图片地址输出，
    导致前端显示破损图片。此函数统一替换为已有的 [IMAGE:xxx] token 机制。
    """
    from backend.application.image_pipeline import replace_image_urls_for_model_context
    from backend.rag.media_blocks import IMAGE_BLOCK_RE, iter_image_blocks

    def _replace_block(m) -> str:
        parsed = iter_image_blocks(m.group(0))
        if not parsed:
            return ""
        block = parsed[0]
        raw_path = str(block.get("image_path") or "").strip()
        caption_raw = str(block.get("caption") or "").strip()
        visual = str(block.get("visual_analysis") or "").strip()
        media_ref = str(block.get("media_ref") or "").strip()
        media_title = str(block.get("media_title") or "").strip()
        # 清除图注中的内网 MinIO URL，避免传给模型后被原样输出
        caption = _strip_internal_urls_from_caption(caption_raw or media_title or media_ref)

        if not raw_path:
            return f"[图片（无路径）：{caption}]" if caption else ""

        # 转换为浏览器可访问的 URL
        url = _make_browser_accessible_image_url(raw_path)
        if not url:
            return f"[图片（无法访问）：{caption}]" if caption else ""

        # 注册到 [IMAGE:xxx] 注册表
        token = registry.register(url, caption or "图片")

        # 给模型的精简说明：token + 视觉描述（帮助模型判断是否与问题相关）
        desc_parts = [f"图注：{caption}"] if caption else []
        if media_ref and media_ref not in caption:
            desc_parts.append(f"图号：{media_ref}")
        if visual:
            desc_parts.append(f"内容描述：{visual[:120]}")
        desc = "，".join(desc_parts)
        return f"[图片 {token}｜{desc}]" if desc else f"[图片 {token}]"

    cleaned = IMAGE_BLOCK_RE.sub(_replace_block, context)
    return replace_image_urls_for_model_context(
        cleaned,
        register_image=registry.register,
    )


def _build_image_whitelist_section(all_images: list[dict], registry: ImageURLRegistry) -> str:
    if not all_images:
        return ""

    unique_images: list[dict] = []
    seen_paths: set[str] = set()
    for img in all_images:
        if not isinstance(img, dict):
            continue
        path = img.get("path", img.get("url"))
        if path and path not in seen_paths:
            seen_paths.add(path)
            unique_images.append(img)

    if not unique_images:
        return ""

    image_lines = []
    for idx, img in enumerate(unique_images[:8], start=1):
        url = (img.get("url") or "").strip()
        if not url:
            continue
        url = _make_browser_accessible_image_url(url)
        if not url:
            continue
        caption = _strip_internal_urls_from_caption(
            (img.get("caption") or img.get("title") or "").strip()
        )
        visual = (img.get("visual_analysis") or "").strip()
        token = registry.register(url, caption or f"图片{idx}")
        desc_parts = []
        if caption:
            desc_parts.append(f"图注：{caption}")
        if visual:
            desc_parts.append(f"内容：{visual[:120]}")
        if not desc_parts:
            desc_parts.append(f"图片{idx}")
        image_lines.append(f"- {token} | {' | '.join(desc_parts)}")

    if not image_lines:
        return ""

    return (
        "\n## 本轮可用图片白名单（仅允许使用以下标识符）\n\n"
        + "\n".join(image_lines)
        + "\n\n> **强制规则**：\n"
        "> 1. 你在本轮回答中只能使用上方白名单里明确列出的 `[IMAGE:xxx]` 标识符；"
        "凡是不在本列表中的 `[IMAGE:xxx]` 一律禁止使用。\n"
        "> 2. 引用图片时必须使用 `![图片说明]([IMAGE:xxx])` 格式，且其中的 `[IMAGE:xxx]` "
        "必须与白名单逐字一致，**不得改写、补号、猜测、递增或自行构造**。\n"
        "> 3. 只在回答内容与图片的图注/描述高度匹配时才引用，切勿在不相关段落中插图。\n"
        "> 4. **严禁**在回答中直接输出 MinIO URL、内网路径或任何非白名单中的图片地址。"
    )


def _build_stats_section(
    *,
    effective_kb_ids: list[str],
    active_kbs: int,
    failed_kbs: int,
    all_stats: dict[str, int],
    merged_block_count: int,
    chunk_top_k: int,
    total_time: float,
    timing_info: str,
    truncated: bool = False,
    total_fragments: int = 0,
) -> str:
    truncate_line = ""
    if truncated and total_fragments > merged_block_count:
        truncate_line = (
            f"- 片段截断: 召回 {total_fragments} 段，按 chunk 保留 {merged_block_count} 段\n"
        )
    return (
        f"\n## 检索统计\n"
        f"- 知识库数量: {len(effective_kb_ids)}\n"
        f"- 成功调用的知识库: {active_kbs}\n"
        f"- 失败的知识库: {failed_kbs}\n"
        f"- 实体: {all_stats['entities']}\n"
        f"- 关系: {all_stats['relationships']}\n"
        f"- 文本块: {merged_block_count}（融合后，上限 {chunk_top_k}）\n"
        f"{truncate_line}"
        f"- 总耗时: {total_time:.2f}s\n"
        f"- 各知识库耗时: {timing_info}"
    )


def _merge_results_rrf(
    results: list[dict],
    final_top_n: int = MAX_CHUNK_TOP_K,
    k: int = 60,
    max_context_chars: int | None = None,
    required_document_codes: list[tuple[str, str]] | None = None,
    query: str = "",
    _registry: Any = None,
) -> tuple[str, int, int, bool]:
    """跨库倒数排名融合 (RRF)。

    在所有成功返回的 ``chunks`` 上统一打分、排序，**全局**最多保留 ``final_top_n`` 条
    （与 ``chunk_top_k`` 一致，默认 4），避免双库各 4 段拼成 8 段。
    若某库仅有 ``context`` 无 ``chunks``，回退拼接该段 context（极少见）。

    Returns:
        (merged_context, kept_fragment_count, total_fragment_count, truncated)
    """
    if _registry is None:
        from backend.application.agent_executors.image_url_registry import get_registry
        _registry = get_registry()
    chunk_scores: dict[str, float] = {}
    chunk_contents: dict[str, dict] = {}
    context_fallback_parts: list[str] = []
    kb_display_names = {
        str(result.get("kb_label") or "").strip()
        for result in results
        if str(result.get("kb_label") or "").strip()
    }

    for result in results:
        if not result.get("success"):
            continue
        kb_id = result.get("kb_id", "unknown")
        kb_label = result.get("kb_label") or kb_id
        chunks = result.get("chunks") or []

        if chunks:
            for rank, chunk in enumerate(chunks):
                content = ""
                if isinstance(chunk, dict):
                    content = chunk.get("content", "")
                    chunk_id = str(chunk.get("id") or hash(content))
                    doc_name = _extract_doc_name_from_chunk(chunk, kb_label=kb_label)
                else:
                    content = str(chunk)
                    chunk_id = str(hash(content))
                    doc_name = kb_label

                unique_chunk_key = f"{kb_id}:{chunk_id}"
                score = 1.0 / (k + rank + 1)

                if unique_chunk_key not in chunk_scores:
                    chunk_scores[unique_chunk_key] = 0.0
                    clean_content = _clean_image_blocks_in_context(content or "", _registry)
                    chunk_contents[unique_chunk_key] = {
                        "content": clean_content,
                        "kb_id": kb_id,
                        "kb_label": kb_label,
                        "doc_name": doc_name,
                        "page_nums": _extract_page_nums_from_chunk(chunk),
                    }

                chunk_scores[unique_chunk_key] += score
        else:
            context = (result.get("context") or "").strip()
            if context:
                context = _clean_image_blocks_in_context(context, _registry)
                context_fallback_parts.append(f"### 片段（来自：{kb_label}）\n\n{context}")

    if chunk_scores:
        sorted_chunks = sorted(
            chunk_scores.items(),
            key=lambda x: x[1],
            reverse=True,
        )
        final_top_n = max(1, min(final_top_n, 50))
        target_matches: list[tuple[str, float]] = []
        target_misses: list[tuple[str, float]] = []
        required_codes = required_document_codes or []
        for item in sorted_chunks:
            doc_name = chunk_contents[item[0]].get("doc_name") or ""
            normalized_name = _normalize_document_code(doc_name)
            if required_codes and any(code in normalized_name for _display, code in required_codes):
                target_matches.append(item)
            else:
                target_misses.append(item)

        if target_matches:
            target_matches = _prioritize_explicit_document_evidence(
                target_matches,
                chunk_contents=chunk_contents,
                query=query,
            )

        selected = (target_matches + target_misses)[:final_top_n]
        if not (required_codes and target_matches):
            selected = _promote_query_supported_evidence(
                selected,
                chunk_contents=chunk_contents,
                query=query,
            )

        ref_block = _build_reference_block(
            selected,
            chunk_contents=chunk_contents,
            kb_display_names=kb_display_names,
        )

        fragment_entries: list[tuple[str, str]] = []
        for chunk_key, _score in selected:
            info = chunk_contents[chunk_key]
            doc_name = info.get("doc_name") or info.get("kb_label") or info["kb_id"]
            fragment_entries.append((
                _format_doc_reference(doc_name, info.get("page_nums") or ()),
                info["content"],
            ))

        kept_entries, truncated = _select_fragment_entries(
            ref_block,
            fragment_entries,
            max_context_chars,
        )
        fragment_parts = [
            _format_fragment(idx, doc_name, content)
            for idx, (doc_name, content) in enumerate(kept_entries, start=1)
        ]
        merged_parts = list(fragment_parts)
        if ref_block and kept_entries:
            with_ref = [ref_block, *fragment_parts]
            if max_context_chars is None or _joined_parts_len(with_ref) <= max_context_chars:
                merged_parts = with_ref

        match_notice = ""
        if required_codes:
            requested = "、".join(display for display, _code in required_codes)
            if target_matches:
                match_notice = f"{_TARGET_DOCUMENT_MATCH_TRUE}\n\n> **目标文档已命中**：来源文档包含 {requested}。"
            else:
                match_notice = (
                    f"{_TARGET_DOCUMENT_MATCH_FALSE}\n\n"
                    f"> **目标文档未命中**：查询明确指定 {requested}，但本轮实际来源文档不包含该标准。"
                    "以下内容仅为相关资料，不能作为该标准限值依据；若已启用联网搜索，应继续检索权威公开来源。"
                )
        if match_notice:
            merged_parts.insert(0, match_notice)

        return _CHUNK_SEPARATOR.join(merged_parts), len(fragment_parts), len(fragment_entries), truncated

    if context_fallback_parts:
        kept, truncated = _fit_whole_parts(context_fallback_parts, max_context_chars)
        return _CHUNK_SEPARATOR.join(kept), len(kept), len(context_fallback_parts), truncated

    return "", 0, 0, False


def _merge_results_weighted(
    results: list[dict],
    final_top_n: int = MAX_CHUNK_TOP_K,
    kb_weights: dict[str, float] | None = None,
    max_context_chars: int | None = None,
    required_document_codes: list[tuple[str, str]] | None = None,
    query: str = "",
    _registry: Any = None,
) -> tuple[str, int, int, bool]:
    """跨库加权融合。

    按每个 chunk 的 ``_fused_score`` 库内 Min-Max 归一化后再乘以该库权重进行排序，
    全局最多保留 ``final_top_n`` 条。

    Args:
        results: 各库返回 of result dict（含 chunks 列表）
        final_top_n: 最终保留 of chunk 条数
        kb_weights: kb_id → 权重映射；未指定的库默认等权 1.0
        max_context_chars: 上下文字符预算
        required_document_codes: 目标文档匹配码

    Returns:
        (merged_context, kept_fragment_count, total_fragment_count, truncated)
    """
    if _registry is None:
        from backend.application.agent_executors.image_url_registry import get_registry
        _registry = get_registry()
    if kb_weights is None:
        kb_weights = {}

    chunk_scores: dict[str, float] = {}
    chunk_contents: dict[str, dict] = {}
    context_fallback_parts: list[str] = []
    kb_display_names = {
        str(result.get("kb_label") or "").strip()
        for result in results
        if str(result.get("kb_label") or "").strip()
    }

    for result in results:
        if not result.get("success"):
            continue
        kb_id = result.get("kb_id", "unknown")
        kb_label = result.get("kb_label") or kb_id
        weight = kb_weights.get(kb_id, 1.0)
        chunks = result.get("chunks") or []

        if chunks:
            # 1. Score Calibration: Extract raw scores and apply Min-Max normalization per KB
            raw_scores = []
            for chunk in chunks:
                if isinstance(chunk, dict):
                    raw_scores.append(float(chunk.get("_fused_score", chunk.get("_ppr_score", 0.0))))
                else:
                    raw_scores.append(0.0)

            max_s = max(raw_scores) if raw_scores else 0.0
            min_s = min(raw_scores) if raw_scores else 0.0
            diff = max_s - min_s

            for idx, chunk in enumerate(chunks):
                content = ""
                if isinstance(chunk, dict):
                    content = chunk.get("content", "")
                    chunk_id = str(chunk.get("id") or hash(content))
                    doc_name = _extract_doc_name_from_chunk(chunk, kb_label=kb_label)
                    fused_score = raw_scores[idx]
                else:
                    content = str(chunk)
                    chunk_id = str(hash(content))
                    doc_name = kb_label
                    fused_score = 0.0

                # Normalization to [0, 1] range
                if diff > 1e-6:
                    calibrated_score = (fused_score - min_s) / diff
                else:
                    calibrated_score = 1.0 if max_s > 0.0 else 0.0

                unique_chunk_key = f"{kb_id}:{chunk_id}"
                weighted_score = calibrated_score * weight

                if unique_chunk_key not in chunk_scores:
                    chunk_scores[unique_chunk_key] = weighted_score
                    clean_content = _clean_image_blocks_in_context(content or "", _registry)
                    chunk_contents[unique_chunk_key] = {
                        "content": clean_content,
                        "kb_id": kb_id,
                        "kb_label": kb_label,
                        "doc_name": doc_name,
                        "page_nums": _extract_page_nums_from_chunk(chunk),
                    }
                else:
                    # 同一 chunk 出现在一个以上库中，取最高加权分
                    chunk_scores[unique_chunk_key] = max(
                        chunk_scores[unique_chunk_key], weighted_score
                    )
        else:
            context = (result.get("context") or "").strip()
            if context:
                context = _clean_image_blocks_in_context(context, _registry)
                context_fallback_parts.append(f"### 片段（来自：{kb_label}）\n\n{context}")

    if chunk_scores:
        sorted_chunks = sorted(
            chunk_scores.items(),
            key=lambda x: x[1],
            reverse=True,
        )
        # 2. Adaptive Top-K: Density-based Truncation (Detect score drop-off point)
        final_top_n = max(1, min(final_top_n, 50))
        truncation_point = final_top_n
        if len(sorted_chunks) > 4:
            max_score = sorted_chunks[0][1]
            if max_score > 0.0:
                for i in range(3, min(len(sorted_chunks) - 1, final_top_n)):
                    current_score = sorted_chunks[i][1]
                    next_score = sorted_chunks[i+1][1]
                    drop = current_score - next_score
                    # Threshold: if drop is greater than 30% of the max score, dynamically truncate
                    if drop > 0.3 * max_score:
                        truncation_point = i + 1
                        logger.info(
                            "[自适应Top-K] 检测到分数断崖式下跌: index=%s, 当前分=%.3f, 下一分=%.3f, 降幅=%.3f (阈值=%.3f), 动态截断至 %s 条",
                            i, current_score, next_score, drop, 0.3 * max_score, truncation_point
                        )
                        break
        final_top_n = truncation_point
        sorted_chunks = sorted_chunks[:final_top_n]

        target_matches: list[tuple[str, float]] = []
        target_misses: list[tuple[str, float]] = []
        required_codes = required_document_codes or []
        for item in sorted_chunks:
            doc_name = chunk_contents[item[0]].get("doc_name") or ""
            normalized_name = _normalize_document_code(doc_name)
            if required_codes and any(code in normalized_name for _display, code in required_codes):
                target_matches.append(item)
            else:
                target_misses.append(item)

        if target_matches:
            target_matches = _prioritize_explicit_document_evidence(
                target_matches,
                chunk_contents=chunk_contents,
                query=query,
            )

        selected = (target_matches + target_misses)[:final_top_n]
        if not (required_codes and target_matches):
            selected = _promote_query_supported_evidence(
                selected,
                chunk_contents=chunk_contents,
                query=query,
            )

        ref_block = _build_reference_block(
            selected,
            chunk_contents=chunk_contents,
            kb_display_names=kb_display_names,
        )

        fragment_entries: list[tuple[str, str]] = []
        for chunk_key, _score in selected:
            info = chunk_contents[chunk_key]
            doc_name = info.get("doc_name") or info.get("kb_label") or info["kb_id"]
            fragment_entries.append((
                _format_doc_reference(doc_name, info.get("page_nums") or ()),
                info["content"],
            ))

        kept_entries, truncated = _select_fragment_entries(
            ref_block,
            fragment_entries,
            max_context_chars,
        )
        fragment_parts = [
            _format_fragment(idx, doc_name, content)
            for idx, (doc_name, content) in enumerate(kept_entries, start=1)
        ]
        merged_parts = list(fragment_parts)
        if ref_block and kept_entries:
            with_ref = [ref_block, *fragment_parts]
            if max_context_chars is None or _joined_parts_len(with_ref) <= max_context_chars:
                merged_parts = with_ref

        match_notice = ""
        if required_codes:
            requested = "、".join(display for display, _code in required_codes)
            if target_matches:
                match_notice = f"{_TARGET_DOCUMENT_MATCH_TRUE}\n\n> **目标文档已命中**：来源文档包含 {requested}。"
            else:
                match_notice = (
                    f"{_TARGET_DOCUMENT_MATCH_FALSE}\n\n"
                    f"> **目标文档未命中**：查询明确指定 {requested}，但本轮实际来源文档不包含该标准。"
                    "以下内容仅为相关资料，不能作为该标准限值依据；若已启用联网搜索，应继续检索权威公开来源。"
                )
        if match_notice:
            merged_parts.insert(0, match_notice)

        return _CHUNK_SEPARATOR.join(merged_parts), len(fragment_parts), len(fragment_entries), truncated

    if context_fallback_parts:
        kept, truncated = _fit_whole_parts(context_fallback_parts, max_context_chars)
        return _CHUNK_SEPARATOR.join(kept), len(kept), len(context_fallback_parts), truncated

    return "", 0, 0, False


@tool("multi_kb_search")
async def multi_kb_search_tool(
    query: str,
    high_level_keywords: str | list[str] | None = None,
    low_level_keywords: str | list[str] | None = None,
    query_type: str | None = None,
    kb_ids: Annotated[list[str] | None, InjectedToolArg] = None,
    top_k: int = _TOOL_DEFAULT_TOP_K,
    chunk_top_k: int = _TOOL_DEFAULT_CHUNK_TOP_K,
    merge_strategy: str = "rrf",
    config: RunnableConfig = None,
) -> str:
    """搜索多个知识库并返回融合后的上下文。

    普通模式可只传 query；也支持额外传入高/低层关键词，跳过自动关键词提取。
    """
    normalized_hl = _normalize_keywords_value(high_level_keywords) if high_level_keywords is not None else None
    normalized_ll = _normalize_keywords_value(low_level_keywords) if low_level_keywords is not None else None

    registry = None
    if config and isinstance(config, dict):
        registry = config.get("configurable", {}).get("image_url_registry")

    return await execute_multi_kb_search(
        query=query,
        kb_ids=kb_ids,
        top_k=top_k,
        chunk_top_k=chunk_top_k,
        merge_strategy=merge_strategy,
        external_hl_keywords=normalized_hl,
        external_ll_keywords=normalized_ll,
        external_query_type=query_type,
        _registry=registry,
    )


async def execute_multi_kb_search(
    query: str,
    kb_ids: list[str] | None = None,
    top_k: int = _TOOL_DEFAULT_TOP_K,
    chunk_top_k: int = _TOOL_DEFAULT_CHUNK_TOP_K,
    merge_strategy: str = "rrf",
    external_hl_keywords: str | list[str] | None = None,
    external_ll_keywords: str | list[str] | None = None,
    external_query_type: str | None = None,
    budget_key: str = "multi_kb_search",
    content_filter_mode: FilterMode | None = None,
    max_result_chars: int | None = None,
    allow_similar_cache: bool = True,
    _structured: bool = False,
    _force_local: bool = False,
    _registry: Any = None,
) -> Any:
    """搜索多个知识库获取相关信息。

    同时检索多个知识库，使用倒数排名融合算法合并结果。
    未显式提供知识库时，读取 MySQL 中启用的默认知识库。

    Args:
        query: 搜索查询内容
        kb_ids: 知识库ID列表；未提供时使用 MySQL 中启用的默认知识库
        top_k: 每个知识库阶段1实体/文档召回上限（与路由 retrieve_context 一致），默认见 _TOOL_DEFAULT_TOP_K
        chunk_top_k: 融合前进内容块的期望条数上限，默认见 _TOOL_DEFAULT_CHUNK_TOP_K
        merge_strategy: 合并策略，rrf(倒数排名融合) 或 weighted(加权)
        content_filter_mode: 内容过滤流水线；None 时使用 KB Profile（domain 库为 MMR_ONLY）
    """
    from backend.core.tools.request_limits import (
        TURN_MAX_EVIDENCE_CHARS,
        consume_tool_budget,
        get_retrieval_request_cache,
        get_turn_kb_chars_remaining,
    )

    if _registry is None:
        from backend.application.agent_executors.image_url_registry import get_registry
        _registry = get_registry()

    # 移除查询中的运行时元数据（如 <runtime_current_time> 块）
    query = re.sub(r"<runtime_current_time>[\s\S]*?</runtime_current_time>", "", query, flags=re.IGNORECASE).strip()

    top_k = max(1, min(top_k, MAX_TOP_K))
    chunk_top_k = max(1, min(chunk_top_k, MAX_CHUNK_TOP_K))
    from backend.application.knowledge_bases.catalog_service import (
        load_knowledge_base_catalog,
    )

    catalog_entries = await load_knowledge_base_catalog(
        kb_ids,
        defaults_only=kb_ids is None,
    )
    effective_kb_ids = [entry.kb_id for entry in catalog_entries]
    kb_labels = {entry.kb_id: entry.kb_name for entry in catalog_entries}
    if not effective_kb_ids:
        scope = "默认" if kb_ids is None else "所选"
        text = f"MySQL 中没有可用的{scope}知识库，请检查知识库状态和智能体绑定。"
        return _structured_result(text) if _structured else text

    # Count every accepted tool invocation, including cache hits and
    # budget-exhausted retries. Provider-only accounting lets a model keep
    # issuing no-op calls forever after the context budget is full.
    allowed, _remaining = consume_tool_budget(budget_key)
    if not allowed:
        logger.warning("[多KB搜索] 已达到本轮调用上限，跳过重复检索")
        text = "知识库检索次数已达上限，请基于已有检索结果回答。"
        result = _structured_result(
            text,
            stats={"tool_budget_exhausted": True, "injected": False},
        )
        return result if _structured else text

    normalized_hl_keywords = _normalize_keywords_value(external_hl_keywords) if external_hl_keywords is not None else None
    normalized_ll_keywords = _normalize_keywords_value(external_ll_keywords) if external_ll_keywords is not None else None

    # 未显式传入关键词时：共享单库提取 + Path-A 仅整句（见 _apply_path_a_fast_keywords）。
    user_provided_keywords = (
        external_hl_keywords is not None or external_ll_keywords is not None
    )
    if not user_provided_keywords:
        normalized_hl_keywords = None
        normalized_ll_keywords = None

    turn_remaining = get_turn_kb_chars_remaining(TURN_MAX_EVIDENCE_CHARS)
    if turn_remaining <= 0:
        logger.warning("[多KB搜索] 本轮检索上下文字符预算已用尽")
        text = (
            f"本轮知识库检索上下文已达上限（约 {TURN_MAX_EVIDENCE_CHARS} 字），"
            "请基于已有检索结果回答，勿再调用 multi_kb_search。"
        )
        result = _context_budget_exhausted_result(text)
        return result if _structured else text

    if max_result_chars and max_result_chars > 0:
        requested_result_chars = max_result_chars
    elif MAX_RESULT_CHARS_FULL_AGENT > 0:
        requested_result_chars = MAX_RESULT_CHARS_FULL_AGENT
    else:
        requested_result_chars = None
    max_result_chars = (
        min(requested_result_chars, turn_remaining)
        if requested_result_chars is not None
        else None
    )

    cache = get_retrieval_request_cache()
    cache_key = _build_request_cache_key(
        query=query,
        kb_ids=effective_kb_ids,
        top_k=top_k,
        chunk_top_k=chunk_top_k,
        merge_strategy=merge_strategy,
        external_hl_keywords=normalized_hl_keywords,
        external_ll_keywords=normalized_ll_keywords,
        external_query_type=external_query_type,
        user_provided_keywords=user_provided_keywords,
        content_filter_mode=content_filter_mode,
        # Cache identity reflects the caller's stable output policy, not the
        # momentary remaining allowance. The latter only affects injection.
        max_result_chars=requested_result_chars,
    )
    cached_value = cache.get_exact(cache_key)
    if cached_value is not None:
        reused = _cached_reuse_result(cached_value, "exact")
        logger.info("[多KB搜索] 命中请求级精确缓存")
        return reused if _structured else _structured_text(reused)
    similar_value = cache.get_similar(cache_key) if allow_similar_cache else None
    if similar_value is not None:
        reused = _cached_reuse_result(similar_value, "similar")
        logger.info("[多KB搜索] 命中请求级相似查询缓存")
        return reused if _structured else _structured_text(reused)

    current_thread = threading.current_thread().name
    in_subagent_thread = current_thread.startswith("subagent-exec-")
    kb_timeout = SUBAGENT_KB_SEARCH_TIMEOUT if in_subagent_thread else KB_SEARCH_TIMEOUT
    total_timeout = SUBAGENT_TOTAL_SEARCH_TIMEOUT if in_subagent_thread else TOTAL_SEARCH_TIMEOUT

    # Deduplicate identical in-flight retrieval calls in the same request scope.
    loop_id = id(asyncio.get_running_loop())
    existing_request_task = cache.get_in_flight(cache_key, loop_id=loop_id)
    if existing_request_task is not None and not existing_request_task.done():
        logger.info("[多KB搜索] 复用同事件循环内进行中的相同检索")
        in_flight_value = _cached_reuse_result(
            await asyncio.shield(existing_request_task),
            "in_flight",
        )
        return (
            in_flight_value
            if _structured
            else _structured_text(in_flight_value)
        )

    async def run_with_provider_capacity() -> dict[str, Any]:
        await cache.acquire_provider_slot()
        try:
            if cache.provider_requests_cancelled():
                return _structured_result(
                    "本次请求的知识库检索已取消，未继续执行排队中的检索。",
                    stats={"cancelled": True},
                )
            cached_after_wait = cache.get_exact(cache_key)
            if cached_after_wait is not None:
                logger.info("[多KB搜索] 等待 provider 容量后命中请求级精确缓存")
                return _cached_reuse_result(cached_after_wait, "exact")
            similar_after_wait = (
                cache.get_similar(cache_key) if allow_similar_cache else None
            )
            if similar_after_wait is not None:
                logger.info("[多KB搜索] 等待 provider 容量后命中请求级相似缓存")
                return _cached_reuse_result(similar_after_wait, "similar")
            rag_service_url = _rag_service_base_url()
            if rag_service_url and not _force_local:
                logger.info("[多KB搜索] 转发到独立知识库服务: %s", rag_service_url)
                return await _execute_remote_multi_kb_search(
                    base_url=rag_service_url,
                    query=query,
                    kb_ids=effective_kb_ids,
                    top_k=top_k,
                    chunk_top_k=chunk_top_k,
                    merge_strategy=merge_strategy,
                    external_hl_keywords=normalized_hl_keywords,
                    external_ll_keywords=normalized_ll_keywords,
                    external_query_type=external_query_type,
                    content_filter_mode=content_filter_mode,
                    max_result_chars=max_result_chars,
                    _registry=_registry,
                )
            return await _execute_multi_kb_search_once(
                query=query,
                effective_kb_ids=effective_kb_ids,
                top_k=top_k,
                chunk_top_k=chunk_top_k,
                merge_strategy=merge_strategy,
                kb_timeout=kb_timeout,
                total_timeout=total_timeout,
                external_hl_keywords=normalized_hl_keywords,
                external_ll_keywords=normalized_ll_keywords,
                external_query_type=external_query_type,
                user_provided_keywords=user_provided_keywords,
                content_filter_mode=content_filter_mode,
                max_result_chars=max_result_chars,
                kb_labels=kb_labels,
                _registry=_registry,
            )
        finally:
            cache.release_provider_slot()

    run_task = asyncio.create_task(run_with_provider_capacity())
    cache.set_in_flight(cache_key, loop_id=loop_id, task=run_task)
    try:
        result = _fit_result_to_output_limit(await run_task, max_result_chars)
        if bool((result.get("stats") or {}).get("cache_reused")):
            return result if _structured else _structured_text(result)
        # Cache provider work even when another concurrent branch consumed the
        # final context slice while this retrieval was running.
        cache.set_exact(cache_key, result)
        injected = _reserve_result_for_turn(result, TURN_MAX_EVIDENCE_CHARS)
        if injected is None:
            text = "本轮剩余检索上下文预算不足，检索结果未注入。"
            exhausted = _context_budget_exhausted_result(text)
            return exhausted if _structured else text
        return injected if _structured else _structured_text(injected)
    finally:
        cache.clear_in_flight(cache_key, run_task)

async def execute_multi_kb_search_structured(
    **kwargs: Any,
) -> dict[str, Any]:
    """Internal structured variant; public tool output remains a string."""
    from backend.application.agent_executors.image_url_registry import get_registry
    registry = get_registry()
    result = await execute_multi_kb_search(**kwargs, _structured=True, _registry=registry)
    res = _ensure_structured_result(result)
    registered_images = []
    for token, entry in registry.iter_entries():
        registered_images.append({
            "token": token,
            "url": entry.url,
            "caption": entry.caption,
        })
    res["registered_images"] = registered_images
    return res


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


def _cached_reuse_result(value: Any, hit: str) -> dict[str, Any]:
    """Reuse structured evidence without injecting the same large text twice."""

    result = _mark_structured_cache_hit(value, hit)
    reused = dict(result)
    reused["text"] = (
        "本轮已存在等价知识库证据，已复用结构化证据记录，"
        "未再次占用检索上下文预算。"
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
                "content_filter_mode": content_filter_mode.value if content_filter_mode else "profile",
                "max_result_chars": int(max_result_chars or 0),
            },
        },
        ensure_ascii=False,
        sort_keys=True,
        separators=(",", ":"),
    )


async def _execute_multi_kb_search_once(
    query: str,
    effective_kb_ids: list[str],
    top_k: int,
    chunk_top_k: int,
    merge_strategy: str,
    kb_timeout: int,
    total_timeout: int,
    external_hl_keywords: list[str] | None = None,
    external_ll_keywords: list[str] | None = None,
    external_query_type: str | None = None,
    user_provided_keywords: bool = False,
    content_filter_mode: FilterMode | None = None,
    max_result_chars: int | None = None,
    kb_labels: dict[str, str] | None = None,
    _registry: Any = None,
) -> dict[str, Any]:
    total_start_time = time.time()
    if _registry is None:
        from backend.application.agent_executors.image_url_registry import get_registry
        _registry = get_registry()
    kb_labels = dict(kb_labels or {})
    logger.info(
        "[多KB搜索] 开始并发查询 %d 个知识库: %s... (top_k=%s, chunk_top_k=%s, kb_timeout=%ss, total_timeout=%ss, path_a_fast=%s, filter=%s)",
        len(effective_kb_ids),
        query[:50],
        top_k,
        chunk_top_k,
        kb_timeout,
        total_timeout,
        _MULTI_KB_PATH_A_QUERY_ONLY,
        content_filter_mode.value if content_filter_mode else "profile",
    )

    shared_query_type: str | None = external_query_type
    shared_query_embedding: Any | None = None
    shared_seed_weights: dict[str, float] | None = None
    per_kb_hl = external_hl_keywords
    per_kb_ll = external_ll_keywords

    if not user_provided_keywords:
        try:
            (
                shared_hl,
                shared_ll,
                shared_query_type,
                shared_seed_weights,
                shared_query_embedding,
            ) = await _prepare_shared_query_context(
                query=query,
                effective_kb_ids=effective_kb_ids,
                external_query_type=shared_query_type,
            )
            per_kb_hl, per_kb_ll = _apply_path_a_fast_keywords(shared_hl, shared_ll)
            logger.info(
                "[多KB搜索] 共享关键词: hl=%d→%d, ll=%d, path_a_query_only=%s",
                len(shared_hl or []),
                len(per_kb_hl),
                len(per_kb_ll),
                _MULTI_KB_PATH_A_QUERY_ONLY,
            )
        except Exception as e:
            logger.warning(f"[多KB搜索] 共享关键词/embedding 预计算失败，回退逐KB计算: {e}")
    else:
        try:
            shared_query_type, shared_query_embedding = await _prepare_shared_embedding_only(
                query=query,
                effective_kb_ids=effective_kb_ids,
                external_query_type=shared_query_type,
            )
        except Exception as e:
            logger.warning(f"[多KB搜索] 共享 embedding 预计算失败: {e}")

    if user_provided_keywords and _MULTI_KB_PATH_A_QUERY_ONLY:
        per_kb_hl, per_kb_ll = _apply_path_a_fast_keywords(per_kb_hl, per_kb_ll)

    tasks = [
        _search_single_kb_with_timeout(
            query,
            kb_id,
            top_k,
            chunk_top_k,
            timeout=kb_timeout,
            external_hl_keywords=per_kb_hl,
            external_ll_keywords=per_kb_ll,
            external_query_type=shared_query_type,
            precomputed_query_embedding=shared_query_embedding,
            seed_weights=shared_seed_weights,
            content_filter_mode=content_filter_mode,
            kb_label=kb_labels.get(kb_id, kb_id),
        )
        for kb_id in effective_kb_ids
    ]

    try:
        results = await asyncio.wait_for(
            asyncio.gather(*tasks),
            timeout=total_timeout,
        )
    except asyncio.TimeoutError:
        logger.error(f"[多KB搜索] 总查询超时 (>{total_timeout}s)")
        return _structured_result(f"搜索超时，请稍后重试。查询: {query[:50]}...")

    all_images: list[dict] = []
    all_stats = {"entities": 0, "relationships": 0, "chunks": 0}
    active_kbs = 0
    failed_kbs = 0
    total_elapsed = 0.0
    kb_timings = []

    for kb_result in results:
        elapsed = kb_result.get("elapsed_time", 0.0)
        kb_label = kb_result.get("kb_label", kb_result.get("kb_id", "unknown"))
        kb_timings.append(f"{kb_label}: {elapsed:.2f}s")
        
        if kb_result.get("success"):
            active_kbs += 1
            total_elapsed += elapsed
            if kb_result.get("images"):
                all_images.extend(kb_result["images"])
            stats = kb_result.get("stats", {})
            all_stats["entities"] += stats.get("entities", 0)
            all_stats["relationships"] += stats.get("relationships", 0)
            all_stats["chunks"] += stats.get("chunks", 0)
        else:
            failed_kbs += 1
            error = kb_result.get("error", "Unknown error")
            logger.warning(f"[多KB搜索] {kb_label} 失败: {error}")

    image_section = _build_image_whitelist_section(all_images, _registry)
    total_time = time.time() - total_start_time
    timing_info = ", ".join(kb_timings)
    context_budget = _estimate_context_char_budget(
        max_result_chars,
        active_kbs=active_kbs,
    )

    if merge_strategy == "weighted":
        merged_context, merged_block_count, total_fragments, context_truncated = _merge_results_weighted(
            results,
            final_top_n=chunk_top_k,
            kb_weights=None,
            max_context_chars=context_budget,
            required_document_codes=_extract_explicit_document_codes(query),
            query=query,
            _registry=_registry,
        )
    else:
        merged_context, merged_block_count, total_fragments, context_truncated = _merge_results_rrf(
            results,
            final_top_n=chunk_top_k,
            max_context_chars=context_budget,
            required_document_codes=_extract_explicit_document_codes(query),
            query=query,
            _registry=_registry,
        )

    result_parts: list[str] = []

    if merged_context:
        result_parts.append(
            f"## 检索到的上下文 (来自 {active_kbs} 个活跃知识库)\n\n{merged_context}"
        )
        if context_truncated and max_result_chars:
            dropped = max(0, total_fragments - merged_block_count)
            result_parts.append(
                _TRUNCATION_NOTE.format(
                    limit=max_result_chars,
                    kept=merged_block_count,
                    dropped=dropped,
                )
            )

    if image_section:
        result_parts.append(image_section)

    result_parts.append(
        _build_stats_section(
            effective_kb_ids=effective_kb_ids,
            active_kbs=active_kbs,
            failed_kbs=failed_kbs,
            all_stats=all_stats,
            merged_block_count=merged_block_count,
            chunk_top_k=chunk_top_k,
            total_time=total_time,
            timing_info=timing_info,
            truncated=context_truncated,
            total_fragments=total_fragments,
        )
    )

    logger.info(
        f"[多KB搜索] 完成: 成功={active_kbs}, 失败={failed_kbs}, "
        f"总耗时={total_time:.2f}s, chunks={merged_block_count}/{total_fragments}, "
        f"truncated={context_truncated}"
    )

    result = "\n".join(result_parts) if result_parts else "未找到相关信息"
    from backend.application.image_pipeline import replace_image_urls_for_model_context

    result = replace_image_urls_for_model_context(
        result,
        register_image=_registry.register,
    )
    output_budget_truncated = bool(
        max_result_chars
        and max_result_chars > 0
        and len(result) > max_result_chars
    )
    if output_budget_truncated:
        result = _truncate_kb_result_text(result, max_result_chars)
    evidence_records = _build_raw_chunk_evidence_records(
        results,
        per_kb_limit=chunk_top_k,
        query=query,
        _registry=_registry,
    )
    return _structured_result(
        result,
        evidence_records=evidence_records,
        stats={
            "active_kbs": active_kbs,
            "failed_kbs": failed_kbs,
            "total_fragments": total_fragments,
            "selected_fragments": merged_block_count,
            "output_budget_truncated": output_budget_truncated,
        },
    )


def _build_raw_chunk_evidence_records(
    results: list[dict[str, Any]],
    *,
    per_kb_limit: int,
    query: str = "",
    _registry: Any = None,
) -> list[dict[str, Any]]:
    from backend.core.agents.context.ledger import make_evidence_record

    records: list[dict[str, Any]] = []
    for result in results:
        if not result.get("success"):
            continue
        kb_id = str(result.get("kb_id") or "unknown")
        kb_label = str(result.get("kb_label") or kb_id)
        candidates: list[dict[str, Any]] = []
        for rank, chunk in enumerate(
            (result.get("chunks") or [])[: max(1, per_kb_limit)],
            start=1,
        ):
            if isinstance(chunk, dict):
                content = str(chunk.get("content") or "")
                chunk_id = str(chunk.get("id") or f"rank-{rank}")
                score_value = chunk.get("score")
                score = float(score_value) if isinstance(score_value, (int, float)) else 1.0 / rank
                doc_title = _extract_doc_name_from_chunk(chunk, kb_label=kb_label)
                page_nums = _extract_page_nums_from_chunk(chunk)
            else:
                content = str(chunk or "")
                chunk_id = f"rank-{rank}"
                score = 1.0 / rank
                doc_title = kb_label
                page_nums = ()
            if _registry is None:
                from backend.application.agent_executors.image_url_registry import get_registry
                _registry = get_registry()
            content = _clean_image_blocks_in_context(content, _registry)
            if not content.strip():
                continue
            candidates.append(
                {
                    "key": f"{kb_id}:{rank}:{chunk_id}",
                    "chunk_id": chunk_id,
                    "doc_title": _format_doc_reference(doc_title, page_nums),
                    "content": content,
                    "score": score,
                }
            )

        if not candidates:
            continue
        original_items = [
            (str(candidate["key"]), float(candidate["score"]))
            for candidate in candidates
        ]
        candidate_contents = {
            str(candidate["key"]): {"content": candidate["content"]}
            for candidate in candidates
        }
        ordered_items = _promote_query_supported_evidence(
            original_items,
            chunk_contents=candidate_contents,
            query=query,
        )
        moved_primary = (
            ordered_items[0][0]
            if ordered_items and ordered_items[0][0] != original_items[0][0]
            else None
        )
        maximum_score = max(score for _key, score in original_items)
        by_key = {str(candidate["key"]): candidate for candidate in candidates}
        for key, original_score in ordered_items:
            candidate = by_key[key]
            score = original_score
            if key == moved_primary:
                score = maximum_score + max(abs(maximum_score) * 0.01, 0.01)
            records.append(
                make_evidence_record(
                    source_tool="multi_kb_search",
                    source_id=kb_id,
                    chunk_id=str(candidate["chunk_id"]),
                    doc_title=str(candidate["doc_title"]),
                    content=str(candidate["content"]),
                    category="retrieval",
                    score=score,
                    kind="retrieval",
                    tags=[
                        "knowledge_base",
                        "raw_chunk",
                        *(
                            ["query_supported_primary"]
                            if key == moved_primary
                            else []
                        ),
                    ],
                )
            )
    return records
