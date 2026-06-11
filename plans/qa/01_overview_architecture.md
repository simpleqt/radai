# 01 · 总体架构与入口

> 📊 **图表：** 完整 ASCII 图见 [00_architecture_diagrams.md](./00_architecture_diagrams.md) 图 1–3、17–19、**28**；正式文档 → [architecture_overview.md](../../docs/architecture_overview.md) · [infrastructure_module.md](../../docs/docs_backend/infrastructure_module.md)

---

## L1 初级 🟢

### Q1：这个 backend 项目的技术栈是什么？整体分几层？

**一句话结论：** 这是一个基于 FastAPI + LangGraph 的「AI 文档处理 + 多域智能问答」后端，采用分层架构，上层 HTTP/CLI 入口，中层 Agent 编排，下层 RAG 与多种存储。

#### 为什么问这个 / 背景

面试官通常先用这道题判断你是否「看过项目全貌」。技术栈决定了你能聊多深：比如 RAG 链路、流式 SSE、多 worker 部署时的 checkpoint 锁问题，都建立在同一套分层之上。回答时既要列技术名词，也要说明**每层负责什么、数据怎么流**。

#### 详细解释

**六层架构（ASCII 架构图）：**

```text
┌───────────────────────────────────────────────────────────────┐
│                             L1 入口                             │
│ cli.py  │  Uvicorn                                            │
└───────────────────────────────────────────────────────────────┘
                                │
                               ▼
┌───────────────────────────────────────────────────────────────┐
│                            L2 Web                             │
│ Middleware  │  8082 / 8083                                    │
└───────────────────────────────────────────────────────────────┘
                                │
                               ▼
┌───────────────────────────────────────────────────────────────┐
│                             L3 路由                             │
│ v1 管理 API  │  v2 Agent Runtime API  │  MCP SSE            │
└───────────────────────────────────────────────────────────────┘
                                │
                               ▼
┌───────────────────────────────────────────────────────────────┐
│                           L4 Agent                            │
│ AgentService  │  WorkflowRegistry  │  AgentProfile            │
└───────────────────────────────────────────────────────────────┘
                                │
                               ▼
┌───────────────────────────────────────────────────────────────┐
│                             L5 能力                             │
│ Tools/RAG/Workers                                             │
└───────────────────────────────────────────────────────────────┘
                                │
                               ▼
┌───────────────────────────────────────────────────────────────┐
│                             L6 存储                             │
│ (MySQL/SQLite/Redis/Milvus/...)                               │
└───────────────────────────────────────────────────────────────┘

连接:
  cli.py ──────► Uvicorn
  Middleware ──────► 8082 / 8083
  AgentService ──────► WorkflowRegistry ──────► Executors
  L1 ──────► L2 Web

```

**典型问答时序：**

```text
┌────────────┐ ┌────────────┐ ┌────────────┐ ┌────────────┐ ┌────────────┐ ┌────────────┐
│     用户     │ │  FastAPI   │ │   Router   │ │   Agent    │ │    RAG     │ │    LLM     │
└────────────┘ └────────────┘ └────────────┘ └────────────┘ └────────────┘ └────────────┘


```

项目可以按「请求从哪进、在哪算、存哪去」理解，自上而下分为六层：

```
┌─────────────────────────────────────────────────────────┐
│  L1 入口层：CLI (doc-agent) / Uvicorn ASGI              │
├─────────────────────────────────────────────────────────┤
│  L2 Web 层：FastAPI + Middleware（CORS、用户上下文）     │
├─────────────────────────────────────────────────────────┤
│  L3 路由层：v1 管理 API + v2 Agent Runtime + MCP SSE    │
├─────────────────────────────────────────────────────────┤
│  L4 Agent 层：AgentService + WorkflowRegistry + Profile │
├─────────────────────────────────────────────────────────┤
│  L5 能力层：Tools / Skills / RAG Service / Worker       │
├─────────────────────────────────────────────────────────┤
│  L6 存储层：MySQL / SQLite / Redis / Milvus / Graph ... │
└─────────────────────────────────────────────────────────┘
```

核心技术栈对照表：

| 类别 | 技术 | 在本项目中的角色 |
|------|------|------------------|
| Web 框架 | FastAPI + Uvicorn | HTTP API、SSE 流式、lifespan 预热 |
| Agent 运行时 | LangChain Agents + LangGraph + AgentService | 图状态机、工具调用、checkpoint、工作流编排 |
| LLM 接入 | OpenAI 兼容 API | MiniMax、DeepSeek、Qwen、GLM 等，经网关统一 |
| RAG | LightRAG + Milvus + NebulaGraph | 向量检索 + 知识图谱 + 关键词索引 |
| 业务数据库 | MySQL + SQLAlchemy 2.0 | 用户、会话元数据、文档任务、模型配置 |
| 消息队列 | Kafka (aiokafka) | 文档 OCR/抽取异步 Worker |
| 缓存/会话 | Redis | 登录 Token、RAG KV、模型配置变更订阅 |
| Agent 状态 | SQLite (LangGraph Checkpointer) | 对话历史、图节点 checkpoint |
| 文件存储 | MinIO + 本地 `data/` | 文档、图表、上传文件 |

一次典型问答的数据流：

```
┌─────────┐  POST /v2/agent-runtime/query  ┌──────────┐  鉴权   ┌───────────────┐
│ 用户/前端│ ──────────────────────────────► │ FastAPI  │ ──────► │ AgentService  │
└─────────┘                                 └────┬─────┘         └───────┬───────┘
                                                │                       │
                                                │                       ▼
                                                │              ┌────────────────┐
                                                │              │ WorkflowRegistry│
                                                │              │ (YAML 驱动)   │
                                                │              └──────┬─────────┘
                                                │                     │
                    ┌───────────────────────────┼───────────────────┤
                    ▼                   ▼                   ▼
              Milvus 向量          ProfiledAgent         StandardAgentWorkflow
              + 关键词图           Executor               CoordinatedAgentWorkflow
                    │                   │                   │
                    └───────────────────┴───────────────────┘
                                        │
                                        ▼
                              AgentEvent → SSEEncoder → 前端
                              chunk / process / done
```

