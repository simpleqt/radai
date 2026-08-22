# RAG (检索增强生成) 模块详解

> **最后同步**：2026-07-14

RAG 模块是「核电子学智能助手」的核心数据引擎。它负责将非结构化的专业文档（如 PDF、Markdown）转化为可检索的知识向量，并提供高效的高精度混合检索服务。

> **生产配置**：核电子学库与期刊库使用 `domain` Profile → Stage-2 **MMR_ONLY**（PPR 已关闭）。  
> **算法与评测主文档** → [知识库算法和架构.md](../知识库算法和架构.md) · [eval_framework.md](../eval_framework.md)  
> 本文档侧重 **代码模块结构** 与 **开发接入**；算法细节与消融结论以算法主文档为准。  
> 📊 面试向流程图 → [plans/qa/03_rag_retrieval.md](../../plans/qa/03_rag_retrieval.md) · [00_architecture_diagrams.md](../../plans/qa/00_architecture_diagrams.md) 图 9–11

### 入库总览（双轨）

当前知识库入库采用 **legacy_lightrag / self_ingestion 长期双轨** 策略。历史知识库默认 legacy；新建知识库可在创建弹窗选择后端。上传先写 `kb_pending_files`，启动入库后置 `status=1`，Worker 统一经 `PendingIngestUseCase` 按后端分流，两套索引命名空间隔离。

```text
上传/Admin ──► MinIO
     │
     └──► kb_pending_files (status 0→1, ingestion_backend)
                │
                ▼
          IngestWorker / StartupTaskProcessor
                │
       ┌────────┴────────┐
       ▼                 ▼
 legacy_lightrag    self_ingestion
       │                 │
       ▼                 ▼
 RAGAnything 解析   SelfIngestionPipeline
       │            ParserRouter → BlockBuilder
       ▼            → LLM/VLM 抽取 → Staging
 LightRAG 入库      → CommitWriter
       │                 ▼
       ▼            MySQL job/chunk metadata
 Milvus + Nebula          + Nebula v2 + Redis BM25
 + Redis             + Milvus chunks/entities v2
       │                 │
       └────────┬────────┘
                ▼
        kb_pending_files status=2 / 3
        self job status=success / partial_success / failed
```

| 后端 | 代码入口 | 适用场景 |
| :--- | :--- | :--- |
| `legacy_lightrag` | `RAGService.process_multimodal()` / `process_md_content()` | 存量知识库、默认新库 |
| `self_ingestion` | `SelfIngestionPipeline.ingest_pending_file()` | 需 `SELF_INGESTION_ENABLED` + KB `self_ingestion_enabled` + 可选白名单；支持学科 Schema、质量门控、版本化提交 |

若 KB 声明 self 但开关/白名单不满足，路由结果为 `blocked`，`kb_pending_files.status=3`，对应 `kb_self_ingest_jobs.status` 记录失败/阻断原因，不会回退 legacy。

详细设计与落地审计 → [入库实现方案.md](../入库实现方案.md) · [入库实现方案审查.md](../入库实现方案审查.md)
---

## 🔍 已有项目模块说明 (Existing Module)

### 1. RAGAnything 引擎 (`rag/raganything/`)

#### 1.1 定位
系统的核心路由层，负责协调文档解析、多模态处理、Embedding 模型、向量存储及检索算法。

#### 1.2 架构设计
采用 Mixin 模式组合功能：
-   **QueryMixin** (`query.py`): 查询功能，支持文本与多模态查询。
-   **ProcessorMixin** (`processor.py`): 文档处理功能，解析文档并处理多模态内容。
-   **BatchMixin** (`batch.py`): 批量处理功能。

#### 1.3 核心流水线
```
文档上传 → 格式检测 → 多模态解析 (MinerU/Docling)
    → 纯文本 LightRAG 入库 (实体抽取 + 关系构建 + 向量化)
    → 多模态内容处理 (图片/表格/公式 → VLM 描述 → LightRAG 入库)
    → Milvus 向量索引 + NebulaGraph 图索引 + Redis 缓存
```

#### 1.4 文档解析器 (`rag/raganything/parser/`)
-   **MineruParser**: 基于 MinerU 的 PDF 解析器（默认），支持 OCR 模式。
-   **DoclingParser**: 基于 Docling 的 PDF 解析器（备选）。
-   解析方法: `auto`（自动检测）/ `ocr`（强制 OCR）/ `txt`（纯文本）。

