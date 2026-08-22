# 移动端 + 网页端（访客/登录）前端实施清单

> **实施状态**（2026-07-14）：
> - ✅ Sprint 1：`/app`、`/m` 路由、`useUiMode`、`useDeviceProfile`（`frontend/`）
> - ✅ Sprint 2：RadAI 登录用户 SSE（`/api/v2/agent-runtime/query`）、`AbortSignal` 停止当前 SSE；`deep_research`/`plan_solve` 通过 `cancelTask` 和后台 task stream 支持续接与取消，普通工作流直接消费当前 SSE
> - ✅ 主前端内访客模式：未登录访问 `/app`/`/m` 时走 `/api/v2/guest/query`，历史保存在 IndexedDB
> - ✅ 访客独立工程：`guest/`（`:8085`）复用 `frontend/src` 核心组件；支持账号登录、注册、短信登录，默认代理 `:8083` `/api/v2/guest/query`（见 [访客独立API端点实施方案](../访客独立API端点实施方案.md)）
> - ✅ 探索式学习：`/explore`、`/m/explore`
> - ⚠️ 未做：登录后会话从 IndexedDB 迁移到 MySQL；独立 guest 工程与主前端内访客模式仍是两条入口链路

## 1. 文档目标

本清单最初用于将“自动识别设备 + 手动切换 + 访客与登录双模式”方案落地到开发。当前代码已完成主要功能，因此本文同时记录现状和仍可回归的检查项。

## 1.1 当前实现基线

| 应用 | 入口 | 实现现状 |
| :--- | :--- | :--- |
| RadAI 主前端 | `frontend/`，5173 | Vue Router；`/app` 与 `/m` 共用 `views/ne-full/index.vue`；未登录自动进入主前端访客模式 |
| CSH-AI 前端 | `frontend_csh_ai/`，5174 | 路由结构与主前端相近，额外提供 `/groundwater/:threadId?` 与 `/admin/groundwater-projects` |
| Guest 独立前端 | `guest/`，8085 | 无 Vue Router；`App.vue` 内管理登录、历史、聊天；alias `@` 指向 `../frontend/src` 复用 `useAgentChat`、`MessageList`、`request` 等 |

当前访客身份分两层：

| 场景 | 身份与存储 |
| :--- | :--- |
| 主前端未登录访问 `/app`/`/m` | `useAgentChat.isGuestMode=true`，请求 `/api/v2/guest/query`，会话落 IndexedDB |
| `guest/` 独立工程 | 先展示 `GuestLogin.vue`；支持账号、短信注册、短信登录。认证后优先走登录态 conversations API；无 token 的 fallback 服务仍保留 `X-Device-ID` 与 IndexedDB |

## 2. 总体实施原则

- 主前端一套工程，两个体验层：移动布局（/m）与桌面布局（/app）。
- 业务逻辑统一复用：消息流、状态管理、接口协议尽量共用。
- 自动识别仅作默认入口，用户手动选择优先级最高。
- 主前端访客对话主存储在浏览器 IndexedDB，服务端只提供对话能力与短期上下文。
- 登录用户沿用现有用户体系与会话能力。

## 3. 路由与模式设计

### 3.1 路由分区

