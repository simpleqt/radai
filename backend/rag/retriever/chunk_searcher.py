"""
Path-C：直连 Milvus chunks_vdb 进行语义 ANN 检索

绕过 LightRAG 的 entity→source_id 链路，直接对 chunk 向量做相似度搜索
"""

from __future__ import annotations

import asyncio
from dataclasses import dataclass
from typing import Any, Dict, List, Optional

import numpy as np

from backend.config.loggers import get_logger
from backend.rag.keywords.models import QueryType
from backend.rag.retriever.config import (
    DirectRetrieverConfig,
    get_chunk_vec_score_floor,
    get_chunk_vec_top_k_multiplier,
)

logger = get_logger(__name__)


@dataclass
class ChunkCandidate:
    """Path-C 召回的 Chunk 候选"""
    chunk_id: str
    content: str
    file_path: str = ""
    full_doc_id: str = ""
    created_at: str = ""
    vec_score: float = 0.0
    recall_path: str = "vec"

    def to_retrieval_dict(self) -> Dict[str, Any]:
        return {
            "id":           self.chunk_id,
            "content":      self.content,
            "file_path":    self.file_path,
            "full_doc_id":  self.full_doc_id,
            "doc_id":       self.full_doc_id,
            "created_at":   self.created_at,
            "_vec_score":   self.vec_score,
            "_entity_score": 0.0,
            "_coverage_score": 0.0,
            "_chunk_score": self.vec_score,
            "_recall_path": self.recall_path,
            "source_type":  "direct_vec",
        }


def _resolve_chunks_collection(lightrag_instance: Any) -> Optional[str]:
    """
    从 LightRAG 实例中自动探测 chunks_vdb 的 Milvus collection 名称

    LightRAG 实例上的 chunks_vdb 是 MilvusVectorDBStorage，
    其 final_namespace 是实际 collection 名称。
    """
    if lightrag_instance is None:
        return None
    try:
        chunks_vdb = getattr(lightrag_instance, "chunks_vdb", None)
        if chunks_vdb is None:
            return None
        name = getattr(chunks_vdb, "final_namespace", None)
        return str(name) if name else None
    except Exception:
        return None


def _get_milvus_client(lightrag_instance: Any):
    """从 LightRAG chunks_vdb 获取 Milvus 底层客户端"""
    try:
        chunks_vdb = getattr(lightrag_instance, "chunks_vdb", None)
        if chunks_vdb is None:
            return None
        return getattr(chunks_vdb, "_client", None)
    except Exception:
        return None


class DirectChunkSearcher:
    """
    直连 Milvus chunks_vdb 的 Chunk 语义搜索器（Path-C）

    使用方式：
        searcher = DirectChunkSearcher(lightrag_instance, config)
        chunks = await searcher.search(query_embedding, query_type, chunk_top_k)
    """

    def __init__(
        self,
        lightrag_instance: Any,
        config: Optional[DirectRetrieverConfig] = None,
        collection_override: Optional[str] = None,
    ):
        self._lightrag = lightrag_instance
        self._config = config or DirectRetrieverConfig()
        self._collection_name: Optional[str] = (
            collection_override
            or self._config.chunk_vec_collection_override
            or _resolve_chunks_collection(lightrag_instance)
        )
        self._milvus_client = _get_milvus_client(lightrag_instance)

    def is_available(self) -> bool:
        return bool(self._collection_name and self._milvus_client is not None)

    async def search(
        self,
        query_embedding: np.ndarray,
        query_type: QueryType,
        base_chunk_top_k: int = 40,
    ) -> List[ChunkCandidate]:
        """
        对 chunks_vdb 做 ANN 搜索，返回 ChunkCandidate 列表
        """
        if not self.is_available():
            logger.warning("[Path-C] chunks_vdb 不可用，跳过直接向量检索")
            return []

        multiplier = get_chunk_vec_top_k_multiplier(query_type)
        score_floor = get_chunk_vec_score_floor(query_type)
        top_k = min(int(base_chunk_top_k * multiplier), 120)

        query_vector = query_embedding.tolist() if hasattr(query_embedding, "tolist") else list(query_embedding)

        def _search_sync() -> List[ChunkCandidate]:
            try:
                search_params = {"metric_type": "COSINE", "params": {"ef": 128}}
                output_fields = ["id", "content", "file_path", "full_doc_id", "created_at"]

                results = self._milvus_client.search(
                    collection_name=self._collection_name,
                    data=[query_vector],
                    anns_field="vector",
                    search_params=search_params,
                    limit=top_k,
                    output_fields=output_fields,
                )

                candidates: List[ChunkCandidate] = []
                for hits in results:
                    for hit in hits:
                        score = float(hit.get("distance", 0.0))
                        if score < score_floor:
                            continue
                        entity = hit.get("entity", hit)
                        chunk_id = str(entity.get("id", ""))
                        content = str(entity.get("content", ""))
                        if not content:
                            continue
                        candidates.append(ChunkCandidate(
                            chunk_id=chunk_id,
                            content=content,
                            file_path=str(entity.get("file_path", "")),
                            full_doc_id=str(entity.get("full_doc_id", "")),
                            created_at=str(entity.get("created_at", "")),
                            vec_score=score,
                        ))
                return candidates
            except Exception as e:
                logger.warning("[Path-C] Milvus search 执行失败: %s", e, exc_info=True)
                return []

        candidates = await asyncio.to_thread(_search_sync)
        logger.info(
            "[Path-C] Chunk 向量召回: collection=%s, top_k=%d, score_floor=%.2f, 结果=%d",
            self._collection_name, top_k, score_floor, len(candidates),
        )
        return candidates
