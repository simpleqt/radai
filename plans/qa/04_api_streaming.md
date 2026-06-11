# 04 · API 与流式（SSE）

> 📊 **图表：** 完整 ASCII 图见 [00_architecture_diagrams.md](./00_architecture_diagrams.md) 图 3、12、**21**；正式文档 → [sse_events_module.md](../../docs/docs_backend/sse_events_module.md)

---

## L1 初级 🟢

### Q1：v2 智能体问答 API 入口在哪里？

**一句话结论：** v2 智能体 API 集中在 `backend/api/routers/agent_runtime.py`，统一前缀 `/api/v2/agent-runtime/`，由 `http_server.py` 的 `create_app()` 注册。

#### 为什么问这个 / 背景

新人常把 v1 的 `rag.py` 和 v2 的 Agent 路由混为一谈。面试官要确认你知道**新功能该加在哪条路由链**。

#### 详细解释

**v2 路由挂载（ASCII 架构图）：**

```text
┌─────────────┐    include_router     ┌──────────────────────┐
│ http_server │ ────────────────────► │ api/routers/         │
│ create_app  │                       │ agent_runtime.py     │
│  (8082/8083)│                       │ prefix=/api/v2       │
└─────────────┘                       └──────────┬───────────┘
                                               │
              ┌────────────┬───────────┬───────┴────────┐
              ▼            ▼           ▼                ▼
        agent_runtime  guest.py    explore.py    其他管理路由
        /agent-runtime/*  访客路由    /explore/*
              │            │           │                │
              └────────────┴──── POST /query (SSE) ──────┘
```



v2 包结构：

```
backend/api/routers/
├── agent_runtime.py  → /agent-runtime/*  统一智能体问答
├── ...               → 其他管理路由
```

注册代码：

```python
# backend/api/http_server.py
app.include_router(agent_runtime_router, prefix="/api/v2")
```

核心端点（`agent_runtime.py`）：

| 方法 | 路径 | 作用 |
|------|------|------|
| POST | `/api/v2/agent-runtime/query` | **流式问答（SSE）** |
| GET | `/api/v2/agent-runtime/task/{task_id}/status` | 深度研究后台任务状态 |

#### 在本项目中的实现

- 问答实现：`backend/api/routers/agent_runtime.py`（统一编排，通过 AgentService 调度）
- 主站挂载：`backend/api/http_server.py` → `app.include_router(agent_runtime_router)`
- 认证：v2 agent 使用 `get_current_user`（Session Token），与 `verify_api_key` 路径略有不同

#### 走一遍具体例子

前端发起智能体问答：

```http
POST /api/v2/agent-runtime/query HTTP/1.1
Authorization: Bearer <session_token>
Content-Type: application/json

{
  "question": "什么是甄别器？",
  "agent_code": "ne",
  "thread_id": "550e8400-e29b-41d4-a716-446655440000"
}
```

1. `agent_runtime.py` 校验用户、确保 MySQL `conversations` 有记录
2. 构造 `AgentRuntimeQueryRequest`，调用 `AgentService.run_stream()`
3. 返回 `StreamingResponse(media_type="text/event-stream")`

#### 常见误区

- ❌ 以为 v2 在 `backend/custom/api/v2/`——旧路径已迁移
- ✅ 说出 `api/routers/agent_runtime.py` + AgentService 编排

#### 延伸追问（L2/L3 only）

- `explore.py` 与 `agent.py` 在 SSE 事件格式上是否一致？
- guest 路由在 8082 和 8083 是否都注册？

---

### Q2：流式响应使用什么协议？

**一句话结论：** 使用 **Server-Sent Events（SSE）**：HTTP 长连接 + `Content-Type: text/event-stream`，服务端通过 async generator 逐条推送 JSON 事件。

#### 为什么问这个 / 背景

SSE vs WebSocket 是经典对比题。本项目选 SSE 是因为**单向推送**足够，且与 FastAPI `StreamingResponse` 集成简单。

#### 详细解释

**SSE 推送链路（ASCII 架构图）：**

```text
┌─────────────────────┐
│ Agent aquery_stream │
└─────────────────────┘

  Agent aquery_stream ──────────────► SH

  SH ──────────────► SR

  SR ──────────────► 前端 EventSource / fetch stream

  前端 EventSource / fetch stream ──────────────► T

  T ──────────────► status

  status ──────────────► chunk

  chunk ──────────────► event/tool

  event/tool ──────────────► done

```

```
┌──────────────┐  AsyncIterator   ┌─────────────────────┐
│ LangGraph /  │ ───────────────► │ StreamHandler       │
│ Lightweight  │  dict events    │ thinking 剥离 + 协议  │
└──────────────┘                  └──────────┬──────────┘
                                           │
                                           ▼
                         StreamingResponse (text/event-stream)
                         data: {"type":"chunk|status|event|done",...}\n\n
                                           │
                                           ▼
                                    ┌─────────────┐
                                    │   前端/UI   │
                                    └─────────────┘
```

协议栈：

```
HTTP/1.1 长连接
  Content-Type: text/event-stream
  Cache-Control: no-cache
  Connection: keep-alive
  X-Accel-Buffering: no          ← 禁止 Nginx 缓冲

数据格式（本项目）:
  data: {"type":"chunk","content":"甄别器是..."}\n\n
  data: {"type":"status","content":"正在检索知识库..."}\n\n
  data: {"type":"done", ...}\n\n
```

Agent 内部流水线：

```
LangGraph graph.astream_events(version="v2")
        ↓
StreamHandler.handle_event() / iter_events()
        ↓
dict: { "type": "chunk"|"status"|"event"|"thinking"|... }
        ↓
agent.py stream_generator: json.dumps → yield f"data: ...\n\n"
        ↓
浏览器 EventSource / fetch ReadableStream 解析
```

