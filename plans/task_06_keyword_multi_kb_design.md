# 任务六：关键词提取与查询过滤多知识库架构设计

> **文档状态**：📦 任务归档。现行提词见 `backend/rag/keywords/extractor.py`（**hybrid** 生产默认），[docs/知识库算法和架构.md](../docs/知识库算法和架构.md) §4.1。

## 1. 现状分析

### 1.1 现有组件

| 组件 | 文件路径 | 当前问题 |
|------|----------|----------|
| KeywordIndexBuilder | `backend/agents/keyword_index_builder.py` | 硬编码 Collection 名称，无知识库隔离 |
| HybridKeywordExtractor | `backend/agents/keyword_extractor.py` | 单例模式，无法切换知识库 |
| NuclearElectronicsAgent | `backend/agents/nuclear_electronics_agent.py` | 全局单例提取器，绑定单一知识库 |

### 1.2 当前架构问题

```
┌─────────────────────────────────────────────────────────────┐
│                    当前架构（单知识库）                        │
├─────────────────────────────────────────────────────────────┤
│                                                             │
│   ┌─────────────────┐     ┌─────────────────────────────┐  │
│   │  Agent 单例     │────▶│  HybridKeywordExtractor     │  │
│   │  (全局唯一)     │     │  (全局单例)                  │  │
│   └─────────────────┘     └─────────────────────────────┘  │
│                                      │                      │
│                                      ▼                      │
│                           ┌─────────────────────────────┐  │
│                           │  Milvus: rag_nuclear_...    │  │
│                           │  Redis: keyword_index:*     │  │
│                           │  (硬编码命名空间)             │  │
│                           └─────────────────────────────┘  │
│                                                             │
└─────────────────────────────────────────────────────────────┘
```

**核心问题：**
1. Collection 名称硬编码为 `rag_nuclear_electronics_keyword_index`
2. Redis Key 前缀固定为 `keyword_index:`
3. 无法根据知识库切换索引
4. 单例模式导致并发问题

---

## 2. 多知识库架构设计方案

### 2.1 目标架构

```
┌─────────────────────────────────────────────────────────────────────────┐
│                        目标架构（多知识库支持）                           │
├─────────────────────────────────────────────────────────────────────────┤
│                                                                         │
│   ┌─────────────────────────────────────────────────────────────────┐  │
│   │                    KeywordIndexManager (管理器)                  │  │
│   │  ┌─────────────┐  ┌─────────────┐  ┌─────────────┐             │  │
│   │  │ KB1 Index   │  │ KB2 Index   │  │ KB3 Index   │  ...        │  │
│   │  │ (缓存)       │  │ (缓存)       │  │ (缓存)       │             │  │
│   │  └─────────────┘  └─────────────┘  └─────────────┘             │  │
│   └─────────────────────────────────────────────────────────────────┘  │
│                                    │                                    │
│                                    ▼                                    │
│   ┌─────────────────────────────────────────────────────────────────┐  │
│   │              PerKBKeywordExtractor (知识库隔离)                  │  │
│   │  ┌─────────────────┐    ┌─────────────────┐    ┌─────────────┐ │  │
│   │  │ Vector Index    │    │ BM25 Index      │    │ Graph Index │ │  │
│   │  │ (Milvus)        │    │ (Redis/Memory)  │    │ (Nebula)    │ │  │
│   │  └─────────────────┘    └─────────────────┘    └─────────────┘ │  │
│   └─────────────────────────────────────────────────────────────────┘  │
│                                    │                                    │
│                                    ▼                                    │
│   ┌─────────────────────────────────────────────────────────────────┐  │
│   │                    存储层 (按知识库隔离)                          │  │
│   │                                                                   │  │
│   │   Milvus: {kb_code}_keyword_index                                │  │
│   │   Redis: {kb_code}:keyword_index:*                               │  │
│   │   Nebula: {kb_code} Space                                        │  │
│   │                                                                   │  │
│   └─────────────────────────────────────────────────────────────────┘  │
│                                                                         │
└─────────────────────────────────────────────────────────────────────────┘
```

