# 03 · RAG 检索系统

> 📊 **图表：** 完整 ASCII 图见 [00_architecture_diagrams.md](./00_architecture_diagrams.md) 图 9–11、15–16、**22、35**；正式文档 → [rag_module.md](../../docs/docs_backend/rag_module.md) · [keywords_module.md](../../docs/docs_backend/keywords_module.md)

---

## L1 初级 🟢

### Q1：本项目的 RAG 基于什么框架？用到了哪些存储？

**一句话结论：** 检索栈以 **LightRAG + 自研 RAGAnything 管道** 为核心，向量存 Milvus、图谱存 NebulaGraph、缓存用 Redis，文档经 MinerU/Docling 解析后入 MinIO/本地 `data/`。

**为什么问这个/背景：**
面试官首先确认你是否知道「数据落在哪、谁负责什么」，这是排障 ingest 失败、检索为空、图谱断连的基础。

**详细解释：**

```text
┌───────────────────────────────────────────────────────────────┐
│                            RAG 存储栈                            │
│ LA  │  (Milvus 向量)  │  (NebulaGraph 图谱)  │  (Redis KV/缓存)     │
└───────────────────────────────────────────────────────────────┘
                                │
                               ▼
┌───────────────────────────────────────────────────────────────┐
│                              入库                               │
│ DOC  │  (MinIO)                                               │
└───────────────────────────────────────────────────────────────┘
                                │
                               ▼
┌───────────────────────────────────────────────────────────────┐
│                              查询                               │
│ Query                                                         │
└───────────────────────────────────────────────────────────────┘

连接:
  LA ──────► (Milvus 向量)
  LA ──────► (NebulaGraph 图谱)
  LA ──────► (Redis KV/缓存)
  DOC ──────► (MinIO)
  DOC ──────► LA
  Query ──────► LA

```

```
                    ┌─────────────────────────────────────┐
                    │         RAGAnything 管道             │
                    │  解析 → chunk → 向量/图谱/元数据      │
                    └─────────────────┬───────────────────┘
                                      │
        ┌─────────────┬───────────────┼───────────────┬─────────────┐
        ▼             ▼               ▼               ▼             ▼
    Milvus      NebulaGraph       Redis           MinIO        本地 data/
   chunk/entity   关系边           KV/缓存         对象存储      开发/备份
   向量索引      图扩展 Path-B/D
```

| 组件 | 角色 | 典型数据 |
|------|------|----------|
| **LightRAG** | 上游框架，本项目大量 patch | entity/relation/chunk 抽象 |
| **Milvus** | 向量检索 Path-A/C | embedding、keyword_index |
| **NebulaGraph** | 图谱 Path-B/D | 实体关系、邻域扩展 |
| **Redis** | 热数据、限流、缓存 | query cache、索引状态 |
| **MinerU/Docling** | PDF/Office 解析 | 结构化文本+图片 |
| **MinIO** | 文件与图片对象 | 文档原件、figure 路径 |

**在本项目中的实现：**

- 服务单例：`backend/rag/service.py` → `get_rag_service()`
- LightRAG patch：Milvus upsert/delete、NebulaGraphStorage 注册、`shared_storage` 改 loop-local
- 入库：`backend/rag/raganything/`、`worker/ingest_worker.py`、`worker/task_worker.py`
- 索引同步：`backend/rag/sync/`

**走一遍具体例子：**

运维上传一份核电子学 PDF：

1. 文件记录写入 MySQL `kb_pending_files`。
2. `ingest_worker` 拉取 status=1 任务 → RAGAnything 解析 → chunk。
3. 向量写入 Milvus，实体关系写入 NebulaGraph，图片 key 存 MinIO。
4. 用户问答时 `get_rag_service()._get_rag_instance(kb_id)` 加载该 KB 的 LightRAG 实例。

**常见误区：**

- ❌ 认为 MySQL 存 chunk 正文用于检索。  
  ✅ MySQL 管元数据/任务队列；检索走向量在 Milvus。
- ❌ 认为 Redis 是主向量库。  
  ✅ Redis 是辅助缓存，非主召回存储。
- ❌ 认为每个 API worker 各有一套 Nebula。  
  ✅ 共享集群，连接在 KB 实例化时建立。

**延伸追问 (L2/L3)：**

- **L2：** LightRAG 的 `shared_storage` 为何改为 loop-local？
- **L3：** 多 KB 多 worker 下 Milvus collection 命名与隔离策略？

---

### Q2：RAG 检索的大致流程是什么？

**一句话结论：** Query 经 **关键词 hybrid 提取** → **Stage-1 四路并行召回（A/B/C/D）** → **融合** → **Stage-2 内容过滤（Profile 决定 FULL/MMR_ONLY/RAW）** → 返回 chunks 给 Agent/API。

**为什么问这个/背景：**
这是 RAG 模块的「主数据流」。不理解 Stage-1/2 分界，就无法解释延迟、召回率、Profile 调参。

**详细解释：**

```text
用户 Query
    │
    ▼
┌───────────────────────────────────────┐
│ 关键词提取 (hybrid)                     │
│ Vector + BM25 + LLM + RRF 融合         │
│ 输出: hl_keywords, ll_keywords,        │
│       query_type, seed_weights         │
└─────────────────┬─────────────────────┘
                  ▼
┌───────────────────────────────────────┐
│ Stage-1 多路召回 (DirectRetriever)      │
│  Path A: 实体 keyword_index 向量       │
│  Path B: NebulaGraph 关系              │
│  Path C: Chunk 语义向量 (Milvus)       │
│  Path D: 图扩展（A/B 种子实体邻域）      │
│  → merge_entity_pool + chunk 评分      │
└─────────────────┬─────────────────────┘
                  ▼
┌───────────────────────────────────────┐
│ Stage-2 内容过滤 (content_filter/)      │
│  FULL: PPR + Rerank + MMR              │
│  MMR_ONLY: 仅 MMR                      │
│  RAW: 跳过，直接返回 Stage-1            │
└─────────────────┬─────────────────────┘
                  ▼
        context_prompt + chunks + images + stats
                  │
                  ▼
        Agent (multi_kb_search) / API (rag.py)
```