#### 在本项目中的实现

- CLI 入口：`backend/cli.py`，默认启动 `backend.api.http_server:app`
- 主服务 lifespan 并行预热 RAG、关键词索引、Checkpointer 等：`backend/api/http_server.py`
- Agent 编排服务：`backend/application/agent_service.py` 的 `AgentService`
- 工作流注册表：`backend/config/workflows/registry.yaml` + `backend/application/agent_workflows/registry.py`
- Agent 建图工厂：`backend/core/agents/factory.py` 的 `create_agent_graph()`
- Profiled 图构建：`backend/application/agent_executors/graph_builder.py` 的 `ProfiledGraphBuilder`
- 模型列表：`backend/config.yaml` + `backend/core/models/factory.py`（可 DB 覆盖）
- 环境变量聚合：`backend/config/settings.py`（Pydantic Settings）

#### 走一遍具体例子

运维同学执行 `doc-agent run-api --port 8082 --workers 4`：

1. Uvicorn 加载 `http_server.py`，触发 `lifespan`：RAG 初始化、活跃 KB 预热、SQLite checkpointer 打开 `backend/data/checkpoints.db`
2. 用户在前端选「核电子学」域，POST 问题「PIN 二极管的工作原理？」
3. 请求经 Middleware 从 Redis 读出 `user_id`，AgentService 加载 NE AgentProfile
4. WorkflowRegistry 解析工作流类型，ProfiledAgentExecutor 构建图，multi_kb_search 检索 Milvus
5. LLM 流式生成 → AgentEvent → SSEEncoder 输出 SSE JSON → 前端打字机效果

#### 常见误区

- ❌ 认为所有对话状态都在 MySQL——**消息正文与图状态在 SQLite checkpoint**，MySQL 只存会话元数据（标题、user_id）
- ❌ 把 AgentService 当成简单的请求转发——实际它整合 Profile 加载、工作流解析、策略评估、上下文构建
- ✅ 分层回答：入口 → 路由 → AgentService → 工具/RAG → 存储，并点出异步 Worker（Kafka）与 API 进程分离

#### 延伸追问（L2/L3 only）

- 为什么 Agent checkpoint 用 SQLite 而业务用 MySQL？多 worker 下 SQLite 有什么风险？
- `MODEL_CONFIG_SOURCE=db` 时模型热更新如何生效？（Redis 订阅，`http_server.py` lifespan）

---

### Q2：项目有几个 HTTP 服务入口？分别做什么？

**一句话结论：** 有两个独立 FastAPI 应用：主 API（8082，全功能）和访客 API（8083，精简攻击面），且使用不同的 SQLite checkpoint 文件物理隔离会话状态。

#### 为什么问这个 / 背景

双服务是刻意的安全与运维设计，不是简单复制一份代码。面试官想确认你是否理解「为什么要拆」以及「拆了之后有什么代价」。

#### 详细解释

```text
┌────────────┐
│ 主站 :8082 │
└────────────┘

  主站 :8082 ──────────────► (MySQL users)

  (MySQL users) ──────────────► 访客 :8083

  访客 :8083 ──────────────► (Redis Token)

```

| 服务 | 入口文件 | 默认端口 | 职责 | Checkpoint 文件 |
|------|----------|----------|------|-----------------|
| 主 API | `backend/api/http_server.py` | 8082 | 认证、KB 管理、v1/v2 Agent、MCP SSE、静态前端 | `backend/data/checkpoints.db` |
| 访客 API | `backend/api/guest_http_server.py` | 8083 | 登录 + 问答 + 会话列表（无管理面） | `backend/data/guest_checkpoints.db` |

访客服务启动时设置环境变量 `GUEST_API_SERVICE=1`，并在文件头注释中明确路由边界：

```
需要: POST /api/v1/auth/login, POST /api/v2/agent/query, GET conversations ...
不需要: 知识库管理、文档入库、MCP、Worker、管理端
```

分离的核心动机：

```
                    ┌──────────────┐
   公网/访客流量 ──→│ 8083 Guest   │── 最小路由集
                    │ guest_cp.db  │
                    └──────────────┘

                    ┌──────────────┐
   内网/管理员 ────→│ 8082 Main    │── 完整 CRUD + MCP
                    │ checkpoints  │
                    └──────────────┘
```

#### 在本项目中的实现

主服务 Checkpointer 初始化（注意实际路径是 `checkpoints.db`，与 `config.yaml` 中的 `app_checkpoints.db` 命名不同——YAML 供 Agent 配置加载，HTTP lifespan 写死 `data/checkpoints.db`）：

```384:397:backend/api/http_server.py
    async def _init_checkpointer() -> None:
        try:
            from langgraph.checkpoint.sqlite.aio import AsyncSqliteSaver
            import aiosqlite
            base_dir = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
            db_path = os.path.join(base_dir, "data", "checkpoints.db")
            os.makedirs(os.path.dirname(db_path), exist_ok=True)
            
            conn = await aiosqlite.connect(db_path)
            saver = AsyncSqliteSaver(conn)
            await saver.setup()
            app.state.checkpointer = saver
            app.state.checkpointer_conn = conn
            logger.info(f"Checkpointer (SQLite) 初始化完成: {db_path}")
```

访客服务：

