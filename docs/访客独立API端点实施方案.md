# 访客独立 API 端点实施方案

> **文档属性**：历史实施方案，保留用于追踪设计演进。
> **实现状态**：✅ 已落地，并在 2026-07-09 按当前实现校正。
> **当前实现路径**：
> - 独立服务：`backend/api/guest_http_server.py`，默认 `GUEST_SERVER_PORT=8083`
> - 访客前端：`guest/`，Vite `8085`，`/api` 代理到 `GUEST_BACKEND_URL` 或 `http://127.0.0.1:8083`
> - 访客问答：`backend/api/routers/guest_agent.py`，`POST /api/v2/guest/query`
> - 短信登录：`backend/api/routers/guest_sms_auth.py`，`/api/v1/guest-sms/*`
> - Checkpointer：`guest_checkpoints.db`，不是 Redis TTL

## 1. 目标与范围

### 1.1 目标

- 提供独立访客问答接口 `POST /api/v2/guest/query`。
- 独立 guest API 服务只暴露访客前端需要的轻量路由，避免加载管理端、文档入库、MCP 等重服务。
- 访客问答强制轻量 workflow，禁用 deep-research、子智能体和 Skills。
- 支持两种身份形态：登录后的访客站点用户，以及无 token 的设备态访客。

### 1.2 范围

- 覆盖 `guest/` 前端、`guest_http_server.py`、`guest_agent.py`、`guest_conversations.py`、`guest_sms_auth.py`。
- 不改动登录用户完整智能体入口 `/api/v2/agent-runtime/query`。
- 不覆盖知识库上传 `/api/v1/uploads/*`、个人文件夹 `/api/v1/my-files` 与对话附件 `/api/v2/chat-files` 的详细设计。

## 2. 总体架构

### 2.1 服务与端口

| 组件 | 当前实现 |
| :--- | :--- |
| guest 前端 | `guest/package.json`，`vite --port 8085` |
| guest 前端代理 | `guest/vite.config.js`，`/api -> GUEST_BACKEND_URL || http://127.0.0.1:8083` |
| guest API | `backend/api/guest_http_server.py`，`GUEST_SERVER_PORT=8083` |
| 主 API | `backend/api/http_server.py`，端口取 `settings.server_port`；通过 v2 router 注册 guest endpoint |

### 2.2 guest API 注册路由

`guest_http_server.py:create_app()` 当前注册：

| 模块 | 注册前缀 | 主要端点 |
| :--- | :--- | :--- |
| `auth` | `/api/v1` | `/auth/login`、`/auth/verify`、`/auth/me`、CAS |
| `guest_sms_auth` | `/api/v1` | `/guest-sms/captcha`、`/guest-sms/send-code`、`/guest-sms/login`、`/guest-sms/register` |
| `guest_conversations` | `/api/v2` | 登录用户 guest 会话列表过滤 |
| `conversations` | `/api/v2` | 会话消息、重命名、删除、回滚等 |
| `files` | `/api/v2` | `/files/{file_id}/download`、工件下载等 |
| `guest_agent` | `/api/v2` | `/guest/query` |
| `agent_runtime` | `/api/v2` | `/agent-runtime/query` 与 profile info |

### 2.3 核心原则

- **路由隔离**：独立 guest API 不注册知识库管理、uploads、MCP、admin、worker 管理路由。
- **工作流降级**：guest endpoint 构造 `AgentRequest(requested_workflow="lightweight")`。
- **身份可选但边界明确**：有 Bearer/API Key 时按真实用户；无凭据时按 `X-Device-ID` 派生 guest 作用域。
- **checkpoint 物理隔离**：guest 服务使用 `guest_checkpoints.db`，同时可加载主站 checkpoint 作为登录用户历史回退。

## 3. API 设计

### 3.1 `POST /api/v2/guest/query`

- 鉴权：可选。`optional_verify_api_key()` 支持 `X-API-Key`、Bearer token、历史 Bearer API Key；无凭据返回 `user_id="guest"`。
- 内容类型：`application/json`
- 返回：`text/event-stream`

请求体：

| 字段 | 当前行为 |
| :--- | :--- |
| `question` | 必填，1 到 20000 字符 |
| `thread_id` | 可选；为空时后端生成 UUID；非空时走 `validate_external_thread_id()` |
| `agent_code` | `ne` 或 `csh`，默认 `ne` |
| `model` | 可选模型名，透传 |
| `use_search` | 可选，默认 `true` |
| `kb_ids` | 可选，最多 64 项，单项不超过 128 字符 |
| `mode` | 若为 `deep-research` 则 400；否则不改变轻量执行路径 |
| `task_type` | 若为 `deep_research` 则 400 |
| `subagent_enabled` | 任意 truthy 都返回 400 |

响应事件：

- `chunk`：可见文本片段。
- `message.replace`：替换当前 assistant 内容。
- `thinking`：路由会过滤 `<think>` 段，正常配置下 guest 请求关闭 thinking。
- `status`：workflow 状态。
- `event`：流程事件。
- `follow_up_suggestions`：按配置流式返回后续建议。
- `data: [DONE]`：流结束。
- `{"error": ...}`：流内错误。

### 3.2 身份与线程键

