# 00 · 架构图表集（ASCII）

> 本文件汇总项目核心 **ASCII 框线图**（终端友好），各模块文档内嵌精简版，完整版在此查阅。

---

## 1. 系统六层架构

```text
┌─────────────────────────────────────────────────────────────────┐
│ L1 入口层                                                        │
│ cli.py / doc-agent  │  Uvicorn ASGI                             │
└──────────────────────────┬──────────────────────────────────────┘
                           │
┌──────────────────────────▼──────────────────────────────────────┐
│ L2 Web 层                                                        │
│ Middleware: CORS / user_context                                 │
│ http_server :8082  │  guest_http_server :8083                    │
└──────────────────────────┬──────────────────────────────────────┘
                           │
┌──────────────────────────▼──────────────────────────────────────┐
│ L3 路由层                                                        │
│ v1: auth/rag/kb/...  │  v2: agent/csh/explore/guest  │  MCP SSE │
└──────────────────────────┬──────────────────────────────────────┘
                           │
┌──────────────────────────▼──────────────────────────────────────┐
│ L4 Agent 层                                                      │
│ AgentService + WorkflowRegistry (YAML registry.yaml)           │
│ StandardAgentWorkflow │ CoordinatedAgentWorkflow             │
│ ProfiledAgentExecutor │ LightweightQAAgent                   │
│ ProfiledGraphBuilder → create_agent_graph (core/agents)        │
└──────────────────────────┬──────────────────────────────────────┘
                           │
┌──────────────────────────▼──────────────────────────────────────┐
│ L5 能力层                                                        │
│ application/tools │ skills/public │ RAG Service │ Workers/Kafka │
└──────────────────────────┬──────────────────────────────────────┘
                           │
┌──────────────────────────▼──────────────────────────────────────┐
│ L6 存储层                                                        │
│ MySQL │ SQLite checkpoint │ Redis │ Milvus │ NebulaGraph │ MinIO │
└─────────────────────────────────────────────────────────────────┘

```

---

## 2. 双 API 服务对比

```text
                    ┌──────────────────────┐
  RadAI 用户 ──────►│ 主站 :8082           │
                    │ 完整 v1+v2 / KB / MCP│
                    │ checkpoints.db       │
                    └──────────┬───────────┘
                               │
         ┌─────────────────────┼─────────────────────┐
         │                     │                     │
         ▼                     ▼                     ▼
    ┌─────────┐          ┌─────────┐          ┌─────────────┐
    │  MySQL  │          │  Redis  │          │ RAG / LLM   │
    │  users  │          │  Token  │          │   网关      │
    └─────────┘          └─────────┘          └─────────────┘
         ▲                     ▲                     ▲
         │                     │                     │
                    ┌──────────┴───────────┐
  访客 / 外链 ─────►│ 访客 :8083           │
                    │ 登录+问答+会话       │
                    │ guest_checkpoints.db │
                    └──────────────────────┘

```

---

## 3. 问答请求时序（full 模式）

```text
  前端      agent-runtime auth   AgentService WorkflowReg Executor  LangGraph
   │               │            │            │              │             │
   │ POST query    │            │            │              │             │
   ├──────────────►│ Bearer     │            │              │             │
   │               ├───────────►│ Redis token│              │             │
   │               │◄───────────┤ user_id    │              │             │
   │               ├────────────────────────►│ run_stream   │             │
   │               │            │            ├─────────────►│ resolve()   │
   │               │            │            │              ├────────────►│
   │               │            │            │              │ full        ├────────────►│
   │               │            │            │              │             │             ├─► LLM 第1轮
   │               │            │            │              │             │             │◄─ tool_call
   │               │            │            │              │             │             ├─► multi_kb_search
   │               │            │            │              │             │             │      └─► RAG
   │               │            │            │              │             │             │◄─ ToolMessage
   │               │            │            │              │   [ContextPruning JIT]
   │               │            │            │              │             │             ├─► LLM 第2轮
   │               │            │            │              │             │◄────────────┤ stream
   │               │◄───────────┴────────────┴──────────────┴─────────────┤ SSE chunk   │
   │◄──────────────┤ text/event-stream (AgentEvent+SSEEncoder)            │             │

```

---

## 4. Router 三分流决策

