"""
DirectRetriever：四路并行召回主类

完全绕过 lightrag.aquery_data，直连存储层执行：
  Path-A  Milvus keyword_index 实体向量 ANN
  Path-B  Redis BM25 + 多关键词扩展
  Path-C  Milvus chunks_vdb 直接 Chunk 语义搜索
  Path-D  NebulaGraph 自适应深度图扩展

输出 RawRetrievalData（每条 entity/relationship/chunk 均携带原始分数标签）
"""

from __future__ import annotations

import asyncio
import time
from typing import Any, Callable, Dict, List, Optional, Tuple

import numpy as np

from backend.config.loggers import get_logger
from backend.rag.content_filter.models import RawRetrievalData
from backend.rag.keywords.models import QueryType
from backend.rag.retriever.chunk_searcher import DirectChunkSearcher
from backend.rag.retriever.config import (
    DirectRetrieverConfig,
    call_embedding_func,
    direct_retriever_config_from_env,
)
from backend.rag.retriever.entity_recall import EntityRecaller, SeedEntity
from backend.rag.retriever.fusion import (
    build_ppr_seed_weights,
    merge_chunk_pool,
    merge_entity_pool,
)
from backend.rag.retriever.graph_expander import GraphExpander

logger = get_logger(__name__)

# LightRAG GRAPH_FIELD_SEP（source_id 中多 chunk 用此分隔）
try:
    from lightrag.utils import GRAPH_FIELD_SEP
except ImportError:
    GRAPH_FIELD_SEP = "<SEP>"