```text
用户点击「停止」
        │
        ▼
 AbortController.abort() / EventSource.close()
        │
        ▼
 ASGI disconnect → stream_generator CancelledError
        │
        ├─ 普通 QA ──► LangGraph 通常随之停止
        │
        └─ 深度研究已提交后台 ──► TaskManager 继续跑
                                    └── 需 cancel task API 显式取消
```



`agent.py` 在流结束时的清理（连接取消场景）：

```python
# 简化逻辑：shield 避免 cleanup 被 cancel 打断
await asyncio.shield(conversation_task)
await asyncio.shield(stream_session.close())
```

两层语义：

| 场景 | HTTP 连接 | 后台 Task |
|------|-----------|-----------|
| 普通 QA 取消 | generator 停止 yield | 一般随之停止 |
| 深度研究已提交后台 | SSE 可能已结束 | **继续执行**，需 TaskManager cancel API |

#### 在本项目中的实现

- 流式 generator：`backend/api/routers/agent_runtime.py`
- 后台任务：`backend/application/tasks/runner.py` — `run_deep_research_background`
- 取消标志：`backend/application/tasks/manager.py` — `check_cancel_flag_from_db`
- ASGI 异常防护：`backend/api/http_server.py` 全局 handler 避免二次 `response.start`

#### 走一遍具体例子

用户发起深度研究，大纲确认后后台跑报告：

1. SSE 推送「研究已提交后台」后连接关闭
2. 用户刷新页面，通过 `GET /api/v2/agent/task/{task_id}/status` 拉进度
3. 若用户在进度页点「取消」，应写 DB cancel flag，runner 轮询后退出——**不是**简单 abort HTTP

普通问答中途 stop：

1. Abort fetch
2. 日志可能出现 `[Agent V2] 流式session在连接取消后结束关闭`
3. 下次同 thread_id 提问，checkpoint 保留已生成 partial 内容（取决于 Handler 是否 commit）

#### 常见误区

- ❌ 认为 abort 一定停 GPU/LLM——网关侧可能仍计费到当前 token 结束
- ❌ 深度研究 stop 只关浏览器 tab——**需显式 cancel task**
- ✅ 区分「连接级取消」与「任务级取消」

#### 延伸追问（L2/L3 only）

- `asyncio.shield` 在 cleanup 里为什么必要？
- TaskManager 如何跨 worker 读任务状态？

---

### Q4：v1 和 v2 API 的主要区别？

**一句话结论：** v1 是管理面 + 遗留 RAG QA；v2 是智能体主路径，统一 SSE 事件、AgentService、WorkflowRegistry——新功能应优先 v2。

#### 为什么问这个 / 背景

维护两套 API 是历史包袱。面试官看你是否知道**该往哪写代码**、迁移风险在哪。

#### 详细解释

| 维度 | v1 (`backend/api/routers/`) | v2 (`backend/api/routers/agent_runtime.py`) |
|------|----------------------------|-------------------------------|
| 定位 | KB/文档/用户/模型 CRUD、遗留 QA | 智能体（NE/CSH/Explore） |
| 前缀 | `/api/v1/*` | `/api/v2/*` |
| Agent | 遗留直调 LLM | AgentService + WorkflowRegistry + ProfiledAgentExecutor |
| SSE | 较分散，格式不统一 | StreamHandler + AgentEvent + SSEEncoder 统一 JSON schema |
| 能力 | 上传、入库、关键词管理 | 意图识别、深度研究、追问建议 |
| 认证 | 混合 Session / API Key | 主站 Session；管理仍 v1 |

架构关系：

```
v1 rag.py ──shared retrieval──→ retrieve_context
                              ↓（迁移中）
v2 agent_runtime.py ──→ AgentService ──→ ProfiledAgentExecutor
                              └──→ StreamHandler + SSEEncoder
```

```text
┌───────────────┐
│ KB/文档/用户 CRUD │
└───────────────┘

  KB/文档/用户 CRUD ──────────────► agent.py SSE

  agent.py SSE ──────────────► AgentService

  AgentService ──────────────► StreamHandler 统一 JSON

```

```
                    ┌─────────────────────────────────────┐
                    │ v1 /api/v1 — 管理面 + 遗留 QA        │
                    │ rag.py · KB CRUD · 用户/模型/上传    │
                    └──────────────────┬──────────────────┘
                                       │ 迁移中
                    ┌──────────────────▼──────────────────┐
                    │ v2 /api/v2 — 智能体主路径            │
                    │ agent_runtime.py → AgentService → StreamHandler │
                    │ 统一 SSE · 深度研究 · 追问建议        │
                    └─────────────────────────────────────┘
```

#### 在本项目中的实现

- v1 RAG：`backend/api/routers/rag.py`
- v1 会话：`backend/api/routers/conversations.py`（v2 也复用会话概念）
- v2 主入口：`backend/api/routers/agent_runtime.py`
- 遗留单库 Agent 链路已下线

#### 走一遍具体例子

产品需求「增加追问建议」：

- ✅ 在 v2 `agent.py` 集成 `follow_up_suggestions.py`，SSE 末尾带 metadata
- ❌ 在 v1 `rag.py` 加 hack——前端若已切 v2，会永远看不到

文档入库仍走 v1：

- `POST /api/v1/knowledge-base/...` → Kafka → worker → ingest-worker

#### 常见误区

- ❌ 认为 v1 已废弃——**管理面几乎全在 v1**
- ❌ v2 不需要 v1——会话列表、文件上传仍依赖 v1 路由
- ✅ 「问答走 v2，管理走 v1，逐步迁移 legacy QA」

#### 延伸追问（L2/L3 only）