```text
                    ┌─────────────────────┐
                    │ 收到 QueryRequest    │
                    └──────────┬──────────┘
                               │
              ┌────────────────▼────────────────┐
              │ task_type=deep_research 或       │
              │ mode=deep-research ?             │
              └────┬──────────────────────┬─────┘
                 是│                      │否
                   ▼                      ▼
         ┌─────────────────┐    ┌─────────────────┐
         │ mode=deep_research│    │ selected_skills │
         └────────┬────────┘    │     非空?       │
                  │             └───┬─────────┬───┘
                  │              否│         │是
                  │                 ▼         ▼
                  │      ┌──────────────┐ ┌──────────────┐
                  │      │ lightweight  │ │     full     │
                  │      └──────┬───────┘ └──────┬───────┘
                  ▼             │                │
    ┌─────────────────────┐     │                │
    │DeepResearchOrchestr.│     │                │
    └──────────┬──────────┘     │                │
               │    ┌───────────┴───┐    ┌───────┴───────────────┐
               │    │ LightweightQA │    │ FullFeature +         │
               │    │    Agent      │    │ ToolAssemblyPipeline  │
               └────┴───────┬───────┴────┴───────────┬───────────┘
                            ▼                        │
                    ┌───────────────┐                │
                    │ aquery_stream │◄───────────────┘
                    │   → SSE       │
                    └───────────────┘

```

---

## 5. 三种 Agent 模式对比

```text
┌──────────────────── lightweight ────────────────────┐
│ 工具: mainly multi_kb │ 无 Skill 链 │ 预检索+少轮 LLM │
│ 低延迟                                              │
└─────────────────────────────────────────────────────┘

┌────────────────────── full ───────────────────────────┐
│ ToolAssembly 动态工具 │ Skills+联网+子Agent          │
│ LangGraph 多轮 tool loop │ ContextPruning + Guardrail│
└─────────────────────────────────────────────────────┘

┌────────────────── deep_research ──────────────────────┐
│ CoordinatedAgentWorkflow 大纲 │ 用户确认 │ 子 Agent 并行 │
│ process SSE 进度                                      │
└─────────────────────────────────────────────────────┘

```

| 维度 | lightweight | full | deep_research |
|------|:-----------:|:----:|:-------------:|
| 触发 | 未选 Skill | 选了 Skill | task_type / mode |
| LangGraph | 通常不走完整图 | 完整图 | 独立编排器 |
| 典型耗时 | 秒级 | 十秒~分钟 | 分钟~小时 |

---
## 6. LangGraph Middleware 链

```text
  before_model 阶段
  ┌──────────────────────────────────────┐
  │ ContextPruningMiddleware (JIT 注入)   │
  └──────────────────┬───────────────────┘
                     ▼
  RuntimeFeatures (并行挂载)
  Clarification │ SubagentLimit │ Summarization │ ToolGuardrail │ ToolErrorHandling
                     │
                     ▼
  ┌──────────── LangGraph 节点 ────────────┐
  │  LLM Node ◄────tool_calls────► Tool Node│
  │       │                         │      │
  │       └──── ToolMessage ────────┘      │
  └────────────────────────────────────────┘

```

---

## 7. ContextPruning：State Elevation + JIT

```text
  Tool Node 返回大 ToolMessage
           │
           ▼
      ┌─────────┐
      │ 工具名?  │
      └─┬───┬───┘
 read_skill│   │multi_kb_search
        ▼   │   ▼
  state.skills  state.evidence
        │   │   │其他超长→截断通知
        └───┴───┘
           ▼
  替换为短占位符 → history 变短
           ▼
  before_model → JIT SystemMessage (≤2块 / 8000字)
           ▼
       注入 LLM 调用

```

---

## 8. 深度研究状态机

```text
  [*] ──用户发起──► draft
                      │
         CoordinatedAgentWorkflow 流式大纲
                      ▼
           awaiting_confirmation ──用户确认──► running
                 │    │                         │
            用户取消│    └────用户取消/超时───────┤
                 ▼                              ▼
            cancelled ◄────────────────── completed
                 │                              │
                 └──────────────► [*] ◄─────────┘

```

---

## 9. RAG 检索流水线

