# 05 · 数据库与认证

> 📊 **图表：** 完整 ASCII 图见 [00_architecture_diagrams.md](./00_architecture_diagrams.md) 图 13、18、17、2、**26、30**；正式文档 → [auth_module.md](../../docs/docs_backend/auth_module.md) · [infrastructure_module.md](../../docs/docs_backend/infrastructure_module.md)

---

## L1 初级 🟢

### Q1：项目用了哪些数据库/存储？各自存什么？

**一句话结论：** 业务与检索数据分散在「MySQL 业务库 + SQLite Agent 状态 + Redis 缓存 + Milvus 向量 + NebulaGraph 图谱 + MinIO/本地文件」六类存储，各司其职，不可互相替代。

#### 为什么问这个 / 背景

「数据库」一词在本项目里是**多存储组合**。面试官想确认你不会把对话正文也塞进 MySQL，或把用户表放 SQLite。

#### 详细解释

存储全景：

```
┌─────────────┐     ┌──────────────┐     ┌─────────────┐
│   MySQL     │     │ SQLite cp    │     │   Redis     │
│ 业务元数据   │     │ Agent 状态   │     │ Token/RAG KV│
└─────────────┘     └──────────────┘     └─────────────┘
       │                    │                    │
       └────────────────────┼────────────────────┘
                            │
              ┌─────────────┴─────────────┐
              │      RAG 检索层            │
              │ Milvus │ Nebula │ MinIO   │
              └───────────────────────────┘
```

```text
┌─────────────┐  Authorization: Bearer   ┌─────────────────┐
│  客户端请求  │ ───────────────────────► │ verify_api_key  │
└─────────────┘  或 X-API-Key / CAS       │ (dependencies)  │
                                          └────────┬────────┘
                                                   │
              ┌────────────────┬───────────────────┼────────────────┐
              ▼                ▼                   ▼                ▼
        Redis Token       MySQL api_keys      CAS SSO ticket    短信验证码
        auth:token:*      hash 校验           重定向回调         auth_sms.py
              │                │                   │                │
              └────────────────┴───────────────────┴────────────────┘
                                                   │
                                                   ▼
                                          CurrentUser 注入 Handler
```



统一 Depends 入口（API Key 优先顺序）：

```102:130:backend/api/dependencies/auth.py
async def verify_api_key(
    x_api_key: str | None = Header(None, alias="X-API-Key"),
    authorization: str | None = Header(None, alias="Authorization"),
) -> ApiKeyAuth:
    """统一认证：优先 X-API-Key；其次 Bearer Token；最后兼容 Bearer API Key。"""
    ...
    if api_key:
        auth = await _resolve_api_key_auth(api_key)
        ...
    if bearer:
        token_auth = await _resolve_bearer_token_auth(bearer)
        if token_auth:
            return token_auth
        key_auth = await _resolve_api_key_auth(bearer)
        ...
```

Web 会话路由（如 v2 agent）常用 `get_current_user`，**仅接受 Redis Session**，不走 API Key 兼容链。

#### 在本项目中的实现

- 统一 API Key / Token：`backend/api/dependencies/auth.py`
- 登录/CAS/Session：`backend/api/routers/auth.py`
- 短信：`backend/api/routers/auth_sms.py`
- 日志上下文（非阻断）：`backend/api/http_server.py` middleware

#### 走一遍具体例子

教职工首次访问：

1. 点击「统一身份登录」→ 302 到 CAS
2. CAS callback 带 ticket → 服务端 validate → 创建/更新 MySQL User → `_issue_auth_token` 写 Redis
3. 前端存 token，后续 Bearer 访问 v2 API

脚本调用管理 API：

1. 管理员创建 API Key 写入 `api_keys` 表
2. 请求带 `X-API-Key: sk-xxx` → `verify_api_key` → `require_admin` 保护的路由

#### 常见误区

- ❌ CAS 后不用 Redis——仍签发 Bearer Token 存 Redis
- ❌ 所有路由都用 `verify_api_key`——v2 问答常用 `get_current_user`
- ✅ 列方式 + Depends 差异

#### 延伸追问（L2/L3 only）

- Redis 挂了 Session 会怎样？（见 Q12）
- CAS 开放重定向如何防护？（见 Q7）

---

### Q3：API Key 校验检查哪些条件？

**一句话结论：** `_resolve_api_key_auth` 校验 Key 存在、激活、未过期、关联用户（若有）仍激活——任一失败返回 None，上层抛 401。

#### 为什么问这个 / 背景

API Key 是长期凭证，面试常问「禁用 Key 是否即时生效」「过期怎么处理」。

#### 详细解释

校验流程：

