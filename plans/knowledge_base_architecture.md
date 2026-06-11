# 知识库架构设计文档

> **文档状态**：📦 **早期架构稿**（多库改造前后期混合内容）。  
> **现行说明**：[docs/知识库算法和架构.md](../docs/知识库算法和架构.md) · [docs/docs_backend/rag_module.md](../docs/docs_backend/rag_module.md)  
> **注意**：下文「PPR 图过滤」为算法能力描述；**生产垂直库 Profile 默认 Stage-2 为 MMR_ONLY（跳过 PPR）**（2026-05-22 起）。

## 1. 概述

本文档描述了多知识库 RAG（Retrieval-Augmented Generation）系统的整体架构设计、核心算法实现以及多知识库管理机制。

### 1.1 系统目标

- **多知识库支持**：支持创建、管理多个独立的知识库，实现物理隔离
- **高效检索**：结合关键词检索、向量检索、图检索等多种检索策略
- **智能过滤**：使用 PPR 图过滤和 MMR 重排序优化检索结果
- **多模态支持**：支持文本、图片、表格、公式等多种内容类型的处理
- **可扩展性**：模块化设计，便于扩展新的存储后端和检索算法

### 1.2 技术栈

| 组件 | 技术选型 |
|------|----------|
| 图数据库 | NebulaGraph |
| 向量数据库 | Milvus |
| 缓存 | Redis |
| 关系数据库 | MySQL |
| 对象存储 | MinIO |
| 核心框架 | LightRAG |

---

## 2. 整体架构

### 2.1 架构图

```
┌─────────────────────────────────────────────────────────────────────────────┐
│                              API Layer                                       │
│  ┌─────────────────┐  ┌─────────────────┐  ┌─────────────────┐             │
│  │   RAG Router    │  │  KB Router      │  │  Upload Router  │             │
│  └────────┬────────┘  └────────┬────────┘  └────────┬────────┘             │
└───────────┼─────────────────────┼─────────────────────┼─────────────────────┘
            │                     │                     │
┌───────────┼─────────────────────┼─────────────────────┼─────────────────────┐
│           ▼                     ▼                     ▼                     │
│  ┌─────────────────────────────────────────────────────────────────────┐   │
│  │                      RAG Service (service.py)                        │   │
│  │  ┌─────────────┐  ┌─────────────┐  ┌─────────────┐                  │   │
│  │  │ RAGAnything │  │  MDIngestor │  │  LightRAG   │                  │   │
│  │  └─────────────┘  └─────────────┘  └─────────────┘                  │   │
│  └─────────────────────────────────────────────────────────────────────┘   │
│                              Service Layer                                  │
├─────────────────────────────────────────────────────────────────────────────┤
│  ┌─────────────────────────────────────────────────────────────────────┐   │
│  │                Knowledge Base Manager (manager.py)                   │   │
│  │  ┌───────────────┐  ┌───────────────┐  ┌───────────────┐            │   │
│  │  │ KB CRUD       │  │ RAG Instance  │  │ KB Statistics │            │   │
│  │  │ Management    │  │ Cache         │  │ Management    │            │   │
│  │  └───────────────┘  └───────────────┘  └───────────────┘            │   │
│  └─────────────────────────────────────────────────────────────────────┘   │
│                          Knowledge Base Layer                               │
├─────────────────────────────────────────────────────────────────────────────┤
│  ┌──────────────────┐  ┌──────────────────┐  ┌──────────────────┐          │
│  │ Keyword Index    │  │ Content Filter   │  │ Index Sync       │          │
│  │ Manager          │  │ Manager          │  │ Manager          │          │
│  │ (index_manager)  │  │ (filter_manager) │  │ (sync_manager)   │          │
│  └────────┬─────────┘  └────────┬─────────┘  └────────┬─────────┘          │
│           │                     │                     │                     │
│           ▼                     ▼                     ▼                     │
│  ┌──────────────────┐  ┌──────────────────┐  ┌──────────────────┐          │
│  │ PerKBKeyword     │  │ PerKBContent     │  │ PPR Graph        │          │
│  │ Index            │  │ Filter           │  │ Filter           │          │
│  └──────────────────┘  └──────────────────┘  └──────────────────┘          │
│                          Processing Layer                                   │
├─────────────────────────────────────────────────────────────────────────────┤
│  ┌──────────────────┐  ┌──────────────────┐  ┌──────────────────┐          │
│  │ NebulaGraph      │  │ Milvus           │  │ Redis            │          │
│  │ Storage          │  │ Vector Store     │  │ Cache            │          │
│  └──────────────────┘  └──────────────────┘  └──────────────────┘          │
│                          Storage Layer                                      │
└─────────────────────────────────────────────────────────────────────────────┘
```

