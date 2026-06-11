# 核电子学智能助手 - 后端项目文档 (Nuclear Electronics Intelligent Assistant - Backend)

欢迎使用「核电子学智能助手」后端项目。本项目作为系统的核心“大脑”，当前已完成从“按领域定制 Agent 类”到“Profile + Workflow + Executor + Standard Event”平台模型的迁移，面向核电子学与水文地质等领域提供统一的专业文档处理、知识维护与智能问答能力。

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
│  ProfiledAgentExecutor / ResearchTaskRuntime / Index Sync        │
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

项目采用分层架构设计，确保了系统的高可用性、可扩展性与模块化：

### 1. API 层 (API Layer)
基于 **FastAPI** 构建，提供高性能的异步 RESTful 接口。
-   **v1 API**: 基础功能，包括用户认证、知识库 CRUD、文档上传与解析、RAG 检索等。
-   **v2 API**: 统一智能体运行接口，接入基于 **LangGraph** 的平台化运行时，支持标准事件流、复杂工作流与后台深研任务。
-   **MCP 服务**: 实现 Model Context Protocol (通过 SSE 传输)，支持工具的动态发现与远程调用。

### 2. 服务层 (Service Layer)
-   **RAGAnything Service**: 核心检索增强引擎。负责文档的解析（支持多模态）、切片 (Chunking)、向量化 (Embedding) 以及基于向量与关键词的混合检索。
-   **Agent Runtime**: 基于 **LangGraph** 的平台化执行引擎。通过 `AgentService + WorkflowRegistry + ProfiledAgentExecutor` 统一编排轻量问答、全功能执行、深度研究任务以及 `plan_solve / multi_agent_*` 协同工作流入口。
-   **Startup Task Processor**: 在主事件循环中运行的后台任务处理器，自动扫描并处理 `PendingFile` 表中 `status=0/1` 的未完成文档入库任务，支持并发限制与断点续传。
-   **Index Sync Scheduler**: 负责知识库索引的定时同步（默认凌晨 2:00），确保 Milvus 向量库、NebulaGraph 图数据库与 MySQL 元数据的一致性。
-   **Ingest Worker**: 独立进程运行的入库 Worker，从 `kb_pending_files` 表拉取 `status=1` 的任务进行处理，支持可配置的轮询间隔与批量大小。

### 3. 基础设施层 (Infrastructure)
-   **数据库 (MySQL)**: 使用 SQLAlchemy 2.0 (Async) + aiomysql 管理元数据、知识库状态、用户信息及任务记录。同步引擎采用 `QueuePool`；异步引擎采用 `NullPool` 避免跨事件循环连接复用问题。
-   **对象存储 (MinIO)**: 存储原始文档、解析后的附件及图片资源。支持流式上传、分块上传 (Chunked Upload)、presigned URL 生成及过期会话自动清理。
-   **向量数据库 (Milvus)**: 存储语义索引，支持毫秒级的相似度检索。通过 `MilvusVectorDBStorage` 适配 LightRAG。
-   **图数据库 (NebulaGraph)**: 存储实体-关系知识图谱，通过 `NebulaGraphStorage` 适配 LightRAG，支持图拓扑检索与 PPR 过滤。
-   **缓存 (Redis)**: 存储 LightRAG 的 KV 缓存与文档状态，通过 `RedisKVStorage` 和 `RedisDocStatusStorage` 适配。
-   **消息队列 (Kafka)**: 用于入库任务的异步分发与结果回传，包含 `KafkaConsumer`、`ResultPublisher` 等组件。

---

## 🚀 核心技术栈 (Technology Stack)

