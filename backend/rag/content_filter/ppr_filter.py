"""
PPR (Personalized PageRank) 图过滤器

从 backend/agents/nuclear_electronics_agent.py 迁移

改进历史：
  v2 - 三维度自适应改进：
    维度1：自适应阻尼系数（基于查询类型+图密度动态调整 α）
    维度2：语义反向边权重（基于嵌入余弦相似度动态计算反向边权重）
    维度3：图密度计算（供 reranker 使用自适应融合权重）
"""

import asyncio
import os
import re
from collections import defaultdict
from concurrent.futures import ThreadPoolExecutor
from dataclasses import dataclass, field
from typing import Any, Callable, Dict, List, Optional, Tuple

import networkx as nx
import numpy as np

from backend.config.loggers import get_logger
from backend.core.utils.math import cosine_similarity
from backend.rag.content_filter.models import RawRetrievalData

logger = get_logger(__name__)


def _merge_ppr_seed_weights(
    entities: List[Dict[str, Any]],
    keyword_seed_weights: Dict[str, float],
    blend_ratio: float = 0.5,
) -> Dict[str, float]:
    """
    双源 PPR 种子权重合并

    将关键词阶段的 seed_weights 与 DirectRetriever 写入实体的 _fused_score 混合，
    产出更精准的 personalization 向量。

    blend_ratio: 检索阶段权重占比（0.0=仅关键词, 1.0=仅检索分）
    """
    # 检索阶段：从实体的 _fused_score 字段提取
    ret_scores: Dict[str, float] = {}
    for e in entities:
        name = e.get("entity_name", "")
        fs = e.get("_fused_score", 0.0)
        if name and fs > 0:
            ret_scores[name] = float(fs)

    # 如果实体中没有 _fused_score（来自旧 LightRAG 路径），直接返回原始 keyword 权重
    if not ret_scores:
        return keyword_seed_weights

    kw_total = sum(keyword_seed_weights.values()) or 1.0
    kw_norm = {k: v / kw_total for k, v in keyword_seed_weights.items()}

    ret_total = sum(ret_scores.values()) or 1.0
    ret_norm = {k: v / ret_total for k, v in ret_scores.items()}

    all_names = set(kw_norm) | set(ret_norm)
    merged: Dict[str, float] = {}
    for name in all_names:
        kw_w  = kw_norm.get(name, 0.0)
        ret_w = ret_norm.get(name, 0.0)
        merged[name] = (1.0 - blend_ratio) * kw_w + blend_ratio * ret_w

    total = sum(merged.values()) or 1.0
    return {k: v / total for k, v in merged.items() if v > 0}


class PPRExecutorManager:
    """
    PPR 线程池管理器

    特性：
    - 动态调整 worker 数量（通过环境变量配置）
    - 任务队列限制（防止内存溢出）
    - 任务超时控制
    - 优雅关闭
    """

    _instance = None
    _lock = asyncio.Lock()

    def __init__(self):
        self.max_workers = int(os.getenv("PPR_MAX_WORKERS", "8"))
        self.max_queue_size = int(os.getenv("PPR_MAX_QUEUE_SIZE", "100"))
        self.task_timeout = float(os.getenv("PPR_TASK_TIMEOUT", "30.0"))

        self._executor = ThreadPoolExecutor(
            max_workers=self.max_workers,
            thread_name_prefix="ppr_worker"
        )
        self._active_tasks = 0
        self._semaphore = asyncio.Semaphore(self.max_queue_size)

        logger.info(
            f"[PPR线程池] 初始化完成 | max_workers={self.max_workers}, "
            f"max_queue_size={self.max_queue_size}, task_timeout={self.task_timeout}s"
        )

    @classmethod
    async def get_instance(cls) -> 'PPRExecutorManager':
        if cls._instance is None:
            async with cls._lock:
                if cls._instance is None:
                    cls._instance = cls()
        return cls._instance

    async def submit(
        self,
        func: Callable,
        *args,
        timeout: float = None,
        **kwargs
    ) -> Any:
        timeout = timeout or self.task_timeout

        if self._semaphore.locked():
            logger.warning(
                f"[PPR线程池] 队列已满 ({self.max_queue_size})，等待可用槽位..."
            )

        async with self._semaphore:
            self._active_tasks += 1
            try:
                loop = asyncio.get_running_loop()
                result = await asyncio.wait_for(
                    loop.run_in_executor(self._executor, func, *args, **kwargs),
                    timeout=timeout
                )
                return result
            except asyncio.TimeoutError:
                logger.error(f"[PPR线程池] 任务超时 ({timeout}s): {func.__name__}")
                raise
            except Exception as e:
                logger.error(f"[PPR线程池] 任务执行失败: {e}")
                raise
            finally:
                self._active_tasks -= 1

    def get_stats(self) -> dict:
        return {
            "max_workers": self.max_workers,
            "active_tasks": self._active_tasks,
            "queue_size": self.max_queue_size - self._semaphore._value,
            "available_slots": self._semaphore._value,
        }

    def shutdown(self, wait: bool = True):
        logger.info(f"[PPR线程池] 关闭中，当前活跃任务: {self._active_tasks}")
        self._executor.shutdown(wait=wait)


