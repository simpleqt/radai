# 任务七：内容过滤多知识库架构设计

> **文档状态**：📦 任务归档。`FilterMode.PPR_MMR` 仍存在于代码作**消融对照**；生产 `domain`/`lightweight` Profile 默认 **MMR_ONLY**。见 [docs/知识库算法和架构.md](../docs/知识库算法和架构.md) §4、§7。

## 1. 现状分析

### 1.1 现有组件

| 组件 | 文件路径 | 当前问题 |
|------|----------|----------|
| PPRGraphFilter | `backend/agents/nuclear_electronics_agent.py` | 绑定在智能体内，无法被其他知识库复用 |
| VectorMMRReranker | `backend/agents/nuclear_electronics_agent.py` | 同上 |
| _rerank_filtered_chunks | `backend/agents/nuclear_electronics_agent.py` | 同上 |

### 1.2 当前架构问题

```
┌─────────────────────────────────────────────────────────────┐
│                    当前架构（智能体绑定）                      │
├─────────────────────────────────────────────────────────────┤
│                                                             │
│   ┌─────────────────────────────────────────────────────┐  │
│   │           NuclearElectronicsAgent                    │  │
│   │  ┌─────────────┐  ┌─────────────┐  ┌─────────────┐  │  │
│   │  │ PPRFilter   │  │ MMRReranker │  │ RerankMethod│  │  │
│   │  │ (内置)       │  │ (内置)       │  │ (内置)       │  │  │
│   │  └─────────────┘  └─────────────┘  └─────────────┘  │  │
│   └─────────────────────────────────────────────────────┘  │
│                          │                                  │
│                          ▼                                  │
│   ┌─────────────────────────────────────────────────────┐  │
│   │              知识库存储层                             │  │
│   │  Milvus / Redis / NebulaGraph                       │  │
│   └─────────────────────────────────────────────────────┘  │
│                                                             │
│   问题：其他智能体/知识库无法复用过滤逻辑                     │
│                                                             │
└─────────────────────────────────────────────────────────────┘
```

**核心问题：**
1. PPR 过滤逻辑绑定在特定智能体内
2. MMR 重排序逻辑无法被其他知识库使用
3. 内容过滤与知识库耦合不清晰
4. 无法统一管理多知识库的过滤策略

---

## 2. 多知识库架构设计方案

### 2.1 目标架构

```
┌─────────────────────────────────────────────────────────────────────────┐
│                        目标架构（知识库端过滤）                           │
├─────────────────────────────────────────────────────────────────────────┤
│                                                                         │
│   ┌─────────────────────────────────────────────────────────────────┐  │
│   │                    Agent 层（轻量化）                            │  │
│   │  ┌─────────────┐  ┌─────────────┐  ┌─────────────┐             │  │
│   │  │ Agent 1     │  │ Agent 2     │  │ Agent 3     │  ...        │  │
│   │  │ (调用过滤)   │  │ (调用过滤)   │  │ (调用过滤)   │             │  │
│   │  └─────────────┘  └─────────────┘  └─────────────┘             │  │
│   └─────────────────────────────────────────────────────────────────┘  │
│                                    │                                    │
│                                    ▼                                    │
│   ┌─────────────────────────────────────────────────────────────────┐  │
│   │              ContentFilterManager (内容过滤管理器)               │  │
│   │  ┌─────────────────────────────────────────────────────────┐   │  │
│   │  │  PerKBContentFilter (知识库级别过滤器)                   │   │  │
│   │  │  ┌─────────────┐  ┌─────────────┐  ┌─────────────┐     │   │  │
│   │  │  │ PPR Filter  │  │ MMR Reranker│  │ Rerank      │     │   │  │
│   │  │  │ (图过滤)     │  │ (多样性)     │  │ (重排序)     │     │   │  │
│   │  │  └─────────────┘  └─────────────┘  └─────────────┘     │   │  │
│   │  └─────────────────────────────────────────────────────────┘   │  │
│   └─────────────────────────────────────────────────────────────────┘  │
│                                    │                                    │
│                                    ▼                                    │
│   ┌─────────────────────────────────────────────────────────────────┐  │
│   │                    存储层 (按知识库隔离)                          │  │
│   │                                                                   │  │
│   │   Milvus: rag_{kb_code}_keyword_index                            │  │
│   │   Redis: {kb_code}:keyword_index:*                               │  │
│   │   Nebula: {kb_code} Space                                        │  │
│   │                                                                   │  │
│   └─────────────────────────────────────────────────────────────────┘  │
│                                                                         │
└─────────────────────────────────────────────────────────────────────────┘
```