```
输入 api_key 字符串
  → SELECT api_keys JOIN users
  → key 存在？
  → is_active == True？
  → expires_at 为空 OR > now()？
  → 若绑定 user_id：user.is_active == True？
  → 返回 ApiKeyAuth(api_key, user_id, role)
```

```text
┌─────────────────────────┐         thread_id          ┌─────────────────────────┐
│ MySQL conversations     │ ─────────────────────────► │ SQLite checkpoints      │
├─────────────────────────┤                            ├─────────────────────────┤
│ title, user_id          │                            │ messages (Human/AI)     │
│ metadata_, is_deleted   │                            │ LangGraph state         │
│ 列表/改名/软删           │                            │ tool 历史               │
└─────────────────────────┘                            └─────────────────────────┘
         │                                                        │
         ▼                                                        ▼
   GET /conversations 列表                              GET /messages 详情
   DELETE 软删 + 清 checkpoint                           回滚截断 checkpoint
```



操作对照：

| 用户操作 | MySQL | SQLite checkpoint |
|----------|-------|-------------------|
| 新建对话 | INSERT conversations | 首次 query 时写入 |
| 改名 | UPDATE title | 不变 |
| 删除 | is_deleted=True | DELETE checkpoint 行 |
| 回滚 | 可能更新 metadata | 截断 messages state |
| 列表 | SELECT where user_id | 不读 |

#### 在本项目中的实现

- 列表/删除/回滚：`backend/api/routers/conversations.py`
- v2 流式前确保记录：`backend/api/routers/agent_runtime.py` — `_ensure_conversation_record`
- Checkpointer 获取：`backend/core/checkpointer/` — `get_checkpointer_from_request`

删除时会清理 checkpoint：

```122:140:backend/api/routers/conversations.py
        checkpointer = get_checkpointer_from_request(request)
        if hasattr(checkpointer, "conn"):
            ...
                await checkpointer.conn.execute(
                    f"DELETE FROM {table_name} WHERE thread_id = ?",
                    (internal_thread_id,),
                )
            await checkpointer.conn.commit()
```

#### 走一遍具体例子

用户点击删除对话 `thread_id=T1`：

1. MySQL：`is_deleted=True`
2. SQLite：删除 `thread_id` 对应 checkpoint 表行
3. 文件：`storage.delete_all_files()` + `delete_thread_dir`
4. 若只完成步骤 1，列表不显示但磁盘/checkpoint 仍占空间

#### 常见误区

- ❌ conversations 表存 messages JSON——**正文在 checkpoint**
- ❌ guest 与主站共用 checkpoint——**不同 db 文件**
- ✅ thread_id 是关联键

#### 延伸追问（L2/L3 only）

- `build_internal_thread_key` 做什么？
- 回滚 `keep_user_turns` 如何映射到 checkpoint？

---

## L2 中级 🟡

### Q5：为什么 async MySQL 使用 NullPool 而 sync 用 QueuePool？

**一句话结论：** 子智能体在**独立线程的持久 event loop** 中跑 async DB；若 async 引擎用 QueuePool，连接会被绑定到 loop A，回到主 ASGI loop B 使用时报 `Future attached to a different loop`——NullPool 每次新建连接避免跨 loop 共享。

#### 为什么问这个 / 背景

这是 asyncio + SQLAlchemy 经典坑。能讲清楚说明读过 `connection.py` 注释并理解 subagent 线程模型。

#### 详细解释

问题场景：

```
Main ASGI Event Loop (loop A)
  → checkout conn #1 from QueuePool
  → 传给 subagent thread 的 loop B 使用
  → 归还 pool
  → loop A 再次 checkout conn #1
  → 💥 Future attached to a different loop
```

```text
┌────────────────┐
│ QueuePool 共享连接 │
└────────────────┘

  QueuePool 共享连接 ──────────────► Future attached / to different loop

  Future attached / to different loop ──────────────► NP

  NP ──────────────► Main ASGI loop A

  Main ASGI loop A ──────────────► Subagent loop B

  QueuePool 共享连接 ──(跨 loop 复用)──► Future attached / to different loop

```

```
Main ASGI Event Loop (loop A)          Subagent Thread Loop (loop B)
        │                                        │
        ▼                                        ▼
   async 查 MySQL                          async 查 MySQL
        │                                        │
        └────────── QueuePool 共享连接 ──────────┘
                           │
                           ▼
              Future attached to a different loop 💥

解决方案：async 引擎 → NullPool（每次新建连接，不跨 loop 复用）
          sync 引擎  → QueuePool（单线程/同步场景可池化）
```

架构原因：

- 子 Agent 线程（`core/tools/task_tool.py` 等）在独立线程维护**长期运行**的 event loop
- 子智能体可能 async 查 MySQL（任务状态、conversation metadata）
- 主线程 API 也在 async 查 MySQL

