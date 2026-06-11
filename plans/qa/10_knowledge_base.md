# 10 · 知识库管理

> 📊 完整 ASCII 图见 [00_architecture_diagrams.md](./00_architecture_diagrams.md) 图 15、18、**27**；正式文档 → [infrastructure_module.md](../../docs/docs_backend/infrastructure_module.md) · [agent_kb_binding_module.md](../../docs/docs_backend/agent_kb_binding_module.md)

> 本模块答案采用**分层讲解**：先给结论，再解释「为什么」，最后用项目里的真实代码和场景串起来。

---

## L1 初级 🟢

### Q1：知识库相关的核心数据表有哪些？

**一句话结论：** KB 元数据与文档关系在 **`knowledge_bases`、`knowledge_base_documents`、`knowledge_base_directories`、`kb_pending_files`、`agent_knowledge_bases`** 等表；通用文档处理还有 **`documents`、`tasks`**（Kafka Worker 链路）。

#### 背景

RAG 不是「只有一个向量库」。MySQL 存**业务真相**（哪个文件属于哪个 KB、入库到哪一步），Milvus/Nebula 存**检索索引**。表设计体现这条链路。

#### 详细解释：表职责

**KB 入库与绑定链路（ASCII 架构图）：**

```text
┌────────────┐
│ 用户上传 PDF   │
└────────────┘

  用户上传 PDF ──────────────► PF

  PF ──────────────► IW

  IW ──────────────► KBD

  KBD ──────────────► (Milvus)

  (Milvus) ──────────────► AKB

  AKB ──────────────► multi_kb_search

  PF ──(status 0→1)──► IW

```

```
用户上传 PDF
      │
      ▼
┌──────────────────┐     status 0→1     ┌────────────────────┐
│ kb_pending_files │ ─────────────────► │ Ingest Worker       │
└──────────────────┘                    └─────────┬──────────┘
                                                  │
              ┌───────────────────────────────────┼──────────────┐
              ▼                                   ▼              ▼
    knowledge_base_documents              Milvus           NebulaGraph
              │                         (向量索引)           (图谱)
              ▼
    agent_knowledge_bases ←── Agent 绑定 kb_ids
              │
              ▼
         multi_kb_search 检索
```

| 表/模型 | 用途 |
|---------|------|
| `KnowledgeBase` | KB 元数据：名称、kb_code、状态 |
| `KnowledgeBaseDocument` | KB 内已入库文档记录，关联 doc_id、file_path |
| `KnowledgeBaseDirectory` | KB 内目录树结构 |
| `PendingFile`（`kb_pending_files`） | 待入库队列，**status 驱动 Ingest Worker** |
| `AgentKnowledgeBase` | Agent 与 KB 多对多绑定 |
| `Document` | 通用文档实体（Kafka 解析链路） |
| `Task` | 异步任务状态（含深度研究，非 KB 专属） |
| `ExperimentDataset` | 实验数据集（Excel 分析等，可选关联） |

```text
multi_kb_search / explore / rag.py
        │
        ▼
┌─────────────────────────┐
│ get_kb_manager()        │  ← event loop 本地单例
│ (threading.local)       │
└──────────┬──────────────┘
           │
           ▼
┌─────────────────────────┐
│ KnowledgeBaseManager    │
│ ├── RAGInstance 缓存     │  {kb_id → LightRAG 懒加载}
│ ├── retrieve_context()  │  统一检索入口
│ ├── KeywordIndexManager │
│ └── asyncio Lock        │
└──────────┬──────────────┘
           │
     ┌─────┴─────┐
     ▼           ▼
  Milvus     NebulaGraph + Redis KV
```



| 职责 | 说明 |
|------|------|
| KB CRUD | 创建、删除、切换当前 KB |
| RAG 实例缓存 | `{kb_id: RAGInstance}` 懒加载 |
| 检索入口 | `retrieve_context()` — multi_kb、explore 共用 |
| 关键词 | `KeywordIndexManager`、`PerKBKeywordExtractor`、BM25 |
| 内容过滤 | 与 `ContentFilterManager`、RetrievalProfile 协作 |
| 并发安全 | asyncio Lock |
| loop 隔离 | `threading.local` + `WeakKeyDictionary` 按 loop 存 Manager |

#### 项目实现

```python
def get_kb_manager() -> KnowledgeBaseManager:
    loop = asyncio.get_running_loop()
    # loop_managers[loop] = KnowledgeBaseManager()
```

子 Agent 独立 event loop 必须拿**自己的 Manager**，否则 `Future attached to a different loop`。

#### 具体例子

`multi_kb_search` 内部：