```text
  用户 Query
      │
      ▼
  关键词 hybrid (vector+BM25+LLM+RRF)
      │
      ▼
  ┌────────── Stage-1 多路召回 ──────────┐
  │ Path-A 实体向量 │ Path-B 关系/图谱    │
  │ Path-C Chunk向量│ Path-D 图扩展       │
  └──────────────────┬───────────────────┘
                     ▼
           融合 merge_entity_pool
                     │
                     ▼
            ┌─ RetrievalProfile ─┐
            │ FULL → PPR+Rerank+MMR│
            │ MMR_ONLY → 仅 MMR    │
            │ RAW → 跳过过滤       │
            └──────────┬───────────┘
                       ▼
                 Top-K chunks → Agent/API

```

---

## 10. RetrievalProfile 选型

```text
  知识库 kb_id/kb_code
           │
           ▼
    ┌──────────────┐
    │   映射规则    │◄── RAG_RETRIEVAL_PROFILE env (覆盖)
    └───┬───┬───┬──┘
        │   │   │
 hotpotqa*  rag_nuclear/CSH  轻量/默认
        │   │   │
        ▼   ▼   ▼
   general_qa  domain  lightweight/default
   关Path-A    开Path-A  MMR_ONLY / FULL
   RAW         MMR_ONLY

```

---

## 11. multi_kb_search 并发与融合

```text
  Query
    │
    ▼
  首个 KB 提取关键词 (后续 KB 复用)
    │
    ├──────────────┬──────────────┐
    ▼              ▼              ▼
 retrieve(KB1) retrieve(KB2)  ... asyncio 并发
    │              │
    └──────┬───────┘
           ▼
    跨库 RRF 融合 → 统一 chunk 列表
           │
    (可选) MULTI_KB_PATH_A_QUERY_ONLY: 每库 Path-A 仅 1 次整句向量

```

---

## 12. SSE 事件流

```text
  LangGraph astream_events
           │
           ▼
    StreamHandler + SSEEncoder
     ├─ type: status
     ├─ type: chunk  (AgentEvent)
     ├─ type: process
     ├─ type: tool
     ├─ type: artifact
     └─ type: done
           │
           ▼
  StreamingResponse (text/event-stream)
           │
           ▼
  前端 EventSource / fetch stream

```

---

## 13. 认证链路

```text
  HTTP Request
       │
       ▼
  ┌─ Header? ─┐
  │           │
 X-API-Key   Bearer Token
  │           │
  ▼           ├─ Session → Redis auth:token ──miss──► MySQL users
 MySQL        └─ 兼容 → 当作 API Key 查 MySQL
 api_keys
  │           │
  └─────┬─────┘
        ▼
  ApiKeyAuth (user_id, role) → 业务路由

```

---

## 14. Guardrails 纵深防御

```text
  用户
   │
   ▼
┌─ L1 进门: HTTP 认证 (Token/API Key/CAS) ─┐
└──────────────────┬───────────────────────┘
                   ▼
┌─ L2 内容: InputValidator ─────────────────┐
└──────────────────┬───────────────────────┘
                   ▼
              Agent / LLM ◄──── RAG 间接注入风险
                   │
                   ▼
┌─ L3 工具: ToolGuardrail + AllowlistProvider ┐
└──────────────────┬───────────────────────┘
                   ▼
              Tools 执行
                   │
                   ▼
┌─ L2 输出: OutputValidator (+ OCRValidator) ─┐
└──────────────────┬───────────────────────┘
                   ▼
  用户          L4 数据: KB/Skill 作用域隔离 ──► RAG

```

---

## 15. 知识库入库生命周期

```text
状态机:
  [*]→Uploaded→Pending(0)→Triggered(1)→Success(2)/Failed(3)
                              Failed ──重试──► Triggered
  Success → Indexed (Milvus+图谱+keyword) → Searchable

数据流:
  上传 ──► MinIO
    │
    └──► MySQL PendingFile ──► Ingest Worker ──► RAGAnything
                                              ├─► Milvus
                                              ├─► NebulaGraph
                                              └─► keyword_index

```

---

## 16. Worker 与 Kafka

```text
  文档上传 API ──► Kafka task topic ──► Task Worker (OCR/抽取)
                                              │
                                              ▼
                                         MySQL documents
                                              │
                                              ▼
                                    kb_pending_files
                                              │
                                              ▼
                                    Ingest Worker (DB 轮询) ──► RAG 入库

  深度研究 API ──► CoordinatedAgentWorkflow (不走 Kafka) ──► SSE process 事件

```

---

## 17. 模型配置优先级