- v1 `/rag/query` 与 v2 `/agent/query` 检索链路差异？
- 何时该 deprecate v1 QA？

---

## L2 中级 🟡

### Q5：`StreamHandler` 负责什么？

**一句话结论：** StreamHandler 是 LangGraph 原始事件与前端 SSE JSON 之间的**翻译层**：映射事件类型、剥离 thinking、解析图片 token、格式化工具事件，并监控「低可见输出」异常。事件通过 `AgentEvent` 标准化后由 `SSEEncoder` 编码。

#### 为什么问这个 / 背景

~987 行的 Handler 是流式体验的核心。面试官想听你是否理解「为什么不能直接 dump LangGraph JSON」。

#### 详细解释

Handler 职责矩阵：

| 输入（LangGraph） | 输出（SSE dict） | 处理逻辑 |
|-------------------|------------------|----------|
| `on_chat_model_stream` | `chunk` / `status` | token 合并、图片占位符 → URL |
| `on_chat_model_end` | 补发 missing tail | 防止流式丢末尾 |
| `on_tool_start/end` | `event` (tool_*) | 参数摘要、耗时 |
| reasoning_content | `thinking`（或剥离） | 与模型配置联动 |
| task 子智能体 runtime | `event` (task_*) | 深度研究进度 |
| context pruning | `status` / `event` | 「正在压缩历史上下文…」 |

```text
┌──────────────────────┐
│ on_chat_model_stream │
└──────────────────────┘

  on_chat_model_stream ──────────────► Thinking 状态机

  Thinking 状态机 ──────────────► I2

  I2 ──────────────► Tool 摘要化

  Tool 摘要化 ──────────────► I3

  I3 ──────────────► TokenResolver 图片

  TokenResolver 图片 ──────────────► type: chunk

```

```
LangGraph astream_events()
        │
        ├─ on_chat_model_stream ──► Thinking 状态机 ──► type: chunk
        ├─ on_tool_start/end ─────► Tool 摘要化 ─────► type: event
        ├─ reasoning_content ─────► 剥离/转发 ─────► type: thinking
        └─ task 子 Agent ─────────► 进度格式化 ────► type: event (process)
        │
        ▼
┌─────────────────────┐
│ StreamHandler         │  TokenResolver 图片占位符 → URL
└──────────┬──────────┘
           ▼
   SSE dict → StreamingResponse → 前端
```

类初始化关键状态：

```python
# application/agent_executors/stream_handler.py
class StreamHandler:
    """Translate LangGraph events into frontend SSE payloads."""

    def __init__(
        self,
        *,
        is_deep_research: bool = False,
        thread_id: str = "",
        question: str = "",
        max_output_chars: int = 0,
        selected_skills: set[str] | None = None,
    ):
        ...
        self._token_resolver = StreamingTokenResolver()
        self._model_round_open = False
        ...
```

特殊机制：

1. **Thinking 标签状态机** — carry buffer 处理跨 chunk 不完整 `</think>`
2. **StreamingTokenResolver** — 模型输出 `![img:xxx]` → 注册表 URL（`image_url_registry.py`）
3. **低可见输出告警** — 工具执行后正文 `< 300` 字符打 WARNING
4. **深度研究** — live markdown artifact id、grace period、task runtime 跟踪

#### 在本项目中的实现

- Handler：`backend/application/agent_executors/stream_handler.py`
- 图片注册表：`backend/application/agent_executors/image_url_registry.py`
- 被调用方：`profiled_executor.py`、`lightweight_qa_agent.py`
- 单元逻辑辅助：`stream_utils.py`

#### 走一遍具体例子

模型输出跨两个 chunk 泄露思考标签：

```
chunk1: "答案是...<redacted_thi"
chunk2: "nking>内部推理最终结论"
```

Handler carry buffer 识别不完整前缀，在 tag 闭合前不 forward 给前端；闭合后只 emit「最终结论」到 `chunk`。

工具 `multi_kb_search` 返回 8000 字但模型只生成「根据检索结果如下：」20 字：

- Handler 会话结束触发 `_LOW_VISIBLE_OUTPUT_WARN_CHARS` 日志，便于排查 prompt 或模型偷懒

#### 常见误区

- ❌ 认为过滤 thinking 只在 HTTP 层——**Handler 是第一道**，HTTP/MCP 还有防御层
- ❌ Handler 负责路由/超时——这些留在 Agent 类（已拆分目标）
- ✅ 翻译 + 安全 + UX 三合一

#### 延伸追问（L2/L3 only）

- `has_incomplete_model_round()` 如何驱动续跑？
- 如何单测 Handler 而不启动 HTTP？

---

### Q6：流式超时如何配置？

**一句话结论：** 超时集中在 `stream_timeouts.py`，通过环境变量控制「事件间空闲」「模型轮次空闲」「心跳间隔」和「未完成轮次续跑次数」。

#### 为什么问这个 / 背景

流式 hang 住是线上常见问题。要能说出**哪一层超时**、日志关键字是什么。

#### 详细解释

配置项（均可环境变量覆盖）：

```python
# application/agent_executors/stream_timeouts.py
# 常规模式：两次 LangGraph 事件之间的最长静默时间（秒）
STREAM_IDLE_TIMEOUT_SECONDS = _int_env("STREAM_IDLE_TIMEOUT_SECONDS", 180)

# 模型轮次已 start 尚未 end 时，允许更长的 token/推理间隔（秒）
MODEL_ROUND_IDLE_TIMEOUT_SECONDS = _int_env("MODEL_ROUND_IDLE_TIMEOUT_SECONDS", 300)

# wait_for 轮询间隔；仅影响心跳 ping 频率，不单独触发终止
STREAM_HEARTBEAT_INTERVAL_SECONDS = _int_env("STREAM_HEARTBEAT_INTERVAL_SECONDS", 30)

# LangGraph 流结束但模型轮次未完成时，从 checkpoint 续跑的最大次数
INCOMPLETE_ROUND_STREAM_RETRY_MAX = _int_env("INCOMPLETE_ROUND_STREAM_RETRY_MAX", 2)
```

