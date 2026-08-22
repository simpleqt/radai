# 核电子学智能助手 - 后端项目文档 (Nuclear Electronics Intelligent Assistant - Backend)

欢迎使用「核电子学智能助手」后端项目。本项目作为系统的核心“大脑”，当前已完成从“按领域定制 Agent 类”到“Profile + Workflow + Executor + Standard Event”平台模型的迁移，面向核电子学、水文地质、基础力学（FMCA）与地下水项目分析等领域提供统一的专业文档处理、知识维护与智能问答能力。Agent Profile 配置见 `backend/config/agents/`（`ne` / `csh` / `groundwater` / `fmca`）。

---

## 🏗️ 系统架构 (System Architecture)

```text
┌─────────────────────────────────────────────────────────────────┐
│                    API 层 (FastAPI)                              │
│  v1: auth / kb / rag / uploads / models                         │
│  v2: agent-runtime / explore / guest                            │
│  MCP SSE / artifacts / conversations                            │
└──────────────────────────┬──────────────────────────────────────┘
                           │
┌──────────────────────────▼──────────────────────────────────────┐
│                       服务层                                       │
│  RAGAnything + RagService │ AgentService / WorkflowRegistry       │
│  ProfiledAgentExecutor / CoordinatedAgentWorkflow / Index Sync   │
└──────┬──────────────┬──────────────┬──────────────┬───────────────┘
       │              │              │              │
┌──────▼──────┐ ┌─────▼─────┐ ┌──────▼──────┐ ┌─────▼─────┐
│   MySQL     │ │  Milvus   │ │ NebulaGraph │ │   MinIO   │
└─────────────┘ └───────────┘ └─────────────┘ └───────────┘
       │              │              │
┌──────▼──────┐ ┌─────▼─────┐
│   Redis     │ │   Kafka   │
└─────────────┘ └───────────┘
```

项目采用分层架构设计，确保了系统的高可用性、可扩展性与模块化。

### 🔄 服务角色与拆分部署 (Service Roles & Split Deployment)
系统支持将 API 服务按角色（`API_SERVICE_ROLE` 环境变量，可选值为 `all`、`agent`、`knowledge`）进行拆分部署，以实现轻量智能体与高负载知识预热的隔离运行：
* **单体兼容模式 (all)**：默认运行方式，同一进程内装配所有服务。
* **轻量智能体进程 (agent)**：端口 `8082`。承载对话运行时，无需本地预热 RAG，工具调用中多库检索委托到知识库进程。
* **知识库检索进程 (knowledge)**：端口 `8086`。专职预热知识库实例、执行四路召回 (`DirectRetriever`) 以及定时索引同步。
* **内部鉴权**：拆分后，两进程间通过 `RAG_SERVICE_BASE_URL` 远程调用，使用配置的 `INTERNAL_API_SECRET` 进行内部接口保护。

详细部署说明见 [split_agent_rag_services.md](../split_agent_rag_services.md)。

### 1. API 层 (API Layer)
基于 **FastAPI** 构建，提供高性能的异步 RESTful 接口。
-   **v1 API**: 基础功能，包括用户认证、知识库 CRUD、文档上传与解析、RAG 检索等。
-   **v2 API**: 统一智能体运行接口，接入基于 **LangGraph** 的平台化运行时，支持标准事件流、复杂工作流与同链路协同深研。
-   **地下水项目 API**: 提供 `/api/v2/groundwater` 项目列表、项目资源、管理员项目包上传与用户授权接口，配合 `groundwater` Agent 完成项目实例分析。
-   **MCP 服务**: 实现 Model Context Protocol (通过 SSE 传输)，支持工具的动态发现与远程调用。