| 阶段 | 输入 | 输出 | 延迟敏感度 |
|------|------|------|------------|
| 关键词 | 原始 query | hl/ll 词表 | 高（曾 ~2.5s/KB） |
| Stage-1 | 关键词+embedding | 候选实体/关系/chunks | 高（Milvus 多次） |
| Stage-2 | 候选池 | 精排后 top chunks | 中（PPR 最重） |

**在本项目中的实现：**

- 公共入口：`backend/api/routers/rag.py` → `retrieve_context()`
- Stage-1 直连：`retrieve_context` 内 `_stage1_direct()`（`_USE_DIRECT_RETRIEVER`）
- Stage-2：`backend/rag/content_filter/` → `get_content_filter_manager()`

```563:605:backend/api/routers/rag.py
    if _USE_DIRECT_RETRIEVER:
        raw_data, effective_seed_weights = await _stage1_direct(...)
    ...
    filter_result = await content_filter.filter_content(
        raw_data=raw_data,
        hl_keywords=hl_keywords,
        query=query,
        ...
        mode=effective_filter_mode,
    )
```

**走一遍具体例子：**

Query：「HPGe 探测器能量分辨率的影响因素」

1. hybrid 提取：`hl=["能量分辨率","HPGe"]`, `ll=["半高宽","统计涨落",...]`。
2. Stage-1：Path-A 命中实体「HPGe探测器」；Path-C 召回相关 chunk；Path-D 扩展关联实体。
3. Profile=`domain` → Stage-2 `MMR_ONLY` 去重保留 6 chunks。
4. 拼装 `context_prompt` 含「可引用文献」列表 → 返回给 `multi_kb_search`。

**常见误区：**

- ❌ 认为 Stage-1 和 Stage-2 可任意跳过而不看 Profile。  
  ✅ `RetrievalProfile.content_filter_mode` 与 path 开关共同决定。
- ❌ 认为 LightRAG `aquery_data` 仍是主路径。  
  ✅ 生产默认 DirectRetriever，旧路径作回退。

**延伸追问 (L2/L3)：**

- **L2：** `seed_weights` 如何影响 Stage-1 实体池融合？
- **L3：** Stage-1 过采样 `chunk_top_k×5` 在什么 Profile 下关闭？

---

### Q3：什么是 RetrievalProfile？有哪些预设？

**一句话结论：** `RetrievalProfile` 按 KB 类型配置 **Stage-1 路径开关** 与 **Stage-2 过滤模式**，避免所有库走同一套重型 PPR 流水线。

**为什么问这个/背景：**
百科 QA 与垂直术语库的最优检索策略不同；Profile 是本项目 RAG「可配置策略层」的核心抽象。

**详细解释：**

**Profile 解析优先级（ASCII 架构图）：**

```text
retrieve_context(kb_id, ...)
        │
        ▼
┌───────────────────────────────────────────────────────────┐
│ resolve_retrieval_profile()  优先级（高 → 低）              │
├───────────────────────────────────────────────────────────┤
│ ① RAG_RETRIEVAL_PROFILE 环境变量（全局强制 A/B）            │
│ ② settings.rag_kb_retrieval_profile_map（按 kb_id）        │
│ ③ _KNOWN_KB_PROFILES（硬编码，子线程无 DB 兜底）            │
│ ④ _KB_CODE_PREFIX_PROFILES（kb_code 前缀 → profile 名）    │
│ ⑤ rag_default_retrieval_profile（默认 default）            │
└───────────────────────────┬───────────────────────────────┘
                            ▼
              RetrievalProfile → enable_path_a/b/c/d
                            → content_filter_mode FULL/MMR/RAW
```



预设 Profile（`backend/rag/retrieval_profile.py`）：

| Profile | Path-A | Path-B/C/D | Stage-2 | 过采样 | 适用 |
|---------|--------|------------|---------|--------|------|
| `default` | 全开 | 全开 | FULL (PPR+Rerank+MMR) | 默认开 | 向后兼容 |
| `general_qa` | **关** | C+D 等 | **RAW**（跳过） | **关** | HotpotQA 类百科 |
| `domain` | **开** | 全开 | **MMR_ONLY** | 默认 | 核电子学、CSH |
| `lightweight` | 开 | 全开 | MMR_ONLY | **关** | 低延迟 snippet |

解析优先级：

```
1. 环境变量 RAG_RETRIEVAL_PROFILE（全局强制）
2. settings.rag_kb_retrieval_profile_map（kb_id 映射）
3. _KNOWN_KB_PROFILES（硬编码 kb_id，子线程无 DB 时用）
4. _KB_CODE_PREFIX_PROFILES（kb_code 前缀）
5. settings.rag_default_retrieval_profile（默认 default）
```

**在本项目中的实现：**

```52:72:backend/rag/retrieval_profile.py
RETRIEVAL_PROFILES: Dict[str, RetrievalProfile] = {
    "default": RetrievalProfile(name="default", content_filter_mode=FilterMode.FULL),
    "general_qa": RetrievalProfile(
        name="general_qa",
        content_filter_mode=FilterMode.RAW,
        enable_path_a=False,
        disable_stage1_oversample=True,
    ),
    "domain": RetrievalProfile(
        name="domain",
        content_filter_mode=FilterMode.MMR_ONLY,
    ),
    ...
}
```

```26:38:backend/rag/retrieval_profile.py
_KB_CODE_PREFIX_PROFILES: tuple[tuple[str, str], ...] = (
    ("hotpotqa_fullwiki", "general_qa"),
    ("hotpotqa", "general_qa"),
    ("rag_nuclear_electronics", "domain"),
    ("nuclearelectronicspaperrepository", "domain"),
)
```

- 应用点：`retrieve_context()` 调用 `resolve_retrieval_profile()` → `build_direct_retriever_config_for_profile()`

**走一遍具体例子：**

| kb_id / kb_code | 解析结果 | 行为 |
|-----------------|----------|------|
| `<kb_id_ne_...>`（核电子学） | `domain` | Path-A 开，MMR_ONLY |
| `hotpotqa_dev` | `general_qa` | Path-A 关，跳过 Stage-2 |
| 新库无映射 | `default` | 全路径 + FULL（需谨慎） |