---

## 3. 详细设计方案

### 3.1 核心组件设计

#### 3.1.1 ContentFilterManager（新增）

**职责：**
- 管理多个知识库的内容过滤器实例
- 实现 LRU 缓存机制
- 提供知识库级别的过滤生命周期管理

```python
# backend/rag/content_filter/manager.py

class ContentFilterManager:
    """
    内容过滤管理器
    
    管理多个知识库的内容过滤器实例，支持：
    - 按知识库隔离过滤逻辑
    - LRU 缓存和自动清理
    - 统一的过滤接口
    """
    
    def __init__(self, max_cached_kb: int = 5):
        self._filters: Dict[str, PerKBContentFilter] = {}
        self._max_cached = max_cached_kb
        self._lock = asyncio.Lock()
    
    async def get_filter(
        self,
        kb_code: str,
        kb_id: str,
        embedding_func: Optional[Callable] = None,
    ) -> PerKBContentFilter:
        """获取指定知识库的内容过滤器（带缓存）"""
        
    async def invalidate_filter(self, kb_code: str):
        """使指定知识库的过滤器失效"""
```

#### 3.1.2 PerKBContentFilter（新增）

**职责：**
- 单个知识库的完整内容过滤逻辑
- 包含 PPR 过滤、MMR 重排序、Rerank 重排序
- 知识库级别的配置管理

```python
# backend/rag/content_filter/per_kb_filter.py

@dataclass
class FilterConfig:
    """过滤配置"""
    ppr_damping: float = 0.85
    ppr_min_score: float = 0.001
    use_directed: bool = True
    reverse_edge_weight: float = 0.2
    max_recall_tokens: int = 40000
    max_final_tokens: int = 17000
    mmr_lambda: float = 0.7
    mmr_top_n: int = 20
    rerank_ppr_weight: float = 0.4
    rerank_rerank_weight: float = 0.6


@dataclass
class FilteredData:
    """过滤后的数据"""
    entities: List[Dict[str, Any]] = field(default_factory=list)
    relationships: List[Dict[str, Any]] = field(default_factory=list)
    chunks: List[Dict[str, Any]] = field(default_factory=list)
    entity_ppr_scores: Dict[str, float] = field(default_factory=dict)


class PerKBContentFilter:
    """
    知识库级别的内容过滤器
    
    完全迁移自 NuclearElectronicsAgent 中的过滤逻辑
    """
    
    def __init__(
        self,
        kb_code: str,
        kb_id: str,
        config: FilterConfig = None,
        embedding_func: Optional[Callable] = None,
    ):
        self.kb_code = kb_code
        self.kb_id = kb_id
        self.config = config or FilterConfig()
        self._embedding_func = embedding_func
        
        # 核心组件
        self._ppr_filter = PPRGraphFilter(
            ppr_damping=self.config.ppr_damping,
            ppr_min_score=self.config.ppr_min_score,
            max_recall_tokens=self.config.max_recall_tokens,
            max_final_tokens=self.config.max_final_tokens,
            use_directed=self.config.use_directed,
            reverse_edge_weight=self.config.reverse_edge_weight,
        )
        self._mmr_reranker = VectorMMRReranker()
    
    async def filter_content(
        self,
        raw_data: RawRetrievalData,
        hl_keywords: List[str],
        query: str,
    ) -> FilteredData:
        """
        完整的内容过滤流水线
        
        流程：
        1. PPR 图过滤
        2. Rerank 加权融合
        3. MMR 多样性重排序
        """
        
    async def filter_by_ppr(
        self,
        raw_data: RawRetrievalData,
        hl_keywords: List[str],
    ) -> FilteredData:
        """PPR 图过滤"""
        
    async def rerank_chunks(
        self,
        query: str,
        filtered_data: FilteredData,
        ppr_weight: float = 0.4,
        rerank_weight: float = 0.6,
    ) -> FilteredData:
        """重排序加权融合"""
        
    async def mmr_rerank(
        self,
        query: str,
        chunks: List[Dict],
        lambda_param: float = 0.7,
        top_n: int = 20,
    ) -> List[Dict]:
        """MMR 多样性重排序"""
```

#### 3.1.3 PPRGraphFilter（迁移）

**职责：**
- PPR (Personalized PageRank) 图过滤
- 构建有向图/无向图
- 计算实体、关系、文本块的 PPR 分数