### 2. 服务层 (Service Layer)
-   **RAGAnything Service**: 核心检索增强引擎。负责 legacy 链路的文档解析、切片、向量化与混合检索；自研链路见 `SelfIngestionPipeline`。
-   **SelfIngestionPipeline** (`rag/ingestion/`): 自研多模态入库流水线，写入 MySQL `kb_self_*`、Milvus v2、Nebula v2、Redis BM25。
-   **Agent Runtime**: 基于 **LangGraph** 的平台化执行引擎。通过 `AgentService + WorkflowRegistry + ProfiledAgentExecutor` 统一编排轻量问答、全功能执行、深度研究任务以及 `plan_solve / multi_agent_*` 协同工作流入口；深研和 `plan_solve` 的 HTTP 请求由可重连后台任务承载。
-   **Startup Task Processor**: 在主事件循环中运行的可选后台任务处理器，仅处理 `PendingFile` 表中已进入 `status=1` 的残留入库任务，支持并发限制与断点续传。
-   **Index Sync Scheduler**: 负责知识库索引的定时同步（默认凌晨 2:00），确保 Milvus 向量库、NebulaGraph 图数据库与 MySQL 元数据的一致性。
-   **Ingest Worker**: 独立进程运行的入库 Worker，从 `kb_pending_files` 表拉取 `status=1` 的任务进行处理，支持可配置的轮询间隔与批量大小。

### 3. 基础设施层 (Infrastructure)
-   **数据库 (MySQL)**: 使用 SQLAlchemy 2.0 + pymysql/aiomysql 管理元数据、知识库状态、用户信息及任务记录。同步引擎采用 `QueuePool`；异步引擎按事件循环维护独立 pooled engine，只有无运行事件循环的 CLI/bootstrap 兼容路径使用 `NullPool`。
-   **对象存储 (MinIO)**: 存储原始文档、解析后的附件及图片资源。支持流式上传、分块上传 (Chunked Upload)、presigned URL 生成及过期会话自动清理。
-   **向量数据库 (Milvus)**: legacy 与 self_ingestion 使用独立 collection；经 `MilvusVectorDBStorage` 或 `ingestion/storage/milvus_writer.py` 写入。
-   **图数据库 (NebulaGraph)**: legacy 与 v2 命名空间隔离；通过 `NebulaGraphStorage` 或 `nebula_writer.py` 适配。
-   **缓存 (Redis)**: legacy LightRAG KV；self 侧 BM25 倒排索引（独立 key 前缀）。
-   **消息队列 (Kafka)**: 用于传统文档解析任务的异步分发与结果回传，包含 `KafkaConsumer`、`ResultPublisher` 等组件；PendingFile 入库 Worker 不走 Kafka。

---

## 🚀 核心技术栈 (Technology Stack)

| 类别 | 技术 | 说明 |
| :--- | :--- | :--- |
| 编程语言 | Python 3.12–3.14 | 全面使用 type hints 与 async/await；启动前校验依赖基线（见 `pyproject.toml` 与 [依赖版本矩阵](../dependency_version_matrix.md)） |
| Web 框架 | FastAPI + Uvicorn | 异步 RESTful API，内置 Swagger 文档 |
| 大模型框架 | LangChain + LangGraph | Agent 编排与状态图管理 |
| RAG 引擎 | LightRAG + RAGAnything + SelfIngestionPipeline | legacy 与 self_ingestion 双轨入库；检索经 DirectRetriever / SelfDirectRetrieverV2 |
| ORM | SQLAlchemy 2.0 (Async) | 异步数据库操作，支持连接池 |
| 向量数据库 | Milvus | 语义向量存储与 ANN 检索 |
| 图数据库 | NebulaGraph | 实体-关系知识图谱存储 |
| 对象存储 | MinIO | 文档与附件存储 |
| 缓存 | Redis | KV 缓存与文档状态管理 |
| 消息队列 | Kafka | 异步任务分发 |
| 工具协议 | FastMCP (Model Context Protocol) | 工具动态发现与远程调用 |
| 文档解析 | MinerU / Docling | PDF 多模态解析（图片、表格、公式） |

---

## 📂 项目结构 (Project Structure)