### 2.2 模块职责

| 模块 | 文件 | 职责 |
|------|------|------|
| RAG Service | `service.py` | RAG 服务入口，文档入库、查询、多模态处理 |
| KB Manager | `knowledge_base/manager.py` | 知识库生命周期管理、RAG 实例缓存 |
| Keyword Index | `keywords/` | 关键词索引构建、BM25 检索、向量检索 |
| Content Filter | `content_filter/` | PPR 图过滤、MMR 重排序、内容过滤 |
| Index Sync | `sync/` | 索引增量同步、定时同步调度 |
| Storage | `storages/` | NebulaGraph 图存储适配 |
| RAGAnything | `raganything/` | 多模态文档处理、LightRAG 封装 |

---

## 3. 多知识库设计

### 3.1 物理隔离策略

每个知识库在存储层面实现完全物理隔离：

```
知识库 A (kb_code: "kb_a")
├── NebulaGraph Space: "kb_a"
├── Milvus Collection: "rag_kb_a_*"
└── Redis Key Prefix: "kb_a:*"

知识库 B (kb_code: "kb_b")
├── NebulaGraph Space: "kb_b"
├── Milvus Collection: "rag_kb_b_*"
└── Redis Key Prefix: "kb_b:*"
```

### 3.2 知识库数据模型

```python
class KnowledgeBase(Base):
    kb_id: str              # 知识库唯一 ID
    kb_code: str            # 知识库编码（用于存储命名）
    kb_name: str            # 知识库名称
    subject: str            # 学科分类
    description: str        # 描述
    owner_id: str           # 所有者 ID
    
    # 存储映射
    nebula_space: str       # NebulaGraph Space 名称
    milvus_collection_prefix: str  # Milvus Collection 前缀
    redis_key_prefix: str   # Redis Key 前缀
    
    # 统计信息
    doc_count: int          # 文档数量
    entity_count: int       # 实体数量
    relation_count: int     # 关系数量
    chunk_count: int        # 分块数量
    
    # 状态
    status: Enum            # creating/active/inactive/deleting
    is_default: bool        # 是否为默认知识库
```

### 3.3 RAG 实例缓存

```python
class KnowledgeBaseManager:
    def __init__(self):
        self._rag_instances: Dict[str, RAGInstance] = {}
        self._max_cached_instances = 10
        self._lock = asyncio.Lock()
    
    async def get_rag_instance(self, kb_id: str) -> RAGAnything:
        """获取或创建 RAG 实例（带 LRU 缓存）"""
        if kb_id in self._rag_instances:
            return self._rag_instances[kb_id].rag
        
        if len(self._rag_instances) >= self._max_cached_instances:
            await self._evict_lru_instance()
        
        rag = await self._create_rag_instance(kb_id)
        self._rag_instances[kb_id] = RAGInstance(kb_id=kb_id, rag=rag)
        return rag
```

### 3.4 知识库创建流程

```
1. 验证 kb_code 唯一性
2. 创建 MySQL 记录（状态：creating）
3. 创建 NebulaGraph Space
   - CREATE SPACE IF NOT EXISTS `{kb_code}` (vid_type=FIXED_STRING(256))
   - 创建 Tag: entity, chunk
   - 创建 Edge Type: relationship, contains
4. 初始化 Milvus Collection
5. 更新 MySQL 记录（状态：active）
```

---

## 4. 核心算法实现

### 4.1 关键词索引系统

#### 4.1.1 索引结构