解决方案对比：

| 引擎 | poolclass | 行为 |
|------|-----------|------|
| sync `get_engine()` | QueuePool | 线程内同步调用，连接复用安全 |
| async `get_async_engine()` | NullPool | 每次 checkout 新建，return 即 close |

源码注释：

```70:75:backend/db/connection.py
        # Use NullPool for async engine to avoid "Future attached to a different loop"
        # errors that occur when connections checked out in one event loop (e.g.,
        # subagent thread's persistent loop) are later used in another loop
        # (e.g., the main ASGI event loop). NullPool creates a new connection
        # for each checkout and closes it on return, preventing cross-loop
        # connection sharing issues.
```

代价：async 路径**无法**连接池复用，高 QPS 时 MySQL 连接数上升，需靠 MySQL `max_connections` 与 API 限流。

#### 在本项目中的实现

- 连接：`backend/db/connection.py`
- 子智能体 executor：`backend/core/tools/task_tool.py`（独立线程执行）
- session 生命周期：`get_async_session()` commit/rollback 模式

#### 走一遍具体例子

深度研究子智能体在线程 T 内 `async with get_async_session()` 查 task：

1. NullPool 在 loop T 新建连接，用完关闭
2. 主 API 同时在 loop M 查 User，另建连接
3. 无 cross-loop 污染

若误改 QueuePool：

- 间歇性 500，`Future attached to a different loop`，难复现

#### 常见误区

- ❌ NullPool 因为「async 不支持连接池」——**支持，是跨 loop 不安全**
- ❌ sync 也改 NullPool——sync 无此问题，QueuePool 更高效
- ✅ 绑定 subagent 线程模型解释

#### 延伸追问（L2/L3 only）

- 能否 per-loop 独立 async engine 而恢复 QueuePool？
- NullPool 下如何监控 MySQL 连接风暴？

---

### Q6：Redis Token 缓存的结构和失效策略？

**一句话结论：** Key 为 `auth:token:{token}`，Value 为 JSON（含 user_id、username、role 等），TTL 与 `TOKEN_EXPIRE_HOURS`（默认 24h）对齐；校验时 Redis 命中则快路径，miss 则 Session 失效。

#### 为什么问这个 / 背景

Session 方案的核心是 Redis 可用性与 TTL 一致性。登录风暴下 Redis 是热路径。

#### 详细解释

写入（登录/CAS 成功后）：

```263:280:backend/api/routers/auth.py
async def _issue_auth_token(
    redis_client: Redis,
    user: User,
    display_name: str | None = None,
) -> str:
    token = generate_token()
    token_data = {
        "user_id": user.user_id,
        "username": user.username,
        "display_name": (display_name or user.username),
        "role": user.role if hasattr(user, "role") else "user",
    }
    await redis_client.setex(
        f"auth:token:{token}",
        TOKEN_EXPIRE_HOURS * 3600,
        json.dumps(token_data),
    )
    return token
```

```text
┌────────────┐ ┌────────────┐ ┌────────────┐ ┌────────────┐
│     前端     │ │  auth.py   │ │   Redis    │ │MySQL users │
└────────────┘ └────────────┘ └────────────┘ └────────────┘


```

读取路径：

| 调用方 | 行为 |
|--------|------|
| Middleware | Redis GET，命中则日志上下文，**不查 DB** |
| `get_current_user` | Redis GET → 再查 MySQL 验证 user 仍存在且 active |
| `_resolve_bearer_token_auth` | Redis GET → DB 查 role/is_active |

注销：

- `DELETE auth:token:{token}`（logout 路由）

#### 在本项目中的实现

- 签发：`backend/api/routers/auth.py` — `_issue_auth_token`, `TOKEN_EXPIRE_HOURS = 24`
- 校验 Depends：`get_current_user`, `verify_api_key` 中的 `_resolve_bearer_token_auth`
- Redis 客户端：`backend/db/redis.py`

#### 走一遍具体例子

用户登录后 23h 持续使用：

- Token TTL 24h，无滑动续期——**24h 后需重新登录**（除非前端实现 refresh，当前未见）

管理员禁用用户：

- `get_current_user` 仍可能 Redis 命中，但 MySQL `is_active=False` → **403**
- Middleware 日志仍有 user_id，但 API 被拒

#### 常见误区

- ❌ Redis 存 password——只存 session metadata
- ❌ Token 永不过期——**setex 24h**
- ✅ TTL + 禁用用户二次校验

#### 延伸追问（L2/L3 only）

- 如何实现 sliding expiration？
- Redis 集群故障切换对 Session 的影响？

---

### Q7：CAS SSO 有哪些安全防护措施？