```text
backend/
├── api/                        # API 控制器与路由定义
│   ├── http_server.py          # 主 API 入口 (FastAPI 应用工厂、路由注册、端口 8082)
│   ├── startup.py              # 主 API 启动生命周期、角色分流、预热与后台任务编排
│   ├── guest_http_server.py    # 访客专用 API 入口 (端口 8083，独立 checkpoint)
│   ├── mcp_server.py           # 可选独立 MCP 服务器 (默认亦挂载于主 API /mcp/sse)
│   ├── routers/                # 核心功能路由
│   │   ├── agent_runtime.py    # 统一智能体运行入口 (由 agent_runtime_v2 聚合到 /api/v2)
│   │   ├── agent_runtime_v2.py # v2 聚合路由：agent-runtime / guest / explore
│   │   ├── explore.py          # 探索式学习 API
│   │   ├── auth.py             # 用户认证 (登录/登出/Token验证/密码修改)
│   │   ├── auth_sms.py         # 短信登录/验证码
│   │   ├── admin.py            # 管理后台聚合能力
│   │   ├── knowledge_base.py   # 知识库 CRUD (创建/删除/切换/统计)
│   │   ├── knowledge_base_documents.py  # 知识库文档管理
│   │   ├── knowledge_proxy.py  # 拆分模式下知识库代理
│   │   ├── self_ingestion_admin.py # 自研入库管理 (jobs/quality/repair)
│   │   ├── custom_learning.py    # 自定义学习闭环 API
│   │   ├── learning_style.py   # 学习风格测评
│   │   ├── directories.py      # 知识库目录管理 (多层目录树)
│   │   ├── rag.py              # RAG 检索与流式问答 (retrieve_context 统一入口)
│   │   ├── document_ingestion.py  # 文档入库 (异步任务)
│   │   ├── document_processing/   # 文档处理 (上传/分块上传/任务管理)
│   │   ├── keywords.py         # 关键词管理
│   │   ├── prompts.py          # 提示词/抽取 Schema 管理
│   │   ├── uploads.py          # 文件上传
│   │   ├── files.py            # 通用文件接口
│   │   ├── mcp.py              # MCP 协议路由
│   │   ├── chat_files.py       # 对话附件上传与列表
│   │   ├── my_files.py         # 个人文件库
│   │   ├── conversations.py    # 会话 CRUD 与历史恢复
│   │   ├── admin_conversations.py  # 管理员会话管理（跨用户查看/删除/统计）
│   │   ├── models.py           # 模型管理
│   │   ├── agents.py           # 智能体注册与 KB 绑定管理
│   │   ├── skills.py           # Skills 上传/版本/授权
│   │   ├── agent_skills.py     # 智能体-Skill 绑定
│   │   ├── datasets.py         # 实验数据集 CRUD
│   │   ├── groundwater_projects.py # 地下水项目管理、项目包上传与用户授权
│   │   ├── chart_images.py     # 图表 PNG 代理服务
│   │   ├── asr.py              # 语音转写
│   │   ├── auth_sms.py         # 主站短信验证码注册/重置
│   │   ├── guest_sms_auth.py   # 访客短信验证码登录/注册
│   │   ├── guest_agent.py      # 访客智能体运行入口
│   │   ├── guest_conversations.py # 访客会话管理
│   │   └── artifacts.py        # 线程工件 HTTP 代理（user_id 校验 + 路径防穿越）
│   └── dependencies/
│       └── auth.py             # 认证依赖注入 (API Key / Bearer Token)
├── core/                       # 核心安全、基座与智能体引擎
│   ├── auth/
│   │   └── identity.py         # 身份提取器 (RunnableConfig -> user_id)
│   ├── storage/
│   │   └── scoped_storage.py   # 用户级隔离存储助手 (ScopedStorage)
│   ├── skills/
│   │   └── scoped_skills_provider.py # Skills 安全提供与 Prompt 生成器
│   └── agents/                 # 核心智能体引擎
│       ├── context/            # ContextArtifactStore / ContextGovernor / result sink
│       ├── factory.py          # StateGraph 工厂 (包含中间件链组装)
│       ├── features.py         # 声明式特性开关 (RuntimeFeatures)
│       ├── state.py            # 线程状态 Schema (ThreadState)
│       ├── thread_key.py       # 用户级与会话级逻辑/物理隔离 Key 生成器
│       └── middlewares/        # 核心智能件链 (Allowlist, ContextPruning JIT, ToolGuardrail, ViewImage, SubagentLimit, LoopDetection, Clarification 等)
├── rag/                        # 检索增强生成核心模块
│   ├── raganything/            # RAGAnything 引擎
│   │   ├── raganything.py      # 核心流水线 (解析→多模态处理→入库)
│   │   ├── config.py           # 配置 (解析器/多模态/重试策略)
│   │   ├── parser/             # 文档解析器 (MinerU/Docling)
│   │   ├── modalprocessors/    # 多模态处理器 (图片/表格/公式)
│   │   ├── query.py            # 查询混入
│   │   ├── processor.py        # 处理器混入
│   │   └── batch.py            # 批量处理混入
│   ├── service.py              # RAG 服务入口 (单例模式，legacy 入库)
│   ├── ingestion/              # ★ 自研入库流水线 (self_ingestion)
│   ├── retriever_v2/           # ★ self_ingestion 检索与 HybridRetriever
│   ├── retrieval_profile.py    # ★ 按 KB 类型选择 Path 开关 + Stage-2 FilterMode
│   ├── retriever/              # ★ DirectRetriever 四路并行召回
│   │   ├── direct_retriever.py #   主入口，asyncio 四路并行
│   │   ├── entity_recall.py    #   Path-A 实体向量 + Path-B BM25
│   │   ├── chunk_searcher.py   #   Path-C Chunk 语义向量
│   │   ├── graph_expander.py   #   Path-D NebulaGraph 图扩展 + 无向 BFS
│   │   ├── fusion.py           #   实体池/Chunk 池合并、PPR 种子权重
│   │   └── config.py           #   QueryType 策略矩阵
│   ├── keywords/               # 关键词提取（向量+BM25+LLM hybrid）、QueryType
│   ├── content_filter/         # 内容过滤流水线
│   │   ├── manager.py          # 过滤管理器 (LRU 缓存 + 按知识库隔离)
│   │   ├── per_kb_filter.py    # 知识库级过滤器 (PPR→Rerank→MMR)
│   │   ├── ppr_filter.py       # PPR 图过滤 (NetworkX 原生 C 实现)
│   │   ├── reranker.py         # Rerank 加权融合 (PPR + Reranker 分数融合)
│   │   ├── mmr_reranker.py     # MMR 多样性重排序 (Maximal Marginal Relevance)
│   │   └── models.py           # 数据模型 (FilterMode/FilterConfig/RawRetrievalData)
│   ├── knowledge_base/         # 知识库管理器 (1500+ 行)
│   └── sync/                   # 索引同步调度
│       ├── rag_index_sync_manager.py    # 索引同步管理器
│       └── rag_index_sync_scheduler.py  # 定时同步调度器
├── application/                # 平台应用层
│   ├── agent_service.py        # 标准运行入口（AgentRequest -> Workflow）
│   ├── agent_workflows/        # lightweight / full / deep_research / coordinated 工作流
│   ├── agent_executors/        # ProfiledExecutor / GraphBuilder / StreamingRuntime
│   ├── agents/                 # ProfileLoader / PromptBuilder / InfoService
│   ├── agent_trace/            # Trace / Replay / Eval JSONL 记录与调试
│   ├── retrieval/              # RAG 查询服务封装与 DirectRetriever 获取
│   ├── ingestion/              # PendingFile 入库用例封装
│   ├── mcp/                    # FastMCP SSE 应用、工具目录与查询服务
│   ├── tools/                  # ToolAssemblyPipeline / Registry / Providers
│   ├── skills/                 # Managed Skill 目录、上传、校验、运行时解析
│   └── event_stream/           # SSEEncoder / thinking filter
├── skills/                     # Skill 能力声明
│   ├── public/                 # 公开 Skills（nuclear-electronics、deep-research 等）
│   └── custom/                 # 用户定制 Skills
├── services/
│   └── startup_task_processor.py  # 启动任务处理器 (断点续传)
├── worker/                     # 独立 Worker 进程
│   ├── ingest_worker.py        # 入库 Worker 主循环（轮询 kb_pending_files）
│   ├── task_worker.py          # Kafka 文档解析 Worker
│   └── components/             # Worker 组件 (处理器/消费者/结果持久化/发布)
├── db/                         # 数据库层
│   ├── connection.py           # 连接管理 (同步/异步双引擎)
│   ├── models/                 # ORM 模型 (KnowledgeBase/Document/Directory/PendingFile/Conversation)
│   ├── crud/                   # CRUD 操作
│   └── init_db.py              # 数据库初始化
├── storage/                    # MinIO 客户端、用户 bucket 与分块上传管理器
│   └── __init__.py
├── kafka/                      # Kafka 消息队列
│   ├── client.py               # Kafka 客户端
│   ├── consumer.py             # 消费者
│   ├── admin.py                # 管理端
│   └── schemas.py              # 消息 Schema
├── config/                     # 日志与系统设置
│   ├── settings.py             # 全局配置 (Pydantic Settings)
│   ├── settings_sections.py    # 按模型/RAG/存储/认证/服务拆分的环境配置
│   ├── network.py              # 代理环境清理
│   ├── prompts/registry.yaml   # 提示词注册表
│   ├── tools/registry.yaml     # 工具注册表
│   └── loggers.py              # 日志工厂
├── config.yaml                 # 旧版运行时配置兼容入口（模型工厂 fallback）
└── scripts/                    # 运维脚本 (数据迁移/索引构建/缓存清理等)
```

