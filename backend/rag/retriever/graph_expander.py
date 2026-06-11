"""
Path-D：直连 NebulaGraph 进行自适应深度图扩展

从种子实体集出发，按 QueryType 决定遍历深度（1~2 hop），
返回扩展实体 + 关系列表，每条记录携带图扩展分数
"""

from __future__ import annotations

import asyncio
from dataclasses import dataclass
from typing import Any, Callable, Dict, List, Optional, Set, Tuple

import numpy as np

from backend.config.loggers import get_logger
from backend.rag.keywords.models import QueryType
from backend.rag.retriever.config import DirectRetrieverConfig, get_graph_depth
from backend.rag.retriever.entity_recall import SeedEntity

logger = get_logger(__name__)


@dataclass
class GraphExpandedEntity:
    """图扩展得到的实体"""
    name: str
    graph_score: float = 0.0
    graph_hop: int = 1
    entity_type: str = ""
    description: str = ""
    source_id: str = ""
    file_path: str = ""
    recall_path: str = "D"

    def to_seed_entity(self) -> SeedEntity:
        return SeedEntity(
            name=self.name,
            fused_score=self.graph_score,
            vec_score=0.0,
            bm25_score=0.0,
            seed_weight=0.0,
            graph_hop=self.graph_hop,
            in_hl=False,
            in_ll=False,
            recall_path=self.recall_path,
            entity_type=self.entity_type,
            description=self.description,
            source_id=self.source_id,
            file_path=self.file_path,
        )


@dataclass
class GraphRelationship:
    """图扩展获取的关系"""
    src_entity: str
    dst_entity: str
    rel_type: str = ""
    description: str = ""
    weight: float = 1.0
    source_id: str = ""
    file_path: str = ""
    graph_score: float = 0.0

    def to_dict(self) -> Dict[str, Any]:
        return {
            "src_id":         self.src_entity,
            "tgt_id":         self.dst_entity,
            "src_tgt":        [self.src_entity, self.dst_entity],
            "relation_type":  self.rel_type,
            "description":    self.description,
            "weight":         self.weight,
            "source_id":      self.source_id,
            "file_path":      self.file_path,
            "_graph_score":   self.graph_score,
            "_recall_path":   "D",
        }