```
execute_multi_kb_search(kb_ids=[A, B])
  → 每 kb_id get_kb_manager().retrieve_context(...)
  → 并发 asyncio gather
  → 各 KB 用缓存 RAGInstance，last_used_at 更新
```

#### 常见误区

- ❌ 「get_kb_manager 全局单例一个进程一个」→ **每个 event loop 一个**。
- ❌ 「Manager = get_rag_service()」→ 相关但分层不同；service 更偏对外 API 封装。
- ❌ 「retrieve_context 同步阻塞」→ async 路径为主，注意线程池调用的 loop 规则。

#### 延伸追问（L2/L3）

- L2：Q6 为何 event loop 本地？Q8 kb_code 与 Profile？

---

### Q3：知识库从上传到可检索，经历哪些阶段？

**一句话结论：** **上传 MinIO → MySQL 记录 → [可选] Kafka Task OCR/解析 → 用户触发 ingest（pending status=1）→ Ingest Worker 向量化 → Milvus + Nebula + keyword → sync 调度对账 → Agent multi_kb_search 可检索**。

#### 背景

「上传成功」≠「能搜到」。产品要设置用户预期，运维要知卡在哪一阶段。

#### 详细解释：阶段图

```
① 用户上传文件
     → MinIO 对象存储
     → kb_pending_files 插入，status=0（已上传，待确认入库）

② [可选] 文档增强链路
     → Kafka Task Worker：OCR（MinerU/Docling）、抽取
     → 更新 Document / FileInfo parse_status

③ 用户/API 触发「开始入库」
     → uploads.py: status=1（待 Ingest Worker 处理）

④ Ingest Worker / StartupTaskProcessor
     → Redis 锁 kb_ingest_lock:{file_id}
     → MD 直传 or PDF→MinerU 路由
     → LightRAG ingest：chunk、embedding、图谱边

⑤ 索引层
     → Milvus 向量 collection
     → NebulaGraph（若启用）
     → KeywordIndexManager 建 keyword_index（Path-A）

⑥ 对账（异步）
     → rag/sync/scheduler 修复 drift

⑦ 在线检索
     → multi_kb_search / retrieve_context / explore_learning
```

```text
┌──────────────────┐
│ Uploaded         │
└──────────────────┘
    ▲  用户上传
    │
    ▼

  (status=0 已上传)
    │
    ▼
┌──────────────────┐
│ Pending          │
└──────────────────┘

  (status=1 触发 ingest)
    │
    ▼
┌──────────────────┐
│ Triggered        │
└──────────────────┘

  (status=2 入库成功)
    │
    ▼
┌──────────────────┐
│ Success          │
└──────────────────┘

  (status=3 失败)
    │
    ▼
┌──────────────────┐
│ Failed           │
└──────────────────┘

  (人工重试)
    │
    ▼
┌──────────────────┐
│ Triggered        │
└──────────────────┘

  (Milvus + 图谱 + keyword)
    │
    ▼
┌──────────────────┐
│ Indexed          │
└──────────────────┘

  (Agent 可检索)
    │
    ▼
┌──────────────────┐
│ Searchable       │
└──────────────────┘


```

```text
┌────────────┐
│ UP         │
└────────────┘

  UP ──────────────► MinIO

  MinIO ──────────────► MYSQL

  MYSQL ──────────────► IW

  IW ──────────────► RA

  RA ──────────────► (Milvus)

  (Milvus) ──────────────► (NebulaGraph)

  (NebulaGraph) ──────────────► keyword_index

```

#### 项目实现

- 上传：`api/routers/uploads.py`
- Task：`worker/task_worker.py`
- Ingest：`worker/ingest_worker.py` + `PendingIngestProcessor`
- 检索：`multi_kb_search_tool.py` + Manager.retrieve_context

#### 具体例子

扫描版 PDF：

```
上传 status=0
点入库 status=1
ingest route=mineru（非 md-direct）
MinerU 解析 3min → embedding → status=2
multi_kb_search 可返回 chunk
若 keyword 未建：Path-A 弱，sync scheduler 后续补
```

#### 常见误区

- ❌ 「上传完立即可搜」→ 需 status=2 且索引就绪。
- ❌ 「Task Worker 做完就能搜」→ OCR 后还需 **ingest 向量**。
- ❌ 「ingest 成功 = keyword 就绪」→ 可能需 sync 补建（见 07-Q15）。

#### 延伸追问（L2/L3）

- L3：Q13 入库失败排障？Q15 绑定变更要不要重启？

---

### Q4：Agent 如何知道该查哪些知识库？