---

## 3. 详细设计方案

### 3.1 核心组件重构

#### 3.1.1 KeywordIndexManager（新增）

**职责：**
- 管理多个知识库的索引实例
- 实现 LRU 缓存机制
- 提供知识库级别的索引生命周期管理

```python
# backend/rag/keywords/index_manager.py

class KeywordIndexManager:
    """
    关键词索引管理器
    
    管理多个知识库的索引实例，支持：
    - 按知识库隔离索引
    - LRU 缓存和自动清理
    - 索引构建和更新
    """
    
    def __init__(self, max_cached_kb: int = 5):
        self._indices: Dict[str, PerKBKeywordIndex] = {}
        self._max_cached = max_cached_kb
        self._lock = asyncio.Lock()
    
    async def get_index(self, kb_code: str) -> PerKBKeywordIndex:
        """获取指定知识库的索引（带缓存）"""
        
    async def build_index(self, kb_code: str, entities: List[EntityData]) -> IndexBuildResult:
        """为指定知识库构建索引"""
        
    async def invalidate_index(self, kb_code: str):
        """使指定知识库的索引失效"""
```

#### 3.1.2 PerKBKeywordIndex（新增）

**职责：**
- 单个知识库的完整索引
- 包含向量索引、BM25索引、图索引
- 知识库级别的资源管理

```python
# backend/rag/keywords/per_kb_index.py

@dataclass
class PerKBKeywordIndex:
    """
    单个知识库的关键词索引
    """
    kb_code: str
    kb_id: str
    
    # 索引组件
    vector_collection: Optional[Collection] = None  # Milvus 向量索引
    bm25_index: Optional[BM25Index] = None          # BM25 文本索引
    graph_index: Optional[nx.Graph] = None          # 图结构索引
    
    # 元数据
    entity_names: List[str] = field(default_factory=list)
    entity_metadata: Dict[str, Dict] = field(default_factory=dict)
    
    # 统计信息
    last_updated: datetime = field(default_factory=datetime.now)
    entity_count: int = 0
    
    async def search_vector(self, query_embedding: np.ndarray, top_k: int = 15) -> List[Tuple[str, float]]:
        """向量搜索"""
        
    async def search_bm25(self, query: str, top_k: int = 15) -> List[Tuple[str, float]]:
        """BM25 搜索"""
        
    async def search_graph(self, seed_entities: List[str], top_k: int = 15) -> List[Tuple[str, float]]:
        """图扩展搜索"""
```

#### 3.1.3 PerKBKeywordExtractor（重构）

**职责：**
- 替换原有的全局单例
- 每个知识库独立的提取器实例
- 支持动态切换知识库

```python
# backend/rag/keywords/extractor.py

class PerKBKeywordExtractor:
    """
    知识库级别的关键词提取器
    
    替换原有的 HybridKeywordExtractor 全局单例
    """
    
    def __init__(
        self,
        kb_code: str,
        kb_id: str,
        index_manager: KeywordIndexManager,
        embedding_func: Optional[Callable] = None,
    ):
        self.kb_code = kb_code
        self.kb_id = kb_id
        self._index_manager = index_manager
        self._embedding_func = embedding_func
    
    async def extract(
        self,
        query: str,
        extraction_mode: ExtractionMode = ExtractionMode.HYBRID,
    ) -> KeywordExtractionResult:
        """
        提取关键词
        
        Args:
            query: 查询文本
            extraction_mode: 提取模式
                - VECTOR: 仅向量检索
                - BM25: 仅 BM25 检索
                - GRAPH: 图扩展检索
                - HYBRID: 混合检索（默认）
        """
        index = await self._index_manager.get_index(self.kb_code)
        
        # 根据模式选择检索策略
        if extraction_mode == ExtractionMode.VECTOR:
            return await self._extract_vector(query, index)
        elif extraction_mode == ExtractionMode.BM25:
            return await self._extract_bm25(query, index)
        elif extraction_mode == ExtractionMode.GRAPH:
            return await self._extract_graph(query, index)
        else:
            return await self._extract_hybrid(query, index)
```

