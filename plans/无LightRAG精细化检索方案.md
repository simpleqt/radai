# 无 LightRAG 精细化检索方案

> **文档状态**：✅ 已实现（DirectRetriever 已替代 Stage1 的 `lightrag.aquery_data(mode="mix")`）  
> **实现代码**：`backend/rag/retriever/direct_retriever.py`、`graph_expander.py`  
> **现行摘要**：[docs/知识库算法和架构.md](../docs/知识库算法和架构.md)  
> **完整演进**：[plans/知识库算法和架构.md](./知识库算法和架构.md)

---

## 一、背景与动机

### 1.1 当前 Stage 1 的瓶颈

现有检索链路的阶段1依赖 `lightrag.aquery_data(mode="mix")`，其内部行为如下：

```
aquery_data(mode="mix")
  ├── 若 hl_keywords/ll_keywords 为空 → 触发 LLM 提取（耗时 >20s）
  ├── local 模式：entity ANN → source_id lookup → chunk retrieve
  ├── global 模式：entity cluster → relationship retrieve
  └── 合并后输出 {entities, relationships, chunks}
```

**核心问题**：

| 问题 | 说明 |
|------|------|
| 黑盒无法干预 | LightRAG 内部召回逻辑无法按 QueryType 调整策略 |
| chunk 召回单一 | 仅走 entity → source_id 链路，缺少直接语义向量检索路径 |
| 图遍历深度固定 | 不区分查询意图，一律 1-hop |
| 分数不透明 | 返回的实体/关系/chunk 无原始召回分数，PPR 只能靠拓扑 |
| 依赖 LightRAG 升级稳定性 | 接口变动风险高 |

### 1.2 目标

1. **完全去除** Stage 1 对 `lightrag.aquery_data` 的依赖
2. **多路并行**：实体向量路径、实体 BM25 路径、Chunk 语义路径、图谱扩展路径并发执行
3. **精细控制**：每个子路径可单独配置 top_k、阈值、图深度
4. **分数透明**：每个候选（实体/关系/chunk）携带来源路径标签与原始分数，供后续 PPR/Rerank 使用
5. **QueryType 感知**：不同意图使用不同策略组合

---

## 二、新架构概览

### 2.1 整体检索流水线

```
用户查询
   │
   ▼
[0] 关键词与意图提取（保留现有）
    ├── hl_keywords, ll_keywords
    ├── QueryType
    ├── weighted_seed_scores
    └── query_embedding（新增：提前计算，四路共用）
   │
   ▼
[1] Stage 1：DirectRetriever — 四路并行召回（★ 新实现）
    ├── Path-A  实体语义召回     Milvus keyword_index ANN
    ├── Path-B  实体关键词召回   Redis BM25 Index
    ├── Path-C  Chunk 直接语义   Milvus chunks_vdb ANN（★ 新增）
    └── Path-D  图谱邻域扩展     NebulaGraph N-hop（深度自适应）
   │
   ▼
[1.5] 多路融合 → RawRetrievalData（携带原始分数标签）
   │
   ▼
[2] Stage 2：PerKBContentFilter（Profile 驱动，非一律 FULL）
    ├── domain / lightweight → **MMR_ONLY**（生产，跳过 PPR/Rerank）★
    ├── general_qa → **RAW**
    ├── default → FULL（PPR → Rerank → Time-Aware → MMR → Evidence Rescue）
    └── 消融对照：PPR_MMR、direct_no_ppr 等见 paper_kb / graphrag_bench
   │
   ▼
[3] 最终截断（_clamp_filtered_chunks）
```

### 2.2 关键存储资源映射

| 存储 | 集合/Key 模式 | 用途 |
|------|--------------|------|
| Milvus `rag_{kb_code}_keyword_index` | `entity_name`, `vector` | Path-A 实体语义搜索 |
| Redis `{kb_code}:keyword_index:bm25_corpus` | BM25 序列化体 | Path-B 实体关键词搜索 |
| Milvus `{kb_code}_chunks` / LightRAG chunks_vdb | `id`, `content`, `vector` | Path-C Chunk 语义搜索 |
| NebulaGraph Space `{kb_code}` | entity TAG + relation EDGE | Path-D 图谱结构扩展 |
| Redis `text_chunks_db` | chunk id → chunk data | Chunk 内容补全 |

