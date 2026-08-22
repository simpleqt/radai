# Infrastructure (基础设施与 API) 模块详解

> **最后同步**：2026-07-14（对齐 `http_server.py` / `startup.py` / 路由注册 / 启动脚本 / Docker 编排 / Checkpointer 后端选择）
> 📊 面试向详解 → [plans/qa/07_workers_kafka.md](../../plans/qa/07_workers_kafka.md) · [10_knowledge_base.md](../../plans/qa/10_knowledge_base.md) · [00_architecture_diagrams.md](../../plans/qa/00_architecture_diagrams.md) 图 15–16、24–25

基础设施模块是支撑「核电子学智能助手」业务逻辑运行的底座，包括 Web 服务框架、持久化存储层、异步任务调度、对象存储集成及消息队列。

### 系统底座总览

```text
                    ┌─────────────────────────────────────────┐
                    │           两条独立后台链路                 │
                    └─────────────────────────────────────────┘

  Kafka 轨（make run-worker）              入库轨（make run-ingest-worker）
  ─────────────────────────              ────────────────────────────────
  上传 → Kafka TaskMessage               kb_pending_files status=0→1
         → Task Worker                         │
         → OCR/MinerU/抽取                     ▼
         → MySQL documents              Ingest Worker 轮询 status=1
                                              │
                                              ▼
                                        RAGAnything → Milvus/Nebula
                                              │
                                              ▼
                                        status=2 / 3

  ⚠️ ResultPersister/Publisher 属 Kafka 轨，不是 Ingest Worker
  ⚠️ 深度研究走 API 内 asyncio Task + TaskManager，不走 Kafka
```

### PendingFile 状态机

```text
status=0 待入库 ──► 管理后台 /uploads/ingest 或认领 ──► status=1
status=1 入库中 ──► IngestWorker（或 StartupTaskProcessor）──► status=2 成功
                                                      └──► status=3 失败
Redis 锁 kb_ingest_lock:{file_id} 防多 Worker 重复处理
```

---

## 🛠️ 已有项目模块说明 (Existing Module)

### 1. FastAPI 核心与生命周期 (`api/http_server.py` + `api/startup.py`)

**功能**: `http_server.py` 负责创建 FastAPI 应用、注册中间件/异常处理/路由/静态资源；`startup.py` 负责 Lifespan、服务角色分流、预热与后台任务清理。

**生命周期管理**:
-   **角色分发与过滤** (`main_lifespan`): 系统基于 `API_SERVICE_ROLE` 环境变量决定启动和初始化的具体服务实例：
    * `runs_agent_runtime` (角色为 `all` 或 `agent`): 启动并挂载 LangGraph `Checkpointer`，初始化实验数据集和管理员后台表，订阅 Redis 模型路由更新。
    * `runs_knowledge_runtime` (角色为 `all` 或 `knowledge`): 初始化 RAG 引擎与实例预热，预加载关键词索引并预热图缓存，启动索引同步调度器与可选的入库处理器。
-   **启动阶段** (`lifespan`): 并行初始化以上各特定角色组件。当耗时操作（如关键词图和 RAG 实例加载）超过 `STARTUP_INIT_TIMEOUT_SECONDS`（默认 12s）时，会自动切入后台协程执行，防止 Web 端口启动受阻。
-   **TLS 与线程池**：启动时可通过 `TLS_CERT_SANDBOX_ENABLED` 统一设置 `SSL_CERT_FILE/SSL_CERT_DIR`；通过 `ASYNCIO_THREAD_POOL_MAX_WORKERS` 控制默认线程池。
-   **`StartupTaskProcessor`**：仅当 `runs_knowledge_runtime` 且 `STARTUP_TASK_PROCESSOR_ENABLED=true` 时启动（**默认 `false`**），见 §5。
-   **运行阶段**: 注册 v1/v2 路由、挂载 `/ui` 前端静态文件、`/static/html-ppt-assets`、`/data/charts` 降级图表目录、`/mcp/sse` MCP SSE 子应用，并暴露 `/health`、`/live`、`/config`、`/metrics`。
-   **关闭阶段**: 优雅停止相应角色的后台任务、停止索引同步调度器、关闭当前配置的 Checkpointer（主 API 可为 SQLite 或 Redis）和异步 MySQL 引擎。