**常见误区：**

- ❌ 认为 Profile 只影响 Stage-2。  
  ✅ 也控制 `enable_path_a/b/c/d` 与 `disable_stage1_oversample`。
- ❌ 在子线程查 DB 失败就无 Profile。  
  ✅ `_KNOWN_KB_PROFILES` 硬编码兜底。

**延伸追问 (L2/L3)：**

- **L2：** `build_direct_retriever_config_for_profile` 与环境变量默认值如何 merge？
- **L3：** 如何为单 KB 做 Profile A/B 而不改代码（仅 env + settings）？

---

### Q4：Agent 如何调用 RAG？和 API 直接检索有什么区别？

**一句话结论：** Agent 通过 **`multi_kb_search` 工具**（内联 `execute_multi_kb_search` → `retrieve_context`）在进程内检索；API 路径走 `api/routers/rag.py` HTTP 端点，适合调试/前端直连，不经过 LangGraph。

**为什么问这个/背景：**
同一套 `retrieve_context` 被多条链路复用；面试官想确认你理解 Agent 路径额外的**并发、RRF、预算、ContextPruning** 层。

**详细解释：**

**Agent 路径 vs API 路径（ASCII 架构图）：**

```text
┌───────────────────────────────────────────────────────────────┐
│                           Agent 路径                            │
│ multi_kb_search tool  │  E  │  RRF  │  ContextPruning         │
└───────────────────────────────────────────────────────────────┘
                                │
                               ▼
┌───────────────────────────────────────────────────────────────┐
│                            API 路径                             │
│ rag.py HTTP  │  RC  │  直接 JSON/SSE 返回                         │
└───────────────────────────────────────────────────────────────┘

连接:
  multi_kb_search tool ──────► E
  E ──────► RRF
  RRF ──────► ContextPruning
  rag.py HTTP ──────► RC
  RC ──────► 直接 JSON/SSE 返回
  retrieve_context 底层 ──────► multi_kb_search tool
  retrieve_context 底层 ──────► rag.py HTTP

```

```
                    ┌─────────────────────────────────────┐
                    │         retrieve_context()          │
                    │    (共用底层 RAG 检索引擎)            │
                    └──────────────┬──────────────────────┘
                                   │
              ┌────────────────────┴────────────────────┐
              ▼                                         ▼
    ┌─────────────────────┐                 ┌─────────────────────┐
    │ Agent 路径           │                 │ API 路径             │
    │ LangGraph tool call │                 │ HTTP rag.py          │
    └──────────┬──────────┘                 └──────────┬──────────┘
               │                                       │
               ▼                                       ▼
    execute_multi_kb_search                   单 kb_id retrieve
    ├── 共享关键词/embedding                   无 RRF 融合
    ├── asyncio 并发多 KB                      无 Agent 预算
    ├── 跨库 RRF 融合                          无 Middleware
    └── 字符/次数限流                          直接 HTTP 响应
               │
               ▼
    ToolMessage → ContextPruning → retrieved_evidence
```

| 维度 | Agent | API |
|------|-------|-----|
| 入口 | `multi_kb_search_tool` | `rag.py` routers |
| 多 KB | 并发 + RRF | 通常单 kb_id |
| 关键词 | 共享提取、Path-A 加速 | 同底层 extract |
| 结果消费 | ToolMessage / Lightweight 直注 | HTTP 响应 |
| 后处理 | ContextPruning、图片 token 注册 | 较少 |

**在本项目中的实现：**

- 工具：`backend/application/tools/providers/multi_kb_search_tool.py`
- 单库检索：`retrieve_context()` in `rag.py`
- Lightweight 内联：同样调 `execute_multi_kb_search`

```408:438:backend/application/tools/providers/multi_kb_search_tool.py
async def _search_single_kb(...) -> dict:
    from backend.api.routers.rag import retrieve_context
    filtered_data, context_prompt, images, stats = await retrieve_context(
        query=query,
        kb_id=kb_id,
        ...
        disable_stage1_oversample=_MULTI_KB_DISABLE_STAGE1_OVERSAMPLE,
        content_filter_mode=content_filter_mode,
    )
```

**走一遍具体例子：**

- **Agent：** 用户问 NE 问题 → Full Agent 调 `multi_kb_search(query="...")` → 并发查核电子学库+论文库 → RRF 合并 6 chunks → 5000 字工具返回 → Middleware 提升。
- **API：** 开发者 `curl /api/rag/retrieve?kb_id=xxx&q=...` → 单库 `retrieve_context` → 原始 stats 便于对比 Profile。

**常见误区：**

- ❌ 认为 Agent 与 API 检索质量必然相同。  
  ✅ multi_kb 关过采样、Path-A 加速、RRF 融合会改变结果形态。
- ❌ 认为 API 不走 Profile。  
  ✅ 共用 `resolve_retrieval_profile`。

**延伸追问 (L2/L3)：**

- **L2：** `content_filter_mode=None` 时 multi_kb 如何继承 KB Profile？
- **L3：** 如何让 Explore 学习模块与 Agent 共用检索缓存？

---

## L2 中级 🟡

### Q5：Stage-1 四条 Path（A/B/C/D）分别做什么？

**一句话结论：** A=实体向量索引，B=图谱关系，C=chunk 语义向量，D=从种子实体图扩展；Profile 可单独关闭任一路径。

**为什么问这个/背景：**
Stage-1 决定 recall 上限与 Milvus/Nebula 调用次数；是延迟优化（关 Path-A）与垂直域实体锚定（开 Path-A）的战场。

**详细解释：**

**Stage-1 四路召回（ASCII 架构图）：**

```text
Query + hybrid 关键词
        │
        ├──────────────┬──────────────┬──────────────┐
        ▼              ▼              ▼              ▼
   ┌─────────┐   ┌─────────┐   ┌─────────┐   ┌─────────┐
   │ Path-A  │   │ Path-B  │   │ Path-C  │   │ Path-D  │
   │实体向量  │   │图谱关系  │   │Chunk语义│   │图扩展    │
   │Milvus   │   │Nebula   │   │Milvus   │   │Nebula   │
   └────┬────┘   └────┬────┘   └────┬────┘   └────┬────┘
        │              │              │              │
        └──────┬───────┴──────────────┘              │
               ▼                                     │
        merge_entity_pool ───────────────────────────┘
        (实体种子 + 权重)
               │
               ▼
        chunk 候选 + 实体关联评分
               │
               ▼
        raw_data → Stage-2 内容过滤
```