---

## 三、新算法详细设计

### 3.1 Path-A：实体语义召回（Milvus ANN）

**现有代码**：`PerKBKeywordIndex.search_vector()` 已实现，直接复用。

**新增扩展**：

```python
class EntityVectorRecallConfig:
    top_k: int = 20                      # 召回数量（QueryType 自适应）
    score_floor: float = 0.30            # COSINE 相似度下限
    query_type_top_k: Dict[QueryType, int] = {
        QueryType.PARAMETER:  25,        # 参数查询需要更多候选
        QueryType.EQUIPMENT:  20,
        QueryType.CONCEPT:    15,
        QueryType.COMPARISON: 25,        # 对比需要双侧候选
        QueryType.GENERAL:    20,
    }
```

**分数标注**：

```python
EntityCandidate(
    name      = "SiPM光电倍增管",
    score     = 0.87,           # COSINE 相似度
    source    = "vec",          # 标记来源路径
    rank      = 0,              # 在本路径内排名
)
```

---

### 3.2 Path-B：实体关键词召回（BM25）

**现有代码**：`PerKBKeywordIndex.search_bm25()` 已实现，直接复用。

**新增扩展**：多查询扩展 BM25 召回 —— 对 hl_keywords 每一条单独 BM25 后合并（而非仅用原始 query 串）。

```python
class EntityBM25RecallConfig:
    top_k: int = 15
    expand_with_keywords: bool = True    # 对每个 hl_keyword 额外跑一次 BM25
    keyword_expand_top_k: int = 8        # 每个关键词扩展多少
    keyword_rrf_k: int = 60
```

**算法**：

```
多查询 BM25：
  results_q    = bm25.search(query, top_k=15)
  results_kw_i = bm25.search(hl_keyword_i, top_k=8)  for each hl_keyword
  merged       = RRF([results_q, results_kw_0, ..., results_kw_n])
```

---

### 3.3 Path-A + B 融合 → 种子实体集

```python
def fuse_entity_paths(
    vec_results: List[EntityCandidate],
    bm25_results: List[EntityCandidate],
    hl_keywords: List[str],
    weighted_seed_scores: Dict[str, float],
    rrf_k: int = 60,
) -> List[SeedEntity]:
```

**融合公式**（三路 RRF + 种子权重提升）：

$$
score_{final}(e) = \frac{1}{k + r_{vec}} + \frac{1}{k + r_{bm25}} + \beta_{seed}(e)
$$

其中 $\beta_{seed}(e)$ 为来自关键词提取阶段的 `weighted_seed_scores`（若 $e$ 在 hl_keywords 中命中则 $\beta = w_{seed}$，否则 $\beta = 0$）。

**结果**：`List[SeedEntity]` 每条携带 `fused_score`、`in_hl`（是否高层关键词命中）、`in_ll`（是否低层关键词命中）。

---

### 3.4 Path-C：Chunk 直接语义召回（★ 全新路径）

**动机**：当前 Stage 1 的 chunk 完全依赖「实体→source_id→chunk」链路，对于描述性内容（CONCEPT 查询）、没有明确实体的自然语言查询，这条路的召回率很低。直接对 `chunks_vdb` 做语义搜索作为独立路径补充。

#### 3.4.1 Milvus Chunks Collection 查询

LightRAG 写入的 chunks 存储在 Milvus（collection 名由 LightRAG 实例 `namespace` 决定，通常为 `{kb_code}_chunks` 或 `{kb_code}__chunks_vdb`），可直接查询。

```python
class DirectChunkSearcher:
    """直连 Milvus chunks_vdb 进行 ANN 检索"""
    
    async def search(
        self,
        query_embedding: np.ndarray,
        top_k: int = 40,
        score_floor: float = 0.25,
    ) -> List[ChunkCandidate]:
        """
        返回: List[ChunkCandidate]
          - chunk_id
          - content
          - file_path / full_doc_id
          - vec_score (COSINE)
          - source = "chunk_vec"
        """
```