主 API 通过 `AGENT_CHECKPOINTER_BACKEND` 选择 Checkpointer：默认 `sqlite`，写入 `backend/data/checkpoints.db`；多 Worker/多实例可设为 `redis`，使用共享 Redis。独立访客 API 仍固定使用 `backend/data/guest_checkpoints.db` 的 SQLite Checkpointer。

**路由结构**: 采用分层路由模型 (`api/routers/`)，实现认证、知识库、RAG 及智能体接口的彻底解耦。

**全局异常处理**:
-   `global_exception_handler`: 捕获所有未处理异常，返回 500 + 错误详情。
-   `http_exception_handler`: 统一 HTTP 异常响应格式。

**MCP SSE 集成**:
-   主 API 通过 `backend.application.mcp.create_mcp_sse_app()` 挂载 `/mcp/sse`。
-   管理/配置查询路由挂载在 `/api/v1/mcp`；可选独立服务入口仍是 `python -m backend.api.mcp_server`。

### 1.1 当前 API 模块边界速览

| 模块 | 实际挂载前缀 | 边界说明 |
| :--- | :--- | :--- |
| 主站认证 | `/api/v1/auth` | 登录、CAS、登出、Token 验证、用户资料与改密 |
| 主站短信认证 | `/api/send-sms-code`, `/api/register`, `/api/forgot-password`, `/api/sms-captcha` | `auth_sms.py` 直接挂载根前缀，兼容旧前端 |
| 访客短信认证 | `/api/v1/guest-sms` | 图形验证码、短信发送、访客注册/登录；当前由独立 `guest_http_server.py` 注册 |
| 知识库/目录/文档 | `/api/v1/knowledge_bases`, `/api/v1/knowledge_bases/{kb_id}/directories`, `/api/v1/knowledge_bases/{kb_id}/documents` | KB 元数据、目录树、文档上传/移动/排序/删除 |
| 上传与入库 | `/api/v1/uploads`, `/api/v1/ingestion`, `/api/v1/documents` | `uploads.py` 管 PendingFile 与分块上传；`document_ingestion.py` 管入库任务；`document_processing/` 管传统文档处理任务 |
| RAG | `/api/v1/rag` | `retrieve`、`retrieve_internal`、`internal/multi-kb-search`、`query_stream`、健康和管理接口 |
| 自研入库管理 | `/api/v1/admin/self-ingestion`, `/api/v1/health/self-ingestion` | jobs、质量事实、修复、backend 切换、shadow rebuild；两个入口均依赖管理员鉴权 |
| 提示词管理 | `/api/v1/prompts` | 当前管理 `extraction_schema` 类提示词/Schema 模板 |
| 自定义学习 | `/api/v1/custom-learning` | 学习计划、节点、练习、资源扩展与计划调整 |
| 学习风格 | `/api/v1/learning-style` | 问卷、答题、学习画像与偏好开关 |
| 管理后台 | `/api/v1/admin`, `/api/v1/admin/conversations` | 用户、API Key、审计日志、任务、系统健康、跨用户会话 |
| 模型/ASR | `/api/v1/models`, `/api/v1/asr/transcribe` | 模型 CRUD 主要写 `usage_scope=chat`；ASR 从 `usage_scope=asr` 读取 |
| Agent 与 Skills | `/api/v1/agents`, `/api/v1/agents/{agent_code}/skills`, `/api/v1/skills` | Agent 注册、KB 绑定、Agent-Skill 绑定、Skill ZIP 上传、版本发布、ACL 授权与审计 |
| v2 Agent 运行时 | `/api/v2/agent-runtime`, `/api/v2/guest`, `/api/v2/explore` | 由 `agent_runtime_v2.py` 聚合运行时、访客问答和探索学习 |
| 会话/文件/工件 | `/api/v2/conversations`, `/api/v2/chat-files`, `/api/v2/files`, `/api/v2/threads/{thread_id}/artifacts/{path}` | 会话 CRUD、附件、MinIO/KB 图片代理、线程工件读取 |

---

### 2. 异步数据库架构 (`db/connection.py`)

**技术栈**: **SQLAlchemy 2.0 (Async)** + **aiomysql**（MySQL 异步驱动）。