#### 1.5 多模态处理器 (`rag/raganything/modalprocessors/`)
-   **ImageModalProcessor**: 图片内容处理，使用 VLM 生成描述文本。
-   **TableModalProcessor**: 表格内容处理，提取结构化数据与洞察。
-   **EquationModalProcessor**: 公式内容处理，生成数学分析描述。
-   **GenericModalProcessor**: 通用内容处理，支持自定义内容类型。
-   **ContextExtractor**: 上下文提取器，为多模态处理器提供周围文本上下文。

#### 1.6 配置 (`rag/raganything/config.py`)
```python
@dataclass
class RAGAnythingConfig:
    working_dir: str = "./rag_storage"
    parse_method: str = "auto"          # auto / ocr / txt
    parser: str = "mineru"              # mineru / docling
    enable_image_processing: bool = True
    enable_table_processing: bool = True
    enable_equation_processing: bool = True
    max_concurrent_files: int = 3       # 最大并发文件数
```

---

### 2. RAG 服务入口 (`rag/service.py`) 与四路并行召回机制 (`DirectRetriever`)

**定位**: 全局单例模式，通过 `get_rag_service()` 获取。它不再使用传统慢速的内置 LightRAG `aquery_data` 接口，而是完全采用高并发的 **`DirectRetriever`** 四路并行检索引擎，直连低延时存储层（Redis, Milvus, NebulaGraph）。

系统在第一阶段（Stage-1）启动多条协程，并发从四个不同的检索通道拉取知识碎片，以极高的效率解决实体歧义并拓宽知识面：

```text
                    ┌─────────────────┐
                    │    用户问题      │
                    └────────┬────────┘
                             │
              ┌──────────────┼──────────────┐
              ▼              ▼              ▼
     计算 Query Embedding   │         Path-B: Redis BM25
              │              │         多源关键词扩展
    Path-A: 实体向量 ANN    │              │
    Milvus keyword_index    │              │
              │              │              │
              └──────┬───────┴──────┬───────┘
                     ▼              │
              召回种子实体 Seeds     │
                     │              │
                     ▼              ▼
              Path-D: 图扩展    Path-C: Chunk 语义
              NebulaGraph      Milvus chunks_vdb
                     │              │
                     └──────┬───────┘
                            ▼
                   合并池 merge_entity/chunk_pool
                            │
                            ▼
                   Lazy Fetch 拉取关联 Chunks
                            │
                            ▼
                   RawRetrievalData 统一输出
```

系统在第一阶段（Stage-1）启动多条协程，并发从四个不同的检索通道拉取知识碎片，以极高的效率解决实体歧义并拓宽知识面：

*   **`Path-A` (Milvus keyword_index 实体向量 ANN)**: 对抽取出的实体键进行高精度的向量相似度近似最近邻（ANN）搜索，即便用户输入非标准化词汇也能准确对齐至图谱节点。
*   **`Path-B` (Redis BM25 + 多关键词扩展)**: 基于全文检索算法（BM25），从 Redis 快速拉取高匹配度实体，配合高中低层级关键词进行召回。
*   **`Path-C` (Milvus chunks_vdb 直接 Chunk 语义搜索)**: 绕过实体节点，直接对底层文本切片（Chunks）进行 Dense 向量语义检索，捕捉实体之外的上下文细节。
*   **`Path-D` (NebulaGraph 自适应深度图扩展)**: 依赖 A/B 通道获得的种子实体，使用 NebulaGraph 自适应展开局部拓扑（Neighborhood Expansion），提取多跳实体关系与关联线索。

**子模块索引**（`backend/rag/retriever/`）：

| 文件 | 职责 |
| :--- | :--- |
| `direct_retriever.py` | 四路 `asyncio.gather` 编排、Lazy Fetch |
| `entity_recall.py` | Path-A（Milvus keyword_index）+ Path-B（Redis BM25） |
| `chunk_searcher.py` | Path-C（Milvus chunks_vdb） |
| `graph_expander.py` | Path-D（双向 nGQL + 无向 BFS 分数传导） |
| `fusion.py` | `merge_entity_pool` / `merge_chunk_pool`、`build_ppr_seed_weights` |
| `config.py` | `DirectRetrieverConfig`、`QueryType` 策略矩阵 |