---

### 3.2 存储层改造

#### 3.2.1 Milvus Collection 命名规范

```python
# 原命名（硬编码）
KEYWORD_INDEX_COLLECTION = "rag_nuclear_electronics_keyword_index"

# 新命名（按知识库动态）
def get_keyword_collection_name(kb_code: str) -> str:
    return f"{kb_code}_keyword_index"

# 示例：
# - nuclear_electronics_keyword_index
# - computer_science_keyword_index
# - mathematics_keyword_index
```

#### 3.2.2 Redis Key 命名规范

```python
# 原命名（全局固定）
# keyword_index:entity_names
# keyword_index:entity_metadata:{entity_name}
# keyword_index:bm25_corpus

# 新命名（按知识库前缀）
def get_redis_key(kb_code: str, key_type: str, *args) -> str:
    base = f"{kb_code}:keyword_index"
    if key_type == "entity_names":
        return f"{base}:entity_names"
    elif key_type == "entity_metadata":
        return f"{base}:entity_metadata:{args[0]}"
    elif key_type == "bm25_corpus":
        return f"{base}:bm25_corpus"
    # ...

# 示例：
# - nuclear_electronics:keyword_index:entity_names
# - computer_science:keyword_index:entity_metadata:CPU
```

#### 3.2.3 NebulaGraph Space 复用

```python
# 直接使用知识库已有的 NebulaGraph Space
# 不需要额外创建，通过 kb_code 访问
```

---

### 3.3 Agent 层改造

#### 3.3.1 NuclearElectronicsAgent 重构

```python
# backend/agents/nuclear_electronics_agent.py

class NuclearElectronicsAgent(BaseAgent):
    """
    核电子学知识问答智能体（多知识库版本）
    """
    
    def __init__(
        self,
        kb_id: Optional[str] = None,      # 指定知识库 ID
        kb_code: Optional[str] = None,    # 指定知识库 Code
        **kwargs
    ):
        super().__init__(**kwargs)
        self._kb_id = kb_id
        self._kb_code = kb_code
        self._extractor: Optional[PerKBKeywordExtractor] = None
        self._ppr_filter = PPRGraphFilter()
    
    async def _get_extractor(self) -> PerKBKeywordExtractor:
        """获取当前知识库的提取器"""
        if self._extractor is None:
            # 如果没有指定知识库，使用默认知识库
            if not self._kb_code:
                kb = await self._get_default_kb()
                self._kb_code = kb.kb_code
                self._kb_id = kb.kb_id
            
            index_manager = get_keyword_index_manager()
            self._extractor = PerKBKeywordExtractor(
                kb_code=self._kb_code,
                kb_id=self._kb_id,
                index_manager=index_manager,
                embedding_func=await self._get_embedding_func(),
            )
        return self._extractor
    
    async def process(self, query: str, **kwargs) -> AgentResponse:
        """
        处理查询
        
        支持通过 kwargs 临时切换知识库：
        - kb_id: 指定知识库 ID
        - kb_code: 指定知识库 Code
        """
        # 临时切换知识库
        temp_kb_id = kwargs.get('kb_id')
        temp_kb_code = kwargs.get('kb_code')
        
        if temp_kb_code and temp_kb_code != self._kb_code:
            extractor = PerKBKeywordExtractor(
                kb_code=temp_kb_code,
                kb_id=temp_kb_id or self._kb_id,
                index_manager=get_keyword_index_manager(),
                embedding_func=await self._get_embedding_func(),
            )
        else:
            extractor = await self._get_extractor()
        
        # 1. 关键词提取
        keywords = await extractor.extract(query)
        
        # 2. PPR 图过滤
        filtered = await self._ppr_filter.filter(
            keywords,
            kb_code=temp_kb_code or self._kb_code,
        )
        
        # 3. 生成回答
        response = await self._generate_response(query, filtered)
        
        return response
```

---

## 4. 集成方案

### 4.1 与现有知识库管理集成