**一句话结论：** 项目对 CAS 回调实施**同源/相对路径重定向校验**防 Open Redirect，ticket 一次性服务端校验，动态 service URL 适配多域名；还需配合 TLS、CAS 标准实践与 Token 签发衔接。

#### 为什么问这个 / 背景

CAS 集成最常见漏洞是开放重定向与 ticket 重用。要能指出代码级防护点。

#### 详细解释

开放重定向防护：

```102:114:backend/api/routers/auth.py
def _is_safe_redirect_target(target: str, request: Request) -> bool:
    """只允许站内相对路径或同源绝对地址，避免开放重定向。"""
    if not target:
        return False
    if target.startswith("/") and not target.startswith("//"):
        return True

    parsed = urlparse(target)
    if not parsed.scheme or not parsed.netloc:
        return False

    current = urlparse(str(request.base_url))
    return parsed.scheme == current.scheme and parsed.netloc == current.netloc
```

其他措施：

| 措施 | 说明 |
|------|------|
| Ticket 校验 | 服务端请求 CAS `validate_url`，解析 XML，一次性 |
| 动态 service URL | 根据 `X-Forwarded-Host` 替换配置域名，支持 radai2 / csh-ai 多域 |
| SSL 验证 | `cas_verify_ssl` 可配置 |
| Cookie 存 target | `CAS_TARGET_URL_COOKIE` 配合安全 redirect |
| 成功后 Token | 不写 session cookie 而签发 Redis Bearer，减 CSRF 面 |

#### 在本项目中的实现

- CAS 路由：`backend/api/routers/auth.py`
- 配置：`backend/config/settings.py` — `cas_*` 字段
- Token 衔接：`_issue_auth_token`

#### 走一遍具体例子

攻击者构造 `?target=https://evil.com`：

- `_is_safe_redirect_target` 返回 False，回退默认 `/login`
- 用户不会被 phishing 到恶意站

合法 CAS 回流：

1. User → CAS 登录
2. Redirect `service_url?ticket=ST-xxx`
3. 服务端 validate ticket → 创建 session token → 前端 `cas_frontend_success_url`

#### 常见误区

- ❌ CAS 登录后无本地 Token——仍走 Redis Bearer
- ❌ 允许任意 `//evil.com` 相对 URL——`startswith("//")` 被拦
- ✅ Open Redirect + ticket + TLS 标准三板斧

#### 延伸追问（L2/L3 only）

- 多域名动态 service URL 如何防 Host Header 注入？
- CAS 登出如何同步清 Redis Token？

---

### Q8：`require_admin` 如何实现？

**一句话结论：** Depends 链先 `verify_api_key` 解析身份（含 role），再 `require_admin` 检查 `role.lower() == "admin"`，否则 403。

#### 为什么问这个 / 背景

RBAC 最小实现。管理面路由（Agent CRUD、KB 绑定）必须挂 admin Depends。

#### 详细解释

```133:137:backend/api/dependencies/auth.py
async def require_admin(auth: ApiKeyAuth = Depends(verify_api_key)) -> ApiKeyAuth:
    """要求管理员权限"""
    if (auth.role or "").lower() != "admin":
        raise HTTPException(status_code=403, detail="需要管理员权限")
    return auth
```

role 来源：

- API Key 路径：JOIN `users.role`
- Bearer Token 路径：Redis JSON 含 role，但 `verify_api_key` 的 token 解析会再查 DB 确认

典型挂载：

- `backend/api/routers/agents.py` — Agent/KB 绑定管理
- 其他 v1 管理路由

非 admin 用户：

- 401 vs 403：未认证 401；已认证非 admin 403

#### 在本项目中的实现

- Depends：`backend/api/dependencies/auth.py`
- 消费：`backend/api/routers/agents.py` 等

#### 走一遍具体例子

普通用户 Bearer Token 调用 `POST /api/v1/agents`：

1. `verify_api_key` 成功，`role=user`
2. `require_admin` → 403 「需要管理员权限」

Admin 使用 API Key：

1. `X-API-Key` 绑定 admin 用户
2. 通过 Depends，执行 CRUD

#### 常见误区

- ❌ 只在前端隐藏按钮——**必须后端 Depends**
- ❌ guest 服务含 admin 路由——8083 无管理路由
- ✅ 403 vs 401 区分

#### 延伸追问（L2/L3 only）

- 是否需要 finer RBAC（kb_admin）？
- admin API Key 轮换流程？

---

### Q9：SQLAlchemy 会话如何管理 commit/rollback？

**一句话结论：** `get_async_session()` 是 async generator：正常结束 `commit`，异常 `rollback` 再 raise；`expire_on_commit=False` 避免 async 下 lazy load 触发隐式 IO。