**一句话结论：** 三层决定：**API 请求 `kb_ids` 显式传入 → Agent-KB DB 绑定默认值 → 工具层 DEFAULT_KB_ID 等常量兜底**；Router 把 kb_ids 传给 Agent 与 `ToolAssemblyPipeline` 闭包内的 multi_kb_search。

#### 背景

多产品（NE/CSH）、多 KB（教材库、论文库、CSH 修复库）并存，不能写死单一 kb_id 在 Prompt 里。

#### 详细解释：三层绑定

```
层 1 — API 显式（优先级最高）
  QueryRequest.kb_ids: ["<kb_id_ne_...>", "<kb_id_paper_...>"]
  前端 KB 多选器

层 2 — Agent 配置
  db/init_agent_bindings.py 初始化 AgentKnowledgeBase
  某 Agent 默认绑 nuclear + paper

层 3 — 工具常量兜底
  multi_kb_search_tool.py:
    DEFAULT_KB_ID   # 核电子学
    PAPER_KB_ID     # 期刊论文
  kb_ids 为空时 ToolAssembly 可能 **禁用** multi_kb_search
```

```text
┌───────────────────────┐  ┌─────────────────────┐  ┌──────────────────┐
│ AgentKnowledgeBase 绑定 │  │ QueryRequest.kb_ids │  │ DEFAULT_KB_ID 常量 │
└───────────────────────┘  └─────────────────────┘  └──────────────────┘
         (默认)
           │
           ▼
         (优先级最高)
           │
           ▼
         (兜底)
           │
           ▼
┌──────────────────────────────────────┐
│ TA                                   │
└──────────────────────────────────────┘
         (kb_ids 非空)
           │
           ▼
         (kb_ids 为空)
           │
           ▼
┌────────────────┐  ┌────────────────────┐
│ MK             │  │ 禁用 multi_kb_search │
└────────────────┘  └────────────────────┘
           │
           ▼
┌──────────────────────────────────────┐
│ retrieve_context                     │
└──────────────────────────────────────┘

```

#### 项目实现

```python
# tool_assembly.py
if self._kb_ids:
    tools_by_name["multi_kb_search"] = self._create_multi_kb_search_tool()
else:
    tools_by_name.pop("multi_kb_search", None)
```

Explore 服务：`TopicsRequest.kb_ids` 取首个做 retrieve_context。

#### 具体例子

CSH 前端只传 CSH kb_id：

```
kb_ids=["<kb_id_csh>"]
RetrievalProfile → domain（_KNOWN_KB_PROFILES）
multi_kb 只查 CSH 库，不会误查 NE 库
```

未传 kb_ids + Agent 无绑定：

```
ToolAssembly 日志：已禁用 multi_kb_search（kb_ids 为空）
Agent 只能靠联网或纯模型（若开启）
```

#### 常见误区

- ❌ 「DEFAULT_KB_ID 总是生效」→ kb_ids 为空时工具可能**不注入**。
- ❌ 「Explore 与 Agent 绑定相同」→ Explore 靠请求 kb_ids，默认可能取 settings。
- ❌ 「改绑定立即影响进行中的 SSE」→ 下一请求生效；RAG 缓存可能仍热。

#### 延伸追问（L2/L3）

- L2：Q8 kb_code 与 Profile？L3：Q14 KB 级 A/B？

---

## L2 中级 🟡

### Q5：`PendingFile` 状态机是怎样的？

**一句话结论：** **`status=0` 已上传待触发 → `1` 已触发待/正在 ingest → `2` 成功 → `3` 失败**；Ingest Worker 只处理 **status=1**，靠 **Redis 锁** 防多 Worker 重复。

#### 背景

`PendingFileStatus` 枚举名与业务口语略有出入：枚举中 `PROCESSING=1`，业务上表示「已进入入库队列/处理中」，不是 Kafka 那种 processing 任务 ID。

#### 详细解释：状态转换

| status | 枚举 | 含义 | 谁设置 |
|--------|------|------|--------|
| 0 | PENDING | 文件在 MinIO，用户未点入库 | 上传 API |
| 1 | PROCESSING | 用户触发 ingest，等待 Worker | `uploads.py` update status=1 |
| 2 | COMPLETED | 向量/索引入库成功 | PendingIngestProcessor |
| 3 | FAILED | 失败，error_message | Processor 异常处理 |

**不允许：** status=1 时用户 delete pending（`uploads.py` 400「正在入库中」）。

#### 项目实现

```python
# ingest_worker.py
.where(PendingFile.status == 1)

# ingest_pending_processor.py
if pending_file.status != 1: skip
Redis SET NX kb_ingest_lock:{file_id} EX 7200
```