---

### RAG 检索流程 (三阶段流水线)

```text
┌──────────┐   hybrid 提词   ┌──────────────────────────────┐
│   查询   │ ──────────────► │ DirectRetriever 四路 A/B/C/D │
└──────────┘                 └──────────────┬───────────────┘
                                            │ 失败
                                            ▼
                                   LightRAG mix 回退
                                            │
                                            ▼
                              ┌─────────────────────────┐
                              │ PerKBContentFilter      │
                              └────────────┬────────────┘
                                           ▼
                                    Prompt 组装
                                           │
                                           ▼
                              LLM / multi_kb_search RRF
```
算法细节与评测结论见 [知识库算法和架构.md](../知识库算法和架构.md)。

### V2 Agent Runtime 问答流程

```text
┌─────────────┐  POST /api/v2/agent-runtime/query  ┌────────────────────┐
│ RadAI/CSH   │ ─────────────────────────────────► │ agent_runtime.py   │
└─────────────┘                                     └─────────┬──────────┘
┌─────────────┐  POST /api/v2/guest/query                    │
│ Guest 前端  │ ─────────────────────────────────────────────┤
└─────────────┘                                              │
                      ┌───────────────────────────────────────┴────────────────────┐
                      ▼                                                            ▼
              AgentService.run_stream()                                  guest edge adapter
                      │
                      ▼
              WorkflowRegistry.resolve()
                      │
         ┌────────────┼─────────────────────────────────┐
         ▼            ▼                                 ▼
   lightweight      full        deep_research / plan_solve / multi_agent_*
         │            │                                 │
         ▼            ▼                                 ▼
 StandardAgent  StandardAgent              CoordinatedAgentWorkflow
 Workflow       Workflow                   planner -> researchers -> writer
         │            │                                 │
         ▼            ▼                                 ▼
        ProfiledAgentExecutor        RuntimeToolInvoker + ProfiledAgentExecutor
         │
         └────────────┬─────────────────────────────────┘
                      ▼
       AgentStreamRuntime / Standard AgentEvent
                      │
                      ▼
       StreamingResponse (SSE)
```