```python
# backend/rag/knowledge_base/manager.py

class KnowledgeBaseManager:
    """
    扩展现有知识库管理器，集成关键词索引管理
    """
    
    def __init__(self):
        # ... 现有代码 ...
        self._keyword_index_manager = KeywordIndexManager()
    
    async def create_knowledge_base(self, ...):
        """创建知识库时初始化关键词索引"""
        kb = await self._create_kb_record(...)
        
        # 初始化空的关键词索引
        await self._keyword_index_manager.build_index(
            kb_code=kb.kb_code,
            entities=[],  # 空索引，后续文档入库时更新
        )
        
        return kb
    
    async def delete_knowledge_base(self, kb_id: str):
        """删除知识库时清理关键词索引"""
        kb = await self.get_knowledge_base(kb_id)
        
        # 清理关键词索引
        await self._keyword_index_manager.invalidate_index(kb.kb_code)
        
        # 删除 Milvus Collection
        await self._delete_keyword_collection(kb.kb_code)
        
        # ... 现有清理代码 ...
    
    async def get_keyword_extractor(self, kb_id: str) -> PerKBKeywordExtractor:
        """获取指定知识库的关键词提取器"""
        kb = await self.get_knowledge_base(kb_id)
        return PerKBKeywordExtractor(
            kb_code=kb.kb_code,
            kb_id=kb.kb_id,
            index_manager=self._keyword_index_manager,
        )
```

### 4.2 与文档入库流程集成

```python
# backend/rag/service.py

class RagService:
    """
    扩展现有 RAG 服务，支持关键词索引更新
    """
    
    async def process_multimodal(
        self,
        file_path: str,
        kb_id: Optional[str] = None,  # 新增：指定知识库
        **kwargs
    ):
        """处理文档并更新关键词索引"""
        
        # 1. 文档解析（现有流程）
        chunks = await self._parse_document(file_path)
        
        # 2. 实体抽取（现有流程）
        entities = await self._extract_entities(chunks)
        
        # 3. 存储到 NebulaGraph（现有流程）
        await self._store_to_nebula(entities, kb_id=kb_id)
        
        # 4. 【新增】更新关键词索引
        if kb_id:
            kb = await self._get_kb(kb_id)
            index_manager = get_keyword_index_manager()
            
            # 将新实体添加到关键词索引
            await index_manager.add_entities(
                kb_code=kb.kb_code,
                entities=[
                    EntityData(
                        entity_name=e['entity_name'],
                        entity_type=e['entity_type'],
                        description=e.get('description', ''),
                        source_id=e['source_id'],
                        file_path=file_path,
                    )
                    for e in entities
                ]
            )
        
        return {"success": True, "entity_count": len(entities)}
```

---

## 5. API 扩展

### 5.1 新增关键词管理 API

```python
# backend/api/routers/keywords.py

router = APIRouter(prefix="/knowledge_bases/{kb_id}/keywords", tags=["关键词管理"])

@router.post("/rebuild")
async def rebuild_keyword_index(
    kb_id: str,
    auth: ApiKeyAuth = Depends(verify_api_key),
):
    """
    重建指定知识库的关键词索引
    
    从 NebulaGraph 重新读取所有实体，重建完整的索引。
    """

@router.post("/extract")
async def extract_keywords(
    kb_id: str,
    query: str,
    mode: ExtractionMode = ExtractionMode.HYBRID,
    auth: ApiKeyAuth = Depends(verify_api_key),
) -> KeywordExtractionResult:
    """
    从查询中提取关键词
    
    使用指定知识库的索引进行关键词提取。
    """

@router.get("/stats")
async def get_keyword_index_stats(
    kb_id: str,
    auth: ApiKeyAuth = Depends(verify_api_key),
) -> KeywordIndexStats:
    """
    获取关键词索引统计信息
    
    包括实体数量、索引大小、最后更新时间等。
    """
```

---

## 6. 迁移计划

### 6.1 阶段一：基础设施（1-2 天）