| Path | 名称 | 机制 | 依赖存储 |
|------|------|------|----------|
| **A** | 实体召回 | query/hl 词对 `keyword_index` 向量检索 | Milvus entity 索引 |
| **B** | 关系召回 | 利用 NebulaGraph 中实体间边 | NebulaGraph |
| **C** | Chunk 向量 | 传统语义相似度 | Milvus chunk 集合 |
| **D** | 图扩展 | 从 A/B 命中实体扩展 k-hop 邻域 | NebulaGraph |

Stage-1 融合概念流：

```
Path A ──┐
Path B ──┼──► merge_entity_pool ──► 实体种子 + 权重
Path D ──┘              │
                        ▼
Path C ──────────► chunk 候选 + 实体关联评分
                        │
                        ▼
                  raw_data (entities, relationships, chunks)
```

**在本项目中的实现：**

- 配置：`RetrievalProfile.enable_path_a/b/c/d` → `build_direct_retriever_config_for_profile()`
- 执行：`rag/retriever/` DirectRetriever（`_stage1_direct`）
- Path-A 加速（multi_kb）：`MULTI_KB_PATH_A_QUERY_ONLY=1` 时仅整句 1 路向量，不对 hl 前 3 词各打一次

**走一遍具体例子：**

Query 含专业术语「CANberra 1525 放大器」：

- **domain Profile：** Path-A 命中实体「1525放大器」→ Path-D 扩展关联「核谱仪」→ Path-C 补语义 chunk → 实体锚定提升 precision。
- **general_qa Profile：** Path-A 关闭 → 主要靠 Path-C 语义 + Path-D 轻量扩展 → 降低 Milvus 调用，适合百科事实句。

**常见误区：**

- ❌ 认为四路 always 并行且等权重。  
  ✅ 融合有 seed_weights、merge_entity_pool 逻辑。
- ❌ 关 Path-A 只影响 Stage-2。  
  ✅ Stage-1 实体池来源减少，影响后续 PPR 种子。

**延伸追问 (L2/L3)：**

- **L2：** Path-A 默认 4 次 keyword_index 日志（1 整句 + 3 hl）如何验证？
- **L3：** 新 Path E（如 BM25 直搜 chunk）接入点在哪？

---

### Q6：Stage-2 内容过滤有哪些模式？

**一句话结论：** `FilterMode` 三种——**FULL**（PPR+Rerank+MMR）、**MMR_ONLY**（仅多样性）、**RAW**（跳过 Stage-2）；由 Profile 或 `content_filter_mode` 参数选定。

**为什么问这个/背景：**
Stage-2 是延迟大户（尤其 PPR）。理解模式差异才能解释为何 domain 库 P95 低于 default。

**详细解释：**

| 模式 | 流水线 | 典型延迟 | 适用 |
|------|--------|----------|------|
| **FULL** | PPR → Rerank → MMR | 最高 | default、大库需精排 |
| **MMR_ONLY** | 跳过 PPR/Rerank，仅 MMR 去重 | 中 | domain、lightweight |
| **RAW** | 无过滤，Stage-1 直出 | 最低 | general_qa、评测对齐 |

模块结构：

```
rag/content_filter/
├── models.py          FilterMode 枚举
├── manager.py         get_content_filter_manager()
├── ppr_filter.py      图传播重排
├── reranker.py        交叉编码/LLM rerank
├── per_kb_filter.py   KB 级策略
└── mmr.py             最大边际相关性
```

**在本项目中的实现：**

- `retrieve_context()` 中：

```501:506:backend/api/routers/rag.py
    profile = resolve_retrieval_profile(kb_id=kb_id, kb_code=kb_code, ...)
    effective_filter_mode = content_filter_mode or profile.content_filter_mode
```

- multi_kb 传入 `content_filter_mode=None` 时跟随各 KB Profile（domain → MMR_ONLY）

**走一遍具体例子：**

同一 Stage-1 候选池 30 chunks：

- **FULL：** PPR 用实体种子在 chunk 图上传播 → Rerank top30→10 → MMR 选 6 → 高精度高延迟。
- **MMR_ONLY：** 直接 MMR 从 30 选 6 → 快，可能漏掉图结构相关 chunk。
- **RAW：** 取 Stage-1 前 6 → 评测时与 HotpotQA baseline 对齐。

**常见误区：**

- ❌ 认为 RAW 等于「低质量」。  
  ✅ 对百科 QA，重型过滤反而可能伤 multi-hop recall。
- ❌ 在 API 强制 FULL 可 universally 提升效果。  
  ✅ 小库 PPR 易过拟合，大库延迟爆炸。

**延伸追问 (L2/L3)：**

- **L2：** MMR 的 λ 参数在哪配置？对 diversity 的影响？
- **L3：** 如何 per-query 动态选 FULL vs MMR（query_type 驱动）？

---

### Q7：`multi_kb_search_tool` 做了哪些性能优化？

**一句话结论：** 核心是 **并发多 KB**、**关键词/embedding 只算一次**、**Path-A 整句单向量**、**跨库 RRF**、**独立超时与错误隔离**、**top_k/字符硬封顶**。

**为什么问这个/背景：**
双库检索若 naive 实现，关键词提取与 Path-A 调用次数线性翻倍；这是本项目 RAG 延迟优化的集中体现。

**详细解释：**

优化清单：

