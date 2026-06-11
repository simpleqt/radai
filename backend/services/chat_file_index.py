"""
对话级文件轻量检索索引

BM25 关键词召回 + TF-IDF 余弦相似度精排，纯 Python 实现，
零外部依赖（不调 embedding API，不需要 Milvus/Redis）。
索引生命周期与会话绑定，LRU 池控制内存上限。
"""

from __future__ import annotations

import math
import re
import threading
from collections import Counter, OrderedDict
from dataclasses import dataclass, field
from typing import Dict, List, Optional

from backend.config.loggers import get_logger
from backend.db.connection import get_session_factory
from backend.db.models import SessionFile, UserFile
from backend.rag.keywords.bm25_index import BM25Index
from sqlalchemy import select

logger = get_logger(__name__)


# ── 数据结构 ─────────────────────────────────────────────

@dataclass
class ChunkMeta:
    file_id: str
    file_name: str
    chunk_index: int
    content: str


@dataclass
class SearchResult:
    content: str
    file_name: str
    file_id: str
    chunk_index: int
    score: float


# ── 工具函数 ─────────────────────────────────────────────

def _tokenize_simple(text: str) -> List[str]:
    """轻量分词：中文按字+bigram，英文按单词，数字保留"""
    words: List[str] = []
    for m in re.finditer(r"[\u4e00-\u9fff]+", text):
        s = m.group()
        words.extend(s)           # 单字
        for i in range(len(s) - 1):
            words.append(s[i : i + 2])   # bigram
    for m in re.finditer(r"[a-zA-Z]{2,}", text):
        words.append(m.group().lower())
    for m in re.finditer(r"\d+(?:\.\d+)?", text):
        words.append(m.group())
    return words


def _cosine_sim(a: Dict[str, float], b: Dict[str, float]) -> float:
    """两个稀疏 TF-IDF 向量的余弦相似度"""
    if not a or not b:
        return 0.0
    common = set(a) & set(b)
    if not common:
        return 0.0
    dot = sum(a[k] * b[k] for k in common)
    norm_a = math.sqrt(sum(v * v for v in a.values()))
    norm_b = math.sqrt(sum(v * v for v in b.values()))
    if norm_a < 1e-9 or norm_b < 1e-9:
        return 0.0
    return dot / (norm_a * norm_b)


_FIGURE_QUERY_RE = re.compile(
    r"(图片|图表|配图|插图|附图|对比图|结构图|示意图|结果图|figure|fig\.?|illustration|image)",
    re.IGNORECASE,
)
_TABLE_QUERY_RE = re.compile(
    r"(表格|数据表|table|tab\.?)",
    re.IGNORECASE,
)
_FIGURE_CONTENT_PATTERNS = [
    re.compile(r"\[图片[:：][^\]]+\]", re.IGNORECASE),
    re.compile(r"!\[[^\]]*\]\([^)]+\)", re.IGNORECASE),
    re.compile(r"\b(?:figure|fig\.?)\s*\d+[:.\-\s]?", re.IGNORECASE),
    re.compile(r"图\s*\d+[:：.\-\s]?", re.IGNORECASE),
]
_TABLE_CONTENT_PATTERNS = [
    re.compile(r"\b(?:table|tab\.?)\s*\d+[:.\-\s]?", re.IGNORECASE),
    re.compile(r"表\s*\d+[:：.\-\s]?", re.IGNORECASE),
]

_RETRIEVAL_STOP_WORDS = {
    "这篇", "这份", "这个", "该", "本", "论文", "文档", "文件", "文章", "报告",
    "里面", "其中", "关于", "有关", "请问", "一下", "一下子", "什么", "哪些",
    "哪个", "哪里", "有没有", "有无", "如何", "怎么", "为什么", "多少", "几个",
    "是什么", "有哪些", "有什么", "讲了什么", "说了什么", "写了什么", "介绍",
    "描述", "分析", "总结", "概述", "内容", "信息", "具体", "相关",
    "please", "show", "tell", "what", "which", "where", "does", "is", "are",
    "the", "a", "an", "in", "of", "to", "for", "about", "with", "from",
}