**实现要点**：
- Collection 名称通过 LightRAG 实例的 `chunks_vdb.final_namespace` 获取（运行时拿，避免硬编码）
- 输出字段：`["id", "content", "file_path", "full_doc_id", "created_at"]`
- 复用 `chunk_query_fallback.py` 中的 Milvus 查询逻辑

#### 3.4.2 QueryType 调整策略

| QueryType | chunk_top_k 系数 | score_floor |
|-----------|-----------------|-------------|
| CONCEPT   | ×2.0 (上限60)   | 0.20（宽松，覆盖定义段落）|
| PARAMETER | ×1.0            | 0.35（严格，保留数值密集段）|
| EQUIPMENT | ×1.5            | 0.28 |
| COMPARISON| ×2.0            | 0.22（需要双侧对比段落）|
| GENERAL   | ×1.2            | 0.28 |

---

### 3.5 Path-D：图谱邻域扩展（NebulaGraph — 深度自适应）

**现有机制**：LightRAG 内部做 global 模式时会遍历实体-关系。新设计直接用 `NebulaGraphStorage` 查询。

#### 3.5.1 自适应图深度

```python
QUERY_TYPE_GRAPH_DEPTH = {
    QueryType.PARAMETER:  1,   # 参数查询：直接相邻关系足够
    QueryType.EQUIPMENT:  1,   # 设备查询：直接关联
    QueryType.CONCEPT:    2,   # 概念查询：需要上下位关系（2-hop）
    QueryType.COMPARISON: 2,   # 对比查询：需要同层并列概念（2-hop）
    QueryType.GENERAL:    1,   # 通用：保守取 1-hop
}
```

#### 3.5.2 图遍历 nGQL

```nGQL
-- 1-hop 出边 + 入边（覆盖有向图双向）
GO 1 STEPS FROM $seed_vids OVER relation 
  BIDIRECT 
  YIELD 
    src(edge) AS src_id, 
    dst(edge) AS dst_id, 
    properties(edge).relation_type AS rel_type,
    properties(edge).description  AS rel_desc,
    properties(edge).weight        AS weight,
    properties(edge).source_id     AS source_id;

-- 2-hop（CONCEPT/COMPARISON 时）
GO 2 STEPS FROM $seed_vids OVER relation 
  BIDIRECT
  YIELD ... ;
```

**图扩展得分**：

$$
score_{graph}(e_{neighbor}) = \frac{1}{hop} \cdot w_{edge} \cdot sim(e_{neighbor}, query)
$$

其中 $sim$ 使用向量化余弦相似度（复用现有 `PerKBKeywordExtractor` 的向量化相似度计算），$w_{edge}$ 为 NebulaGraph relation 的 `weight` 字段，$hop \in \{1, 2\}$。

#### 3.5.3 关系权重过滤

只保留边权重 $w_{edge} > threshold_{rel}$（默认 0.3），避免低质量抽取关系污染图扩展结果。

---

### 3.6 多路融合 → RawRetrievalData

#### 3.6.1 实体池合并

```python
def merge_entity_pool(
    ab_seeds: List[SeedEntity],         # Path-A+B 融合结果
    d_graph_expanded: List[SeedEntity], # Path-D 图扩展结果
    query_type: QueryType,
) -> List[dict]:
    """
    合并策略：
    1. ab_seeds 为主权重（来源可信度更高）
    2. 图扩展实体若与 ab_seeds 重叠 → 分数叠加
    3. 纯图扩展实体 → 乘以折扣系数 graph_discount
    4. 按 final_score 排序，截断至 entity_pool_size
    """
```

**折扣系数**：

$$
score_{merged}(e) = \begin{cases}
score_{AB}(e) + \gamma_{graph} \cdot score_D(e) & \text{若同时出现} \\
score_{AB}(e) & \text{仅 A+B} \\
\gamma_{graph} \cdot score_D(e) & \text{仅图扩展}
\end{cases}
$$

默认 $\gamma_{graph} = 0.5$（图扩展实体权重打折，作为补充而非主导）。