| # | 优化 | 机制 | 节省 |
|---|------|------|------|
| 1 | 并发查询 | `asyncio.gather` + `wait_for` 总超时 |  wall time ≈ max(单库) |
| 2 | 共享关键词 | `_prepare_shared_query_context` 仅在首个 KB 调 `_extract_keywords` | ~2.5s × (N-1) |
| 3 | 共享 embedding | 首个 KB 算 query embedding 复用 | 避免重复 embed |
| 4 | Path-A 加速 | `MULTI_KB_PATH_A_QUERY_ONLY=1` 清空 hl，仅整句向量 | 双库 2 次 vs 8 次 index |
| 5 | 关 Stage-1 过采样 | `_MULTI_KB_DISABLE_STAGE1_OVERSAMPLE=True` | 缩小候选池 |
| 6 | 跨库 RRF | `_merge_results_rrf` 全局 top chunk_top_k | 避免 8+8 段拼接 |
| 7 | 错误隔离 | 单库 timeout/异常 → success=False，其他继续 | 可用性 |
| 8 | 防并行工具 | `kb_lock` 拒绝第二个并发 multi_kb | 防 Agent 重复检索 |
| 9 | 子 Agent 去重 | `_INFLIGHT_MULTI_KB_TASKS` 相同 query 复用 | 防 loop 内双调 |

环境变量：

| 变量 | 默认 | 说明 |
|------|------|------|
| `MULTI_KB_PATH_A_QUERY_ONLY` | 1 | Path-A 仅整句 |
| `MULTI_KB_SEARCH_DEFAULT_TOP_K` | 10 | 阶段1 top_k |
| `MULTI_KB_SEARCH_MAX_TOP_K` | 12 | 硬顶 |
| `MULTI_KB_SEARCH_MAX_RESULT_CHARS` | 5000 | Full Agent 工具返回上限 |

**在本项目中的实现：**

- 源码：`backend/application/tools/providers/multi_kb_search_tool.py`
- 关键函数：`_prepare_shared_query_context`、`_apply_path_a_fast_keywords`、`_merge_results_rrf`

```558:565:backend/application/tools/providers/multi_kb_search_tool.py
def _apply_path_a_fast_keywords(hl, ll):
    if not _MULTI_KB_PATH_A_QUERY_ONLY:
        return list(hl or []), list(ll or [])
    return [], list(ll or [])  # Path-A 仅整句；ll 仍供 Path-B/D
```

```1105:1120:backend/application/tools/providers/multi_kb_search_tool.py
    tasks = [
        _search_single_kb_with_timeout(..., external_hl_keywords=per_kb_hl, ...)
        for kb_id in effective_kb_ids
    ]
    results = await asyncio.wait_for(asyncio.gather(*tasks), timeout=total_timeout)
```

**走一遍具体例子：**

双库检索，默认配置：

1. 首个 KB 提取关键词（~2.5s，仅一次）。
2. Path-A fast：每库 1 次 keyword_index → 共 2 次（非 8 次）。
3. 两库各 ~4s 并发 → 总 ~4-5s（非 8s 串行）。
4. RRF 从两库 chunks 全局选 6 段 → 拼成 ≤5000 字返回。

**常见误区：**

- ❌ 提高 `top_k` 到 10 能提速。  
  ✅ 注释明确：扩大候选池**通常更慢**。
- ❌ 两库各返回 chunk_top_k 段再拼接。  
  ✅ RRF **全局**截断到 chunk_top_k（默认 6）。
- ❌ Agent 可并行调两个 multi_kb。  
  ✅ `kb_lock` 会拒绝并提示用已有 evidence。

**延伸追问 (L2/L3)：**

- **L2：** `TURN_MAX_EVIDENCE_CHARS` 与 `MAX_RESULT_CHARS` 如何叠加？
- **L3：** weighted merge 策略与 RRF 的实现差异？

---

### Q8：关键词提取的 hybrid 策略是什么？

**一句话结论：** 生产路径 `PerKBKeywordExtractor` 融合 **向量相似、BM25 统计、LLM 抽取** 三路结果，再用 **RRF** 合并，兼顾延迟与召回。

**为什么问这个/背景：**
纯 LLM 提词约 2.5s/KB，是检索链路的头号延迟；hybrid 是本项目可观测的性能优化点。

**详细解释：**

```
Query
  ├── Vector 路: embedding 近邻词/实体
  ├── BM25 路:  统计显著 term
  └── LLM 路:   高阶 hl + 低阶 ll 概念/术语
              │
              ▼
         RRF 融合
              │
              ▼
    hl_keywords, ll_keywords, query_type, seed_weights
```

| 输出字段 | 用途 |
|----------|------|
| `hl_keywords` | Path-A 实体检索（multi_kb fast 模式下可清空） |
| `ll_keywords` | Path-B/D 图扩展 |
| `query_type` | general/parameter/concept/equipment/comparison |
| `seed_weights` | Stage-1 实体池融合权重 |

**在本项目中的实现：**

- 模块：`backend/rag/keywords/`
- 入口：`rag.py` → `_extract_keywords()`；multi_kb → `_extract_shared_keywords_once()`
- 意图检测：`detect_query_type()`（可 thread  offload）

**走一遍具体例子：**

「比较 NaI 和 HPGe 探测器的优缺点」：

1. LLM 路输出 `query_type=comparison`，hl 含「NaI」「HPGe」。
2. BM25 补全语料高频词「闪烁体」「半导体」。
3. Vector 路拉近 embedding 空间相关实体。
4. RRF 合并 → Stage-1 多种子并行召回。

**常见误区：**

- ❌ 认为 Agent 传入 `query_type` 就跳过 hl/ll 提取。  
  ✅ 仅传 hl/ll 才视为外部关键词；单传 query_type 仍提取（见 `retrieve_context` 注释）。
- ❌ 认为 hybrid 不要 LLM。  
  ✅ LLM 仍是重要一路，只是与 BM25/Vector 并行缩短关键路径。

**延伸追问 (L2/L3)：**

- **L2：** deep_research Prompt 要求 Agent 自提关键词如何与 hybrid 提取协作？
- **L3：** 如何缓存 query→keywords 跨 session 复用？

---

### Q9：LightRAG 在本项目做了哪些 patch？

**一句话结论：** 主要 patch 包括 Milvus 行为适配、**NebulaGraphStorage** 注册、**shared_storage 改 loop-local**、async limiter 与 Uvicorn 多 worker 兼容。