```python
class PerKBKeywordIndex:
    kb_code: str                    # 知识库编码
    entity_names: List[str]         # 实体名称列表
    entity_metadata: Dict[str, Dict] # 实体元数据
    bm25_index: BM25Index           # BM25 倒排索引
    
    # 存储后端
    _milvus_collection: Collection  # Milvus 向量索引
    _redis_client: Redis            # Redis 缓存
```

#### 4.1.2 混合检索策略

```python
async def hybrid_search(query: str, top_k: int = 15) -> List[Tuple[str, float]]:
    """
    混合检索：向量检索 + BM25 检索 + RRF 融合
    """
    # 1. 向量检索
    query_embedding = await embedding_func([query])
    vector_results = await milvus_search(query_embedding, top_k=vector_top_k)
    
    # 2. BM25 检索
    bm25_results = bm25_index.search(query, top_k=bm25_top_k)
    
    # 3. RRF (Reciprocal Rank Fusion) 融合
    rrf_scores = defaultdict(float)
    for rank, (doc, _) in enumerate(vector_results):
        rrf_scores[doc] += 1 / (rrf_k + rank + 1)
    for rank, (doc, _) in enumerate(bm25_results):
        rrf_scores[doc] += 1 / (rrf_k + rank + 1)
    
    return sorted(rrf_scores.items(), key=lambda x: x[1], reverse=True)[:top_k]
```

#### 4.1.3 关键词提取

##### 高级关键词与低级关键词概念

| 类型 | 定义 | 来源 | 用途 |
|------|------|------|------|
| **高级关键词** | 与查询语义相关的核心实体/概念 | 向量检索 + BM25 检索 + RRF 融合 | 用于图检索的种子节点，定位知识图谱中的核心实体 |
| **低级关键词** | 与高级关键词相关的细节信息 | 图拓扑扩展 / 正则提取 | 补充具体参数值、数值、属性等细节信息 |

**高级关键词示例**：
- 查询："SiPM 探测器的工作原理是什么？"
- 高级关键词：`["SiPM", "探测器", "工作原理", "光子探测", "雪崩二极管"]`
- 特点：代表知识库中的实体节点，用于在图中定位相关区域

**低级关键词示例**：
- 查询："SiPM 的工作电压是多少？"
- 高级关键词：`["SiPM", "工作电压"]`
- 低级关键词：`["电压", "偏压", "30V", "增益", "击穿电压"]`
- 特点：包含具体数值、参数名称等细节信息

##### 提取流程

```python
class PerKBKeywordExtractor:
    async def extract(self, query: str) -> KeywordExtractionResult:
        """
        多策略关键词提取
        
        流程：
        1. 检测查询类型（参数/概念/设备/比较）
        2. 向量 + BM25 混合检索获取高级关键词
        3. 基于图拓扑扩展提取低级关键词
        4. 降级方案：正则表达式提取数值参数
        """
        # 1. 检测查询类型
        query_type = self._detect_query_type(query)
        # - PARAMETER: 包含"参数"、"值"、"多少"等
        # - CONCEPT: 包含"什么是"、"原理"、"定义"等
        # - EQUIPMENT: 包含设备名称如"探测器"、"放大器"
        # - COMPARISON: 包含"比较"、"对比"、"区别"等
        
        # 2. 混合检索获取高级关键词
        vector_results = await self._vector_search(query, top_k=15)
        bm25_results = self._bm25_search(query, top_k=15)
        fused_results = self._rrf_fusion(vector_results, bm25_results)
        
        # 高级关键词 = 融合后的实体名称（分数 > 阈值）
        high_level_keywords = [
            entity_name for entity_name, score in fused_results 
            if score > min_score
        ][:10]
        
        # 3. 图拓扑扩展提取低级关键词
        if high_level_keywords:
            # 基于实体关系图，从高级关键词出发进行邻居扩展
            low_level_keywords = await self.extract_low_level_from_graph(
                high_level_keywords,
                max_depth=2,  # 扩展深度
            )
        
        # 4. 降级方案：正则表达式提取
        if not low_level_keywords:
            # 提取数值参数：能量(keV/MeV)、电压(V/mV)、时间(ns/μs)
            low_level_keywords = self._extract_low_level_fallback(query)
        
        return KeywordExtractionResult(
            high_level_keywords=high_level_keywords,  # 核心实体
            low_level_keywords=low_level_keywords,    # 细节信息
        )
```

