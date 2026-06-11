# 认证实现 vs 规划文档对齐审计

> 对照：  
> - 规划：[plans/认证方案.md](../认证方案.md)（长期愿景）  
> - 实现：[docs/docs_backend/auth_module.md](../../docs/docs_backend/auth_module.md)  
> - 代码：`api/routers/auth.py`、`auth_sms.py`、`api/dependencies/auth.py`、`http_server.py` 中间件

---

## 1. 文档层级（避免误读）

| 文档 | 定位 |
|------|------|
| `plans/认证方案.md` | **规划** — 文首已标注「非现行运维手册」✅ |
| `docs/docs_backend/auth_module.md` | **现行** auth.py 说明 |
| `docs/docs_backend/sms_auth_module.md` | 短信子模块 |

审查结论：**规划文档本身已做区分**；风险在于研发仍按规划实现 HMAC/MFA。

---

## 2. 规划 vs 实现矩阵（扩展）

| 能力 | 规划方案 | 代码实现 | 对齐 | 备注 |
|------|----------|----------|------|------|
| Web 登录 | JWT + refresh | Redis Bearer 24h | 部分 | **无 refresh** |
| API 调用 | HMAC 签名 + nonce | `verify_api_key` 明文 Key | 否 | 规划 §2.1 |
| 统一认证网关 | 独立网关服务 | 合并在 `http_server` 中间件 | 否 | |
| MFA / mTLS | 支持 | 未实现 | 否 | |
| 短信验证码 | 网关抽象 + Redis | 进程 dict + 硬编码密钥 | 否 | **P0** auth_sms |
| Token 存储 | — | `auth:token:{token}` JSON | 是 | |
| CAS | — | `settings.cas_*` 可选 | 超出规划 | 已落地 |
| API Key | DB `ApiKey` 表 | `dependencies/auth.py` | 是 | 无签名 |
| 中间件认证 | AuthMiddleware | `user_context_middleware` 可选注入 | 部分 | 未登录可访问多数路由 |
| MCP | mTLS | `/mcp/sse` + API Key | 部分 | |
| 权限 RBAC | 细粒度 | `User.role` admin/user | 部分 | 路由层分散校验 |
| 审计日志 | 集中 | 分散 logger | 部分 | |

---

## 3. `auth.py` 实现要点

### 3.1 函数/端点地图

| 符号 | 职责 |
|------|------|
| `verify_password` / `hash_password` | werkzeug |
| `generate_token` | `secrets.token_urlsafe(32)` |
| `_issue_auth_token` | Redis SETEX 24h |
| `get_current_user` | Bearer 或 `?token=` |
| `POST /login` | 用户名密码 |
| `GET/POST /cas/*` | CAS 2.0 校验 |
| `POST /logout` | DEL Redis key |
| `GET /verify` / `GET /me` | 会话校验 |
| `POST /change-password` | 需登录 |

### 3.2 质量

| ID | 严重度 | 问题 |
|----|--------|------|
| AU-1 | medium | `verify_password` 宽泛 `except` → False |
| AU-2 | low | 登录失败与成功均返回 200 + `success:false` — 非 401 |
| AU-3 | ok | CAS `_is_safe_redirect_target` 防开放重定向 |
| AU-4 | info | Token 24h 固定 `TOKEN_EXPIRE_HOURS` — 无滑动续期 |

---

## 4. `auth_sms.py`（见 per_file 审查 P0）

| 问题 | 与规划差距 |
|------|------------|
| `SMS_SECRET` 源码硬编码 | 规划要求密钥管理 |
| 验证码内存 dict | 规划要求 Redis + TTL |
| `print` 日志 | 非生产可观测性 |

---

## 5. `dependencies/auth.py` vs 中间件双路径

| 路径 | 行为 |
|------|------|
| `Depends(get_current_user)` | 路由级强制登录 |
| `Depends(verify_api_key)` | RAG/程序化 |
| `user_context_middleware` | 可选解析 Bearer/X-API-Key，**失败静默** |

**风险 AU-5**：同一 Token 在中间件与 Depends 行为不一致；排障困难。

---

## 6. `http_server` 与规划「网关」差异

规划设想独立 AuthMiddleware 统一鉴权；现行：

- CORS 硬编码
- 用户上下文中间件（非阻断）
- 各路由自行 Depends

**优点**：灵活；**缺点**：易漏保护端点（需安全审计清单）。

---

## 7. 与 Agent V2 的关系

| 端点 | 认证 |
|------|------|
| `/api/v2/agent/*` | `get_current_user` |
| `/api/v2/guest/*` | 访客 device id |
| `/api/v2/csh_agent/*` | 同 agent |
| `/api/v1/rag/*` | 多 `verify_api_key` |

规划中的「统一网关」未覆盖 V1/V2 前缀分裂。

---

## 8. 建议

### 文档

1. 在 `plans/认证方案.md` 顶部增加「**勿实现签名章节直至产品立项**」横幅（已有速查表，可加强）。  
2. 新建 `plans/debug/auth_gap_tracker.md` 跟踪 AU/CF 项（可选）。

### 代码（未来）

| 优先级 | 项 |
|--------|-----|
| P0 | auth_sms 密钥与 Redis |
| P1 | get_file_meta 归属校验 |
| P2 | 评估 API Key HMAC（破坏性） |
| P3 | refresh token 或滑动过期 |

---

## 9. 结论

- **现行 auth 以 Redis Session Token + API Key 为主**，与规划「统一网关 + HMAC」** intentionally 未对齐**。  
- **CAS 为超额实现**（好事）。  
- **短信模块是当前认证栈最大短板**（与规划目标差距大且不安全）。  
- 阅读认证相关需求时，**以 `auth_module.md` + 本审计为准**，`plans/认证方案.md` 仅作路线图。
