"""
Rerank 加权融合重排序

支持通过 API 调用 Rerank 模型（如 Qwen3-Reranker-8B）

v2 改进：
  维度3 - 自适应融合权重：基于图密度+查询类型动态调整 PPR/Rerank 融合比例
"""

import httpx
import math
import numpy as np
from datetime import datetime
from typing import Any, Dict, List, Optional

from backend.config.loggers import get_logger
from backend.config.settings import settings
from backend.rag.content_filter.ppr_filter import PPRFilteredData
from backend.rag.keywords.models import QueryType

logger = get_logger(__name__)

HIGH_TIME_WORDS = [
    "最新", "最近", "近年", "当前", "进展", "前沿", "新型",
    "汇报", "近期", "刚发表", "最近几年", "近期研究",
]

LOW_TIME_WORDS = [
    "原理", "定义", "公式", "参数", "规格", "结构", "组成",
    "基础", "经典", "历史", "传统", "早期",
]


def _normalize_scores_safe(
    scores: Dict[int, float],
    method: str = "zscore"
) -> Dict[int, float]:
    """安全的分数归一化，处理所有值相同的边界情况"""
    if not scores:
        return {}

    values = list(scores.values())
    min_val, max_val = min(values), max(values)

    if max_val == min_val:
        # 保持原始顺序，赋予微小差异，避免全 0.5
        return {
            idx: 0.5 + (i / max(1, len(scores))) * 0.01
            for i, idx in enumerate(scores.keys())
        }

    if method == "zscore":
        mean_val = np.mean(values)
        std_val = np.std(values)
        if std_val < 1e-6:
            return {idx: 0.5 for idx in scores.keys()}

        z_scores = {idx: (score - mean_val) / std_val for idx, score in scores.items()}
        return {idx: 1 / (1 + math.exp(-z)) for idx, z in z_scores.items()}

    # fallback 为 min-max
    range_val = max_val - min_val
    if range_val <= 0:
        return {idx: 0.5 for idx in scores.keys()}

    return {idx: (score - min_val) / range_val for idx, score in scores.items()}


async def call_rerank_api(
    query: str,
    documents: List[str],
    top_n: int,
) -> List[Dict[str, any]]:
    """
    调用 Rerank API
    
    支持：
    - DashScope: https://dashscope.aliyuncs.com/compatible-mode/v1/reranks
    - vLLM: http://x.x.x.x:8012/v1/rerank
    """
    api_base = (settings.rerank_api_base or "").rstrip("/")
    api_key = settings.rerank_api_key
    model = settings.rerank_model or "Qwen3-Reranker-8B"
    
    if not api_base or not api_key:
        logger.warning("[Rerank] API 配置不完整，跳过 Rerank")
        return []
    
    if "/v1" in api_base and "dashscope" not in api_base.lower():
        rerank_endpoint = f"{api_base}/rerank"
    else:
        rerank_endpoint = f"{api_base}/reranks"
    
    try:
        async with httpx.AsyncClient(timeout=60.0) as client:
            resp = await client.post(
                rerank_endpoint,
                json={
                    "model": model,
                    "query": query,
                    "documents": documents,
                    "top_n": top_n,
                },
                headers={
                    "Authorization": f"Bearer {api_key}",
                    "Content-Type": "application/json",
                },
            )
            
            if resp.status_code == 200:
                data = resp.json()
                if "results" in data and data["results"]:
                    return [
                        {
                            "index": result.get("index", 0),
                            "relevance_score": result.get("relevance_score", 0.0),
                        }
                        for result in data["results"]
                    ]
            else:
                logger.warning(f"[Rerank] API 返回状态码 {resp.status_code}")
                return []
    except Exception as e:
        logger.warning(f"[Rerank] API 调用失败: {e}")
        return []
    
    return []