```text
  create_chat_model
         │
         ▼
  MODEL_CONFIG_SOURCE?
    ├─ db (默认) ──► MySQL llm_models ──未命中──┐
    └─ yaml ──────► config.yaml models ──未命中──┤
                                                ▼
                                    settings.py / .env
                                                │
                                                ▼
                              ChatOpenAI / Patched 类
         Redis model_config_changed ──热更新──► llm_models

```

---

## 18. 存储职责一览（ER 风格）

```text
实体关系:
  User ||--o{ Conversation : owns
  User ||--o{ ApiKey : has
  Conversation ||--|| ThreadCheckpoint : thread_id
  Agent ||--o{ AgentKnowledgeBase : binds
  KnowledgeBase ||--o{ KnowledgeBaseDocument : contains
  KnowledgeBase ||--o{ PendingFile : ingests
  Document ||--o{ Task : triggers

  User              Conversation         ThreadCheckpoint
  ├─ user_id        ├─ thread_id          ├─ sqlite_file
  ├─ role           └─ title             └─ note: LangGraph state
  └─ is_active

  KnowledgeBase
  ├─ kb_id
  └─ kb_code

```

---

## 19. 配置分层

```text
  ┌─ Agent 运行时: config.yaml (models/skills/checkpointer) ─┐
  ┌─ 基础设施: .env + settings.py (MySQL/Kafka/Milvus/...) ──┐
  ┌─ 动态覆盖: llm_models 表 + Redis 热更新 ─────────────────┐
                           │
                           ▼
                   create_chat_model (DBM 优先)
                           │
              ENV ─────────┴────────► RAG/Worker/DB 连接

```

---

## 20. Explore 与主 Agent 路径对比

```text
  主 Agent /api/v2/agent-runtime  探索学习 /api/v2/explore
  AgentService → LangGraph       explore.py → explore_learning.py
       → multi_kb tool                  → retrieve_context 直接调用
       → SSE chunk/done                 → create_chat_model ainvoke
                                        → 导图/习题/详解 JSON/SSE
              │                                │
              └────────── RAG Service ◄────────┘

```

---

## 21. V2 SSE 事件管线

```text
  前端 agent-v2.ts     agent_runtime SSE LangGraph      stream_handler    Checkpointer
        │                   │                │                │                │
        │ POST /query (SSE) │                │                │                │
        ├──────────────────►│ run_stream     │                │                │
        │                   ├───────────────►│ astream_events │                │
        │                   │                ├───────────────►│ AgentEvent     │
        │◄──────────────────┴────────────────┴────────────────┤ +SSEEncoder    │
        │  (loop)                                              │                │
        │                                                   checkpoint put ─────►│
        │◄──────────────── follow_up_suggestions ────────────┤                │
        │◄──────────────── [DONE] ───────────────────────────┘                │

```

---


```
前端 useAgentChat.sendMessage()
    │
    ▼ POST /api/v2/agent-runtime/query (SSE)
agent_runtime.py ──► AgentService.run_stream()
    │
    ▼ WorkflowRegistry → ProfiledAgentExecutor → graph.astream_events
stream_handler.py (StreamHandler + AgentEvent + SSEEncoder)
    │
    ├── chunk / thinking / status
    ├── event (tool_start, tool_end, research_*)
    ├── draft_chunk (深研大纲)
    └── follow_up_suggestions (流末)
    │
    ▼
MessageList / ReasoningBlock / ArtifactSidebar
```

---

## 22. multi_kb_search 进程内链路

```text
  multi_kb_search_tool ──► execute_multi_kb_search ──► rag.retrieve_context
                                                              │
                                                              ▼
                                                    DirectRetriever 四路
                                                              │
                                                              ▼
                                                    PerKBContentFilter
                                                              │
                                                              ▼
                                                    多库 RRF (k=60)
                                                              │
                                                              ▼
                                                    ToolMessage → ContextPruning

```

---


```
Agent 调用 multi_kb_search(query, kb_ids=[...])
        │
        ▼
execute_multi_kb_search()  （进程内，非 HTTP /retrieve_internal）
        │
        ├─ kb_1 ──► retrieve_context(kb_id_1) ──► Stage-1/2/3
        ├─ kb_2 ──► retrieve_context(kb_id_2) ──► Stage-1/2/3
        └─ asyncio.gather 并发
        │
        ▼
RRF 倒数排名融合 (k=60) → 截断 MAX_RESULT_CHARS
        │
        ▼
ToolMessage → ContextPruning → JIT 注入（full 模式）
```