决策逻辑：

```
                    ┌─────────────────────────┐
                    │ 等待下一 LangGraph 事件   │
                    └───────────┬─────────────┘
                                │
              handler.has_incomplete_model_round()?
                     /                    \
                   是                      否
                    │                      │
     MODEL_ROUND_IDLE (300s)    STREAM_IDLE (180s)
                    │                      │
              超时 → 推送 error/done        │
              或 INCOMPLETE_ROUND 续跑 ──────┘
                    (最多 2 次)
```

```text
POST /api/v2/agent-runtime/query (task_type=deep_research)
        │
        ▼
   ┌─────────┐
   │  draft  │  CoordinatedAgentWorkflow 流式大纲
   └────┬────┘
        │ 用户 confirm / revise
        ▼
┌───────────────────┐
│ awaiting_confirm  │
└─────────┬─────────┘
          │ confirmed
          ▼
     ┌─────────┐
     │ running │  CoordinatedAgentWorkflow 子 Agent 并行
     └────┬────┘  TaskManager + CoordinatedAgentWorkflow
          │
    ┌─────┴─────┐
    ▼           ▼
completed   cancelled
    │           │
    └─────┬─────┘
          ▼
   SSE process / artifact + MySQL tasks 表
```



`agent_runtime.py` 关键分支（简化）：

1. `deep_research_mode = (task_type == "deep_research")`
2. 未确认 → CoordinatedAgentWorkflow 流式大纲，workflow state = `awaiting_confirmation`
3. `confirmation_status == "confirmed"` → CoordinatedAgentWorkflow 启动子 Agent
4. 运行时 `runtime_deep_research_mode` 控制 subagent、mode、工具集

持久化：

| 存储 | 内容 |
|------|------|
| SQLite checkpoint | 消息、图状态 |
| MySQL `conversations.metadata` | workflow state JSON |
| TaskManager / DB | task_id、cancel flag、recent_events |

#### 在本项目中的实现

- API 编排：`backend/api/routers/agent_runtime.py`
- 工作流状态：`backend/application/workflow/research_state.py`
- 大纲阶段：`backend/application/agent_workflows/` — CoordinatedAgentWorkflow
- 后台 runner：`backend/application/tasks/runner.py`
- 工作流配置：`backend/config/workflows/registry.yaml`

#### 走一遍具体例子

用户：「请深度调研核电厂数字化仪控趋势」

1. **第一轮 POST**（无 confirm）→ SSE 流式输出研究大纲 + `process` 事件「等待确认」
2. 前端展示 Confirm 按钮，用户点击 confirm 带 `confirmation_status: "confirmed"`
3. **第二轮 POST** → 提交 `run_deep_research_background`，返回 task_id
4. 前端轮询 `GET /api/v2/agent/task/{task_id}/status` 或使用 SSE process 事件
5. 完成时收到 `save_deep_research_report` 工具触发的 artifact 事件

#### 常见误区

- ❌ 以为一次 SSE 连接跑完全程——**确认后常转后台 Task**
- ❌ 忽略 CSH 域独立 orchestrator
- ✅ 状态机 + 三存储 + 前后端两轮交互

#### 延伸追问（L2/L3 only）

- `RESEARCH_WORKFLOW_TERMINAL_STATES` 包含哪些？
- 刷新页面如何用 task_id 恢复进度？

---

### Q8：HTTP Middleware 如何注入用户上下文？

**一句话结论：** `user_context_middleware` 在请求入口解析 Bearer / X-API-Key，优先 Redis Session 快路径，将 `user_id`、`username`、`request_id` 写入 ContextVar，供日志过滤器自动附加。

#### 为什么问这个 / 背景

可观测性是大规模排障基础。ContextVar 让**深层 Agent 代码不用传 request 对象**也能打出用户维度日志。

#### 详细解释

Middleware 流程：

```
Request 进入
  → request_id = secrets.token_hex(8)
  → set_user_context(request_id=...)
  → 解析 Authorization Bearer / ?token= / X-API-Key
  → Bearer: Redis auth:token:{token}  → user_id, username
  → miss: _resolve_api_key_auth(bearer)
  → X-API-Key: DB 查 api_keys
  → set_user_context(user_id, username)
  → call_next(request)
  → finally: clear_user_context()
```

```text
┌───────────────────────┐ ┌───────────────────────┐ ┌───────────────────────┐ ┌───────────────────────┐ ┌───────────────────────┐
│     HTTP Request      │ │user_context_middleware│ │         Redis         │ │      ContextVar       │ │         业务路由          │
└───────────────────────┘ └───────────────────────┘ └───────────────────────┘ └───────────────────────┘ └───────────────────────┘


```

```
Request 进入
        │
        ▼
 request_id = token_hex(8) → set_user_context(request_id)
        │
        ├─ Bearer / ?token= → Redis auth:token:{token} → user_id
        ├─ miss → _resolve_api_key_auth
        └─ X-API-Key → MySQL api_keys
        │
        ▼
 set_user_context(user_id, username) → call_next → finally clear

Middleware：只写日志上下文，不阻断未认证请求
Depends verify_api_key / get_current_user：路由级 401/403
```

与 Depends 认证的分工：

| 机制 | 作用 | 是否阻断未认证请求 |
|------|------|-------------------|
| Middleware | 日志上下文 | **否**，未登录静默跳过 |
| `verify_api_key` / `get_current_user` | 路由级鉴权 | **是**，401/403 |