def compute_adaptive_fusion_weights(
    graph_density: float = 0.0,
    query_type=None,
    base_ppr_weight: float = 0.4,
) -> tuple:
    """
    维度3：PPR 与 Rerank 自适应融合权重计算

    公式：
        w_ppr = clip(base - 0.1×I_dense + 0.1×I_sparse + Δ_query, 0.2, 0.8)
        w_rerank = 1 - w_ppr

    Δ_query：
        CONCEPT/COMPARISON → +0.05  (结构信息更重要，提高 PPR 权重)
        PARAMETER          → -0.05  (语义相关性更重要，降低 PPR 权重)
        其他            →  0.00

    Args:
        graph_density: 平均度（d_avg = 2|E|/|V|），0 表示未知
        query_type: QueryType 枚举或 None
        base_ppr_weight: PPR 基准权重（默认 0.4）

    Returns:
        (ppr_weight, rerank_weight) 元组
    """
    ppr_w = base_ppr_weight

    # 图密度调整
    if graph_density > 10:
        ppr_w -= 0.1  # 稠密图：PPR 可靠性下降
    elif 0 < graph_density < 3:
        ppr_w += 0.1  # 稀疏图：PPR 提供独特结构信息

    # 查询类型调整
    if query_type is not None:
        try:
            from backend.rag.keywords.models import QueryType
            if query_type in (QueryType.CONCEPT, QueryType.COMPARISON):
                ppr_w += 0.05
            elif query_type == QueryType.PARAMETER:
                ppr_w -= 0.05
        except Exception:
            pass

    ppr_w = float(np.clip(ppr_w, 0.2, 0.8))
    rerank_w = round(1.0 - ppr_w, 6)  # 中和FP精度误差

    logger.debug(
        f"[自适应融合] base_ppr={base_ppr_weight}, d_avg={graph_density:.2f} "
        f"→ ppr_w={ppr_w:.3f}, rerank_w={rerank_w:.3f}"
    )
    return ppr_w, rerank_w


def gaussian_decay(
    pub_year: int,
    pub_month: int,
    current_year: int,
    current_month: int,
    sigma: float,
) -> float:
    """计算高斯时间衰减分数。"""
    delta_years = (current_year - pub_year) + (current_month - pub_month) / 12.0
    if delta_years < 0:
        delta_years = 0.0
    sigma = max(0.1, float(sigma))
    return float(math.exp(- (delta_years ** 2) / (2 * sigma ** 2)))


def estimate_time_sensitivity(
    query: str,
    query_type: QueryType | None,
    alpha_base: float = 0.3,
    time_high_boost: float = 0.4,
    time_low_penalty: float = 0.3,
) -> float:
    """根据 QueryType + 查询词估计时间敏感度，范围 [0, 1]。"""
    base = {
        QueryType.PARAMETER: 0.0,
        QueryType.EQUIPMENT: 0.1,
        QueryType.CONCEPT: 0.3,
        QueryType.COMPARISON: 0.4,
        QueryType.GENERAL: float(alpha_base),
    }.get(query_type, float(alpha_base))

    for word in HIGH_TIME_WORDS:
        if word in query:
            base += float(time_high_boost)
            break

    for word in LOW_TIME_WORDS:
        if word in query:
            base -= float(time_low_penalty)
            break

    return float(max(0.0, min(1.0, base)))


def time_aware_fusion(
    query: str,
    chunks: List[Dict[str, Any]],
    query_type: QueryType | None,
    sigma: float,
    alpha_base: float,
    time_high_boost: float,
    time_low_penalty: float,
) -> None:
    """
    在已有 _fused_score 基础上叠加查询感知时间权重，原地更新 chunks 的 _fused_score。

    约定：
    - _doc_time_year/_doc_time_month 由上游注入（优先来自 document_date）
    - _doc_time_confidence 取值 [0,1]，无日期时为 0
    """
    if not chunks:
        return

    alpha_time = estimate_time_sensitivity(
        query=query,
        query_type=query_type,
        alpha_base=alpha_base,
        time_high_boost=time_high_boost,
        time_low_penalty=time_low_penalty,
    )
    if alpha_time <= 0.0:
        logger.info("[时间感知] alpha_time=0，跳过时间融合: query_type=%s", query_type)
        return

    now = datetime.now()
    current_year, current_month = now.year, now.month

    raw_time_scores: List[float] = []
    valid_flags: List[bool] = []
    for chunk in chunks:
        year = chunk.get("_doc_time_year")
        month = chunk.get("_doc_time_month")
        if isinstance(year, int) and isinstance(month, int):
            raw_time_scores.append(
                gaussian_decay(
                    pub_year=year,
                    pub_month=max(1, min(12, month)),
                    current_year=current_year,
                    current_month=current_month,
                    sigma=sigma,
                )
            )
            valid_flags.append(True)
        else:
            raw_time_scores.append(0.0)
            valid_flags.append(False)

    max_time_score = max(raw_time_scores) if raw_time_scores else 0.0
    norm_time_scores = [
        (score / max_time_score) if max_time_score > 0 else 0.0
        for score in raw_time_scores
    ]

    for i, chunk in enumerate(chunks):
        semantic_score = float(chunk.get("_fused_score", chunk.get("_ppr_score", 0.0)))
        beta_doc = float(chunk.get("_doc_time_confidence", 0.0))
        beta_doc = max(0.0, min(1.0, beta_doc))

        if not valid_flags[i] or beta_doc <= 0.0:
            chunk["_time_score"] = 0.0
            chunk["_time_alpha"] = alpha_time
            chunk["_time_beta"] = beta_doc
            chunk["_fused_score"] = semantic_score
            continue

        time_score = norm_time_scores[i]
        gate = alpha_time * beta_doc
        final_score = (1.0 - gate) * semantic_score + gate * time_score

        chunk["_time_score"] = time_score
        chunk["_time_alpha"] = alpha_time
        chunk["_time_beta"] = beta_doc
        chunk["_fused_score"] = final_score

    top_preview = [
        round(float(c.get("_time_score", 0.0)), 3)
        for c in chunks[:3]
    ]
    beta_preview = [
        round(float(c.get("_time_beta", 0.0)), 3)
        for c in chunks[:3]
    ]
    time_hits = sum(1 for c in chunks if float(c.get("_time_beta", 0.0)) > 0.0 and float(c.get("_time_score", 0.0)) > 0.0)
    logger.info(
        "[时间感知] query_type=%s, alpha=%.2f, sigma=%.2f, 命中chunk=%s/%s, 前3条beta=%s, 前3条时间分=%s",
        query_type,
        alpha_time,
        float(sigma),
        time_hits,
        len(chunks),
        beta_preview,
        top_preview,
    )