#### 为什么问这个 / 背景

「谁负责 commit」决定 transaction 边界。FastAPI Depends generator 模式是项目标准范式。

#### 详细解释

```103:112:backend/db/connection.py
async def get_async_session() -> AsyncGenerator[AsyncSession, None]:
    """获取异步会话"""
    factory = get_async_session_factory()
    async with factory() as session:
        try:
            yield session
            await session.commit()
        except Exception:
            await session.rollback()
            raise
```

要点：

| 配置/行为 | 原因 |
|-----------|------|
| `autocommit=False` | 显式事务 |
| `autoflush=False` | 减少意外 flush |
| `expire_on_commit=False` | commit 后对象仍可用，async 不会 hidden await |
| 路由内异常 | 自动 rollback，不会半提交 |

注意：generator 内 **手动 `await session.commit()`** 可能导致 double commit 或逻辑混乱，应遵循「路由只改对象，出口统一 commit」。

#### 在本项目中的实现

- 定义：`backend/db/connection.py`
- Depends：`get_db_session` in `auth.py` dependencies；路由直接 `Depends(get_async_session)`

#### 走一遍具体例子

路由创建 conversation：

1. `async def create(..., session: AsyncSession = Depends(get_async_session))`
2. `session.add(conv)` 无 flush
3. 正常返回 → generator `commit`
4. 若 `raise HTTPException(404)` → rollback，conv 未持久化

#### 常见误区

- ❌ HTTPException 不触发 rollback——在 Python 3 FastAPI 中 **Exception 会 rollback**（包括 HTTPException 若未特殊处理——需注意 HTTPException 继承 Exception，会 rollback！）
- ❌ 长流式 SSE 整个持有 session——v2 agent 应控制 session 生命周期，避免长事务
- ✅ generator commit/rollback 模式 + expire_on_commit

#### 延伸追问（L2/L3 only）

- 流式 API 中 `_ensure_conversation_record` 如何避免 break main flow？
- 只读路由是否需要 commit？

---

### Q10：模型配置存 DB 时表结构是什么？

**一句话结论：** `llm_models` 表（`backend/db/models/llm_model.py`）存模型名、provider、LangChain `use` 类路径、API 连接参数、能力 flags 等；默认 `MODEL_CONFIG_SOURCE=db` 时 **MySQL 优先于 config.yaml**。

#### 为什么问这个 / 背景

运维改模型不应改代码发版。DB 配置 + Redis 热更新是生产常见模式。

#### 详细解释

核心字段（节选）：

| 字段 | 含义 |
|------|------|
| name + usage_scope | 联合唯一，chat 域对话模型 |
| display_name, provider | 前端展示 |
| use | 如 `langchain_openai:ChatOpenAI` |
| model, api_key, base_url | 连接参数 |
| max_tokens, temperature, extra_body | 推理参数 |
| supports_vision, supports_thinking | 能力标记 |
| is_active, sort_order | 启用与排序 |

加载优先级（factory 文档）：

```
MODEL_CONFIG_SOURCE=db (默认):
  MySQL llm_models (usage_scope='chat', active)
    → config.yaml models[]
      → settings 默认

MODEL_CONFIG_SOURCE=yaml:
  跳过 DB → yaml → settings
```

热更新：

- `http_server.py` lifespan 启动 `subscribe_model_changes()` 后台 Task
- 管理端改模型 → 发 Redis 消息 → worker 刷新内存缓存

#### 在本项目中的实现

- ORM：`backend/db/models/llm_model.py`
- 工厂：`backend/core/models/factory.py`
- 缓存订阅：`backend/core/models/model_cache.py`（lifespan 引用）
- 管理 API：`backend/api/routers/models.py`

#### 走一遍具体例子

运维将默认模型从 MiniMax 切到 DeepSeek：

1. 管理 UI 更新 `llm_models` 行，`is_active` 切换
2. Redis pub 通知
3. 新请求 `create_chat_model("deepseek-v4-flash")` 读新配置
4. **已在进行的长 SSE 不受影响**（已实例化 LLM）

#### 常见误区

- ❌ 只改 yaml 生产就生效——默认 **DB 优先**
- ❌ api_key 明文展示给前端——应后端持有，前端只选 model name
- ✅ 表结构 + 优先级 + 热更新三角

#### 延伸追问（L2/L3 only）

- `usage_scope` 预留 kb_chat/embedding 如何使用？
- api_key 加密存储策略？

---

## L3 高级 🔴

### Q11：SQLite Checkpointer 在生产多实例部署的风险？

