# 核电子学智能助手 - 前端项目文档 (Nuclear Electronics Intelligent Assistant - Frontend)

欢迎使用「核电子学智能助手」前端项目。本项目是基于 Vue 3 构建的专业级 AI 对话助手与智能体管理平台，专为核电子学领域的学术研究与文档处理设计。

> **文档同步（2026-07-25）**：三端已落地 [响应式重设计](./frontend_responsive_redesign_plan.md)——统一 `AppShell` 自适应布局，`/m/*` 仅作旧链接重定向；专家模式对话附件走 `/api/v2/chat-files/upload`；自定义学习 `/custom-learning`、学习风格 `/learning-style`、用户设置 `/settings` 已接入当前 API；管理后台支持入库/检索后端选择；`deep_research`/`plan_solve` 通过可重连 task stream 获取后台进度和最终答案。

> 成理 CSH-AI 前端为独立工程 `frontend_csh_ai/`（端口 5174），复用专家模式、探索式学习、设置、知识库管理等主体结构，并额外提供 `/groundwater/:threadId?` 与 `/admin/groundwater-projects`。FMCA 前端为 `frontend_fmca/`（端口 5176，`agent_code=fmca`，不绑定知识库），导航启用知识问答与探索式学习。访客前端为 `guest/`（端口 8085），通过 Vite alias 复用本工程 `src/` 下的核心组件与 composable；Guest 为 JS 应用、无 Vue Router，不接 `AppShell`。

---

## 🚀 技术栈 (Technology Stack)

| 类别 | 技术 | 版本 | 说明 |
| :--- | :--- | :--- | :--- |
| 核心框架 | Vue 3 | 3.5+ | Composition API + `<script setup>` 语法 |
| 构建工具 | Vite | 8.0+ | 极速 HMR 与生产构建 |
| 样式引擎 | Tailwind CSS | 4.0 | 原子化 CSS，`@tailwindcss/vite` 插件集成 |
| 编程语言 | TypeScript | 6.0 | 严格类型检查 |
| 路由 | Vue Router | 4.6+ | `createWebHistory` 模式，路由守卫鉴权 |
| UI 组件 | Radix Vue | 1.9+ | 无样式基础组件 |
| 图标库 | Lucide Vue Next | 1.0+ | 现代图标集 |
| Markdown | Marked | 17.0+ | 高性能 Markdown 解析 |
| 数学公式 | KaTeX | 0.16+ | 工业级 LaTeX 渲染 |
| 安全过滤 | DOMPurify | 3.3+ | XSS 防护 |
| 工具函数 | clsx + tailwind-merge | - | 条件样式合并 |

---

## ✨ 核心特性 (Key Features)

### 1. 专家模式对话（单一自适应布局）
- **统一入口** (`/app/:threadId?`)：`AppShell` 三列栅格（侧栏 / 主区 / 工件面板），按视口与侧栏状态自适应，不再按 URL 区分桌面/移动。
- 根路径 `/` 重定向到 `/app`。旧路径 `/m/*`、`/chat`、`/ne-full` 仅作兼容重定向。
- 侧栏状态由 `useSidebar` + `localStorage.sidebar_state` 驱动（`<lg` 抽屉，`≥lg` 可并排）。

### 2. V2 Agent 全功能交互
- SSE 流式响应，支持标准 `message.delta` / `reasoning.delta` / `workflow.step` / `plan.created` / `coordination.*` / `follow_up.created` 事件，并兼容旧 `chunk` / `status` / `thinking` / `event` / `follow_up_suggestions` / `draft_chunk`。
- `lightweight`、`full` 和 `multi_agent_*` 直接消费 `/query` SSE；`deep_research` 与 `plan_solve` 的 `/query` 先返回 `task_id`，随后通过 task `status/stream/cancel` 接口续接、查看和取消。
- 推理过程折叠面板 (`ReasoningBlock`)、工具调用徽章 (`ToolBadge`)、子 Agent 工作流时间线。
- 知识库双选 (`KBSelector`)、Skills 选择、深度研究协同计划与进度展示。
- 工件侧栏 (`ArtifactSidebar`)：支持 markdown/html/code/text/file 预览、带认证下载、从会话工件列表恢复。
- 回答后追问建议 (`FollowUpSuggestions`)。
- 对话附件上传 (`useFileUpload` + `FilePreviewChip`)：本地文件批量上传至 `/api/v2/chat-files/upload`，可选择自动解析或 MinerU；个人文件库文件可从弹窗添加并绑定到当前会话。

