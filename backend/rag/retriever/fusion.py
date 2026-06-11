"""
多路检索结果融合

1. 实体池合并：A+B 种子 + D 图扩展 → 统一实体列表（边感知截断）
2. Chunk 池合并：实体链路 chunks + Path-C 直接向量 chunks
3. Chunk 综合评分（vec + entity_link + coverage）
4. PPR 种子权重双源合并
"""

from __future__ import annotations

from collections import defaultdict
from typing import Any, Dict, List, Optional, Tuple

import numpy as np

from backend.config.loggers import get_logger
from backend.rag.keywords.models import QueryType
from backend.rag.retriever.chunk_searcher import ChunkCandidate
from backend.rag.retriever.config import (
    DirectRetrieverConfig,
    get_chunk_score_weights,
    get_graph_entity_discount,
)
from backend.rag.retriever.entity_recall import SeedEntity
from backend.rag.retriever.graph_expander import GraphExpandedEntity, GraphRelationship

logger = get_logger(__name__)


# ─────────────────────────────────────────────────────────────────
# 1. 实体池合并（边感知截断）
# ─────────────────────────────────────────────────────────────────

def merge_entity_pool(
    ab_seeds: List[SeedEntity],
    d_expanded: List[GraphExpandedEntity],
    d_relationships: List[GraphRelationship],
    query_type: QueryType,
    config: Optional[DirectRetrieverConfig] = None,
) -> Tuple[List[Dict[str, Any]], List[Dict[str, Any]]]:
    """
    合并 A+B 种子实体与 D 图扩展实体，同时返回过滤后的关系列表。

    策略（边感知截断）：
    1. AB 为主，D 折扣后叠加（重叠实体分数累加）
    2. 取分数 top-60% 作为 core 集合
    3. 从剩余候选中，优先选择与 core 有边相连的实体填充余量
    4. 仅保留两端均在最终实体池中的关系

    Returns:
        (merged_entities, filtered_relationship_dicts)
    """
    cfg = config or DirectRetrieverConfig()
    gamma = get_graph_entity_discount(query_type)
    limit = cfg.entity_pool_size

    # ── 构建合并表 ───────────────────────────────────────────────
    pool: Dict[str, Dict[str, Any]] = {}

    for s in ab_seeds:
        d = s.to_dict()
        pool[s.name] = d

    for ge in d_expanded:
        discounted = ge.graph_score * gamma
        if ge.name in pool:
            pool[ge.name]["_fused_score"] = (
                pool[ge.name].get("_fused_score", 0.0) + discounted * 0.5
            )
            if not pool[ge.name].get("entity_type") and ge.entity_type:
                pool[ge.name]["entity_type"] = ge.entity_type
            if not pool[ge.name].get("description") and ge.description:
                pool[ge.name]["description"] = ge.description
            if not pool[ge.name].get("source_id") and ge.source_id:
                pool[ge.name]["source_id"] = ge.source_id
            pool[ge.name]["_recall_path"] = pool[ge.name].get("_recall_path", "") + "+D"
        else:
            seed_e = ge.to_seed_entity()
            seed_e.fused_score = discounted
            d = seed_e.to_dict()
            pool[ge.name] = d

    all_sorted = sorted(pool.values(), key=lambda x: -x.get("_fused_score", 0.0))

    if len(all_sorted) <= limit or not d_relationships:
        merged = all_sorted[:limit]
        surviving = {e["entity_name"] for e in merged}
        filtered_rels = _filter_relationships(d_relationships, surviving)
        logger.info(
            "[Fusion] 实体池合并(无需边感知): AB=%d, D=%d → 合并=%d (limit=%d), 存活边=%d/%d",
            len(ab_seeds), len(d_expanded), len(merged), limit,
            len(filtered_rels), len(d_relationships),
        )
        return merged, filtered_rels

    # ── 边感知截断 ───────────────────────────────────────────────
    # 建立邻接表
    adj: Dict[str, set] = defaultdict(set)
    for rel in d_relationships:
        adj[rel.src_entity].add(rel.dst_entity)
        adj[rel.dst_entity].add(rel.src_entity)

    # Phase-1: 分数 top-60% 作为 core
    core_budget = max(1, int(limit * 0.6))
    core = all_sorted[:core_budget]
    core_names = {e["entity_name"] for e in core}

    # Phase-2: 剩余候选按「分数 + 边连通性加成」排序
    remaining = all_sorted[core_budget:]
    edge_bonus_weight = 0.15
    for r in remaining:
        name = r.get("entity_name", "")
        neighbors_in_core = len(adj.get(name, set()) & core_names)
        r["_edge_bonus"] = neighbors_in_core * edge_bonus_weight

    remaining.sort(
        key=lambda x: -(x.get("_fused_score", 0.0) + x.get("_edge_bonus", 0.0))
    )
    expansion = remaining[: limit - core_budget]

    merged = core + expansion

    # 清理临时字段
    for e in merged:
        e.pop("_edge_bonus", None)

    surviving = {e["entity_name"] for e in merged}
    filtered_rels = _filter_relationships(d_relationships, surviving)

    logger.info(
        "[Fusion] 实体池合并(边感知): AB=%d, D=%d, 候选=%d → core=%d + expansion=%d = %d (limit=%d), "
        "存活边=%d/%d (%.0f%%)",
        len(ab_seeds), len(d_expanded), len(all_sorted),
        len(core), len(expansion), len(merged), limit,
        len(filtered_rels), len(d_relationships),
        100.0 * len(filtered_rels) / max(1, len(d_relationships)),
    )
    return merged, filtered_rels


