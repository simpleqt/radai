# 独立访客与轻量问答 API 模块详解

> **最后同步**：2026-07-14
> **现状说明**：访客前端位于 `guest/`，开发端口 **8085**，代理到 `GUEST_BACKEND_URL` 或默认 `http://127.0.0.1:8083`。独立访客 API 服务位于 `backend/api/guest_http_server.py`，默认监听 **8083**。访客问答入口为 `POST /api/v2/guest/query`，内部调用 `AgentService.run_stream()` 并强制轻量 workflow。

系统提供两层访客能力：

1. **独立访客专用 API 服务**：`backend/api/guest_http_server.py`，默认 `:8083`，只注册访客站点需要的认证、会话、文件读取与轻量问答路由。
2. **主 API 上的访客问答路由**：主服务通过 `agent_runtime_v2` 注册 `/api/v2/guest/query`，供普通站点在 guest restricted 模式下复用。访客短信认证 `/api/v1/guest-sms/*` 目前只由独立 guest API 注册。

```text
guest 前端 :8085
  │  Vite proxy /api -> GUEST_BACKEND_URL 或 http://127.0.0.1:8083
  ▼
guest API :8083
  ├─ /api/v1/auth/*              账号密码/CAS token
  ├─ /api/v1/guest-sms/*         访客短信验证码、注册、登录
  ├─ /api/v2/guest/query         轻量问答 SSE
  ├─ /api/v2/agent-runtime/*     登录态运行入口与 profile info
  ├─ /api/v2/conversations       登录用户会话列表/历史
  └─ /api/v2/files/*             文件下载与工件读取
```

---

## 双通道架构对比

| 特性维度 | 登录用户通道 (`/api/v2/agent-runtime/query`) | 访客轻量通道 (`/api/v2/guest/query`) |
| :--- | :--- | :--- |
| 物理进程与端口 | 主 API `:8082`（实际端口取 `settings.server_port`） | 独立 guest API `:8083`，也可由主 API 注册同一路由 |
| 前端入口 | `frontend/` | `guest/`，Vite `:8085` |
| 身份认证 | `Authorization: Bearer <token>` 或 `X-API-Key` | `optional_verify_api_key`：有 token 则按登录用户；无 token 则要求合法 `X-Device-ID` 并派生 `guest_<hash>` |
| token 来源 | `/api/v1/auth/login`、CAS、普通短信注册 | `/api/v1/guest-sms/login`、`/api/v1/guest-sms/register` 也签发同类 Redis token |
| 对话历史 | MySQL `Conversation` + LangGraph checkpoint | 未登录模式由 IndexedDB 保存展示历史；已登录 guest 前端通过 `/api/v2/conversations` 读取服务端历史 |
| Checkpointer | 默认 `checkpoints.db`；可通过 `AGENT_CHECKPOINTER_BACKEND=redis` 使用共享 Redis | guest 服务固定使用 `guest_checkpoints.db`，并加载主站 checkpoint 作为登录用户历史回退 |
| 工作流 | full / lightweight / deep_research 等 | 强制 `requested_workflow="lightweight"`，禁用 deep-research、子智能体、思考链 |
| Skills | 按用户/智能体策略装配 | `selected_skills=[]` |

---

## 访客服务生命周期与预热

`guest_http_server.py` 使用独立 FastAPI lifespan：

- 设置 `GUEST_API_SERVICE=1`，初始化日志、应用目录与 SQLite checkpointer。
- `GUEST_SERVER_PORT` 默认 `8083`，`GUEST_CHECKPOINT_DB` 默认 `guest_checkpoints.db`，`GUEST_SERVER_WORKERS` 默认 `1`。
- 通过 `_wait_with_timeout()` 对 RAG、RAG 实例预热、检索栈预热、guest checkpointer 和主站 checkpointer 回退做超时保护；超时任务转入后台继续执行，不阻塞 HTTP 服务启动。
- 访客知识库不再硬编码两个常量，而是从运行时智能体 `ne` 的知识库绑定读取 active KB，并预热 RAG instance、关键词索引与 DirectRetriever。
- CORS 允许 `localhost/127.0.0.1:8085`、`localhost/127.0.0.1:8084`、`fpitekai.com` 及已配置内网/公网访问域。

健康检查：

```text
GET /health
```

返回 `service=guest-qa-api`、端口、checkpoint 数据库名以及组件 readiness。

---

## 认证与短信登录

独立访客服务注册两类认证路由：