```python
# backend/rag/content_filter/ppr_filter.py

class PPRGraphFilter:
    """
    生产级 PPR 图过滤 - 基于NetworkX原生C实现
    
    核心优化：使用nx.pagerank_numpy替代手写幂迭代，速度提升100倍+
    
    三阶段检索流水线：
    1. 放宽初筛，获取原始全量上下文
    2. 计算局部子图的 PPR 权重进行图过滤
    3. 原子化动态组装，控制 Token 数量
    """
    
    def __init__(
        self,
        ppr_damping: float = 0.85,
        ppr_min_score: float = 0.001,
        max_recall_tokens: int = 40000,
        max_final_tokens: int = 17000,
        use_directed: bool = True,
        reverse_edge_weight: float = 0.2,
    ):
        # ... 完全保留原有实现 ...
    
    def build_subgraph_directed(
        self,
        entities: List[Dict[str, Any]],
        relationships: List[Dict[str, Any]],
    ) -> Tuple[nx.DiGraph, Dict[str, int]]:
        """构建有向图"""
        
    def build_subgraph_undirected(
        self,
        entities: List[Dict[str, Any]],
        relationships: List[Dict[str, Any]],
    ) -> Tuple[nx.Graph, Dict[str, int]]:
        """构建无向图"""
        
    def compute_ppr_native(
        self,
        G: nx.Graph,
        seed_entities: List[str],
        entity_to_idx: Dict[str, int],
    ) -> Dict[str, float]:
        """使用NetworkX计算PPR"""
        
    async def filter_by_ppr(
        self,
        raw_data: RawRetrievalData,
        hl_keywords: List[str],
    ) -> FilteredData:
        """生产级安全的PPR过滤"""
        
    def atomic_assembly(
        self,
        filtered_data: FilteredData,
        max_tokens: Optional[int] = None,
    ) -> str:
        """原子化动态组装 Prompt"""
```

#### 3.1.4 VectorMMRReranker（迁移）

**职责：**
- 基于稠密向量的 MMR 重排序
- 使用 Embedding 向量计算语义相似度
- 解决中文分词失效问题

```python
# backend/rag/content_filter/mmr_reranker.py

class VectorMMRReranker:
    """
    基于稠密向量的MMR重排序
    使用真实的Embedding向量计算语义相似度，彻底解决中文分词失效问题
    """
    
    def __init__(self, embedding_dim: int = 4096):
        self.embedding_dim = embedding_dim
        self._embedder = None
    
    async def rerank(
        self,
        query: str,
        chunks: List[Dict],
        rerank_scores: Dict[int, float],
        lambda_param: float = 0.7,
        top_n: int = 20,
        use_fused_score: bool = False,
        precomputed_embeddings: Optional[List[List[float]]] = None,
    ) -> List[Dict]:
        """
        MMR重排序核心算法
        
        Args:
            query: 查询文本
            chunks: 候选chunk列表
            rerank_scores: 原始相关性分数
            lambda_param: 相关性-多样性权衡参数
            top_n: 返回数量
            use_fused_score: 是否使用融合分数（PPR + Rerank）
            precomputed_embeddings: 预计算的embedding向量
        """
```

---

### 3.2 数据模型定义

```python
# backend/rag/content_filter/models.py

from dataclasses import dataclass, field
from typing import Any, Dict, List, Optional
from enum import Enum


class FilterMode(Enum):
    """过滤模式"""
    PPR_ONLY = "ppr_only"           # 仅 PPR 过滤
    PPR_RERANK = "ppr_rerank"       # PPR + Rerank
    PPR_MMR = "ppr_mmr"             # PPR + MMR
    FULL = "full"                   # 完整流水线


@dataclass
class RawRetrievalData:
    """原始检索数据"""
    entities: List[Dict[str, Any]] = field(default_factory=list)
    relationships: List[Dict[str, Any]] = field(default_factory=list)
    chunks: List[Dict[str, Any]] = field(default_factory=list)


@dataclass
class FilteredData:
    """过滤后的数据"""
    entities: List[Dict[str, Any]] = field(default_factory=list)
    relationships: List[Dict[str, Any]] = field(default_factory=list)
    chunks: List[Dict[str, Any]] = field(default_factory=list)
    entity_ppr_scores: Dict[str, float] = field(default_factory=dict)


@dataclass
class FilterConfig:
    """过滤配置"""
    ppr_damping: float = 0.85
    ppr_min_score: float = 0.001
    use_directed: bool = True
    reverse_edge_weight: float = 0.2
    max_recall_tokens: int = 40000
    max_final_tokens: int = 17000
    mmr_lambda: float = 0.7
    mmr_top_n: int = 20
    rerank_ppr_weight: float = 0.4
    rerank_rerank_weight: float = 0.6


@dataclass
class FilterResult:
    """过滤结果"""
    filtered_data: FilteredData
    prompt: str
    filter_time_ms: float = 0.0
    stats: Dict[str, Any] = field(default_factory=dict)
```