### 安全与隔离逻辑 (Security & Isolation)
```
RunnableConfig (thread_id) → IdentityProvider (user_id)
    → Composite Internal Key (u_{user_id}__t_{thread_id})
    → ScopedStorage (MinIO 强制前缀 users/{user_id}/threads/{thread_id}/)
    → ScopedSkillsProvider (根据用户可见性及所选 skills 动态生成 Prompt)
    → Agent Runtime (物理挂载沙箱 / 路径遍历防御工具集)

```

---

## 🛠️ 环境配置与运行 (Setup & Execution)

### 1. 环境准备
推荐使用项目自带的 `venv314` 虚拟环境：
```bash
make install          # 或 pip install -r requirements.txt
```

### 2. 环境变量配置 (`backend/.env`)
关键配置项包括：
-   `LLM_PROVIDER`: 模型服务商 (如 qwen, deepseek)
-   `DASHSCOPE_API_KEY`: 阿里云模型密钥
-   `SERVER_PORT`: 服务运行端口 (默认 8082)
-   `MINIO_ENDPOINT`, `MINIO_ACCESS_KEY`, `MINIO_SECRET_KEY`: MinIO 连接信息
-   `MYSQL_HOST`, `MYSQL_PORT`, `MYSQL_USER`, `MYSQL_PASSWORD`, `MYSQL_DATABASE`: MySQL 连接信息
-   `MILVUS_URI`, `MILVUS_USER`, `MILVUS_PASSWORD`, `MILVUS_DB_NAME`: Milvus 连接信息
-   `NEBULA_HOST`, `NEBULA_GRAPH_PORT`: NebulaGraph 连接信息
-   `REDIS_HOST`, `REDIS_PORT`: Redis 连接信息
-   `CAS_SERVICE_URL`: CAS 回调地址白名单；多个前端共用后端时用逗号分隔，Host/`X-Forwarded-Host` 只能从白名单中选择，不能生成新地址
-   `API_SERVICE_ROLE`: 主 API 角色，`all` / `agent` / `knowledge`
-   `RAG_SERVICE_BASE_URL`, `INTERNAL_API_SECRET`: 拆分部署时 agent → knowledge 的内部检索调用
-   `MODEL_CONFIG_SOURCE`: 模型配置来源，默认 `db`，失败时 fallback 到 `backend/config.yaml` 与 `settings`

