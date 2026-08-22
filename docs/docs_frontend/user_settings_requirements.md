# 前端用户设置需求文档

> 状态：已实现  
> 适用范围：`frontend/` 主前端；`frontend_csh_ai/` 已同步设置页路由、视图和接口封装  
> 最后同步：2026-07-09；当前主前端与 CSH-AI 前端均已补齐设置页路由、用户自助设置接口和前端入口。

## 1. 背景与现状

当前前端已具备用户登录、Token 验证、用户信息本地持久化、个人文件库、学习风格偏好和统一“用户设置”入口。

现状代码基线：

| 模块 | 当前状态 |
| :--- | :--- |
| `frontend/src/components/NavRail.vue` | 设置按钮已绑定跳转；根据当前模式进入 `/settings` 或移动端设置入口 |
| `frontend_csh_ai/src/components/NavRail.vue` | 已有设置入口，进入 `/settings` 或 `/m/settings` |
| `frontend/src/router/index.ts` | 已有 `/settings`、`/m/settings`，均设置 `meta.requiresAuth=true` |
| `frontend_csh_ai/src/router/index.ts` | 已有 `/settings`、`/m/settings`，均设置 `meta.requiresAuth=true` |
| `frontend/src/utils/request.ts` | 已维护 `authState.token`、`authState.user`，并持久化 `auth_token`、`user_info` |
| `frontend/src/api/auth.ts` | 已有登录、退出、Token 验证、CAS 登录 URL 构造 |
| `backend/api/routers/auth.py` | 登录、`GET/PATCH /auth/me`、`POST /auth/me/change-password` 均返回/维护 `display_name`、`email`、`role`、`is_active` 等基础资料 |
| `backend/api/routers/admin.py` | 管理员仍可管理用户、API Key、审计和任务；普通用户设置不复用管理员接口 |
| `frontend/src/composables/useUiMode.ts` | 已支持 `auto`、`mobile`、`desktop` 展示模式，并写入 `localStorage.ui_mode` |
| `frontend/src/composables/useThemePreference.ts` | 已支持 `system`、`light`、`dark`，并写入 `localStorage.theme` |
| `frontend/src/api/user-settings.ts`、`frontend_csh_ai/src/api/user-settings.ts` | 已封装 `me/updateMe/changePassword` |

## 2. 目标

提供普通用户可访问的“用户设置”页面，让用户可以查看账号信息、修改基础资料、管理密码和配置常用前端偏好，并让现有设置按钮具备明确可用的跳转行为。

核心目标：

1. 设置按钮点击后进入用户设置页面。
2. 用户可以查看当前登录账号、角色、邮箱、创建时间、最近登录时间。
3. 用户可以自助修改邮箱和显示名。
4. 密码登录用户可以修改密码。
5. 用户可以配置界面展示模式、主题、学习风格是否应用到 AI。
6. 未登录用户点击设置时，引导登录；登录后回到设置页。
7. 移动端提供等价入口与响应式设置页。

## 2.1 当前实现摘要

- 页面文件：`frontend/src/views/settings/index.vue`。
- API 文件：`frontend/src/api/user-settings.ts`。
- 路由：`/settings`、`/m/settings`，均要求登录。
- 页面分区：账号信息、安全设置、偏好设置。
- 账号信息：展示用户名、显示名、邮箱、角色、状态、登录来源、创建时间、最近登录；支持编辑显示名和邮箱。
- 安全设置：密码账号可改密；CAS/无本地密码账号隐藏本地改密表单。
- 偏好设置：本地保存 UI 模式和主题；学习风格“应用到 AI”复用 `/learning-style/profile/preferences`。
- 资料保存成功后调用 `setAuth(authState.value.token || '', response.user)` 同步本地登录态。

## 3. 非目标

本期不实现以下能力：

| 能力 | 说明 |
| :--- | :--- |
| 管理员用户管理 | 已在 `/admin/system` 实现，不并入普通用户设置 |
| API Key 自助管理 | 可作为后续“开发者设置”扩展，本期不开放 |
| 账号注销/删除 | 涉及数据清理、审计与合规策略，后续单独设计 |
| CAS 账号解绑 | CAS 用户身份来源由统一认证系统管理，本期只展示登录来源 |
| 头像上传 | 当前 `users` 表无头像字段，避免引入文件存储和裁剪流程 |