**双引擎设计**:
-   **同步引擎**: `get_engine()` → `create_engine` + `QueuePool`（`pymysql`），用于 Worker、模型工厂同步 fallback、初始化脚本等同步场景。
-   **异步引擎**: `get_async_engine()` 在有运行事件循环时按 loop 缓存独立 `create_async_engine`（aiomysql pooled engine），用于 FastAPI 路由；只有无运行事件循环的 CLI/bootstrap 兼容路径才使用 `NullPool`，避免跨事件循环复用同一个异步连接池。

**连接池配置**:
-   `pool_size`: 可配置连接池大小。
-   `max_overflow`: 可配置最大溢出连接数。
-   每个进程的理论连接上限为 `pool_size + max_overflow`；多 worker 部署需再乘以进程数，并低于 MySQL `max_connections`，否则应先扩容数据库而不是盲目增大连接池。
-   `pool_pre_ping=True`: 每次从池中取连接时先检测存活，避免断连错误。
-   `pool_recycle=3600`: 连接使用 1 小时后自动回收重建。

**会话管理**:
-   `get_async_session()`: FastAPI 依赖注入用的异步会话生成器，自动 commit/rollback。
-   `async_session_context()`: 上下文管理器形式的异步会话，用于 Worker 等非 DI 场景。
-   字符集强制 `utf8mb4`，确保中文与特殊字符正确存储。

---

### 3. 数据库模型概览

核心业务表分布在 `backend/db/models/` 多个文件中：

| 表名 | 模型文件 | 说明 |
| :--- | :--- | :--- |
| `users` / `api_keys` | `__init__.py` | 用户与 API Key |
| `conversations` | `__init__.py` | 会话元数据（MySQL 持久化） |
| `user_files` / `session_files` | `__init__.py` | 个人文件库与会话绑定 |
| `knowledge_bases` | `knowledge_base.py` | 知识库元数据 |
| `kb_documents` | `knowledge_base.py` | 知识库文档关联 |
| `kb_directories` | `knowledge_base.py` | 目录树 |
| `kb_pending_files` | `knowledge_base.py` | 入库任务队列 |
| `kb_self_ingest_jobs` / `kb_self_ingest_chunks` | `knowledge_base.py` | 自研入库 job/chunk 审计与血缘 |
| `llm_models` / `llm_model_access` | `llm_model.py` | 模型配置与访问控制 |
| `agents` / `agent_kb_bindings` | `agent.py` | 智能体与 KB 绑定 |
| `skills` / `skill_versions` / `skill_acl` / `agent_skill_bindings` / `skill_audit_logs` | `skill.py` | Skill 目录、不可变版本、ACL、Agent 绑定与审计 |
| `experiment_datasets` | `dataset.py` | 实验数据集 |
| `learning_style_attempts` / `learning_style_profiles` | `__init__.py` | 学习风格测评与用户画像 |
| `learning_plans` / `learning_plan_nodes` / `learning_node_attempts` / `learning_plan_adjustments` | `custom_learning.py` | 自定义学习闭环 |
| `groundwater_projects` / `groundwater_project_users` | `groundwater_project.py` | 地下水项目包与用户授权 |
| `context_artifacts` | `context_artifact.py` | Agent 运行上下文工件元数据 |

知识库相关表详情见下表；其他模块文档：

- 模型管理 → [model_management_module.md](./model_management_module.md)
- Agent 绑定 → [agent_kb_binding_module.md](./agent_kb_binding_module.md)
- 数据集 → `backend/api/routers/datasets.py`
- 个人文件 → `backend/api/routers/my_files.py`

**知识库核心表：**

| 表名 | 说明 | 关键字段 |
| :--- | :--- | :--- |
| `knowledge_bases` | 知识库管理 | `kb_id`(PK), `kb_code`(Unique), `kb_name`, `status`(Enum), `is_default` |
| `kb_documents` | 知识库文档关联 | `id`(PK), `kb_id`(FK), `doc_id`, `file_name`, `parse_status`, `storage_status`, `category_path` |
| `kb_directories` | 知识库目录树 | `id`(PK), `kb_id`(FK), `category_path`, `parent_path`, `depth`, `name`, `icon`, `color` |
| `kb_pending_files` | 待入库文件缓存 | `id`(PK), `kb_id`(FK), `file_id`(Unique), `file_name`, `minio_path`, `status`(0-3), `retry_count` |