### 3. 子系统配置
主配置由 `backend/config/settings.py` + `backend/config/settings_sections.py` 从 `backend/.env` 读取，并保留 flat access 兼容。`backend/config.yaml` 仍作为模型工厂与旧运行时的 fallback 配置；智能体 profile、子智能体 manifest、工具、MCP、工作流、提示词等注册表分别位于 `backend/config/agents/`（含 `runtime_agents.yaml`）、`backend/config/tools/`、`backend/config/mcp/`、`backend/config/workflows/`、`backend/config/prompts/`。

### 4. 运行服务

完整运维说明见项目根目录 `Makefile`、`scripts/run_*` 与 `scripts/*service.sh`。

> **文档同步（2026-07-14）**：本目录各模块已与 `backend/` 代码逐项对照修订；含自研入库双轨、`runtime_agents.yaml`、可重连协同任务、Trace/Replay、Checkpointer 后端选择、自定义学习 API、提示词管理与访客短信认证。

#### A. 单体兼容模式 (Monolithic Mode)
在单进程中同时运行智能体逻辑与知识库检索：
```bash
make run-api              # 主 API (端口 8082) → http://localhost:8082/docs
make run-guest-api        # 访客 API (端口 8083) → http://localhost:8083
make run-worker           # Kafka 任务 Worker
make run-ingest-worker    # 文档入库 Worker
```

#### B. 拆分服务模式 (Split-Service Mode)
为了在高负载与预热复杂场景下保持系统高响应，可拆分为两个独立的进程运行（详情见 [split_agent_rag_services.md](../split_agent_rag_services.md)）：
* **知识库进程** (Port 8086)：承担 RAG、索引预热及图同步，由 `RAG_SERVICE_BASE_URL` 远程调用。
  ```bash
  scripts/run_knowledge_api.sh
  ```
* **轻量智能体进程** (Port 8082)：热重载快速启动，委托检索到 8086。
  ```bash
  scripts/run_agent_api.sh
  ```

```bash
# 可选：独立 MCP 服务器（主 API 已挂载 /mcp/sse）
venv314/bin/python -m backend.api.mcp_server
```

**Checkpointer**：主 API 默认使用 `AsyncSqliteSaver`，写入 `backend/data/checkpoints.db`；设置 `AGENT_CHECKPOINTER_BACKEND=redis` 时改用共享 `AsyncRedisSaver`，适合多 Worker/多实例。独立访客 API 固定使用 `AsyncSqliteSaver` 写入 `backend/data/guest_checkpoints.db`。

**运行时持久化与 Trace**：`AGENT_RUNTIME_STORE_BACKEND` 可选 `disabled`（默认）、`memory`、`sqlite` 或共享 `sql`，用于运行树镜像/恢复；`AGENT_TRACE_BACKEND` 默认 `jsonl`，也支持 `indexed`（SQLite 索引 + 本地/MinIO 对象）和 `sql`（共享 MySQL 索引 + 本地/MinIO 对象）。启用 SQL 运行树或 Trace 索引前需先执行对应迁移脚本。