ContextVar 定义：

```12:29:backend/config/filters.py
# 每个请求的用户上下文，通过中间件注入
_user_id_ctx: ContextVar[Optional[str]] = ContextVar("user_id", default=None)
_username_ctx: ContextVar[Optional[str]] = ContextVar("username", default=None)
_request_id_ctx: ContextVar[Optional[str]] = ContextVar("request_id", default=None)

def set_user_context(
    user_id: Optional[str] = None,
    username: Optional[str] = None,
    request_id: Optional[str] = None,
) -> None:
    """设置当前请求的用户上下文（在中间件中调用）"""
```

#### 在本项目中的实现

- Middleware：`backend/api/http_server.py` L479-549
- 日志 Filter 读取：`backend/config/filters.py`
- 路由级认证：`backend/api/dependencies/auth.py`、`backend/api/routers/auth.py`

#### 走一遍具体例子

某用户 SSE 连接带 `?token=xxx`（WebSocket/SSE 无法自定义 Header 时的兼容）：

1. Middleware 从 query 读 token
2. Redis 命中，日志：`[user_id=u123] [request_id=a1b2c3d4] RAG 检索...`
3. 排障时在 Kibana/文件搜 `request_id` 串起全链路

未带认证的 `/health`：

- Middleware 只设 `request_id`，不设 user_id，不报错

#### 常见误区

- ❌ 以为 Middleware 等于登录校验——**仅日志**；真正 401 在 Depends
- ❌ 忽略 `?token=` 查询参数兼容
- ✅ ContextVar + 快路径 Redis + 慢路径 API Key

#### 延伸追问（L2/L3 only）

- ContextVar 在 async generator 流式响应中是否传播？
- guest 服务是否复用同一 middleware？

---

### Q9：MCP SSE 在本项目中是什么角色？

**一句话结论：** MCP SSE 是 **Model Context Protocol** 传输层，让 Cursor 等外部客户端调用 `query_knowledge_base` 工具；与 v2 前端问答 SSE **不同协议、不同路由**，可共享 RAG 能力。

#### 为什么问这个 / 背景

项目里有两套「SSE」，名字相同用途不同。混淆是面试减分项。

#### 详细解释

**两套 SSE 对比（ASCII 架构图）：**

```text
┌────────────┐
│ Web 前端     │
└────────────┘

  Web 前端 ──────────────► AQ

  AQ ──────────────► SH

  SH ──────────────► {type: chunk|status|...}

  {type: chunk|status|...} ──────────────► Cursor/Claude

  Cursor/Claude ──────────────► MCP

  MCP ──────────────► RPC

  RPC ──────────────► query_knowledge_base

  query_knowledge_base ──────────────► (RAG Service)

```

```
┌─────────────────────┐              ┌─────────────────────┐
│ v2 Agent SSE        │              │ MCP SSE             │
├─────────────────────┤              ├─────────────────────┤
│ /api/v2/agent/query │              │ /mcp/ Starlette     │
│ 自研前端            │              │ Cursor 等外部客户端  │
│ JSON type/content   │              │ JSON-RPC over SSE   │
│ Session / API Key   │              │ MCP 路由配置        │
└──────────┬──────────┘              └──────────┬──────────┘
           │                                      │
           └──────────────┬───────────────────────┘
                          ▼
                 共享 RAG / Milvus（不共享 browser thread）
```

对比：

| 维度 | v2 Agent SSE | MCP SSE |
|------|-------------|---------|
| 路径 | `/api/v2/agent/query` | MCP Starlette mount（如 `/mcp/`） |
| 客户端 | 自研 Web 前端 | Cursor、Claude Desktop 等 |
|  payload | JSON `{type, content}` | MCP JSON-RPC over SSE |
| 认证 | Session / API Key | 依 MCP 路由配置 |
| 实现 | FastAPI StreamingResponse | FastMCP + `SseServerTransport` |

MCP 创建：

```665:693:backend/api/http_server.py
def create_mcp_sse_app():
    """创建MCP SSE应用"""
    mcp_server = mcp_app._mcp_server
    sse = SseServerTransport("/messages/")
    ...
    return Starlette(
        debug=True,
        routes=[
            Route("/", endpoint=handle_sse),
            Mount("/messages/", app=sse.handle_post_message),
        ],
    )
```

MCP 工具 `query_knowledge_base` 内部仍走 Agent 流，且对 think 标签做 `_strip_thinking_segments` 过滤。

#### 在本项目中的实现

- FastMCP 定义：`backend/api/http_server.py` — `mcp_app`
- MCP 路由：`backend/api/routers/mcp.py`
- RAG 能力复用：`get_rag_service()`、核电子学 Agent 流

#### 走一遍具体例子

开发者在 Cursor 配置 MCP 指向本校 API：

1. Cursor 建立 MCP SSE 连接
2. 调用 `query_knowledge_base("PIN  diode")`
3. 服务端跑 RAG + 简化 Agent 流，返回纯文本答案（非 v2 chunk 格式）
4. 与浏览器用户同时访问，共享 Milvus 索引，**不共享** browser session thread

#### 常见误区

- ❌ 用 EventSource 连 MCP 路径期望 `{type:chunk}`——**协议不兼容**
- ❌ MCP 走 v2 认证中间件——需确认 mount 点与 auth 配置
- ✅ 两套 SSE 并列，底层 RAG 可共享

#### 延伸追问（L2/L3 only）

- MCP 与 internal `/retrieve_internal` 的认证差异？
- 为何 MCP 答案要 strip thinking 而不仅靠模型配置？

---

### Q10：流式响应中 reasoning/thinking 如何处理？