# _PPR_EXECUTOR = ThreadPoolExecutor(max_workers=4, thread_name_prefix="ppr_worker")


@dataclass
class PPRFilteredData:
    """PPR 过滤后的数据"""
    entities: List[Dict[str, Any]] = field(default_factory=list)
    relationships: List[Dict[str, Any]] = field(default_factory=list)
    chunks: List[Dict[str, Any]] = field(default_factory=list)
    entity_ppr_scores: Dict[str, float] = field(default_factory=dict)
    knowledge_paths: List[Dict[str, Any]] = field(default_factory=list)
    """结构化的知识路径（证据链条），每条路径包含 nodes、relations、score"""


class PPRGraphFilter:
    """
    生产级PPR图过滤 - 基于NetworkX原生C实现

    核心优化：使用 nx.pagerank() 进行高效 PPR 计算

    三阶段检索流水线：
    1. 放宽初筛，获取原始全量上下文
    2. 计算局部子图的 PPR 权重进行图过滤
    3. 原子化动态组装，控制 Token 数量

    v2 改进（三维度自适应）：
    - 维度1：自适应阻尼系数（adaptive_ppr_damping=True 时生效）
    - 维度2：语义反向边权重（embedding_func 不为 None 时生效）
    - 图密度计算（graph_density 属性，供融合权重使用）
    """
    
    # 静态类属性：跨实例共享的实体嵌入缓存
    _GLOBAL_ENTITY_EMB_CACHE: Dict[str, np.ndarray] = {}

    def __init__(
        self,
        ppr_damping: float = 0.85,
        ppr_min_score: float = 0.001,
        max_recall_tokens: int = 40000,
        max_final_tokens: int = 17000,
        use_directed: bool = True,
        reverse_edge_weight: float = 0.2,
        reverse_edge_similarity_floor: float = 0.25,
        reverse_edge_min_scale: float = 0.02,
        reverse_edge_similarity_gamma: float = 2.0,
        # ── v2 新增：自适应改进参数 ────────────────────────────────────
        embedding_func: Optional[Callable] = None,
        adaptive_ppr_damping: bool = True,
        adaptive_reverse_edge: bool = True,
    ):
        self.ppr_damping = ppr_damping
        self.ppr_min_score = ppr_min_score
        self.max_recall_tokens = max_recall_tokens
        self.max_final_tokens = max_final_tokens
        self.use_directed = use_directed
        self.reverse_edge_weight = reverse_edge_weight
        self.reverse_edge_similarity_floor = reverse_edge_similarity_floor
        self.reverse_edge_min_scale = reverse_edge_min_scale
        self.reverse_edge_similarity_gamma = reverse_edge_similarity_gamma
        # v2 新增
        self.embedding_func = embedding_func
        self.adaptive_ppr_damping = adaptive_ppr_damping
        self.adaptive_reverse_edge = adaptive_reverse_edge

        # 内部状态
        self._tokenizer = None
        self._executor_manager = None
        # 使用实例级缓存和类级静态缓存结合，提高跨请求命中率
        self._entity_embedding_cache: Dict[str, np.ndarray] = {}  
        self.graph_density: float = 0.0  
    
    def _get_tokenizer(self):
        if self._tokenizer is None:
            try:
                from lightrag.utils import TiktokenTokenizer
                self._tokenizer = TiktokenTokenizer()
            except Exception:
                self._tokenizer = None
        return self._tokenizer
    
    def _count_tokens(self, text: str) -> int:
        """精确计算 token 数量

        优先使用 tiktoken，如果不可用则执行中英文混合估算。"""
        if not text:
            return 0

        tokenizer = self._get_tokenizer()
        if tokenizer:
            try:
                return len(tokenizer.encode(text))
            except Exception as e:
                logger.debug(f"tiktoken 编码失败: {e}")

        # 降级估算：中文约1.5字符/token，其他字符约4字符/token
        chinese_chars = sum(1 for c in text if '\u4e00' <= c <= '\u9fff')
        non_chinese_chars = len(text) - chinese_chars
        estimated_tokens = int(chinese_chars / 1.5) + (non_chinese_chars // 4)

        # 添加 10% 安全余量
        return max(1, int(estimated_tokens * 1.1))
    
    # ─────────────────────────────────────────────────────────────────────
    # 维度1：自适应阻尼系数
    # ─────────────────────────────────────────────────────────────────────

    @staticmethod
    def compute_adaptive_damping(
        base_damping: float = 0.85,
        query_type=None,
        graph_density: float = 0.0,
    ) -> float:
        """
        计算自适应阻尼系数

        公式：
            α_final = clip(base + Δ_query + Δ_density, 0.70, 0.95)

        Δ_query：
            PARAMETER/EQUIPMENT → +0.05  (局部型：深度探索局部子图)
            CONCEPT/COMPARISON  → -0.08  (全局型：广泛搜索、避免陷入局部)
            GENERAL/其他        →  0.00

        Δ_density (d_avg = 2|E|/|V|)：
            d_avg > 10 → -0.04  (稠密图：防止游走陷入 Hub 节点)
            d_avg < 3  → +0.03  (稀疏图：扩大探索范围)
            否则       →  0.00
        """
        # 延迟导入，避免循环引用
        try:
            from backend.rag.keywords.models import QueryType
        except ImportError:
            query_type = None

        delta_query = 0.0
        if query_type is not None:
            try:
                from backend.rag.keywords.models import QueryType
                if query_type in (QueryType.PARAMETER, QueryType.EQUIPMENT):
                    delta_query = 0.05
                elif query_type in (QueryType.CONCEPT, QueryType.COMPARISON):
                    delta_query = -0.08
            except Exception:
                pass

        delta_density = 0.0
        if graph_density > 10:
            delta_density = -0.04
        elif 0 < graph_density < 3:
            delta_density = 0.03

        alpha = float(np.clip(base_damping + delta_query + delta_density, 0.70, 0.95))
        logger.debug(
            f"[自适应阻尼] base={base_damping}, Δ_query={delta_query:+.2f}, "
            f"Δ_density={delta_density:+.2f}, d_avg={graph_density:.2f} → α={alpha:.4f}"
        )
        return alpha

    @staticmethod
    def compute_graph_density(G: nx.DiGraph) -> float:
        """
        计算图的平均度（无向意义下）：d_avg = 2|E| / |V|

        有向图中每条边被计数一次，故 d_avg = |E| / |V|（单向平均度）。
        这里按定义取 2|E|/|V| 以与文档中的定义保持一致。
        """
        n = G.number_of_nodes()
        if n == 0:
            return 0.0
        return 2.0 * G.number_of_edges() / n

    # ─────────────────────────────────────────────────────────────────────
    # 维度2：语义反向边权重
    # ─────────────────────────────────────────────────────────────────────

    def _semantic_reverse_scale(
        self,
        src_emb: Optional[np.ndarray],
        tgt_emb: Optional[np.ndarray],
    ) -> float:
        """计算语义反向边的缩放系数，返回 [min_scale, 1]。"""
        if src_emb is None or tgt_emb is None:
            return 1.0

        cos_sim = cosine_similarity(src_emb, tgt_emb)
        mapped = float(np.clip((cos_sim + 1.0) / 2.0, 0.0, 1.0))
        if mapped <= self.reverse_edge_similarity_floor:
            return self.reverse_edge_min_scale

        normalized = (mapped - self.reverse_edge_similarity_floor) / max(
            1e-6,
            1.0 - self.reverse_edge_similarity_floor,
        )
        return self.reverse_edge_min_scale + (
            (1.0 - self.reverse_edge_min_scale)
            * (normalized ** self.reverse_edge_similarity_gamma)
        )

    # ─────────────────────────────────────────────────────────────────────
    # 图构建
    # ─────────────────────────────────────────────────────────────────────

    def build_subgraph_directed(
        self,
        entities: List[Dict[str, Any]],
        relationships: List[Dict[str, Any]],
        entity_embeddings: Optional[Dict[str, np.ndarray]] = None,
    ) -> Tuple[nx.DiGraph, Dict[str, int]]:
        """
        构建有向图 - 使用 NetworkX 原生操作

        改进（维度2）：若提供了 entity_embeddings，则反向边权重由嵌入余弦相似度动态计算，
        否则回退到固定的 self.reverse_edge_weight（向后兼容）。

        正向权重 = rel.weight (通常为 1.0)
        反向权重 = base_reverse_weight * f((cos_sim + 1) / 2)
        """
        G = nx.DiGraph()
        entity_to_idx = {}

        # 建立实体名称 → 描述 映射（用于嵌入文本构建）
        entity_desc_map: Dict[str, str] = {}
        for i, entity in enumerate(entities):
            entity_name = entity.get("entity_name", "")
            if entity_name and entity_name not in entity_to_idx:
                entity_to_idx[entity_name] = i
                G.add_node(i, **entity)
                entity_desc_map[entity_name] = entity.get("description", "")

        use_semantic = (
            self.adaptive_reverse_edge
            and entity_embeddings is not None
            and len(entity_embeddings) > 0
        )

        edges = []
        semantic_count = 0
        semantic_min_hits = 0
        semantic_scale_total = 0.0
        for rel in relationships:
            src = rel.get("src_id", "")
            tgt = rel.get("tgt_id", "")
            weight = float(rel.get("weight", 1.0))

            if src in entity_to_idx and tgt in entity_to_idx:
                src_idx = entity_to_idx[src]
                tgt_idx = entity_to_idx[tgt]
                edges.append((src_idx, tgt_idx, weight))

                if use_semantic:
                    src_emb = entity_embeddings.get(src)
                    tgt_emb = entity_embeddings.get(tgt)
                    rev_scale = self._semantic_reverse_scale(src_emb, tgt_emb)
                    rev_w = self.reverse_edge_weight * rev_scale * weight
                    semantic_count += 1
                    semantic_scale_total += rev_scale
                    if rev_scale <= self.reverse_edge_min_scale + 1e-9:
                        semantic_min_hits += 1
                else:
                    rev_w = weight * self.reverse_edge_weight
                edges.append((tgt_idx, src_idx, rev_w))

        if edges:
            G.add_weighted_edges_from(edges)

        logger.info(
            f"[图构建] 有向图 | 节点数: {G.number_of_nodes()}, 边数: {G.number_of_edges()} "
            f"| 语义反向边: {semantic_count}, 固定反向边: {len(edges)//2 - semantic_count}"
        )
        if G.number_of_edges() > 0:
            reverse_edge_ratio = semantic_count / max(1, len(edges) // 2)
            avg_semantic_scale = semantic_scale_total / max(1, semantic_count)
            min_hit_ratio = semantic_min_hits / max(1, semantic_count)
            logger.info(
                f"[语义反向边] 启用状态={use_semantic}, 比例={reverse_edge_ratio:.2%}, "
                f"base_reverse_weight={self.reverse_edge_weight}, floor={self.reverse_edge_similarity_floor}, "
                f"min_scale={self.reverse_edge_min_scale}, gamma={self.reverse_edge_similarity_gamma}, "
                f"avg_scale={avg_semantic_scale:.4f}, min_hit_ratio={min_hit_ratio:.2%}"
            )
            if use_semantic and min_hit_ratio >= 0.5:
                logger.warning(
                    f"[语义反向边] 超过一半语义回边落到最低档，可能存在噪声: min_hit_ratio={min_hit_ratio:.2%}"
                )
        return G, entity_to_idx
    
    def build_subgraph_undirected(
        self,
        entities: List[Dict[str, Any]],
        relationships: List[Dict[str, Any]],
    ) -> Tuple[nx.Graph, Dict[str, int]]:
        """
        构建无向图 - 使用NetworkX原生操作（已弃用，保留向后兼容）
        
        警告：无向图可能导致"主题漂移"问题，建议使用有向图
        """
        G = nx.Graph()
        entity_to_idx = {}
        
        for i, entity in enumerate(entities):
            entity_name = entity.get("entity_name", "")
            if entity_name and entity_name not in entity_to_idx:
                entity_to_idx[entity_name] = i
                G.add_node(i, **entity)
        
        edges = []
        for rel in relationships:
            src = rel.get("src_id", "")
            tgt = rel.get("tgt_id", "")
            weight = float(rel.get("weight", 1.0))
            
            if src in entity_to_idx and tgt in entity_to_idx:
                src_idx = entity_to_idx[src]
                tgt_idx = entity_to_idx[tgt]
                edges.append((src_idx, tgt_idx, weight))
        
        if edges:
            G.add_weighted_edges_from(edges)
        
        logger.info(f"[图构建] 无向图 | 节点数: {G.number_of_nodes()}, 边数: {G.number_of_edges()}")
        return G, entity_to_idx
    
    def compute_ppr_native(
        self,
        G: nx.DiGraph,
        seed_entities: List[str],
        entity_to_idx: Dict[str, int],
        alpha: Optional[float] = None,
        seed_weights: Optional[Dict[str, float]] = None,
    ) -> Dict[str, float]:
        """
        使用 NetworkX 计算 PPR

        维度1 改进：alpha 参数可由外部传入自适应值；
        若不传则使用实例的 ppr_damping（向后兼容）。
        """
        if G.number_of_nodes() == 0:
            return {}

        effective_alpha = alpha if alpha is not None else self.ppr_damping

        weighted_indices: Dict[int, float] = {}
        if seed_weights:
            for entity_name, raw_weight in seed_weights.items():
                idx = entity_to_idx.get(entity_name)
                if idx is None:
                    continue
                weight = float(raw_weight)
                if weight > 0:
                    weighted_indices[idx] = weighted_indices.get(idx, 0.0) + weight

            if weighted_indices:
                total_w = float(sum(weighted_indices.values()))
                if total_w > 0:
                    personalization = {
                        idx: float(weight / total_w)
                        for idx, weight in weighted_indices.items()
                    }
                else:
                    personalization = {i: 1.0 / G.number_of_nodes() for i in range(G.number_of_nodes())}
            else:
                personalization = {i: 1.0 / G.number_of_nodes() for i in range(G.number_of_nodes())}
        else:
            seed_indices = [entity_to_idx[e] for e in seed_entities if e in entity_to_idx]
            if not seed_indices:
                personalization = {i: 1.0 / G.number_of_nodes() for i in range(G.number_of_nodes())}
            else:
                w = 1.0 / len(seed_indices)
                personalization = {idx: w for idx in seed_indices}

        # ── 执行 PageRank 迭代 ──────────────────────────────────────
        node_count = G.number_of_nodes()
        target_l1 = 1e-8
        adaptive_tol = max(1e-12, target_l1 / max(1, node_count))
        
        ppr_scores = None
        try:
            ppr_scores = nx.pagerank(
                G, alpha=effective_alpha, personalization=personalization,
                weight="weight", max_iter=1000, tol=adaptive_tol
            )
        except Exception as e:
            logger.warning(f"[PPR计算] 迭代未收敛: {e}，回退到均匀分布")
            ppr_scores = {i: 1.0 / node_count for i in range(node_count)}

        idx_to_entity = {v: k for k, v in entity_to_idx.items()}
        entity_scores = {}
        for idx, score in ppr_scores.items():
            if idx in idx_to_entity:
                entity_scores[idx_to_entity[idx]] = score

        return entity_scores
    
    def _extract_seeds(self, entities: List[Dict], hl_keywords: List[str]) -> List[str]:
        """提取种子实体"""
        seeds = []
        keyword_set = set(k.lower() for k in hl_keywords)
        
        for entity in entities:
            entity_name = entity.get("entity_name", "").lower()
            description = entity.get("description", "").lower()
            
            for kw in keyword_set:
                if kw in entity_name or kw in description:
                    seeds.append(entity.get("entity_name", ""))
                    break
        
        if not seeds and entities:
            seeds = [e.get("entity_name", "") for e in entities[:5]]
        
        logger.info(f"[种子提取] 共{len(seeds)}个种子: {seeds[:5]}")
        return seeds
    
    async def _batch_embed_entities(
        self,
        entities: List[Dict[str, Any]],
    ) -> Dict[str, np.ndarray]:
        """
        批量预计算实体嵌入向量（维度2：语义反向边权重）。
        带共享缓存优化。
        """
        if self.embedding_func is None or not self.adaptive_reverse_edge:
            return {}

        results = {}
        missing_entities = []
        texts = []
        
        for entity in entities:
            name = entity.get("entity_name", "")
            if not name:
                continue
                
            # 1. 优先从类级全局缓存获取
            if name in self._GLOBAL_ENTITY_EMB_CACHE:
                emb = self._GLOBAL_ENTITY_EMB_CACHE[name]
                self._entity_embedding_cache[name] = emb # 同步到实例缓存
                results[name] = emb
                continue
                
            # 2. 其次从实例缓存获取
            if name in self._entity_embedding_cache:
                results[name] = self._entity_embedding_cache[name]
                continue
                
            # 3. 都没有则准备批量计算
            desc = entity.get("description", "")[:80]
            text = f"{name}: {desc}" if desc else name
            missing_entities.append(name)
            texts.append(text)

        if texts:
            try:
                # 分批调用（每批 32），避免单次 API 调用 100+ 文本阻塞事件循环
                BATCH = 32
                all_embeddings: list = []
                for b_start in range(0, len(texts), BATCH):
                    batch_texts = texts[b_start: b_start + BATCH]
                    result = self.embedding_func(batch_texts)
                    if asyncio.iscoroutine(result):
                        result = await result
                    if isinstance(result, np.ndarray):
                        all_embeddings.extend(result.tolist())
                    else:
                        all_embeddings.extend(list(result))
                    # 主动让出事件循环，允许其他协程（如另一个KB的任务）插入执行
                    await asyncio.sleep(0)

                embeddings = np.array(all_embeddings, dtype=np.float32)
                
                # 兼容不同维度的返回
                if embeddings.ndim == 1 and len(missing_entities) == 1:
                    embeddings = embeddings.reshape(1, -1)
                
                for i, name in enumerate(missing_entities):
                    emb = embeddings[i].astype(np.float32)
                    # 写入双级缓存
                    self._GLOBAL_ENTITY_EMB_CACHE[name] = emb
                    self._entity_embedding_cache[name] = emb
                    results[name] = emb
                    
            except Exception as e:
                logger.warning(f"[语义反向边] 批量嵌入失败: {e}")
                
        return results

    async def filter_by_ppr(
        self,
        raw_data: RawRetrievalData,
        hl_keywords: List[str],
        query_type=None,
        seed_weights: Optional[Dict[str, float]] = None,
    ) -> PPRFilteredData:
        """
        生产级安全的 PPR 过滤 - 所有 CPU 密集型操作都在线程池执行

        v2 改进：
        - 调用前批量预计算实体嵌入（维度2，异步，不阻塞图构建）
        - 构建图时传入嵌入字典（语义反向边权重）
        - 计算图密度并存入 self.graph_density（供融合权重使用）
        - PPR 使用自适应 α（维度1）
        """
        if not raw_data.entities:
            return PPRFilteredData(chunks=raw_data.chunks)

        if self._executor_manager is None:
            self._executor_manager = await PPRExecutorManager.get_instance()

        # 维度2：并行预计算实体嵌入
        has_relationships = bool(raw_data.relationships)
        entity_embeddings = {}
        if has_relationships and self.adaptive_reverse_edge:
            MAX_EMBED_ENTITIES = 60
            entities_to_embed = raw_data.entities[:MAX_EMBED_ENTITIES]
            entity_embeddings = await self._batch_embed_entities(entities_to_embed)
            if entity_embeddings:
                logger.info(f"[PPR嵌入] 预计算嵌入: {len(entity_embeddings)}/{len(raw_data.entities)} 个实体")

        if self.use_directed:
            G, entity_to_idx = await self._executor_manager.submit(
                self.build_subgraph_directed,
                raw_data.entities,
                raw_data.relationships,
                entity_embeddings,
                timeout=10.0,
            )
        else:
            G, entity_to_idx = await self._executor_manager.submit(
                self.build_subgraph_undirected,
                raw_data.entities,
                raw_data.relationships,
                timeout=10.0,
            )

        if G.number_of_nodes() == 0:
            return PPRFilteredData(chunks=raw_data.chunks)

        # 计算图密度（供维度1和维度3使用）
        self.graph_density = self.compute_graph_density(G)
        logger.info(f"[图密度] d_avg={self.graph_density:.2f}")

        # 维度1：自适应阻尼系数
        if self.adaptive_ppr_damping:
            effective_alpha = self.compute_adaptive_damping(
                base_damping=self.ppr_damping,
                query_type=query_type,
                graph_density=self.graph_density,
            )
        else:
            effective_alpha = self.ppr_damping

        seed_entities = self._extract_seeds(raw_data.entities, hl_keywords)

        # 双源种子权重增强
        effective_seed_weights = _merge_ppr_seed_weights(
            raw_data.entities,
            seed_weights or {},
            blend_ratio=0.5,
        )

        entity_ppr_scores = await self._executor_manager.submit(
            self.compute_ppr_native,
            G,
            seed_entities,
            entity_to_idx,
            effective_alpha,
            effective_seed_weights or seed_weights,
            timeout=20.0,
        )
        
        filtered_entities = []
        for entity in raw_data.entities:
            entity_name = entity.get("entity_name", "")
            score = entity_ppr_scores.get(entity_name, 0)
            entity["_ppr_score"] = score
            filtered_entities.append(entity)
        
        # 保留 PPR 分数最高的前 N 个实体，而非用绝对阈值过滤
        # 避免图稀疏时过度裁剪导致召回不足
        max_entities = max(20, len(raw_data.entities) // 3)
        if len(filtered_entities) > max_entities:
            filtered_entities.sort(key=lambda e: -e.get("_ppr_score", 0))
            filtered_entities = filtered_entities[:max_entities]
        
        valid_entity_names = {e.get("entity_name") for e in filtered_entities}
        filtered_relationships = []
        for rel in raw_data.relationships:
            src = rel.get("src_id", "")
            tgt = rel.get("tgt_id", "")
            if src in valid_entity_names or tgt in valid_entity_names:
                src_score = entity_ppr_scores.get(src, 0)
                tgt_score = entity_ppr_scores.get(tgt, 0)
                rel["_ppr_score"] = max(src_score, tgt_score)
                filtered_relationships.append(rel)

        min_nodes = 5
        min_edges = 3
        if len(filtered_entities) < min_nodes or len(filtered_relationships) < min_edges:
            logger.warning(
                "[PPR兜底] 子图过小，触发1-hop回退: entities=%s, relationships=%s",
                len(filtered_entities),
                len(filtered_relationships),
            )

            fallback_node_indices = set()
            for seed in seed_entities:
                if seed not in entity_to_idx:
                    continue
                seed_idx = entity_to_idx[seed]
                fallback_node_indices.add(seed_idx)
                if seed_idx in G:
                    try:
                        fallback_node_indices.update(G.successors(seed_idx))
                    except Exception:
                        pass
                    try:
                        fallback_node_indices.update(G.predecessors(seed_idx))
                    except Exception:
                        pass
                    try:
                        fallback_node_indices.update(G.neighbors(seed_idx))
                    except Exception:
                        pass

            if fallback_node_indices:
                fallback_entity_names = {
                    G.nodes[idx].get("entity_name")
                    for idx in fallback_node_indices
                    if idx in G and G.nodes[idx].get("entity_name")
                }
                if fallback_entity_names:
                    filtered_entities = [
                        entity for entity in raw_data.entities
                        if entity.get("entity_name") in fallback_entity_names
                    ]
                    valid_entity_names = {e.get("entity_name") for e in filtered_entities}
                    filtered_relationships = []
                    for rel in raw_data.relationships:
                        src = rel.get("src_id", "")
                        tgt = rel.get("tgt_id", "")
                        if src in valid_entity_names and tgt in valid_entity_names:
                            src_score = entity_ppr_scores.get(src, 0)
                            tgt_score = entity_ppr_scores.get(tgt, 0)
                            rel["_ppr_score"] = max(src_score, tgt_score)
                            filtered_relationships.append(rel)
                    logger.info(
                        "[PPR兜底] 回退后规模: entities=%s, relationships=%s",
                        len(filtered_entities),
                        len(filtered_relationships),
                    )
        
        chunk_scores = defaultdict(list)
        for entity in filtered_entities:
            source_id = entity.get("source_id", "")
            if source_id:
                # source_id 使用 <SEP> 分隔多个 chunk ID
                if "<SEP>" in source_id:
                    chunk_ids = [s.strip() for s in source_id.split("<SEP>") if s.strip()]
                else:
                    chunk_ids = [s.strip() for s in source_id.split(",") if s.strip()]
                for chunk_id in chunk_ids:
                    chunk_scores[chunk_id].append(entity.get("_ppr_score", 0))
        
        for chunk in raw_data.chunks:
            chunk_id = chunk.get("chunk_id", "")
            scores = chunk_scores.get(chunk_id, [])
            chunk["_ppr_score"] = max(scores) if scores else 0
        
        filtered_chunks = sorted(raw_data.chunks, key=lambda c: -c.get("_ppr_score", 0))
        
        logger.info(f"[PPR过滤] 实体: {len(raw_data.entities)}->{len(filtered_entities)}, "
                   f"关系: {len(raw_data.relationships)}->{len(filtered_relationships)}, "
                   f"chunk: {len(raw_data.chunks)}->{len(filtered_chunks)}")
        
        # 提取核心知识路径（证据链条）并序列化为结构化数据
        ppr_filtered = PPRFilteredData(
            entities=filtered_entities,
            relationships=filtered_relationships,
            chunks=filtered_chunks,
            entity_ppr_scores=entity_ppr_scores,
        )
        ppr_filtered.knowledge_paths = self._serialize_knowledge_paths(ppr_filtered)

        return ppr_filtered
    
    def atomic_assembly(
        self,
        filtered_data: PPRFilteredData,
        query: Optional[str] = None,
        max_tokens: Optional[int] = None,
    ) -> str:
        """
        原子化动态组装 Prompt (去重优化版)
        
        策略：
        1. 优先提取核心知识路径。
        2. 仅针对路径中涉及的实体和关系提供详细描述（Details）。
        3. 对于不在路径中但评分较高的实体，提供精简背景（Background）。
        4. 显著减少内容重复，提升 Token 利用率。
        """
        max_tokens = max_tokens or self.max_final_tokens
        prompt_parts = []
        current_tokens = 0
        
        # 1. 提取核心路径
        paths = self._extract_core_paths(
            filtered_data,
            query=query,
            top_n=5,
            max_hops=2,
        )
        
        # 2. 收集路径涉及的元素（用于去重）
        path_entities_names = set()
        path_relationships_keys = set()
        for nodes, rels, _ in paths:
            path_entities_names.update(nodes)
            for r in rels:
                path_relationships_keys.add(f"{r.get('src_id')}->{r.get('tgt_id')}")
        
        # 3. 构建知识路径区 (仅展示结构)
        path_section = self._build_path_section(paths)
        if path_section:
            path_tokens = self._count_tokens(path_section)
            prompt_parts.append(path_section)
            current_tokens += path_tokens

        # 4. 构建知识详情区 (包含路径内实体的全量描述)
        # 获取路径内实体的完整信息
        entities_in_path = [
            e for e in filtered_data.entities 
            if e.get("entity_name") in path_entities_names
        ]
        relationships_in_path = [
            r for r in filtered_data.relationships
            if f"{r.get('src_id')}->{r.get('tgt_id')}" in path_relationships_keys
        ]
        
        details_max_tokens = int(max_tokens * 0.20)
        details_section = self._build_graph_details_section(
            entities_in_path, relationships_in_path
        )
        if details_section:
            details_tokens = self._count_tokens(details_section)
            if details_tokens > details_max_tokens:
                details_section = details_section[:details_max_tokens * 4]
            details_tokens = self._count_tokens(details_section)
            prompt_parts.append(details_section)
            current_tokens += details_tokens

        # 5. 构建背景补充区 (不在路径中的高分实体)
        background_entities = [
            e for e in filtered_data.entities[:20]
            if e.get("entity_name") not in path_entities_names
        ]
        if background_entities:
            background_max_tokens = int(max_tokens * 0.10)
            # 使用简化的解析器展示背景
            bg_lines = ["\n## 相关背景实体\n"]
            for e in background_entities[:10]:
                name = e.get("entity_name", "")
                desc = e.get("description", "")[:100]
                bg_lines.append(f"- {name}: {desc}")
            
            bg_section = "\n".join(bg_lines)
            bg_tokens = self._count_tokens(bg_section)
            if bg_tokens < background_max_tokens:
              prompt_parts.append(bg_section)
              current_tokens += bg_tokens

        # 6. 构建文本块区 (占用主要 Token)
        chunk_max_tokens = max_tokens - current_tokens
        chunk_section = self._build_chunk_section_atomic(
            filtered_data.chunks,
            chunk_max_tokens,
        )
        if chunk_section:
            prompt_parts.append(chunk_section)
        
        return "\n\n".join(prompt_parts) if prompt_parts else ""

    
    def _extract_core_paths(
        self,
        filtered_data: PPRFilteredData,
        query: Optional[str],
        top_n: int = 5,
        max_hops: int = 2,
    ) -> List[Tuple[List[str], List[Dict[str, Any]], float]]:
        """围绕 query 在过滤后的子图中抽取核心路径。"""
        if not filtered_data.entities or not filtered_data.relationships:
            return []

        entity_map: Dict[str, Dict[str, Any]] = {}
        for entity in filtered_data.entities:
            name = entity.get("entity_name", "")
            if name:
                entity_map[name] = entity

        if not entity_map:
            return []

        adjacency: Dict[str, List[Tuple[str, Dict[str, Any]]]] = defaultdict(list)
        for rel in filtered_data.relationships:
            src = rel.get("src_id", "")
            tgt = rel.get("tgt_id", "")
            if src in entity_map and tgt in entity_map:
                adjacency[src].append((tgt, rel))

        if not adjacency:
            return []

        query_tokens: List[str] = []
        if query:
            query_tokens = re.findall(r"[a-zA-Z0-9_\u4e00-\u9fff]+", query.lower())

        starts: List[str] = []
        if query_tokens:
            for entity_name, entity in entity_map.items():
                haystack = f"{entity_name} {entity.get('description', '')}".lower()
                if any(token and token in haystack for token in query_tokens):
                    starts.append(entity_name)

        if not starts:
            starts = [
                e.get("entity_name", "")
                for e in sorted(
                    filtered_data.entities,
                    key=lambda x: -x.get("_ppr_score", 0),
                )[:8]
                if e.get("entity_name", "")
            ]

        starts = list(dict.fromkeys(starts))[:8]

        path_candidates: List[Tuple[List[str], List[Dict[str, Any]], float]] = []

        for start in starts:
            stack: List[Tuple[str, List[str], List[Dict[str, Any]], float]] = [
                (start, [start], [], entity_map[start].get("_ppr_score", 0.0))
            ]

            while stack:
                current, nodes, rels, score = stack.pop()
                hop_count = len(nodes) - 1

                if hop_count >= 1:
                    path_candidates.append((nodes, rels, score))

                if hop_count >= max_hops:
                    continue

                for nxt, rel in adjacency.get(current, []):
                    if nxt in nodes:
                        continue
                    rel_score = rel.get("_ppr_score", 0.0)
                    next_score = (
                        score
                        + entity_map[nxt].get("_ppr_score", 0.0)
                        + 0.3 * rel_score
                    )
                    stack.append((nxt, nodes + [nxt], rels + [rel], next_score))

        if not path_candidates:
            return []

        path_candidates.sort(key=lambda x: x[2], reverse=True)

        unique_paths: List[Tuple[List[str], List[Dict[str, Any]], float]] = []
        seen_signatures = set()
        for nodes, rels, score in path_candidates:
            signature = tuple(nodes)
            if signature in seen_signatures:
                continue
            seen_signatures.add(signature)
            unique_paths.append((nodes, rels, score))
            if len(unique_paths) >= top_n:
                break

        return unique_paths

    def _build_graph_details_section(
        self,
        entities: List[Dict[str, Any]],
        relationships: List[Dict[str, Any]],
    ) -> str:
        """构建知识图谱详情区，包含实体和关系的描述。"""
        if not entities and not relationships:
            return ""

        lines = ["## 知识图谱详情\n"]
        
        if entities:
            lines.append("### 实体详情")
            for entity in entities:
                name = entity.get("entity_name", "Unknown")
                etype = entity.get("entity_type", "")
                desc = entity.get("description", "")[:200]
                lines.append(f"- **{name}** ({etype}): {desc}")
        
        if relationships:
            lines.append("\n### 关系详情")
            for rel in relationships:
                src = rel.get("src_id", "")
                tgt = rel.get("tgt_id", "")
                desc = rel.get("description", "")[:150]
                keywords = rel.get("keywords", "")
                lines.append(f"- {src} -> {tgt}: {desc}" + (f" ({keywords})" if keywords else ""))

        return "\n".join(lines)

    def _build_path_section(
        self,
        paths: List[Tuple[List[str], List[Dict[str, Any]], float]],
    ) -> str:
        if not paths:
            return ""

        lines = ["## 核心知识路径\n"]
        for idx, (nodes, _, _) in enumerate(paths, start=1):
            chain = " -> ".join(nodes)
            lines.append(f"- 路径 {idx}: {chain}")

        return "\n".join(lines)

    
    def _build_chunk_section_atomic(
        self,
        chunks: List[Dict[str, Any]],
        max_tokens: int,
    ) -> str:
        if not chunks:
            return ""
        
        lines = ["## 相关文档内容\n"]
        current_tokens = self._count_tokens("\n".join(lines))
        
        for i, chunk in enumerate(chunks):
            content = chunk.get("content", "")
            file_path = chunk.get("file_path", "")
            
            chunk_text = f"\n### 文档片段 {i+1} (来源: {file_path})\n{content}\n"
            chunk_tokens = self._count_tokens(chunk_text)
            
            if current_tokens + chunk_tokens > max_tokens:
                logger.info(f"[原子组装] 达到 Token 限制，停止添加文本块 (已添加 {i} 个)")
                break
            
            lines.append(chunk_text)
            current_tokens += chunk_tokens
        
        return "\n".join(lines)

    def _serialize_knowledge_paths(
        self,
        filtered_data: PPRFilteredData,
        query: Optional[str] = None,
        top_n: int = 5,
        max_hops: int = 2,
    ) -> List[Dict[str, Any]]:
        """
        将核心路径序列化为结构化的证据链条。

        从过滤后的数据中提取核心路径（_extract_core_paths），
        并将每条路径转换为可被 JSON 直接序列化的字典格式。

        返回格式示例：
        [
            {
                "nodes": ["实体A", "实体B", "实体C"],
                "relations": [
                    {"src": "实体A", "tgt": "实体B", "description": "..."},
                    {"src": "实体B", "tgt": "实体C", "description": "..."}
                ],
                "score": 0.95
            }
        ]
        """
        raw_paths = self._extract_core_paths(
            filtered_data,
            query=query,
            top_n=top_n,
            max_hops=max_hops,
        )

        if not raw_paths:
            return []

        serialized: List[Dict[str, Any]] = []
        for nodes, rels, score in raw_paths:
            serialized_rels = []
            for rel in rels:
                serialized_rels.append({
                    "src": rel.get("src_id", ""),
                    "tgt": rel.get("tgt_id", ""),
                    "description": rel.get("description", ""),
                    "keywords": rel.get("keywords", ""),
                    "weight": rel.get("weight", 1.0),
                })
            serialized.append({
                "nodes": nodes,
                "relations": serialized_rels,
                "score": round(score, 4),
            })

        logger.info(f"[知识序列化] 提取 {len(serialized)} 条证据链条")
        return serialized