## 4. 用户角色与权限

| 用户类型 | 访问设置页 | 可编辑资料 | 可改密码 | 说明 |
| :--- | :--- | :--- | :--- | :--- |
| 未登录用户 | 否 | 否 | 否 | 访问 `/settings` 时跳转 `/login?redirect=/settings` |
| 普通密码用户 | 是 | 是 | 是 | 可修改邮箱、显示名、密码和前端偏好 |
| CAS 用户 | 是 | 部分 | 否 | 可修改本地偏好；账号名由 CAS 同步，不允许在本系统改密码 |
| 管理员用户 | 是 | 是 | 是 | 用户设置只处理个人设置；管理员能力仍在 `/admin/*` |

## 5. 信息架构

建议页面采用三段式结构，桌面端为左侧设置导航 + 右侧内容，移动端为分组列表 + 单页滚动。

| 分组 | 功能 | 优先级 |
| :--- | :--- | :--- |
| 账号信息 | 用户名、显示名、邮箱、角色、账号状态、创建时间、最近登录 | P0 |
| 安全设置 | 修改密码、退出登录、登录来源提示 | P0 |
| 偏好设置 | 主题、界面模式、学习风格应用到 AI | P0 |
| 数据与隐私 | 会话、个人文件入口；数据删除说明 | P1 |
| 关于系统 | 前端版本、后端连通状态、帮助文档入口 | P2 |

## 6. 页面与交互需求

### 6.1 入口

| 端 | 需求 |
| :--- | :--- |
| 桌面端 | `NavRail.vue` 底部“设置”按钮点击跳转 `/settings` |
| 移动端 | 底部导航需要增加“设置”入口，或在用户头像/更多菜单中提供“设置”入口；MVP 推荐增加 `/m/settings` 路由但不占用底部主导航超过 5 个入口 |
| 未登录 | 点击设置跳转 `/login?redirect=/settings`，登录成功后回到设置页 |

### 6.2 账号信息

展示字段：

| 字段 | 来源 | 是否可编辑 |
| :--- | :--- | :--- |
| 用户 ID | `authState.user.user_id` / `GET /auth/me` | 否 |
| 用户名 | `username` | 否 |
| 显示名 | `display_name` | 是 |
| 邮箱 | `email` | 是 |
| 角色 | `role` | 否 |
| 账号状态 | `is_active` | 否 |
| 创建时间 | `created_at` | 否 |
| 最近登录 | `last_login` | 否 |
| 登录来源 | 后端按 `user.password` 推断：有本地密码为 `password`，否则为 `cas` | 否 |

交互规则：

1. 进入页面时优先调用 `GET /auth/me` 获取最新资料，避免只依赖 `localStorage.user_info`。
2. 显示名为空时回退展示 `username`。
3. 邮箱保存前做前端格式校验；后端必须做唯一性校验。
4. 保存成功后同步更新 `authState.user` 和 `localStorage.user_info`。

### 6.3 安全设置

修改密码表单字段：

| 字段 | 规则 |
| :--- | :--- |
| 当前密码 | 必填 |
| 新密码 | 必填，至少 6 位；后端可扩展强度规则 |
| 确认新密码 | 必须与新密码一致 |

交互规则：

1. 密码修改成功后提示用户重新登录，清空当前 Token 并跳转 `/login`。
2. CAS 用户隐藏修改密码表单，展示“请在统一认证系统中修改密码”。
3. 连续失败时展示通用错误，不暴露账号是否存在等敏感信息。
4. 退出登录按钮调用前端 `logout()`；可选调用 `POST /auth/logout` 清理后端 Token。

### 6.4 偏好设置

MVP 偏好项：

| 偏好 | 当前基础 | 存储建议 | 说明 |
| :--- | :--- | :--- | :--- |
| 界面模式 | `useUiMode.ts` | 前端 `localStorage.ui_mode`，后续同步后端 | `auto` / `desktop` / `mobile` |
| 主题 | 管理后台已有 `localStorage.theme` 模式 | 前端 `localStorage.theme`，后续同步后端 | `system` / `light` / `dark` |
| 学习风格应用到 AI | `GET/PATCH /learning-style/profile/preferences` | 后端已有 | 设置页中复用已有接口 |

