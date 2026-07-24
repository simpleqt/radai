"""
Path-A（实体向量召回）+ Path-B（BM25 召回）+ RRF 融合

输出：SeedEntity 列表，携带多路原始分数与融合分
"""

from __future__ import annotations

import asyncio
from dataclasses import dataclass
from typing import Any, Callable, Dict, List, Optional, Tuple

import numpy as np

from backend.config.loggers import get_logger
from backend.core.utils.math import cosine_similarity
from backend.rag.keywords.models import QueryType
from backend.rag.retriever.config import (
    DirectRetrieverConfig,
    get_entity_vec_score_floor,
    get_entity_vec_top_k,
)

logger = get_logger(__name__)


# ─────────────────────────────────────────────────────────────────
# 数据结构
# ─────────────────────────────────────────────────────────────────

@dataclass
class SeedEntity:
    """融合后的种子实体"""
    name: str
    fused_score: float = 0.0
    vec_score: float = 0.0
    bm25_score: float = 0.0
    seed_weight: float = 0.0        # 来自关键词阶段 weighted_seed_scores
    graph_hop: int = 0              # 0=种子, 1=1-hop, 2=2-hop
    in_hl: bool = False             # 是否在 hl_keywords 中命中
    in_ll: bool = False             # 是否在 ll_keywords 中命中
    recall_path: str = "AB"         # 来源路径标记
    entity_type: str = ""
    description: str = ""
    source_id: str = ""
    file_path: str = ""

    def to_dict(self) -> Dict[str, Any]:
        return {
            "entity_name":    self.name,
            "entity_type":    self.entity_type,
            "description":    self.description,
            "source_id":      self.source_id,
            "file_path":      self.file_path,
            "_vec_score":     self.vec_score,
            "_bm25_score":    self.bm25_score,
            "_seed_weight":   self.seed_weight,
            "_graph_hop":     self.graph_hop,
            "_fused_score":   self.fused_score,
            "_recall_path":   self.recall_path,
            "_in_hl":         self.in_hl,
            "_in_ll":         self.in_ll,
        }


# ─────────────────────────────────────────────────────────────────
# RRF 工具
# ─────────────────────────────────────────────────────────────────

def _rrf_score(rank: int, k: int = 60) -> float:
    return 1.0 / (k + rank + 1)


def rrf_fusion(
    *ranked_lists: List[Tuple[str, float]],
    k: int = 60,
) -> Dict[str, float]:
    """
    多路 RRF 融合
    每路输入 [(entity_name, score), ...] 已按 score 降序
    返回 {entity_name: rrf_score}
    """
    scores: Dict[str, float] = {}
    for ranked in ranked_lists:
        for rank, (name, _) in enumerate(ranked):
            scores[name] = scores.get(name, 0.0) + _rrf_score(rank, k)
    return scores


# ─────────────────────────────────────────────────────────────────
# Path-A + Path-B 融合器
# ─────────────────────────────────────────────────────────────────