class DirectRetriever:
    """
    四路并行检索器，替代 _stage1_recall 中的 lightrag.aquery_data 调用

    实例化：
        retriever = DirectRetriever(
            kb_code, kb_id,
            embedding_func  = rag.lightrag.embedding_func,
            keyword_index   = per_kb_keyword_index,
            nebula_storage  = rag.lightrag.chunk_entity_relation_graph,
            lightrag_instance = rag.lightrag,
            config          = DirectRetrieverConfig(),
        )

    调用：
        raw_data = await retriever.retrieve(query, hl_keywords, ll_keywords, ...)
    """

    def __init__(
        self,
        kb_code: str,
        kb_id: str,
        embedding_func: Callable,
        keyword_index: Any,          # PerKBKeywordIndex
        nebula_storage: Any,         # NebulaGraphStorage
        lightrag_instance: Any,      # 用于获取 chunks_vdb（text_chunks_db）引用
        config: Optional[DirectRetrieverConfig] = None,
    ):
        self._kb_code = kb_code
        self._kb_id = kb_id
        self._embedding_func = embedding_func
        self._config = config or direct_retriever_config_from_env()

        self._entity_recaller = EntityRecaller(
            keyword_index=keyword_index,
            embedding_func=embedding_func,
            config=self._config,
        )
        self._chunk_searcher = DirectChunkSearcher(
            lightrag_instance=lightrag_instance,
            config=self._config,
        )
        self._graph_expander = GraphExpander(
            nebula_storage=nebula_storage,
            embedding_func=embedding_func,
            config=self._config,
        )
        self._lightrag = lightrag_instance

    # ─────────────────────────────────────────────────────────────
    # 公开接口
    # ─────────────────────────────────────────────────────────────

    async def retrieve(
        self,
        query: str,
        hl_keywords: List[str],
        ll_keywords: List[str],
        query_type: QueryType,
        weighted_seed_scores: Dict[str, float],
        top_k: int = 80,
        chunk_top_k: int = 60,
        query_embedding: Optional[np.ndarray] = None,
    ) -> Tuple[RawRetrievalData, Dict[str, float]]:
        """
        执行四路并行召回

        Returns:
            (raw_data, enhanced_seed_weights)
            raw_data              : 携带原始分数标签的 RawRetrievalData
            enhanced_seed_weights : 双源合并后的 PPR 种子权重
        """
        t0 = time.monotonic()
        cfg = self._config

        # ── Step 0: 获取 query embedding（四路共用）──────────────
        if query_embedding is None:
            query_embedding = await self._get_query_embedding(query)

        # ── Step 1: 并行执行 Path-A+B 和 Path-C ─────────────────
        ab_task = self._entity_recaller.recall(
            query, query_embedding, hl_keywords, ll_keywords,
            query_type, weighted_seed_scores,
        ) if (cfg.enable_path_a or cfg.enable_path_b) else _empty_list()

        c_task = self._chunk_searcher.search(
            query_embedding, query_type, base_chunk_top_k=chunk_top_k,
        ) if cfg.enable_path_c else _empty_list()

        seeds, chunk_vec_results = await asyncio.gather(ab_task, c_task)

        # ── Step 2: Path-D 图扩展（依赖种子集）──────────────────
        d_entities, relationships = [], []
        if cfg.enable_path_d and seeds:
            d_entities, relationships = await self._graph_expander.expand(
                seeds, query_embedding, query_type
            )

        # ── Step 3: 实体池合并（边感知截断 + 关系过滤）─────────────
        all_entities, filtered_rel_dicts = merge_entity_pool(
            seeds, d_entities, relationships, query_type, cfg
        )

        # ── Step 4: 实体链路 Chunks 拉取 ─────────────────────────
        entity_chunks = await self._fetch_entity_linked_chunks(
            all_entities, seeds, chunk_top_k
        )

        # ── Step 5: Chunk 池合并 ──────────────────────────────────
        all_chunks = merge_chunk_pool(
            entity_chunks, chunk_vec_results, seeds, query_type, cfg
        )

        # ── Step 6: 双源 PPR 种子权重 ────────────────────────────
        enhanced_weights = build_ppr_seed_weights(
            all_entities,
            weighted_seed_scores,
            blend_ratio=cfg.ppr_seed_blend_ratio,
        )

        elapsed_ms = (time.monotonic() - t0) * 1000
        logger.info(
            "[DirectRetriever] 完成: kb=%s, 耗时=%.1fms, "
            "实体=%d, 关系=%d, chunks=%d",
            self._kb_code, elapsed_ms,
            len(all_entities), len(filtered_rel_dicts), len(all_chunks),
        )

        raw_data = RawRetrievalData(
            entities=all_entities,
            relationships=filtered_rel_dicts,
            chunks=all_chunks,
            query_embedding=query_embedding,
        )
        return raw_data, enhanced_weights

    # ─────────────────────────────────────────────────────────────
    # 内部工具
    # ─────────────────────────────────────────────────────────────

    async def _get_query_embedding(self, query: str) -> np.ndarray:
        """计算查询文本的 embedding（兼容 async/sync embedding_func）"""
        try:
            result = await call_embedding_func(self._embedding_func, [query])
            # 避免对 numpy array 做 bool 判断（会抛 ambiguous truth value）
            if result is not None and len(result) > 0:
                return np.array(result[0], dtype=np.float32)
        except Exception as e:
            logger.warning("[DirectRetriever] query embedding 计算失败: %s", e)
        # 回退：返回零向量（各路径会优雅降级）
        return np.zeros(4096, dtype=np.float32)

    async def _fetch_entity_linked_chunks(
        self,
        entities: List[Dict[str, Any]],
        seeds: List[SeedEntity],
        chunk_top_k: int,
    ) -> List[Dict[str, Any]]:
        """
        通过实体的 source_id 字段拉取关联 chunk

        source_id 可能是单个 chunk_id 或 GRAPH_FIELD_SEP 分隔的多个 chunk_id
        """
        if not entities:
            return []

        # 收集所有 chunk_id（去重）
        chunk_id_set: Dict[str, Dict[str, Any]] = {}
        entity_weight: Dict[str, float] = {
            s.name: s.fused_score for s in seeds
        }

        for e in entities:
            ent_name = e.get("entity_name", "")
            source_id = e.get("source_id", "")
            if not source_id:
                continue
            
            # e.get("_fused_score") 不一定存在，我们应该优先取图扩展带来的 e.get("_graph_score") 或者 e.get("_fused_score")
            # 因为经过融合后，图节点的权重主要体现在 _graph_score
            fallback_score = e.get("_graph_score", e.get("_fused_score", 0.1))
            e_weight = entity_weight.get(ent_name, fallback_score)

            for cid in source_id.split(GRAPH_FIELD_SEP):
                cid = cid.strip()
                if not cid:
                    continue
                if cid not in chunk_id_set or chunk_id_set[cid]["_entity_score"] < e_weight:
                    chunk_id_set[cid] = {
                        "_entity_score": e_weight,
                        "_source_entity": ent_name,
                    }

        if not chunk_id_set:
            return []

        # 按实体权重排序取 top chunk_id
        sorted_cids = sorted(
            chunk_id_set.items(),
            key=lambda x: -x[1]["_entity_score"],
        )
        
        # 【Lazy Fetch 优化】：动态截断，避免拉取过多垃圾 chunk。
        # 这里改为只取 chunk_top_k 的 1.5 倍，而不是 3 倍。因为基于传导分数的实体权重已经足够准确。
        lazy_limit = int(chunk_top_k * 1.5)
        top_cids = [cid for cid, _ in sorted_cids[:lazy_limit]]

        # 批量拉取（Redis → Milvus fallback）
        chunks = await self._batch_fetch_chunks(top_cids)

        # 标注来源信息
        result = []
        for ch in chunks:
            if not ch or not ch.get("content"):
                continue
            cid_key = ch.get("id") or ch.get("chunk_id") or ""
            meta = chunk_id_set.get(cid_key, {})
            ch = ch.copy()
            ch["_entity_score"] = meta.get("_entity_score", 0.3)
            ch["_vec_score"]    = 0.0
            ch["_recall_path"]  = "entity"
            result.append(ch)

        logger.info(
            "[DirectRetriever] 实体链路 chunk 拉取: 实体=%d, chunk_ids=%d, 返回=%d",
            len(entities), len(top_cids), len(result),
        )
        return result

    async def _batch_fetch_chunks(
        self, chunk_ids: List[str]
    ) -> List[Dict[str, Any]]:
        """
        批量获取 chunk 内容：优先 Redis text_chunks_db，缺失时 Milvus fallback
        """
        if not chunk_ids:
            return []

        # LightRAG 中 KV 存储的属性名是 text_chunks（不是 text_chunks_db）
        text_chunks_db = (
            getattr(self._lightrag, "text_chunks", None)
            or getattr(self._lightrag, "text_chunks_db", None)
        )
        chunks_vdb = getattr(self._lightrag, "chunks_vdb", None)

        if text_chunks_db is None:
            logger.warning("[DirectRetriever] text_chunks / text_chunks_db 均未找到，跳过实体链路")
            return []

        try:
            from backend.rag.utils.lightrag_chunk_patch import get_chunks_with_milvus_fallback

            chunk_data_list = await get_chunks_with_milvus_fallback(
                chunk_ids, text_chunks_db, chunks_vdb
            )
            result = []
            for cid, data in zip(chunk_ids, chunk_data_list):
                if data and data.get("content"):
                    data = dict(data)
                    data.setdefault("id", cid)
                    data.setdefault("chunk_id", cid)
                    result.append(data)
            return result
        except Exception as e:
            logger.warning("[DirectRetriever] batch_fetch_chunks 失败: %s", e)
            return []