```151:166:backend/api/guest_http_server.py
    async def _init_checkpointer() -> None:
        try:
            import aiosqlite
            from langgraph.checkpoint.sqlite.aio import AsyncSqliteSaver

            base_dir = Path(__file__).resolve().parents[1] / "data"
            base_dir.mkdir(parents=True, exist_ok=True)
            db_path = base_dir / GUEST_CHECKPOINT_DB

            conn = await aiosqlite.connect(str(db_path))
            saver = AsyncSqliteSaver(conn)
            await saver.setup()
            app.state.checkpointer = saver
            app.state.checkpointer_conn = conn
            app.state.checkpoint_db = str(db_path)
            logger.info("[Guest] Checkpointer 初始化完成: %s", db_path)
```

#### 走一遍具体例子

某高校部署：公网域名 `guest.example.edu` 反代到 8083，教职工域名 `radai2.cdut.edu.cn` 反代到 8082。

- 访客在 8083 登录问答，LangGraph thread 写入 `guest_checkpoints.db`
- 管理员在 8082 上传 PDF、绑定 KB、配置 `llm_models` 表
- 即使访客 token 泄露，也无法调用 8082 上的 `/api/v1/knowledge-base/*`（路由根本不存在）

#### 常见误区

- ❌ 认为两个服务共享同一个 checkpoint DB——**物理文件不同**，thread_id 即使相同也不会读到对方状态
- ❌ 以为访客服务「不需要 RAG」——访客仍会预热 `DEFAULT_KB_ID` 和 `PAPER_KB_ID` 两个知识库
- ✅ 强调「攻击面最小化 + 状态隔离 + 可独立扩缩容」三角权衡

#### 延伸追问（L2/L3 only）

- 双服务如何避免配置 drift？能否合并为单服务 + RBAC + checkpoint namespace？
- 访客 API 默认 `GUEST_SERVER_WORKERS=1`，与主站 `--workers 4` 的差异原因？

---

### Q3：CLI 提供了哪些常用命令？

**一句话结论：** 通过 `doc-agent` 子命令统一管理 API 启动、Kafka Worker、数据库初始化、测试与代码质量检查。

#### 为什么问这个 / 背景

生产环境很少直接在 IDE 里点 Run，运维和 CI 都依赖 CLI。知道命令等于知道「最小可运行单元有哪些进程」。

#### 详细解释

**CLI 进程拓扑（ASCII 架构图）：**

```text
┌──────────────────────────────────────┐
│ CLI                                  │
└──────────────────────────────────────┘
           │
           ▼
           │
           ▼
           │
           ▼
           │
           ▼
┌────────────────┐  ┌──────────────────────────┐  ┌────────────────┐  ┌────────────────┐
│ API            │  │ init-db / migrate / seed │  │ IW             │  │ TW             │
└────────────────┘  └──────────────────────────┘  └────────────────┘  └────────────────┘
           │
           ▼
           │
           ▼
           │
           ▼
           │
           ▼
┌─────────────────────┐  ┌────────────────┐  ┌─────────────────┐  ┌────────────────┐
│ Uvicorn http_server │  │ (Milvus)       │  │ (MySQL pending) │  │ (Kafka)        │
└─────────────────────┘  └────────────────┘  └─────────────────┘  └────────────────┘

```

```
doc-agent
    │
    ├── run-api ──────────────► Uvicorn :8082 (http_server.py)
    │                           └── lifespan 预热 RAG / checkpoint
    │
    ├── run-worker ───────────► Kafka Task Worker (OCR/解析)
    │
    ├── run-ingest-worker ────► DB 轮询 Ingest (向量化入库)
    │
    └── init-db / migrate / seed / test / lint ...
```

入口：`backend/cli.py`，使用 `argparse` 子命令模式。

| 命令 | 作用 | 关键参数/说明 |
|------|------|---------------|
| `run-api` | 启动主 API | `--port` 默认 8082；`--reload` 时不能多 worker |
| `run-worker` | Kafka 文档处理 Worker | 消费 OCR/抽取任务 |
| `run-ingest-worker` | 知识库入库 Worker | DB 轮询入库任务 |
| `init-db` | 初始化 MySQL 表 | 调用 `backend.db.init_db` |
| `migrate` | 数据库迁移 | |
| `seed` | 填充测试数据 | |
| `test` | pytest | 支持 `--cov` |
| `format` / `lint` | black/isort / flake8/mypy | |
| `clean` / `version` | 清理 / 版本 | 委托 make |

`run-api` 绑定 `0.0.0.0` 的注释说明：内部子智能体 HTTP 回调走本机回环地址，需本机可达；外网访问靠防火墙而非 bind 地址限制。

#### 在本项目中的实现

```81:99:backend/cli.py
    if args.command == "run-api":
        import uvicorn

        # 绑定到 0.0.0.0 确保内部 API 调用（本机回环地址）可达。
        # 外部访问控制应由防火墙/iptables 处理，而非 uvicorn 的 bind 地址。
        kwargs = {
            "host": "0.0.0.0",
            "port": args.port,
            "reload": args.reload,
        }
        
        # 仅当 reload 不是 True 时，UVicorn 才允许配置 multiple workers
        if not args.reload:
            kwargs["workers"] = args.workers

        uvicorn.run(
            "backend.api.http_server:app",
            **kwargs
        )
```

#### 走一遍具体例子

新环境首次部署：

```bash
doc-agent init-db          # 建 MySQL 表
doc-agent run-api --workers 4 --port 8082   # API 进程
doc-agent run-worker       # 文档处理（另开终端）
doc-agent run-ingest-worker  # 入库（另开终端）
# 访客前端还需单独启动 guest 服务（通常 uvicorn guest_http_server:app --port 8083）
```

#### 常见误区

- ❌ 只启动 `run-api` 就期望 PDF 自动入库——**入库依赖 ingest-worker + Kafka/DB 任务链**
- ❌ 开发时用 `--reload` 同时设 `--workers 4`——Uvicorn 不允许 reload 多 worker
- ✅ 至少三个进程：API + document worker + ingest worker（RAG 全功能场景）