##### 图拓扑扩展算法

```python
async def extract_low_level_from_graph(
    self,
    high_level_keywords: List[str],
    max_depth: int = 2,
) -> List[str]:
    """
    从图拓扑中提取低级关键词
    
    算法：BFS 遍历 + 度数加权
    
    原理：
    - 高级关键词作为种子节点
    - 沿图的边进行 BFS 扩展
    - 根据距离衰减和节点度数计算权重
    - 选择权重最高的邻居作为低级关键词
    """
    candidates: Dict[str, float] = {}
    seed_set = set(high_level_keywords)
    
    for seed in high_level_keywords:
        # BFS 遍历
        queue = [(seed, 0)]  # (节点, 深度)
        visited = {seed}
        
        while queue:
            node, depth = queue.pop(0)
            
            if depth >= max_depth:
                continue
            
            for neighbor in graph.neighbors(node):
                if neighbor in visited or neighbor in seed_set:
                    continue
                visited.add(neighbor)
                queue.append((neighbor, depth + 1))
                
                # 计算权重：距离衰减 × 度数权重
                distance_weight = 1.0 / (depth + 1)
                degree_weight = log(1 + graph.degree(neighbor))
                candidates[neighbor] += distance_weight * degree_weight
    
    # 返回权重最高的候选
    return sorted(candidates.items(), key=lambda x: -x[1])[:7]
```

##### 正则降级方案

```python
def _extract_low_level_fallback(self, query: str) -> List[str]:
    """
    正则表达式提取低级关键词（降级方案）
    
    当图扩展结果为空时使用，提取：
    - 能量值：\d+(?:\.\d+)?\s*(?:keV|MeV|GeV)
    - 电压值：\d+(?:\.\d+)?\s*(?:V|mV|kV)
    - 时间值：\d+(?:\.\d+)?\s*(?:ns|μs|ms)
    - 参数名：分辨率、增益、带宽、噪声等
    """
    low_level = []
    
    # 能量参数
    low_level.extend(re.findall(r'\d+(?:\.\d+)?\s*(?:keV|MeV|GeV)', query))
    
    # 电压参数
    low_level.extend(re.findall(r'\d+(?:\.\d+)?\s*(?:V|mV|kV)', query))
    
    # 时间参数
    low_level.extend(re.findall(r'\d+(?:\.\d+)?\s*(?:ns|μs|ms)', query))
    
    # 领域关键词
    domain_keywords = ["分辨率", "增益", "带宽", "噪声", "效率", "阈值"]
    low_level.extend([kw for kw in domain_keywords if kw in query])
    
    return list(set(low_level))[:7]
```

### 4.2 PPR 图过滤算法

#### 4.2.1 算法原理

PPR (Personalized PageRank) 是一种基于图的节点重要性排序算法，用于从知识图谱中筛选与查询最相关的实体和关系。

```
PPR 公式：
PR(p) = (1-d)/N + d * Σ(PR(q)/L(q))
其中：
- d: 阻尼系数（通常 0.85）
- N: 节点总数
- L(q): 节点 q 的出度
```

#### 4.2.2 实现细节

```python
class PPRGraphFilter:
    def build_subgraph_directed(
        self,
        entities: List[Dict],
        relationships: List[Dict],
    ) -> Tuple[nx.DiGraph, Dict[str, int]]:
        """
        构建有向图
        - 正向边权重: 1.0
        - 反向边权重: 0.2（衰减）
        """
        G = nx.DiGraph()
        
        for entity in entities:
            G.add_node(entity_name, **entity)
        
        for rel in relationships:
            src = rel["src_id"]
            tgt = rel["tgt_id"]
            weight = rel.get("weight", 1.0)
            
            # 正向边
            G.add_edge(src, tgt, weight=weight)
            # 反向边（衰减）
            G.add_edge(tgt, src, weight=weight * reverse_edge_weight)
        
        return G
    
    async def filter_by_ppr(
        self,
        raw_data: RawRetrievalData,
        seed_entities: List[str],
        max_tokens: int = 17000,
    ) -> PPRFilteredData:
        """
        PPR 过滤主流程
        """
        # 1. 构建子图
        G, entity_to_idx = self.build_subgraph_directed(
            raw_data.entities,
            raw_data.relationships,
        )
        
        # 2. 设置种子节点
        personalization = {
            entity_to_idx[e]: 1.0 / len(seed_entities)
            for e in seed_entities if e in entity_to_idx
        }
        
        # 3. 计算 PPR 分数（使用 NetworkX 优化实现）
        ppr_scores = nx.pagerank_numpy(
            G,
            alpha=self.ppr_damping,
            personalization=personalization,
        )
        
        # 4. 过滤低分节点
        filtered_entities = [
            e for e in entities
            if ppr_scores.get(entity_to_idx.get(e["entity_name"]), 0) > self.ppr_min_score
        ]
        
        # 5. Token 预算控制
        return self._assemble_with_token_budget(filtered_entities, max_tokens)
```

