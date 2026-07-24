"""
知识库级别的关键词提取器

"""

import asyncio
import contextvars
import re
import threading
import time
from collections import defaultdict
from concurrent.futures import ThreadPoolExecutor
from dataclasses import dataclass, field
from typing import Any, Callable, ClassVar, Dict, List, Optional, Tuple

import networkx as nx
import numpy as np

from backend.config.loggers import get_logger
from backend.rag.keywords.index_manager import KeywordIndexManager
from backend.rag.keywords.models import (
    ExtractionMode,
    KeywordExtractionResult,
    QueryType,
)
from backend.rag.keywords.per_kb_index import PerKBKeywordIndex

logger = get_logger(__name__)

_executor = ThreadPoolExecutor(max_workers=4, thread_name_prefix="keyword_")


def detect_query_type(query: str) -> QueryType:
    """轻量查询意图识别，可在多 KB 场景复用。"""
    query_lower = query.lower()

    param_indicators = ["参数", "值", "多少", "多大", "规格", "指标", "性能"]
    if any(ind in query for ind in param_indicators):
        return QueryType.PARAMETER

    equipment_keywords = ["探测器", "放大器", "分析器", "PMT", "SiPM", "ADC", "DAC", "FPGA"]
    if any(kw.lower() in query_lower for kw in equipment_keywords):
        return QueryType.EQUIPMENT

    comparison_indicators = ["比较", "对比", "区别", "差异", "优缺点", "哪个好"]
    if any(ind in query for ind in comparison_indicators):
        return QueryType.COMPARISON

    concept_indicators = ["什么是", "原理", "概念", "定义", "作用", "功能"]
    if any(ind in query for ind in concept_indicators):
        return QueryType.CONCEPT

    return QueryType.GENERAL