---

## 23. 深度研究双通道 SSE

```text
  POST /agent-runtime/query (deep_research)
           │
           ▼
  CoordinatedAgentWorkflow (大纲阶段)
           │
           ▼
  主 SSE: draft_chunk + research_draft_created
           │
           ▼
  DraftCard 确认
           │ confirmed
           ▼
  CoordinatedAgentWorkflow (子 Agent 并行)
           ├─► GET /task/id/stream (进度 SSE)
           └─► save_deep_research_report (artifact)

```

---

## 24. 主 API Lifespan 启动序

```text
  Uvicorn ──startup──► http_server lifespan
                              │
              ┌───────────────┴───────────────┐ (par 并行)
              ▼                               ▼
        RAG 预热 get_rag_service      IndexSyncScheduler
              │
              │ (超时后转后台)
              ▼
        [opt] StartupTaskProcessor 扫 kb_pending status=1
              │
              ▼
        接受 HTTP 请求

```

---

## 25. Kafka 轨 vs 入库轨（对照）

```text
  Kafka 轨 (task_worker)              入库轨 (ingest_worker)
  ─────────────────────               ─────────────────────
  文档上传 → Kafka produce            kb_pending status=1
       → TaskConsumer                      → PendingIngestProcessor
       → DocumentProcessingUseCase         → RAGAnything + LightRAG
       → ResultPersister → MySQL           → Milvus + NebulaGraph
                                            → status=2/3
              │
              └────可能写入 pending────────►┘

```

---


```
Task Worker (Kafka)          Ingest Worker (DB 轮询)
──────────────────          ─────────────────────────
make run-worker             make run-ingest-worker
worker/task_worker.py       worker/ingest_worker.py
OCR / 解析 / 抽取            RAG 向量化入库
ResultPublisher             无 Kafka
```

---

## 26. 认证与鉴权路径

```text
  请求 → CORS + user_context → verify_api_key / Bearer
                                    ├─► Redis auth:token
                                    ├─► MySQL api_keys
                                    └─► 失败 → 401
                                    │
                                    ▼
                              路由 handler

```

---

## 27. 个人文件 / 附件 / 工件 三层

```text
  用户
   ├─► my-files 个人库 ──────► MinIO (user scope)
   ├─► chat-files 会话附件 ──► ChatFileIndex BM25 ──► chat_file_search ──► Agent (full)
   └─► artifacts 线程工件 ───► ScopedStorage (thread prefix)

```

---

## 28. 生产部署进程拓扑

```text
  运维进程                          依赖服务
  ─────────                         ────────
  doc-agent run-api :8082  ────────► MySQL, Redis, Milvus, Nebula, MinIO
  doc-agent run-worker     ────────► Kafka → MySQL
  doc-agent run-ingest     ────────► MySQL → Milvus, Nebula, MinIO
  doc-agent run-guest :8083────────► Milvus, Redis

```

---

## 29. 追问建议（Follow-up）时序

```text
  Agent 流 ──回答结束──► follow_up_suggestions.generate()
                              │
                              ├─► LLM (可选)
                              │◄── 2-4 条建议
                              │
                              ├─► SSE follow_up_suggestions
                              └─► checkpoint additional_kwargs

```

---

## 30. MySQL 异步 vs 同步双引擎

```text
  FastAPI 路由                    Worker / 子线程
  get_async_session               async_session_context / sync engine
  NullPool + aiomysql             QueuePool + pymysql
         │                               │
         └───────────► MySQL ◄───────────┘

  注意: 跨 event loop 禁止复用 async 连接

```

---


```
FastAPI 路由     → create_async_engine + NullPool（避免跨 loop 复用）
Ingest Worker    → 同步 engine + QueuePool
子 Agent 线程    → 必须 NullPool / 线程本地连接（见 05_database_auth Q4）
```

---

## 31. 实验数据集上传与 Agent 查询

```text
  用户/管理员 → POST /datasets/upload → 解析 schema
                    ├─► MinIO datasets/
                    ├─► 本地 datasets/
                    └─► experiment_datasets (DB)

  excel-experiment-analysis Skill
    ├─ list_experiment_datasets → DB
    └─ query_excel_data (DuckDB) → MinIO → chart_images PNG

```

---

## 32. CSH-AI 域工作流解析