### 4.3 MMR 重排序算法

#### 4.3.1 算法原理

MMR (Maximal Marginal Relevance) 用于在保持相关性的同时增加结果多样性，避免返回过于相似的内容。

```
MMR 公式：
MMR = λ * Sim1(q, d) - (1-λ) * max(Sim2(d, d'))
其中：
- q: 查询
- d: 候选文档
- d': 已选文档
- λ: 相关性权重（通常 0.5-0.7）
- Sim1: 查询-文档相似度
- Sim2: 文档-文档相似度
```

#### 4.3.2 实现细节

```python
class VectorMMRReranker:
    async def rerank(
        self,
        query: str,
        chunks: List[Dict],
        top_k: int = 10,
        lambda_param: float = 0.7,
    ) -> List[Dict]:
        """
        基于 Embedding 的 MMR 重排序
        """
        # 1. 获取查询和候选的 Embedding
        query_embedding = await self._get_embeddings([query])
        chunk_embeddings = await self._get_embeddings([c["content"] for c in chunks])
        
        # 2. 计算查询-候选相似度
        query_sim = cosine_similarity(query_embedding, chunk_embeddings)
        
        # 3. 贪心选择
        selected = []
        selected_embeddings = []
        remaining = list(range(len(chunks)))
        
        while len(selected) < top_k and remaining:
            best_score = -float('inf')
            best_idx = None
            
            for idx in remaining:
                # 相关性分数
                relevance = query_sim[idx]
                
                # 多样性惩罚
                if selected_embeddings:
                    max_sim = max(
                        cosine_similarity([chunk_embeddings[idx]], [se])
                        for se in selected_embeddings
                    )
                else:
                    max_sim = 0
                
                # MMR 分数
                mmr_score = lambda_param * relevance - (1 - lambda_param) * max_sim
                
                if mmr_score > best_score:
                    best_score = mmr_score
                    best_idx = idx
            
            selected.append(best_idx)
            selected_embeddings.append(chunk_embeddings[best_idx])
            remaining.remove(best_idx)
        
        return [chunks[i] for i in selected]
```

### 4.4 检索流水线

```
┌─────────────────────────────────────────────────────────────────────────────┐
│                          检索流水线                                          │
├─────────────────────────────────────────────────────────────────────────────┤
│                                                                             │
│  ┌─────────────┐                                                            │
│  │   Query     │                                                            │
│  └──────┬──────┘                                                            │
│         ▼                                                                   │
│  ┌─────────────────────────────────────────────────────────────────────┐   │
│  │ Stage 1: 关键词提取                                                   │   │
│  │ - 查询类型检测（参数/概念/设备/比较）                                  │   │
│  │ - 混合检索（向量 + BM25）                                             │   │
│  │ - 图扩展                                                              │   │
│  │ - 关键词重排序                                                        │   │
│  └─────────────────────────────────────────────────────────────────────┘   │
│         ▼                                                                   │
│  ┌─────────────────────────────────────────────────────────────────────┐   │
│  │ Stage 2: 多路召回                                                     │   │
│  │ - 实体检索（NebulaGraph）                                             │   │
│  │ - 关系检索（NebulaGraph）                                             │   │
│  │ - 文本块检索（Milvus + LightRAG）                                     │   │
│  └─────────────────────────────────────────────────────────────────────┘   │
│         ▼                                                                   │
│  ┌─────────────────────────────────────────────────────────────────────┐   │
│  │ Stage 3: PPR 图过滤                                                   │   │
│  │ - 构建子图（有向图 + 反向边衰减）                                      │   │
│  │ - 计算 PPR 分数                                                       │   │
│  │ - 过滤低分节点                                                        │   │
│  │ - Token 预算控制                                                      │   │
│  └─────────────────────────────────────────────────────────────────────┘   │
│         ▼                                                                   │
│  ┌─────────────────────────────────────────────────────────────────────┐   │
│  │ Stage 4: MMR 重排序                                                   │   │
│  │ - 计算查询-候选相似度                                                  │   │
│  │ - 贪心选择（相关性 + 多样性）                                          │   │
│  │ - 返回 Top-K 结果                                                     │   │
│  └─────────────────────────────────────────────────────────────────────┘   │
│         ▼                                                                   │
│  ┌─────────────┐                                                            │
│  │   Result    │                                                            │
│  └─────────────┘                                                            │
│                                                                             │
└─────────────────────────────────────────────────────────────────────────────┘
```