**一句话结论：** 三层防御——模型配置尽量关闭 thinking、StreamHandler 状态机剥离标签、HTTP/MCP/runner 路径额外 `_strip_thinking_segments`；主站与 guest 仍未 100% 行为对齐（架构待办 P0-3）。

#### 为什么问这个 / 背景

推理模型（MiniMax、Qwen thinking 版）即使用 API 参数关闭，网关仍可能推送 reasoning_content。必须**假设模型层不可靠**。

#### 详细解释

**Thinking 三层防御（ASCII 架构图）：**

```text
┌──────────────────────────────────────┐
│ L1                                   │
└──────────────────────────────────────┘
           │
           ▼
┌──────────────────────────────────────┐
│ L2                                   │
└──────────────────────────────────────┘
           │
           ▼
┌──────────────────────────────────────┐
│ L3                                   │
└──────────────────────────────────────┘
           │
           ▼
┌──────────────────────────────────────┐
│ 用户可见正文 chunk                         │
└──────────────────────────────────────┘

```

防御层：

```
Layer 1 模型配置 (config.yaml)
  extra_body.thinking.type: disabled · enable_thinking: false
        ↓ 仍可能泄漏
Layer 2 StreamHandler
  剥离 thinking 标签 / reasoning_content → type:thinking 或丢弃
        ↓ 仍可能泄漏
Layer 3 HTTP/MCP/runner
  _strip_thinking_segments + carry buffer（跨 chunk 拼接）
```

`config.yaml` 示例（MiniMax）：

```yaml
extra_body:
  thinking:
    type: disabled
reasoning_effort: none
supports_thinking: false
```

Handler 仍可能 yield：

```python
yield {"type": "thinking", "content": reasoning_text}
```

前端应默认隐藏；主站 `agent.py` 亦复制了一套 strip 逻辑（与 `http_server.py` MCP 路径一致）。

guest/runner：**已统一过滤**（`application/tasks/runner.py` 中 `_split_thinking_segments`）。

#### 在本项目中的实现

- 模型 thinking 开关：`backend/config.yaml` models 段
- Handler：`backend/application/agent_executors/stream_handler.py`
- 主站 API strip：`backend/api/routers/agent_runtime.py` 顶部 regex
- MCP strip：`backend/api/http_server.py` L70-118
- Runner strip：`backend/application/tasks/runner.py`

#### 走一遍具体例子

MiniMax 网关 bug 返回 `<thought>链式思考</thought>可见答案`：

1. Handler 状态机：`in_thinking=True` 期间不 forward
2. 闭合标签后只 emit「可见答案」
3. 若 Handler 漏网，HTTP 层 strip 再滤一道
4. 用户界面始终只见答案（理想情况）

#### 常见误区

- ❌ 「config 关了 thinking 就不用 Handler」——**必须防御性编程**
- ❌ 把 `type:thinking` SSE 展示给用户——产品/安全决策
- ✅ 三层过滤 + 承认 guest/主站未完全对齐

#### 延伸追问（L2/L3 only）

- carry buffer 如何防 `<redacted_thi` + `nking>` 跨 chunk？
- 为何 MiniMax 注释说「无法完全关闭思考」？

---

## L3 高级 🔴

### Q11：为什么 LangGraph 事件要先转 Handler 再 SSE，而不是直接 dump JSON？

**一句话结论：** 直接 dump 会把 LangGraph 内部事件、版本差异、敏感 tool 参数和 thinking 暴露给前端；Handler 提供**稳定协议、安全过滤、UX 聚合和可单测性**。

#### 为什么问这个 / 背景

这是架构取舍题。~987 行 Handler 是「贵但值得」还是「该拆」——考察系统思维。

#### 详细解释

**Handler 边界价值（ASCII 架构图）：**

```text
┌──────────────────────────┐
│ LangGraph astream_events │
└──────────────────────────┘

  LangGraph astream_events ──────────────► H

  H ──────────────► P

  P ──────────────► 安全: thinking/tool 摘要

  安全: thinking/tool 摘要 ──────────────► UX: chunk 合并/status

  UX: chunk 合并/status ──────────────► 前端 agent-v2.ts

```

若直接 `astream_events → json.dumps` 的问题：

| 问题 | 后果 |
|------|------|
| 事件 schema 随 LangGraph 版本变 | 前端频繁 break |
| 泄露 `checkpoint`、内部 state | 安全风险 |
| micro-chunk 每 token 一条 | 前端渲染卡顿 |
| tool args 含 API key 片段 | 敏感信息暴露 |
| reasoning 字段直出 | 违反产品策略 |

Handler 提供的价值：

1. **协议稳定** — 前端只认 `{type, content, kind, metadata}`
2. **安全** — thinking 剥离、tool 摘要化（`summarize_tool_output`）
3. **UX** — 合并 chunk、注入 `status`、artifact 元数据、深度研究 live markdown
4. **可测试** — 纯函数 + async iterator，无需 ASGI
5. **运维** — 低可见输出 WARN、model round 统计

代价：

- 单文件过大，架构计划拆「可插拔事件处理器链」
- 与 HTTP 层 strip 逻辑部分重复（P0-3）

#### 在本项目中的实现

- Handler：`backend/application/agent_executors/stream_handler.py`
- Agent 调用模式：`async for event in handler.iter_events(astream_events): yield sse_line(event)`
- 规划参考：架构 redesign 文档中的 Event Processor Chain

#### 走一遍具体例子

LangGraph 升级 `on_tool_end` payload 字段 rename：

- 无 Handler：前端 20 处解析全挂
- 有 Handler：只改 `handle_tool_end()` 映射，前端无感

#### 常见误区

- ❌ Handler 只是「格式转换」——还有安全、UX、观测
- ❌ 用 OpenAPI 描述 SSE 事件就够了——**运行时是流式 dict，非静态 schema**
- ✅ 稳定边界 + 测试策略