| 类别 | 技术 | 说明 |
| :--- | :--- | :--- |
| 编程语言 | Python 3.14+ | 全面使用 type hints 与 async/await（见 `pyproject.toml`） |
| Web 框架 | FastAPI + Uvicorn | 异步 RESTful API，内置 Swagger 文档 |
| 大模型框架 | LangChain + LangGraph | Agent 编排与状态图管理 |
| RAG 引擎 | LightRAG + RAGAnything | 多模态文档解析与知识图谱检索 |
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
│   ├── http_server.py          # 主 API 入口 (FastAPI 应用工厂 + 生命周期，端口 8082)
│   ├── guest_http_server.py    # 访客专用 API 入口 (端口 8083，独立 checkpoint)
│   ├── mcp_server.py           # 可选独立 MCP 服务器 (默认亦挂载于主 API /mcp/sse)
│   ├── routers/                # 核心功能路由
│   │   ├── auth.py             # 用户认证 (登录/登出/Token验证/密码修改)
│   │   ├── knowledge_base.py   # 知识库 CRUD (创建/删除/切换/统计)
│   │   ├── knowledge_base_documents.py  # 知识库文档管理
│   │   ├── directories.py      # 知识库目录管理 (多层目录树)
│   │   ├── rag.py              # RAG 检索与流式问答 (retrieve_context 统一入口)
│   │   ├── document_ingestion.py  # 文档入库 (异步任务)
│   │   ├── document_processing/   # 文档处理 (上传/分块上传/任务管理)
│   │   ├── keywords.py         # 关键词管理
│   │   ├── extraction.py       # 内容抽取
│   │   ├── uploads.py          # 文件上传
│   │   ├── mcp.py              # MCP 协议路由
│   │   ├── chat_files.py       # 对话附件上传与列表
│   │   ├── my_files.py         # 个人文件库
│   │   ├── conversations.py    # 会话 CRUD 与历史恢复
│   │   ├── admin_conversations.py  # 管理员会话管理（跨用户查看/删除/统计）
│   │   ├── models.py           # 模型管理
│   │   ├── agents.py           # 智能体注册与 KB 绑定管理
│   │   ├── datasets.py         # 实验数据集 CRUD
│   │   ├── chart_images.py     # 图表 PNG 代理服务
│   │   ├── asr.py              # 语音转写
│   │   ├── auth_sms.py         # 短信验证码注册/重置
│   │   └── artifacts.py        # 线程工件 HTTP 代理（user_id 校验 + 路径防穿越）
│   └── dependencies/
│       └── auth.py             # 认证依赖注入 (API Key / Bearer Token)
├── core/                       # 核心安全、基座与智能体引擎 [NEW]
│   ├── auth/
│   │   └── identity.py         # 身份提取器 (RunnableConfig -> user_id)
│   ├── skills/
│   │   └── scoped_skills_provider.py # Skills 安全提供与 Prompt 生成器
│   └── agents/                 # 核心智能体引擎
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
│   ├── service.py              # RAG 服务入口 (单例模式)
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
│   ├── research/               # 深研草案、后台任务、工作流服务
│   ├── tools/                  # ToolAssemblyPipeline / Registry / Providers
│   ├── skills/                 # SkillManifest Loader
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
├── storage/                    # 存储抽象层
│   ├── __init__.py             # MinIO 客户端 + 分块上传管理器
│   └── scoped_storage.py       # 用户级隔离存储助手 (ScopedStorage)
├── kafka/                      # Kafka 消息队列
│   ├── client.py               # Kafka 客户端
│   ├── consumer.py             # 消费者
│   ├── admin.py                # 管理端
│   └── schemas.py              # 消息 Schema
├── config/                     # 日志与系统设置
│   ├── settings.py             # 全局配置 (Pydantic Settings)
│   └── loggers.py              # 日志工厂
│   └── config.yaml             # 后端运行时与模型配置文件
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
         ┌────────────┼───────────────┐
         ▼            ▼               ▼
   lightweight      full        deep_research      coordinated
         │            │               │
         ▼            ▼               ▼
 StandardAgent  StandardAgent   DeepResearch
 Workflow       Workflow        Workflow
         │            │               │
         ▼            ▼               ▼
 ProfiledAgentExecutor / ResearchWorkflowService
         │
         └──────┬──────┘
                ▼
       aquery_stream()
                │
    lightweight │ full: astream_events + ContextPruning + StreamHandler
    预检索+单次LLM │
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
-   `MYSQL_URL`, `ASYNC_MYSQL_URL`: MySQL 连接信息
-   `MILVUS_CONNECTION_URI`: Milvus 连接信息
-   `NEBULA_HOST`, `NEBULA_PORT`: NebulaGraph 连接信息
-   `REDIS_HOST`, `REDIS_PORT`: Redis 连接信息

### 3. 子系统配置 (`backend/config.yaml`)
`config.yaml` 用于后端运行时配置，包括：
-   模型配置 (名称、参数、超时)
-   工具路径 (技能目录、沙箱目录)
-   Sandbox 沙箱配置 (本地/远程)
-   记忆与摘要配置
-   追踪与日志配置

### 4. 运行服务

完整运维说明见项目根目录 `Makefile` 与 `backend/config.yaml`。

> **文档同步（2026-06-08）**：本目录各模块已与 `backend/` 代码逐项对照修订；旧 `backend/custom/` 目录已删除，功能已迁入 `application/` 与 `api/routers/`。