**知识库状态枚举** (`KnowledgeBaseStatus`):
-   `CREATING` → `ACTIVE` → `INACTIVE` → `DELETING`

**待入库文件状态** (`PendingFileStatus`):
-   `0` (待入库) → `1` (入库中) → `2` (已入库) / `3` (失败)

**索引设计**: 每个表均设计了复合索引，覆盖按 `kb_id` + 状态/日期/路径的常见查询模式。

### 3.1 RAG 向量与图存储（与 MySQL 元数据配合）

| 组件 | 用途 | 代码/文档 |
| :--- | :--- | :--- |
| **Milvus** | legacy：实体/关系/chunk 向量；self：`chunks_v2` / `entities_v2` 等独立 collection | `backend/rag/storages/`、`backend/rag/ingestion/storage/milvus_writer.py` |
| **NebulaGraph** | legacy：LightRAG 图谱；self：v2 tag/edge 命名空间 | `nebula_storage.py`、`ingestion/storage/nebula_writer.py` |
| **Redis** | legacy：LightRAG KV；self：BM25 倒排索引（独立 key 前缀） | `RagService` / `ingestion/storage/redis_writer.py` |
| **MinIO** | 原始文件与解析产物 | 见下文 §4 |

检索编排与 Profile → [rag_module.md](./rag_module.md)、[知识库算法和架构.md](../知识库算法和架构.md)。

---

### 4. MinIO 对象存储集成 (`storage/__init__.py` + `core/storage/scoped_storage.py`)

**定位**: 后端原始文件与附件的唯一存储来源。

**核心组件**:
-   **MinIO 客户端**: 全局单例，自动创建 Bucket。
-   **ChunkedUploadManager**: 分块上传管理器，支持大文件分块上传、进度追踪、过期会话自动清理。
-   **StorageManager**: 文件存储管理器，支持原子写入、SHA256 哈希校验、过期文件清理。
-   **ScopedStorage**: 原生工具/报告工件使用的用户线程前缀封装，只允许 `users/{user_id}/threads/{thread_id}/artifacts/` 作用域读写。

**交互方式**:
-   `upload_bytes_to_minio()`: 流式上传字节内容到 MinIO。
-   `get_presigned_url()`: 生成预签名 URL（默认 24 小时有效）。
-   `get_public_url()`: 生成公开访问 URL。

**分块上传流程**:

```text
init_upload → save_chunk (×N) → complete_upload → MinIO 存储
    → kb_pending_files (status=0)
    ↑ 支持: 进度查询 / 取消上传 / 过期清理
```

---

### 5. 启动时后台任务检查 (`services/startup_task_processor.py`)

**开关**: `STARTUP_TASK_PROCESSOR_ENABLED`（默认 **`false`**）。生产环境更常用独立 **Ingest Worker**（§6）；启用本处理器时需在 `.env` 显式打开。

**机制**: 在主 API 事件循环中运行的后台协程，每 30 秒轮询 `kb_pending_files`。

**处理范围**（与 Ingest Worker 分工）:
- 仅处理 **`status=1`（入库中）** 的记录，**不**将 `status=0` 批量提升为 1。
- `status=0`（待入库）由上传/管理 API 或 Ingest Worker 认领链路负责。

**处理流程**（双轨，与 Ingest Worker 一致）:
1.  查询 `status=1` 的待处理文件（批量上限 100），经 Redis 分布式锁 `kb_ingest_lock:{file_id}` 防重。
2.  使用 `asyncio.Semaphore` 控制并发（`settings.max_concurrent_files`，默认 5）。
3.  读取 `kb_pending_files.ingestion_backend` 分流：
    - `legacy_lightrag`：MinIO 下载 → RAGAnything 解析 → LightRAG 入库；
    - `self_ingestion`：`SelfIngestionPipeline.ingest_pending_file()`（需全局开关，否则标记 blocked）。
4.  成功 → `status=2`；失败/blocked → `status=3` + 错误信息；完成后清理临时缓存。

**优势**: API 进程重启后，可继续消化已标记为「入库中」的残留任务。

---

### 6. 独立入库 Worker (`worker/ingest_worker.py`)

**定位**: 可独立部署的入库进程（`make run-ingest-worker`），从 `kb_pending_files` 拉取 **`status=1`** 任务，**不经过 Kafka**。