**为什么问这个/背景：**
上游 LightRAG 假设单 loop、单进程；本项目有多 KB、子 Agent 独立 loop、多 worker，不 patch 会出现死锁与连接错乱。

**详细解释：**

| Patch | 问题 | 解决 |
|-------|------|------|
| Milvus upsert/delete/query | API 与版本差异 | 统一封装 |
| NebulaGraphStorage | 默认无 Nebula 后端 | 注册自定义 Storage |
| loop-local shared_storage | 跨 asyncio loop 抢锁死锁 | 每 loop 独立锁 |
| async limiters | 多 worker 并发限制失效 | 与部署形态对齐 |

**在本项目中的实现：**

- 初始化：`backend/rag/service.py` 内 `get_rag_service()` / `_get_rag_instance()`
- 子 Agent 线程内仍可通过 kb_id 获取 RAG 实例，依赖 loop-local 锁

**走一遍具体例子：**

主 loop 请求检索同时 subagent-exec 线程也检索同一 KB：

- **未 patch：** 共享 asyncio Lock → `RuntimeError: Lock bound to different event loop`。
- **已 patch：** 各 loop 独立 storage 锁 → 并行安全（DB 连接仍须 NullPool）。

**常见误区：**

- ❌ 认为 patch 可以 upstream 贡献后直接删。  
  ✅ 需验证多 worker + subagent 场景。
- ❌ 认为 LightRAG 负责 Stage-2 过滤。  
  ✅ Stage-2 主要在 `content_filter/`，非 LightRAG 原生。

**延伸追问 (L2/L3)：**

- **L2：** RAG 实例 per kb_id 的生命周期与 LRU 策略？
- **L3：** 升级 LightRAG 版本的回归测试清单？

---

### Q10：知识库入库流程是怎样的？

**一句话结论：** 双 Worker 路径——**Kafka Worker** 处理 OCR/增强任务，**Ingest Worker** 轮询 MySQL 待入库文件执行 RAGAnything；另有 **sync 调度器** 对齐索引状态。

**为什么问这个/背景：**
检索为空Often 是 ingest 未完成的的问题；考察 ingest→索引→检索的闭环。

**详细解释：**

**入库双 Worker 链路（ASCII 架构图）：**

```text
文档上传
    │
    ▼
┌──────────────────────┐
│ kb_pending_files     │  status: 0 → 1 → 2
│ (MySQL 待入库队列)    │
└──────────┬───────────┘
           │
     ┌─────┴─────┐
     ▼           ▼
┌──────────┐ ┌──────────────┐
│ Kafka    │ │ Ingest Worker│
│ Task     │ │ (DB 轮询)     │
│ Worker   │ └──────┬───────┘
│ OCR/增强  │        │
└────┬─────┘        │
     └──────┬───────┘
            ▼
   MinerU/Docling 解析 → chunk → embedding
            │
     ┌──────┴──────┐
     ▼             ▼
  Milvus      NebulaGraph
     │
     ▼
 rag/sync/ 定时对齐索引状态
```



**在本项目中的实现：**

- `backend/worker/task_worker.py` — Kafka 消费
- `backend/worker/ingest_worker.py` — MySQL 轮询
- `backend/rag/raganything/` — 解析与写入管道
- `backend/rag/sync/` — 索引同步

**走一遍具体例子：**

用户上传 PDF 到「核电子学库」：

1. API 写 `kb_pending_files`，对象存 MinIO。
2. Ingest Worker claim 任务 → MinerU 解析 → 500 chunks。
3. 批量 embed 写 Milvus；实体关系写 Nebula。
4. status 更新为完成；用户检索时可命中新 chunk。
5. 若 ingest 未完成：检索 recall 低，但 API 通常不 500（除非 kb_id 无效）。

**常见误区：**

- ❌ 认为上传即可检索。  
  ✅ 异步 ingest 有延迟。
- ❌ 认为 Kafka 与 Ingest 做同一件事。  
  ✅ 分工不同，可能串联。

**延伸追问 (L2/L3)：**

- **L2：** ingest 失败重试与 status 字段语义？
- **L3：** 如何实现增量 re-index 而不全库重建？

---

## L3 高级 🔴

### Q11：为什么 vertical domain（核电子学）保留 Path-A 而 general_qa 关闭？

**一句话结论：** 消融结论——百科 multi-hop QA 靠 chunk 语义即可，Path-A 增延迟收益低；垂直库专业实体/标准号/设备名需 **实体锚定**，Path-A 对 recall 关键，但 Stage-2 用 MMR_ONLY 控延迟。

**为什么问这个/背景：**
这是 **Recall-Latency 可解释 tradeoff** 的 L3 题，要求结合实验而非背表格。

**详细解释：**

| 维度 | general_qa | domain |
|------|------------|--------|
|  query 特点 |  Wikipedia 事实句 | 术语、型号、标准号 |
| Path-A | 关：减少 Milvus 调用 | 开：实体索引命中 |
| Stage-2 | RAW：保留 Stage-1 多样性 | MMR_ONLY：跳过 PPR |
| 过采样 | 关 | 默认（multi_kb 另强制关） |
| 风险 | 实体链噪音 | PPR 过重 |

```
                Recall
                  ▲
                  │     domain (+Path-A, MMR_ONLY)
                  │    ╱
                  │   ╱
                  │  ╱  default (FULL)
                  │ ╱
                  │╱________________► Latency
            general_qa (no A, RAW)
```

**在本项目中的实现：**

- 注释与映射：`retrieval_profile.py` 文件头说明 + `_KB_CODE_PREFIX_PROFILES`
- 核电子学 kb_id 硬编码在 `_KNOWN_KB_PROFILES` → `domain`

**走一遍具体例子：**

- Query「HotpotQA: Where was X born?」→ general_qa → Path-C 语义足够 → 无需实体图。
- Query「CANberra 1525 的增益公式？」→ domain → Path-A 命中「1525」实体 → 避免纯语义漂移到无关放大器。

**常见误区：**

- ❌ 认为 domain 应用 FULL Stage-2 最好。  
  ✅ 消融显示 PPR 对垂直库边际收益小于延迟成本。
- ❌ 一次实验定终身。  
  ✅ 需随 KB 数据分布 A/B 调整 Profile。