**一句话结论：** 多 Uvicorn worker/多机共享同一 SQLite 文件会导致写锁瓶颈、NFS 损坏风险、备份遗漏和无法跨机 session——生产高并发应迁移 PostgreSQL checkpointer 或接受 workers=1。

#### 为什么问这个 / 背景

SQLite 适合单节点 demo，生产 horizontal scale 时必须正视 checkpoint 瓶颈。

#### 详细解释

风险矩阵：

| 风险 | 机制 | 症状 |
|------|------|------|
| 写锁串行 | SQLite 单 writer | P99 延迟尖刺，`database is locked` |
| 多 worker 同文件 | 4 进程共享 `checkpoints.db` | 并发对话越多越卡 |
| NFS/网络盘 | file locking 不可靠 | 损坏、checkpoint 丢失 |
| 备份分裂 | MySQL 与 SQLite 独立备份 | 恢复后会话内容对不上 metadata |
| 跨机 session | 文件本地 | LB 换机后 thread 不存在 |

演进方向：

```
Phase 1: workers=1 或 guest 已默认 1
Phase 2: LangGraph AsyncPostgresSaver
Phase 3: checkpoint 与 app 分离，共享 PG 集群
```

#### 在本项目中的实现

- 初始化：`backend/api/http_server.py` — `AsyncSqliteSaver`
- 访客独立文件：`guest_checkpoints.db`
- 清理：`conversations.py` 直接 SQL DELETE checkpoint 表

#### 走一遍具体例子

促销期间 4 worker × 200 并发 SSE：

1. checkpoint 写入 QPS 高
2. SQLite busy，Agent `astream` 卡顿
3. 临时方案：降 workers=1；长期：迁 PG

#### 常见误区

- ❌ 「SQLite 很快所以没问题」——**写并发**才是瓶颈
- ❌ checkpoint 放 NFS 共享——**官方不推荐**
- ✅ 锁 + 备份 + 扩展三风险

#### 延伸追问（L2/L3 only）

- 如何在线迁移 SQLite → PostgreSQL checkpoint？
- thread_id 全局唯一是否足够？

---

### Q12：Redis 挂了认证会怎样？

**一句话结论：** Bearer Session **快路径完全失效**——`get_current_user` Redis miss 直接 401；无 DB fallback；API Key 路径仍查 MySQL，部分脚本可用；整体仍属「Redis 必需」架构。

#### 为什么问这个 / 背景

SRE 面试爱问依赖故障 blast radius。要能提出降级方案即使当前未实现。

#### 详细解释

故障影响面：

```
Redis DOWN
  ├─ Session 用户 (Web)     → 401 几乎全部失败
  ├─ Middleware 日志上下文   → 无 user_id，服务仍运行
  ├─ API Key 用户           → 仍可用（MySQL）
  ├─ LightRAG KV            → 检索变慢或失败（另一维度）
  └─ 模型热更新订阅          → 不更新，旧配置继续
```

`get_current_user` 逻辑：

```300:302:backend/api/routers/auth.py
    token_data_str = await redis_client.get(f"auth:token:{token}")
    if not token_data_str:
        raise HTTPException(status_code=401, detail="无效的认证令牌或已过期")
```

**无**「回源 MySQL session 表」fallback（当前实现）。

改进建议（面试加分）：

- MySQL `sessions` 表 + Redis 作 cache aside
- Redis Sentinel/Cluster
- 健康检查失败告警 + 只读维护页

#### 在本项目中的实现

- Session 读：`auth.py` `get_current_user`
- Token 写：`setex` on login
- Redis：`backend/db/redis.py`

#### 走一遍具体例子

Redis 重启 30s：

1. 所有前端用户 401，需重新登录
2. 后台 API Key 集成正常
3. 运维应优先恢复 Redis 而非扩容 API

#### 常见误区

- ❌ API Key 也依赖 Redis——**不依赖**
- ❌ Middleware 会挡所有请求——**仅日志**
- ✅ blast radius + 改进方案

#### 延伸追问（L2/L3 only）

- 如何实现 session 双写 MySQL？
- Redis 与 MySQL token 一致性如何保证 logout？

---

### Q13：API Key 与 User Token 混用 Bearer 头如何区分？

**一句话结论：** `verify_api_key` 顺序为：有 `X-API-Key` 则只当 Key；否则 Bearer **先** `_resolve_bearer_token_auth`（Redis Session），**再** `_resolve_api_key_auth(bearer)` 兼容旧客户端；冲突风险在于 Session miss 后误把短 token 当 Key 查 DB。

#### 为什么问这个 / 背景

Header 设计历史包袱。要说清顺序 + 生产应用前缀约定。

#### 详细解释

解析顺序（`verify_api_key`）：