#### 延伸追问（L2/L3 only）

- 如何设计 handler 链：`ThinkingFilter → ChunkMerge → ToolFormatter`？
- Handler 与 OpenTelemetry span 如何关联？

---

### Q12：SSE 长连接在 Uvicorn 多 worker 下有什么坑？

**一句话结论：** SSE 连接本身 sticky 到单 worker 没问题；真正坑在 **SQLite checkpoint 多写**、**每 worker 独立 RAG 预热内存**、以及**客户端断连后后台 Task 仍运行**。

#### 为什么问这个 / 背景

生产常见 `--workers 4`。要能说明哪些资源是 per-worker、哪些必须外置。

#### 详细解释

```
                    ┌──────── Worker 1 ──── SSE conn A
Load Balancer ──────┼──────── Worker 2 ──── SSE conn B
                    └──────── Worker 3 ──── checkpoints.db 写竞争
                              ↑
                         同一 SQLite 文件
```

| 坑点 | 说明 | 缓解 |
|------|------|------|
| SSE 连接 | 单连接绑定单 worker，LB 无需 sticky | 正常 |
| SQLite checkpoint | 多 worker 写锁串行，延迟↑ | PostgreSQL checkpointer 或 workers=1 |
| RAG 预热 | 每 worker 各加载 KB 索引 | 接受内存×N 或 sidecar 检索服务 |
| Agent 图实例 | 进程内内存，不共享 | 无状态 API 设计 |
| 断连 vs 后台 Task | 深度研究继续跑 | TaskManager + cancel flag |
| Nginx 缓冲 | 默认缓冲 SSE | `X-Accel-Buffering: no` |

CLI 默认 `--workers 4`；访客服务默认 `GUEST_SERVER_WORKERS=1`  partly 为避免 checkpoint 争用。

#### 在本项目中的实现

- Workers 配置：`backend/cli.py`
- Checkpoint：`http_server.py` `_init_checkpointer`
- 任务跨进程：`backend/application/tasks/manager.py` — DB 持久化 task 状态

#### 走一遍具体例子

滚动发布 4 worker API，用户长问答 10 分钟：

1. 连接始终在 worker-2，无 mid-stream 切换
2. checkpoint 写入 worker-2 持有的 SQLite 连接；worker-1 同时服务其他用户写同一文件 → 偶发 `database is locked`
3. 监控 SQLite busy timeout；高并发应迁移 PG

#### 常见误区

- ❌ 「SSE 要多 worker sticky」——**同连接不会跨 worker**；问题是共享存储
- ❌ 水平扩展 API 就能线性扩展 LLM 并发——瓶颈可能在网关 quota
- ✅ 分连接层、状态层、计算层说坑

#### 延伸追问（L2/L3 only）

- LangGraph PostgreSQL checkpointer 迁移步骤？
- 为何 guest 默认 1 worker？

---

### Q13：如何避免 StreamingResponse 异常导致 ASGI 协议错误？

**一句话结论：** 一旦 SSE 发送了 `response.start`，异常不能再构造新的 HTTP 响应；项目用 try/except 包裹 yield、末尾发 error 事件，并让全局 handler 识别「二次 response.start」直接 re-raise。

#### 为什么问这个 / 背景

Starlette/FastAPI 经典坑：`Expected ASGI message 'http.response.body' but got 'http.response.start'`。体现对 ASGI 生命周期的理解。

#### 详细解释

**ASGI SSE 异常（ASCII 架构图）：**

```text
┌──────────────────┐
│ Start            │
└──────────────────┘
    ▲  http.response.start
    │
    ▼

  (yield chunk)
    │
    ▼
┌──────────────────┐
│ Body             │
└──────────────────┘

  (yield chunk)
    │
    ▼
┌──────────────────┐
│ Body             │
└──────────────────┘

  (generator raise)
    │
    ▼
┌──────────────────┐
│ Error            │
└──────────────────┘

  (全局 handler 再 start)
    │
    ▼
┌──────────────────┐
│ Bad              │
└──────────────────┘

  (ASGI 协议错误)
    │
    ▼
┌──────────────────┐
│ RuntimeError     │
└──────────────────┘

  (正常结束)
    │
    ▼
┌──────────────────┐
│ OK               │
└──────────────────┘


```

ASGI HTTP 响应阶段：

```
http.response.start  (headers)  ← 只能一次
http.response.body   (chunk 1)
http.response.body   (chunk 2)
...
http.response.body   (empty chunk, 结束)
```

错误模式：

```
response.start 已发送
  → generator 内 raise Exception
  → FastAPI 全局 handler 试图 JSONResponse  ← 第二次 start → RuntimeError
```

项目对策：

1. **generator 内捕获** — yield 最后一条 `{"type":"error",...}` 而非 raise
2. **全局 handler 识别** — 特定 RuntimeError 直接上抛，不再 JSONResponse

```552:560:backend/api/http_server.py
@app.exception_handler(Exception)
async def global_exception_handler(request: Request, exc: Exception):
    ...
    if isinstance(exc, RuntimeError) and "Expected ASGI message 'http.response.body'" in str(exc):
        logger.warning("检测到已进入 response.body 阶段的协议错误，跳过全局 JSON 异常响应: %s", exc)
        raise exc
```

3. **连接取消** — `agent.py` finally 用 shield 关闭 session，避免 cleanup 异常冒泡

#### 在本项目中的实现

- 全局 handler：`backend/api/http_server.py`
- 流 generator：`backend/api/routers/agent_runtime.py` — try/except/finally
- 日志：`app.error.log`、logger exception

#### 走一遍具体例子

LLM 流中途 Milvus 超时异常：

