# Infrastructure (基础设施与 API) 模块详解

> **最后同步**：2026-06-01（含双 Worker 链路、PendingFile 状态机、Lifespan ASCII）  
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

### 1. FastAPI 核心与生命周期 (`api/http_server.py`)

**功能**: 管理应用启动 (Lifespan)、中间件 (CORS) 以及异常处理中心。

**生命周期管理**:
-   **启动阶段** (`lifespan`): 并行初始化 RAG 服务、关键词索引预加载、索引同步调度器，超时后自动切换为后台继续执行（`STARTUP_INIT_TIMEOUT_SECONDS`，默认 12s）。
-   **`StartupTaskProcessor`**：仅当 `STARTUP_TASK_PROCESSOR_ENABLED=true` 时启动（**默认 `false`**），见 §5。
-   **运行阶段**: 注册所有 v1/v2 路由、挂载前端静态文件、挂载 MCP SSE 端点。
-   **关闭阶段**: 优雅停止后台任务、清理索引同步调度器、释放资源。

**路由结构**: 采用分层路由模型 (`api/routers/`)，实现认证、知识库、RAG 及智能体接口的彻底解耦。

**全局异常处理**:
-   `global_exception_handler`: 捕获所有未处理异常，返回 500 + 错误详情。
-   `http_exception_handler`: 统一 HTTP 异常响应格式。

**MCP SSE 集成**:
-   通过 `FastMCP` 定义 MCP 工具 (`query_knowledge_base`)。
-   使用 `SseServerTransport` 实现 SSE 传输，挂载到 `/mcp/sse` 路径。

---

### 2. 异步数据库架构 (`db/connection.py`)

**技术栈**: **SQLAlchemy 2.0 (Async)** + **aiomysql**（MySQL 异步驱动）。

**双引擎设计**:
-   **同步引擎**: `get_engine()` → `create_engine` + `QueuePool`（`pymysql`），用于 Worker 等同步场景。
-   **异步引擎**: `get_async_engine()` → `create_async_engine` + `NullPool`，用于 FastAPI 路由，避免跨事件循环连接复用问题。

**连接池配置**:
-   `pool_size`: 可配置连接池大小。
-   `max_overflow`: 可配置最大溢出连接数。
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
| `llm_models` / `llm_model_access` | `llm_model.py` | 模型配置与访问控制 |
| `agents` / `agent_kb_bindings` | `agent.py` | 智能体与 KB 绑定 |
| `experiment_datasets` | `dataset.py` | 实验数据集 |

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
| **Milvus** | 实体/关系/chunk 向量；每 KB 独立 collection | `backend/rag/storages/` |
| **NebulaGraph** | 知识图谱；每 KB 独立 Space（`kb_code`） | `nebula_storage.py` |
| **Redis** | LightRAG KV：`full_docs`、`text_chunks`、`doc_status` 等 | `RagService` 初始化 |
| **MinIO** | 原始文件与解析产物 | 见下文 §4 |

检索编排与 Profile → [rag_module.md](./rag_module.md)、[知识库算法和架构.md](../知识库算法和架构.md)。

---

### 4. MinIO 对象存储集成 (`storage/__init__.py`)

**定位**: 后端原始文件与附件的唯一存储来源。

**核心组件**:
-   **MinIO 客户端**: 全局单例，自动创建 Bucket。
-   **ChunkedUploadManager**: 分块上传管理器，支持大文件分块上传、进度追踪、过期会话自动清理。
-   **StorageManager**: 文件存储管理器，支持原子写入、SHA256 哈希校验、过期文件清理。

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

**处理流程**:
1.  查询 `status=1` 的待处理文件（批量上限 100），经 Redis 分布式锁 `kb_ingest_lock:{file_id}` 防重。
2.  使用 `asyncio.Semaphore` 控制并发（`settings.max_concurrent_files`，默认 5）。
3.  对每个文件：从 MinIO 下载 → RAGAnything 多模态解析 → LightRAG 入库。
4.  成功 → `status=2`；失败 → `status=3` + 错误信息；完成后清理临时缓存。

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

**核心组件**: `PendingIngestProcessor`（`worker/components/ingest_pending_processor.py`）— 单文件下载、解析、LightRAG 入库与状态回写。

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

### 8. 认证系统 (`api/routers/auth.py`)

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
-   `POST /api/send-sms-code`: 发送验证码
-   `POST /api/register`: 验证码注册
-   `POST /api/forgot-password`: 验证码重置密码

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