统一检索入口：`backend/api/routers/rag.py` → `retrieve_context()`；环境变量 `USE_DIRECT_RETRIEVER`（默认 `true`）。

#### 2.2 极致性能优化

*   **双源 PPR 种子权重**: 将向量召回与图搜索的分数通过 `build_ppr_seed_weights` 动态混合，作为第二阶段 Personalized PageRank 的高保真初始概率分布。
*   **Lazy Fetch 实体链路截断**: 动态过滤实体关联的 `source_id`，利用 `int(chunk_top_k * 1.5)` 的自适应限额进行批量延迟拉取，避免长尾垃圾文本稀释 Prompt 语义。
*   **多级容灾降级**: 任何一个数据源（如 Redis 离线或 NebulaGraph 抖动）异常时，均有零延时异常处理机制（Exception Catch + Zero Vector Fallback），保证系统持续可用。

#### 2.3 三阶段检索流水线

```
阶段1: 四路并行高效召回 (DirectRetriever)
    → Bypassing lightrag.aquery_data
    → 实体 ANN (Milvus) + Redis BM25 + 语义切片 (Milvus) + Nebula 局部图扩展
    --> 得到 RawRetrievalData 并标记原始分数归属

阶段2: 级联深度过滤 (PerKBContentFilter，由 Retrieval Profile 选择模式)
    → RAW / MMR_ONLY（生产 domain）/ PPR_MMR（消融）/ FULL（default）

阶段3: 智能资产组装与推理
    → 上下文结构化 Prompt 构建 + 图片/表格提取 + LLM 流式输出
```

---

### 3. 内容过滤流水线 (`rag/content_filter/`)

#### 3.1 架构概述
按知识库隔离的过滤流水线，通过 `ContentFilterManager` 的 LRU 缓存管理 `PerKBContentFilter` 实例。

#### 3.2 过滤模式 (`FilterMode`)
| 模式 | 流程 | 适用场景 |
| :--- | :--- | :--- |
| `RAW` | 无过滤 | 直接使用 Stage-1 召回内容，适用于多跳百科问答 |
| `PPR_ONLY` | PPR 图过滤 | 快速检索，无需精排 |
| `PPR_RERANK` | PPR → Rerank | 需要语义精排 |
| `PPR_MMR` | PPR → MMR | 消融对照（历史 domain 默认） |
| `MMR_ONLY` | MMR 重排 | **垂直领域生产配置**（`domain` / `lightweight` Profile） |
| `RERANK_MMR` | Rerank → MMR | 跳过 PPR 的精排变体 |
| `FULL` | PPR → Rerank → Time-Aware → MMR → Evidence Rescue | `default` Profile 完整链路 |

#### 3.3 PPR 图过滤 (`ppr_filter.py`)

**算法**: Personalized PageRank (PPR)，基于 NetworkX 原生 C 实现 (`nx.pagerank_numpy`)。

> **注意**：PPR 在 `FULL` / `PPR_*` 过滤模式下启用。当前生产 Profile（`domain`、`lightweight`）使用 **MMR_ONLY**，Stage-2 跳过 PPR，直接进入 MMR 重排。PPR 仍用于评测消融与 `general_qa` Profile。

**在 FULL 模式下的位置**:
1.  Stage-1: `DirectRetriever` 四路并行召回 → `RawRetrievalData`
2.  Stage-2: PPR 图过滤 → Rerank 加权融合 → MMR 多样性重排
3.  Stage-3: 上下文 Prompt 组装 + LLM 流式输出

**核心参数** (`FilterConfig`):
```python
ppr_damping: float = 0.85          # PPR 阻尼系数
ppr_min_score: float = 0.001       # PPR 最低分数阈值
use_directed: bool = True          # 有向图
reverse_edge_weight: float = 0.2   # 反向边权重
reverse_edge_similarity_floor: float = 0.25   # 低相似度阈值
reverse_edge_min_scale: float = 0.02          # 最低缩放系数
reverse_edge_similarity_gamma: float = 2.0    # 相似度曲线幂次
max_recall_tokens: int = 40000     # 召回阶段最大 Token 数
max_final_tokens: int = 17000      # 最终输出最大 Token 数
```

