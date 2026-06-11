"""
DirectRetriever 配置

QueryType 感知策略矩阵与全局参数
"""

import asyncio
import os
from dataclasses import dataclass
from typing import Callable, Dict, List, Optional

from backend.rag.keywords.models import QueryType


# ─────────────────────────────────────────────────────────────────
# QueryType → 策略矩阵
# ─────────────────────────────────────────────────────────────────

# Path-A：实体向量 top_k
ENTITY_VEC_TOP_K: Dict[str, int] = {
    QueryType.PARAMETER.value:  25,
    QueryType.EQUIPMENT.value:  20,
    QueryType.CONCEPT.value:    15,
    QueryType.COMPARISON.value: 25,
    QueryType.GENERAL.value:    20,
}

# Path-A：向量相似度下限
ENTITY_VEC_SCORE_FLOOR: Dict[str, float] = {
    QueryType.PARAMETER.value:  0.30,
    QueryType.EQUIPMENT.value:  0.28,
    QueryType.CONCEPT.value:    0.25,
    QueryType.COMPARISON.value: 0.28,
    QueryType.GENERAL.value:    0.28,
}

# Path-C：Chunk ANN top_k 乘数
CHUNK_VEC_TOP_K_MULTIPLIER: Dict[str, float] = {
    QueryType.PARAMETER.value:  1.0,
    QueryType.EQUIPMENT.value:  1.5,
    QueryType.CONCEPT.value:    2.0,
    QueryType.COMPARISON.value: 2.0,
    QueryType.GENERAL.value:    1.2,
}

# Path-C：Chunk 向量分数下限
CHUNK_VEC_SCORE_FLOOR: Dict[str, float] = {
    QueryType.PARAMETER.value:  0.35,
    QueryType.EQUIPMENT.value:  0.28,
    QueryType.CONCEPT.value:    0.20,
    QueryType.COMPARISON.value: 0.22,
    QueryType.GENERAL.value:    0.28,
}

# Path-D：图遍历深度
GRAPH_DEPTH: Dict[str, int] = {
    QueryType.PARAMETER.value:  1,
    QueryType.EQUIPMENT.value:  1,
    QueryType.CONCEPT.value:    2,
    QueryType.COMPARISON.value: 2,
    QueryType.GENERAL.value:    1,
}

# Chunk 综合评分权重 (vec, entity_link, coverage)
CHUNK_SCORE_WEIGHTS: Dict[str, tuple] = {
    QueryType.GENERAL.value:    (0.5, 0.3, 0.2),
    QueryType.PARAMETER.value:  (0.3, 0.4, 0.3),
    QueryType.CONCEPT.value:    (0.6, 0.2, 0.2),
    QueryType.EQUIPMENT.value:  (0.4, 0.4, 0.2),
    QueryType.COMPARISON.value: (0.5, 0.2, 0.3),
}

# 图扩展实体折扣系数
GRAPH_ENTITY_DISCOUNT: Dict[str, float] = {
    QueryType.GENERAL.value:    0.5,
    QueryType.PARAMETER.value:  0.4,
    QueryType.CONCEPT.value:    0.6,
    QueryType.EQUIPMENT.value:  0.5,
    QueryType.COMPARISON.value: 0.6,
}


def get_query_type_value(qt: QueryType) -> str:
    return qt.value if isinstance(qt, QueryType) else str(qt)


def get_entity_vec_top_k(qt: QueryType) -> int:
    return ENTITY_VEC_TOP_K.get(get_query_type_value(qt), 20)


def get_entity_vec_score_floor(qt: QueryType) -> float:
    return ENTITY_VEC_SCORE_FLOOR.get(get_query_type_value(qt), 0.28)


def get_chunk_vec_top_k_multiplier(qt: QueryType) -> float:
    return CHUNK_VEC_TOP_K_MULTIPLIER.get(get_query_type_value(qt), 1.2)


def get_chunk_vec_score_floor(qt: QueryType) -> float:
    return CHUNK_VEC_SCORE_FLOOR.get(get_query_type_value(qt), 0.28)


def get_graph_depth(qt: QueryType) -> int:
    return GRAPH_DEPTH.get(get_query_type_value(qt), 1)


def get_chunk_score_weights(qt: QueryType) -> tuple:
    return CHUNK_SCORE_WEIGHTS.get(get_query_type_value(qt), (0.5, 0.3, 0.2))


def get_graph_entity_discount(qt: QueryType) -> float:
    return GRAPH_ENTITY_DISCOUNT.get(get_query_type_value(qt), 0.5)


# ─────────────────────────────────────────────────────────────────
# 全局配置类
# ─────────────────────────────────────────────────────────────────