**配置项**:
| 环境变量 | 默认 | 说明 |
| :--- | :--- | :--- |
| `INGEST_WORKER_POLL_INTERVAL` | `2` | 轮询间隔（秒） |
| `INGEST_WORKER_BATCH_SIZE` | `5` | 每批成功处理目标数 |
| `INGEST_WORKER_CANDIDATE_MULTIPLIER` | `10` | 候选拉取倍数，`candidate_limit = batch_size × multiplier` |

**核心组件**: `PendingIngestProcessor`（`worker/components/ingest_pending_processor.py`）— 经 `PendingIngestUseCase` 双轨路由，legacy 走 RAGService，self 走 `SelfIngestionPipeline`。

> 自研入库管理 API：`/api/v1/admin/self-ingestion/*`（`self_ingestion_admin.py`）；健康检查别名 `GET /api/v1/health/self-ingestion`，同样通过 `require_admin` 校验。详见 [self_ingestion_quickstart.md](../self_ingestion_quickstart.md)。

> **勿与 Kafka Worker 混淆**：`ResultPersister` / `ResultPublisher` / `TaskConsumer` 属于 **`worker/task_worker.py`** 的文档解析 Kafka 流水线，不是 ingest worker。

---

### 7. Kafka 消息队列 (`kafka/`)

**组件**:
-   `client.py`: Kafka 客户端封装。
-   `consumer.py`: 消费者（文档任务 Worker 使用）。
-   `admin.py`: 管理端，Topic 创建与配置。
-   `schemas.py` / `log_schemas.py`: 消息 Schema。

**用途**: **文档解析/任务 Worker**（`make run-worker`）的异步分发与结果回传；与 §6 的 **PendingFile 入库 Worker** 为两条独立链路。

---

### 8. 启动脚本与容器边界

**CLI 入口** (`backend/cli.py`):
- `python -m backend.cli run-api --service-role {all,agent,knowledge} --host ... --port ... --workers 1`
- `python -m backend.cli run-worker` 启动 Kafka 文档任务 Worker。
- `python -m backend.cli run-ingest-worker --workers 1` 启动 PendingFile 入库 Worker。

**运维脚本**:
- `scripts/run_agent_api.sh`：设置 `API_SERVICE_ROLE=agent`，默认端口 `8082`，默认 `RAG_SERVICE_BASE_URL=http://127.0.0.1:8086`，可通过 `AGENT_API_RELOAD=true` 开启 reload。
- `scripts/run_knowledge_api.sh`：设置 `API_SERVICE_ROLE=knowledge`，默认监听 `127.0.0.1:8086`，并显式 `unset RAG_SERVICE_BASE_URL`，避免知识库服务代理到自身。
- `scripts/run_guest_backend.sh` / `scripts/guest_backend_service.sh`：启动 `backend.api.guest_http_server`，默认端口 `8083`，默认使用 `guest_checkpoints.db`。
- `scripts/agent_api_service.sh`、`scripts/knowledge_api_service.sh`、`scripts/ingest_worker_service.sh`：提供 `start|stop|restart|status` 守护式管理，并统一清理代理环境变量，避免内网服务调用走终端代理。

**Docker**:
- `Dockerfile` 的 builder/runtime 均基于 `python:3.13-slim`，builder 先构建 wheel，runtime 以非 root 用户安装并运行；镜像暴露 `8082`，默认执行 `python -m backend.api.http_server`。
- `docker-compose.yml` 当前编排 `api`、`worker`、`ingest-worker`、`mysql`、`redis`、`zookeeper`、`kafka`、`minio`、`prometheus`、`grafana`。Milvus、NebulaGraph 未在该 compose 文件内声明，需要通过 `APP_ENV_FILE` 指向的应用环境文件接入外部服务或另行编排；Compose 插值密钥来自根目录 `.env`。
- compose 中 `api` 显式 `STARTUP_TASK_PROCESSOR_ENABLED=false`，入库推荐走独立 `ingest-worker`。

---

### 9. 认证系统 (`api/routers/auth.py`)

**认证方式**:
-   **Bearer Token**: 登录后获取 Token（24 小时有效），存于 **Redis** (`auth:token:{token}`)，支持分布式部署与 Token 持久化，避免重启服务导致 Token 失效。
-   **API Key**: 通过 `verify_api_key` 依赖注入，用于服务间调用。