失败人工重跑：DB 把 status 从 3 改回 1（运维操作，需确认无重复 chunk）。

#### 具体例子

```
上传 10 文件 → 10 行 status=0
批量「开始入库」→ 10 行 status=1
ingest-worker 2 实例 + Redis 锁 → 并行不同 file_id
9 成功 status=2，1 失败 status=3
管理 UI 对 status=3 显示「重试」→ 运维改 1
```

#### 常见误区

- ❌ 「status=1 是待上传」→ 0 才是待触发；1 是**已触发**。
- ❌ 「多 Worker 靠 DB 行锁」→ 主要靠 **Redis NX**。
- ❌ 「COMPLETED 后 pending 行删除」→ 通常**保留**作审计，文档在 knowledge_base_documents。

#### 延伸追问（L3）

- Q7 ingest 重复？Q13 一直 status=1 卡死？

---

### Q6：为什么 KB Manager 要「event loop 本地」？

**一句话结论：** LightRAG/asyncio 锁/连接绑定在 **创建它们的 event loop** 上；子 Agent、线程池若共享跨 loop 的 Manager → **`Future attached to a different loop`** 经典崩溃。

#### 背景

FastAPI 主 loop 跑 SSE；子 Agent 可能 `asyncio.new_event_loop()` 在独立线程。Python asyncio **不支持**跨 loop await 同一 Future。

#### 详细解释

**问题链：**

```
主 loop 创建 KnowledgeBaseManager + RAGInstance
  → 子线程新 loop 调用同一 Manager.retrieve_context
  → await 主 loop 上创建的 Lock/Future
  → RuntimeError: Future attached to a different loop
```

**解法：**

```python
_kb_manager_local = threading.local()
loop_managers = weakref.WeakKeyDictionary()  # loop → Manager
# 每个 running loop 懒创建独立 KnowledgeBaseManager
```

sync 上下文（无 running loop）用 `_kb_manager_local.sync_manager`  fallback。

#### 项目实现

与 `db` NullPool、子 Agent thread 模式同族问题；`get_rag_service()` 也需注意 loop/worker 边界。

#### 具体例子

```
主 Agent await multi_kb_search  → 主 loop Manager → OK
task_tool 派发子 Agent 在线程 loop
  → 子 Agent 内 multi_kb_search
  → 子 loop 新建 Manager → 独立 RAG 缓存（内存重复但正确）
```

#### 常见误区

- ❌ 「weakref 会自动释放 RAG」→ 只 weakref **loop 键**；RAG 实例仍占内存直到 loop GC。
- ❌ 「多 Uvicorn worker 共享 Manager」→ **每进程每 loop** 各一份。
- ❌ 「用 run_in_executor 就安全」→ executor 里若 wrongly share async 对象仍错。

#### 延伸追问（L3）

- Q12 多 worker 内存 × N 如何优化 LRU？

---

### Q7：关键词索引（Keyword Index）在 KB 生命周期中的位置？

**一句话结论：** **入库后构建 keyword_index（实体向量索引）**供 Stage-1 Path-A 召回；**查询时** hybrid 提取高/低阶关键词；二者共同决定 multi_kb 日志里「向量搜索」次数与延迟。

#### 背景

RAG 不是纯向量 cosine。本项目 Stage-1 多路径（Path-A 实体、Path-B/C/D），Path-A 依赖 **keyword 图 + keyword_index**。

#### 详细解释：构建 vs 查询

| 阶段 | 做什么 | 组件 |
|------|--------|------|
| **Ingest 后** | 建 keyword_index、BM25 | `KeywordIndexManager` |
| **API 启动** | 预热活跃 KB keyword 图缓存 | lifespan / warmup |
| **查询时** | Per-KB 或共享 hybrid 关键词提取 | `PerKBKeywordExtractor` |
| **Path-A** | 整句向量 + top hl 词各打 keyword_index | retrieve_context 内 |

multi_kb 优化：仅首 KB 提取关键词；`MULTI_KB_PATH_A_QUERY_ONLY=1` 减少 keyword_index 调用次数。

#### 项目实现

`rag/keywords/index_manager.py`、`extractor.py`、`bm25_index.py`

Manager.retrieve_context 根据 `RetrievalProfile.enable_path_a` 决定是否走 Path-A。

#### 具体例子

ingest 成功但 keyword 构建失败：

```
Milvus 有向量 → Path-B 可能仍有结果
Path-A 日志无「keyword_index 返回 N 条」
multi_kb 召回变弱、延迟可能反而低
rag/sync scheduler 下一周期补 keyword
```

#### 常见误区