1. **创建新的模块结构**
   ```
   backend/rag/keywords/
   ├── __init__.py
   ├── index_manager.py      # KeywordIndexManager
   ├── per_kb_index.py       # PerKBKeywordIndex
   ├── extractor.py          # PerKBKeywordExtractor
   ├── bm25_index.py         # BM25Index（从原文件迁移）
   └── models.py             # 数据模型定义
   ```

2. **实现存储层隔离**
   - 修改 Milvus Collection 命名
   - 修改 Redis Key 前缀
   - 保持向后兼容的迁移脚本

### 6.2 阶段二：核心组件（2-3 天）

1. **实现 KeywordIndexManager**
   - LRU 缓存机制
   - 索引生命周期管理

2. **实现 PerKBKeywordExtractor**
   - 替换原有全局单例
   - 支持动态知识库切换

3. **集成到 KnowledgeBaseManager**
   - 创建/删除知识库时管理索引

### 6.3 阶段三：Agent 改造（1-2 天）

1. **重构 NuclearElectronicsAgent**
   - 支持 kb_id/kb_code 参数
   - 移除全局单例提取器

2. **集成到 RAG Service**
   - 文档入库时更新关键词索引

### 6.4 阶段四：API 与测试（1-2 天）

1. **新增关键词管理 API**
2. **编写迁移脚本**
3. **完整功能测试**

---

## 7. 文件变更清单

### 7.1 新增文件

| 文件路径 | 说明 |
|----------|------|
| `backend/rag/keywords/__init__.py` | 模块初始化 |
| `backend/rag/keywords/index_manager.py` | 索引管理器 |
| `backend/rag/keywords/per_kb_index.py` | 知识库级索引 |
| `backend/rag/keywords/extractor.py` | 关键词提取器 |
| `backend/rag/keywords/bm25_index.py` | BM25 索引实现 |
| `backend/rag/keywords/models.py` | 数据模型 |
| `backend/api/routers/keywords.py` | 关键词管理 API |
| `backend/scripts/migrate_keywords_to_multi_kb.py` | 迁移脚本 |

### 7.2 修改文件

| 文件路径 | 修改内容 |
|----------|----------|
| `backend/agents/keyword_index_builder.py` | 废弃，功能迁移到新模块 |
| `backend/agents/keyword_extractor.py` | 废弃，功能迁移到新模块 |
| `backend/agents/nuclear_electronics_agent.py` | 使用新的提取器 |
| `backend/rag/knowledge_base/manager.py` | 集成索引管理器 |
| `backend/rag/service.py` | 文档入库时更新索引 |
| `backend/api/http_server.py` | 注册新的 API 路由 |

---

## 8. 风险评估与缓解

| 风险 | 影响 | 缓解措施 |
|------|------|----------|
| 索引重建时间长 | 高 | 支持增量更新，后台异步重建 |
| 内存占用增加 | 中 | LRU 缓存限制，按需加载 |
| 向后兼容性 | 中 | 提供迁移脚本，保留旧接口过渡期 |
| 并发性能 | 低 | 知识库级别锁，避免全局竞争 |

---

## 9. 总结

本设计方案将关键词提取和查询过滤从单知识库架构改造为多知识库架构，核心改进包括：

1. **存储隔离**：每个知识库独立的 Milvus Collection 和 Redis Key 前缀
2. **组件隔离**：每个知识库独立的索引和提取器实例
3. **统一管理**：KeywordIndexManager 统一管理多知识库的索引生命周期
4. **向后兼容**：提供迁移脚本，平滑过渡

**预期收益：**
- 支持多知识库独立的关键词提取
- 更好的并发性能和资源隔离
- 更灵活的知识库管理

---

## 8. 后续扩展：内容过滤迁移

### 8.1 概述

关键词提取完成后，需要进行内容过滤。原有实现中，PPR 过滤和 MMR 重排序逻辑绑定在 `NuclearElectronicsAgent` 内部，无法被其他知识库复用。

详细设计见：[任务七：内容过滤多知识库架构设计](./task_07_content_filter_multi_kb_design.md)

### 8.2 迁移内容

