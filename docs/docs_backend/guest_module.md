# 独立访客免登录接口 (Guest API) 模块详解

> **最后同步**：2026-06-08  
> **现状说明**：访客能力已切到标准平台主干。`/api/v2/guest/query` 直接调用 `AgentService.run_stream()`，再在路由边缘局部映射为访客前端当前使用的事件格式；不再直接实例化旧 `NEFullFeatureAgent`。

在推广体验、移动端快捷分享与高校教学演练的场景下，强制用户进行账号注册与登录会极大地抬高使用门槛。

为此，系统特别设计了**独立访客通道**。系统提供两层访客能力：
1. **主 API 上的访客路由** (`/api/v2/guest/query`) — 供普通用户与访客混用 8082 进程。
2. **独立访客专用 API 服务** (`backend/api/guest_http_server.py`，默认监听 **8083** 端口) — 提供完全隔离的极简轻量级高并发服务。

```text
┌─ 主站 :8082 ─────────────────┐    ┌─ 访客 :8083 / guest :8085 ──┐
│ RadAI/CSH 登录用户            │    │ Guest 前端                   │
│      │                       │    │      │                       │
│      ▼                       │    │      ▼                       │
│ /api/v2/agent-runtime/query  │    │ /api/v2/guest/query          │
│      │                       │    │      │                       │
│      ▼                       │    │      ├── guest_checkpoints.db│
│ checkpoints.db               │    │      └── IndexedDB 主存历史  │
│ MySQL conversations          │    └──────────────┬────────────────┘
└──────────────┬───────────────┘                   │
               │                                   │
               └─────────────┬─────────────────────┘
                             ▼
                  lightweight workflow
                             │
                             ▼
                    双默认 KB 检索
```

---

## 🧭 双通道架构对比

| 特性维度 | 登录用户通道 (`/api/v2/agent-runtime/query`) | 访客专用服务 (`:8083` / `/api/v2/guest/query`) |
| :--- | :--- | :--- |
| **物理进程与端口** | 主 API 进程 (`:8082`) | 独立进程 (`:8083`) 或主 API 路由代理 |
| **身份认证机制** | Bearer Token / API Key 强制验证 | 免密访问（基于 HTTP 请求的 `X-Device-ID` 标头） |
| **对话历史持久化** | MySQL `Conversation` + checkpoint | 前端 **IndexedDB 为主**；8083 另提供 `guest_conversations` 列表（过滤 `guest_checkpoints.db` 有消息的 thread） |
| **上下文 Checkpointer** | SQLite 文件 `checkpoints.db`（全局） | SQLite 文件 `guest_checkpoints.db` (**物理完全隔离**) |
| **知识库可见范围** | 允许访问用户关联/授权的所有知识库 | 仅限预置的两个通用知识库 (Default NE 库 + Paper 库) |
| **智能体运行模式** | 普通 QA、多层中间件、子 Agent、深研 | 强行降级为 **lightweight workflow** |
| **思考流 SSE** | 支持 `<think>` 推理标签流式渲染 | 默认关闭深度思考，降低耗时与算力消耗 |

---

## 🏗️ 访客专用服务端口 (`:8083`) 生命周期与预热机制

访客专用服务 (`guest_http_server.py`) 拥有极简的依赖项，从根本上隔离了昂贵的对象入库、Kafka 消息消费、Nebula 复杂同步等繁重服务，确保其能在千人在线时保持亚秒级响应。

### 1. 异步 Lifespan 生命周期管理

服务启动时激活异步 Lifespan，在保障极端容灾的同时进行后台预热：

*   **非阻塞超时启动保护 (`_wait_with_timeout`)**:
    *   通过 `asyncio.shield` 封装 RAG 与索引加载任务。如果这些重载任务在 12 秒内没有初始化完毕，服务会强制完成 HTTP 端口的开启并把任务切换到后台静默运行，从而**防止任何外部索引源超时导致 API 服务崩溃无法开启**。

*   **RAG 实例与 DirectRetriever 并发预热**:
    *   访客模式仅有权限检索两个默认的知识库：
        1.  `DEFAULT_KB_ID`: 默认核电子学专业教科书库
        2.  `PAPER_KB_ID`: 学术期刊论文库
    *   Lifespan 启动后，自动并发拉起这两个库的 `_get_rag_instance(kb_id)` 进行内存常驻；随后调用 `DirectChunkSearcher` 和 `PerKBKeywordIndex` 执行关键词树与向量检索栈的并发预加热 (`asyncio.gather`)，避免首位访客提问时产生“冷启动”延迟。