```python
if auth.user_id and auth.user_id not in {"", "guest"}:
    user_id = auth.user_id
    conversation_id = external_tid
    internal_tid = build_internal_thread_key(user_id, external_tid)
else:
    device_id = resolve_device_id(request)
    user_id = f"guest_{device_id}"
    internal_tid = f"g_{device_id}__t_{external_tid}"
    conversation_id = internal_tid
```

`resolve_device_id()` 当前要求 `X-Device-ID` 匹配：

- `gdev_<uuid>`
- `gdev_<10-16位时间戳>_<16-64位随机串>`

服务端不会直接把浏览器 ID 放进内部 key，而是用 `minio_secret_key` 做 HMAC 后截断为 32 位。

## 4. 登录、短信与 token

访客站点当前不是纯免登录页面。`guest/src/App.vue` 在没有 token 时展示 `GuestLogin`，登录成功后才进入聊天页。

| 登录方式 | 前端 | 后端 |
| :--- | :--- | :--- |
| 账号密码 | `AuthAPI.login()` | `POST /api/v1/auth/login` |
| 短信登录 | `AuthAPI.guestSmsLogin()` | `POST /api/v1/guest-sms/login` |
| 短信注册 | `AuthAPI.guestSmsRegister()` | `POST /api/v1/guest-sms/register` |
| 人机验证 | `AuthAPI.getGuestSmsCaptcha()` | `GET /api/v1/guest-sms/captcha` |
| 发送验证码 | `AuthAPI.sendGuestSmsCode()` | `POST /api/v1/guest-sms/send-code` |

短信实现要点：

- captcha、短信验证码、发送锁均存 Redis。
- 验证码 TTL 300 秒；同手机号发送锁 60 秒。
- 短信网关使用腾讯云 SMS；配置缺失时返回明确错误。
- 登录时若手机号用户不存在，会创建 `username=guest_{telephone}` 的普通 `User` 并签发 token。
- token 与主认证一致：`auth:token:{token}`，有效期 24 小时。

## 5. 会话与存储

### 5.1 当前两种历史模式

1. **未登录 guest restricted 模式**
   主前端在 `VITE_FORCE_GUEST_MODE=true` 或无 token 时调用 `/api/v2/guest/query`，并使用 IndexedDB `guest-chat-db` 保存 `conversations`、`messages`。这条路径会发送 `X-Device-ID`。

2. **独立 guest 前端登录模式**
   `guest/` 前端登录后复用主前端 `useAgentChat` 与 `ConversationAPI`。会话列表来自后端 `/api/v2/conversations`。独立 guest 服务先注册 `guest_conversations`，该实现会过滤出当前用户在 `guest_checkpoints.db` 中确有消息的会话。

### 5.2 服务端 checkpoint

- guest 服务通过 `init_app_sqlite_checkpointer(app, GUEST_CHECKPOINT_DB)` 初始化 `guest_checkpoints.db`。
- `MAIN_CHECKPOINT_DB` 默认 `checkpoints.db`，作为 `main_checkpointer` 回退加载，供登录用户读取主站历史。
- 当前实现不是 Redis TTL 会话态；Redis 不保存问答上下文。

## 6. 安全与风控

已实现：

- deep-research / 子智能体请求直接 400。
- `X-Device-ID` 有格式和长度限制，并用 HMAC 派生内部作用域。
- `question` 最大 20000 字符。
- `kb_ids` 数量和单项长度受 Pydantic 校验。
- guest endpoint 固定 `selected_skills=[]`、`thinking_enabled=False`、`subagent_enabled=False`。
- CORS 仅配置访客前端与部署域名。

未在当前代码中看到的点：

- 独立的 IP + device_id 限流中间件。
- SSE 硬超时中断。
- Redis TTL 清理 guest 对话上下文。

这些可以作为后续风控增强项，而不是当前已实现能力。

## 7. 前端协同

当前前端有两套相关实现：

- `guest/src/App.vue`：独立访客站点，登录后使用主前端的 `useAgentChat`，`VITE_FORCE_GUEST_MODE=true` 使 query endpoint 固定为 `/api/v2/guest/query`。
- `guest/src/services/guestChatService.js`：轻量封装，仍直接调用 `/api/v2/guest/query`；但其生成的 device id 格式与后端当前 `gdev_*` 校验不一致，应视为遗留代码或需后续修正。

## 8. 验收标准

- guest 前端 `8085` 能通过代理访问 guest API `8083`。
- 账号密码、短信登录、短信注册均能签发 token，并通过 `/api/v1/auth/verify`。
- 登录后 `/api/v2/guest/query` 使用 `u_{user_id}__t_{thread_id}` 内部键，走 `guest_checkpoints.db`。
- 无 token 调用 `/api/v2/guest/query` 时，合法 `X-Device-ID` 能派生 `g_{device}__t_{thread}`。
- deep-research、`task_type=deep_research`、`subagent_enabled=true` 返回 400。
- guest 会话列表只显示当前用户在 guest checkpoint 中有消息的会话。

---

本方案当前已从“纯免登录、Redis TTL 短期态”的历史设想，演进为“登录访客站点 + 可选设备态 guest endpoint + SQLite checkpoint 隔离”的实现。
