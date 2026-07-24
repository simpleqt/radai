"""
内容过滤数据模型

从 backend/agents/nuclear_electronics_agent.py 迁移
"""

from dataclasses import dataclass, field
from enum import Enum
from typing import Any, Dict, List, Optional


class FilterMode(Enum):
    """过滤模式"""
    PPR_ONLY = "ppr_only"
    PPR_RERANK = "ppr_rerank"
    PPR_MMR = "ppr_mmr"
    FULL = "full"
    MMR_ONLY = "mmr_only"  # 跳过 PPR，仅 MMR 重排序
    MMR_TIME_AWARE = "mmr_time_aware"  # MMR + Time-Aware（跳过 PPR 与 Rerank）
    RERANK_MMR = "rerank_mmr"  # 跳过 PPR，直接 Rerank + MMR
    RERANK = "rerank"                # 跳过 PPR，仅 Rerank
    PPR_RERANK_MMR = "ppr_rerank_mmr" # PPR + Rerank + MMR (默认)
    RAW = "raw"


@dataclass
class TimeAwareConfig:
    """查询感知时间加权配置"""
    enabled: bool = True
    sigma: float = 5.0
    alpha_base: float = 0.3
    time_high_boost: float = 0.4
    time_low_penalty: float = 0.3
    undated_doc_beta: float = 0.7
    created_at_fallback_beta: float = 0.1


@dataclass
class RescueBonusConfig:
    enabled: bool = True
    rerank_threshold: float = 0.80
    ppr_ceiling: float = 0.15
    base_bonus: float = 0.05
    slope: float = 0.75
    hl_keyword_bonus: float = 0.05
    max_bonus: float = 0.20


@dataclass
class FilterConfig:
    """过滤配置"""
    ppr_damping: float = 0.85
    ppr_min_score: float = 0.001
    use_directed: bool = True
    reverse_edge_weight: float = 0.2
    reverse_edge_similarity_floor: float = 0.25
    reverse_edge_min_scale: float = 0.02
    reverse_edge_similarity_gamma: float = 2.0
    max_recall_tokens: int = 40000
    max_final_tokens: int = 17000
    mmr_lambda: float = 0.7
    mmr_top_n: int = 20
    rerank_ppr_weight: float = 0.4
    rerank_rerank_weight: float = 0.6
    # ── v2：自适应 PPR 改进（消融实验开关） ──────────────────────────────
    # 维度1：自适应阻尼系数（基于查询类型+图密度动态调整 α）
    adaptive_ppr_damping: bool = True
    # 维度2：语义反向边权重（基于嵌入余弦相似度动态计算反向边权重）
    adaptive_reverse_edge: bool = True
    # 维度3：PPR 与 Rerank 自适应融合权重（基于图密度+查询类型动态调整）
    adaptive_fusion_weight: bool = True
    
    # ── v4.1：PPR 机制改进（预留字段；Path-D 图扩展已不再按 relation_type 白名单剪边） ──
    ppr_dynamic_edge_pruning: bool = True

    # 查询感知时间加权（在 Rerank 融合后、MMR 前生效）
    time_aware: TimeAwareConfig = field(default_factory=TimeAwareConfig)
    rescue_bonus: RescueBonusConfig = field(default_factory=RescueBonusConfig)


@dataclass
class RawRetrievalData:
    """原始检索数据"""
    entities: List[Dict[str, Any]] = field(default_factory=list)
    relationships: List[Dict[str, Any]] = field(default_factory=list)
    chunks: List[Dict[str, Any]] = field(default_factory=list)
    query_embedding: Optional[Any] = None  # v4.1 新增：用于语义随机游走


@dataclass
class FilteredData:
    """过滤后的数据"""
    entities: List[Dict[str, Any]] = field(default_factory=list)
    relationships: List[Dict[str, Any]] = field(default_factory=list)
    chunks: List[Dict[str, Any]] = field(default_factory=list)
    entity_ppr_scores: Dict[str, float] = field(default_factory=dict)
    knowledge_paths: List[Dict[str, Any]] = field(default_factory=list)
    """结构化的知识路径（证据链条），每条路径包含 nodes、relations、score"""


@dataclass
class FilterResult:
    """过滤结果"""
    filtered_data: FilteredData
    prompt: str = ""
    filter_time_ms: float = 0.0
    stats: Dict[str, Any] = field(default_factory=dict)