---

### 3.3 与 KnowledgeBaseManager 集成

```python
# backend/rag/knowledge_base/manager.py (扩展)

class KnowledgeBaseManager:
    """
    扩展现有知识库管理器，集成内容过滤管理
    """
    
    def __init__(self):
        # ... 现有代码 ...
        self._content_filter_manager = ContentFilterManager()
    
    async def get_content_filter(
        self,
        session: AsyncSession,
        kb_id: str,
        embedding_func: Optional[Callable] = None,
    ) -> PerKBContentFilter:
        """
        获取指定知识库的内容过滤器
        
        Args:
            session: 数据库会话
            kb_id: 知识库 ID
            embedding_func: 嵌入函数（可选）
            
        Returns:
            内容过滤器实例
        """
        kb = await self.get_knowledge_base(session, kb_id)
        if not kb:
            raise ValueError(f"知识库不存在: {kb_id}")
        
        return await self._content_filter_manager.get_filter(
            kb_code=kb.kb_code,
            kb_id=kb.kb_id,
            embedding_func=embedding_func,
        )
    
    async def filter_content(
        self,
        session: AsyncSession,
        kb_id: str,
        raw_data: RawRetrievalData,
        hl_keywords: List[str],
        query: str,
        mode: FilterMode = FilterMode.FULL,
    ) -> FilterResult:
        """
        过滤内容
        
        Args:
            session: 数据库会话
            kb_id: 知识库 ID
            raw_data: 原始检索数据
            hl_keywords: 高级关键词
            query: 查询文本
            mode: 过滤模式
            
        Returns:
            过滤结果
        """
        kb = await self.get_knowledge_base(session, kb_id)
        if not kb:
            raise ValueError(f"知识库不存在: {kb_id}")
        
        content_filter = await self.get_content_filter(session, kb_id)
        
        start_time = time.time()
        
        if mode == FilterMode.PPR_ONLY:
            filtered_data = await content_filter.filter_by_ppr(raw_data, hl_keywords)
        elif mode == FilterMode.PPR_RERANK:
            filtered_data = await content_filter.filter_by_ppr(raw_data, hl_keywords)
            filtered_data = await content_filter.rerank_chunks(query, filtered_data)
        elif mode == FilterMode.PPR_MMR:
            filtered_data = await content_filter.filter_by_ppr(raw_data, hl_keywords)
            filtered_data.chunks = await content_filter.mmr_rerank(
                query, filtered_data.chunks
            )
        else:  # FULL
            filtered_data = await content_filter.filter_content(
                raw_data, hl_keywords, query
            )
        
        prompt = content_filter._ppr_filter.atomic_assembly(filtered_data)
        
        filter_time_ms = (time.time() - start_time) * 1000
        
        return FilterResult(
            filtered_data=filtered_data,
            prompt=prompt,
            filter_time_ms=filter_time_ms,
            stats={
                "entities": len(filtered_data.entities),
                "relationships": len(filtered_data.relationships),
                "chunks": len(filtered_data.chunks),
            }
        )
```

---

### 3.4 Agent 层改造

```python
# backend/agents/nuclear_electronics_agent.py (重构)

class NuclearElectronicsAgent(BaseAgent):
    """
    核电子学知识问答智能体（多知识库版本）
    
    改造：移除内置的 PPR/MMR 组件，改为调用知识库管理器
    """
    
    def __init__(
        self,
        kb_id: Optional[str] = None,
        kb_code: Optional[str] = None,
        **kwargs
    ):
        super().__init__(**kwargs)
        self._kb_id = kb_id
        self._kb_code = kb_code
        # 移除: self.ppr_filter = PPRGraphFilter(...)
        # 移除: self.mmr_reranker = VectorMMRReranker()
    
    async def _filter_content(
        self,
        raw_data: RawRetrievalData,
        hl_keywords: List[str],
        query: str,
    ) -> FilterResult:
        """
        过滤内容 - 委托给知识库管理器
        """
        from backend.rag.knowledge_base.manager import get_kb_manager
        from backend.db.connection import get_async_session_factory
        
        factory = get_async_session_factory()
        async with factory() as session:
            kb_manager = get_kb_manager()
            return await kb_manager.filter_content(
                session=session,
                kb_id=self._kb_id,
                raw_data=raw_data,
                hl_keywords=hl_keywords,
                query=query,
                mode=FilterMode.FULL,
            )
```

---