#### 3.6.2 关系池构建

从 Path-D 获取的边记录直接填充 `relationships`：

```python
@dataclass
class RetrievedRelationship:
    src_entity: str
    dst_entity: str
    rel_type: str
    description: str
    weight: float
    source_id: str          # 关联 chunk 的 source_id
    graph_score: float      # 来自图扩展的原始分数
```

#### 3.6.3 Chunk 池合并（★ 核心新逻辑）

Chunk 来自两个通道：
- **通道1（实体链路）**：entity → `source_id` → Redis text_chunks_db / Milvus fallback
- **通道2（直接向量）**：Path-C 直接 ANN 召回

```python
def merge_chunk_pool(
    entity_linked_chunks: List[dict],   # 通道1
    direct_vec_chunks: List[ChunkCandidate],  # 通道2（Path-C）
    seed_entities: List[SeedEntity],
    query_type: QueryType,
) -> List[dict]:
```

**Chunk 综合评分**（新增 `chunk_entity_coverage` 维度）：

$$
score_{chunk}(c) = \alpha_1 \cdot s_{vec}(c) + \alpha_2 \cdot s_{entity\_link}(c) + \alpha_3 \cdot s_{coverage}(c)
$$

| 分量 | 含义 | 权重 $\alpha_i$ |
|------|------|----------------|
| $s_{vec}(c)$ | Chunk 内容与 query 的向量相似度 | 0.5 |
| $s_{entity\_link}(c)$ | Chunk 通过实体链路召回时的实体权重 | 0.3 |
| $s_{coverage}(c)$ | Chunk 内容中命中种子实体的比例（实体覆盖度） | 0.2 |

**实体覆盖度计算**：

```python
def chunk_entity_coverage(chunk_content: str, seed_entities: List[SeedEntity]) -> float:
    """
    统计 chunk 内容中出现的种子实体数量（加权）
    coverage = Σ w_seed(e) * I(e in chunk) / Σ w_seed(e)
    """
    total_weight = sum(e.fused_score for e in seed_entities)
    if total_weight == 0:
        return 0.0
    hit_weight = sum(
        e.fused_score for e in seed_entities
        if e.name.lower() in chunk_content.lower()
    )
    return hit_weight / total_weight
```

QueryType 权重自适应调整：

| QueryType | $\alpha_1$(vec) | $\alpha_2$(entity) | $\alpha_3$(coverage) |
|-----------|----------------|--------------------|----------------------|
| CONCEPT   | 0.6            | 0.2                | 0.2 |
| PARAMETER | 0.3            | 0.4                | 0.3 |
| EQUIPMENT | 0.4            | 0.4                | 0.2 |
| COMPARISON| 0.5            | 0.2                | 0.3 |
| GENERAL   | 0.5            | 0.3                | 0.2 |

---

### 3.7 完整 DirectRetriever 接口

```python
class DirectRetriever:
    """
    完全绕过 LightRAG.aquery_data 的直连多路检索器
    
    替代原有 _stage1_recall()
    """
    
    def __init__(
        self,
        kb_code: str,
        kb_id: str,
        embedding_func: Callable,
        keyword_index: PerKBKeywordIndex,
        nebula_storage: NebulaGraphStorage,
        lightrag_instance: Optional[Any] = None,  # 仅用于获取 chunks_vdb 引用
        config: Optional[DirectRetrieverConfig] = None,
    ): ...
    
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
    ) -> RawRetrievalData:
        """
        四路并行召回，返回携带原始分数标签的 RawRetrievalData
        """
        # 1. 提前计算 query embedding（四路共用）
        if query_embedding is None:
            query_embedding = await self._embed_query(query)
        
        # 2. 四路并行
        vec_task    = self._run_path_a(query_embedding, query_type, top_k)
        bm25_task   = self._run_path_b(query, hl_keywords, top_k)
        chunk_task  = self._run_path_c(query_embedding, query_type, chunk_top_k)
        # Path-D 依赖 A+B 结果，在 A+B 融合后串行触发
        
        vec_results, bm25_results, chunk_vec_results = await asyncio.gather(
            vec_task, bm25_task, chunk_task
        )
        
        # 3. 实体融合 → 种子集
        seeds = fuse_entity_paths(vec_results, bm25_results, hl_keywords, weighted_seed_scores)
        
        # 4. 图扩展（基于种子集）
        graph_entities, relationships = await self._run_path_d(seeds, query_embedding, query_type)
        
        # 5. 实体池合并
        all_entities = merge_entity_pool(seeds, graph_entities, query_type)
        
        # 6. Chunk 池合并
        entity_chunks = await self._fetch_entity_linked_chunks(all_entities, chunk_top_k)
        all_chunks    = merge_chunk_pool(entity_chunks, chunk_vec_results, seeds, query_type)
        
        # 7. 构建 RawRetrievalData
        return RawRetrievalData(
            entities      = [e.to_dict() for e in all_entities],
            relationships = [r.to_dict() for r in relationships],
            chunks        = all_chunks,
        )
```