# ─────────────────────────────────────────────────────────────────
# 工厂函数
# ─────────────────────────────────────────────────────────────────

async def build_direct_retriever(
    kb_code: str,
    kb_id: str,
    rag_instance: Any,
    keyword_index: Any,
    config: Optional[DirectRetrieverConfig] = None,
) -> Optional[DirectRetriever]:
    """
    从 RAGAnything 实例构建 DirectRetriever

    Args:
        kb_code          : 知识库 code
        kb_id            : 知识库 ID
        rag_instance     : RAGAnything 实例（含 lightrag 属性）
        keyword_index    : PerKBKeywordIndex 实例
        config           : 可选配置

    Returns:
        DirectRetriever 实例，若依赖缺失则返回 None
    """
    try:
        lightrag = getattr(rag_instance, "lightrag", None)
        if lightrag is None:
            logger.warning("[build_direct_retriever] lightrag 实例不存在")
            return None

        embedding_func = getattr(lightrag, "embedding_func", None)
        if embedding_func is None:
            logger.warning("[build_direct_retriever] embedding_func 未找到")
            return None

        # LightRAG 中图存储的实际属性名为 chunk_entity_relation_graph
        graph_storage = getattr(lightrag, "chunk_entity_relation_graph", None)
        if graph_storage is None:
            graph_storage = getattr(lightrag, "graph_storage", None)
        if graph_storage is None:
            graph_storage = getattr(lightrag, "knowledge_graph_inst", None)

        return DirectRetriever(
            kb_code=kb_code,
            kb_id=kb_id,
            embedding_func=embedding_func,
            keyword_index=keyword_index,
            nebula_storage=graph_storage,
            lightrag_instance=lightrag,
            config=config or direct_retriever_config_from_env(),
        )
    except Exception as e:
        logger.error("[build_direct_retriever] 构建失败: %s", e, exc_info=True)
        return None


# ─────────────────────────────────────────────────────────────────
# 工具
# ─────────────────────────────────────────────────────────────────

async def _empty_list():
    return []