- ✅ generator catch → `yield data: {"type":"error","message":"检索超时"}\n\n` → 正常结束 body
- ❌ 直接 raise → 全局 handler 500 JSON → ASGI 协议错误 → 客户端看到 hung 连接

#### 常见误区

- ❌ 任何异常都交给 `@app.exception_handler`——**流式已 start 时不行**
- ❌ 忽略客户端 abort 引发的 CancelledError——需区分记录级别
- ✅ ASGI 两阶段 + error 事件兜底

#### 延伸追问（L2/L3 only）

- middleware 里抛异常 vs generator 里抛有何不同？
- 如何用 integration test 复现 double start？

---

### Q14：guest API 与主站 API 的 SSE 事件是否完全一致？

**一句话结论：** **协议格式大体一致**（复用同类 Agent/Handler 逻辑），但在路由集、checkpoint 文件、think 过滤完整度、认证方式上存在差异——集成测试必须分别覆盖 8082 与 8083。

#### 为什么问这个 / 背景

前端常假设「guest 就是阉割版主站」。实际上隔离点不止权限，还有**状态存储与过滤行为**。

#### 详细解释

| 维度 | 主站 8082 | 访客 8083 |
|------|-----------|-----------|
| 路由 | 完整 v2 + v1 管理 | login + query + conversations |
| Checkpoint | `data/checkpoints.db` | `data/guest_checkpoints.db` |
| RAG 预热 | 全部 active KB | 仅 DEFAULT_KB + PAPER_KB |
| Think 过滤 | Handler + API strip（仍有 P0-3 待办） | runner/API 已强调统一过滤 |
| 认证 | Session + API Key + CAS | 简化登录 |
| Workers 默认 | 4 | 1 |

访客路由清单（源码注释）：

```
POST /api/v1/auth/login
GET  /api/v2/agent/info
POST /api/v2/agent/query
GET  /api/v2/conversations
GET  /api/v2/conversations/{thread_id}/messages
```

SSE JSON schema 层面：`chunk`、`status`、`event` 类型同名，前端 parser 可复用；但**深度研究、CSH、explore** 在 guest 可能不可用或行为简化。

#### 在本项目中的实现

- Guest 服务：`backend/api/guest_http_server.py`
- Guest 会话：`backend/api/routers/guest_conversations.py`
- Guest v2：`backend/api/routers/guest_agent.py`
- 共享 Handler/Agent：`application/agent_executors/*`

#### 走一遍具体例子

QA 团队只测 8082 主站 SSE 回归：

- 8083 升级后 Handler 版本落后 → guest 仍泄漏 thinking → **生产 guest 用户投诉**
- 正确：CI matrix `PORT=8082,8083` 各跑 SSE snapshot 测试

#### 常见误区

- ❌ guest thread_id 与主站互通——**checkpoint 不同文件**
- ❌ 协议一致 = 功能一致——路由与 KB 范围不同
- ✅ 格式可复用，行为需分端口测试

#### 延伸追问（L2/L3 only）

- guest 环境变量 `GUEST_API_SERVICE=1` 下游谁消费？
- 如何把 think 过滤抽到共享模块完成 P0-3？

---

## L2 补充 🟡

### Q15：V2 SSE 事件 `type` 完整清单与前端映射？

**一句话结论：** 主对话常用 **`chunk` / `thinking` / `status` / `event` / `follow_up_suggestions` / `draft_chunk` / `error`**；探索详解复用 subset；心跳为 `: ping` comment 行。

| type | 前端组件 | 说明 |
|------|----------|------|
| `chunk` | MessageList 正文 | 回答 token |
| `thinking` | ReasoningBlock | 推理（若未剥离） |
| `status` | ToolBadge | 「正在检索…」 |
| `event` | ProcessEventsPanel | `tool_start`/`tool_end` 等 kind |
| `follow_up_suggestions` | FollowUpSuggestions | 流末尾追问 |
| `draft_chunk` | ResearchDraftCard | 深研大纲 |

#### 参考

- `docs/docs_backend/sse_events_module.md`
- `00_architecture_diagrams.md` 图 12

---

### Q16：`follow_up_suggestions` 事件何时、如何产生？

**一句话结论：** 主流结束前后，`FollowUpSuggestionService.generate()` 基于**用户问题 + 助手回答 + tool_summary** 调 LLM；失败走模板兜底；可选 `persist_follow_up_to_checkpoint`。

```text
┌───────────────┐ ┌───────────────┐ ┌───────────────┐
│ StreamHandler │ │FollowUpService│ │      LLM      │
└───────────────┘ └───────────────┘ └───────────────┘

  [Note FU: 累积 tool_summary]

```

#### 配置

- `settings.follow_up_suggestions_enabled`
- guest 站可能仅用模板（见 guest 模块文档）

---

### Q17：深度研究 SSE 与普通问答如何分叉？

**一句话结论：** 普通问答持续 `chunk`；深研先 **`draft_chunk` 大纲流** → 用户确认 → **`process` / background 事件** → 可能切换 `/task/{id}/stream` 订阅后台进度。

| 阶段 | SSE 特征 |
|------|----------|
| 大纲 | `draft_chunk`、`research_draft_created` |
| 确认后 | `background_task_started` |
| 执行 | `process` kind 带 stage/progress |

---

### Q18：`build_internal_thread_key` 解决什么问题？

**一句话结论：** 将 `(user_id, client_thread_id)` 映射为 **checkpointer 内部 thread_id**，防止用户 A 猜测 thread_id 读取用户 B 的 checkpoint。

#### 在本项目中的实现

- `backend/core/agents/thread_key.py`
- conversations API 读历史前必须校验 Conversation 归属

#### 常见误区

- ❌ 前端传的 thread_id 直接作 SQLite key → ✅ 需 internal key + 归属校验