### 3. 会话与文件
- **侧栏会话列表**（`AppSidebar`）：登录用户对接 `/api/v2/conversations`，支持新建/重命名/删除；访客模式使用 IndexedDB 本地保存、删除和清空。
- **用户登录** (`/login`)：普通用户 Bearer Token 认证。
- **个人文件库** (`/my-files`)：对接 `/api/v1/my-files` 列表/搜索/重命名/删除，上传走 `/api/v1/files/upload`，下载使用带认证 `downloadWithAuth()`。
- **用户设置** (`/settings`)：需登录；账号资料与密码修改调用 `/api/v1/auth/me`、`/api/v1/auth/me/change-password`，本地主题偏好使用浏览器存储，需求见 [user_settings_requirements.md](./user_settings_requirements.md)。

### 4. 探索式学习 (Explore)
- **统一路由** (`/explore`)：发现态主题气泡 + 思维导图 + 知识面板；窄屏知识面板为底部 Sheet。
- 前端调用 `/api/v2/explore/topics|map|expand|resources|quiz|quiz/grade`，详解与追问使用 `/api/v2/explore/detail`、`/api/v2/explore/detail/chat` SSE；领域由 `VITE_EXPLORE_DOMAIN` 区分 RadAI=`ne`、CSH-AI=`csh`、FMCA=`fmca`。
- 详见探索式学习相关文档 · 总览 [探索式学习功能方案](../../plans/探索式学习功能方案.md)。

### 5. 自定义学习 (Custom Learning)
- **统一路由** (`/custom-learning`、`/custom-learning/path/:planId?`)：我的学习计划、按目标生成计划、学习路径图。
- 后端 `/api/v1/custom-learning`；计划生成可传入 `use_learning_style` 与 `explore_snapshot`，用于把学习风格测评结果和探索式学习当前知识点沉淀为路径。
- 详见 [custom_learning_closed_loop_design.md](../custom_learning_closed_loop_design.md)。

### 6. 学习风格 (Learning Style)
- **统一路由** (`/learning-style`)：学习偏好问卷、阶段保存、提交评分和是否应用到 AI。
- API：`/api/v1/learning-style/questionnaire`、`/attempts/current`、`/attempts`、`/attempts/{id}/answers`、`/attempts/{id}/submit`、`/profile`、`/profile/preferences`。
- 自定义学习工作台会读取 style summary，并在生成计划时选择是否应用。

### 7. RAG 与知识库检索（后端）

专家模式不直连 RAG HTTP；检索经 Agent 工具 `multi_kb_search` → `retrieve_context`（按 KB 路由 legacy DirectRetriever 或 self SelfDirectRetrieverV2）。算法说明 → [知识库算法和架构.md](../知识库算法和架构.md)。

### 8. 深度知识库管理 (KB Admin)
- 内嵌于 `/admin/*`，非独立应用。
- 多级目录、分块上传、异步入库（legacy / self_ingestion 双轨）、BM25/关键词图谱初始化。
- 创建知识库时可选择 `ingestion_backend` / `retrieval_backend`。

---

## 📂 四端前端结构

| 应用 | 目录 | 端口 | 后端 | 默认 agent_code |
| :--- | :--- | :--- | :--- | :--- |
| RadAI | `frontend/` | 5173 | 主 API `:8082` | `ne` |
| CSH-AI | `frontend_csh_ai/` | 5174 | 主 API `:8082` | `csh`（地下水页为 `groundwater`） |
| FMCA | `frontend_fmca/` | 5176 | 主 API `:8082` | `fmca`（`kb_policy: disabled`） |
| Guest | `guest/` | 8085 | 访客 API `:8083` | 访客精简入口 |