---

## 四、RawRetrievalData 扩展字段

为让下游 PPR/Rerank 能利用原始分数，在 chunk/entity dict 中新增标注字段（不破坏现有接口）：

```python
# Entity dict 新增字段
{
    "entity_name": "SiPM光电倍增管",
    "entity_type": "EQUIPMENT",
    "description": "...",
    "source_id": "...",
    # ↓ 新增
    "_vec_score":    0.87,         # Path-A 向量分
    "_bm25_score":   0.65,         # Path-B BM25 分
    "_seed_weight":  0.42,         # 关键词阶段权重
    "_graph_hop":    0,            # 0=种子, 1=1-hop, 2=2-hop
    "_fused_score":  0.78,         # 多路融合后分数（PPR 种子权重来源）
    "_recall_path":  "AB+D",       # 来源路径标记（可观测性）
}

# Chunk dict 新增字段
{
    "content": "...",
    "file_path": "...",
    # ↓ 新增
    "_vec_score":       0.75,      # Path-C 向量分
    "_entity_score":    0.68,      # 实体链路分
    "_coverage_score":  0.45,      # 实体覆盖度分
    "_chunk_score":     0.66,      # 综合 chunk 分（三分量加权）
    "_recall_path":     "entity+vec", # "entity" | "vec" | "entity+vec"
}
```

这些字段会被透传到 PPR/Rerank 过滤器中，具体用途：
- `_fused_score` → PPR `personalization` 种子权重（替代原 `weighted_seed_scores` 均分）
- `_chunk_score` → Rerank 融合时作为第三路分数叠加（可选开关）
- `_recall_path` → 可观测性日志与 A/B 测试

---

## 五、QueryType 策略矩阵

| 策略维度 | GENERAL | PARAMETER | CONCEPT | EQUIPMENT | COMPARISON |
|----------|---------|-----------|---------|-----------|------------|
| 实体 vec top_k | 20 | 25 | 15 | 20 | 25 |
| BM25 扩展关键词 | 否 | 是 | 否 | 是 | 是 |
| 图深度 (hop) | 1 | 1 | 2 | 1 | 2 |
| 关系权重下限 | 0.3 | 0.4 | 0.2 | 0.3 | 0.2 |
| Chunk vec top_k 系数 | ×1.2 | ×1.0 | ×2.0 | ×1.5 | ×2.0 |
| Chunk vec score_floor | 0.28 | 0.35 | 0.20 | 0.28 | 0.22 |
| $\alpha_1$ (vec 权重) | 0.5 | 0.3 | 0.6 | 0.4 | 0.5 |
| $\alpha_2$ (entity 权重) | 0.3 | 0.4 | 0.2 | 0.4 | 0.2 |
| $\alpha_3$ (coverage) | 0.2 | 0.3 | 0.2 | 0.2 | 0.3 |
| 图折扣 $\gamma_{graph}$ | 0.5 | 0.4 | 0.6 | 0.5 | 0.6 |

---

## 六、配置数据结构