_IMAGE_QUERY_KEYWORDS = [
    "figure", "fig", "image", "illustration", "caption", "图片", "图", "图注",
]
_TABLE_QUERY_KEYWORDS = [
    "table", "tab", "dataset", "statistics", "表格", "表", "数据表",
]
_SUMMARY_QUERY_TERMS = "abstract introduction methodology method experiment results conclusion summary main contribution"


def split_text(
    text: str,
    chunk_size: int = 800,
    chunk_overlap: int = 100,
) -> List[str]:
    """智能分块 - 尊重段落和句子边界"""
    if chunk_size <= 0:
        raise ValueError("chunk_size must be positive")

    paragraphs = re.split(r"\n{2,}", text)
    chunks: List[str] = []
    current = ""

    for para in paragraphs:
        if len(current) + len(para) <= chunk_size:
            current += ("\n\n" + para) if current else para
        else:
            if current:
                chunks.append(current.strip())
            if len(para) > chunk_size:
                sentences = re.split(r"(?<=[。！？.!?])\s*", para)
                current = ""
                for sent in sentences:
                    if len(current) + len(sent) <= chunk_size:
                        current += sent
                    else:
                        if current:
                            chunks.append(current.strip())
                        current = sent
            else:
                current = para

    if current.strip():
        chunks.append(current.strip())
    return _apply_chunk_overlap(chunks, chunk_overlap)


def _apply_chunk_overlap(chunks: List[str], chunk_overlap: int) -> List[str]:
    """为相邻块补充字符级重叠，降低边界信息截断。"""
    if chunk_overlap <= 0 or len(chunks) <= 1:
        return chunks

    normalized_overlap = max(1, chunk_overlap)
    overlapped_chunks: List[str] = [chunks[0]]

    for chunk in chunks[1:]:
        prev_chunk = overlapped_chunks[-1]
        prefix = prev_chunk[-normalized_overlap:]
        if prefix and not chunk.startswith(prefix):
            overlapped_chunks.append(f"{prefix}{chunk}")
        else:
            overlapped_chunks.append(chunk)

    return overlapped_chunks


# ── 索引核心 ─────────────────────────────────────────────