@dataclass
class DirectRetrieverConfig:
    """DirectRetriever 全局配置（可通过环境变量/运行时覆盖）"""

    # Path-A
    entity_vec_top_k: int = 20
    entity_vec_score_floor: float = 0.28

    # Path-B
    entity_bm25_top_k: int = 15
    entity_bm25_expand_keywords: bool = True
    entity_bm25_keyword_expand_top_k: int = 8
    rrf_k: int = 60

    # 种子多样化
    seed_mmr_lambda: float = 0.25
    seed_mmr_top_n: int = 10
    seed_type_balance_enabled: bool = False

    # Path-C
    chunk_vec_top_k: int = 40
    chunk_vec_score_floor: float = 0.25
    chunk_vec_collection_override: Optional[str] = None

    # Path-D
    graph_depth: int = 1
    graph_relation_weight_floor: float = 0.45
    graph_max_neighbors_per_seed: int = 10  # 从15降到10
    graph_max_seeds: int = 30               # 图扩展最多使用 top-N 种子，限制 NebulaGraph 查询规模
    graph_entity_discount: float = 0.5

    # 融合池上限
    entity_pool_size: int = 150  # 需保留足够多的图扩展实体以维持 PPR 子图连通性
    chunk_pool_size: int = 60

    # 并发控制
    max_concurrent_chunk_fetches: int = 10

    # 是否启用各路径（用于消融实验）
    enable_path_a: bool = True      # 实体向量
    enable_path_b: bool = True      # 实体 BM25
    enable_path_c: bool = True      # Chunk 语义
    enable_path_d: bool = True      # 图扩展

    # PPR 种子权重合并比例（0=仅关键词阶段, 1=仅检索阶段）
    ppr_seed_blend_ratio: float = 0.5

    # Rerank 融合是否加入 chunk_score 作为第三维
    use_chunk_score_in_rerank: bool = False
    chunk_score_rerank_weight: float = 0.15


def direct_retriever_config_from_env() -> DirectRetrieverConfig:
    """从环境变量构建 DirectRetrieverConfig（供进程内缓存的检索器使用）。

    - ``RAG_RETRIEVE_FAST=1``：预设更小实体池 / 图扩展 / BM25，换召回换延迟。
    - 亦可逐项覆盖：``RAG_ENTITY_POOL_SIZE``、``RAG_CHUNK_POOL_SIZE``、
      ``RAG_GRAPH_MAX_SEEDS``、``RAG_GRAPH_MAX_NEIGHBORS_PER_SEED``、
      ``RAG_ENTITY_BM25_TOP_K``、``RAG_ENTITY_BM25_KEYWORD_EXPAND_TOP_K``。
    """
    cfg = DirectRetrieverConfig()
    fast = os.getenv("RAG_RETRIEVE_FAST", "").strip().lower() in ("1", "true", "yes", "on")
    if fast:
        cfg.entity_pool_size = min(cfg.entity_pool_size, 90)
        cfg.chunk_pool_size = min(cfg.chunk_pool_size, 40)
        cfg.graph_max_seeds = min(cfg.graph_max_seeds, 20)
        cfg.graph_max_neighbors_per_seed = min(cfg.graph_max_neighbors_per_seed, 10)
        cfg.entity_bm25_top_k = min(cfg.entity_bm25_top_k, 10)
        cfg.entity_bm25_keyword_expand_top_k = min(cfg.entity_bm25_keyword_expand_top_k, 5)

    def _opt_int(name: str) -> Optional[int]:
        raw = os.getenv(name)
        if raw is None or not str(raw).strip():
            return None
        try:
            return int(raw)
        except ValueError:
            return None

    if (v := _opt_int("RAG_ENTITY_POOL_SIZE")) is not None and v >= 10:
        cfg.entity_pool_size = v
    if (v := _opt_int("RAG_CHUNK_POOL_SIZE")) is not None and v >= 5:
        cfg.chunk_pool_size = v
    if (v := _opt_int("RAG_GRAPH_MAX_SEEDS")) is not None and v >= 5:
        cfg.graph_max_seeds = v
    if (v := _opt_int("RAG_GRAPH_MAX_NEIGHBORS_PER_SEED")) is not None and v >= 3:
        cfg.graph_max_neighbors_per_seed = v
    if (v := _opt_int("RAG_ENTITY_BM25_TOP_K")) is not None and v >= 3:
        cfg.entity_bm25_top_k = v
    if (v := _opt_int("RAG_ENTITY_BM25_KEYWORD_EXPAND_TOP_K")) is not None and v >= 2:
        cfg.entity_bm25_keyword_expand_top_k = v

    return cfg


# ─────────────────────────────────────────────────────────────────
# 通用 embedding 调用辅助（兼容 async / sync 两种 embedding_func）
# ─────────────────────────────────────────────────────────────────

async def call_embedding_func(
    embedding_func: Callable,
    texts: List[str],
) -> List:
    """
    统一调用 embedding_func，兼容两种形式：
      - 协程函数（async def / functools.partial 等 wrapped）：直接 await
      - 普通同步函数：通过 asyncio.to_thread 在线程池中运行

    判断策略：
      1. asyncio.iscoroutinefunction 检测（标准协程函数）
      2. inspect.iscoroutinefunction 检测（覆盖 functools.partial 等 wrapper）
      3. 实际调用后结果是 coroutine → await 它（最终兜底）
    """
    import inspect

    is_coro = (
        asyncio.iscoroutinefunction(embedding_func)
        or inspect.iscoroutinefunction(embedding_func)
    )

    if is_coro:
        return await embedding_func(texts)

    # 尝试同步调用；若返回 coroutine（callable class / partial 漏检），再 await
    result = await asyncio.to_thread(embedding_func, texts)
    if asyncio.iscoroutine(result):
        return await result
    return result