- ❌ 「关键词提取 = 关键词索引」→ 提取是**查询时** LLM/hybrid；索引是**入库后**离线建。
- ❌ 「关 Path-A 就无需 keyword index」→ domain Profile 通常 **开 Path-A**。
- ❌ 「keyword 存在 MySQL」→ 主要在内存/Redis/图结构，视实现。

#### 延伸追问（L3）

- 场景 A 双库慢：keyword_index 次数排查（08 附录）

---

### Q8：`kb_code` 与 `RetrievalProfile` 如何关联？

**一句话结论：** Manager 查 KB 元数据得 **`kb_code`**，经 **`_KB_CODE_PREFIX_PROFILES` 前缀映射**到 Profile 名（general_qa/domain/lightweight/default），决定 Path 开关与 Stage-2 过滤；子线程无法查 DB 时用 **`_KNOWN_KB_PROFILES` 按 kb_id 硬编码兜底**。

#### 背景

不同 KB 数据特性不同：百科型 HotpotQA 适合关 Path-A、RAW 过滤；垂直核电子库适合 domain + MMR_ONLY。不能所有 KB 走同一套 FULL 流水线。

#### 详细解释

```python
# retrieval_profile.py
_KB_CODE_PREFIX_PROFILES = (
    ("hotpotqa_fullwiki", "general_qa"),
    ("rag_nuclear_electronics", "domain"),
    ("nuclearelectronicspaperrepository", "domain"),
)
_KNOWN_KB_PROFILES = {
    "<kb_id_ne>": "domain",  # NE
    "<kb_id_paper>": "domain",  # paper
    "<kb_id_csh>": "domain",  # CSH
}
```

| Profile | Path-A | Stage-2 过滤 | 适用 |
|---------|--------|--------------|------|
| general_qa | 关 | RAW | 百科多跳 |
| domain | 开 | MMR_ONLY | 核电子/CSH 垂直 |
| lightweight | 部分关 | MMR_ONLY | 低延迟 |
| default | 全开 | FULL | 兼容旧行为 |

```text
┌─────────────────────┐  ┌───────────────────────────┐
│ 知识库 kb_id / kb_code │  │ RAG_RETRIEVAL_PROFILE env │
└─────────────────────┘  └───────────────────────────┘
           │
           ▼
┌──────────────────────────────────────┐
│ Map                                  │
└──────────────────────────────────────┘
         (hotpotqa*)
           │
           ▼
         (rag_nuclear_* / CSH)
           │
           ▼
         (轻量场景)
           │
           ▼
         (默认)
           │
           ▼
┌────────────────────┐  ┌─────────────────────────────┐  ┌────────────────────────────┐  ┌─────────────────────────────┐
│ default / FULL 流水线 │  │ domain / 开Path-A / MMR_ONLY │  │ general_qa / 关Path-A / RAW │  │ lightweight / MMR_ONLY 无过采样 │
└────────────────────┘  └─────────────────────────────┘  └────────────────────────────┘  └─────────────────────────────┘

```

#### 项目实现

`resolve_retrieval_profile(kb_code, kb_id)` 逻辑在 `retrieval_profile.py`；Manager 检索前解析 Profile 调整 `DirectRetrieverConfig`。

子 Agent 线程无 DB：靠 `_KNOWN_KB_PROFILES[kb_id]`。

#### 具体例子

NE 教材库 kb_code=`rag_nuclear_electronics`：

```
→ profile domain
→ enable_path_a=True, content_filter MMR_ONLY
→ 比 default FULL 快、垂直精度更好
误配 default → P95 升高、日志 Path-A 次数暴增
```

#### 常见误区

- ❌ 「Profile 存在 MySQL」→ 主要靠 **code 前缀 + 硬编码 kb_id**。
- ❌ 「改 kb_code 无影响」→ 直接换 Profile 策略。
- ❌ 「lightweight Agent 自动 lightweight Profile」→ Agent 模式与 RetrievalProfile **独立**，需分别配置。

#### 延伸追问（L3）

- Q14 KB 级 A/B 不同 Profile？

---

### Q9：知识库 API 路由有哪些？

**一句话结论：** KB 管理分散在 **`api/routers/knowledge_base*.py`、`document_*.py`、`uploads.py`、`rag.py`**；v2 Agent **不管理 KB**，但 query 带 **`kb_ids` 消费** KB 内容。

#### 背景

历史演进导致路由拆分：CRUD、上传、ingest、直连 RAG 遗留 QA 各一组。面试要能**按能力找路由**，而非背全路径。

#### 详细解释