class ChatFileIndex:
    """会话级文档轻量检索索引

    BM25 关键词召回 + TF-IDF 余弦相似度精排，纯 Python 实现。
    不依赖任何外部服务（无 embedding API / Milvus / Redis）。

    典型性能：
      60 块 → 构建 <50ms，检索 <5ms
      600 块 → 构建 <300ms，检索 <20ms
    """

    def __init__(self, thread_id: str):
        self.thread_id = thread_id
        self._bm25 = BM25Index()
        self._chunks: List[ChunkMeta] = []
        self._idf: Dict[str, float] = {}
        self._chunk_tfidf: List[Dict[str, float]] = []
        self._lock = threading.RLock()

    # ── 构建 ──────────────────────────────────────────────

    def add_document(
        self,
        file_id: str,
        content: str,
        file_name: str,
        chunk_size: int = 800,
        chunk_overlap: int = 100,
    ) -> int:
        """分块并建立索引，同步完成，通常 <100ms"""
        chunks = split_text(content, chunk_size, chunk_overlap)
        if not chunks:
            return 0

        with self._lock:
            for i, text in enumerate(chunks):
                self._chunks.append(
                    ChunkMeta(
                        file_id=file_id,
                        file_name=file_name,
                        chunk_index=i,
                        content=text,
                    )
                )
                self._bm25.add_document(text)

            self._rebuild_tfidf()
            total_chunks = len(self._chunks)

        logger.info(
            "[ChatFileIndex] 添加文档: file_id=%s, 块数=%d, 总块数=%d",
            file_id,
            len(chunks),
            total_chunks,
        )
        return len(chunks)

    def remove_document(self, file_id: str) -> int:
        """移除指定文件的所有块并重建索引"""
        with self._lock:
            before = len(self._chunks)
            self._chunks = [c for c in self._chunks if c.file_id != file_id]
            removed = before - len(self._chunks)
            if removed > 0:
                self._rebuild_bm25()
                self._rebuild_tfidf()
        if removed > 0:
            logger.info(
                "[ChatFileIndex] 移除文档: file_id=%s, 移除块数=%d", file_id, removed
            )
        return removed

    def _rebuild_bm25(self) -> None:
        """全量重建 BM25 索引（仅在删除文档时调用）"""
        self._bm25 = BM25Index()
        all_texts = [c.content for c in self._chunks]
        if all_texts:
            self._bm25.build(all_texts)

    def _rebuild_tfidf(self) -> None:
        """增量更新：重建全局 IDF 和每块 TF-IDF 向量"""
        n = len(self._chunks)
        if n == 0:
            self._idf = {}
            self._chunk_tfidf = []
            return

        doc_freq: Dict[str, int] = {}
        tokenized: List[List[str]] = []

        for chunk in self._chunks:
            tokens = _tokenize_simple(chunk.content)
            tokenized.append(tokens)
            for term in set(tokens):
                doc_freq[term] = doc_freq.get(term, 0) + 1

        self._idf = {
            term: math.log((n + 1) / (df + 1)) + 1
            for term, df in doc_freq.items()
        }

        self._chunk_tfidf = []
        for tokens in tokenized:
            tf = Counter(tokens)
            total = max(len(tokens), 1)
            tfidf = {
                term: (count / total) * self._idf.get(term, 1.0)
                for term, count in tf.items()
            }
            self._chunk_tfidf.append(tfidf)

    # ── 检索 ──────────────────────────────────────────────

    def search(
        self,
        query: str,
        top_k: int = 5,
        bm25_weight: float = 0.6,
        tfidf_weight: float = 0.4,
        expand_window: int = 1,
        keywords: List[str] | None = None,
    ) -> List[SearchResult]:
        """混合检索入口，内部自动执行多轮检索尝试并合并结果。"""
        with self._lock:
            if not self._chunks:
                return []

            prepared_query, prepared_keywords = prepare_search_inputs(query, keywords or [])
            attempts = self._build_search_attempts(prepared_query, prepared_keywords)
            merged_results: Dict[tuple[str, int], SearchResult] = {}
            ranked_keys: list[tuple[str, int]] = []

            for attempt_query, attempt_keywords in attempts:
                attempt_results = self._search_once(
                    attempt_query,
                    top_k=top_k,
                    bm25_weight=bm25_weight,
                    tfidf_weight=tfidf_weight,
                    expand_window=expand_window,
                    keywords=attempt_keywords,
                    allow_fallback=False,
                )
                for result in attempt_results:
                    key = (result.file_id, result.chunk_index)
                    existing = merged_results.get(key)
                    if existing is None:
                        merged_results[key] = result
                        ranked_keys.append(key)
                    elif result.score > existing.score:
                        merged_results[key] = result

            if merged_results:
                ordered = sorted(
                    (merged_results[key] for key in ranked_keys),
                    key=lambda item: (-item.score, item.file_name, item.chunk_index),
                )
                return ordered[:top_k]

            return self._search_once(
                prepared_query,
                top_k=top_k,
                bm25_weight=bm25_weight,
                tfidf_weight=tfidf_weight,
                expand_window=expand_window,
                keywords=prepared_keywords,
                allow_fallback=True,
            )

    def _search_once(
        self,
        query: str,
        top_k: int = 5,
        bm25_weight: float = 0.6,
        tfidf_weight: float = 0.4,
        expand_window: int = 1,
        keywords: List[str] | None = None,
        *,
        allow_fallback: bool,
    ) -> List[SearchResult]:
        """混合检索：关键词精确匹配（优先）+ BM25 + TF-IDF + 顺序回退。

        参数：
            query:       原始查询文本（支持任意语言）
            keywords:    模型提取的精确关键词列表（推荐传入以提升跨语言准确性）
            top_k:       返回的最大结果数
            expand_window: 上下文窗口扩展（每个命中块前后各取 N 块）

        检索策略（按优先级）：
          1. keywords 关键词精确匹配（大小写不敏感正则）
          2. BM25 + TF-IDF 融合召回
          3. 顺序回退：BM25 无结果 / 通用总结型查询时返回文档开头
        """
        pattern_results = self._pattern_search(query, keywords or [], top_k, expand_window)
        if pattern_results:
            logger.info(
                "[ChatFileIndex] 模式匹配命中: query=%s, keywords=%s, 命中=%d 块",
                query[:50],
                (keywords or [])[:5],
                len(pattern_results),
            )
            return pattern_results

        # ── 第1路：关键词精确匹配 ────────────────────────────────────────
        if keywords:
            kw_results = self._keyword_search(keywords, top_k, expand_window)
            if kw_results:
                logger.info(
                    "[ChatFileIndex] 关键词精确匹配成功: keywords=%s, 命中=%d 块",
                    keywords[:5],
                    len(kw_results),
                )
                return kw_results

        # 通用总结型查询（没有实质关键词）→ 直接回退到文档开头
        is_generic_query = _is_generic_summary_query(query)

        if not is_generic_query:
            # ── 第2路：BM25 + TF-IDF ─────────────────────────────────────
            bm25_raw = self._bm25.search(query, top_k=min(top_k * 3, len(self._chunks)))

            if bm25_raw:
                candidate_indices = [idx for idx, _ in bm25_raw]
                bm25_scores: Dict[int, float] = {idx: score for idx, score in bm25_raw}

                query_tfidf = self._compute_query_tfidf(query)
                tfidf_scores: Dict[int, float] = {
                    idx: _cosine_sim(query_tfidf, self._chunk_tfidf[idx])
                    for idx in candidate_indices
                    if idx < len(self._chunk_tfidf)
                }

                bm25_max = max(bm25_scores.values(), default=1.0)
                tfidf_max = max(tfidf_scores.values(), default=1.0)

                fused: Dict[int, float] = {}
                for idx in candidate_indices:
                    b = bm25_scores.get(idx, 0.0) / max(bm25_max, 1e-9)
                    t = tfidf_scores.get(idx, 0.0) / max(tfidf_max, 1e-9)
                    fused[idx] = bm25_weight * b + tfidf_weight * t

                ranked = sorted(fused.items(), key=lambda x: -x[1])[:top_k]
                results = self._build_results_with_window(ranked, expand_window)
                if results:
                    return results

        if not allow_fallback:
            return []

        # ── 第3路：顺序回退（跨语言 / 通用查询）───────────────────────
        logger.info(
            "[ChatFileIndex] 回退到文档开头。query=%s, is_generic=%s, has_keywords=%s",
            query[:50],
            is_generic_query,
            bool(keywords),
        )
        return self._head_chunks(top_k, expand_window)

    def _pattern_search(
        self,
        query: str,
        keywords: List[str],
        top_k: int,
        expand_window: int,
    ) -> List[SearchResult]:
        patterns: list[re.Pattern[str]] = []

        if _FIGURE_QUERY_RE.search(query):
            patterns.extend(_FIGURE_CONTENT_PATTERNS)
        if _TABLE_QUERY_RE.search(query):
            patterns.extend(_TABLE_CONTENT_PATTERNS)

        normalized_keywords = [
            kw.strip() for kw in keywords if isinstance(kw, str) and len(kw.strip()) > 1
        ]
        for kw in normalized_keywords[:6]:
            patterns.append(re.compile(re.escape(kw), re.IGNORECASE))

        direct_query = " ".join((query or "").split()).strip()
        if direct_query and len(direct_query) >= 2:
            patterns.append(re.compile(re.escape(direct_query), re.IGNORECASE))

        if not patterns:
            return []

        chunk_scores: Dict[int, float] = {}
        for idx, chunk in enumerate(self._chunks):
            score = 0.0
            for pattern in patterns:
                matches = pattern.findall(chunk.content)
                if matches:
                    score += min(len(matches), 3)
            if score > 0:
                chunk_scores[idx] = score

        if not chunk_scores:
            return []

        max_score = max(chunk_scores.values(), default=1.0)
        ranked = sorted(
            ((idx, score / max(max_score, 1e-9)) for idx, score in chunk_scores.items()),
            key=lambda item: -item[1],
        )[:top_k]
        return self._build_results_with_window(ranked, expand_window)

    def _build_search_attempts(
        self,
        query: str,
        keywords: List[str],
    ) -> List[tuple[str, List[str]]]:
        attempts: list[tuple[str, List[str]]] = []
        seen: set[tuple[str, tuple[str, ...]]] = set()

        normalized_keywords = [
            kw.strip()
            for kw in keywords
            if isinstance(kw, str) and len(kw.strip()) > 1
        ][:6]

        def _add_attempt(attempt_query: str, attempt_keywords: List[str]) -> None:
            normalized_query = " ".join((attempt_query or "").split()).strip()
            normalized_kws = tuple(
                kw.strip() for kw in attempt_keywords if kw and len(kw.strip()) > 1
            )
            if not normalized_query and not normalized_kws:
                return
            key = (normalized_query, normalized_kws)
            if key in seen:
                return
            seen.add(key)
            attempts.append((normalized_query, list(normalized_kws)))

        _add_attempt(query, normalized_keywords)

        if normalized_keywords:
            _add_attempt(" ".join(normalized_keywords), [])
            for kw in normalized_keywords[:4]:
                _add_attempt(kw, [kw])

        return attempts

    def _keyword_search(
        self, keywords: List[str], top_k: int, expand_window: int
    ) -> List[SearchResult]:
        """关键词精确匹配：对每个 keyword 做大小写不敏感子串搜索。

        对每个块统计命中的关键词数量，按命中数排序返回。
        """
        # 去重并过滤过短关键词（≤1字符通常无意义）
        valid_kws = list(dict.fromkeys(
            kw.strip() for kw in keywords if len(kw.strip()) > 1
        ))
        if not valid_kws:
            return []

        # 编译正则（每个关键词独立匹配，大小写不敏感）
        patterns = [re.compile(re.escape(kw), re.IGNORECASE) for kw in valid_kws]

        chunk_scores: Dict[int, float] = {}
        for idx, chunk in enumerate(self._chunks):
            hit_count = sum(1 for p in patterns if p.search(chunk.content))
            if hit_count > 0:
                # 多个关键词命中加分，归一化到 [0,1]
                chunk_scores[idx] = hit_count / len(valid_kws)

        if not chunk_scores:
            return []

        ranked = sorted(chunk_scores.items(), key=lambda x: -x[1])[:top_k]
        return self._build_results_with_window(ranked, expand_window)

    def _build_results_with_window(
        self, ranked: List[tuple], expand_window: int
    ) -> List[SearchResult]:
        """将排序结果加上上下文窗口扩展后构建 SearchResult 列表。"""
        results: List[SearchResult] = []
        seen_ranges: Dict[str, set] = {}

        for idx, score in ranked:
            chunk = self._chunks[idx]
            fid = chunk.file_id
            if fid not in seen_ranges:
                seen_ranges[fid] = set()

            expanded_content = self._expand_context(idx, expand_window, seen_ranges[fid])
            start = max(0, idx - expand_window)
            end = min(len(self._chunks), idx + expand_window + 1)
            for i in range(start, end):
                if self._chunks[i].file_id == fid:
                    seen_ranges[fid].add(i)

            results.append(
                SearchResult(
                    content=expanded_content,
                    file_name=chunk.file_name,
                    file_id=fid,
                    chunk_index=chunk.chunk_index,
                    score=round(score, 4),
                )
            )
        return results

    def _head_chunks(self, top_k: int, expand_window: int = 1) -> List[SearchResult]:
        """按文件顺序返回文档开头的 top_k 个块（每文件独立取前几块）。"""
        # 按 file_id 分组，每个文件取前 ceil(top_k / file_count) 块
        file_order: List[str] = []
        per_file: Dict[str, List[int]] = {}
        for i, c in enumerate(self._chunks):
            if c.file_id not in per_file:
                per_file[c.file_id] = []
                file_order.append(c.file_id)
            per_file[c.file_id].append(i)

        n_files = max(len(file_order), 1)
        per_file_quota = max(1, (top_k + n_files - 1) // n_files)

        results: List[SearchResult] = []
        seen_ranges: Dict[str, set] = {}

        for fid in file_order:
            indices = per_file[fid][:per_file_quota]
            if fid not in seen_ranges:
                seen_ranges[fid] = set()
            for idx in indices:
                expanded = self._expand_context(idx, expand_window, seen_ranges[fid])
                start = max(0, idx - expand_window)
                end = min(len(self._chunks), idx + expand_window + 1)
                for i in range(start, end):
                    if self._chunks[i].file_id == fid:
                        seen_ranges[fid].add(i)
                results.append(
                    SearchResult(
                        content=expanded,
                        file_name=self._chunks[idx].file_name,
                        file_id=fid,
                        chunk_index=self._chunks[idx].chunk_index,
                        score=0.0,  # 顺序回退，分数无意义
                    )
                )
            if len(results) >= top_k:
                break

        return results[:top_k]

    def _compute_query_tfidf(self, query: str) -> Dict[str, float]:
        tokens = _tokenize_simple(query)
        tf = Counter(tokens)
        total = max(len(tokens), 1)
        return {
            term: (count / total) * self._idf.get(term, 1.0)
            for term, count in tf.items()
        }

    def _expand_context(
        self, center_idx: int, window: int, already_seen: set
    ) -> str:
        """扩展上下文窗口，将前后块拼入（同文件）"""
        parts: List[str] = []
        file_id = self._chunks[center_idx].file_id
        for offset in range(-window, window + 1):
            i = center_idx + offset
            if i < 0 or i >= len(self._chunks):
                continue
            if self._chunks[i].file_id != file_id:
                continue
            if i in already_seen and i != center_idx:
                continue
            parts.append(self._chunks[i].content)
        return "\n\n".join(parts)

    # ── 属性 ──────────────────────────────────────────────

    @property
    def chunk_count(self) -> int:
        with self._lock:
            return len(self._chunks)

    @property
    def file_count(self) -> int:
        with self._lock:
            return len({c.file_id for c in self._chunks})

    @property
    def file_ids(self) -> List[str]:
        with self._lock:
            seen: List[str] = []
            for c in self._chunks:
                if c.file_id not in seen:
                    seen.append(c.file_id)
            return seen


# ── 全局索引池 ────────────────────────────────────────────

class ChatFileIndexManager:
    """全局 LRU 索引池

    控制同时在内存中的索引数量，避免内存膨胀。
    100 个活跃会话 × ~200KB/会话 ≈ 20MB，极轻量。
    """

    def __init__(self, max_threads: int = 100):
        self._indices: OrderedDict[str, ChatFileIndex] = OrderedDict()
        self._max_threads = max_threads
        self._lock = threading.RLock()

    def _evict_if_needed_locked(self) -> None:
        if len(self._indices) >= self._max_threads:
            evicted_id, _ = self._indices.popitem(last=False)
            logger.info("[ChatFileIndexManager] LRU 驱逐: thread_id=%s", evicted_id)

    def _store_index_locked(
        self, thread_id: str, index: ChatFileIndex
    ) -> ChatFileIndex:
        self._evict_if_needed_locked()
        self._indices[thread_id] = index
        return index

    def _load_thread_documents_from_storage(
        self, thread_id: str
    ) -> List[tuple[str, str, str]]:
        session_factory = get_session_factory()
        with session_factory() as session:
            stmt = (
                select(
                    UserFile.file_id,
                    UserFile.display_name,
                    UserFile.metadata_,
                )
                .join(SessionFile, SessionFile.file_id == UserFile.file_id)
                .where(SessionFile.session_id == thread_id)
                .where(UserFile.status == "active")
                .order_by(SessionFile.created_at.asc(), UserFile.created_at.asc())
            )
            rows = session.execute(stmt).all()

        documents: List[tuple[str, str, str]] = []
        seen_file_ids: set[str] = set()
        for file_id, display_name, metadata in rows:
            if file_id in seen_file_ids:
                continue
            seen_file_ids.add(file_id)
            meta = metadata or {}
            if meta.get("file_category") != "long_doc":
                continue
            content = str(meta.get("content") or "").strip()
            if not content:
                continue
            documents.append((file_id, display_name, content))
        return documents

    def _hydrate_index(self, thread_id: str) -> Optional[ChatFileIndex]:
        documents = self._load_thread_documents_from_storage(thread_id)
        if not documents:
            return None

        index = ChatFileIndex(thread_id)
        for file_id, file_name, content in documents:
            index.add_document(
                file_id=file_id,
                content=content,
                file_name=file_name,
            )
        logger.info(
            "[ChatFileIndexManager] 已从存储重建索引: thread_id=%s, files=%d, chunks=%d",
            thread_id,
            len(documents),
            index.chunk_count,
        )
        return index

    def get_or_create(self, thread_id: str) -> ChatFileIndex:
        with self._lock:
            if thread_id in self._indices:
                self._indices.move_to_end(thread_id)
                return self._indices[thread_id]

            index = ChatFileIndex(thread_id)
            return self._store_index_locked(thread_id, index)

    def get(self, thread_id: str) -> Optional[ChatFileIndex]:
        with self._lock:
            if thread_id in self._indices:
                self._indices.move_to_end(thread_id)
                return self._indices[thread_id]

        hydrated = self._hydrate_index(thread_id)
        if hydrated is None:
            return None

        with self._lock:
            if thread_id in self._indices:
                self._indices.move_to_end(thread_id)
                return self._indices[thread_id]
            return self._store_index_locked(thread_id, hydrated)

    def remove(self, thread_id: str) -> None:
        with self._lock:
            self._indices.pop(thread_id, None)

    def has_files(self, thread_id: str) -> bool:
        idx = self.get(thread_id)
        return idx is not None and idx.chunk_count > 0


# 全局单例
_index_manager = ChatFileIndexManager(max_threads=100)


def get_index_manager() -> ChatFileIndexManager:
    return _index_manager


# ── 查询辅助 ──────────────────────────────────────────────

# 通用总结型查询模式：不含实质内容关键词，BM25 无法有效检索
_GENERIC_QUERY_PATTERNS = re.compile(
    r"^(这|那|此|该|本)?"
    r"(篇|份|个|部|章|段|节|文件|文档|论文|报告|文章|资料|附件|内容|文本)?"
    r"(讲|说|写|介绍|描述|解释|分析|总结|阐述|叙述)?(了|的)?"
    r"(什么|啥|内容|信息|主题|主旨|重点|要点|概要|摘要|大意|意思)$",
    re.IGNORECASE,
)

_GENERIC_KEYWORDS = {
    "总结", "摘要", "概述", "summary", "summarize", "abstract",
    "讲了什么", "说了什么", "写了什么", "主要内容", "主要讲",
    "介绍了什么", "概括", "大意", "要点", "核心内容",
    "这篇", "这份", "这个文档", "这个文件", "上传的",
}


def prepare_search_inputs(
    query: str,
    keywords: List[str] | None = None,
) -> tuple[str, List[str]]:
    """将自然语言问题整理成更适合检索的 query/keywords。

    无论上层是否直接传入用户原句，底层都会先做这一步，避免把整句自然语言
    原封不动喂给 BM25。
    """
    raw_query = " ".join((query or "").split()).strip()
    normalized_keywords: list[str] = []
    seen_keywords: set[str] = set()

    def _push_keyword(value: str) -> None:
        kw = " ".join((value or "").split()).strip().strip(".,;:：()[]{}<>")
        if len(kw) < 2:
            return
        lowered = kw.lower()
        if lowered in seen_keywords:
            return
        seen_keywords.add(lowered)
        normalized_keywords.append(kw)

    for kw in keywords or []:
        if isinstance(kw, str):
            _push_keyword(kw)

    # 保留引号中的精确短语，优先级很高
    for pattern in (
        r'"([^"\n]{2,80})"',
        r"'([^'\n]{2,80})'",
        r"“([^”\n]{2,80})”",
        r"‘([^’\n]{2,80})’",
    ):
        for match in re.findall(pattern, raw_query):
            _push_keyword(match)

    # 英文术语、缩写、数字编号
    for token in re.findall(r"[A-Za-z][A-Za-z0-9_\-]{1,40}", raw_query):
        if token.lower() not in _RETRIEVAL_STOP_WORDS:
            _push_keyword(token)
    for token in re.findall(r"(?:fig(?:ure)?|table|tab\.?)\s*\d+", raw_query, re.IGNORECASE):
        _push_keyword(token)
    for token in re.findall(r"图\s*\d+|表\s*\d+", raw_query):
        _push_keyword(token)

    # 中文术语候选
    for token in re.findall(
        r"[\u4e00-\u9fff]{2,12}(?:方法|模型|框架|算法|实验|结果|图片|图表|图像|图注|摘要|引言|结论|对比图|数据集)",
        raw_query,
    ):
        if token not in _RETRIEVAL_STOP_WORDS:
            _push_keyword(token)
    for token in re.findall(r"[\u4e00-\u9fff]{2,12}", raw_query):
        if token not in _RETRIEVAL_STOP_WORDS:
            _push_keyword(token)

    if _FIGURE_QUERY_RE.search(raw_query):
        for kw in _IMAGE_QUERY_KEYWORDS:
            _push_keyword(kw)
    if _TABLE_QUERY_RE.search(raw_query):
        for kw in _TABLE_QUERY_KEYWORDS:
            _push_keyword(kw)

    if _is_generic_summary_query(raw_query):
        for token in _SUMMARY_QUERY_TERMS.split():
            _push_keyword(token)
        return _SUMMARY_QUERY_TERMS, normalized_keywords[:12]

    if normalized_keywords:
        # 用提炼后的关键词组合构造检索 query，避免直接把整句问句交给 BM25
        rewritten_query = " ".join(normalized_keywords[:8])
        return rewritten_query, normalized_keywords[:12]

    return raw_query, normalized_keywords[:12]


def _is_generic_summary_query(query: str) -> bool:
    """判断是否为通用总结/概述类查询，这类查询不含实质关键词，BM25 效果差。"""
    q = query.strip().lower()
    # 极短查询（≤15字符）且命中模式
    if len(q) <= 25 and _GENERIC_QUERY_PATTERNS.search(q):
        return True
    # 包含通用关键词
    for kw in _GENERIC_KEYWORDS:
        if kw in q:
            return True
    return False