**延伸追问 (L2/L3)：**

- **L2：** 如何用日志统计 Path-A 命中率和空结果率？
- **L3：** 自动 Profile 选择（元数据分类器）如何设计？

---

### Q12：多 KB RRF 融合与单 KB 内融合有什么区别？

**一句话结论：** 单 KB 内在 `rag/retriever/fusion.py` 做实体池 merge 与 chunk 评分；跨 KB 在 `multi_kb_search_tool._merge_results_rrf` 对**各库已过滤 chunks** 做 Reciprocal Rank Fusion，且**全局**截断到 `chunk_top_k`。

**为什么问这个/背景：**
两层融合解决不同问题：单库融合利用图结构；跨库融合解决**评分尺度不可比**。

**详细解释：**

**单 KB 内：**

```
Path A/B/D 实体 ──► merge_entity_pool
Path C chunks   ──► 与实体种子联合评分
                         │
                         ▼
                   Stage-2 过滤
```

**跨 KB：**

```
KB1 chunks (rank 0..n)  ──┐
KB2 chunks (rank 0..m)  ──┼──► RRF score = Σ 1/(k+rank+1), k=60
                          │
                          ▼
                   全局排序 → top chunk_top_k
```

RRF 特点：不依赖各 KB 绝对分，适合「核电子学库」与「论文库」评分标度不同。

**局限：**

- 不感知 KB 权威度/时效性
- 噪声库与高质库等权
- 当前主要靠 kb 绑定而非动态权重

**在本项目中的实现：**

```733:783:backend/application/tools/providers/multi_kb_search_tool.py
def _merge_results_rrf(results, final_top_n=MAX_CHUNK_TOP_K, k=60, ...):
    for result in results:
        for rank, chunk in enumerate(chunks):
            score = 1.0 / (k + rank + 1)
            chunk_scores[unique_chunk_key] += score
    sorted_chunks = sorted(chunk_scores.items(), key=lambda x: x[1], reverse=True)
    selected = sorted_chunks[:final_top_n]
```

**走一遍具体例子：**

核电子学库 rank0 chunk A（专业公式），论文库 rank0 chunk B（综述段落）：

- RRF：两库 rank0 各得 `1/61` 分 → 接近并列；若 A 在 KB1 rank0 且 KB2 无相似，A 仍上位。
- 若 weighted 策略：需校准两库 score 分布（当前 merge_strategy 默认 rrf）。

**常见误区：**

- ❌ 认为双库各 6 段共 12 段返回。  
  ✅ RRF 全局最多 chunk_top_k（默认 6）。
- ❌ 认为 RRF 在 Stage-2 之前做。  
  ✅ 各库先 complete retrieve_context（含 Stage-2），再跨库 RRF。

**延伸追问 (L2/L3)：**

- **L2：** `merge_strategy=weighted` 何时启用？权重从哪来？
- **L3：** 如何引入 KB 可信度先验而不破坏 RRF 无标定优势？

---

### Q13：RAG 预热失败或超时会怎样？对线上问答有何影响？

**一句话结论：** `http_server.py` lifespan 对 RAG 预热设超时后 **API 仍启动**（优雅降级），预热后台继续；首条问答可能 **冷启动** 延迟升高，但不会因预热失败整体 500。

**为什么问这个/背景：**
生产可用性题：区分「服务不可用」与「首请求慢」。

**详细解释：**

```
服务启动
    │
    ▼
lifespan: RAG 预热任务
    │
    ├── 成功 → 首请求低延迟
    │
    └── 超时 → 记录 warn，API 继续 listen
              └── 后台继续预热
                        │
                        ▼
              首条 get_rag_service() / _get_rag_instance()
              可能触发完整初始化（秒级~十秒级）
```

| 状态 | 用户感知 | 系统行为 |
|------|----------|----------|
| 预热完成 | 首问正常速度 | KB 实例已缓存 |
| 预热超时 | 首问慢，后续正常 | 懒加载 RAG 实例 |
| ingest 未完成 | 回答偏空/泛化 | 检索 recall↓，非 500 |
| kb_id 不存在 | 可能 4xx/工具 error | 需看异常处理分支 |

**在本项目中的实现：**

- 启动：`backend/http_server.py` lifespan
- 懒加载：`rag/service.py` `get_rag_service()`、`_get_rag_instance(kb_id)`
- 日志：`logs/api.log`、RAG 初始化与 keyword 索引日志

**走一遍具体例子：**

部署新版本后 Milvus 短暂不可达：

1. 预热 30s 超时 → Uvicorn 仍绑定 8082。
2. 用户 1 分钟内提问 → 首次 `_get_rag_instance` 重连 Milvus → 延迟 +8s。
3. Milvus 恢复后后续请求正常；ingest 若未完成则内容仍少。

**常见误区：**

- ❌ 认为预热失败 = 服务挂了。  
  ✅ Graceful degradation 设计。
- ❌ 忽略 ingest 与预热的区别。  
  ✅ 预热是客户端/实例；ingest 是数据有无。

**延伸追问 (L2/L3)：**

- **L2：** 健康检查端点是否包含 RAG ready 状态？
- **L3：** 如何实现 readiness probe 与 liveness 分离？

---

### Q14：如何为新知识库选择合适的 RetrievalProfile？

**一句话结论：** 按**内容类型**选 Profile（百科→general_qa，术语密集→domain，快搜→lightweight），通过 **kb_code 前缀 / kb_id 映射 / env** 注册，再用 recall@k、P95、引用准确率验证。

**为什么问这个/背景：**
实操题：新库默认 `default` FULL 往往延迟爆炸或小库过拟合。

**详细解释：**

决策树：

```
新 KB 入库
    │
    ▼
内容类型？
    ├── 百科/Wikipedia 多跳 ──► general_qa
    ├── 专业垂直/术语多    ──► domain
    └── 只要快速 snippet   ──► lightweight
    │
    ▼
注册方式（任选/组合）
    ├── _KB_CODE_PREFIX_PROFILES  前缀匹配
    ├── _KNOWN_KB_PROFILES        固定 kb_id
    ├── settings.rag_kb_retrieval_profile_map  JSON
    └── RAG_RETRIEVAL_PROFILE     全局强制（慎用）
    │
    ▼
验证
    ├── recall@k / MRR
    ├── P95 延迟（关注 Path-A 次数）
    └── Agent 回答引用是否指向具体文献
```