**核心端点**（挂载前缀 **`/api/v1/auth`**）:
-   `POST /api/v1/auth/login`: 用户登录，返回 Token + 用户信息。
-   `POST /api/v1/auth/logout`: 登出，清除 Token。
-   `GET /api/v1/auth/verify`: 验证 Token 有效性。
-   `GET /api/v1/auth/me`: 获取当前用户信息。
-   `POST /api/v1/auth/change-password`: 修改密码（需验证旧密码）。

**密码安全**: 使用 `werkzeug.security` 的 `generate_password_hash` / `check_password_hash`。

**短信认证**（独立路由，见 `backend/api/routers/auth_sms.py`）:
-   `GET /api/sms-captcha`: 获取图形验证码
-   `POST /api/send-sms-code`: 发送验证码
-   `POST /api/register`: 验证码注册
-   `POST /api/forgot-password`: 验证码重置密码

**访客短信认证**（`backend/api/routers/guest_sms_auth.py`，在独立 guest API 挂载 `/api/v1/guest-sms`）:
-   `GET /api/v1/guest-sms/captcha`: 获取访客图形验证码
-   `POST /api/v1/guest-sms/send-code`: 发送访客短信验证码
-   `POST /api/v1/guest-sms/register`: 访客注册
-   `POST /api/v1/guest-sms/login`: 访客短信登录

---

## 🔧 运维排障速查

| 现象 | 检查项 |
| :--- | :--- |
| 文档一直 status=1 | `make run-ingest-worker` 是否运行；Redis 锁 `kb_ingest_lock:*`；`ingest_worker.log` |
| 重启后无人处理 status=0 | 正常：需 API/上传流程将 0→1，或管理后台 `/api/v1/uploads/ingest` |
| API 启动慢 | `STARTUP_INIT_TIMEOUT_SECONDS`（默认 12s）后 RAG 预热转后台 |
| Token 登录失效 | Redis 是否可达；键 `auth:token:{token}` |
| `/api/v1/rag/health` 401 | 需 `X-API-Key` 或 Bearer，非匿名端点 |

主 API 路由注册见 `backend/api/http_server.py`（`include_router` 列表）；访客精简集见 `guest_http_server.py`。

---

## 🚀 如何进行后续开发 (Future Development)

### 1. 微服务化扩展 (Service Decoupling)
-   **建议**: 对于重负载的任务（如海量 PDF 解析、混合索引同步），未来可将其拆分为独立的 Python 进程或微服务，通过消息队列 (如 RabbitMQ) 进行异步通信。当前 `IngestWorker` 已具备独立部署能力，可作为微服务化的起点。

### 2. 数据加密与脱敏 (Security Hardening)
-   **方向**: 在 `storage/` 层集成 SSE (服务端加密)，并对 `db/` 中存储的个人信息及 API Key 进行动态掩码或全链路加密。当前密码已使用 `werkzeug` 哈希，但 API Key 仍以明文存储。

### 3. 多租户隔离优化 (Multi-tenancy)
-   **现状**: ✅ 已实现基于 `user_id` 的存储逻辑隔离。系统通过 `ScopedStorage`（MinIO 前缀隔离）和 `ScopedSkillsProvider`（Skills 动态过滤）确保了原生环境下的用户数据安全。
-   **规划**: 后续开发可在 Schema 层面引入更深度的 `tenant_id`，实现计算资源的物理隔离。

### 4. 全链路可观测性 (Observability)
-   **落地**: 集成 **OpenTelemetry** 或 **Prometheus** 指标。监控 RAG 检索各个阶段的耗时 (Latency) 和成功率 (Success Rate)，并对后端慢查询进行自动预警。当前已有 `backend/config/loggers.py` 提供结构化日志，可在此基础上接入 OTLP Exporter。

### 5. Token 存储优化 (已完成)
-   **现状**: ✅ 已实现。系统使用 Redis 管理 Token (`auth:token:{token}`)，支持分布式部署，并在服务重启后保持登录状态有效。

---
> [!CAUTION]
> 在修改 `db/models/` 时，必须使用 **Alembic** 生成迁移脚本，严禁直接通过 SQL 或手动方式修改生产环境数据库结构。
