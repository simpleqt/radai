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

logger = logging.getLogger(__name__)

DEFAULT_KB_ID = os.getenv("NE_DEFAULT_KB_ID", "")  # 核电子学知识库
PAPER_KB_ID = os.getenv("NE_PAPER_KB_ID", "")  # 期刊论文库

KB_LABELS = {
    DEFAULT_KB_ID: "核电子学库",
    PAPER_KB_ID: "期刊论文库",
}

# 禁止作为文献引用展示的知识库/容器名称（非具体文章名）
_KB_DISPLAY_NAMES = frozenset(KB_LABELS.values())


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
    if display in _KB_DISPLAY_NAMES:
        return ""
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
    0, int(os.getenv("MULTI_KB_SEARCH_MAX_RESULT_CHARS", "5000"))
)
# 非轻量普通模式每轮 multi_kb_search 最大调用次数
NORMAL_MAX_CALLS_PER_TURN = max(
    1, int(os.getenv("MULTI_KB_SEARCH_NORMAL_MAX_CALLS", "2"))
)
# 单次检索 chunk 正文的字符预算（不含图片白名单等元数据）
_EVIDENCE_BODY_CHAR_BUDGET = max(
    500, int(os.getenv("MULTI_KB_SEARCH_EVIDENCE_BODY_CHARS", "4500"))
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


def _normalize_document_code(value: str) -> str:
    return re.sub(r"[^A-Z0-9]", "", str(value or "").upper())


def _extract_explicit_document_codes(query: str) -> list[tuple[str, str]]:
    """Extract standard/document codes that the user explicitly requires."""
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
    return found


def _joined_parts_len(parts: list[str]) -> int:
    if not parts:
        return 0
    return sum(len(part) for part in parts) + len(_CHUNK_SEPARATOR) * (len(parts) - 1)


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
            kept.append(part)
            truncated = len(parts) > 1
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
            kept.append((doc_name, content))
            truncated = len(entries) > 1
            break
        truncated = True
        break

    if len(kept) < len(entries) and not truncated:
        truncated = True
    return kept, truncated


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
    """从图注文本中移除含内网 MinIO URL 的括号引用块，避免模型将内部地址输出给用户。

    处理两类情况：
    1. 整括号内含 MinIO URL，例如：
       「（注：此处引用 http://IP:PORT/files/... 对应文献中的Figure X.X，...）」
       → 整个括号块连同内容一起删除
    2. 括号外裸露的 MinIO URL
       → 只删除 URL 本身
    """
    import re as _re
    if not caption:
        return caption

    _MINIO_URL_RE = r'https?://[^\s\)\]）"\']+/files/[^\s\)\]）"\']*'

    # 步骤 1：删除包含 MinIO URL 的整个括号块（中文全角括号或半角括号）
    # 匹配 （...URL...） 或 (...URL...)，括号内可含任意非括号字符
    cleaned = _re.sub(
        r'[（(][^（(）)]*' + _MINIO_URL_RE + r'[^（(）)]*[）)]',
        '',
        caption,
    )

    # 步骤 2：清除残余的裸露 MinIO URL（括号外的情况）
    cleaned = _re.sub(_MINIO_URL_RE, '', cleaned)

    # 步骤 3：清理多余的空白和标点
    cleaned = _re.sub(r'  +', ' ', cleaned).strip()
    cleaned = cleaned.rstrip('，,；;、')
    return cleaned


def _make_browser_accessible_image_url(url: str) -> str:
    """将内部图片路径转换为浏览器可访问的 URL。

    处理以下几种情况：
    1. 已是 http(s):// URL → 直接返回（假设公网可达）
    2. 本地文件路径 / MinIO 对象 key → 尝试生成公开 URL 或预签名 URL
    3. 空值 → 返回空字符串
    """
    raw = (url or "").strip()
    if not raw:
        return ""

    # 已是标准 HTTP(S) URL——但若是 MinIO 公开 URL，需确保路径双编码
    # （MinIO key 里若含字面 % 字符，HTTP URL 里 % 必须编码为 %25，否则 MinIO 解码后 key 不匹配）
    if raw.startswith(("http://", "https://")):
        try:
            from backend.storage import normalize_minio_url
            return normalize_minio_url(raw)
        except Exception:
            return raw

    # data: URL (base64) 也可以直接用
    if raw.startswith("data:"):
        return raw

    # 内部路径：尝试 MinIO 公开 URL / 预签名 URL（24h 有效，浏览器直接访问）
    try:
        from backend.storage import get_presigned_url, get_public_url

        # 标准化路径（去掉前导斜杠）
        obj_key = raw.lstrip("/").replace("\\", "/")

        # 优先尝试公开 URL（无需预签名）。
        # 某些部署会把公开 base 配置为同源路径（例如 "/files"），此时 get_public_url
        # 可能返回 "/files/..."，同样应当视为可直接访问。
        public = get_public_url(obj_key)
        if public and public.startswith(("http://", "https://", "/files/")):
            return public

        # 其次生成预签名 URL
        presigned = get_presigned_url(obj_key)
        if presigned and presigned.startswith(("http://", "https://")):
            return presigned
    except Exception as e:
        logger.debug("[multi_kb_search] 图片 URL 转换失败: %s, error=%s", raw[:80], e)

    # 最后回退：保留原始 key/path。当前链路要求上游尽量提供 /files/ 或 http(s) URL，
    # 前端不再对未知图片路径做代理包装。
    return raw


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
) -> dict:
    """搜索单个知识库（带超时保护）"""
    kb_label = KB_LABELS.get(kb_id, kb_id)
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
) -> dict:
    """搜索单个知识库：走 retrieve_context（含内容过滤与上下文拼装）。"""
    kb_label = KB_LABELS.get(kb_id, kb_id)
    total_started = time.time()
    try:
        from backend.api.routers.rag import retrieve_context

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
    from backend.api.routers.rag import _extract_keywords

    hl, ll, query_type, seed_weights = await _extract_keywords(
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


def _clean_image_blocks_in_context(context: str) -> str:
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
    import re as _re
    from backend.application.agent_executors.image_url_registry import register_image_url

    _IMG_BLOCK_RE = _re.compile(
        r"Image Content Analysis:\s*\n"
        r"Image Path:\s*([^\r\n]+)\s*\n"
        r"Captions:\s*([^\r\n]*)\s*\n"
        r"Footnotes:\s*([^\r\n]*)\s*\n*"
        r"(?:Visual Analysis:\s*([^\r\n]+(?:\n(?![A-Z][a-zA-Z ]+:)[^\r\n]+)*))?"
        ,
        _re.IGNORECASE | _re.MULTILINE,
    )

    def _replace_block(m: _re.Match) -> str:
        raw_path = (m.group(1) or "").strip()
        caption_raw = (m.group(2) or "").strip()
        visual = (m.group(4) or "").strip()
        # 清除图注中的内网 MinIO URL，避免传给模型后被原样输出
        caption = _strip_internal_urls_from_caption(caption_raw)

        if not raw_path:
            return f"[图片（无路径）：{caption}]" if caption else ""

        # 转换为浏览器可访问的 URL
        url = _make_browser_accessible_image_url(raw_path)
        if not url:
            return f"[图片（无法访问）：{caption}]" if caption else ""

        # 注册到 [IMAGE:xxx] 注册表
        token = register_image_url(url, caption or "图片")

        # 给模型的精简说明：token + 视觉描述（帮助模型判断是否与问题相关）
        desc_parts = [f"图注：{caption}"] if caption else []
        if visual:
            desc_parts.append(f"内容描述：{visual[:120]}")
        desc = "，".join(desc_parts)
        return f"[图片 {token}｜{desc}]" if desc else f"[图片 {token}]"

    return _IMG_BLOCK_RE.sub(_replace_block, context)


def _build_image_whitelist_section(all_images: list[dict]) -> str:
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

    from backend.application.agent_executors.image_url_registry import register_image_url

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
        token = register_image_url(url, caption or f"图片{idx}")
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
) -> tuple[str, int, int, bool]:
    """跨库倒数排名融合 (RRF)。

    在所有成功返回的 ``chunks`` 上统一打分、排序，**全局**最多保留 ``final_top_n`` 条
    （与 ``chunk_top_k`` 一致，默认 4），避免双库各 4 段拼成 8 段。
    若某库仅有 ``context`` 无 ``chunks``，回退拼接该段 context（极少见）。

    Returns:
        (merged_context, kept_fragment_count, total_fragment_count, truncated)
    """
    chunk_scores: dict[str, float] = {}
    chunk_contents: dict[str, dict] = {}
    context_fallback_parts: list[str] = []

    for result in results:
        if not result.get("success"):
            continue
        kb_id = result.get("kb_id", "unknown")
        kb_label = KB_LABELS.get(kb_id, kb_id)
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
                    clean_content = _clean_image_blocks_in_context(content or "")
                    chunk_contents[unique_chunk_key] = {
                        "content": clean_content,
                        "kb_id": kb_id,
                        "doc_name": doc_name,
                    }

                chunk_scores[unique_chunk_key] += score
        else:
            context = (result.get("context") or "").strip()
            if context:
                context = _clean_image_blocks_in_context(context)
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

        selected = (target_matches + target_misses)[:final_top_n]

        unique_doc_names: list[str] = []
        seen_doc_names: set[str] = set()
        for chunk_key, _score in selected:
            doc_name = chunk_contents[chunk_key].get("doc_name") or ""
            if doc_name and doc_name not in seen_doc_names and doc_name not in _KB_DISPLAY_NAMES:
                seen_doc_names.add(doc_name)
                unique_doc_names.append(doc_name)

        ref_block: str | None = None
        if unique_doc_names:
            ref_lines = "\n".join(f"- [{idx}] {name}" for idx, name in enumerate(unique_doc_names, start=1))
            ref_block = (
                "## 可引用文献\n\n"
                "以下为本轮检索片段的实际来源文档；正文引用 [n] 与末尾「参考文档」"
                "必须使用这些具体文章/文献名称，禁止使用知识库名称（如“核电子学库”“期刊论文库”）：\n\n"
                f"{ref_lines}"
            )

        fragment_entries: list[tuple[str, str]] = []
        for chunk_key, _score in selected:
            info = chunk_contents[chunk_key]
            doc_name = info.get("doc_name") or KB_LABELS.get(info["kb_id"], info["kb_id"])
            fragment_entries.append((doc_name, info["content"]))

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
) -> str:
    """搜索多个知识库并返回融合后的上下文。

    普通模式可只传 query；也支持额外传入高/低层关键词，跳过自动关键词提取。
    """
    normalized_hl = _normalize_keywords_value(high_level_keywords) if high_level_keywords is not None else None
    normalized_ll = _normalize_keywords_value(low_level_keywords) if low_level_keywords is not None else None

    return await execute_multi_kb_search(
        query=query,
        kb_ids=kb_ids,
        top_k=top_k,
        chunk_top_k=chunk_top_k,
        merge_strategy=merge_strategy,
        external_hl_keywords=normalized_hl,
        external_ll_keywords=normalized_ll,
        external_query_type=query_type,
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
) -> str:
    """搜索多个知识库获取相关信息。

    同时检索多个知识库，使用倒数排名融合算法合并结果。
    默认检索核电子学知识库和期刊论文库。

    Args:
        query: 搜索查询内容
        kb_ids: 知识库ID列表，默认使用全部知识库
        top_k: 每个知识库阶段1实体/文档召回上限（与路由 retrieve_context 一致），默认见 _TOOL_DEFAULT_TOP_K
        chunk_top_k: 融合前进内容块的期望条数上限，默认见 _TOOL_DEFAULT_CHUNK_TOP_K
        merge_strategy: 合并策略，rrf(倒数排名融合) 或 weighted(加权)
        content_filter_mode: 内容过滤流水线；None 时使用 KB Profile（domain 库为 MMR_ONLY）
    """
    from backend.core.tools.request_limits import (
        TURN_MAX_EVIDENCE_CHARS,
        consume_tool_budget,
        get_multi_kb_request_lock,
        get_turn_kb_chars_remaining,
        record_turn_kb_chars,
    )

    # 移除查询中的运行时元数据（如 <runtime_current_time> 块）
    query = re.sub(r"<runtime_current_time>[\s\S]*?</runtime_current_time>", "", query, flags=re.IGNORECASE).strip()

    kb_lock = get_multi_kb_request_lock()
    if kb_lock.locked():
        logger.warning("[多KB搜索] 拒绝并行检索：已有 multi_kb_search 正在执行")
        return (
            "禁止并行调用 multi_kb_search。请等待当前检索完成；"
            "若已有足够证据，请直接基于 retrieved_evidence 回答，勿重复检索。"
        )

    turn_remaining = get_turn_kb_chars_remaining(TURN_MAX_EVIDENCE_CHARS)
    if turn_remaining <= 0:
        logger.warning("[多KB搜索] 本轮检索上下文字符预算已用尽")
        return (
            f"本轮知识库检索上下文已达上限（约 {TURN_MAX_EVIDENCE_CHARS} 字），"
            "请基于已有检索结果回答，勿再调用 multi_kb_search。"
        )

    allowed, _remaining = consume_tool_budget(budget_key)
    if not allowed:
        logger.warning("[多KB搜索] 已达到本轮调用上限，跳过重复检索")
        return "知识库检索次数已达上限，请基于已有检索结果回答。"

    if max_result_chars and max_result_chars > 0:
        max_result_chars = min(max_result_chars, turn_remaining)
    elif MAX_RESULT_CHARS_FULL_AGENT > 0:
        max_result_chars = min(MAX_RESULT_CHARS_FULL_AGENT, turn_remaining)

    top_k = max(1, min(top_k, MAX_TOP_K))
    chunk_top_k = max(1, min(chunk_top_k, MAX_CHUNK_TOP_K))
    effective_kb_ids = kb_ids or [DEFAULT_KB_ID, PAPER_KB_ID]

    normalized_hl_keywords = _normalize_keywords_value(external_hl_keywords) if external_hl_keywords is not None else None
    normalized_ll_keywords = _normalize_keywords_value(external_ll_keywords) if external_ll_keywords is not None else None

    # 未显式传入关键词时：共享单库提取 + Path-A 仅整句（见 _apply_path_a_fast_keywords）。
    user_provided_keywords = (
        external_hl_keywords is not None or external_ll_keywords is not None
    )
    if not user_provided_keywords:
        normalized_hl_keywords = None
        normalized_ll_keywords = None

    current_thread = threading.current_thread().name
    in_subagent_thread = current_thread.startswith("subagent-exec-")
    kb_timeout = SUBAGENT_KB_SEARCH_TIMEOUT if in_subagent_thread else KB_SEARCH_TIMEOUT
    total_timeout = SUBAGENT_TOTAL_SEARCH_TIMEOUT if in_subagent_thread else TOTAL_SEARCH_TIMEOUT

    # Deduplicate identical in-flight retrieval calls in the same thread/loop scope.
    # Subagent loops may occasionally issue repeated identical tool calls.
    async with kb_lock:
        dedupe_key = ""
        if in_subagent_thread:
            loop = asyncio.get_running_loop()
            dedupe_key = "|".join(
                [
                    current_thread,
                    str(id(loop)),
                    (query or "").strip(),
                    ",".join(sorted(effective_kb_ids)),
                    str(top_k),
                    str(chunk_top_k),
                    merge_strategy,
                    str(_MULTI_KB_PATH_A_QUERY_ONLY),
                    ",".join(normalized_hl_keywords or []),
                    ",".join(normalized_ll_keywords or []),
                    str(external_query_type or ""),
                    "shared" if not user_provided_keywords else "external",
                    content_filter_mode.value if content_filter_mode else "profile",
                ]
            )
            existing_task: asyncio.Task[str] | None = None
            with _INFLIGHT_MULTI_KB_LOCK:
                existing_task = _INFLIGHT_MULTI_KB_TASKS.get(dedupe_key)

            if existing_task is not None and not existing_task.done():
                logger.info("[多KB搜索] 复用进行中的相同检索请求（subagent）")
                return await existing_task

            run_task = asyncio.create_task(
                _execute_multi_kb_search_once(
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
                )
            )
            with _INFLIGHT_MULTI_KB_LOCK:
                _INFLIGHT_MULTI_KB_TASKS[dedupe_key] = run_task
            try:
                return await run_task
            finally:
                with _INFLIGHT_MULTI_KB_LOCK:
                    current = _INFLIGHT_MULTI_KB_TASKS.get(dedupe_key)
                    if current is run_task:
                        _INFLIGHT_MULTI_KB_TASKS.pop(dedupe_key, None)

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
) -> str:
    total_start_time = time.time()
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
        return f"搜索超时，请稍后重试。查询: {query[:50]}..."

    all_images: list[dict] = []
    all_stats = {"entities": 0, "relationships": 0, "chunks": 0}
    active_kbs = 0
    failed_kbs = 0
    total_elapsed = 0.0
    kb_timings = []

    for result in results:
        elapsed = result.get("elapsed_time", 0.0)
        kb_label = result.get("kb_label", result.get("kb_id", "unknown"))
        kb_timings.append(f"{kb_label}: {elapsed:.2f}s")
        
        if result.get("success"):
            active_kbs += 1
            total_elapsed += elapsed
            if result.get("images"):
                all_images.extend(result["images"])
            stats = result.get("stats", {})
            all_stats["entities"] += stats.get("entities", 0)
            all_stats["relationships"] += stats.get("relationships", 0)
            all_stats["chunks"] += stats.get("chunks", 0)
        else:
            failed_kbs += 1
            error = result.get("error", "Unknown error")
            logger.warning(f"[多KB搜索] {kb_label} 失败: {error}")

    image_section = _build_image_whitelist_section(all_images)
    total_time = time.time() - total_start_time
    timing_info = ", ".join(kb_timings)
    context_budget = _estimate_context_char_budget(
        max_result_chars,
        active_kbs=active_kbs,
    )

    merged_context, merged_block_count, total_fragments, context_truncated = _merge_results_rrf(
        results,
        final_top_n=chunk_top_k,
        max_context_chars=context_budget,
        required_document_codes=_extract_explicit_document_codes(query),
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
    from backend.core.tools.request_limits import (
        TURN_MAX_EVIDENCE_CHARS,
        record_turn_kb_chars,
    )

    turn_total = record_turn_kb_chars(len(result))
    logger.info(
        "[多KB搜索] 本轮累计检索上下文: %d/%d 字",
        turn_total,
        TURN_MAX_EVIDENCE_CHARS,
    )
    return result