| 注册方式 | 优点 | 缺点 |
|----------|------|------|
| kb_code 前缀 | 批量、可读 | 前缀冲突需排序 |
| kb_id 字典 | 精确 | 需维护 UUID |
| env 强制 | A/B 快 | 影响全部 KB |

**在本项目中的实现：**

```104:122:backend/rag/retrieval_profile.py
def _resolve_profile_name(kb_id, kb_code) -> str:
    if forced := _profile_from_env(): ...
    if kb_id:
        if mapped := _kb_profile_map_from_settings().get(kb_id): ...
        if mapped := _KNOWN_KB_PROFILES.get(kb_id): ...
    for prefix, profile_name in _KB_CODE_PREFIX_PROFILES:
        if code.startswith(prefix): ...
    return _default_profile_name()
```

**走一遍具体例子：**

新库「CSH 污染场地修复」，kb_id 已在 `_KNOWN_KB_PROFILES` → `domain`：

1. 自动 Path-A 开 + MMR_ONLY。
2. multi_kb 与单库 API 行为一致。
3. 若 P95 仍高：设 `MULTI_KB_PATH_A_QUERY_ONLY=1`（默认已开）或降 top_k。

**常见误区：**

- ❌ 新库不配置 → default FULL「最安全」。  
  ✅ 小库 FULL PPR 易过拟合；大库延迟高。
- ❌ 只测 API 不测 Agent 路径。  
  ✅ multi_kb 有关过采样、RRF 差异。

**延伸追问 (L2/L3)：**

- **L2：** `retrieval_profile_override` 参数在哪些 API 可用？
- **L3：** 离线评测集如何自动化选 Profile？

---

### Q15：NebulaGraph 挂掉时系统行为是什么？

**一句话结论：** Path-B/D 依赖图谱，故障时召回**降级为 Path-C 向量为主**（Path-A 若开仍可用）；LightRAG 初始化 Nebula 失败可能导致 KB 实例化报错或跳过图谱写入；**无自动 Profile fallback**，需运维关 Path-B/D 或 env 覆盖。

**为什么问这个/背景：**
高可用与降级策略题；考察是否读过 Path 依赖与运维手段。

**详细解释：**

```
Nebula 健康
    Path A (Milvus) ──┐
    Path B (Graph)  ──┼──► 完整召回
    Path C (Vector) ──┤
    Path D (扩展)   ──┘

Nebula 故障
    Path B/D 失败/空结果
    Path A/C 仍可能工作
         │
         ▼
    召回降级：语义向量为主，关系链丢失
    ingest 侧：新关系无法写入
```

| 场景 | 行为 |
|------|------|
| 运行时查询失败 | B/D 空，A/C 继续 |
| 初始化连接失败 | KB 实例化可能失败或 no-graph 模式 |
| domain Profile | 实体扩展弱，专业 recall 下降 |
| general_qa | 影响较小（本就弱依赖图谱） |

**在本项目中的实现：**

- Path 开关：`RetrievalProfile.enable_path_b/d`
- Storage：`NebulaGraphStorage` in `rag/service.py`
- **未实现**自动检测 Nebula 并切换 Profile

**走一遍具体例子：**

Nebula 集群维护 2 小时：

1. domain 库 Query「某标准规定的限值」→ Path-B 关系链断 → 仅靠 Path-C 语义，可能漏标准号精确匹配。
2. 运维临时：`RAG_RETRIEVAL_PROFILE=lightweight` 或改 Profile 关 path_b/d（需代码/配置支持）。
3. ingest 暂停写图，向量 ingest 仍可进行（视实现分支）。

**生产建议：**

- 监控 Nebula 连接延迟与错误率
- domain 库准备 feature flag 关 Path-B/D
- 图谱恢复后跑 sync 补关系

**常见误区：**

- ❌ 认为 Nebula 挂掉服务全 500。  
  ✅ 多数检索请求降级完成。
- ❌ 认为 Milvus 与 Nebula 同生共死。  
  ✅ 独立故障域。

**延伸追问 (L2/L3)：**

- **L2：** DirectRetriever 对 Nebula 异常是 fail-open 还是 fail-closed？
- **L3：** 如何设计「图谱可选」的 Profile 热切换而不重启 worker？

---

## L3 补充 🔴

### Q16：PPR（Personalized PageRank）在本项目 RAG 里做什么？

**一句话结论：** Stage-2 **FULL** 模式下，PPR 以 Stage-1 种子为起点在图谱/异构图上扩散重排 chunk；**domain Profile 的 MMR_ONLY 跳过 PPR** 以换延迟。

| Profile | PPR |
|---------|-----|
| default FULL | ✅ |
| domain MMR_ONLY | ❌ |
| general_qa RAW | 跳过 Stage-2 |

**实现：** `rag/content_filter/ppr_filter.py`

---

### Q17：Path-D 图扩展（graph_expander）如何工作？

**一句话结论：** 从 Path-A/B 种子实体在 NebulaGraph 扩展 K 跳邻域，补关系链召回；Nebula 故障时 Path-D 为空，召回降级。

```text
┌────────────┐
│ 种子实体       │
└────────────┘

  种子实体 ──────────────► GE

  GE ──────────────► NG

  NG ──────────────► 并入融合池

```

**实现：** `rag/retriever/graph_expander.py`

---

### Q18：关键词索引如何初始化与维护？

**一句话结论：** ingest 后 KeywordIndexManager 建 BM25+实体 ANN；API lifespan 预热活跃 KB；`rag/sync/` 定时对账。

| 时机 | 动作 |
|------|------|
| ingest 成功 | 建/更新 keyword_index |
| API 启动 | 预热缓存 |
| 运维 | keywords 管理 API 触发重建 |

**延伸：** 子线程无法查 DB 时 `_KNOWN_KB_PROFILES` 硬编码 kb_id→Profile。