| 组件 | 原位置 | 新位置 | 说明 |
|------|--------|--------|------|
| PPRGraphFilter | `nuclear_electronics_agent.py` | `content_filter/ppr_filter.py` | PPR 图过滤 |
| VectorMMRReranker | `nuclear_electronics_agent.py` | `content_filter/mmr_reranker.py` | MMR 重排序 |
| _rerank_filtered_chunks | `nuclear_electronics_agent.py` | `content_filter/reranker.py` | Rerank 加权融合 |

### 8.3 架构关系

```
┌─────────────────────────────────────────────────────────────────┐
│                        完整检索流水线                            │
├─────────────────────────────────────────────────────────────────┤
│                                                                 │
│   ┌─────────────────────────────────────────────────────────┐  │
│   │  阶段1: 关键词提取 (task_06)                              │  │
│   │  ┌─────────────┐    ┌─────────────┐    ┌─────────────┐  │  │
│   │  │ Vector      │ +  │ BM25        │ -> │ RRF Fusion  │  │  │
│   │  │ Search      │    │ Search      │    │             │  │  │
│   │  └─────────────┘    └─────────────┘    └─────────────┘  │  │
│   └─────────────────────────────────────────────────────────┘  │
│                              │                                  │
│                              ▼                                  │
│   ┌─────────────────────────────────────────────────────────┐  │
│   │  阶段2: 内容过滤 (task_07)                               │  │
│   │  ┌─────────────┐    ┌─────────────┐    ┌─────────────┐  │  │
│   │  │ PPR Filter  │ -> │ Rerank      │ -> │ MMR Rerank  │  │  │
│   │  │ (图过滤)     │    │ (加权融合)   │    │ (多样性)     │  │  │
│   │  └─────────────┘    └─────────────┘    └─────────────┘  │  │
│   └─────────────────────────────────────────────────────────┘  │
│                                                                 │
└─────────────────────────────────────────────────────────────────┘
```

### 8.4 实现状态

| 任务 | 状态 | 说明 |
|------|------|------|
| 关键词提取迁移 | ✅ 已完成 | 见本文档 |
| 内容过滤迁移 | 📋 待实现 | 见 task_07 设计文档 |

---

## 9. 已完成的修改

### 9.1 BM25 分词策略优化

**问题**：原有分词策略将整个中文句子作为一个词，导致无法匹配部分关键词。

**解决**：使用滑动窗口分词策略，支持部分匹配。

```python
# 原有分词
chinese_pattern = re.compile(r'[\u4e00-\u9fff]{2,}')
# "什么是前置放大器" -> ["什么是前置放大器"]

# 新分词策略
for i in range(len(chinese_text)):
    for length in range(2, min(8, len(chinese_text) - i + 1)):
        word = chinese_text[i:i+length]
# "什么是前置放大器" -> ["什么", "什么是", "前置", "放大器", "前置放大器", ...]
```

### 9.2 Milvus Collection 名称修复

**问题**：代码中使用的 Collection 名称与实际不一致。

**解决**：修改 `get_keyword_collection_name` 函数。

```python
# 原有
def get_keyword_collection_name(kb_code: str) -> str:
    return f"{kb_code}_keyword_index"

# 修复后
def get_keyword_collection_name(kb_code: str) -> str:
    return f"rag_{kb_code}_keyword_index"
```

### 9.3 服务启动时预加载索引

**问题**：首次查询需要 120 秒初始化索引。

**解决**：在服务启动时预加载默认知识库的索引。

```python
# backend/api/http_server.py

async def _init_keyword_index() -> None:
    # 预加载默认知识库的索引
    kb = await session.execute(
        select(KnowledgeBase).where(KnowledgeBase.is_default == True)
    )
    if kb:
        index = await kb_manager._keyword_index_manager.get_index(kb.kb_code, kb.kb_id)
        logger.info(f"[关键词索引] 预加载完成: 实体数={len(index.entity_names)}")
```

### 9.4 低级关键词提取限制

**问题**：`_extract_low_level_fallback` 返回结果未限制数量。

**解决**：添加 `[:7]` 限制，与原有实现一致。

```python
# 原有
return list(set(low_level))

# 修复后
return list(set(low_level))[:7]
```