---

## 5. 索引同步机制

### 5.1 同步策略

```python
class RAGIndexSyncManager:
    """
    索引同步管理器
    
    支持：
    1. 入库后增量同步
    2. 定时全量同步
    3. 手动触发同步
    """
    
    async def sync_after_insert(self, kb_code: str, doc_ids: List[str]):
        """入库后增量同步"""
        # 1. 获取新增实体
        entities = await self._get_new_entities(kb_code, doc_ids)
        
        # 2. 批量添加到索引
        await self._batch_add_entities(kb_code, kb_id, entities)
    
    async def force_sync(self, kb_code: str, kb_id: str):
        """强制全量同步"""
        # 1. 获取所有实体
        all_entities = await storage.get_all_nodes()
        
        # 2. 获取已索引实体
        existing = await index_manager.get_indexed_entities(kb_code)
        
        # 3. 计算差异
        new_entities = [e for e in all_entities if e["entity_id"] not in existing]
        
        # 4. 批量添加
        await self._batch_add_entities(kb_code, kb_id, new_entities)
```

### 5.2 定时同步调度

```python
class RAGIndexSyncScheduler:
    """
    定时同步调度器
    
    默认配置：
    - 同步间隔：1 小时
    - 同步时间：每小时的第 0 分钟
    """
    
    async def start(self):
        """启动定时任务"""
        self._scheduler.add_job(
            self._sync_all_knowledge_bases,
            trigger=CronTrigger(minute=0),  # 每小时
            id="rag_index_sync",
            replace_existing=True,
        )
        self._scheduler.start()
```

---

## 6. 存储层实现

### 6.1 NebulaGraph 存储适配

```python
class NebulaGraphStorage(BaseGraphStorage):
    """
    NebulaGraph 存储实现
    
    Schema 设计：
    - Tag: entity (entity_name, entity_type, description, source_id)
    - Tag: chunk (content, file_path, doc_id)
    - Edge: relationship (src_id, tgt_id, weight, description)
    - Edge: contains (entity_id, chunk_id)
    """
    
    async def initialize(self):
        """初始化 Space 和 Schema"""
        # 创建 Space
        await self._execute(f"""
            CREATE SPACE IF NOT EXISTS `{self._space}` 
            (vid_type=FIXED_STRING(256))
        """)
        
        # 创建 Tag
        await self._execute(f"""
            CREATE TAG IF NOT EXISTS entity (
                entity_name STRING,
                entity_type STRING,
                description STRING,
                source_id STRING
            )
        """)
        
        # 创建 Edge Type
        await self._execute(f"""
            CREATE EDGE IF NOT EXISTS relationship (
                weight DOUBLE,
                description STRING
            )
        """)
```

### 6.2 数据一致性保证