```python
@dataclass
class DirectRetrieverConfig:
    """DirectRetriever 全局配置"""
    
    # Path-A
    entity_vec_top_k: int = 20
    entity_vec_score_floor: float = 0.30
    
    # Path-B
    entity_bm25_top_k: int = 15
    entity_bm25_expand_keywords: bool = True
    entity_bm25_keyword_expand_top_k: int = 8
    rrf_k: int = 60
    
    # Path-C
    chunk_vec_top_k: int = 40
    chunk_vec_score_floor: float = 0.25
    chunk_vec_collection_override: Optional[str] = None  # 若 None 则自动探测
    
    # Path-D
    graph_depth: int = 1                    # 默认深度，QueryType 自适应覆盖
    graph_relation_weight_floor: float = 0.3
    graph_max_neighbors_per_seed: int = 50  # 单种子最大扩展邻居数
    graph_entity_discount: float = 0.5      # 图扩展实体折扣系数
    
    # 融合
    entity_pool_size: int = 120             # 实体池上限（进入 PPR 前）
    chunk_pool_size: int = 80               # Chunk 池上限
    
    # QueryType 感知（覆盖默认值）
    query_type_overrides: Dict[str, Dict] = field(default_factory=dict)
    
    # 并发
    max_concurrent_chunk_fetches: int = 10  # 实体→chunk 拉取并发数
```

---

## 七、与现有过滤链路的衔接

**Stage 2 不变**，但需要两处增强：

### 7.1 PPR 种子权重增强

当前 PPR 的 `personalization` 权重来自 `weighted_seed_scores`（关键词阶段产出）。新方案中，将 entity dict 中的 `_fused_score` 合并进来：

```python
def build_ppr_seed_weights(
    entities: List[dict],
    keyword_seed_weights: Dict[str, float],
) -> Dict[str, float]:
    """
    合并两路种子权重：
    - keyword_seed_weights（关键词阶段）
    - entity._fused_score（多路检索阶段）
    最终做 L1 归一化
    """
    merged = {}
    for e in entities:
        name = e.get("entity_name", "")
        kw_w  = keyword_seed_weights.get(name, 0.0)
        ret_w = e.get("_fused_score", 0.0)
        merged[name] = 0.5 * kw_w + 0.5 * ret_w  # 等权合并，可配置
    # L1 归一化
    total = sum(merged.values()) or 1.0
    return {k: v / total for k, v in merged.items()}
```

### 7.2 Rerank 第三路分数（可选）

Rerank 阶段可选择性地将 `_chunk_score` 作为第三维度融合：

```python
# 原有融合
fused = ppr_weight * ppr_norm + rerank_weight * rerank_norm

# 新增可选第三维（开关：use_retrieval_score_in_rerank）
if use_retrieval_score_in_rerank:
    chunk_ret_score = chunk.get("_chunk_score", 0.5)
    fused = (
        (ppr_weight - 0.1) * ppr_norm
        + (rerank_weight - 0.05) * rerank_norm
        + 0.15 * chunk_ret_score_norm
    )
```

---

## 八、新增核心文件清单

| 文件路径 | 职责 |
|----------|------|
| `backend/rag/retriever/direct_retriever.py` | ★ DirectRetriever 主类，四路并行召回入口 |
| `backend/rag/retriever/entity_recall.py` | Path-A（向量）+ Path-B（BM25）+ 融合逻辑 |
| `backend/rag/retriever/chunk_searcher.py` | Path-C，直连 Milvus chunks_vdb ANN 搜索 |
| `backend/rag/retriever/graph_expander.py` | Path-D，直连 NebulaGraph 图扩展（自适应深度）|
| `backend/rag/retriever/fusion.py` | 实体池合并、Chunk 池合并、覆盖度计算 |
| `backend/rag/retriever/config.py` | `DirectRetrieverConfig` + QueryType 策略矩阵 |
| `backend/rag/retriever/__init__.py` | 模块导出 |

修改文件：

| 文件路径 | 修改内容 |
|----------|---------|
| `backend/api/routers/rag.py` | `_stage1_recall` 替换为 `DirectRetriever.retrieve()`；传入 query_embedding |
| `backend/rag/content_filter/ppr_filter.py` | 读取 `_fused_score` 字段增强种子权重 |
| `backend/rag/content_filter/reranker.py` | 可选读取 `_chunk_score` 作为第三融合维度 |
| `backend/rag/content_filter/models.py` | `RawRetrievalData` 补充 `_retrieval_meta` 字段（可观测性）|