class GraphExpander:
    """
    Path-D：基于 NebulaGraph 的自适应深度图扩展器

    直接访问 NebulaGraphStorage 的 _pool，执行 GO N STEPS 查询
    """

    def __init__(
        self, 
        nebula_storage: Any,        # NebulaGraphStorage 实例
        embedding_func: Optional[Callable] = None,
        config: Optional[DirectRetrieverConfig] = None,
    ):
        self._nebula = nebula_storage
        self._embedding_func = embedding_func
        self._config = config or DirectRetrieverConfig()

    def _is_available(self) -> bool:
        return (
            self._nebula is not None
            and getattr(self._nebula, "_initialized", False)
            and getattr(self._nebula, "_pool", None) is not None
        )

    def _escape(self, s: str) -> str:
        if not s:
            return ""
        return (
            s.replace("\\", "\\\\")
             .replace('"', '\\"')
             .replace("\n", "\\n")
             .replace("\r", "\\r")
        )

    def _sync_expand(
        self,
        seed_scores: Dict[str, float],
        depth: int,
        rel_weight_floor: float,
        max_neighbors: int,
    ) -> Tuple[List[GraphExpandedEntity], List[GraphRelationship]]:
        """同步执行 NebulaGraph GO 查询，并利用结构传导打分"""
        if not seed_scores:
            return [], []

        from collections import defaultdict

        seed_names = list(seed_scores.keys())
        pool = self._nebula._pool
        space = self._nebula._space
        user = self._nebula._user
        password = self._nebula._password

        session = None
        entities: List[GraphExpandedEntity] = []
        relationships: List[GraphRelationship] = []
        seen_rel_keys: Set[Tuple[str, str]] = set()
        
        # 收集所有遇到的节点属性
        all_nodes_info: Dict[str, Dict[str, str]] = {}

        # NebulaGraph 中所有边统一为 relation 边类型，语义通过 relation_type 属性区分
        over_clause = "OVER relation"

        try:
            session = pool.get_session(user, password)
            session.execute(f"USE `{space}`")

            batch_size = 200
            for batch_start in range(0, len(seed_names), batch_size):
                batch = seed_names[batch_start: batch_start + batch_size]
                vids = ", ".join(f'"{self._escape(n)}"' for n in batch)

                per_step_limit = max_neighbors * len(batch)
                limit_list = "[" + ", ".join([str(per_step_limit)] * depth) + "]"

                nGQL = (
                    f"GO {depth} STEPS FROM {vids} {over_clause} BIDIRECT "
                    f"WHERE properties(edge).description != '' "
                    f"AND properties(edge).weight >= {rel_weight_floor} "
                    f"YIELD "
                    f"  id($^)                               AS src_id, "
                    f"  id($$)                               AS dst_id, "
                    f"  properties(edge).relation_type       AS rel_type, "
                    f"  properties(edge).description         AS rel_desc, "
                    f"  properties(edge).weight              AS weight, "
                    f"  properties(edge).source_id           AS source_id, "
                    f"  properties(edge).file_path           AS file_path, "
                    f"  $^.entity.entity_type                AS src_type, "
                    f"  $^.entity.description                AS src_desc, "
                    f"  $^.entity.source_id                  AS src_source_id, "
                    f"  $^.entity.file_path                  AS src_file_path, "
                    f"  $$.entity.entity_type                AS dst_type, "
                    f"  $$.entity.description                AS dst_desc, "
                    f"  $$.entity.source_id                  AS dst_source_id, "
                    f"  $$.entity.file_path                  AS dst_file_path "
                    f"LIMIT {limit_list}"
                )

                result = session.execute(nGQL)
                if not result.is_succeeded():
                    logger.warning("[Path-D] GO 查询失败: %s", result.error_msg())
                    continue

                for row_idx in range(result.row_size()):
                    row = result.row_values(row_idx)
                    try:
                        src_id      = self._nebula._safe_get_string(row[0])
                        dst_id      = self._nebula._safe_get_string(row[1])
                        rel_type    = self._nebula._safe_get_string(row[2])
                        rel_desc    = self._nebula._safe_get_string(row[3])
                        weight_val  = row[4].as_double() if not row[4].is_empty() else 0.5
                        src_id_str  = self._nebula._safe_get_string(row[5])
                        file_path   = self._nebula._safe_get_string(row[6])
                        
                        src_type    = self._nebula._safe_get_string(row[7])
                        src_desc    = self._nebula._safe_get_string(row[8])
                        src_src_id  = self._nebula._safe_get_string(row[9])
                        src_fp      = self._nebula._safe_get_string(row[10])
                        
                        dst_type    = self._nebula._safe_get_string(row[11])
                        dst_desc    = self._nebula._safe_get_string(row[12])
                        dst_src_id  = self._nebula._safe_get_string(row[13])
                        dst_fp      = self._nebula._safe_get_string(row[14])
                    except Exception:
                        continue

                    # 收集节点属性（去重，同时照顾双向）
                    if src_id:
                        all_nodes_info[src_id] = {
                            "entity_type": src_type,
                            "description": src_desc,
                            "source_id": src_src_id,
                            "file_path": src_fp,
                        }
                    if dst_id:
                        all_nodes_info[dst_id] = {
                            "entity_type": dst_type,
                            "description": dst_desc,
                            "source_id": dst_src_id,
                            "file_path": dst_fp,
                        }

                    # 收集关系（去重）
                    rel_key = (min(src_id, dst_id), max(src_id, dst_id))
                    if rel_key not in seen_rel_keys and src_id and dst_id:
                        seen_rel_keys.add(rel_key)
                        relationships.append(GraphRelationship(
                            src_entity=src_id,
                            dst_entity=dst_id,
                            rel_type=rel_type,
                            description=rel_desc,
                            weight=weight_val,
                            source_id=src_id_str,
                            file_path=file_path,
                        ))

        except Exception as e:
            logger.warning("[Path-D] NebulaGraph 图扩展失败: %s", e, exc_info=True)
        finally:
            if session:
                try:
                    session.release()
                except Exception:
                    pass

        # ── BFS / Dijkstra-like Score Propagation ──
        entity_graph_scores: Dict[str, float] = dict(seed_scores)
        
        # 建立无向邻接图
        adj = defaultdict(list)
        for rel in relationships:
            adj[rel.src_entity].append((rel.dst_entity, rel.weight))
            adj[rel.dst_entity].append((rel.src_entity, rel.weight))

        # 层序迭代传导得分
        current_level = set(seed_scores.keys())
        for hop in range(1, depth + 1):
            next_level = set()
            hop_decay = 0.8 ** hop
            for u in current_level:
                score_u = entity_graph_scores.get(u, 0.1)
                for v, weight_val in adj[u]:
                    if v in seed_scores:
                        continue  # 种子节点的分数保持原样
                    
                    smoothed_weight = 1.0 + np.log1p(max(0, weight_val - 1.0))
                    伝导分 = score_u * smoothed_weight * hop_decay
                    
                    if 伝导分 > entity_graph_scores.get(v, 0.0):
                        entity_graph_scores[v] = 伝导分
                        next_level.add(v)
            current_level = next_level

        # 收集目标实体并赋予计算出的结构分
        for node_id, info in all_nodes_info.items():
            if node_id and node_id not in seed_scores:
                score = entity_graph_scores.get(node_id, 0.0)
                if score > 0.0:
                    entities.append(GraphExpandedEntity(
                        name=node_id,
                        graph_score=score,
                        graph_hop=depth,
                        entity_type=info["entity_type"],
                        description=info["description"],
                        source_id=info["source_id"],
                        file_path=info["file_path"],
                    ))

        # 统一写入结构传导分，并归一化到 [0, 1] 区间
        if entities:
            max_score = max((e.graph_score for e in entities), default=1.0)
            if max_score > 0:
                for e in entities:
                    e.graph_score = min(1.0, e.graph_score / max_score)

        return entities, relationships

    async def expand(
        self,
        seeds: List[SeedEntity],
        query_embedding: np.ndarray,
        query_type: QueryType,
    ) -> Tuple[List[GraphExpandedEntity], List[GraphRelationship]]:
        """
        从种子实体出发进行图扩展

        Returns:
            (expanded_entities, relationships)
            expanded_entities 不含种子本身（已在 AB 中），只包含邻居
        """
        if not self._is_available():
            logger.warning("[Path-D] NebulaGraph 未初始化，跳过图扩展")
            return [], []

        if not seeds:
            return [], []

        cfg = self._config
        depth = get_graph_depth(query_type)
        rel_floor = cfg.graph_relation_weight_floor
        max_nb = cfg.graph_max_neighbors_per_seed

        # 按 fused_score 降序，只取 top-N 种子进行图扩展
        # 种子过多会导致 NebulaGraph GO 查询耗时线性增长
        max_seeds = cfg.graph_max_seeds
        top_seeds = sorted(seeds, key=lambda s: s.fused_score, reverse=True)[:max_seeds]
        seed_scores = {s.name: s.fused_score for s in top_seeds}
        logger.info("[Path-D] 图扩展: 种子=%d/%d, depth=%d, rel_floor=%.2f",
                    len(seed_scores), len(seeds), depth, rel_floor)

        # 在线程池中执行同步 NebulaGraph 查询
        entities, relationships = await asyncio.to_thread(
            self._sync_expand, seed_scores, depth, rel_floor, max_nb
        )

        return entities, relationships