- 桌面端：/app/*
- 移动端：/m/*
- 公共入口：/

### 3.2 模式优先级

1. 用户显式选择（localStorage: ui_mode=mobile|desktop）
2. 自动识别（UA + 断点宽度）
3. 默认回退（desktop）

### 3.3 身份模式

- guest：主前端内访客模式（无需登录）或独立 guest 工程登录前 fallback 模式
- user：登录模式（持 token）

## 4. Sprint 拆解（可直接进入迭代）

## Sprint 1：基础骨架与切换能力

### 开发项

- 新增布局壳组件
  - MobileShell（顶部栏 + 底部导航 + 安全区）
  - DesktopShell（侧栏 + 顶栏）
- 新增设备识别工具
  - useDeviceProfile（ua、width、touch 能力）
- 新增模式管理
  - useUiMode（auto/mobile/desktop）
- 新增入口路由守卫
  - 按优先级重定向到 /m 或 /app
- 页面提供“切换到桌面版/手机版”按钮

### 验收标准

- 首次访问自动进入对应端布局。
- 手动切换后刷新页面仍保持选择。
- 横竖屏切换不导致无限重定向。

### 测试清单

- 路由守卫单测：auto/mobile/desktop 三种模式。
- 浏览器兼容手测：Chrome、Edge、手机浏览器。

## Sprint 2：统一聊天域模型与接口适配

### 开发项

- 抽象统一 ChatDomain
  - Conversation、Message、ThreadState、SendPayload
- 抽象 ChatServiceAdapter
  - GuestAdapter -> /api/v2/guest/query
  - UserAdapter -> /api/v2/agent-runtime/query
- 统一 SSE 事件解析层
  - chunk/status/event/error/done
- 统一消息渲染组件接口
  - MessageList、ChatInput、TypingStatus

### 验收标准

- guest 与 user 模式均可完成流式问答。
- 两种模式下前端渲染逻辑完全复用。
- 接口异常可显示统一错误提示。

### 测试清单

- SSE 解析单测（粘包、空包、error 包）。
- Adapter 单测（参数映射、header 注入）。

## Sprint 3：访客本地存储（IndexedDB）与上下文连续

### 开发项

- 新建 GuestConversationStore（IndexedDB）
  - 表：conversations、messages、settings
- 生成并持久化匿名 device_id
- 访客 thread_id 策略
  - 新建会话时生成
  - 继续会话时复用
- 历史会话管理
  - 新建、重命名、删除、清空
- 会话导入导出
  - JSON 导出
  - JSON 导入（去重合并）

### 验收标准

- 刷新后访客会话和消息完整恢复。
- 同一 thread_id 可连续多轮问答。
- 删除会话仅影响本地，不影响服务端用户会话。

### 测试清单

- IndexedDB CRUD 单测。
- 大数据量压测（例如 200+ 会话、单会话 500+ 消息）。

## Sprint 4：登录模式融合与账号切换

### 开发项

- 登录态检查与切换
  - guest -> user（登录后迁移策略）
  - user -> guest（登出后隔离策略）
- 会话迁移策略（建议可配置）
  - 方案 A：不迁移，保持隔离
  - 方案 B：用户确认后迁移部分会话
- 用户模式会话侧栏
  - 拉取服务端会话列表
  - 切换加载历史

### 验收标准

- 登录/登出流程稳定，不串数据。
- guest 与 user 的会话列表严格隔离。
- 切换账号后不会读取上一个账号会话。

### 测试清单

- 鉴权过期回归测试。
- 多账号切换回归测试。

## Sprint 5：移动端体验优化与发布准备

### 开发项

- 移动端输入体验
  - 键盘弹起视口适配
  - 输入框跟随与防遮挡
- 手势与交互
  - 左滑会话操作（删除/重命名）
  - 抽屉开合动画
- 性能优化
  - 消息虚拟列表
  - 长文本懒渲染
- 可观测性
  - 前端埋点（首屏、发送耗时、错误率）

### 验收标准

- 主流手机浏览器输入不抖动、不卡顿。
- 长会话滚动稳定，帧率可接受。
- 发布前关键路径无 P1/P2 缺陷。

### 测试清单

- 真机测试（Android/iOS 各 2 台以上）。
- Lighthouse 与 Web Vitals 指标达标。

## 5. 当前代码结构

```text
frontend/src/
  router/index.ts                         # /app、/m、公开学习页、settings、admin 守卫
  views/ne-full/index.vue                 # 桌面/移动专家模式共用入口
  views/ne-full/composables/useAgentChat.ts
  services/storage/guestIndexedDb.ts      # 主前端访客历史
  composables/useUiMode.ts
  composables/useDeviceProfile.ts

guest/src/
  App.vue                                 # 独立访客应用状态机
  components/GuestLogin.vue               # 账号、注册、短信登录
  components/GuestChatBox.vue
  components/GuestHistorySidebar.vue
  services/guestChatService.js            # fallback 访客请求 + device_id
```

## 6. API 协议约定（前端侧）

### 6.1 访客请求

- endpoint: /api/v2/guest/query
- headers:
  - X-Device-ID: <local_device_id>
- body:
  - question
  - thread_id
  - model（可选）
  - kb_ids（可选）
  - use_search=false
  - selected_skills=[]
  - subagent_enabled=false
  - thinking=false
  - mode=lightweight

### 6.2 用户请求

- endpoint: /api/v2/agent-runtime/query
- headers:
  - Authorization: Bearer <token>
- body:
  - agent_code
  - question
  - thread_id
  - model（可选）
  - use_search（可选）
  - kb_ids（可选）
  - selected_skills（可选）
  - metadata.file_ids（对话附件）

## 7. 风险与预案

- 设备识别误判
  - 预案：始终允许手动切换并持久化。
- 访客本地数据过大
  - 预案：会话上限、分页加载、导出后清理。
- SSE 中断导致消息不完整
  - 预案：消息状态机 + 重试按钮 + 失败标识。
- 登录与访客数据串扰
  - 预案：存储命名空间隔离（guest/user 前缀）。

## 8. 发布前检查（Go-Live Checklist）

- 路由切换与手动覆盖策略验证完成。
- guest 与 user 两条链路回归通过。
- IndexedDB 数据迁移脚本（若有）验证通过。
- 关键页面移动端真机验证通过。
- 监控与告警（前端错误、接口失败率）已接入。

## 9. 里程碑建议

- M1（1 周）：完成 Sprint 1 + Sprint 2
- M2（1 周）：完成 Sprint 3
- M3（1 周）：完成 Sprint 4 + Sprint 5 核心项
- M4（0.5 周）：联调、压测、灰度发布

---

该清单可直接作为开发任务拆解依据，按 Sprint 建 issue 后即可进入实现阶段。