---

## 📜 核心路由说明 (API Routers)

| 路由前缀 | 功能说明 | 关键端点 |
| :--- | :--- | :--- |
| `/api/v1/auth` | 用户认证 | `POST /login`, `POST /logout`, `GET /verify`, `GET /me`, `POST /change-password` |
| `/api/v1/knowledge_bases` | 知识库管理 | `POST /`, `GET /`, `GET /default`, `GET /{kb_id}`, `PUT /{kb_id}`, `DELETE /{kb_id}`, `POST /{kb_id}/switch`, `POST /{kb_id}/stats` |
| `/api/v1/knowledge_bases/{kb_id}/documents` | 知识库文档 | `GET /`, `POST /`, `GET /tree`, `POST /reorder`, `PATCH/DELETE /{doc_id}`, 批量移动/删除 |
| `/api/v1/knowledge_bases/{kb_id}/directories` | 知识库目录 | 多层目录树管理 (CRUD + 排序 + 图标/颜色) |
| `/api/v1/documents` | 文档处理 | 上传、分块上传、任务管理 |
| `/api/v1/ingestion` | 文档入库 | 启动文档解析与存库任务 (异步) |
| `/api/v1/admin/self-ingestion` | 自研入库管理 | jobs/health/repair/switch-backend/shadow-rebuild（需管理员） |
| `/api/v1/health/self-ingestion` | 自研入库健康检查 | 队列与失败摘要（健康别名，同样依赖管理员鉴权） |
| `/api/v1/custom-learning` | 自定义学习 | 计划 CRUD、节点学习、练习评分、计划调整 |
| `/api/v1/learning-style` | 学习偏好测评 | 题库、答题、结果画像、`apply_to_ai` 开关 |
| `/api/v1/prompts` | 提示词管理 | 抽取 Schema 模板分类、列表、详情、创建、更新（需管理员） |
| `/api/v1/rag` | RAG 服务 | `POST /retrieve`, `POST /retrieve_internal`, `POST /internal/multi-kb-search`, `POST /query_stream` |
| `/api/v1/keywords` | 关键词管理 | 关键词索引构建与查询 |
| `/api/v1/mcp` | MCP 管理/查询 | 配置、工具目录与 query 代理；MCP SSE 传输入口为 `/mcp/sse` |
| `/api/v1/uploads` | 文件上传 | 通用文件上传接口 |
| `/api/v2/agent-runtime` | 统一智能体运行入口 | `POST /query`, `GET /profiles/{agent_code}/info`, `GET /tools`, `GET /skill-manifests`；`GET /runs/{run_id}/*` Trace 调试、`POST /runs/{run_id}/cancel|replay`；`deep_research/plan_solve` 使用 `/{agent_code}/task/{task_id}/status|stream|cancel` |
| `/api/v2/guest` | 访客模式 | `POST /query` (匿名访客免登录流式问答，IndexedDB 前端持久化，SQLite Checkpointer) |
| `/api/v1/guest-sms` | 访客短信认证 | `GET /captcha`, `POST /send-code`, `POST /register`, `POST /login`（独立 guest API 挂载） |
| `/api/v2/conversations` | 会话管理 | 列表、重命名、删除、历史消息恢复 |
| `/api/v2/threads/{id}/artifacts/{path}` | 线程工件 | 读取 Agent 生成的报告/文件（需登录 + 会话归属校验） |
| `/api/v2/explore` | 探索式学习 | 主题发现、脑图、详解 SSE、习题等 |
| `/api/v2/chat-files` | 对话附件 | 上传、列表（会话级文件） |
| `/api/v1/my-files` | 个人文件库 | 列表、重命名、删除 |
| `/api/v1/files/upload` | 文件上传 | 通用文件上传 |
| `/api/v1/sessions/{id}/files/attach` | 会话附件 | 将文件附加到会话 |
| `/api/v1/agents` | 智能体管理 | Agent CRUD、KB 绑定、`/{agent_code}/skills` Agent-Skill 绑定 |
| `/api/v1/skills` | Skill 管理 | ZIP 上传、版本发布/激活/拒绝、启停、授权、下载 |
| `/api/v1/datasets` | 实验数据集 | 注册、上传、预览、Agent 绑定 |
| `/api/v1/charts/{user_id}/{chart_id}` | 图表服务 | 可视化 PNG 代理 |
| `/api/v1/admin/conversations` | 管理员会话管理 | 全局会话列表、消息获取、删除、统计（需 admin 角色） |
| `/api/v1/asr/transcribe` | 语音转写 | 音频 → 文本 |
| `/api/send-sms-code` | 短信认证 | 发送验证码 |
| `/api/register` | 短信认证 | 验证码注册 |
| `/api/forgot-password` | 短信认证 | 验证码重置密码 |
| `/mcp/sse` | MCP SSE | MCP SSE 端点 (工具动态发现) |
| `/health` | 健康检查 | 服务健康状态 |
| `/config` | 配置查询 | 当前 LLM/VLM 配置 |