```
1. X-API-Key 头非空?
     YES → 仅 API Key 校验
     NO  → 继续
2. Authorization Bearer 非空?
     YES → Redis Session (_resolve_bearer_token_auth)
           失败 → MySQL API Key (_resolve_api_key_auth(bearer))
     NO  → 401 缺少凭据
```

Middleware（日志用）顺序类似，但 Redis miss 后会尝试 API Key DB 查。

冲突场景：

| 场景 | 结果 |
|------|------|
| 合法 Session token | Redis 命中，OK |
| 过期 Session | Redis miss → 尝试当 API Key → 通常 miss → 401 |
| 旧客户端 Bearer 放 API Key | Redis miss → API Key 命中，OK |
| Key 字符串像 UUID 且与 token 格式重叠 | 理论上误解析，应用 `sess_` / `sk-` 前缀区分 |

`get_current_user` **不**走 API Key fallback——仅 Redis Session，行为更严格。

#### 在本项目中的实现

- 统一解析：`backend/api/dependencies/auth.py`
- Web 会话：`backend/api/routers/auth.py` — `get_current_user`
- Middleware：`backend/api/http_server.py`

#### 走一遍具体例子

集成方误把 API Key 放在 Bearer 且无 X-API-Key：

1. Redis miss
2. `_resolve_api_key_auth(bearer)` 命中
3. `verify_api_key` 成功——**兼容路径生效**

前端 Session 调 v2 agent（`get_current_user`）：

1. 仅 Redis
2. Key 放 Bearer 不生效 → 401

#### 常见误区

- ❌ 所有路由解析顺序相同——**verify vs get_current_user 不同**
- ❌ X-API-Key 与 Bearer 同时传——**X-API-Key 优先**
- ✅ 顺序表 + 前缀约定

#### 延伸追问（L2/L3 only）

- OAuth2 迁移如何统一？
- 如何 audit 仍用 Bearer-API-Key 的客户端？

---

### Q14：访客与主站用户数据是否隔离？

**一句话结论：** **身份**可共用 MySQL `users`；**会话状态**物理隔离（不同 checkpoint 文件）；**conversation 表**若共用则靠 `user_id` 隔离；**安全关键**是 8083 不暴露管理 API 且应用防火墙限制。

#### 为什么问这个 / 背景

数据隔离题易混淆「用户账号」与「对话状态」。双服务架构必须讲清边界。

#### 详细解释

隔离维度：

| 维度 | 主站 8082 | 访客 8083 | 隔离方式 |
|------|-----------|-----------|----------|
| users 表 | 读写 | 登录读写 | 共用，role/来源区分 |
| conversations | 有 | 有（guest_conversations） | user_id 过滤 |
| checkpoint | checkpoints.db | guest_checkpoints.db | **文件级** |
| KB 管理 | 有 | 无 | 路由不存在 |
| admin API | 有 | 无 | 攻击面 |

```text
┌───────────────────────────────────────────────────────────────┐
│                           主站 :8082                            │
│ checkpoints.db  │  完整 v1 + v2                                 │
└───────────────────────────────────────────────────────────────┘
                                │
                               ▼
┌───────────────────────────────────────────────────────────────┐
│                           访客 :8083                            │
│ guest_checkpoints.db  │  登录 + 问答 + 会话                         │
└───────────────────────────────────────────────────────────────┘

连接:
  Main ──────► (MySQL users / conversations)
  Guest ──────► (MySQL users / conversations)

```

```
Guest User (user_id=G1, thread=T1)
  → guest_checkpoints.db :: T1

Main User (user_id=G1, thread=T1)   # 同 user_id 不同服务
  → checkpoints.db :: T1              # 不同存储，内容独立
```

网络安全：

- 8083 仅 CDN/公网
- 8082 内网/VPN
- 即使 thread_id 猜测，跨服务也读不到 checkpoint

#### 在本项目中的实现

- Guest 服务：`backend/api/guest_http_server.py`
- Guest 会话：`backend/api/routers/guest_conversations.py`
- 环境标记：`GUEST_API_SERVICE=1`

#### 走一遍具体例子

访客注册账号后在 8083 聊 10 轮：

1. metadata 写入 MySQL（若 guest 登录创建 user）
2. 消息在 `guest_checkpoints.db`
3. 同一 email 登录主站 8082 → **看不到** guest checkpoint 历史（除非产品做迁移工具）

#### 常见误区

- ❌ 双服务 = 双 MySQL——**通常单库**
- ❌ thread_id 全局唯一即可跨服务——**checkpoint 文件不同**
- ✅ 账号/元数据/状态 三层隔离

#### 延伸追问（L2/L3 only）

- 如何实现 guest → 正式用户会话迁移？
- guest 默认 KB 范围见 `GUEST_KB_IDS`

---