**知识路径提取**: `_build_path_section()` 提取核心知识路径（Top-N 路径 + 关系线索），增强推理可解释性。

#### 3.4 Rerank 加权融合 (`reranker.py`)

**算法**: PPR 分数（逻辑连通性）+ Reranker 分数（语义相关性）加权融合。

**Rerank API 支持**:
-   DashScope: `https://dashscope.aliyuncs.com/compatible-mode/v1/reranks`
-   vLLM: `http://x.x.x.x:8012/v1/rerank`
-   默认模型: `Qwen3-Reranker-8B`

**安全归一化** (`_normalize_scores_safe`):
-   使用 Z-Score 归一化 + Sigmoid 映射到 [0, 1]。
-   边界处理：当所有分数相同时，赋予微小差异避免全 0.5 丢失信息。

**融合公式**:
```
fused_score = ppr_weight × ppr_normalized + rerank_weight × rerank_normalized
```
默认权重: `ppr_weight=0.4`, `rerank_weight=0.6`。

**降级策略**: 当 Rerank API 不可用时，自动回退到纯 PPR 分数排序。

#### 3.5 MMR 多样性重排序 (`mmr_reranker.py`)

**算法**: Maximal Marginal Relevance (MMR)，兼顾相关性与多样性。

**核心参数**:
```python
mmr_lambda: float = 0.7    # 相关性权重 (1-λ 为多样性权重)
mmr_top_n: int = 20        # 返回文本块数量
```

**MMR 公式**:
```
MMR = λ × Sim(d_i, q) - (1-λ) × max[Sim(d_i, d_j)]  (j ∈ S)
```

---

### 4. 知识库管理器 (`rag/knowledge_base/`)

**定位**: 1500+ 行的核心管理器，负责知识库的全生命周期管理。

**功能**:
-   知识库 CRUD（创建、删除、切换、统计）。
-   多层级目录管理（物理目录与虚拟目录同步）。
-   文档状态追踪（解析状态、入库状态）。
-   知识库配置管理（双库配置：核电子学库 + 期刊论文库）。

---

### 5. 索引同步调度 (`rag/sync/`)

#### 5.1 索引同步管理器 (`rag_index_sync_manager.py`)
-   面向 legacy LightRAG 索引的补充同步工具：入库后可从旧 Milvus chunks collection 找到 `chunk_id`，再从旧 Nebula 节点拉实体并批量补入。
-   支持入库后异步增量同步；状态保存在进程内 `_sync_status`。
-   **不负责** self_ingestion v2 的一致性修复。self v2 的 Milvus/Nebula/Redis 补偿由 `backend/rag/ingestion/repair_worker.py` 基于 `staging_uri` 重放。

#### 5.2 定时同步调度器 (`rag_index_sync_scheduler.py`)
-   默认凌晨 2:00 执行定时同步。
-   支持手动触发同步。
-   调度器只负责任务编排；健康检查请使用 `/api/v1/admin/self-ingestion/health` 或 `/api/v1/health/self-ingestion`（self v2）。

---

### 6. 文档处理器 (`rag/document_processor.py`)

**定位**: legacy 文档处理统一入口，复用 `RagService` 的初始化逻辑。

**核心流程**:
1.  懒加载 `RAGAnything` 实例（复用 `RagService` 单例）。
2.  调用 `RAGAnything.process_document()` 进行解析与入库。
3.  更新文档状态到数据库。

---

### 7. 检索配置与 Profile 管理 (`rag/retrieval_profile.py`)

**定位**: 检索策略路由器。根据知识库类型（`kb_id` 或 `kb_code`）动态匹配最佳的四路召回路径 (Stage-1) 与第二阶段过滤策略 (Stage-2)。

#### 7.1 核心 Profile 策略定义
| Profile 名称 | Stage-1 路径路由 | Stage-2 过滤模式 (`FilterMode`) | 适用场景 |
| :--- | :--- | :--- | :--- |
| `default` | 四路全开（Path A+B+C+D） | `FULL` | 默认配置，完整 PPR→Rerank→MMR |
| `general_qa` | 关闭 Path-A，开启 B/C/D | `RAW` | 多跳百科（HotpotQA、GraphRAG novel） |
| `domain` | 四路全开 | `MMR_ONLY` | **垂直领域生产**（核电子学/期刊/CSH） |
| `lightweight` | 四路全开，关闭过采样 | `MMR_ONLY` | 轻量 Agent / 访客 |