## 4. 文件结构

```
backend/rag/content_filter/
├── __init__.py              # 模块导出
├── manager.py               # ContentFilterManager
├── per_kb_filter.py         # PerKBContentFilter
├── ppr_filter.py            # PPRGraphFilter（迁移）
├── mmr_reranker.py          # VectorMMRReranker（迁移）
├── reranker.py              # Rerank 相关方法
└── models.py                # 数据模型定义
```

---

## 5. API 扩展

```python
# backend/api/routers/content_filter.py

router = APIRouter(
    prefix="/knowledge_bases/{kb_id}/filter",
    tags=["内容过滤"]
)

@router.post("/filter")
async def filter_content(
    kb_id: str,
    raw_data: RawRetrievalData,
    hl_keywords: List[str],
    query: str,
    mode: FilterMode = FilterMode.FULL,
    auth: ApiKeyAuth = Depends(verify_api_key),
) -> FilterResult:
    """
    过滤内容
    
    使用指定知识库的过滤器进行内容过滤。
    """

@router.post("/ppr")
async def filter_by_ppr(
    kb_id: str,
    raw_data: RawRetrievalData,
    hl_keywords: List[str],
    auth: ApiKeyAuth = Depends(verify_api_key),
) -> FilteredData:
    """
    PPR 图过滤
    
    仅执行 PPR 过滤步骤。
    """

@router.post("/mmr")
async def mmr_rerank(
    kb_id: str,
    query: str,
    chunks: List[Dict],
    lambda_param: float = 0.7,
    top_n: int = 20,
    auth: ApiKeyAuth = Depends(verify_api_key),
) -> List[Dict]:
    """
    MMR 多样性重排序
    """
```

---

## 6. 迁移计划

### 6.1 阶段一：创建模块结构（0.5 天）

1. 创建 `backend/rag/content_filter/` 目录
2. 定义数据模型 `models.py`
3. 创建模块初始化文件

### 6.2 阶段二：迁移核心组件（1 天）

1. 迁移 `PPRGraphFilter` 到 `ppr_filter.py`
2. 迁移 `VectorMMRReranker` 到 `mmr_reranker.py`
3. 迁移 `_rerank_filtered_chunks` 到 `reranker.py`
4. 确保算法完全一致，不做修改

### 6.3 阶段三：实现管理器（0.5 天）

1. 实现 `ContentFilterManager`
2. 实现 `PerKBContentFilter`
3. 集成到 `KnowledgeBaseManager`

### 6.4 阶段四：重构 Agent（0.5 天）

1. 移除 Agent 内置的过滤组件
2. 改为调用知识库管理器
3. 测试验证功能一致

### 6.5 阶段五：API 与测试（0.5 天）

1. 新增内容过滤 API
2. 编写单元测试
3. 完整功能测试

---

## 7. 文件变更清单

### 7.1 新增文件

| 文件路径 | 说明 |
|----------|------|
| `backend/rag/content_filter/__init__.py` | 模块导出 |
| `backend/rag/content_filter/manager.py` | ContentFilterManager |
| `backend/rag/content_filter/per_kb_filter.py` | PerKBContentFilter |
| `backend/rag/content_filter/ppr_filter.py` | PPRGraphFilter（迁移） |
| `backend/rag/content_filter/mmr_reranker.py` | VectorMMRReranker（迁移） |
| `backend/rag/content_filter/reranker.py` | Rerank 相关方法 |
| `backend/rag/content_filter/models.py` | 数据模型定义 |
| `backend/api/routers/content_filter.py` | 内容过滤 API |

### 7.2 修改文件

| 文件路径 | 修改内容 |
|----------|----------|
| `backend/rag/knowledge_base/manager.py` | 集成 ContentFilterManager |
| `backend/agents/nuclear_electronics_agent.py` | 移除内置过滤组件，改用管理器 |
| `backend/api/http_server.py` | 注册内容过滤 API 路由 |

### 7.3 可删除文件

| 文件路径 | 说明 |
|----------|------|
| 无 | 保留原有文件作为参考 |

---

## 8. 注意事项

### 8.1 算法保持不变

- PPR 过滤算法完全保留原有实现
- MMR 重排序算法完全保留原有实现
- Rerank 加权融合算法完全保留原有实现
- 仅做架构迁移，不做算法修改

### 8.2 向后兼容

- Agent 调用方式保持不变
- 返回结果格式保持不变
- 配置参数保持不变

### 8.3 性能考虑

- 使用 LRU 缓存减少过滤器创建开销
- 异步执行 CPU 密集型操作
- 支持预计算 Embedding 向量