@dataclass
class PerKBKeywordExtractor:
    """
    知识库级别的关键词提取器
    
    替换原有的 HybridKeywordExtractor 全局单例
    """
    kb_code: str
    kb_id: str
    index_manager: KeywordIndexManager
    embedding_func: Optional[Callable] = None
    embedding_dim: int = 4096
    
    vector_top_k: int = 10
    bm25_top_k: int = 5
    rrf_k: int = 60
    min_score: float = 0.005
    low_level_similarity_threshold: float = 0.4
    
    _index: Optional[PerKBKeywordIndex] = None
    _graph: Optional[nx.Graph] = None
    _graph_entity_to_idx: Dict[str, int] = field(default_factory=dict)
    _graph_built: bool = False
    _graph_lock: asyncio.Lock = field(default_factory=asyncio.Lock)
    _global_graph_cache: ClassVar[Dict[str, Tuple[nx.Graph, Dict[str, int]]]] = {}
    _global_graph_locks: ClassVar[Dict[str, asyncio.Lock]] = {}
    _global_graph_locks_guard: ClassVar[threading.Lock] = threading.Lock()
    
    async def _get_index(self) -> PerKBKeywordIndex:
        """获取知识库索引"""
        if self._index is None:
            self._index = await self.index_manager.get_index(self.kb_code, self.kb_id)
        return self._index
    
    def _detect_query_type(self, query: str) -> QueryType:
        """检测查询类型"""
        return detect_query_type(query)
    
    async def _vector_search(
        self,
        query: str,
        top_k: int = 15,
        precomputed_query_embedding: Any | None = None,
    ) -> List[Tuple[str, float]]:
        """向量检索"""
        index = await self._get_index()
        self._index = index
        
        if not index.entity_names:
            logger.warning("[向量检索] 索引实体列表为空")
            return []
        
        if self.embedding_func is None:
            logger.warning("[向量检索] 未配置 embedding_func")
            return []
        
        try:
            logger.info(f"[向量检索] 开始检索, query='{query[:50]}...', top_k={top_k}")
            
            query_embedding = precomputed_query_embedding
            if query_embedding is None:
                query_embedding = self.embedding_func([query])

                if asyncio.iscoroutine(query_embedding):
                    query_embedding = await query_embedding

                if isinstance(query_embedding, np.ndarray):
                    query_embedding = query_embedding[0]
                else:
                    query_embedding = query_embedding[0]
            else:
                if isinstance(query_embedding, np.ndarray):
                    if query_embedding.ndim > 1:
                        query_embedding = query_embedding[0]
                elif isinstance(query_embedding, list) and query_embedding and isinstance(query_embedding[0], (list, np.ndarray)):
                    query_embedding = query_embedding[0]
            
            logger.info(f"[向量检索] embedding 维度: {len(query_embedding)}")
            
            results = await index.search_vector(np.array(query_embedding), top_k)
            
            logger.info(f"[向量检索] 返回结果数: {len(results)}, 前3个: {results[:3]}")
            
            return results
            
        except Exception as e:
            logger.warning(f"[向量检索] 失败: {e}", exc_info=True)
            return []
    
    def _bm25_search(self, query: str, top_k: int = 15) -> List[Tuple[str, float]]:
        """BM25 检索"""
        index = self._index
        
        if index is None:
            logger.warning("[BM25检索] 索引未初始化")
            return []
        
        if index.bm25_index is None:
            logger.warning("[BM25检索] BM25索引未构建")
            return []
        
        try:
            logger.info(f"[BM25检索] 开始检索, query='{query[:50]}...', top_k={top_k}")
            
            results = index.bm25_index.search(query, top_k * 5)
            
            logger.info(f"[BM25检索] 原始结果数: {len(results)}")
            
            all_results: Dict[str, Tuple[str, float]] = {}
            
            for doc_idx, score in results:
                if doc_idx < len(index.entity_names):
                    entity_name = index.entity_names[doc_idx]
                    
                    base_name = entity_name.lower().strip()
                    base_name = re.sub(r'[\s\-_]+', '', base_name)
                    
                    if base_name not in all_results:
                        all_results[base_name] = (entity_name, score)
                    else:
                        existing_name, existing_score = all_results[base_name]
                        # 同一归一化名称冲突时，优先保留高分；同分再选更短名称。
                        if score > existing_score or (
                            score == existing_score and len(entity_name) < len(existing_name)
                        ):
                            all_results[base_name] = (entity_name, score)
            
            output = sorted(all_results.values(), key=lambda x: -x[1])[:top_k]
            
            logger.info(f"[BM25检索] 去重后结果数: {len(output)}, 前3个: {output[:3]}")
            
            return output
            
        except Exception as e:
            logger.warning(f"[BM25检索] 失败: {e}", exc_info=True)
            return []
    
    def _rrf_fusion(
        self,
        vector_results: List[Tuple[str, float]],
        bm25_results: List[Tuple[str, float]],
        top_n: int = 7,
        vector_weight: float = 0.6,
        bm25_weight: float = 0.4,
    ) -> List[Tuple[str, float]]:
        """RRF 融合策略"""
        logger.info(f"[RRF融合] 向量结果数: {len(vector_results)}, BM25结果数: {len(bm25_results)}, 权重: 向量={vector_weight}, BM25={bm25_weight}")
        
        rrf_scores: Dict[str, float] = {}
        
        for rank, (entity_name, _) in enumerate(vector_results):
            if entity_name not in rrf_scores:
                rrf_scores[entity_name] = 0.0
            rrf_scores[entity_name] += vector_weight / (self.rrf_k + rank + 1)
        
        for rank, (entity_name, _) in enumerate(bm25_results):
            if entity_name not in rrf_scores:
                rrf_scores[entity_name] = 0.0
            rrf_scores[entity_name] += bm25_weight / (self.rrf_k + rank + 1)
        
        sorted_results = sorted(rrf_scores.items(), key=lambda x: -x[1])[:top_n]
        
        logger.info(f"[RRF融合] 融合后结果数: {len(sorted_results)}, 前5个: {sorted_results[:5]}")
        
        return sorted_results

    def _merge_without_rrf(
        self,
        vector_results: List[Tuple[str, float]],
        bm25_results: List[Tuple[str, float]],
        top_n: int = 15,
        vector_weight: float = 0.7,
        bm25_weight: float = 0.3,
    ) -> List[Tuple[str, float]]:
        """按通道内归一化分数做加权融合，不使用 RRF。"""

        def _normalize(results: List[Tuple[str, float]]) -> Dict[str, float]:
            if not results:
                return {}
            scores = [float(s) for _, s in results]
            max_score = max(scores)
            min_score = min(scores)
            if max_score <= min_score:
                return {name: 1.0 for name, _ in results}
            scale = max_score - min_score
            return {name: (float(score) - min_score) / scale for name, score in results}

        vector_norm = _normalize(vector_results)
        bm25_norm = _normalize(bm25_results)

        merged: Dict[str, float] = {}
        for name, score in vector_norm.items():
            merged[name] = merged.get(name, 0.0) + vector_weight * score
        for name, score in bm25_norm.items():
            merged[name] = merged.get(name, 0.0) + bm25_weight * score

        sorted_results = sorted(merged.items(), key=lambda x: -x[1])[:top_n]
        logger.info(
            "[非RRF融合] 向量=%s, BM25=%s, 融合后=%s",
            len(vector_results),
            len(bm25_results),
            len(sorted_results),
        )
        return sorted_results
    
    async def _build_full_graph(self) -> None:
        """构建全局实体关系图 - 非阻塞版本"""
        # 进程内按 kb 共享锁和共享缓存，避免多实例并发重复建图
        with self._global_graph_locks_guard:
            kb_lock = self._global_graph_locks.get(self.kb_id)
            if kb_lock is None:
                kb_lock = asyncio.Lock()
                self._global_graph_locks[self.kb_id] = kb_lock

        async with kb_lock:
            cached = self._global_graph_cache.get(self.kb_id)
            if cached is not None:
                self._graph, self._graph_entity_to_idx = cached
                self._graph_built = True
                logger.info(
                    f"[图构建] 命中进程内图缓存: kb={self.kb_code}, "
                    f"节点数={self._graph.number_of_nodes()}, 边数={self._graph.number_of_edges()}"
                )
                return

            if self._graph_built and self._graph is not None:
                return
            
            start_time = time.time()
            logger.info(f"[图构建] 开始构建知识库 {self.kb_code} 实体关系图...")
            
            try:
                index = await self._get_index()
                
                loop = asyncio.get_running_loop()
                context = contextvars.copy_context()
                result = await loop.run_in_executor(
                    _executor,
                    context.run,
                    self._sync_build_graph,
                    index.entity_names,
                    index.entity_metadata,
                )
                
                self._graph = result["graph"]
                self._graph_entity_to_idx = result["entity_to_idx"]
                self._global_graph_cache[self.kb_id] = (self._graph, self._graph_entity_to_idx)
                
                self._graph_built = True
                elapsed = (time.time() - start_time) * 1000
                logger.info(
                    f"[图构建] 完成: kb={self.kb_code}, 节点数={self._graph.number_of_nodes()}, "
                    f"边数={self._graph.number_of_edges()}, 耗时={elapsed:.1f}ms"
                )
                
            except Exception as e:
                logger.error(f"[图构建] 失败: {e}")
                self._graph = None
                self._graph_built = False
    
    def _sync_build_graph(
        self, 
        entity_names: List[str], 
        entity_metadata: Dict[str, Dict]
    ) -> Dict[str, Any]:
        """同步构建图 - 在线程池中执行"""
        graph = nx.Graph()
        entity_to_idx = {}
        
        for i, entity_name in enumerate(entity_names):
            if entity_name and entity_name not in entity_to_idx:
                entity_to_idx[entity_name] = i
                metadata = entity_metadata.get(entity_name, {})
                graph.add_node(entity_name, **metadata)
        
        source_to_entities: Dict[str, List[str]] = defaultdict(list)
        
        for entity_name, metadata in entity_metadata.items():
            source_id = metadata.get("source_id", "")
            if source_id:
                for chunk_id in source_id.split(","):
                    chunk_id = chunk_id.strip()
                    if chunk_id:
                        source_to_entities[chunk_id].append(entity_name)
        
        edges_added = 0
        for entities in source_to_entities.values():
            if len(entities) > 1:
                for i, src in enumerate(entities):
                    for tgt in entities[i + 1:]:
                        if not graph.has_edge(src, tgt):
                            graph.add_edge(src, tgt, weight=1.0)
                            edges_added += 1
        
        logger.info(f"[图构建] 基于共现构建了 {edges_added} 条边")
        
        return {"graph": graph, "entity_to_idx": entity_to_idx}

    async def _embed_texts(self, texts: List[str]) -> Optional[List[Any]]:
        """批量获取 embedding，兼容同步/异步 embedding_func。"""
        if not texts or self.embedding_func is None:
            return None

        try:
            embeddings = self.embedding_func(texts)
            if asyncio.iscoroutine(embeddings):
                embeddings = await embeddings

            if isinstance(embeddings, np.ndarray):
                if embeddings.ndim == 1:
                    return [embeddings]
                return list(embeddings)

            if embeddings is None:
                return None

            return list(embeddings)
        except Exception as e:
            logger.warning(f"[低级关键词] embedding 计算失败: {e}")
            return None

    @staticmethod
    async def get_weighted_seeds(
        self,
        query: str,
        seed_entities: List[str],
        floor: float = 0.3,
    ) -> Dict[str, float]:
        """
        计算加权种子分布（用于 PPR personalization）。

        策略：
        1. 计算 query 与每个 seed 的余弦相似度
        2. 减去下限 floor 后做 ReLU
        3. L1 归一化得到概率分布
        4. 若全部退化（<=0）则回退为均匀分布
        """
        if not seed_entities:
            return {}

        if self.embedding_func is None:
            uniform = 1.0 / len(seed_entities)
            return {seed: uniform for seed in seed_entities}

        index = await self._get_index()
        texts = [query]
        for seed in seed_entities:
            metadata = index.entity_metadata.get(seed, {}) if index else {}
            description = str(metadata.get("description", "") or "")[:80]
            texts.append(f"{seed}: {description}" if description else seed)

        embeddings = await self._embed_texts(texts)
        if embeddings is None or len(embeddings) != len(seed_entities) + 1:
            uniform = 1.0 / len(seed_entities)
            return {seed: uniform for seed in seed_entities}

        query_emb = np.array(embeddings[0], dtype=np.float32)
        seed_embs = np.array(embeddings[1:], dtype=np.float32)

        query_norm = np.linalg.norm(query_emb)
        seed_norms = np.linalg.norm(seed_embs, axis=1)
        valid_mask = (seed_norms > 0) & (query_norm > 0)

        cosine_scores = np.zeros(len(seed_entities), dtype=np.float32)
        if np.any(valid_mask):
            cosine_scores[valid_mask] = (
                np.dot(seed_embs[valid_mask], query_emb)
                / (seed_norms[valid_mask] * query_norm)
            )

        shifted = np.maximum(cosine_scores - float(floor), 0.0)
        total = float(np.sum(shifted))

        if total <= 1e-12:
            uniform = 1.0 / len(seed_entities)
            logger.info("[加权种子] 全部低于floor=%.3f，回退均匀分布", floor)
            return {seed: uniform for seed in seed_entities}

        normalized = shifted / total
        weighted = {
            seed_entities[i]: float(normalized[i])
            for i in range(len(seed_entities))
            if float(normalized[i]) > 0
        }
        if not weighted:
            uniform = 1.0 / len(seed_entities)
            return {seed: uniform for seed in seed_entities}

        top3 = sorted(weighted.items(), key=lambda x: -x[1])[:3]
        logger.info("[加权种子] seeds=%s, floor=%.3f, top3=%s", len(seed_entities), floor, top3)
        return weighted

    @staticmethod
    def _is_low_level_noise(keyword: str) -> bool:
        """规则化噪声过滤：人名、刊名、文集等。"""
        if not keyword:
            return True

        kw = keyword.strip()
        if len(kw) <= 1:
            return True

        # 典型文献与机构类噪声
        noise_patterns = [
            r"(学报|期刊|杂志|文集|论文集|出版社|报|年鉴|标准)$",
            r"(大学|学院|研究院|研究所|实验室)$",
            r"(省|市|县|自治区|地区|中国|广西|北京|上海|广州)$",
            r"^(第?\d+[卷期章节])$",
        ]

        for pattern in noise_patterns:
            if re.search(pattern, kw, re.IGNORECASE):
                return True

        # 仅在“疑似中文姓名”场景过滤，避免误杀大量 2-4 字术语。
        common_surnames = {
            "赵", "钱", "孙", "李", "周", "吴", "郑", "王", "冯", "陈", "褚", "卫", "蒋", "沈", "韩", "杨",
            "朱", "秦", "尤", "许", "何", "吕", "施", "张", "孔", "曹", "严", "华", "金", "魏", "陶", "姜",
            "戚", "谢", "邹", "喻", "柏", "水", "窦", "章", "云", "苏", "潘", "葛", "奚", "范", "彭", "郎",
            "鲁", "韦", "昌", "马", "苗", "凤", "花", "方", "俞", "任", "袁", "柳", "酆", "鲍", "史", "唐",
            "费", "廉", "岑", "薛", "雷", "贺", "倪", "汤", "滕", "殷", "罗", "毕", "郝", "邬", "安", "常",
            "乐", "于", "时", "傅", "皮", "卞", "齐", "康", "伍", "余", "元", "卜", "顾", "孟", "平", "黄",
            "和", "穆", "萧", "尹", "姚", "邵", "湛", "汪", "祁", "毛", "禹", "狄", "米", "贝", "明", "臧",
            "计", "伏", "成", "戴", "谈", "宋", "茅", "庞", "熊", "纪", "舒", "屈", "项", "祝", "董", "梁",
            "杜", "阮", "蓝", "闵", "席", "季", "麻", "强", "贾", "路", "娄", "危", "江", "童", "颜", "郭",
            "梅", "盛", "林", "刁", "钟", "徐", "邱", "骆", "高", "夏", "蔡", "田", "樊", "胡", "凌", "霍",
            "虞", "万", "支", "柯", "昝", "管", "卢", "莫", "经", "房", "裘", "缪", "干", "解", "应", "宗",
            "丁", "宣", "贲", "邓", "郁", "单", "杭", "洪", "包", "诸", "左", "石", "崔", "吉", "钮", "龚",
            "程", "嵇", "邢", "滑", "裴", "陆", "荣", "翁", "荀", "羊", "於", "惠", "甄", "曲", "家", "封",
            "芮", "羿", "储", "靳", "汲", "邴", "糜", "松", "井", "段", "富", "巫", "乌", "焦", "巴", "弓",
            "牧", "隗", "山", "谷", "车", "侯", "宓", "蓬", "全", "郗", "班", "仰", "秋", "仲", "伊", "宫",
            "宁", "仇", "栾", "暴", "甘", "斜", "厉", "戎", "祖", "武", "符", "刘", "景", "詹", "束", "龙"
        }
        if re.fullmatch(r"[\u4e00-\u9fa5]{2,3}", kw) and kw[0] in common_surnames:
            return True

        return False
    
    async def extract_low_level_from_graph(
        self,
        query: str,
        high_level_keywords: List[str],
        top_k: int = 7,
        max_depth: int = 2,
        min_degree: int = 1,
        max_degree: int = 100,
    ) -> List[str]:
        """从图拓扑中提取低级关键词。

        流程：
        1. 从高级关键词做 1-2 跳邻居扩展
        2. 正则规则过滤噪声候选
        3. 使用 embedding 与 query 计算相关性，过滤相似度 < 0.4
        4. 使用 1/dist * log(degree) * similarity_score 重排序
        """
        if not high_level_keywords:
            return []
        
        if not self._graph_built or self._graph is None:
            await self._build_full_graph()
        
        if self._graph is None or self._graph.number_of_nodes() == 0:
            logger.warning("[图提取] 图未构建或为空")
            return []
        
        candidates: Dict[str, Dict[str, float]] = {}
        seed_set = set(high_level_keywords)
        
        for seed in high_level_keywords:
            if seed not in self._graph:
                continue
            
            visited = {seed}
            queue = [(seed, 0)]
            
            while queue:
                node, depth = queue.pop(0)
                
                if depth >= max_depth:
                    continue
                
                for neighbor in self._graph.neighbors(node):
                    if neighbor in visited:
                        continue
                    visited.add(neighbor)
                    
                    if neighbor in seed_set:
                        queue.append((neighbor, depth + 1))
                        continue
                    
                    queue.append((neighbor, depth + 1))
                    
                    if depth + 1 <= max_depth:
                        deg = self._graph.degree(neighbor)

                        if deg < min_degree or deg > max_degree:
                            continue

                        if self._is_low_level_noise(neighbor):
                            continue

                        dist = float(depth + 1)
                        if neighbor not in candidates:
                            candidates[neighbor] = {"dist": dist, "degree": float(deg)}
                        else:
                            candidates[neighbor]["dist"] = min(candidates[neighbor]["dist"], dist)
                            candidates[neighbor]["degree"] = max(candidates[neighbor]["degree"], float(deg))

        if not candidates:
            logger.info("[图提取] 无候选低级关键词")
            return []

        candidate_terms = list(candidates.keys())
        embeddings = await self._embed_texts([query] + candidate_terms)
        if embeddings is None or len(embeddings) != len(candidate_terms) + 1:
            logger.warning("[图提取] 无法完成相关性校准，返回空低级关键词")
            return []

        query_emb = np.array(embeddings[0], dtype=np.float32)
        candidate_embs = np.array(embeddings[1:], dtype=np.float32)
        
        query_norm = np.linalg.norm(query_emb)
        candidate_norms = np.linalg.norm(candidate_embs, axis=1)
        
        valid_mask = (candidate_norms > 0) & (query_norm > 0)
        similarity_scores = np.zeros(len(candidate_terms), dtype=np.float32)
        
        if np.any(valid_mask):
            similarity_scores[valid_mask] = np.dot(candidate_embs[valid_mask], query_emb) / (candidate_norms[valid_mask] * query_norm)
        
        similarity_scores = np.clip(similarity_scores, 0.0, 1.0)
        
        passed_mask = similarity_scores >= self.low_level_similarity_threshold
        filtered_by_similarity = int(np.sum(~passed_mask))
        
        scored_candidates: List[Tuple[str, float]] = []
        passed_indices = np.where(passed_mask)[0]
        
        for idx in passed_indices:
            term = candidate_terms[idx]
            similarity_score = float(similarity_scores[idx])
            dist = max(candidates[term]["dist"], 1.0)
            degree = max(candidates[term]["degree"], 2.0)
            final_score = (1.0 / dist) * float(np.log(degree)) * similarity_score
            scored_candidates.append((term, final_score))

        scored_candidates.sort(key=lambda x: -x[1])
        low_level = [kw for kw, _ in scored_candidates[:top_k]]

        logger.info(
            f"[图提取] 候选={len(candidate_terms)}, 相似度过滤={filtered_by_similarity}, "
            f"阈值={self.low_level_similarity_threshold}, 最终低级关键词={len(low_level)}"
        )
        return low_level
    
    def _extract_low_level_fallback(self, query: str) -> List[str]:
        """后备方案：基于正则的低级关键词提取"""
        low_level = []
        
        energy_pattern = r'\d+(?:\.\d+)?\s*(?:keV|MeV|GeV|kev|mev|gev)'
        energy_matches = re.findall(energy_pattern, query, re.IGNORECASE)
        low_level.extend(energy_matches)
        
        voltage_pattern = r'\d+(?:\.\d+)?\s*(?:V|mV|kV)'
        voltage_matches = re.findall(voltage_pattern, query, re.IGNORECASE)
        low_level.extend(voltage_matches)
        
        time_pattern = r'\d+(?:\.\d+)?\s*(?:ns|μs|ms|s)'
        time_matches = re.findall(time_pattern, query, re.IGNORECASE)
        low_level.extend(time_matches)
        
        param_keywords = [
            "分辨率", "增益", "带宽", "噪声", "效率", "阈值", "电压", "电流",
            "温度", "压力", "速度", "时间", "能量", "功率", "频率"
        ]
        for kw in param_keywords:
            if kw in query:
                low_level.append(kw)
        
        return list(set(low_level))[:7]

    async def _refine_high_level_keywords_with_chat_model(
        self,
        query: str,
        candidates: List[str],
    ) -> List[str]:
        """若 config.yaml 配置了 ``keyword_extraction_chat_model``，用该 chat 模型精炼高级关键词。"""
        if not candidates:
            return []

        try:
            from backend.core.config.app_config import get_app_config
            from backend.core.models import create_chat_model
        except ImportError:
            return candidates

        cfg = get_app_config()
        model_name = (getattr(cfg, "keyword_extraction_chat_model", None) or "").strip()
        if not model_name:
            return candidates
        if cfg.get_model_config(model_name) is None:
            logger.warning(
                "[关键词LLM] keyword_extraction_chat_model=%s 不在 config.yaml 的 models 列表中，跳过",
                model_name,
            )
            return candidates

        lines_in = "\n".join(f"- {c}" for c in candidates[:25])
        prompt = (
            f"用户检索问题：{query}\n\n"
            "下列候选实体名来自知识库索引（已粗排）。请从中挑选最多10个与问题最直接相关、"
            "适合作为图检索种子的条目；删去明显无关的噪声（如纯期刊名、资助编号、泛化词）。\n"
            "只输出保留的实体名，每行一个，不要前缀、编号或解释。\n\n"
            f"{lines_in}"
        )
        try:
            model = create_chat_model(name=model_name, thinking_enabled=False)
            resp = await asyncio.wait_for(model.ainvoke(prompt), timeout=25.0)
            raw = getattr(resp, "content", resp)
            if isinstance(raw, list):
                parts: List[str] = []
                for x in raw:
                    if isinstance(x, dict) and isinstance(x.get("text"), str):
                        parts.append(x["text"])
                    else:
                        parts.append(str(x))
                raw = "".join(parts)
            text = str(raw or "").strip()
        except asyncio.TimeoutError:
            logger.warning("[关键词LLM] 超时，保留向量+BM25 候选")
            return candidates
        except Exception as e:
            logger.warning("[关键词LLM] 调用失败: %s，保留向量+BM25 候选", e)
            return candidates

        cand_map = {c.lower(): c for c in candidates}
        picked: List[str] = []
        for line in text.splitlines():
            name = line.strip()
            name = name.lstrip("-*• ").strip()
            for prefix in ("1.", "2.", "3.", "4.", "5.", "6.", "7.", "8.", "9.", "10."):
                if name.startswith(prefix):
                    name = name[len(prefix) :].strip()
                    break
            if not name:
                continue
            key = name.lower()
            if key in cand_map:
                canon = cand_map[key]
                if canon not in picked:
                    picked.append(canon)

        if not picked:
            logger.info("[关键词LLM] 输出未匹配任何候选，保留算法结果")
            return candidates

        logger.info(
            "[关键词LLM] model=%s 精炼候选 %d -> %d",
            model_name,
            len(candidates),
            len(picked[:10]),
        )
        return picked[:10]

    async def extract(
        self,
        query: str,
        extraction_mode: ExtractionMode = ExtractionMode.HYBRID,
        external_query_type: QueryType | str | None = None,
        precomputed_query_embedding: Any | None = None,
        enable_rrf: bool = True,
    ) -> KeywordExtractionResult:
        """
        提取关键词
        
        主入口方法 - 完全匹配旧的 HybridKeywordExtractor 逻辑
        
        流程：
        1. 检测查询类型
        2. 向量+BM25 获取高级关键词
        3. 基于图拓扑提取低级关键词
        4. 如果图提取结果为空，降级到正则后备方案
        """
        start_time = time.time()
        
        index = await self._get_index()
        self._index = index  # 确保 self._index 被设置
        
        result = KeywordExtractionResult()
        result.kb_code = self.kb_code
        result.kb_id = self.kb_id
        result.extraction_mode = extraction_mode

        if isinstance(external_query_type, QueryType):
            query_type = external_query_type
        elif isinstance(external_query_type, str):
            normalized = external_query_type.strip().lower()
            try:
                query_type = QueryType(normalized)
            except Exception:
                query_type = self._detect_query_type(query)
        else:
            query_type = self._detect_query_type(query)

        result.query_type = query_type
        logger.info(f"[意图识别] 检测到查询类型: {result.query_type}")
        
        if not index.entity_names:
            logger.warning(f"[关键词提取] 知识库 {self.kb_code} 索引未初始化，返回空结果")
            result.low_level_keywords = self._extract_low_level_fallback(query)
            result.extraction_time_ms = (time.time() - start_time) * 1000
            return result
        
        run_vector = extraction_mode in (ExtractionMode.VECTOR, ExtractionMode.HYBRID, ExtractionMode.GRAPH)
        run_bm25 = extraction_mode in (ExtractionMode.BM25, ExtractionMode.HYBRID, ExtractionMode.GRAPH)

        vector_results: List[Tuple[str, float]] = []
        bm25_results: List[Tuple[str, float]] = []

        if run_vector and run_bm25:
            loop = asyncio.get_running_loop()
            vector_task = asyncio.create_task(
                self._vector_search(
                    query,
                    self.vector_top_k,
                    precomputed_query_embedding=precomputed_query_embedding,
                )
            )
            context = contextvars.copy_context()
            bm25_task = loop.run_in_executor(
                _executor,
                context.run,
                self._bm25_search,
                query,
                self.bm25_top_k,
            )
            vector_results, bm25_results = await asyncio.gather(vector_task, bm25_task)
        elif run_vector:
            vector_results = await self._vector_search(
                query,
                self.vector_top_k,
                precomputed_query_embedding=precomputed_query_embedding,
            )
        elif run_bm25:
            loop = asyncio.get_running_loop()
            context = contextvars.copy_context()
            bm25_results = await loop.run_in_executor(
                _executor,
                context.run,
                self._bm25_search,
                query,
                self.bm25_top_k,
            )

        if run_vector and run_bm25:
            if enable_rrf:
                fused_results = self._rrf_fusion(
                    vector_results,
                    bm25_results,
                    top_n=15,
                    vector_weight=0.7,
                    bm25_weight=0.3,
                )
            else:
                fused_results = self._merge_without_rrf(
                    vector_results,
                    bm25_results,
                    top_n=15,
                    vector_weight=0.7,
                    bm25_weight=0.3,
                )
        elif run_vector:
            fused_results = vector_results[:15]
        elif run_bm25:
            fused_results = bm25_results[:15]
        else:
            fused_results = []
        
        # 当只有 BM25 结果时，降低分数阈值
        effective_min_score = self.min_score
        if not vector_results and bm25_results:
            effective_min_score = 0.001  # 降低阈值
        
        result.high_level_keywords = [entity_name for entity_name, score in fused_results if score > effective_min_score][:10]
        result.entity_scores = {entity_name: score for entity_name, score in fused_results if score > effective_min_score}
        result.high_level_keywords = await self._refine_high_level_keywords_with_chat_model(
            query, result.high_level_keywords
        )
        if result.high_level_keywords:
            result.entity_scores = {
                k: v for k, v in result.entity_scores.items() if k in result.high_level_keywords
            }
        result.weighted_seed_scores = await self.get_weighted_seeds(
            query=query,
            seed_entities=result.high_level_keywords,
        )
        
        if result.high_level_keywords:
            low_level = await self.extract_low_level_from_graph(query, result.high_level_keywords)
            
            if not low_level:
                low_level = self._extract_low_level_fallback(query)
            
            result.low_level_keywords = low_level
        else:
            result.low_level_keywords = self._extract_low_level_fallback(query)
        
        result.extraction_time_ms = (time.time() - start_time) * 1000
        
        logger.info(
            f"[关键词提取] 耗时={result.extraction_time_ms:.1f}ms, "
            f"类型={query_type.value}, "
            f"高级={len(result.high_level_keywords)}个, "
            f"低级={len(result.low_level_keywords)}个"
        )
        
        return result