```text
  POST /agent-runtime/query (domain=csh)
              │
              ▼
        AgentService.run_stream()
              │
              ▼
        WorkflowRegistry.resolve_name()
              │
              ▼
        csh.yaml AgentProfile
         ┌─────┴─────────────────────┐
         │ preferred_workflow?       │
         ▼                           ▼
  CoordinatedAgentWF          StandardAgentWF
  (deep_research)             ├─ lightweight (无 Skill)
                              └─ full (有 Skill)
                                   └─► 水文地质垂直 KB

```

---

## 33. 会话管理：MySQL + Checkpointer

```text
  前端 → /api/v2/conversations → MySQL (conversations)
                              → checkpoints.db

  DELETE thread → MySQL is_deleted + 清 checkpoint + MinIO + 沙箱目录
  GET /messages ← checkpoint channel_values.messages
  POST /rollback → 保留前 N 个 user turn

```

---


```
GET /messages  ← checkpoint channel_values.messages
DELETE         → is_deleted + 清 checkpoint + MinIO + 本地目录
POST /rollback → 保留前 N 个 user turn
```

---

## 34. Kafka Task Worker 流水线

```text
  upload API ──TaskMessage──► kafka_task_topic
                                    │
                                    ▼
                              TaskConsumer
                                    │
                                    ├─► extraction_results (get_by_doc_id)
                                    ├─► ResultPersister → DB update
                                    └─► kafka_result_topic (publish_success)

```

---

## 35. 关键词索引：管理 API → 检索 Stage-0

```text
  POST /keywords/init ──► Redis BM25 + 低级关键词图

  Query ──► hybrid 提词 ──┬─► BM25
                        ├─► Path-A Milvus ANN
                        └─► Path-B BM25 扩展
                                    │
                                    ▼
                          DirectRetriever Stage-1

```

---

## 36. LLM/VLM 集成 vs Agent 模型工厂

```text
  Agent API ──► create_chat_model ──► llm_models (+ Redis 热更新)

  RAGAnything/LightRAG ──► integrations/llm.py ──► settings.active_llm_config
  VLM 页图理解 ──────────► integrations/vlm.py ──► settings.vlm_*

```

---

## 37. MCP SSE vs v2 Agent SSE

```text
  v2 Agent SSE                    MCP SSE
  ─────────────                   ───────
  Web → /api/v2/agent/query       Cursor → /mcp/ JSON-RPC
  {chunk|status|...}              query_knowledge_base
  Session Token                   MCP 路由配置
         │                               │
         └───────► RAG (共享 Milvus，不共享 thread) ◄────┘

```

---


```
v2 Agent SSE                    MCP SSE
/api/v2/agent/query             /mcp/ Starlette mount
{type: chunk|status|...}        JSON-RPC over SSE
Session Token                   MCP 路由配置
共享 Milvus，不共享 thread
```

---

## 38. Thinking 三层过滤

```text
  config.yaml 关 thinking
           │
           ▼
  StreamHandler 状态机
           │
           ▼
  HTTP / MCP / runner strip
           │
           ▼
  用户可见 chunk

```

---


---

## 使用说明

| 场景 | 推荐图表 |
|------|----------|
| 架构鸟瞰面试 | 图 1、2、3 |
| Agent 专项 | 图 4、5、6、7、8 |
| RAG 专项 | 图 9、10、11、**22** |
| SSE / API | 图 3、12、**21、23、37、38** |
| 安全 | 图 13、14 |
| Skills / 工具 | 图 6、7、**22** |
| KB / Worker | 图 15、16、**24–25、34** |
| RAG 关键词 | **35** |
| 模型配置 | 图 17、19、**30** |
| 存储设计 | 图 18 |
| Explore | 图 20 |
| 基础设施 / Worker | 图 15、16、**24–25** |
| 部署运维 | 图 15、16、**28** |
| 认证 | **26** |
| 文件三层 | **27** |
| 部署拓扑 | **28** |
| 追问建议 | **29** |
| MySQL/SQLite 双引擎 | **30** |
| 实验数据集 | **31** |
| CSH 路由 | **32** |
| 会话 CRUD | **33** |
| Kafka Task Worker | **34** |
| 关键词索引 | **35** |
| LLM/VLM 集成 | **36** |
| MCP vs v2 SSE | **37** |
| Thinking 三层过滤 | **38** |

各模块文档（01–12、`docs/docs_backend/*`）在对应章节有**精简版**内嵌图，与本文件完整版一致。