**`FilterMode` 全集**（`content_filter/models.py`）：`RAW`、`MMR_ONLY`、`RERANK`、`RERANK_MMR`、`PPR_ONLY`、`PPR_RERANK`、`PPR_MMR`、`PPR_RERANK_MMR`、`FULL`。生产垂直库 Profile 固定 **`MMR_ONLY`**（跳过 PPR/Rerank）。

> 评测对比：`general_qa` → `RAW`；`default` → `FULL`；生产 NE/期刊/CSH → `domain` + `MMR_ONLY`。

#### 7.2 核心方法
-   `resolve_retrieval_profile(kb_id, kb_code)`: 运行时根据输入知识库的标识或编码动态路由到最适合的 Profile 策略。
-   `build_direct_retriever_config_for_profile(profile)`: 根据解析出的 Profile 动态装配 DirectRetriever 底层各子通道（A/B/C/D）的开关与并发限额。

#### 7.3 已知知识库 ID（生产 / 评测）

| 用途 | kb_id | Profile |
| :--- | :--- | :--- |
| 核电子学主库 | `c056bf59-6091-45ed-98cb-e2e860580a69` | `domain` |
| 期刊论文库 | `de4ccde7-b286-4e49-bd8c-9dd37bd61878` | `domain` |
| CSH 水文地质库 | `b4c3f145-de5f-42fb-88de-2d4856d57460` | `domain` |
| GraphRAG-Bench medical | `aab46593-1a67-4917-a1c7-376c0af5ccaa` | `domain`（评测库，**未**写入 `_KNOWN_KB_PROFILES`） |
| GraphRAG-Bench novel | `4163d2a5-3bfb-4c83-ab61-dc9221044ad0` | `general_qa`（同上，需 `RAG_KB_RETRIEVAL_PROFILE_MAP` 或 `kb_code` 前缀） |

生产三库硬编码于 `retrieval_profile.py` 的 `_KNOWN_KB_PROFILES`；评测/临时库通过环境变量 **`RAG_KB_RETRIEVAL_PROFILE_MAP`**（JSON：`kb_id → profile`）或 **`kb_code` 前缀** 解析：

`hotpotqa_fullwiki*` / `hotpotqa*` → `general_qa`；`rag_nuclear_electronics*` / `nuclearelectronicspaperrepository*` → `domain`。

---

### 8. 关键词提取（Stage 0 · `rag/keywords/`）

> 管理 API 与运维流程 → `backend/api/routers/keywords.py`

**模块**：`PerKBKeywordExtractor`（`extractor.py`），在 `retrieve_context` 的 Stage-1 之前执行。

| 能力 | 说明 |
| :--- | :--- |
| 向量 + BM25 | RRF 融合（默认 0.7 / 0.3） |
| 图邻域扩展 | 基于 hl 关键词在 KB 内图扩展低层实体 |
| 输出 | `hl_keywords` / `ll_keywords` / `QueryType` / `weighted_seed_scores` |
| 生产默认 | **hybrid**（勿在医学库改用 LightRAG LLM 提词） |

评测对照：`eval/graphrag_bench/keyword_sources.py`（hybrid vs LLM）、`keyword_compare.py`（Jaccard）。结论见 [知识库算法和架构.md](../知识库算法和架构.md) §4.1、§11.6。

---

### 9. 多库 Agent 检索（`multi_kb_search_tool.py`）

- 默认双库：核电子学 `c056bf59-…` + 期刊 `de4ccde7-…`；CSH Agent 绑定 `b4c3f145-…`。
- 进程内调用 `retrieve_context`；未传 hl/ll 时共享一次关键词提取与 query embedding。
- 环境变量：`MULTI_KB_PATH_A_QUERY_ONLY`（默认 1）、`MULTI_KB_SEARCH_DEFAULT_TOP_K`（10）、`MULTI_KB_SEARCH_DEFAULT_CHUNK_TOP_K`（6）等，详见算法主文档 §8。

---

## 📄 Markdown 直入库

`.md` 文件可通过现有上传接口入库。当前没有直接 POST Markdown 文本内容的生产路由。