async def rerank_filtered_chunks(
    query: str,
    filtered_data: PPRFilteredData,
    top_n: int = 30,
    ppr_weight: float = 0.4,
    rerank_weight: float = 0.6,
    # ── v2 新增：维度3 自适应融合权重 ────────────────────────────────────
    graph_density: float = 0.0,
    query_type=None,
    adaptive_fusion_weight: bool = True,
    rerank_results: Optional[List[Dict[str, Any]]] = None,
    hl_keywords: Optional[List[str]] = None,
    rescue_bonus_config: Optional[Any] = None,
) -> PPRFilteredData:
    """
    对 PPR 过滤后的文本块进行重排序，使用加权融合策略

    将 PPR 分数（逻辑连通性）与 Reranker 分数（语义相关性）进行加权融合，
    而非让 Reranker 完全覆盖 PPR 结果。

    v2 改进（维度3）：若提供 graph_density/query_type 且 adaptive_fusion_weight=True，
    则采用自适应权重替代固定 ppr_weight/rerank_weight。

    Args:
        query: 查询文本
        filtered_data: PPR 过滤后的数据
        top_n: 返回的文本块数量
        ppr_weight: PPR 分数权重（递备）
        rerank_weight: Reranker 分数权重（递备）
        graph_density: 平均度（由 PPRGraphFilter.graph_density 提供）
        query_type: 查询类型（QueryType 枚举）
        adaptive_fusion_weight: 是否开启自适应融合权重（消融实验开关）

    Returns:
        融合排序后的 PPRFilteredData
    """
    if not filtered_data.chunks:
        return filtered_data

    # 维度3：自适应融合权重
    if adaptive_fusion_weight and (graph_density > 0 or query_type is not None):
        ppr_weight, rerank_weight = compute_adaptive_fusion_weights(
            graph_density=graph_density,
            query_type=query_type,
            base_ppr_weight=ppr_weight,
        )
        logger.info(
            f"[自适应融合] d_avg={graph_density:.2f} → ppr_w={ppr_weight:.3f}, rerank_w={rerank_weight:.3f}"
        )

    # 如果没有关键词（如 minimal_vector 模式），降低 Rerank 权重，保护原始向量排序
    if not hl_keywords:
        rerank_weight = 0.2
        ppr_weight = 0.8
    
    # 打印融合参数，方便调试
    logger.info(f"[重排序] 执行加权融合: ppr_w={ppr_weight:.3f}, rerank_w={rerank_weight:.3f}, top_n={top_n}")

    chunk_contents = []
    chunk_map = {}
    for i, chunk in enumerate(filtered_data.chunks):
        content = chunk.get("content", "")
        if content:
            chunk_contents.append(content)
            chunk_map[len(chunk_contents) - 1] = i
    
    if not chunk_contents:
        return filtered_data
    
    if rerank_results is None:
        rerank_results = await call_rerank_api(query, chunk_contents, len(chunk_contents))
    
    rerank_scores = {}
    for result in rerank_results:
        original_idx = chunk_map.get(result.get("index", -1), -1)
        score = result.get("relevance_score", 0.0)
        if original_idx >= 0:
            rerank_scores[original_idx] = score
    
    if not rerank_scores:
        logger.info("[重排序] Rerank 未返回结果，使用原始/向量分数排序")
        for chunk in filtered_data.chunks:
            chunk["_fused_score"] = chunk.get("_ppr_score", chunk.get("_vec_score", 0.0))
        filtered_data.chunks.sort(key=lambda c: c.get("_fused_score", 0), reverse=True)
        filtered_data.chunks = filtered_data.chunks[:top_n]
        return filtered_data

    def _hits_hl_keyword(chunk: Dict[str, Any], keywords: Optional[List[str]]) -> bool:
        if not keywords:
            return False
        content_lower = chunk.get("content", "").lower()
        return any((kw or "").lower() in content_lower for kw in keywords if kw)
    
    # 使用安全归一化，避免所有分数一致时丢失信息
    ppr_scores_dict = {
        i: chunk.get("_ppr_score", chunk.get("_vec_score", 0.0))
        for i, chunk in enumerate(filtered_data.chunks)
    }
    ppr_normalized = _normalize_scores_safe(ppr_scores_dict, method="zscore")
    rerank_normalized = _normalize_scores_safe(rerank_scores, method="zscore")

    fused_chunks = []
    rescue_triggered = 0
    for i, chunk in enumerate(filtered_data.chunks):
        ppr_raw = chunk.get("_ppr_score", 0.0)
        rerank_raw = rerank_scores.get(i, 0.0)

        ppr_norm = ppr_normalized.get(i, 0.5)
        rerank_norm = rerank_normalized.get(i, 0.5)

        fused_score = ppr_weight * ppr_norm + rerank_weight * rerank_norm

        rescue_bonus = 0.0
        cfg = rescue_bonus_config
        if cfg and getattr(cfg, "enabled", False):
            rerank_threshold = float(getattr(cfg, "rerank_threshold", 0.80))
            ppr_ceiling = float(getattr(cfg, "ppr_ceiling", 0.05))
            base_bonus = float(getattr(cfg, "base_bonus", 0.05))
            slope = float(getattr(cfg, "slope", 0.75))
            hl_bonus = float(getattr(cfg, "hl_keyword_bonus", 0.05))
            max_bonus = float(getattr(cfg, "max_bonus", 0.20))
            if rerank_raw >= rerank_threshold and ppr_raw <= ppr_ceiling:
                rescue_bonus = (rerank_raw - rerank_threshold) * slope + base_bonus
                if _hits_hl_keyword(chunk, hl_keywords):
                    rescue_bonus += hl_bonus
                rescue_bonus = min(rescue_bonus, max_bonus)
                fused_score += rescue_bonus
                rescue_triggered += 1

        # 关键词加分：chunk 包含 HL 关键词时提升 fused_score，
        # 防止 MMR 多样性重排时把正确答案丢弃
        if hl_keywords:
            content_lower = chunk.get("content", "").lower()
            kw_hits = sum(1 for kw in hl_keywords if kw.lower() in content_lower)
            if kw_hits > 0:
                # 每命中一个关键词加 0.08，上限 0.25
                boost = min(0.25, kw_hits * 0.08)
                fused_score += boost

        chunk["_ppr_score_original"] = ppr_raw
        chunk["_rerank_score"] = rerank_raw
        chunk["_ppr_normalized"] = ppr_norm
        chunk["_rerank_normalized"] = rerank_norm
        chunk["_rescue_bonus"] = rescue_bonus
        chunk["_fused_score"] = fused_score

        fused_chunks.append(chunk)
    
    fused_chunks.sort(key=lambda c: c.get("_fused_score", 0), reverse=True)
    fused_chunks = fused_chunks[:top_n]
    
    logger.info(
        f"[重排序] 加权融合完成 | PPR权重={ppr_weight}, Rerank权重={rerank_weight} | "
        f"原始 {len(filtered_data.chunks)} 个 -> 融合后 {len(fused_chunks)} 个 | "
        f"Rescue触发={rescue_triggered}/{len(filtered_data.chunks)} (threshold={float(getattr(cfg, 'rerank_threshold', 0)) if cfg else 0}, ceiling={float(getattr(cfg, 'ppr_ceiling', 0)) if cfg else 0})"
    )
    
    filtered_data.chunks = fused_chunks
    
    return filtered_data