> RadAI / CSH-AI / FMCA 的 Vite 开发代理 `/api` 与 `/knowledge-api` 默认指向本机主 API `http://127.0.0.1:8082`，可分别通过 `VITE_API_PROXY_TARGET`、`VITE_KNOWLEDGE_API_PROXY_TARGET` 覆盖；`/files` 当前仍指向文件服务 `http://202.115.138.101:9004`。

```text
┌─────────────────────────────────────────────────────────────────┐
│ RadAI frontend :5173                                            │
│  /app 专家模式 │ /explore │ /custom-learning │ /admin           │
└──────────────────────────┬──────────────────────────────────────┘
                           │ HTTP/SSE
┌──────────────────────────▼──────────────────────────────────────┐
│ CSH-AI frontend_csh_ai :5174  →  agent-runtime + groundwater    │
└──────────────────────────┬──────────────────────────────────────┘
                           │
┌──────────────────────────▼──────────────────────────────────────┐
│ FMCA frontend_fmca :5176  →  agent-runtime（无 KB 绑定）          │
└──────────────────────────┬──────────────────────────────────────┘
                           │
┌──────────────────────────▼──────────────────────────────────────┐
│                    主 API :8082                                  │
└─────────────────────────────────────────────────────────────────┘

┌─────────────────────────────────────────────────────────────────┐
│ Guest guest :8085  →  复用 ne-full 组件 + IndexedDB             │
└──────────────────────────┬──────────────────────────────────────┘
                           │
                           ▼
                  访客 API :8083
```

```bash
# 仓库根目录：后台启动 RadAI + CSH-AI + FMCA
bash run_frontends.sh

# RadAI
cd frontend && npm install && npm run dev

# CSH-AI
cd frontend_csh_ai && npm install && npm run dev

# FMCA
cd frontend_fmca && npm install && npm run dev

# 访客前端；可用 GUEST_BACKEND_URL 覆盖默认 http://127.0.0.1:8083
cd guest && npm run dev
```

---

## 📂 项目结构 (RadAI `frontend/`)

```text
frontend/
├── index.html
├── vite.config.ts                # 安全 guard；代理 /api → 8082，/files → MinIO
├── package.json
└── src/
    ├── main.ts
    ├── router/index.ts           # 路由 + 移动端/桌面端守卫 + 鉴权
    ├── utils/request.ts          # Bearer Token、自动校验、下载封装
    ├── utils/sseClient.ts        # 通用 fetch + ReadableStream SSE 解析器（Explore 等复用）
    ├── api/
    │   ├── agent-v2.ts           # V2 Agent SSE、ChatFileAPI、后台任务 status/stream/cancel
    │   ├── conversations.ts      # 会话 CRUD
    │   ├── auth.ts               # 用户认证、CAS URL、RadAI 访客短信登录/注册
    │   ├── user-settings.ts      # /auth/me 个人设置
    │   ├── learning-style.ts     # 学习风格测评
    │   ├── asr.ts                # 语音转写
    │   ├── explore.ts            # 探索式学习 /api/v2/explore
    │   ├── admin.ts              # 旧管理认证兼容封装
    │   ├── admin-management.ts   # 用户/API Key/审计/任务/系统健康/自研入库运维
    │   ├── admin-platform.ts     # 模型、智能体、会话管理
    │   ├── admin-runtime.ts      # 运行监控 / Trace / Replay
    │   ├── prompt-management.ts  # 提示词模板 / 抽取 Schema
    │   ├── skills.ts             # Managed Skills
    │   ├── datasets.ts           # 实验数据集
    │   ├── kb.ts                 # 知识库 CRUD（含 ingestion_backend）
    │   └── custom-learning.ts    # 自定义学习 API
    ├── components/layout/
    │   ├── AppShell.vue          # 三列栅格骨架（侧栏 / 主区 / 面板）
    │   ├── AppSidebar.vue        # 品牌 + 导航 + 会话列表 + 用户区
    │   └── TopBar.vue            # 顶栏与侧栏开关
    ├── composables/
    │   ├── useSidebar.ts         # 侧栏 expanded/collapsed + overlay
    │   ├── useMediaQuery.ts      # matchMedia 封装
    │   ├── useThemePreference.ts # light/dark
    │   └── useUpload.ts          # 管理后台分块上传
    ├── app/
    │   └── navigation.ts         # 侧栏一级导航项
    └── views/
        ├── login/UserLogin.vue   # 用户登录
        ├── ne-full/              # 专家模式（主对话）
        │   ├── index.vue
        │   ├── MyFilesView.vue
        │   ├── composables/
        │   │   ├── useAgentChat.ts
        │   │   └── useFileUpload.ts
        │   └── components/
        │       ├── ChatBox.vue
        │       ├── MessageList.vue
        │       ├── MarkdownRenderer.vue
        │       ├── ReasoningBlock.vue
        │       ├── ToolBadge.vue
        │       ├── KBSelector.vue        # 遗留/备用 KB 选择器；当前输入区内置 KB 菜单
        │       ├── ArtifactSidebar.vue
        │       ├── FollowUpSuggestions.vue
        │       ├── FilePreviewChip.vue
        │       ├── MyFilesPickerDialog.vue
        │       ├── ResearchDraftCard.vue
        │       ├── ProcessEventsPanel.vue
        │       ├── WorkflowTimeline.vue
        │       ├── SubagentWorkflowSwitcher.vue
        │       └── SubagentWorkflowTimeline.vue
        ├── admin/                # 管理后台
        │   ├── login.vue
        │   ├── dashboard.vue
        │   ├── knowledge-bases.vue  # 创建 KB 时选择入库/检索后端
        │   ├── kb-detail.vue
        │   ├── system.vue         # 自研入库 switch-backend
        │   └── components/AdminLayout.vue
        ├── explore/              # 探索式学习
        ├── learning-style/       # 学习风格
        ├── settings/             # 用户设置
        └── custom-learning/      # 自定义学习
            ├── index.vue
            └── path.vue
```