---

## 九、实现步骤（分阶段交付）

### Phase 1：基础直连召回（可替换 Stage 1，保持 Stage 2 不变）

1. 实现 `DirectChunkSearcher`（Path-C）—— 最快，依赖少
2. 实现 `EntityRecaller`（Path-A + Path-B 融合）
3. 实现 `GraphExpander`（Path-D，从 NebulaGraphStorage 直查）
4. 集成为 `DirectRetriever.retrieve()`
5. 在 `rag.py` 的 `_stage1_recall` 中新增开关 `use_direct_retriever=True`，对比验证

### Phase 2：精细化 Chunk 评分

1. 实现 `chunk_entity_coverage` 计算
2. 实现三分量 Chunk 评分（vec + entity_link + coverage）
3. 实现 QueryType 策略矩阵的动态切换

### Phase 3：下游增强

1. PPR 种子权重增强（`build_ppr_seed_weights` 合并两路）
2. Rerank 第三路融合维度（带开关）
3. 可观测性日志增强（路径标签统计、各路召回数量）

---

## 十、性能预期

| 维度 | 当前（LightRAG mix） | 新方案（DirectRetriever）|
|------|---------------------|------------------------|
| Stage 1 延迟（有关键词）| 3~8 s | **0.5~2 s**（四路并发，无 LightRAG 内部串行）|
| Stage 1 延迟（无关键词）| >20 s（触发 LLM）| **1~3 s**（直接 query_embedding 替代）|
| Chunk 召回路径 | 仅 entity→source_id | entity链路 + **直接向量搜索**（双路）|
| 图遍历深度控制 | 固定 | **QueryType 自适应** (1~2 hop) |
| 分数透明度 | 无 | **entity + chunk 均携带原始分数** |
| PPR 种子权重 | 仅关键词阶段 | **关键词 + 多路检索双源合并** |

---

## 十一、关键算法公式汇总

| 算法 | 公式 |
|------|------|
| 多路实体融合（RRF + 种子提升）| $s(e)=\frac{1}{k+r_{vec}}+\frac{1}{k+r_{bm25}}+\beta_{seed}(e)$ |
| 图扩展实体得分 | $s_{graph}(e)=\frac{1}{hop}\cdot w_{edge}\cdot sim(e,q)$ |
| 实体池合并折扣 | $s_{merged}(e)=s_{AB}(e)+\gamma_{graph}\cdot s_D(e)$ |
| Chunk 综合评分 | $s_{chunk}=\alpha_1 s_{vec}+\alpha_2 s_{link}+\alpha_3 s_{coverage}$ |
| 实体覆盖度 | $s_{coverage}=\frac{\sum_{e \in seeds, e \in c} w(e)}{\sum_{e \in seeds} w(e)}$ |
| PPR 种子权重合并 | $w_{ppr}(e)=\frac{0.5 w_{kw}(e)+0.5 w_{ret}(e)}{\sum_i(\cdot)}$ |

---

## 十二、风险与回退机制

| 风险 | 回退策略 |
|------|---------|
| NebulaGraph 连接异常 | Path-D 失败 → 跳过图扩展，仅用 A+B 种子 |
| Milvus chunks_vdb 未找到 | Path-C 失败 → 仅用实体链路 chunks |
| 实体种子集为空（A+B 均无结果）| 退化为 query embedding 直接 chunk ANN |
| 整个 DirectRetriever 异常 | 开关回退到原 `lightrag.aquery_data` 路径 |

**回退开关**（`rag.py` 级别）：

```python
USE_DIRECT_RETRIEVER = os.getenv("USE_DIRECT_RETRIEVER", "true").lower() == "true"

if USE_DIRECT_RETRIEVER:
    raw_data = await direct_retriever.retrieve(...)
else:
    raw_data = await _stage1_recall(...)  # 原有 LightRAG 路径
```