| 路由模块 | 能力 |
|----------|------|
| `knowledge_base.py` 及变体 | KB CRUD、目录树、文档列表 |
| `uploads.py` | 批量上传、pending 列表、触发 ingest status=1、ingest 进度 |
| `document_*.py` | 文档处理、抽取、ingestion 状态 |
| `document_ingestion.py` | parse_status/storage_status legacy 链路 |
| `rag.py` | 直连检索、遗留 QA |
| `api/v2/agent.py` | Agent 问答，参数 `kb_ids` |
| `api/v2/explore.py` | 探索学习，参数 `kb_ids` |

认证：`verify_api_key` / CAS session；KB 操作需 admin 或 tenant 权限（视部署）。

#### 项目实现

OpenAPI `/docs` 可扫 tag：`knowledge_base`、`uploads`、`explore`。

Agent v2 **刻意不**暴露 KB 管理，避免访客面过大。

#### 具体例子

运维重跑 failed pending：

```
GET  /uploads/pending?status=3     # 查失败
POST /uploads/.../start-ingest     # 或 DB 改 status=1
ingest-worker 日志跟踪 file_id
```

#### 常见误区

- ❌ 「Agent API 能删 KB」→ 管理在 admin 路由。
- ❌ 「rag.py 是主检索入口」→ 在线 Agent 主路径是 **tool multi_kb_search**。
- ❌ 「uploads 与 document 上传重复」→ 产品场景不同，可能并存 legacy。

#### 延伸追问

- uploads status 与 document parse_status 两套状态如何对齐？

---

### Q10：会话上传文件 vs 知识库文件，检索路径有何不同？

**一句话结论：** **KB 文件**走 Milvus+图谱+keyword、**multi_kb_search**、持久多用户共享；**会话文件**走内存 BM25+TF-IDF、**chat_file_search**、仅当前 thread、**不调 embedding**。

#### 背景

用户临时上传课程 PDF 不应强制走完整 ingest（分钟级）；但问「刚传的文件」必须能答。会话索引是 **轻量妥协**。

#### 详细解释

| 类型 | 索引 | 工具 | 存储 | 生命周期 |
|------|------|------|------|----------|
| **知识库** | Milvus + Nebula + keyword | `multi_kb_search` | MinIO 持久 | 全租户共享 |
| **会话文件** | 内存 BM25+TF-IDF | `chat_file_search` | `chat_file_index.py` LRU | thread 级 |

#### 项目实现

```python
# tool_assembly.py
if self._thread_id and has_thread_file_index(self._thread_id):
    tools_by_name["chat_file_search"] = create_scoped_chat_file_search_tool(thread_id)
```

`services/chat_file_index.py` 解析上传文件建倒排索引，scope 限定 thread_id。

#### 具体例子

```
场景 A：「核电子学教材里放大器原理」→ multi_kb_search（KB）
场景 B：「我上传的实验报告结论是什么」→ chat_file_search（session）
场景 C：两者都有 → ToolAssembly 可能注入两个工具，Prompt 引导区分
```

#### 常见误区

- ❌ 「会话文件会自动 ingest 进 KB」→ **不会**，除非走 KB 上传流程。
- ❌ 「chat_file 能搜图片 PDF 扫描件」→ 依赖文本提取质量，无 OCR 则空。
- ❌ 「chat_file 结果进 Milvus」→ **纯内存**，重启 thread 可能需 re-upload。

#### 延伸追问（L3）

- 06-Q13 双工具共存与 Router 优化？

---

## L3 高级 🔴

### Q11：删除知识库时，存储层如何协调清理？

**一句话结论：** 删 KB 需协调 **MySQL 元数据、Milvus collection/向量、Nebula 子图、Redis KV、本地 data/ 缓存、keyword 内存缓存** 六层；任一步失败会产生 **orphan 向量或占空间查不到**。

#### 背景

分布式存储无跨系统 ACID 事务。删 KB 是 **分布式垃圾回收**问题，需要补偿与对账。

#### 详细解释：清理顺序（理想）

```
1. 标记 KB deleting（防新 ingest）
2. 停止 RAGInstance 缓存引用
3. Milvus drop collection 或 delete by kb_id
4. Nebula 删子图/空间（若启用）
5. Redis 删 LightRAG 相关 key
6. 本地 cache/data 目录
7. MySQL 删 knowledge_base_documents、pending、绑定行
8. 最后删 knowledge_bases 行
9. keyword IndexManager invalidate
```

**风险：**

| 失败点 | 后果 |
|--------|------|
| Milvus 删失败 | orphan 向量占空间 |
| MySQL 先删 | 无法定位 Milvus 删哪些 |
| 缓存未 invalidate | 旧实例仍响应检索 |

#### 项目实现

