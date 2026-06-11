"""
MMR (Maximal Marginal Relevance) 重排序器

从 backend/agents/nuclear_electronics_agent.py 迁移
完全保留原有算法实现，不做任何修改
"""

import time
from typing import Dict, List, Optional

import numpy as np

from backend.config.loggers import get_logger

logger = get_logger(__name__)


class VectorMMRReranker:
    """
    基于稠密向量的MMR重排序
    使用真实的Embedding向量计算语义相似度，彻底解决中文分词失效问题
    """
    
    def __init__(self, embedding_dim: int = 768):
        self.embedding_dim = embedding_dim
        self._embedder = None
    
    async def _get_embedder(self):
        """懒加载embedding服务"""
        if self._embedder is None:
            try:
                from backend.rag.service import get_rag_service
                rag_service = get_rag_service()
                await rag_service.initialize()
                if hasattr(rag_service, '_rag') and hasattr(rag_service._rag, 'lightrag'):
                    self._embedder = rag_service._rag.lightrag
            except Exception as e:
                logger.warning(f"[MMR] 获取embedding服务失败: {e}")
                self._embedder = None
        return self._embedder
    
    async def _get_embeddings(self, texts: List[str]) -> List[List[float]]:
        """获取文本的embedding向量"""
        embedder = await self._get_embedder()
        if embedder is None:
            return self._mock_embeddings(len(texts))
        
        try:
            if hasattr(embedder, 'embedding_func'):
                embeddings = await embedder.embedding_func(texts)
                return embeddings
            else:
                return self._mock_embeddings(len(texts))
        except Exception as e:
            logger.warning(f"[MMR] 获取embedding失败: {e}")
            return self._mock_embeddings(len(texts))
    
    def _mock_embeddings(self, count: int) -> List[List[float]]:
        """生成模拟embedding（用于降级场景）"""
        rng = np.random.RandomState(42)
        return [rng.randn(self.embedding_dim).tolist() for _ in range(count)]
    
    async def rerank(
        self,
        query: str,
        chunks: List[Dict],
        rerank_scores: Dict[int, float],
        lambda_param: float = 0.7,
        top_n: int = 20,
        use_fused_score: bool = False,
        precomputed_embeddings: Optional[List[List[float]]] = None,
        hl_keywords: Optional[List[str]] = None,
        protected_chunk_indices: Optional[set[int]] = None,
    ) -> List[Dict]:
        """
        MMR重排序核心算法
        
        Args:
            query: 查询文本
            chunks: 候选chunk列表
            rerank_scores: {index: score} 交叉注意力分数或融合分数
            lambda_param: MMR的lambda参数（0.7看重相关，0.3看重多样）
            top_n: 返回数量
            use_fused_score: 是否使用融合分数（PPR + Rerank）
            precomputed_embeddings: 预计算的embedding向量，避免重复计算
            hl_keywords: 高层关键词，用于保护包含关键词的chunk不被丢弃
        
        Returns:
            重排序后的chunk列表
        """
        if len(chunks) <= top_n:
            return chunks

        # 标记包含 HL 关键词的 chunk，MMR 不能丢弃它们
        keyword_chunk_indices = set()
        if hl_keywords:
            for i, chunk in enumerate(chunks):
                content_lower = chunk.get("content", "").lower()
                if any(kw.lower() in content_lower for kw in hl_keywords):
                    keyword_chunk_indices.add(i)

        protected_chunk_indices = set(protected_chunk_indices or set())
        protected_chunk_indices.update(keyword_chunk_indices)
        
        if precomputed_embeddings is not None and len(precomputed_embeddings) == len(chunks):
            embeddings = precomputed_embeddings
            logger.info("[MMR] 使用预计算的embedding向量，跳过实时计算")
        else:
            contents = [c.get("content", "")[:500] for c in chunks]
            try:
                embeddings = await self._get_embeddings(contents)
            except Exception as e:
                logger.warning(f"[MMR] 获取embedding失败: {e}，回退到原始排序")
                return chunks
        
        embeddings_array = np.array(embeddings, dtype=np.float32)
        norms = np.linalg.norm(embeddings_array, axis=1, keepdims=True)
        norms = np.where(norms == 0, 1, norms)
        normalized = embeddings_array / norms
        
        scores_list = [rerank_scores.get(i, 0.0) for i in range(len(chunks))]
        min_score, max_score = min(scores_list), max(scores_list)
        score_range = max_score - min_score if max_score > min_score else 1.0
        
        candidates = []
        for i, chunk in enumerate(chunks):
            norm_score = (scores_list[i] - min_score) / score_range
            candidates.append({
                'index': i,
                'chunk': chunk,
                'rel_score': norm_score,
                'vector': normalized[i],
                'original_score': scores_list[i]
            })
        
        # 先按相关性分数排序，保留前 top_n/2 个最相关的 chunk 不动
        # 多样性只影响剩余位置，确保正确答案不会被推到 Top-10 之外
        candidates.sort(key=lambda x: -x['rel_score'])
        guaranteed = int(top_n * 0.5)  # 保底一半位置给最相关的chunk
        guaranteed_candidates = candidates[:guaranteed]
        selected = list(guaranteed_candidates)
        candidates = candidates[guaranteed:]

        for cand in list(candidates):
            if cand['index'] in protected_chunk_indices and all(s['index'] != cand['index'] for s in selected):
                selected.append(cand)
                candidates.remove(cand)
        
        start_time = time.time()
        iterations = 0
        
        while len(selected) < min(top_n, len(candidates) + len(selected)):
            iterations += 1
            best_mmr = -float('inf')
            best_idx = -1
            
            selected_vectors = np.array([s['vector'] for s in selected])
            
            for i, cand in enumerate(candidates):
                sims = np.dot(selected_vectors, cand['vector'])
                max_sim = np.max(sims) if len(sims) > 0 else 0
                
                mmr = lambda_param * cand['rel_score'] - (1 - lambda_param) * max_sim
                
                if mmr > best_mmr:
                    best_mmr = mmr
                    best_idx = i
            
            if best_idx >= 0:
                selected.append(candidates.pop(best_idx))
        
        elapsed = time.time() - start_time
        score_type = "融合分数" if use_fused_score else "交叉注意力分数"
        logger.info(f"[MMR重排序] 使用{score_type}，迭代{iterations}次，耗时{elapsed*1000:.1f}ms，最终选择{len(selected)}个")

        # 关键词保护：确保包含 HL 关键词的 chunk 不被 MMR 丢弃
        if protected_chunk_indices:
            selected_indices = {s['index'] for s in selected}
            missing_kw_indices = protected_chunk_indices - selected_indices
            if missing_kw_indices:
                # 找回被丢弃的保护 chunk，替换掉末尾的非保护 chunk
                result_chunks = [s['chunk'] for s in selected]
                for kw_idx in missing_kw_indices:
                    # 从后往前找第一个非保护 chunk 替换
                    for j in range(len(result_chunks) - 1, -1, -1):
                        if selected[j]['index'] not in protected_chunk_indices:
                            result_chunks[j] = chunks[kw_idx]
                            selected[j] = {'index': kw_idx, 'chunk': chunks[kw_idx],
                                          'rel_score': 1.0, 'vector': normalized[kw_idx],
                                          'original_score': rerank_scores.get(kw_idx, 0)}
                            break
                logger.info(f"[MMR保护] 找回 {len(missing_kw_indices)} 个被丢弃的保护chunk")
                return result_chunks

        return [s['chunk'] for s in selected]
