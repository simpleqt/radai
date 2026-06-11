"""
backend.rag.retriever — 无 LightRAG 精细化检索模块

核心入口：
    DirectRetriever   四路并行召回主类
    build_direct_retriever  工厂函数
"""

from backend.rag.retriever.config import DirectRetrieverConfig
from backend.rag.retriever.direct_retriever import DirectRetriever, build_direct_retriever
from backend.rag.retriever.entity_recall import EntityRecaller, SeedEntity
from backend.rag.retriever.chunk_searcher import DirectChunkSearcher, ChunkCandidate
from backend.rag.retriever.graph_expander import GraphExpander, GraphExpandedEntity, GraphRelationship
from backend.rag.retriever.fusion import (
    merge_entity_pool,
    merge_chunk_pool,
    build_ppr_seed_weights,
    chunk_entity_coverage,
)

__all__ = [
    "DirectRetrieverConfig",
    "DirectRetriever",
    "build_direct_retriever",
    "EntityRecaller",
    "SeedEntity",
    "DirectChunkSearcher",
    "ChunkCandidate",
    "GraphExpander",
    "GraphExpandedEntity",
    "GraphRelationship",
    "merge_entity_pool",
    "merge_chunk_pool",
    "build_ppr_seed_weights",
    "chunk_entity_coverage",
]