保存规则：

1. 本地偏好应立即生效。
2. 后端偏好接口失败时保留原值，并提示保存失败。
3. 偏好更新不应刷新页面或打断当前会话。

## 7. 路由需求

新增路由：

| 路径 | 组件 | 权限 | 标题 |
| :--- | :--- | :--- | :--- |
| `/settings` | `views/settings/index.vue` | 需登录 | 用户设置 |
| `/m/settings` | `views/settings/index.vue` | 需登录 | 用户设置 |

路由守卫：

1. `meta.requiresAuth = true`。
2. 未登录访问时跳转 `/login?redirect=/settings` 或 `/login?redirect=/m/settings`。
3. 已登录访问 `/login` 时，如果存在 `redirect` 且为站内安全路径，应优先回跳该路径。

## 8. 接口需求

### 8.1 获取当前用户资料

```http
GET /api/v1/auth/me
Authorization: Bearer <token>
```

响应：

```json
{
  "user": {
    "user_id": "uuid",
    "username": "zhangsan",
    "display_name": "张三",
    "email": "zhangsan@example.com",
    "role": "user",
    "is_active": true,
    "auth_provider": "password",
    "created_at": "2026-07-05T10:00:00",
    "updated_at": "2026-07-05T10:00:00",
    "last_login": "2026-07-05T11:00:00"
  }
}
```

### 8.2 更新当前用户资料

```http
PATCH /api/v1/auth/me
Authorization: Bearer <token>
Content-Type: application/json
```

请求：

```json
{
  "display_name": "张三",
  "email": "zhangsan@example.com"
}
```

响应同 `GET /auth/me`。

后端校验：

1. `email` 为空时允许清空；非空时必须符合邮箱格式且全局唯一。
2. `display_name` 长度建议 1-50；为空时后端可存 `null`，前端回退 `username`。
3. 不允许普通用户修改 `username`、`role`、`is_active`。

> 当前实现已经读写 `users.display_name`；若旧环境尚未迁移该列，需要先执行对应数据库迁移，否则 `PATCH /auth/me` 的显示名更新会失败。

### 8.3 修改密码

```http
POST /api/v1/auth/me/change-password
Authorization: Bearer <token>
Content-Type: application/json
```

请求：

```json
{
  "current_password": "old-password",
  "new_password": "new-password"
}
```

响应：

```json
{
  "success": true
}
```

后端要求：

1. 校验当前密码正确。
2. 新密码写入前必须哈希。
3. 修改成功后建议删除当前用户所有 `auth:token:*`，强制重新登录。
4. `password IS NULL` 的 CAS 用户返回 `400`，提示“当前账号不支持本地密码修改”。

### 8.4 用户偏好

当前偏好分两类：

| 偏好 | 当前接口/存储 | 说明 |
| :--- | :--- | :--- |
| UI 模式 | `localStorage.ui_mode` | `auto` / `desktop` / `mobile`，由 `useUiMode()` 立即生效 |
| 主题 | `localStorage.theme` | `system` / `light` / `dark`，由 `useThemePreference()` 立即生效 |
| 学习风格应用到 AI | `GET/PATCH /api/v1/learning-style/profile/preferences` | 只有已完成测评并生成 profile 后可切换 |

若后续需要跨设备同步，可新增：

```http
GET /api/v1/auth/me/preferences
PATCH /api/v1/auth/me/preferences
```

建议响应：

```json
{
  "preferences": {
    "theme": "system",
    "ui_mode": "auto",
    "apply_learning_style_to_ai": true
  }
}
```

## 9. 前端实现

已新增文件：

```text
frontend/src/views/settings/index.vue
frontend/src/api/user-settings.ts
frontend_csh_ai/src/views/settings/index.vue       # CSH-AI 设置页
frontend_csh_ai/src/api/user-settings.ts
```

已落地改动点：