> **已移除**：`views/chat/`（V1 标准 RAG 对话页）已从代码库删除，相关能力由 V2 专家模式统一承接。

### CSH-AI、FMCA 与 Guest 差异

```text
frontend_csh_ai/
├── src/views/groundwater/index.vue              # /groundwater/:threadId?
├── src/views/admin/groundwater-projects.vue     # /admin/groundwater-projects
├── src/api/groundwater.ts                       # /api/v2/groundwater/admin/*
└── vite.config.ts                               # 端口 5174；allowedHosts 含 csh-ai 域名

frontend_fmca/
├── src/app/navigation.ts                        # 导航：知识问答 + 探索式学习
├── vite.config.ts                               # 端口 5176
└── src/router/index.ts                          # admin 子集：dashboard/kbs/prompts/skills/kb-detail

guest/
├── src/App.vue                                  # 无 Vue Router，组件内管理登录/对话/历史
├── src/components/GuestLogin.vue                # 账号、注册、短信登录；复用 AuthAPI
├── src/components/GuestChatBox.vue
├── src/components/GuestHistorySidebar.vue
├── src/services/guestChatService.js             # 访客轻量查询服务与 X-Device-ID 辅助
└── vite.config.js                               # 端口 8085；@ → ../frontend/src，@guest → ./src
```

CSH-AI 没有 RadAI 的 `/custom-learning`、`/learning-style`、`/admin/platform`、`/admin/runtime`、`/admin/system` 路由；其后台重点是知识库、提示词、Skills、数据集与地下水项目绑定。FMCA 导航仅启用知识问答与探索式学习，管理后台为 dashboard/kbs/prompts/skills 子集，且后端 `fmca` Profile 不绑定知识库。Guest 应用不是 Vue Router SPA（JS、无 TypeScript/Vue Router），当前登录页支持账号登录、短信注册和短信登录；认证后复用 `ne-full` 的 `useAgentChat` 与 `MessageList`，未登录 fallback 服务仍保留 IndexedDB 本地会话能力。

---

### V2 Agent 对话流程

```text
  ChatBox ──sendMessage──► useAgentChat ──queryStream──► agent-v2.ts
       ◄── message.delta / reasoning.delta / workflow.step ── SSE ◄── POST /api/v2/agent-runtime/query
       ◄── follow_up.created ──► MessageList / ArtifactSidebar 更新

用户输入 → ChatBox → useAgentChat.sendMessage()
    → AgentAPI.queryStream() (POST /api/v2/agent-runtime/query 或 /api/v2/guest/query, SSE)
        → onMessageDelta / onReasoningDelta / onWorkflowStep
        → onFollowUpCreated → FollowUpSuggestions 渲染
    → MessageList + ArtifactSidebar + AppSidebar 会话列表联动更新
```