def _filter_relationships(
    relationships: List[GraphRelationship],
    surviving_names: set,
) -> List[Dict[str, Any]]:
    """仅保留两端均在存活实体集中的关系"""
    return [
        rel.to_dict()
        for rel in relationships
        if rel.src_entity in surviving_names and rel.dst_entity in surviving_names
    ]


# ─────────────────────────────────────────────────────────────────
# 2. 实体覆盖度
# ─────────────────────────────────────────────────────────────────

def chunk_entity_coverage(
    chunk_content: str,
    seeds: List[SeedEntity],
) -> float:
    """
    计算 chunk 命中种子实体的加权比例

        coverage = Σ w(e) * I(e.name in content) / Σ w(e)

    使用 fused_score 作为权重
    """
    if not seeds or not chunk_content:
        return 0.0

    total_weight = sum(s.fused_score for s in seeds)
    if total_weight <= 0:
        return 0.0

    content_lower = chunk_content.lower()
    hit_weight = sum(
        s.fused_score
        for s in seeds
        if s.name.lower() in content_lower
    )
    return hit_weight / total_weight


# ─────────────────────────────────────────────────────────────────
# 3. Chunk 池合并与综合评分
# ─────────────────────────────────────────────────────────────────

def _score_chunk(
    vec_score: float,
    entity_score: float,
    coverage_score: float,
    alpha1: float,
    alpha2: float,
    alpha3: float,
) -> float:
    return alpha1 * vec_score + alpha2 * entity_score + alpha3 * coverage_score