#### 延伸追问（L2/L3 only）

- 访客服务为何没有 CLI 子命令？如何启动？
- `--workers 4` 与 SQLite checkpoint 写锁的关系？

---

### Q4：配置文件有哪些？各自管什么？

**一句话结论：** 配置分三层——`config.yaml`（Agent/模型运行时）、`settings.py`（基础设施连接）、`.env`（密钥），模型还可被 MySQL `llm_models` 表覆盖并 Redis 热更新。

#### 为什么问这个 / 背景

配置混乱是线上事故高发区。面试官考察你是否知道「改模型名该改哪」「改 MySQL 地址该改哪」，以及优先级顺序。

#### 详细解释

**配置三层（ASCII 架构图）：**

```text
┌───────────────────────────────────────────────────────────────┐
│                  模型配置 MODEL_CONFIG_SOURCE=db                  │
│ (MySQL llm_models)  │  YAML  │  settings/.env                 │
└───────────────────────────────────────────────────────────────┘
                                │
                               ▼
┌───────────────────────────────────────────────────────────────┐
│                             基础设施                              │
│ .env  │  settings.py                                          │
└───────────────────────────────────────────────────────────────┘
                                │
                               ▼
┌───────────────────────────────────────────────────────────────┐
│                           Agent 运行时                           │
│ config.yaml checkpointer/skills                               │
└───────────────────────────────────────────────────────────────┘

连接:
  (MySQL llm_models) ──────► YAML
  YAML ──────► settings/.env
  .env ──────► settings.py

```

```
┌─────────────────────────────────────────────────────────────┐
│ 模型配置（MODEL_CONFIG_SOURCE=db）                            │
│   MySQL llm_models  →  config.yaml models  →  settings/.env │
│   Redis 订阅热更新（管理面 CRUD 后推送各 worker）              │
├─────────────────────────────────────────────────────────────┤
│ 基础设施连接                                                  │
│   .env / 环境变量  →  settings.py（MySQL/Redis/Kafka/...）   │
├─────────────────────────────────────────────────────────────┤
│ Agent 运行时（config.yaml）                                   │
│   models[] · skills.path · checkpointer 类型                 │
│   ⚠ yaml checkpointer 路径 ≠ http_server 实际 checkpoints.db  │
└─────────────────────────────────────────────────────────────┘
```

```
优先级（模型配置，MODEL_CONFIG_SOURCE=db 时）:
  MySQL llm_models → config.yaml → settings/.env 默认值

优先级（基础设施）:
  环境变量 / .env → settings.py Field 默认值

优先级（Agent checkpointer 等）:
  config.yaml checkpointer 段（供 Agent 配置解析）
  ≠ http_server lifespan 实际使用的 checkpoints.db 路径（见 Q2）
```

| 文件 | 管理内容 | 典型键 |
|------|----------|--------|
| `backend/config.yaml` | 模型列表、skills 路径、title 生成、memory、checkpointer 类型 | `models[]`, `skills.path`, `checkpointer` |
| `backend/config/settings.py` | MySQL/Redis/Kafka/Milvus/NebulaGraph/CAS/日志 | `mysql_url`, `internal_api_secret`, `cas_*` |
| `backend/.env` | 本地密钥、连接串（不入库） | `MYSQL_*`, `REDIS_*`, `INTERNAL_API_SECRET` |

`config.yaml` 中模型示例（思考模式关闭）：

```yaml
checkpointer:
  type: sqlite
  connection_string: app_checkpoints.db   # Agent 配置语义；HTTP 层见 Q2 实际路径
```

#### 在本项目中的实现

- 模型工厂优先级文档：`backend/core/models/factory.py` 文件头注释
- lifespan 订阅 Redis 模型变更：`backend/api/http_server.py` 中 `subscribe_model_changes()`
- Settings 定义：`backend/config/settings.py`（Pydantic `BaseSettings`）

#### 走一遍具体例子

运维在管理后台修改「默认对话模型」：

1. 更新 MySQL `llm_models` 表某行 `is_active`、`base_url`
2. 后台发布 Redis 通知（model_cache 模块）
3. 各 Uvicorn worker 收到订阅，刷新内存模型列表
4. **无需重启** API 即可对新请求生效（已创建的 LLM 实例可能仍缓存，取决于工厂实现）

开发者本地换 DeepSeek 网关：

1. 改 `.env` 中 `DASHSCOPE_API_KEY` 或直接在 `config.yaml` 加模型条目
2. 若 `MODEL_CONFIG_SOURCE=yaml`，跳过 DB 读取

#### 常见误区

- ❌ 只改 `config.yaml` 但生产 `MODEL_CONFIG_SOURCE=db`——**DB 优先，YAML 改动不生效**
- ❌ 把 API Key 提交进 `config.yaml`——仓库中已有示例 key，生产应走 `.env` 或 DB 加密字段
- ✅ 说清三层职责 + 模型热更新路径

#### 延伸追问（L2/L3 only）

- `config_version: 6` 在 yaml 里做什么用？
- checkpointer 在 yaml 与 http_server 路径不一致，如何排查「会话丢失」？

---

## L2 中级 🟡

### Q5：主 API 启动时 lifespan 做了哪些初始化？为什么要并行？

**一句话结论：** lifespan 用 `asyncio.gather` 并行启动 RAG、索引预热、调度器、Checkpointer 等 IO 密集型任务，每项带超时；超时则降级为后台继续，保证端口尽快可用。

#### 为什么问这个 / 背景

冷启动慢会触发 K8s 健康检查失败、滚动发布卡住。并行 + 超时降级是「可用性优先」的工程取舍。

#### 详细解释