### Q15：internal_api_secret 保护什么？

**一句话结论：** `INTERNAL_API_SECRET` 保护 **`/retrieve_internal` 等内部 RAG 接口**，供子智能体线程通过 `X-Internal-Secret` 调用，防止外网绕过用户鉴权直接打高成本检索。

#### 为什么问这个 / 背景

这是**服务间信任边界**，不是用户登录。子智能体在独立线程，需 HTTP 回调主服务已预热的 RAG 缓存。

#### 详细解释

Threat model：

```
攻击者 ──POST /retrieve_internal──→ 高成本 Milvus+Graph 检索
         若无 secret → 资源耗尽/数据爬取

合法子智能体 ──Header: X-Internal-Secret──→ 与 settings.internal_api_secret 比对
```

校验逻辑：

```882:891:backend/api/routers/rag.py
    expected_secret = app_settings.internal_api_secret
    if expected_secret:
        if not x_internal_secret or x_internal_secret != expected_secret:
            logger.warning("[内部检索] 拒绝未授权访问: secret 不匹配")
            raise HTTPException(status_code=403, detail="Forbidden: invalid internal secret")
    else:
        logger.warning(
            "[内部检索] internal_api_secret 未配置，内部接口存在暴露风险！"
            "请在 .env 中设置 INTERNAL_API_SECRET"
        )
```

与 API Key 区别：

| | API Key | Internal Secret |
|--|---------|-----------------|
| 身份 | 用户/集成方 | 服务内部 |
| Header | X-API-Key | X-Internal-Secret |
| 轮换 |  per user | 全集群同步 |
| 暴露面 | 可给第三方 | 仅 worker/agent 配置 |

未配置 secret 时：**只打 warning，不阻断**——生产必须配置。

#### 在本项目中的实现

- Settings：`backend/config/settings.py` — `internal_api_secret`
- 路由：`backend/api/routers/rag.py` — `rag_retrieve_internal`
- 调用方：子智能体 HTTP 客户端（application/tools/ 工具）

#### 走一遍具体例子

部署新 worker 节点忘记配 `INTERNAL_API_SECRET`：

1. 子智能体调 `/retrieve_internal` → 403
2. 深度研究检索失败，日志「secret 不匹配」
3. 运维在 `.env` 统一 secret 并滚动重启 API + worker

secret 轮换：

1. 生成新 secret
2. 同时更新 API 与所有 subagent 配置
3. 旧 secret 立即失效——**需协调，无 grace period**

#### 常见误区

- ❌ internal secret 可给前端——**仅服务端**
- ❌ 与 Bearer Token 互换——**不同 Header 语义**
- ❌ 未配置=安全——**默认 warn 且可能开放**

#### 延伸追问（L2/L3 only）

- 为何子智能体不直接 import RAG 而走 HTTP？
- 如何用 mTLS 替代 shared secret？

---

## L2 补充 🟡

### Q16：会话删除时级联清理哪些资源？

**一句话结论：** `DELETE /api/v2/conversations/{thread_id}` 做 **逻辑删 MySQL + 物理删 SQLite checkpoint + MinIO 线程前缀 + 沙箱目录**。

```text
┌──────────────────────────────────────┐
│ DELETE thread                        │
└──────────────────────────────────────┘
           │
           ▼
┌──────────────────────────────────────┐
│ M                                    │
└──────────────────────────────────────┘
           │
           ▼
┌──────────────────────────────────────┐
│ CP                                   │
└──────────────────────────────────────┘
           │
           ▼
┌──────────────────────────────────────┐
│ MINIO                                │
└──────────────────────────────────────┘
           │
           ▼
┌──────────────────────────────────────┐
│ delete_thread_dir 沙箱                 │
└──────────────────────────────────────┘

```

**实现：** `api/routers/conversations.py`

---

### Q17：`POST .../rollback` 会话回滚如何实现？

**一句话结论：** 按 `keep_user_turns` 保留前 N 个用户轮，从 checkpointer 截断 `messages` 并写回，使 Agent「忘记」后续对话。

#### 走一遍具体例子

用户第 3 轮发现回答跑偏 → rollback `keep_user_turns=2` → 历史只剩前 2 轮用户消息及对应 AI → 第 3 轮重新提问。

---

### Q18：短信认证（SMS）与密码登录如何共存？

**一句话结论：** `api/routers/auth_sms.py` 提供验证码注册/忘记密码；签发 Token 后仍走 **Redis `auth:token:{token}`** 快路径，与 CAS/密码登录共享会话体系。

| 流程 | 入口 |
|------|------|
| 密码/CAS | auth.py |
| 短信验证码 | auth_sms.py |
| 校验 | 统一 verify_api_key / get_current_user |