具体 delete 实现见 `KnowledgeBaseManager.delete` / `rag/service`（程度因版本而异）——面试应提 **事务日志 + 补偿任务** 作为改进。

#### 具体例子

```
运维删 KB「测试库」
MySQL 行 gone，Milvus collection 仍在
→ multi_kb 若误绑旧 kb_id 可能 404 或空
→ 需脚本按 naming convention 扫 orphan collection
```

#### 常见误区

- ❌ 「删 MySQL 就够」→ 向量库必须同步。
- ❌ 「LightRAG 自动 GC」→ 需显式调 delete API。
- ❌ 「pending 文件自动清」→ 需 FK cascade 或 manual。

#### 延伸追问

- 软删除 vs 硬删除对产品/合规的影响？

---

### Q12：多 KB 实例缓存的内存策略？

**一句话结论：** 活跃 KB **懒加载 + API 预热**；inactive 实例可能**长期驻留**；优化方向 **LRU eviction、weakref、RAG 检索 sidecar 进程分离**。

#### 背景

每个 LightRAG 实例含图结构、缓存、连接——可能 **数百 MB**。API 4 worker × 3 热 KB ≈ 灾难级内存。

#### 详细解释

**现状：**

- `RAGInstance` 缓存于 Manager `_rag_instances`
- `last_used_at` 字段存在，完整 LRU 未必启用
- Manager 中部分 **weakref** 用于 loop 键，非 RAG 本体

**优化方向：**

| 策略 | 说明 |
|------|------|
| LRU eviction | 超 N 个 KB 踢最久未用 |
| TTL | 30min 无 retrieve 则 drop |
| 弱引用 RAG | 仅 strong ref 在 retrieve 期间 |
| Sidecar | 检索专进程，API worker 无 RAG 内存 |
| 共享 Redis | 冷 KB 状态外置 |

#### 项目实现

`get_rag_service()` 单例 + `http_server` lifespan 预热活跃 KB。

多 Uvicorn worker → 每进程各预热一份（08-Q12）。

#### 具体例子

```
10 个 KB，用户只用 2 个
无 eviction → 10 实例全在内存
加 LRU max=3 → 只保留最近 3 个 kb_id 实例
第 4 个 KB 冷启动延迟 ~5s（可接受 trade-off）
```

#### 常见误区

- ❌ 「Milvus 在外就不占 API 内存」→ LightRAG **图+缓存**仍在进程内。
- ❌ 「weakref loop = RAG weakref」→ 仅 loop 映射 weak。
- ❌ 「重启 API 是唯一清缓存办法」→ 应 programmatic invalidate。

#### 延伸追问

- 预热列表从哪来——Agent 绑定 vs 访问日志？

---

### Q13：入库失败常见原因与排障？

**一句话结论：** 看 **pending status=3、ingest_worker.log、ingest.log、Milvus/Nebula 连通性、MinerU 路由、keyword 构建**；现象表驱动排查。

#### 背景

「入库失败」用户只看 UI 红点，后端有十多种根因。系统化表减少盲目重启。

#### 详细解释：现象 → 原因

| 现象 | 可能原因 |
|------|----------|
| pending **一直 status=1** | Ingest Worker 未启动；Redis 锁死；StartupTaskProcessor 与 Worker 互抢全 skip |
| **Milvus upsert 失败** | collection schema 不匹配；LightRAG 版本 patch 不一致 |
| **图谱写入失败** | Nebula 连接/space 不存在 |
| **解析为空** | MinerU/Docling 不支持格式；PDF 加密 |
| **检索无结果** | ingest 成功但 **keyword 索引未建**；Profile 关 Path-A |
| **status=3** | 看 error_message 列 + ingest.log stack trace |

**日志路径：**

```
logs/ingest_worker.log   — 抢锁、route=md-direct/mineru
logs/ingest.log          — embedding、Milvus、[KB xxx]
grep file_id / kb_id
```

#### 项目实现

`PendingIngestProcessor` MD 与 PDF 分支；失败 `update status=3`。

#### 具体例子

```
现象：status=2 但 Agent 搜不到
→ ingest.log 有 upsert success
→ 无 keyword_index build log
→ 等 rag/sync 或手动 trigger rebuild
→ 或 MULTI_KB_PATH_A_QUERY_ONLY 误解为「无结果」
```

#### 常见误区

- ❌ 「失败就删 pending 重传」→ 先查 MinIO 是否已有对象，避免 duplicate chunk。
- ❌ 「只重启 ingest-worker」→ Redis/MySQL/Milvus 任一挂都会 fail。
- ❌ 「MinerU 失败会 status=2」→ 应 status=3。

#### 延伸追问

- 如何 batch 重试 status=3 且 error 含 timeout 的行？