class EntityRecaller:
    """
    Path-A（Milvus 向量 ANN）+ Path-B（Redis BM25 + 多关键词扩展）实体召回
    并通过 RRF + 种子权重注入产出 SeedEntity 列表
    """

    def __init__(
        self,
        keyword_index,              # PerKBKeywordIndex
        embedding_func: Callable,
        config: Optional[DirectRetrieverConfig] = None,
    ):
        self._index = keyword_index
        self._embedding_func = embedding_func
        self._config = config or DirectRetrieverConfig()

    async def _diversify_seeds(
        self,
        seeds: List[SeedEntity],
    ) -> List[SeedEntity]:
        cfg = self._config
        top_n = min(max(1, cfg.seed_mmr_top_n), len(seeds))
        if top_n <= 1:
            return seeds

        candidates = seeds[:top_n]
        selected: List[SeedEntity] = []
        remaining = list(candidates)

        name_to_embedding: Dict[str, np.ndarray] = {}
        if cfg.seed_mmr_lambda > 0:
            texts = []
            emb_names = []
            for seed in candidates:
                desc = (seed.description or "").strip()
                texts.append(f"{seed.name}: {desc[:80]}" if desc else seed.name)
                emb_names.append(seed.name)
            try:
                embeddings = await self._embedding_func(texts)
                if embeddings is not None and len(embeddings) == len(emb_names):
                    name_to_embedding = {
                        name: np.array(emb, dtype=np.float32)
                        for name, emb in zip(emb_names, embeddings)
                    }
            except Exception as e:
                logger.warning("[EntityRecall] 种子 MMR embedding 失败: %s", e)

        def _mmr_penalty(seed: SeedEntity) -> float:
            if cfg.seed_mmr_lambda <= 0 or not selected:
                return 0.0
            cand_emb = name_to_embedding.get(seed.name)
            if cand_emb is None:
                return 0.0
            max_sim = 0.0
            for picked in selected:
                picked_emb = name_to_embedding.get(picked.name)
                if picked_emb is None:
                    continue
                max_sim = max(max_sim, cosine_similarity(cand_emb, picked_emb))
            return cfg.seed_mmr_lambda * max_sim

        while remaining:
            scored = []
            for seed in remaining:
                score = seed.fused_score - _mmr_penalty(seed)
                scored.append((score, seed))
            scored.sort(key=lambda item: item[0], reverse=True)
            chosen = scored[0][1]
            selected.append(chosen)
            remaining = [seed for seed in remaining if seed.name != chosen.name]

        if top_n < len(seeds):
            selected.extend(seeds[top_n:])

        logger.info(
            "[EntityRecall] 种子多样化: top_n=%d, lambda=%.2f",
            top_n,
            cfg.seed_mmr_lambda,
        )
        return selected

    # ── Path-A ──────────────────────────────────────────────────

    async def _path_a(
        self,
        query_embedding: np.ndarray,
        hl_keywords: List[str],
        query_type: QueryType,
    ) -> List[Tuple[str, float]]:
        """
        Milvus ANN 实体向量召回
        优化（v4）：双向表征对齐。
        针对“句向量搜短实体名”的 Modality Gap 问题，引入 hl_keywords 向量召回作为补充。
        """
        top_k = get_entity_vec_top_k(query_type)
        score_floor = get_entity_vec_score_floor(query_type)
        cfg = self._config

        try:
            tasks = [self._index.search_vector(query_embedding, top_k=top_k)]
            
            # 扩展召回：针对 hl_keywords 进行向量检索，对齐实体表征尺度
            if hl_keywords:
                # 仅对前 3 个核心关键词进行向量扩展，避免 API 调用过多
                kw_to_embed = hl_keywords[:3]
                try:
                    kw_embs = await self._embedding_func(kw_to_embed)
                    if kw_embs is not None:
                        for kw_emb in kw_embs:
                            tasks.append(self._index.search_vector(np.array(kw_emb), top_k=top_k))
                except Exception as ex:
                    logger.warning("[Path-A] 关键词 embedding 失败: %s", ex)

            all_results = await asyncio.gather(*tasks, return_exceptions=True)
            
            # 多路结果 RRF 融合
            valid_results = [res for res in all_results if isinstance(res, list)]
            if not valid_results:
                return []
            
            if len(valid_results) == 1:
                results = valid_results[0]
            else:
                # 使用 RRF 融合多路向量召回（Query + Keywords）
                rrf_map = rrf_fusion(*valid_results, k=cfg.rrf_k)
                # 将 RRF 分数映射回 0~1 的伪相似度，以便后续 floor 过滤一致
                max_rrf = max(rrf_map.values(), default=1.0)
                results = [(name, score / max_rrf) for name, score in rrf_map.items()]
                results.sort(key=lambda x: -x[1])

            filtered = [(name, s) for name, s in results if s >= score_floor]
            # 兜底：若全部被 floor 过滤，降级到 top-3 避免 Path-A 完全失效
            if not filtered and results:
                fallback_floor = results[0][1] * 0.5  # 取最高分的一半作为宽松阈值
                filtered = [(name, s) for name, s in results[:3] if s >= fallback_floor]
                logger.info("[Path-A] 向量召回兜底(floor降级): 原始=%d, 列表数=%d, floor=%.2f→%.3f, 保留=%d",
                            len(results), len(valid_results), score_floor, fallback_floor, len(filtered))
            else:
                logger.info("[Path-A] 向量召回: 原始=%d, 列表数=%d, 过滤后=%d (floor=%.2f)",
                            len(results), len(valid_results), len(filtered), score_floor)
            return filtered
        except Exception as e:
            logger.warning("[Path-A] 向量召回失败: %s", e)
            return []

    # ── Path-B ──────────────────────────────────────────────────

    async def _path_b(
        self,
        query: str,
        hl_keywords: List[str],
        query_type: QueryType,
    ) -> List[Tuple[str, float]]:
        """Redis BM25 + 多关键词扩展召回"""
        cfg = self._config
        top_k = cfg.entity_bm25_top_k

        try:
            # 主查询
            base_results = await self._index.search_bm25(query, top_k=top_k)
            all_lists: List[List[Tuple[str, float]]] = [base_results]

            # 关键词扩展
            if cfg.entity_bm25_expand_keywords and hl_keywords:
                expand_tasks = [
                    self._index.search_bm25(kw, top_k=cfg.entity_bm25_keyword_expand_top_k)
                    for kw in hl_keywords[:5]  # 最多扩展 5 个关键词
                ]
                expanded = await asyncio.gather(*expand_tasks, return_exceptions=True)
                for res in expanded:
                    if isinstance(res, list) and res:
                        all_lists.append(res)

            if len(all_lists) == 1:
                logger.info("[Path-B] BM25 召回: %d 条（无扩展）", len(base_results))
                return base_results

            # 多路 RRF 融合
            rrf = rrf_fusion(*all_lists, k=cfg.rrf_k)
            merged = sorted(rrf.items(), key=lambda x: -x[1])
            logger.info("[Path-B] BM25+扩展 RRF 融合: 列表数=%d, 结果=%d",
                        len(all_lists), len(merged))
            return merged

        except Exception as e:
            logger.warning("[Path-B] BM25 召回失败: %s", e)
            return []

    # ── A+B 融合 → SeedEntity ───────────────────────────────────

    async def recall(
        self,
        query: str,
        query_embedding: np.ndarray,
        hl_keywords: List[str],
        ll_keywords: List[str],
        query_type: QueryType,
        weighted_seed_scores: Dict[str, float],
    ) -> List[SeedEntity]:
        """
        并行执行 Path-A 和 Path-B，RRF 融合后注入种子权重
        返回按 fused_score 降序排列的 SeedEntity 列表
        """
        cfg = self._config

        # 并行执行
        path_a_task = self._path_a(query_embedding, hl_keywords, query_type) if cfg.enable_path_a else asyncio.sleep(0, result=[])
        path_b_task = self._path_b(query, hl_keywords, query_type) if cfg.enable_path_b else asyncio.sleep(0, result=[])

        vec_results, bm25_results = await asyncio.gather(path_a_task, path_b_task)

        # 构建原始分数映射（用于最终标注）
        vec_score_map = dict(vec_results)
        bm25_score_map = dict(bm25_results)

        # RRF 融合
        rrf_scores = rrf_fusion(vec_results, bm25_results, k=cfg.rrf_k)

        # hl/ll 快速查找集合
        hl_set = {kw.lower() for kw in hl_keywords}
        ll_set = {kw.lower() for kw in ll_keywords}

        # 种子权重最大值（用于归一化）
        max_seed = max(weighted_seed_scores.values(), default=1.0) or 1.0

        # 组装 SeedEntity
        seeds: List[SeedEntity] = []
        for name, rrf_s in rrf_scores.items():
            name_lower = name.lower()
            seed_w = weighted_seed_scores.get(name, 0.0) / max_seed

            # 种子权重提升：hl_keywords 中命中给一个小 bonus
            beta_seed = seed_w * 0.2
            final_score = rrf_s + beta_seed

            meta = self._index.entity_metadata.get(name, {})
            seeds.append(SeedEntity(
                name=name,
                fused_score=final_score,
                vec_score=vec_score_map.get(name, 0.0),
                bm25_score=bm25_score_map.get(name, 0.0),
                seed_weight=seed_w,
                graph_hop=0,
                in_hl=name_lower in hl_set,
                in_ll=name_lower in ll_set,
                recall_path="AB",
                entity_type=meta.get("entity_type", ""),
                description=meta.get("description", ""),
                source_id=meta.get("source_id", ""),
                file_path=meta.get("file_path", ""),
            ))

        seeds.sort(key=lambda e: -e.fused_score)
        if cfg.seed_mmr_lambda > 0 or cfg.seed_type_balance_enabled:
            seeds = await self._diversify_seeds(seeds)
        logger.info("[EntityRecall] 种子实体: vec=%d, bm25=%d, 融合后=%d",
                    len(vec_results), len(bm25_results), len(seeds))
        return seeds