| 路由 | 说明 |
| :--- | :--- |
| `POST /api/v1/auth/login` | 账号密码登录，签发 24 小时 Redis token |
| `GET /api/v1/auth/verify`、`GET/PATCH /api/v1/auth/me` | token 验证与用户资料 |
| `GET /api/v1/guest-sms/captcha` | 生成算术验证码，Redis key `guest_sms:captcha:{id}`，TTL 300 秒 |
| `POST /api/v1/guest-sms/send-code` | 校验 captcha 后调用腾讯云短信，验证码 Redis key `guest_sms:code:{telephone}`，TTL 300 秒，发送锁 60 秒 |
| `POST /api/v1/guest-sms/login` | 手机验证码登录；不存在用户时创建 `username=guest_{telephone}` 用户并签发 token |
| `POST /api/v1/guest-sms/register` | 手机验证码 + 密码注册；成功后签发 token |

前端 `guest/src/components/GuestLogin.vue` 当前默认要求用户先完成账号登录、注册或短信登录，再进入聊天页。登录态写入主前端复用的 `authState`，后续请求自动携带 `Authorization: Bearer <token>`。

`/api/v2/guest/query` 本身仍支持无 token 调用：无 token 时读取 `X-Device-ID`，格式必须为 `gdev_<uuid>` 或 `gdev_<timestamp>_<random>`；服务端用 `minio_secret_key` 做 HMAC，派生不可伪造的内部设备作用域。

---

## 访客问答执行流

```text
POST /api/v2/guest/query
  │
  ├─ 校验 mode=deep-research / task_type=deep_research / subagent_enabled → 400
  ├─ external_tid = payload.thread_id or uuid4()
  ├─ 有 Bearer/API Key:
  │    user_id = auth.user_id
  │    conversation_id = external_tid
  │    internal_tid = u_{user_id}__t_{external_tid}
  └─ 无凭据:
       device_id = HMAC(X-Device-ID 或临时 anon uuid)
       user_id = guest_{device_id}
       conversation_id = g_{device_id}__t_{external_tid}
       internal_tid = g_{device_id}__t_{external_tid}
  │
  ▼
AgentRequest(
  requested_workflow="lightweight",
  selected_skills=[],
  subagent_enabled=False,
  thinking_enabled=False,
  selected_kb_ids=payload.kb_ids or []
)
  │
  ▼
AgentService.run_stream()
  │
  ▼
标准 AgentEvent → guest 路由边缘映射为 SSE:
chunk / message.replace / thinking / status / event / follow_up_suggestions / [DONE]
```

请求体当前支持字段：

- `question`：必填，1 到 20000 字符。
- `thread_id`：可选，使用 `validate_external_thread_id()` 校验。
- `agent_code`：`ne` 或 `csh`，默认 `ne`。
- `model`、`use_search`、`kb_ids`：透传到轻量 AgentRequest。
- `mode`、`task_type`、`subagent_enabled`：仅用于拒绝 deep-research/子智能体。

前端 `frontend/src/api/agent-v2.ts` 在 guest endpoint 下会强制发送 `selected_skills=[]`、`thinking=false`、`mode=lightweight`；`useAgentChat` 在 `VITE_FORCE_GUEST_MODE=true` 或未登录时选择 `/api/v2/guest/query`。

---

## 会话与历史策略

当前实现同时存在两种历史模式：

1. **未登录 guest restricted 模式**
   `useAgentChat` 使用 IndexedDB `guest-chat-db` 的 `conversations` 与 `messages` 表保存展示历史；线程 ID 和消息都留在浏览器本地。`X-Device-ID` 存在 `sessionStorage`，用于服务端 checkpoint 作用域。

2. **独立 guest 前端登录模式**
   `guest/src/App.vue` 登录后复用 `ConversationAPI.list()`、`ConversationAPI.getMessages()`，服务端通过 Bearer token 按真实用户读取会话。独立 guest 服务中的 `guest_conversations.py` 覆盖 `GET /api/v2/conversations` 列表：只返回当前用户在 `guest_checkpoints.db` 中确有消息的会话，避免主站 MySQL 中“有标题但 guest checkpoint 无消息”的条目混入。

服务端状态：

- guest 服务使用 `init_app_sqlite_checkpointer(app, "guest_checkpoints.db")`。
- 同时加载 `MAIN_CHECKPOINT_DB`（默认 `checkpoints.db`）为 `main_checkpointer` 回退，供登录用户读取主站产生的历史消息。
- 当前实现不是 Redis TTL 会话态；Redis 只用于 auth token 与短信验证码。

---

## 安全约束

- deep-research、`task_type=deep_research`、`subagent_enabled=true` 在 guest endpoint 返回 400。
- `X-Device-ID` 长度与格式受限，内部作用域使用 HMAC 派生，不直接信任浏览器 ID。
- 可选认证依赖 `optional_verify_api_key()` 支持 `X-API-Key`、Bearer token、历史 Bearer API Key；无凭据才降级为 guest。
- 访客 endpoint 固定轻量 workflow、关闭思考链、清空技能。
- follow-up suggestions 可按配置开启，guest 路由会流式返回 `follow_up_suggestions` 事件。

---

© 2026 核电子学智能助手项目组