---

### Q14：如何做 KB 级别的 A/B（不同 RetrievalProfile）？

**一句话结论：** 实践路径：**复制/shadow kb_id**、在 **`_KNOWN_KB_PROFILES` 或 DB 元数据扩展 `retrieval_profile` 字段**、用 **`datasets` 评测集**对比 recall@k/P95/引用准确率；当前 Profile **主要靠 code 前缀 + 硬编码 kb_id**。

#### 背景

domain Profile 来自消融实验，但新 KB（如 CSH 新库）可能需验证是否仍适用 default。

#### 详细解释：A/B 步骤

```
1. 准备评测集 ExperimentDataset（datasets 路由 CRUD）
2. Shadow KB：同文档 ingest 两份 kb_id A/B
3. A: profile domain, B: profile default（改 _KNOWN_KB_PROFILES）
4. 批跑 retrieve_context fixed queries
5. 指标：recall@5、P95 latency、citation 人工判分
6. Agent 端到端：固定 prompt 看是否引用正确文献名
7. 环境开关：RETRIEVAL_PROFILE_OVERRIDE_B=general_qa（若实现）
```

**长期：** DB `knowledge_bases.retrieval_profile` 列，Manager 读 DB 而非硬编码。

#### 项目实现

`api/routers/datasets.py`、`ExperimentDataset` 模型。

`retrieval_profile.py` 的 `@lru_cache` 与环境变量 override（若有）。

#### 具体例子

```
KB「CSH-试验」两种 Profile 各跑 100 题
domain: recall@5=0.78, P95=1.2s
default: recall@5=0.81, P95=2.8s
→ 选 domain，牺牲 0.03 recall 换延迟
```

#### 常见误区

- ❌ 「只 A/B Agent Prompt」→ Profile 变则 **retrieve 层**变，应分层测。
- ❌ 「线上直接改 _KNOWN_KB_PROFILES 无风险」→ 需 shadow，防 production 回归。
- ❌ 「A/B 不需固定 kb_ids」→ 必须隔离 KB 内容相同。

#### 延伸追问

- 08-Q13 线上 SLI 与 offline A/B 如何关联？

---

### Q15：Agent-KB 绑定变更后，是否需要重启服务？

**一句话结论：** **MySQL 绑定**下次请求读 DB **即生效**（若 API 动态读）；**RAG 实例缓存**对新 KB 首次访问才实例化，**删 KB 需 invalidate**；`init_agent_bindings` 只是**初始化脚本**，非运行时热更新源。

#### 背景

运维常问：「给 Agent 加了新 KB，要不要 restart？」取决于**绑定读路径**与**缓存状态**。

#### 详细解释

| 变更类型 | 是否需重启 | 说明 |
|----------|------------|------|
| 新增 Agent-KB 绑定 | 通常**不需要** | 下请求 kb_ids 或 binding 读 DB |
| 新 KB 首次检索 | 冷启动延迟 | 自动 `get_kb_manager` 实例化 |
| 删除 KB 绑定 | 建议 **invalidate 缓存** | 旧 RAGInstance 可能仍占内存 |
| 删除 KB 本身 | **必须**清理缓存+存储 | 见 Q11 |
| 改 RetrievalProfile 硬编码 | **需重启** | 若仅代码 `_KNOWN_KB_PROFILES` |
| init_agent_bindings 脚本 | 一次性 | 生产变更走 admin API/DB |

**建议运维 SOP：**

```
绑定变更后：
1. 调 KB 预热 API（若有）或发一次探测 retrieve
2. 观察 get_kb_manager 日志新 kb_id
3. 若删 KB：restart API worker 或 call invalidate
4. 不必重启 ingest-worker（无关绑定）
```

#### 项目实现

Agent API 从 DB 读 `AgentKnowledgeBase`（具体见 agent router 与 init 脚本）。

RAG warmup 在 `http_server` lifespan。

#### 具体例子

```
给 CSH Agent 增加新 KB 绑定
不重启 → 用户选新 KB 提问
首问 P95 +5s（冷启动）
后续正常
若未绑 kb_ids 且 binding 未生效 → multi_kb 仍 disabled
```

#### 常见误区

- ❌ 「restart 万能」→ 绑定问题多数 **DB 即生效**。
- ❌ 「ingest-worker 要重启才识新 KB」→ ingest 按 kb_id 处理，与 Agent 绑定无关。
- ❌ 「init 脚本可反复跑生产」→ 可能 duplicate binding，应 upsert 逻辑。

#### 延伸追问

- Agent 默认 kb_ids 从 binding 自动填充的 API 在哪一层实现？