### 文档上传入库流程（管理后台）
```
用户选择文件 → useUpload.addFiles()
    → 直传或分块上传 → POST /api/v1/uploads/*
    → KBDetailAPI.ingest() 触发异步入库
```

---

## ⚙️ 路由列表

| 路径 | 组件 | 功能说明 | 权限 |
| :--- | :--- | :--- | :--- |
| `/` | — | 重定向至 `/app` | 公开 |
| `/app/:threadId?` | `ne-full/index.vue` | 专家模式（自适应布局）；未登录进入访客模式 | 公开 |
| `/m/*` | — | 兼容重定向至对应 `/app` 或功能页 | 公开 |
| `/chat/:threadId?` | — | 重定向至 `/app` | 公开 |
| `/ne-full/:threadId?` | — | 重定向至 `/app` | 公开 |
| `/login` | `login/UserLogin.vue` | 用户登录 | 仅未登录 |
| `/settings` | `settings/index.vue` | 用户设置 | 需登录 |
| `/my-files` | `ne-full/MyFilesView.vue` | 个人文件库 | 需登录 |
| `/explore` | `explore/index.vue` | 探索式学习 | 公开 |
| `/custom-learning` | `custom-learning/index.vue` | 自定义学习首页 | 公开 |
| `/custom-learning/path/:planId?` | `custom-learning/path.vue` | 学习路径图 | 公开 |
| `/learning-style` | `learning-style/index.vue` | 学习风格测评 | 公开 |
| `/admin/login` | `admin/login.vue` | 管理员登录 | 公开 |
| `/admin/dashboard` | `admin/dashboard.vue` | 统计看板 | 需 admin |
| `/admin/kbs` | `admin/knowledge-bases.vue` | 知识库列表 | 需 admin |
| `/admin/prompts` | `admin/prompt-management.vue` | 提示词管理 | 需 admin |
| `/admin/skills` | `admin/skills.vue` | Skills 管理 | 需 admin |
| `/admin/datasets` | `admin/datasets.vue` | 实验数据管理 | 需 admin |
| `/admin/platform` | `admin/platform.vue` | 平台运维（RadAI） | 需 admin |
| `/admin/runtime` | `admin/runtime.vue` | 运行监控（RadAI） | 需 admin |
| `/admin/system` | `admin/system.vue` | 系统管理（RadAI） | 需 admin |
| `/admin/kb/:id` | `admin/kb-detail.vue` | 知识库详情 | 需 admin |

路由守卫：`/admin/*` 需 admin 角色；`/my-files`、`/settings` 需登录；普通用户访问 admin 会重定向到 `/app`。`/explore`、`/custom-learning`、`/learning-style` 当前未在路由 meta 上强制登录，具体接口若需认证会由请求层处理。

---

## 🛠️ 快速上手

```bash
cd frontend
npm install
npm run dev          # http://localhost:5173
npm run build
npm run preview
```

---

## 📖 详细模块文档

| 文档 | 说明 |
| :--- | :--- |
| [ne_full_module.md](./ne_full_module.md) | 专家模式模块 (V2 Agent) |
| [admin_module.md](./admin_module.md) | 管理后台模块 |
| [infrastructure_module.md](./infrastructure_module.md) | 路由/请求/上传/样式 |
| [mobile_desktop_guest_frontend_implementation_checklist.md](./mobile_desktop_guest_frontend_implementation_checklist.md) | 移动端/访客实施清单 |
| [frontend_responsive_redesign_plan.md](./frontend_responsive_redesign_plan.md) | **响应式重设计方案（已落地）**：单一自适应布局、断点体系、ChatGPT 式中性视觉；含实施记录 |

> V1 标准对话模块文档 [chat_module.md](./chat_module.md) 已标记为**历史参考**（对应页面已移除）。

---

© 2026 核电子学智能助手项目组