```bash
make run-api              # 主 API → http://<api_host>:<api_port>/docs
make run-guest-api        # 访客 API → http://<guest_api_host>:<guest_port>
make run-worker           # Kafka 任务 Worker
make run-ingest-worker    # 文档入库 Worker

# 可选：独立 MCP 服务器（主 API 已挂载 /mcp/sse）
venv314/bin/python -m backend.api.mcp_server
```

**Checkpointer**：主 API 与访客 API 均使用 `AsyncSqliteSaver`，分别写入 `backend/data/checkpoints.db` 与 `backend/data/guest_checkpoints.db`。

---

## 📜 核心路由说明 (API Routers)

| 路由前缀 | 功能说明 | 关键端点 |
| :--- | :--- | :--- |
| `/api/v1/auth` | 用户认证 | `POST /login`, `POST /logout`, `GET /verify`, `GET /me`, `POST /change-password` |
| `/api/v1/knowledge_bases` | 知识库管理 | `POST /`, `GET /`, `GET /default`, `GET /{kb_id}`, `PUT /{kb_id}`, `DELETE /{kb_id}`, `POST /{kb_id}/switch`, `POST /{kb_id}/stats` |
| `/api/v1/knowledge_bases/{kb_id}/documents` | 知识库文档 | `GET /` (列表，支持解析/入库状态过滤) |
| `/api/v1/directories` | 知识库目录 | 多层目录树管理 (CRUD + 排序 + 图标/颜色) |
| `/api/v1/documents` | 文档处理 | 上传、分块上传、任务管理 |
| `/api/v1/ingestion` | 文档入库 | 启动文档解析与存库任务 (异步) |
| `/api/v1/rag` | RAG 服务 | `POST /retrieve`, `POST /retrieve_internal` (进程内直调), `POST /query/stream` (流式问答) |
| `/api/v1/keywords` | 关键词管理 | 关键词索引构建与查询 |
| `/api/v1/extraction` | 内容抽取 | 文档内容抽取 |
| `/api/v1/mcp` | MCP 服务 | MCP 协议支持，暴露系统工具集 |
| `/api/v1/uploads` | 文件上传 | 通用文件上传接口 |
| `/api/v2/agent-runtime` | 统一智能体运行入口 | `POST /query`, `GET /profiles/{agent_code}/info`, `GET /tools`, `GET /skill-manifests`, 深研任务状态/取消/SSE |
| `/api/v2/guest` | 访客模式 | `POST /query` (匿名访客免登录流式问答，IndexedDB 前端持久化，SQLite Checkpointer) |
| `/api/v2/conversations` | 会话管理 | 列表、重命名、删除、历史消息恢复 |
| `/api/v2/threads/{id}/artifacts/{path}` | 线程工件 | 读取 Agent 生成的报告/文件（需登录 + 会话归属校验） |
| `/api/v2/explore` | 探索式学习 | 主题发现、脑图、详解 SSE、习题等 |
| `/api/v2/chat-files` | 对话附件 | 上传、列表（会话级文件） |
| `/api/v1/my-files` | 个人文件库 | 列表、重命名、删除 |
| `/api/v1/files/upload` | 文件上传 | 通用文件上传 |
| `/api/v1/sessions/{id}/files/attach` | 会话附件 | 将文件附加到会话 |
| `/api/v1/agents` | 智能体管理 | Agent CRUD、KB 绑定 |
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
-   对话状态通过 `AsyncSqliteSaver` 持久化；登录用户使用内部键 `u_{user_id}__t_{thread_id}` 隔离。

### RAG 检索开发
-   统一入口：`backend/api/routers/rag.py` → `retrieve_context()`；Stage-1 默认 `DirectRetriever`（`USE_DIRECT_RETRIEVER=true`）。
-   生产垂直库 Profile：`domain` / `lightweight` → Stage-2 **MMR_ONLY**（跳过 PPR/Rerank）；百科库 `general_qa` → **RAW**。
-   关键词：生产固定 **hybrid**（`PerKBKeywordExtractor`）；GraphRAG 评测勿向 LightRAG 注入 `_our_kw`。
-   内容过滤器按知识库隔离（`PerKBContentFilter`），通过 `ContentFilterManager` 的 LRU 缓存管理。
-   修改后建议跑 `eval/datasets/paper_kb/` 或 `eval/graphrag_bench/run_official_graphrag_eval.py`（见 [eval_framework.md](../eval_framework.md)）。
-   修改 `rag/service.py` 时注意异步锁，避免并发入库写冲突；PPR 仅在 `FULL`/`PPR_*` 模式启用（`nx.pagerank_numpy`）。

---
© 2026 核电子学智能助手项目组.