启动阶段任务清单（均在 `http_server.py` 的 `lifespan` 内）：

| 序号 | 任务名 | 作用 | 超时 |
|------|--------|------|------|
| 1 | RAG 初始化 | `get_rag_service().initialize()` | 默认 12s |
| 2 | RAG 实例预热 | 对所有 `active` KB 调用 `_get_rag_instance` | 12s |
| 3 | 关键词索引预加载 | 每 KB 加载 entity 索引 | 12s |
| 4 | 关键词图缓存预热 | `_build_full_graph()` | 最长 180s |
| 5 | RAG 索引同步调度器 | 定时同步 Milvus/图谱 | 12s |
| 6 | Checkpointer | SQLite `checkpoints.db` | 12s |
| 7 | 实验数据集表 | `init_dataset_table` | 12s |
| 8 | （并行于 gather 外）Redis 模型变更订阅 | 热更新模型 | 无超时，后台 Task |

并行结构：

**Lifespan 并行启动（ASCII 架构图）：**

```text
http_server.py lifespan 启动
        │
        ▼
 asyncio.gather（并行，默认各 12s 超时）
        │
   ┌────┼────┬────┬────┬────┬────┬────┐
   ▼    ▼    ▼    ▼    ▼    ▼    ▼    ▼
 RAG  预热  关键词 关键词图 索引  CP  数据集
 init      索引   缓存    sync
   │    │    │    │    │    │    │
   └────┴────┴────┴────┴────┴────┘
        │
        ├─ 超时 → _wait_with_timeout 转 startup_background_tasks
        └─ /health 尽快 200，重任务后台继续

（并行于 gather）Redis 模型变更订阅 → 热更新
```



```419:431:backend/api/http_server.py
    await asyncio.gather(
        _wait_with_timeout("RAG", _init_rag()),
        _wait_with_timeout("RAG实例预热", _prewarm_rag_instances()),
        _wait_with_timeout("关键词索引", _init_keyword_index()),
        _wait_with_timeout(
            "关键词图缓存预热",
            _prewarm_keyword_graph_cache(),
            timeout_seconds=STARTUP_KEYWORD_GRAPH_PREWARM_TIMEOUT_SECONDS,
        ),
        _wait_with_timeout("索引同步调度器", _init_index_sync_scheduler()),
        _wait_with_timeout("Checkpointer", _init_checkpointer()),
        _wait_with_timeout("实验数据集", _init_experiment_datasets()),
    )
```

`_wait_with_timeout` 逻辑：超时后不 kill 任务，而是 append 到 `startup_background_tasks`，lifespan yield 后继续跑。

**为何并行：** 任务彼此无硬依赖（都读 MySQL active KB 列表，但可各自容错），串行冷启动可能 30s+；并行缩短 wall time。

**为何超时降级：** 关键词图构建可能很慢；阻塞 lifespan 会导致 `/health` 迟迟不可用。

#### 在本项目中的实现

- 超时秒数：`STARTUP_INIT_TIMEOUT_SECONDS` 环境变量，默认 12
- 关键词图：`STARTUP_KEYWORD_GRAPH_PREWARM_TIMEOUT_SECONDS` 默认 180
- 可选关闭：`STARTUP_RAG_PREWARM_ENABLED=false`
- 关闭时还有 `STARTUP_TASK_PROCESSOR_ENABLED` 控制启动任务处理器

#### 走一遍具体例子

某 KB 有 50 万实体，关键词图预热超过 180s：

1. 日志：`[关键词图缓存预热] 初始化超过 180.0s，切换为后台继续执行`
2. `/health` 已返回 200，前端可登录
3. 后台线程继续建图；前几分钟的 RAG 检索可能慢半拍，之后恢复正常

#### 常见误区

- ❌ 认为预热失败 API 不会启动——多数任务 **catch 异常只打 warning**
- ❌ 忽略多 worker：每个 worker 进程 **各自跑一遍 lifespan**，RAG 预热内存不共享
- ✅ 说出并行 gather + shield + 超时转后台的三段式策略

#### 延伸追问（L2/L3 only）

- 如何观测「后台预热是否完成」？（日志 `[RAG预热] 完成`）
- 生产是否应关闭部分预热以换更快启动？

---

### Q6：`config.yaml` 里的 checkpointer 配置意味着什么？

**一句话结论：** 它声明 LangGraph Agent 会话状态应持久化到 SQLite 文件，与 MySQL 业务库解耦；但 HTTP 服务 lifespan 实际挂载的是 `data/checkpoints.db`，与 yaml 中的文件名可能不一致，排查时要注意。

#### 为什么问这个 / 背景

Checkpoint 是对话「记忆」的存储位置。混淆 yaml 配置与实际文件路径，会导致「本地有会话、线上找不到」类 bug。

#### 详细解释

**Checkpointer 配置 vs 运行时（ASCII 架构图）：**

```text
┌──────────────────────┐
│ http_server lifespan │
└──────────────────────┘

  http_server lifespan ──────────────► (data/checkpoints.db)

  (data/checkpoints.db) ──────────────► guest_http_server

  guest_http_server ──────────────► (guest_checkpoints.db)

  http_server lifespan ──(实际挂载)──► (data/checkpoints.db)

```

```
config.yaml                          http_server.py lifespan
┌─────────────────────┐              ┌─────────────────────────┐
│ checkpointer:       │   语义 ≠     │ 实际打开：               │
│   type: sqlite      │   运行时路径  │ data/checkpoints.db     │
│   connection_string:│              │ (主站 8082)              │
│   app_checkpoints.db│              ├─────────────────────────┤
└─────────────────────┘              │ guest_checkpoints.db     │
                                     │ (访客 8083)              │
                                     └─────────────────────────┘

LangGraph thread 状态：messages · tool 历史 · retrieved_evidence ...
MySQL conversations 只存元数据（title/user_id），不存消息正文
```