```python
# 入库事务
async def insert_document(file_path: str, kb_id: str):
    async with transaction():
        # 1. 解析文档
        md_content = await parse_document(file_path)
        
        # 2. 插入 LightRAG（实体、关系、分块）
        result = await lightrag.ainsert(md_content, doc_id=doc_id)
        
        # 3. 记录到 MySQL
        await kb_manager.add_document(
            kb_id=kb_id,
            doc_id=doc_id,
            chunk_count=result["chunk_count"],
            entity_count=result["entity_count"],
            relation_count=result["relation_count"],
        )
        
        # 4. 更新知识库统计
        await kb_manager.update_knowledge_base_stats(kb_id)
        
        # 5. 触发索引同步
        await sync_manager.sync_after_insert(kb_code, [doc_id])
```

---

## 7. 性能优化

### 7.1 缓存策略

| 缓存层 | 缓存内容 | 过期策略 |
|--------|----------|----------|
| RAG 实例缓存 | LightRAG 实例 | LRU，最大 10 个 |
| 关键词索引缓存 | PerKBKeywordIndex | LRU，最大 5 个 |
| Redis 缓存 | 实体名称、BM25 索引 | 持久化 |
| LLM 缓存 | LLM 响应 | 按文档 ID 清理 |

### 7.2 批量处理优化

```python
# 批量索引同步
async def _batch_add_entities(entities: List[Dict], batch_size: int = 100):
    """批量添加实体，减少 Redis 写入次数"""
    for i in range(0, len(entities), batch_size):
        batch = entities[i:i + batch_size]
        is_last_batch = (i + batch_size) >= len(entities)
        
        # 只在最后一个批次保存到 Redis
        await index_manager.add_entities(
            kb_code, kb_id, batch,
            save_immediately=is_last_batch
        )
```

### 7.3 并发控制

```python
# 异步锁保护共享资源
class KnowledgeBaseManager:
    def __init__(self):
        self._lock = asyncio.Lock()
    
    async def get_rag_instance(self, kb_id: str):
        async with self._lock:
            # 创建或获取 RAG 实例
            pass
```

---

## 8. 扩展性设计

### 8.1 存储后端扩展

```python
# 注册新的存储实现
from lightrag.kg import STORAGES

STORAGES["CustomGraphStorage"] = "backend.rag.storages.custom_storage"
```

### 8.2 检索策略扩展

```python
# 自定义检索策略
class CustomRetrievalStrategy:
    async def retrieve(self, query: str, kb_id: str) -> RawRetrievalData:
        # 自定义检索逻辑
        pass

# 注册到内容过滤管理器
filter_manager.register_strategy("custom", CustomRetrievalStrategy())
```

### 8.3 多模态处理器扩展

```python
# 自定义模态处理器
class AudioModalProcessor(BaseModalProcessor):
    async def process(self, content: bytes, context: str) -> Dict:
        # 音频处理逻辑
        pass

# 注册到 RAGAnything
rag_anything.register_modal_processor("audio", AudioModalProcessor())
```

---

## 9. 监控与日志

### 9.1 关键指标

| 指标 | 描述 | 采集方式 |
|------|------|----------|
| 入库耗时 | 文档入库总耗时 | 日志统计 |
| 检索耗时 | 检索流水线各阶段耗时 | 日志统计 |
| LLM TTFT | 首 Token 返回时间 | 日志统计 |
| 索引同步延迟 | 实体索引同步延迟 | 日志统计 |
| 缓存命中率 | RAG 实例缓存命中率 | 监控系统 |

### 9.2 日志格式

```
[时间] | [级别] | [模块] | [线程] | [文件:行号] | [消息]

示例：
2024-03-21 14:53:45 | INFO | backend.rag.service | MainThread | service.py:1352 | 
[RAGService] 触发索引同步: kb=test_multimodal, doc=32ae750c-a30e-4267-a4fa-2b565107bb6b
```

---

## 10. 总结

本系统采用分层架构设计，实现了：

1. **多知识库物理隔离**：每个知识库独立使用 NebulaGraph Space、Milvus Collection 和 Redis Key 前缀
2. **高效检索流水线**：关键词提取 → 多路召回 → PPR 图过滤 → MMR 重排序
3. **智能内容过滤**：基于 PPR 的图过滤和基于 MMR 的多样性重排序
4. **增量索引同步**：入库后自动触发增量同步，支持定时全量同步
5. **多模态支持**：支持文本、图片、表格、公式等多种内容类型

系统设计遵循高内聚、低耦合原则，各模块职责清晰，便于维护和扩展。