| 文件 | 改动 |
| :--- | :--- |
| `frontend/src/router/index.ts` | 新增 `/settings`、`/m/settings` 路由，均 `requiresAuth` |
| `frontend/src/components/NavRail.vue` | 设置按钮绑定跳转；移动端路径与当前模式联动 |
| `frontend/src/views/login/UserLogin.vue` | 登录成功后支持安全 `redirect` 回跳 |
| `frontend/src/utils/request.ts` | 保存资料成功后复用 `setAuth(token, user)` 同步本地状态 |
| `frontend/src/composables/useUiMode.ts` | 设置页复用现有模式读写逻辑 |
| `frontend/src/api/learning-style.ts` | 设置页复用 `profile()` 与 `updatePreferences()` |

## 10. 数据模型

当前用户设置依赖 `users` 表中的 `display_name`、`email`、`password`、`role`、`is_active`、`created_at`、`updated_at`、`last_login`。邮箱更新会做格式校验和唯一性校验；显示名最大 50 字符，空字符串会归一化为 `NULL`，展示时回退 `username`。

若希望 UI 模式和主题跨设备同步，建议新增表：

```sql
CREATE TABLE user_preferences (
  user_id VARCHAR(36) PRIMARY KEY,
  theme VARCHAR(20) NOT NULL DEFAULT 'system',
  ui_mode VARCHAR(20) NOT NULL DEFAULT 'auto',
  settings_json JSON NULL,
  created_at DATETIME NOT NULL,
  updated_at DATETIME NOT NULL
);
```

## 11. 验收标准

P0 验收：

1. 桌面端点击左侧“设置”按钮能进入 `/settings`。
2. 移动端能进入 `/m/settings` 或通过“更多/我的”入口进入设置。
3. 未登录访问设置页会跳转登录；登录后回到原设置页。
4. 设置页能展示当前用户资料，刷新页面后仍正确。
5. 邮箱修改成功后，页面展示和 `localStorage.user_info` 同步更新。
6. 密码修改成功后当前登录态失效，用户需要重新登录。
7. 界面模式切换后立即影响 `/app` 与 `/m` 的默认跳转策略。
8. 学习风格“应用到 AI”开关与现有学习风格页面状态一致。
9. 普通用户不能通过接口修改自己的角色、账号状态或其他用户资料。
10. 所有新增接口未登录返回 401，账号禁用返回 403。

## 12. 测试用例

| 场景 | 预期 |
| :--- | :--- |
| 未登录打开 `/settings` | 跳转 `/login?redirect=/settings` |
| 登录后点击设置 | 正常进入设置页 |
| 修改邮箱为非法格式 | 前端阻止提交并提示 |
| 修改邮箱为已存在邮箱 | 后端返回 409，前端展示错误 |
| 修改密码时当前密码错误 | 后端返回 400/401，前端提示修改失败 |
| CAS 用户打开安全设置 | 不显示本地改密表单 |
| 切换 `ui_mode=mobile` | 根路径 `/` 优先进入 `/m` |
| 关闭学习风格应用 | `/learning-style` 页面状态同步为关闭 |
| 管理员打开用户设置 | 只展示个人设置，不出现用户管理表格 |

## 13. 迭代拆分

### Phase 1：最小可用（已完成）

1. 新增设置页与路由。
2. 设置按钮跳转。
3. 展示账号资料。
4. 实现邮箱修改。
5. 实现密码修改。
6. 支持 UI 模式和学习风格偏好。

### Phase 2：体验完善（部分完成）

1. 主题设置支持 `system`。（已完成，本地存储）
2. 移动端设置入口与页面体验优化。（已完成主路由与响应式页面）
3. 设置页增加个人文件、会话历史快捷入口。
4. 后端新增用户偏好表，实现跨设备同步。

### Phase 3：高级账号能力

1. API Key 自助查看与轮换。
2. 登录设备/会话管理。
3. 账号注销与数据导出。
4. 头像上传与个人资料扩展。

## 14. 风险与注意事项

1. `users.email` 已有唯一约束，清空邮箱时需确认数据库允许多个 `NULL`。
2. 当前 Token 存在 Redis 中，若要修改密码后踢下所有端，需要能按 `user_id` 扫描或维护用户 Token 索引。
3. `display_name` 目前不是持久化字段，编辑显示名前必须先补数据模型。
4. 设置页更新 `authState.user` 时不能丢失 `role`、`is_active` 等路由守卫依赖字段。
5. `redirect` 参数必须限制为站内相对路径，避免开放重定向。