```yaml
checkpointer:
  type: sqlite
  connection_string: app_checkpoints.db
```

语义：

| 维度 | 说明 |
|------|------|
| 存什么 | LangGraph thread 状态：messages、图节点 checkpoint、tool 结果引用 |
| 不存什么 | 会话标题、user_id（在 MySQL `conversations`） |
| 为何 SQLite | 读写频繁、blob 结构化；部署简单；与 MySQL 备份周期解耦 |
| 访客隔离 | 8083 使用 `guest_checkpoints.db`，独立 conn |

Agent 图编译时传入 `app.state.checkpointer`（来自 lifespan），保证同进程内 REST API 与 Agent 共享同一 saver。

#### 在本项目中的实现

- YAML 段：`backend/config.yaml` L140-142
- HTTP 层实际初始化：`backend/api/http_server.py` → `data/checkpoints.db`
- 删除会话时清理 checkpoint：`backend/api/routers/conversations.py` 的 `delete_conversation`

#### 走一遍具体例子

用户删除对话 thread_id=`abc-123`：

1. API 将 MySQL `conversations.is_deleted = True`
2. 调用 `get_checkpointer_from_request(request)` 拿到 saver
3. 执行 SQL 删除 checkpoint 表中 `thread_id` 相关行
4. 清理沙箱目录 `get_paths().delete_thread_dir(...)`

若只删 MySQL 不删 SQLite，用户「新建同 ID 会话」可能看到幽灵历史。

#### 常见误区

- ❌ 以为 `connection_string: app_checkpoints.db` 就是线上路径——**lifespan 写死 checkpoints.db**
- ❌ 把 checkpoint 当 Redis 缓存——它是 **持久化**，重启后仍在
- ✅ 区分「配置语义」与「运行时挂载路径」

#### 延伸追问（L2/L3 only）

- 多 Uvicorn worker 共写同一 SQLite 文件的锁问题？
- 迁移到 PostgreSQL checkpointer 需要改哪些入口？

---

### Q7：项目里 Agent 框架的分层结构是怎样的？

**一句话结论：** 智能体框架采用四层分层架构：`domain/agents/`（协议层）→ `application/`（编排层）→ `core/agents/`（引擎层）→ `langchain.agents`（底层）；不再有 custom/deerflow 分离。

#### 为什么问这个 / 背景

框架已从早期的 custom + deerflow 分离架构演变为统一的分层架构。面试官要听清**当前架构的分层职责和协作方式**。

#### 详细解释

**四层 Agent 架构（ASCII 架构图）：**

```text
┌─────────────────────────────────────────┐
│  domain/agents/  (协议层)                  │
│  AgentProfile · AgentRequest              │
│  AgentRuntimeContext · AgentEvent         │
│  WorkflowSpec (YAML 驱动)                │
└────────────────────┬────────────────────┘
                     │ 约束接口
┌────────────────────▼────────────────────┐
│  application/  (编排层)                   │
│  AgentService · WorkflowRegistry          │
│  StandardAgentWorkflow · CoordinatedAgentWorkflow │
│  ProfiledAgentExecutor · LightweightQAAgent │
│  PromptBuilder · ToolAssemblyPipeline     │
└────────────────────┬────────────────────┘
                     │ 调用
┌────────────────────▼────────────────────┐
│  core/agents/  (引擎层)                   │
│  create_agent_graph() · RuntimeFeatures   │
│  ThreadState · Middleware 链            │
└────────────────────┬────────────────────┘
                     │ 封装
┌────────────────────▼────────────────────┐
│  langchain.agents.create_agent           │
│  LangGraph CompiledStateGraph            │
└─────────────────────────────────────────┘
```

| 层级 | 路径 | 职责 |
|------|------|------|
| 协议层 | `domain/agents/` | AgentProfile、AgentRequest、AgentRuntimeContext、AgentEvent、WorkflowSpec |
| 编排层 | `application/` | AgentService 入口、WorkflowRegistry、工作流实现、执行器、Prompt/Tool 装配 |
| 引擎层 | `core/agents/` | `create_agent_graph()`、RuntimeFeatures、ThreadState、Middleware |
| 底层 | `langchain.agents` | LangGraph 图状态机、`create_agent()` |

#### 在本项目中的实现

- 编排服务：`backend/application/agent_service.py` — `AgentService.run_stream()`
- 工作流注册：`backend/config/workflows/registry.yaml` + `backend/application/agent_workflows/registry.py`
- Agent Profile：`backend/config/agents/ne.yaml` / `csh.yaml` / `groundwater.yaml`
- Profile 加载：`backend/application/agents/profile_loader.py` — `AgentProfileLoader`
- 执行器：`backend/application/agent_executors/profiled_executor.py` — `ProfiledAgentExecutor`
- 图构建：`backend/application/agent_executors/graph_builder.py` — `ProfiledGraphBuilder`
- 建图工厂：`backend/core/agents/factory.py` — `create_agent_graph()`

#### 走一遍具体例子

POST `/api/v2/agent-runtime/query`，`agent_code=ne`：

1. `agent_runtime.py` 构造 `AgentRequest`
2. `AgentService.run_stream()` 加载 NE AgentProfile，构建 AgentRuntimeContext
3. WorkflowRegistry 根据请求和 Profile 解析工作流类型 → StandardAgentWorkflow
4. ProfiledAgentExecutor 构建图（ProfiledGraphBuilder + create_agent_graph）
5. 流式事件经 AgentEvent + SSEEncoder 推给前端

#### 常见误区

- ❌ 「我们用的是 DeerFlow Agent」——DeerFlow 已不再使用，统一为四层分层架构
- ❌ 认为每个域有独立的 Agent 类——现在统一用 `ProfiledAgentExecutor` + YAML Profile
- ✅ 说出四层职责分工 + YAML 驱动的 Profile 和工作流机制