---

## 📊 知识库配置 (Knowledge Base Configuration)

系统预配置了两个核心知识库：

| 知识库 | kb_code | 类型 | 说明 |
| :--- | :--- | :--- | :--- |
| 核电子学知识库 | `rag_nuclear_electronics` | professional | 核探测器、放大器、信号处理等专业知识 |
| 核电子学期刊论文库 | `NuclearElectronicsPaperRepository` | journal | Nuclear Instruments and Methods in Physics Research Section A |

V2 Agent 默认同时检索两个知识库，使用 **RRF (Reciprocal Rank Fusion)** 算法合并结果。

---

## 🛡️ 开发指南 (Development Support)

### 编码规范
1.  **异步优先**: 所有 I/O 操作（DB, LLM, Storage）必须使用 `async/await` 模式。
2.  **日志记录**: 请使用 `backend.config.loggers.get_logger(__name__)` 提供的 logger，确保日志在 `backend/logs` 下正确循环记录。
3.  **任务隔离**: 耗时较长的预处理任务应当通过 `StartupTaskProcessor` 或 `IngestWorker` 处理，避免阻塞 Web 响应。
4.  **单例模式**: 核心服务（RagService、KBManager、ContentFilterManager）均采用全局单例，通过 `get_xxx()` 函数获取。
5.  **数据库迁移**: 修改 `db/models/` 时，必须使用 **Alembic** 生成迁移脚本，严禁直接修改生产环境数据库结构。

### Agent 开发
-   业务 Agent 基于 `backend/core/agents/factory.py` 的 `create_agent_graph()` 构建。
-   使用 `RuntimeFeatures` 声明式启用特性（sandbox, guardrail, summarization, vision, subagent）。
-   中间件链由工厂自动组装，支持 `@Next/@Prev` 锚点插入自定义中间件。
-   对话状态通过配置的 Checkpointer 持久化：主 API 默认 `AsyncSqliteSaver`，共享部署可切换 `AsyncRedisSaver`；独立访客 API 使用单独的 SQLite Checkpointer。登录用户使用内部键 `u_{user_id}__t_{thread_id}` 隔离。

### RAG 检索开发
-   统一入口：`backend/api/routers/rag.py` → `retrieve_context()`；Stage-1 默认 `DirectRetriever`（`USE_DIRECT_RETRIEVER=true`）。
-   生产垂直库 Profile：`domain` / `lightweight` → Stage-2 **MMR_ONLY**（跳过 PPR/Rerank）；百科库 `general_qa` → **RAW**。
-   关键词：生产固定 **hybrid**（`PerKBKeywordExtractor`）；GraphRAG 评测勿向 LightRAG 注入 `_our_kw`。
-   内容过滤器按知识库隔离（`PerKBContentFilter`），通过 `ContentFilterManager` 的 LRU 缓存管理。
-   修改后建议跑 `eval/datasets/paper_kb/` 或 `eval/graphrag_bench/run_official_graphrag_eval.py`（见 [eval_framework.md](../eval_framework.md)）。
-   修改 `rag/service.py` 时注意异步锁，避免并发入库写冲突；PPR 仅在 `FULL`/`PPR_*` 模式启用（`nx.pagerank_numpy`）。

---
© 2026 核电子学智能助手项目组.