def merge_chunk_pool(
    entity_linked_chunks: List[Dict[str, Any]],
    direct_vec_chunks: List[ChunkCandidate],
    seeds: List[SeedEntity],
    query_type: QueryType,
    config: Optional[DirectRetrieverConfig] = None,
) -> List[Dict[str, Any]]:
    """
    合并两路 chunks 并计算综合评分

    entity_linked_chunks : 实体→source_id→chunk 链路
    direct_vec_chunks    : Path-C 直接 ANN
    """
    cfg = config or DirectRetrieverConfig()
    alpha1, alpha2, alpha3 = get_chunk_score_weights(query_type)

    # 去重表：chunk_id → dict
    pool: Dict[str, Dict[str, Any]] = {}

    # ── 实体链路 chunks ──────────────────────────────────────────
    for ch in entity_linked_chunks:
        cid = _extract_chunk_id(ch)
        content = ch.get("content", "")
        vec_s = float(ch.get("_vec_score", 0.0))
        ent_s = float(ch.get("_entity_score", 0.5))   # 已在 fetch 时标注
        cov_s = chunk_entity_coverage(content, seeds)

        final = _score_chunk(vec_s, ent_s, cov_s, alpha1, alpha2, alpha3)
        ch = ch.copy()
        ch["_vec_score"]       = vec_s
        ch["_entity_score"]    = ent_s
        ch["_coverage_score"]  = cov_s
        ch["_chunk_score"]     = final
        ch["_recall_path"]     = ch.get("_recall_path", "entity")

        if cid not in pool or pool[cid].get("_chunk_score", 0.0) < final:
            pool[cid] = ch

    # ── Path-C 直接向量 chunks ───────────────────────────────────
    for cc in direct_vec_chunks:
        cid = cc.chunk_id
        cov_s = chunk_entity_coverage(cc.content, seeds)
        final = _score_chunk(cc.vec_score, 0.0, cov_s, alpha1, alpha2, alpha3)

        if cid not in pool:
            d = cc.to_retrieval_dict()
            d["_coverage_score"] = cov_s
            d["_chunk_score"]    = final
            pool[cid] = d
        else:
            # 【双源召回奖励】同一 chunk 同时被实体链路和向量找到
            existing = pool[cid]
            
            # Bonus: 当一个块既包含强实体关系，又在语义向量上与 Query 接近时，给予 20% 加成
            dual_bonus = 0.20
            
            # 更新向量分
            existing["_vec_score"] = max(existing.get("_vec_score", 0.0), cc.vec_score)
            
            # 重新计算综合分：利用已有的 entity_score 和新更新的 vec_score
            new_final = _score_chunk(existing["_vec_score"], existing.get("_entity_score", 0.0), cov_s, alpha1, alpha2, alpha3)
            new_final = new_final * (1.0 + dual_bonus)
            
            if new_final > existing.get("_chunk_score", 0.0):
                existing["_coverage_score"] = cov_s
                existing["_chunk_score"]    = new_final
                
            existing["_recall_path"] = "entity+vec"

    merged = list(pool.values())
    merged.sort(key=lambda x: -x.get("_chunk_score", 0.0))
    merged = merged[: cfg.chunk_pool_size]

    logger.info(
        "[Fusion] Chunk 池合并: entity_linked=%d, vec=%d → 合并=%d (limit=%d)",
        len(entity_linked_chunks), len(direct_vec_chunks),
        len(merged), cfg.chunk_pool_size,
    )
    return merged


def _extract_chunk_id(ch: Dict[str, Any]) -> str:
    for key in ("chunk_id", "id", "_id"):
        v = ch.get(key)
        if isinstance(v, str) and v.strip():
            return v.strip()
    return str(id(ch))


# ─────────────────────────────────────────────────────────────────
# 4. PPR 种子权重双源合并
# ─────────────────────────────────────────────────────────────────

def build_ppr_seed_weights(
    entities: List[Dict[str, Any]],
    keyword_seed_weights: Dict[str, float],
    blend_ratio: float = 0.5,
) -> Dict[str, float]:
    """
    合并关键词阶段权重（_seed_weight）与多路检索阶段权重（_fused_score）

    blend_ratio：检索阶段权重占比（0=仅关键词, 1=仅检索）
    """
    merged: Dict[str, float] = {}

    # 关键词阶段权重：L1 归一化
    kw_total = sum(keyword_seed_weights.values()) or 1.0
    kw_norm = {k: v / kw_total for k, v in keyword_seed_weights.items()}

    # 检索阶段权重
    ret_scores = {
        e["entity_name"]: e.get("_fused_score", 0.0)
        for e in entities
        if e.get("entity_name")
    }
    ret_total = sum(ret_scores.values()) or 1.0
    ret_norm = {k: v / ret_total for k, v in ret_scores.items()}

    # 所有实体名
    all_names = set(kw_norm) | set(ret_norm)
    for name in all_names:
        kw_w  = kw_norm.get(name, 0.0)
        ret_w = ret_norm.get(name, 0.0)
        merged[name] = (1.0 - blend_ratio) * kw_w + blend_ratio * ret_w

    # L1 归一化
    total = sum(merged.values()) or 1.0
    return {k: v / total for k, v in merged.items() if v > 0}