#### 延伸追问（L2/L3 only）

- `RuntimeFeatures` 与 `middleware` 全量接管的互斥约束是什么？
- 如何将建图逻辑抽成测试友好的 pure function 并 mock LLM/tools？

---

### Q8：域（Domain）是如何划分的？

**一句话结论：** 至少 NE（核电子学）、CSH（污染场地修复）和 Groundwater（地下水）三个域，通过 YAML AgentProfile 配置隔离，共享同一套 AgentService 和工作流引擎。

#### 为什么问这个 / 背景

多域是「一套平台、多个业务」的典型模式。考察 Prompt/KB/Agent 是否隔离，以及 API 路由如何分流。

#### 详细解释

**多域分流（ASCII 架构图）：**

```text
┌──────────────────────────────────────────────────────┐
│         共享：AgentService · WorkflowRegistry         │
│         RAG · SSE · 模型工厂 · Middleware 链         │
└──────────────────────┬───────────────────────────────┘
                       │
        ┌─────────────┼─────────────────┬─────────────┐
        ▼             ▼                 ▼             ▼
  ┌───────────┐  ┌───────────┐  ┌───────────────┐
  │ NE 域       │  │ CSH 域     │  │ Groundwater 域 │
  ├───────────┤  ├───────────┤  ├───────────────┤
  │ ne.yaml     │  │ csh.yaml  │  │ groundwater.yaml │
  │ prompt: ne  │  │ prompt: csh│  │ prompt: gw      │
  │ 核电子学库  │  │ CSH 水文库│  │ 地下水项目库   │
  └───────────┘  └───────────┘  └───────────────┘

  Explore: /api/v2/explore/* — 独立 Prompt 服务
```

| 域 | 配置文件 | prompt_profile | 知识库 | 工具策略 |
|----|----------|----------------|--------|----------|
| NE | `config/agents/ne.yaml` | `ne` | 核电子学库 + 期刊论文库 | `ne_default` |
| CSH | `config/agents/csh.yaml` | `csh` | CSH 水文地质库 | `csh_default` |
| Groundwater | `config/agents/groundwater.yaml` | `gw` | 地下水项目库 | 按项目配置 |

域差异通过 YAML 配置驱动，而非独立 Agent 类：

| 差异维度 | 机制 |
|----------|------|
| Prompt | `PromptBuilder` 按 `prompt_profile` 加载不同模板（`prompt_templates/ne.py`、`csh.py`） |
| 知识库 | `default_kb_ids` 在 Profile YAML 中配置 |
| 工具集 | `tool_policy` 通过 `PolicyToolAssembler` 按域+工作流筛选工具 |
| Guardrails | `guardrail_policy` 通过 `RuntimePolicyGuardrailProvider` 按域限制工具 |
| UI | `ui_profile` 前端展示配置 |

#### 在本项目中的实现

- Profile 配置：`backend/config/agents/ne.yaml`、`csh.yaml`、`groundwater.yaml`
- Profile 加载：`backend/application/agents/profile_loader.py` — `AgentProfileLoader`
- Prompt 模板：`backend/application/agents/prompt_templates/ne.py`、`csh.py`、`groundwater.py`
- 探索模式：`backend/application/explore/service.py`
- 工具装配：`backend/application/tools/tool_assembly_pipeline.py`（按域+策略注入工具集）

#### 走一遍具体例子

CSH 用户问「某场地苯系物超标如何修复？」：

1. 前端 POST `/api/v2/agent-runtime/query`，`agent_code=csh`
2. AgentService 加载 `csh.yaml` Profile，解析 `prompt_profile=csh`、`tool_policy=csh_default`
3. PromptBuilder 用 `prompt_templates/csh.py` 生成 System Prompt
4. 若触发深度研究，CoordinatedAgentWorkflow 处理大纲、确认、并行检索、报告生成

#### 常见误区

- ❌ 认为每个域有独立的 Router 和 Agent 类——现在通过 YAML Profile + 统一 AgentService
- ❌ 认为改 NE Prompt 会影响 CSH——模板文件分离，但 **共享 core Middleware 链**
- ✅ 从 Profile YAML → PromptBuilder → ToolPolicy → KB 四件套说明隔离

#### 延伸追问（L2/L3 only）

- 新增第三业务域最少改哪些文件？（只需新增 YAML Profile + Prompt 模板）
- `multi_kb_search` 默认 KB 在访客与主站是否相同？

---

## L3 高级 🔴

### Q9：为什么采用「双 API + 双 checkpoint」而不是单服务加权限隔离？

**一句话结论：** 双服务用物理隔离换取更小攻击面、独立扩缩容和 checkpoint 不串扰；代价是代码重复、双倍预热和配置 drift 风险。

#### 为什么问这个 / 背景

这是架构权衡题，没有唯一正确答案。面试官看你是否能量化 pros/cons，并给出演进路径。

#### 详细解释

**双服务 vs 单服务（ASCII 架构图）：**

```text
Tier 1 — 最小可运行（纯 LLM，无 RAG）
  Python 3.11+ · MySQL · Redis · LLM OpenAI 兼容网关
        │
        ▼
Tier 2 — 标准生产（RAG 问答）
  + Milvus · MinIO/S3 · Redis LightRAG KV
        │
        ▼
Tier 3 — 完整功能
  + NebulaGraph · Kafka · run-worker · run-ingest-worker · 可选 8083 访客

可降级：
  Nebula 关 → Profile 仅向量+关键词
  Kafka 关  → 同步入库（慢）
  ingest 停 → 旧 KB 仍可检索，新文档无法入库
```



**Tier 1 — 最小可运行（纯 LLM 对话，无 RAG）：**

