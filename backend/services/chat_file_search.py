"""聊天文件检索索引:BM25/TF-IDF 双通道、图表模式检索、多轮尝试与上下文扩窗。(拆自 chat_file_index.py)"""

from __future__ import annotations

from backend.config.loggers import get_logger

from backend.config.log_privacy import text_fingerprint
from backend.core.text.bm25_index import BM25Index
from collections import Counter
from typing import Dict
from typing import List
import math
import re
import threading
from backend.services.chat_file_chunking import (
    ChunkMeta,
    SearchResult,
    _FIGURE_CONTENT_PATTERNS,
    _FIGURE_QUERY_RE,
    _TABLE_CONTENT_PATTERNS,
    _TABLE_QUERY_RE,
    _cosine_sim,
    _is_generic_summary_query,
    _tokenize_simple,
    prepare_search_inputs,
    split_text,
)

logger = get_logger(__name__)


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
                "[ChatFileIndex] 模式匹配命中: query_meta=%s, keyword_count=%d, 命中=%d 块",
                text_fingerprint(query),
                len(keywords or []),
                len(pattern_results),
            )
            return pattern_results

        # ── 第1路：关键词精确匹配 ────────────────────────────────────────
        if keywords:
            kw_results = self._keyword_search(keywords, top_k, expand_window)
            if kw_results:
                logger.info(
                    "[ChatFileIndex] 关键词精确匹配成功: keyword_count=%d, 命中=%d 块",
                    len(keywords),
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
            "[ChatFileIndex] 回退到文档开头。query_meta=%s, is_generic=%s, has_keywords=%s",
            text_fingerprint(query),
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