```text
POST /api/v1/uploads/batch  →  PendingFile (status=0)
POST /api/v1/uploads/ingest  →  status=1
ingest-worker:
  legacy_lightrag + .md  → route=legacy-md-direct → MDIngestor
  self_ingestion + .md   → SelfIngestionPipeline → markdown_parser.py / BlockBuilder
```

self 链路的 staging/commit 会写入 `kb_self_ingest_jobs.staging_uri`、`kb_self_chunks`、Milvus `{kb_code}_chunks_v2` / `{kb_code}_entities_v2`、Redis BM25/metadata、NebulaGraph `entity_v2/relation_v2/evidence_v2`。MySQL 不再保存自研图谱事实表或质量事实表。

详见 [md_ingestion_api.md](../md_ingestion_api.md)、[md_ingestion_design.md](../md_ingestion_design.md)。

---

## 🚀 如何进行后续开发 (Future Development)

### 1. 深度切片优化 (Semantic Chunking)
-   **现状**: 当前使用 LightRAG 内置的切片策略（基于 Token 数量 + 重叠窗口）。
-   **优化方向**: 引入 `SemanticSplitter`（已在 `rag/raganything/chunking.py` 中实现），根据上下文语义的变化动态调整切片边界，极大提升信息召回的完整性。
-   **实现路径**: 在 `RAGAnythingConfig` 中增加 `chunking_strategy` 配置项，支持 `fixed` / `semantic` 两种模式。

### 2. 长文档理解与摘要
-   **计划**: 在入库阶段自动生成文档全文摘要及关键词标签，存储为附加 Metadata，加速初级过滤。
-   **实现路径**: 在 `ProcessorMixin.process_document()` 中增加摘要生成步骤，将摘要存入 `RedisKVStorage`。

### 3. 多路检索器自定义
-   **扩展点**: 在 `rag/service.py` 中预留更多检索路由接口，支持接入 Graph-based RAG（如基于知识图谱的检索），以解决跨文档推理问题。
-   **当前状态**: 生产 Stage-1 默认 **DirectRetriever**（`USE_DIRECT_RETRIEVER=true`）。LightRAG 仍支持 `local` / `global` / `hybrid` / `naive` / `mix`；`mix` 为 LightRAG 库内默认，仅作 DR 失败回退或评测对照（见 [知识库算法和架构.md](../知识库算法和架构.md) §5）。

### 4. 实时网页检索集成
-   **落地**: 将 Web Search 结果实时转换为 RAG 临时索引，使智能体具备"即搜即用"的实时知识处理能力。
-   **实现路径**: 在 `multi_kb_search_tool` 中增加 `web_search_and_index` 选项，将本地 Firecrawl 搜索结果作为临时文档插入 LightRAG。

### 5. 多模态查询增强
-   **现状**: `RAGAnything.aquery_with_multimodal()` 已支持图片/表格/公式查询；统一 Agent Runtime 的 `POST /api/v2/agent-runtime/query` 也已支持 `attachments`（`base64` 或 HTTP(S) `image_url`），由运行时将图片内容注入支持视觉的模型。
-   **边界**: 直接调用 legacy `/api/v1/rag` 检索接口时，图片查询仍需由上层先完成视觉语义提取；不要把 Agent Runtime 的附件能力误认为所有 RAG HTTP 路由都自动支持图片输入。
-   **输出安全**: legacy RAG 与 Agent Runtime 的 `message.replace` 出口会清理答案正文中误泄露的内部 `/api/v2/files/kb-image?...` 代理地址，同时保留 Markdown/HTML 图片和代码块中的合法图片引用。

### 6. Rerank 模型优化
-   **现状**: 使用 `Qwen3-Reranker-8B` 远程 API。
-   **优化方向**: 支持本地部署 Rerank 模型（如 BGE-Reranker），减少网络延迟与 API 成本。在 `settings.py` 中增加 `rerank_local_model_path` 配置。

---
> [!TIP]
> 修改 `rag/service.py` 时，请务必关注异步锁机制，避免在并发入库时造成向量索引的写冲突。内容过滤器按知识库隔离（`PerKBContentFilter`），通过 `ContentFilterManager` 的 LRU 缓存管理，修改过滤逻辑时请注意缓存失效策略。