| 组件 | 用途 |
|------|------|
| Python 3.11+ | 运行时 |
| MySQL | 用户、会话元数据、API Key |
| Redis | Session Token |
| LLM OpenAI 兼容网关 | 模型推理 |

**Tier 2 — 标准生产（RAG 问答）：**

| 组件 | 用途 |
|------|------|
| Milvus | 向量检索 |
| MinIO 或 S3 兼容 | 文档对象存储 |
| Redis（扩展） | LightRAG KV |

**Tier 3 — 完整功能：**

| 组件 | 用途 |
|------|------|
| NebulaGraph | 知识图谱（Path-B/D 检索） |
| Kafka | 文档 OCR/抽取 |
| `run-worker` | 消费 Kafka 任务 |
| `run-ingest-worker` | DB 轮询入库 |
| 可选 8083 | 访客服务 |

**可降级策略：**

```
NebulaGraph 关闭 → 改 Retrieval Profile，仅向量+关键词
Kafka 关闭       → 同步入库（慢，不适合批量）
访客不部署       → 仅 8082
ingest-worker 停 → 新文档无法入 KB，旧 KB 仍可检索
```

#### 在本项目中的实现

- 连接串：`backend/config/settings.py`
- 初始化：`doc-agent init-db`
- 健康检查：`GET /health` on 8082
- 内部检索密钥：`INTERNAL_API_SECRET`（子智能体调 RAG 内部 API）

#### 走一遍具体例子

校内 GPU 服务器快速 demo：

1. Docker 起 MySQL + Redis + Milvus
2. `.env` 填网关 API Key
3. `doc-agent init-db && doc-agent run-api`
4. 手动导入一个小 KB（跳过 Kafka）→ 验证 `/api/v2/agent/query`

生产 CDUT 部署：

- 8082 内网 + 8083 公网反代
- Kafka 集群 + 2 个 worker 副本
- NebulaGraph 3 节点
-  nightly 备份 MySQL + `data/checkpoints.db`

#### 常见误区

- ❌ 只装 Milvus 不装 ingest-worker——**向量库空，检索无结果**
- ❌ Redis 可选——Session 认证强依赖 Redis，挂了全站 401（见 05 Q12）
- ❌ 忽略 `INTERNAL_API_SECRET`——子智能体内部检索接口会暴露

#### 延伸追问（L2/L3 only）

- 多 worker 部署时 SQLite checkpoint 如何换 PostgreSQL？
- TLS 证书加载：`TLS_CERT_SANDBOX_ENABLED` 解决什么 Linux 权限问题？

---

## L2 补充 🟡

### Q12：v1 API 主要路由分组有哪些？和 v2 如何分工？

**一句话结论：** v1（`api/routers/`）负责**管理面与能力 API**（auth、KB、文档、RAG 直连、模型、数据集）；agent-runtime（`api/routers/agent_runtime.py`）负责**智能体主路径**，通过 `agent_code` 区分域。

#### 详细解释

```text
┌────────────┐
│ 前端         │
└────────────┘

  前端 ──────────────► v2 /api/v2

  v2 /api/v2 ──────────────► 管理后台

  管理后台 ──────────────► v1 /api/v1

```

| 能力 | 推荐 API | 原因 |
|------|----------|------|
| 智能体 SSE 问答 | v2 agent-runtime | AgentService + 统一事件协议 |
| KB 文档上传入库 | v1 document_* | 完整 CRUD |
| 直连 RAG 调试 | v1 rag | 不经 LangGraph |
| 探索学习 | v2 explore | 专用服务层 |

#### 常见误区

- ❌ 新功能继续堆 v1 `rag.py` → ✅ 智能体能力走 agent-runtime
- ❌ v1/v2 共用同一 checkpoint 逻辑但路由不同 → 会话 API 以 agent-runtime conversations 为准

---

### Q13：MCP 服务在本项目中如何挂载？

**一句话结论：** 主 API（8082）通过 **FastMCP + SSE Transport** 暴露 MCP 端点，与 agent-runtime SSE **不同通道**，可共享底层 RAG/工具能力。

#### 在本项目中的实现

- 挂载：`backend/api/http_server.py` lifespan 或路由 include
- 用途：Cursor 等 MCP 客户端发现工具/资源
- 鉴权：需独立考虑，不应弱于 HTTP API

#### 延伸追问

- MCP tool 与 Agent `multi_kb_search` 是否复用同一 RAG 实例？

---

### Q14：`scripts/run_services.sh` 典型启动哪些进程？

**一句话结论：** 一键脚本通常编排 **API + Task Worker + Ingest Worker + 可选前端**，生产需配合 systemd/Docker 分别监控。

#### 走一遍具体例子

```bash
# 典型拆分（以项目 scripts 为准）
doc-agent run-api --port 8082 --workers 4
doc-agent run-worker --workers 1
doc-agent run-ingest-worker --workers 1
# 访客站单独：guest_http_server :8083
```

#### 常见误区

- ❌ 只启 API 不启 ingest-worker → KB 文件 pending 堆积

---

### Q15：`config.yaml` 中 memory / summarization / title 配置含义？

**一句话结论：** 三者均为 **可选子系统**：`memory` 长期事实记忆、`summarization` 长对话摘要、`title` 自动生成会话标题；当前项目多数为 **disabled**，但代码路径保留。

| 配置块 | 默认 | 作用 |
|--------|------|------|
| `memory.enabled` | false | JSON 文件存 user facts，JIT 注入 |
| `summarization.enabled` | false | 超长对话自动摘要替换 history |
| `title.enabled` | true | 首轮后 LLM 生成 ≤6 词标题 |

#### 延伸追问

- 开启 summarization 与 ContextPruning 同时启用时，history 谁主导裁剪？
