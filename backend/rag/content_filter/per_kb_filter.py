"""
知识库级别的内容过滤器

整合 PPR 过滤、Rerank 加权融合和 MMR 重排序

v2：传递 query_type、embedding_func 完整支持三维度自适应改进
"""

import asyncio
import time
from datetime import date, datetime
from pathlib import Path
from urllib.parse import unquote, urlsplit
from typing import Any, Callable, Dict, List, Optional

from backend.config.loggers import get_logger
from backend.rag.content_filter.models import (
    FilterConfig,
    FilteredData,
    FilterMode,
    FilterResult,
    RawRetrievalData,
)
from backend.rag.content_filter.mmr_reranker import VectorMMRReranker
from backend.rag.content_filter.ppr_filter import PPRFilteredData, PPRGraphFilter
from backend.rag.content_filter.reranker import rerank_filtered_chunks, call_rerank_api, time_aware_fusion
from backend.rag.keywords.models import QueryType

logger = get_logger(__name__)


class PerKBContentFilter:
    """
    知识库级别的内容过滤器
    
    完全迁移自 NuclearElectronicsAgent 中的过滤逻辑
    """
    
    def __init__(
        self,
        kb_code: str,
        kb_id: str,
        config: Optional[FilterConfig] = None,
        embedding_func: Optional[Callable] = None,
    ):
        self.kb_code = kb_code
        self.kb_id = kb_id
        self.config = config or FilterConfig()
        self._embedding_func = embedding_func
        
        self._ppr_filter = PPRGraphFilter(
            ppr_damping=self.config.ppr_damping,
            ppr_min_score=self.config.ppr_min_score,
            max_recall_tokens=self.config.max_recall_tokens,
            max_final_tokens=self.config.max_final_tokens,
            use_directed=self.config.use_directed,
            reverse_edge_weight=self.config.reverse_edge_weight,
            reverse_edge_similarity_floor=self.config.reverse_edge_similarity_floor,
            reverse_edge_min_scale=self.config.reverse_edge_min_scale,
            reverse_edge_similarity_gamma=self.config.reverse_edge_similarity_gamma,
            # v2：三维度自适应改进参数
            embedding_func=embedding_func,
            adaptive_ppr_damping=self.config.adaptive_ppr_damping,
            adaptive_reverse_edge=self.config.adaptive_reverse_edge,
        )
        self._mmr_reranker = VectorMMRReranker()

    @staticmethod
    def _extract_doc_identifier(chunk: Dict[str, Any]) -> str | None:
        """从 chunk 中提取文档标识（优先 full_doc_id/doc_id）。"""
        for key in ("full_doc_id", "doc_id", "_doc_id"):
            value = chunk.get(key)
            if isinstance(value, str) and value.strip():
                return value.strip()

        metadata = chunk.get("metadata") or {}
        if isinstance(metadata, dict):
            for key in ("full_doc_id", "doc_id"):
                value = metadata.get(key)
                if isinstance(value, str) and value.strip():
                    return value.strip()
        return None

    @staticmethod
    def _extract_file_path(chunk: Dict[str, Any]) -> str | None:
        """从 chunk 中提取来源文件路径，用于 doc_id 缺失时兜底关联。"""
        for key in ("file_path", "source_file_path"):
            value = chunk.get(key)
            if isinstance(value, str) and value.strip():
                return value.strip()

        metadata = chunk.get("metadata") or {}
        if isinstance(metadata, dict):
            for key in ("file_path", "source_file_path"):
                value = metadata.get(key)
                if isinstance(value, str) and value.strip():
                    return value.strip()
        return None

    @staticmethod
    def _normalize_file_path_candidates(value: str | None) -> List[str]:
        """生成用于关联 MySQL 的文件路径候选值。"""
        if not isinstance(value, str) or not value.strip():
            return []

        normalized = value.strip().replace("\\", "/")
        candidates = [normalized]

        try:
            parsed = urlsplit(normalized)
            if parsed.scheme and parsed.path:
                url_path = unquote(parsed.path).replace("\\", "/")
                if url_path:
                    candidates.append(url_path)
                    candidates.append(Path(url_path).name)
            else:
                candidates.append(Path(normalized).name)
        except Exception:
            candidates.append(Path(normalized).name)

        deduped: List[str] = []
        seen: set[str] = set()
        for item in candidates:
            item = item.strip()
            if not item or item in seen:
                continue
            seen.add(item)
            deduped.append(item)
        return deduped

    @staticmethod
    def _extract_year_month_from_value(value: Any) -> tuple[int, int] | None:
        """将 date/datetime/字符串 转换为 (year, month)。"""
        if isinstance(value, datetime):
            return int(value.year), int(value.month)
        if isinstance(value, date):
            return int(value.year), int(value.month)
        if isinstance(value, str) and value.strip():
            text = value.strip()
            for fmt in ("%Y-%m-%d", "%Y/%m/%d", "%Y-%m", "%Y/%m", "%Y%m%d"):
                try:
                    parsed = datetime.strptime(text, fmt)
                    return int(parsed.year), int(parsed.month)
                except Exception:
                    continue
            if len(text) >= 7 and text[4] in ("-", "/"):
                try:
                    y = int(text[:4])
                    m = int(text[5:7])
                    if 1 <= m <= 12:
                        return y, m
                except Exception:
                    pass
        return None

    async def _build_doc_date_map(
        self,
        doc_ids: List[str],
        file_paths: List[str],
    ) -> Dict[str, tuple[int, int]]:
        """批量从 MySQL kb_documents.document_date 获取 year/month。"""
        if not doc_ids and not file_paths:
            return {}

        logger.info(
            "[时间感知] 开始查询文档日期: kb_id=%s, doc_ids=%s, file_paths=%s",
            self.kb_id,
            len(doc_ids),
            len(file_paths),
        )

        try:
            from sqlalchemy import or_, select

            from backend.db.connection import get_async_session_factory
            from backend.db.models.knowledge_base import KnowledgeBaseDocument

            factory = get_async_session_factory()
            async with factory() as session:
                conditions = []
                if doc_ids:
                    conditions.append(KnowledgeBaseDocument.doc_id.in_(doc_ids))
                if file_paths:
                    conditions.append(KnowledgeBaseDocument.file_path.in_(file_paths))

                stmt = select(
                    KnowledgeBaseDocument.doc_id,
                    KnowledgeBaseDocument.file_path,
                    KnowledgeBaseDocument.document_date,
                ).where(KnowledgeBaseDocument.kb_id == self.kb_id)
                if conditions:
                    stmt = stmt.where(or_(*conditions))

                rows = (await session.execute(stmt)).all()

            result: Dict[str, tuple[int, int]] = {}
            for doc_id, file_path, document_date in rows:
                ym = self._extract_year_month_from_value(document_date)
                if ym is None:
                    continue

                if isinstance(doc_id, str) and doc_id.strip():
                    result[doc_id.strip()] = ym

                if isinstance(file_path, str) and file_path.strip():
                    for candidate in self._normalize_file_path_candidates(file_path):
                        result.setdefault(candidate, ym)

            sample_items = list(result.items())[:3]
            logger.info(
                "[时间感知] 文档日期查询完成: 命中=%s, sample=%s",
                len(result),
                sample_items,
            )
            return result
        except Exception as e:
            logger.warning("[时间感知] 读取 kb_documents.document_date 失败: %s", e)
            return {}

    async def _annotate_chunk_time_features(self, chunks: List[Dict[str, Any]]) -> None:
        """
        为每个 chunk 注入 _doc_time_year/_doc_time_month/_doc_time_confidence。

        规则：
        - document_date 命中：confidence=1.0
        - 仅有 created_at：confidence=config.created_at_fallback_beta
        - 无日期：confidence=config.undated_doc_beta（默认 0）
        """
        if not chunks:
            return

        undated_beta = float(self.config.time_aware.undated_doc_beta)
        created_fallback_beta = float(self.config.time_aware.created_at_fallback_beta)

        doc_id_by_idx: Dict[int, str] = {}
        file_path_by_idx: Dict[int, str] = {}
        unique_doc_ids: List[str] = []
        unique_file_paths: List[str] = []
        seen_doc_ids: set[str] = set()
        seen_file_paths: set[str] = set()
        for idx, chunk in enumerate(chunks):
            doc_id = self._extract_doc_identifier(chunk)
            if not doc_id:
                file_path = self._extract_file_path(chunk)
                if file_path:
                    file_path_by_idx[idx] = file_path
                    for candidate in self._normalize_file_path_candidates(file_path):
                        if candidate not in seen_file_paths:
                            seen_file_paths.add(candidate)
                            unique_file_paths.append(candidate)
                continue

            doc_id_by_idx[idx] = doc_id
            if doc_id not in seen_doc_ids:
                seen_doc_ids.add(doc_id)
                unique_doc_ids.append(doc_id)

            file_path = self._extract_file_path(chunk)
            if file_path:
                file_path_by_idx[idx] = file_path
                for candidate in self._normalize_file_path_candidates(file_path):
                    if candidate not in seen_file_paths:
                        seen_file_paths.add(candidate)
                        unique_file_paths.append(candidate)

        doc_date_map = await self._build_doc_date_map(unique_doc_ids, unique_file_paths)

        with_doc_date = 0
        with_doc_date_by_doc_id = 0
        with_doc_date_by_file_path = 0
        with_created_at = 0

        for idx, chunk in enumerate(chunks):
            chunk["_doc_time_year"] = None
            chunk["_doc_time_month"] = None
            chunk["_doc_time_confidence"] = undated_beta

            doc_id = doc_id_by_idx.get(idx)
            file_path = file_path_by_idx.get(idx)
            matched_key = None

            if doc_id and doc_id in doc_date_map:
                matched_key = doc_id
                with_doc_date_by_doc_id += 1
            elif file_path:
                for candidate in self._normalize_file_path_candidates(file_path):
                    if candidate in doc_date_map:
                        matched_key = candidate
                        with_doc_date_by_file_path += 1
                        break

            if matched_key:
                year, month = doc_date_map[matched_key]
                chunk["_doc_time_year"] = year
                chunk["_doc_time_month"] = month
                chunk["_doc_time_confidence"] = 1.0
                with_doc_date += 1
                continue

            created_ym = self._extract_year_month_from_value(chunk.get("created_at"))
            if created_ym is not None:
                chunk["_doc_time_year"], chunk["_doc_time_month"] = created_ym
                chunk["_doc_time_confidence"] = created_fallback_beta
                with_created_at += 1

        logger.info(
            "[时间感知] chunk 时间特征注入完成: total=%s, doc_date=%s (doc_id=%s, file_path=%s), created_at=%s, undated=%s",
            len(chunks),
            with_doc_date,
            with_doc_date_by_doc_id,
            with_doc_date_by_file_path,
            with_created_at,
            len(chunks) - with_doc_date - with_created_at,
        )
    
    async def filter_content(
        self,
        raw_data: RawRetrievalData,
        hl_keywords: List[str],
        query: str,
        mode: FilterMode = FilterMode.FULL,
        query_type: Optional[QueryType] = None,
        seed_weights: Optional[Dict[str, float]] = None,
        skip_assembly: bool = False,
    ) -> FilterResult:
        """
        完整的内容过滤流水线

        流程：
        1. PPR 图过滤（维度1自适应阻尼＋维度2语义反向边）
        2. Rerank 加权融合（维度3自适应融合权重）
        3. MMR 多样性重排序

        Args:
            raw_data: 原始检索数据
            hl_keywords: 高级关键词
            query: 查询文本
            mode: 过滤模式
            query_type: 查询类型（QueryType 枚举或 None，用于肌骨实验）
            seed_weights: 种子权重
            skip_assembly: 是否跳过最终的 Prompt 组装（评测时可开启以节省开销）

        Returns:
            过滤结果
        """
        start_time = time.time()

        # 并行化优化：将图计算 (PPR) 与 远程 API 调用 (Rerank) 并行执行
        if mode == FilterMode.FULL or mode == FilterMode.PPR_RERANK:
            # 先进行 PPR 过滤，再对过滤后的 chunk 调用 Rerank。
            # 这样可保证 rerank 结果索引与 filtered_data.chunks 完全对齐，避免融合错位。
            ppr_data = await self.filter_by_ppr(
                raw_data,
                hl_keywords,
                query_type=query_type,
                seed_weights=seed_weights,
            )
            filtered_data = await self.rerank_chunks(query, ppr_data, query_type=query_type, hl_keywords=hl_keywords)

            if self.config.time_aware.enabled:
                logger.info(
                    "[时间感知] 启用: sigma=%.2f, alpha_base=%.2f, high_boost=%.2f, low_penalty=%.2f, undated_beta=%.2f, created_at_beta=%.2f",
                    float(self.config.time_aware.sigma),
                    float(self.config.time_aware.alpha_base),
                    float(self.config.time_aware.time_high_boost),
                    float(self.config.time_aware.time_low_penalty),
                    float(self.config.time_aware.undated_doc_beta),
                    float(self.config.time_aware.created_at_fallback_beta),
                )
                await self._annotate_chunk_time_features(filtered_data.chunks)
                time_aware_fusion(
                    query=query,
                    chunks=filtered_data.chunks,
                    query_type=query_type,
                    sigma=float(self.config.time_aware.sigma),
                    alpha_base=float(self.config.time_aware.alpha_base),
                    time_high_boost=float(self.config.time_aware.time_high_boost),
                    time_low_penalty=float(self.config.time_aware.time_low_penalty),
                )
                filtered_data.chunks.sort(
                    key=lambda c: float(c.get("_fused_score", c.get("_ppr_score", 0.0))),
                    reverse=True,
                )
            
            if mode == FilterMode.FULL:
                filtered_data.chunks = await self.mmr_rerank(
                    query, filtered_data.chunks, hl_keywords=hl_keywords, query_type=query_type
                )
        elif mode == FilterMode.PPR_ONLY:
            filtered_data = await self.filter_by_ppr(
                raw_data,
                hl_keywords,
                query_type=query_type,
                seed_weights=seed_weights,
            )
        elif mode == FilterMode.PPR_MMR:
            filtered_data = await self.filter_by_ppr(
                raw_data,
                hl_keywords,
                query_type=query_type,
                seed_weights=seed_weights,
            )
            filtered_data.chunks = await self.mmr_rerank(
                query, filtered_data.chunks, query_type=query_type
            )
        elif mode == FilterMode.MMR_ONLY:
            # 跳过 PPR，直接对原始 chunk 做 MMR 重排序
            filtered_data = PPRFilteredData(
                entities=list(raw_data.entities or []),
                relationships=list(raw_data.relationships or []),
                chunks=list(raw_data.chunks or []),
            )
            filtered_data.chunks = await self.mmr_rerank(
                query, filtered_data.chunks, query_type=query_type
            )
        elif mode in [FilterMode.RERANK_MMR, FilterMode.RERANK]:
            # 跳过 PPR，直接进行 Rerank
            filtered_data = PPRFilteredData(
                entities=list(raw_data.entities or []),
                relationships=list(raw_data.relationships or []),
                chunks=list(raw_data.chunks or []),
            )
            # Rerank (由于没有 PPR 分数，强行设置权重为 100% Rerank，避免受底层召回分数的干扰)
            filtered_data = await self.rerank_chunks(
                query, filtered_data, query_type=query_type, hl_keywords=hl_keywords,
                ppr_weight=0.0, rerank_weight=1.0, adaptive_fusion_weight=False,
            )
            # 仅在 RERANK_MMR 模式下执行 MMR
            if mode == FilterMode.RERANK_MMR:
                filtered_data.chunks = await self.mmr_rerank(
                    query, filtered_data.chunks, query_type=query_type
                )
        elif mode == FilterMode.RAW:
            filtered_data = PPRFilteredData(
                entities=list(raw_data.entities or []),
                relationships=list(raw_data.relationships or []),
                chunks=list(raw_data.chunks or []),
            )
        else:
            # 兜底：如果 mode 未匹配，使用 RAW 模式逻辑
            filtered_data = PPRFilteredData(
                entities=list(raw_data.entities or []),
                relationships=list(raw_data.relationships or []),
                chunks=list(raw_data.chunks or []),
            )
        
        if skip_assembly:
            prompt = ""
            logger.info("[内容过滤] 跳过 Prompt 组装 (skip_assembly=True)")
        else:
            prompt = self._ppr_filter.atomic_assembly(filtered_data, query=query)
        
        filter_time_ms = (time.time() - start_time) * 1000
        
        return FilterResult(
            filtered_data=self._convert_to_filtered_data(filtered_data),
            prompt=prompt,
            filter_time_ms=filter_time_ms,
            stats={
                "kb_code": self.kb_code,
                "entities": len(filtered_data.entities),
                "relationships": len(filtered_data.relationships),
                "chunks": len(filtered_data.chunks),
                "mode": mode.value,
            }
        )
    
    async def filter_by_ppr(
        self,
        raw_data: RawRetrievalData,
        hl_keywords: List[str],
        query_type: Optional[QueryType] = None,
        seed_weights: Optional[Dict[str, float]] = None,
    ) -> PPRFilteredData:
        """
        PPR 图过滤（v2：可传入 query_type 开启维度1自适应阻尼）

        Args:
            raw_data: 原始检索数据
            hl_keywords: 高级关键词
            query_type: 查询类型（QueryType 枚举或 None）

        Returns:
            PPR 过滤后的数据
        """
        return await self._ppr_filter.filter_by_ppr(
            raw_data,
            hl_keywords,
            query_type=query_type,
            seed_weights=seed_weights,
        )
    
    async def rerank_chunks(
        self,
        query: str,
        filtered_data: PPRFilteredData,
        ppr_weight: Optional[float] = None,
        rerank_weight: Optional[float] = None,
        top_n: int = 30,
        query_type=None,
        graph_density: Optional[float] = None,
        rerank_results: Optional[List[Dict[str, Any]]] = None,
        hl_keywords: Optional[List[str]] = None,
        adaptive_fusion_weight: Optional[bool] = None,
    ) -> PPRFilteredData:
        """
        重排序加权融合（v2：支持维度3自适应融合权重）

        Args:
            query: 查询文本
            filtered_data: PPR 过滤后的数据
            ppr_weight: PPR 分数权重（递备当自适应关闭时使用）
            rerank_weight: Reranker 分数权重（递备）
            top_n: 返回数量
            query_type: 查询类型（传递给自适应权重计算）
            graph_density: 平均度，默认从 _ppr_filter.graph_density 读取

        Returns:
            融合排序后的数据
        """
        ppr_weight = ppr_weight if ppr_weight is not None else self.config.rerank_ppr_weight
        rerank_weight = rerank_weight if rerank_weight is not None else self.config.rerank_rerank_weight
        # 自动从 PPR 过滤器读取最新图密度（维度3）
        effective_density = graph_density if graph_density is not None else self._ppr_filter.graph_density
        
        use_adaptive = adaptive_fusion_weight if adaptive_fusion_weight is not None else self.config.adaptive_fusion_weight

        return await rerank_filtered_chunks(
            query=query,
            filtered_data=filtered_data,
            top_n=top_n,
            ppr_weight=ppr_weight,
            rerank_weight=rerank_weight,
            # v2 维度3
            graph_density=effective_density,
            query_type=query_type,
            adaptive_fusion_weight=use_adaptive,
            rerank_results=rerank_results,
            hl_keywords=hl_keywords,
            rescue_bonus_config=self.config.rescue_bonus,
        )
    
    async def mmr_rerank(
        self,
        query: str,
        chunks: List[Dict[str, Any]],
        lambda_param: Optional[float] = None,
        top_n: Optional[int] = None,
        hl_keywords: Optional[List[str]] = None,
        query_type: Optional[QueryType] = None,
    ) -> List[Dict[str, Any]]:
        """
        MMR 多样性重排序
        
        Args:
            query: 查询文本
            chunks: 文本块列表
            lambda_param: MMR lambda 参数
            top_n: 返回数量
            
        Returns:
            重排序后的文本块列表
        """
        lambda_param = lambda_param or self.config.mmr_lambda
        top_n = top_n or self.config.mmr_top_n
        
        rerank_scores = {i: chunk.get("_fused_score", chunk.get("_ppr_score", 0.0)) 
                        for i, chunk in enumerate(chunks)}
        
        precomputed_embeddings = [chunk.get("_embedding") for chunk in chunks]
        if any(e is None for e in precomputed_embeddings):
            precomputed_embeddings = None

        protected_chunk_indices = set()
        for i, chunk in enumerate(chunks):
            ppr_score = float(chunk.get("_ppr_score", 0.0) or 0.0)
            rerank_score = float(chunk.get("_rerank_score", 0.0) or 0.0)
            fused_score = float(chunk.get("_fused_score", ppr_score) or 0.0)
            if ppr_score >= 0.08 or rerank_score >= 0.78 or fused_score >= 0.62:
                protected_chunk_indices.add(i)

        return await self._mmr_reranker.rerank(
            query=query,
            chunks=chunks,
            rerank_scores=rerank_scores,
            lambda_param=lambda_param,
            top_n=top_n,
            use_fused_score=True,
            precomputed_embeddings=precomputed_embeddings,
            hl_keywords=hl_keywords,
            protected_chunk_indices=protected_chunk_indices,
        )
    
    def atomic_assembly(
        self,
        filtered_data: PPRFilteredData,
        query: Optional[str] = None,
        max_tokens: Optional[int] = None,
    ) -> str:
        """
        原子化动态组装 Prompt
        
        Args:
            filtered_data: 过滤后的数据
            query: 用户查询
            max_tokens: 最大 Token 数
            
        Returns:
            组装后的 Prompt
        """
        return self._ppr_filter.atomic_assembly(
            filtered_data,
            query=query,
            max_tokens=max_tokens,
        )
    
    def _convert_to_filtered_data(self, ppr_data: PPRFilteredData) -> FilteredData:
        """将 PPRFilteredData 转换为 FilteredData"""
        return FilteredData(
            entities=ppr_data.entities,
            relationships=ppr_data.relationships,
            chunks=ppr_data.chunks,
            entity_ppr_scores=ppr_data.entity_ppr_scores,
            knowledge_paths=ppr_data.knowledge_paths,
        )