*   **CORS 域名授信机制**:
    *   服务在中件层配置了精细的 CORS 网关限制，仅对授权的前端客户端（如 `guest/` 前端开发端口 `8085`、主服务映射端口 `8084` 及高校特定内网网段）开放跨域凭据传输 (`allow_credentials=True`)。

---

## 🏗️ 访客请求执行流与端点

```text
访客提问 (question, thread_id)
    │
    ▼ [backend/api/routers/guest_agent.py]
resolve_device_id(request) 提取 X-Device-ID（缺失则 anon_*）
    │
    ▼
internal_tid = g_{device_id}__t_{external_thread_id}
    │
    ▼
若 mode=deep-research / task_type=deep_research / subagent_enabled → 400
    │
    ▼
构造 AgentRequest(lightweight=True, thinking=False)
    │
    ▼
AgentService.run_stream()
    │
    ▼
WorkflowRegistry → StandardAgentWorkflow → ProfiledAgentExecutor
    │
    ▼
标准 AgentEvent → guest 路由局部映射 → SSE
```

### 1. 访客专用服务路由（`guest_http_server.py`）

8083 进程注册的路由模块（非全量主 API）：

| 模块 | 前缀 | 说明 |
| :--- | :--- | :--- |
| `auth` | `/api/v1/auth` | `POST /api/v1/auth/login`（可选登录，与主 API 同路由前缀） |
| `guest` | `/api/v2/guest` | **`POST /query`** 访客流式问答 |
| `agent-runtime` | `/api/v2/agent-runtime` | 登录态统一运行入口；访客主路径仍为 `/api/v2/guest/query` |
| `guest_conversations` + `conversations` | `/api/v2/conversations` | 会话列表与历史（guest 列表过滤 checkpoint） |
| `files` | `/api/v2` | 会话工件/图表读取 |
| — | `/health` | 健康检查 |

主 API `:8082` 亦挂载 `POST /api/v2/guest/query`，与 8083 逻辑相同，checkpoint 仍隔离。

---

## 💾 访客上下文的双轨存储策略

由于访客通道“不污染 MySQL”的核心要求，系统为其打造了巧妙的**“双轨存储机制”**：

### 1. 客户端 IndexedDB 主存 (会话树)
*   访客的前端应用（`:8085`）完全利用浏览器的 **IndexedDB** 数据库来维护 `guest_conversations` 表和 `guest_messages` 表。
*   会话的创建、重命名、删除、历史记录渲染等 100% 在访客本地磁盘中独立完成，极大削减了后端服务的 API 存储压力。

### 2. 服务端 SQLite Checkpointer 辅存 (对话上下文)
*   **物理隔离**: 8083 服务在 Lifespan 阶段通过异步驱动 `aiosqlite` 建立专属辅存库：
    `backend/data/guest_checkpoints.db`
    它与主服务的 `checkpoints.db` 完全物理隔离，确保两端数据彻底解耦。
*   **状态暂存**: 使用 **`AsyncSqliteSaver`** 实例进行状态存取。模型进行多轮流式对话时的 LangGraph Graph Checkpoints 会以此隔离库为媒介进行秒级快照。
*   **生命周期**: 访客退出或会话终止时，由前端调用 API 的会话删除端点，同步在 `guest_checkpoints.db` 中进行物理删除；多 Workers 部署下，此 SQLite 实例保持进程互斥只读。

---

## 🛡️ 访客安全防护与风控设计

1.  **特性限制**: `mode=deep-research`、`task_type=deep_research` 或 `subagent_enabled` 会直接返回 400；其他 mode 字段不会改变执行路径，访客请求始终被构造成 `lightweight` workflow。
2.  **检索限额**: 与主站相同走 `multi_kb_search` 默认 `top_k=10`、`chunk_top_k=6`（环境变量硬顶见 [知识库算法和架构.md](../知识库算法和架构.md) §8）。
3.  **工具预算**: 仍受 `request_limits.py` 和 workflow/tool policy 约束；访客因走 lightweight workflow，工具面更窄。

---

© 2026 核电子学智能助手项目组
