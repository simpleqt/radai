# 核电子学智能助手 - 前端项目文档 (Nuclear Electronics Intelligent Assistant - Frontend)

欢迎使用「核电子学智能助手」前端项目。本项目是基于 Vue 3 构建的专业级 AI 对话助手与知识库管理系统，专为核电子学领域的学术研究与文档处理设计。

> **文档同步（2026-06-08）**：前端已消费标准 `AgentEvent` envelope（`message.delta/reasoning.delta/workflow.step/follow_up.created`）；旧 `backend/custom/` 已删除，后端接口统一走 `/api/v2/agent-runtime/*`。

> 成理 CSH-AI 前端为独立工程 `frontend_csh_ai/`（端口 5174），结构与本文档描述基本一致。访客前端为 `guest/`（端口 8085），通过 Vite alias 复用本工程 `src/` 下的核心组件与 composable。

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

### 1. 双端专家模式对话
- **桌面端** (`/app/:threadId?`)：侧栏历史 + 主对话区 + 工件预览侧栏。
- **移动端** (`/m/:threadId?`)：同一套 `ne-full` 页面，响应式布局适配。
- 根路径 `/` 根据 UA/屏宽或 `localStorage.ui_mode` 自动重定向到 `/app` 或 `/m`。
- 旧路径 `/chat`、`/ne-full` 已重定向至上述路由，**不再作为独立页面**。

### 2. V2 Agent 全功能交互
- SSE 流式响应，支持 `chunk` / `status` / `thinking` / `event` / `follow_up_suggestions` / `draft_chunk` 等事件；深度研究另消费 `research_draft_created`、`background_task_started` 等 `event.kind`。
- 推理过程折叠面板 (`ReasoningBlock`)、工具调用徽章 (`ToolBadge`)、子 Agent 工作流时间线。
- 知识库双选 (`KBSelector`)、Skills 选择、深度研究大纲确认 (`ResearchDraftCard`)。
- 工件侧栏 (`ArtifactSidebar`)、过程事件面板 (`ProcessEventsPanel`)。
- 回答后追问建议 (`FollowUpSuggestions`)。
- 对话附件上传 (`useFileUpload` + `FilePreviewChip`)。

### 3. 会话与文件
- **历史侧栏** (`HistorySidebar`)：对接 `/api/v2/conversations`，支持新建/重命名/删除。
- **用户登录** (`/login`)：普通用户 Bearer Token 认证。
- **个人文件库** (`/my-files`)：对接 `/api/v1/my-files`。

### 4. 探索式学习 (Explore)
- **桌面** (`/explore`)：发现态主题气泡 + 思维导图 + 知识面板三 Tab。
- **移动** (`/m/explore`)：同构页面，知识面板为底部 Sheet。
- 详见探索式学习相关文档 · 总览 [探索式学习功能方案](../plans/探索式学习功能方案.md)。

### 5. RAG 与知识库检索（后端）

专家模式不直连 RAG HTTP；检索经 Agent 工具 `multi_kb_search` → `retrieve_context`（四路召回 + Profile）。算法说明 → [知识库算法和架构.md](../知识库算法和架构.md)。

### 6. 深度知识库管理 (KB Admin)
- 内嵌于 `/admin/*`，非独立应用。
- 多级目录、分块上传、异步入库、BM25/关键词图谱初始化。

---

## 📂 三端前端结构

| 应用 | 目录 | 端口 | 后端 |
| :--- | :--- | :--- | :--- |
| RadAI | `frontend/` | 5173 | 主 API `:8082` |
| CSH-AI | `frontend_csh_ai/` | 5174 | 主 API `:8082` |
| Guest | `guest/` | 8085 | 访客 API `:8083` |

> CSH-AI 前端登录态默认调用 `/api/v2/agent-runtime/*`，结构同 RadAI。

```text
┌─────────────────────────────────────────────────────────────────┐
│ RadAI frontend :5173                                            │
│  /app · /m 专家模式 │ /explore │ /admin 知识库管理               │
└──────────────────────────┬──────────────────────────────────────┘
                           │ HTTP/SSE
┌──────────────────────────▼──────────────────────────────────────┐
│ CSH-AI frontend_csh_ai :5174  →  /api/v2/agent-runtime         │
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
# 后台启动 RadAI + CSH-AI
bash run_frontends.sh

# 访客前端
cd guest && npm run dev
```

---

## 📂 项目结构 (RadAI `frontend/`)

```text
frontend/
├── index.html
├── vite.config.ts                # 代理 /api → 8082，/files → MinIO
├── package.json
└── src/
    ├── main.ts
    ├── router/index.ts           # 路由 + 移动端/桌面端守卫 + 鉴权
    ├── utils/request.ts          # Bearer + API Key 双认证
    ├── api/
    │   ├── agent-v2.ts           # V2 Agent SSE（含 thinking、follow_up）
    │   ├── conversations.ts      # 会话 CRUD
    │   ├── auth.ts               # 用户认证
    │   ├── asr.ts                # 语音转写
    │   ├── admin.ts              # 管理员认证
    │   └── kb.ts                 # 知识库 CRUD
    ├── composables/
    │   └── useUpload.ts          # 管理后台分块上传
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
        │       ├── KBSelector.vue
        │       ├── HistorySidebar.vue
        │       ├── ArtifactSidebar.vue
        │       ├── FollowUpSuggestions.vue
        │       ├── FilePreviewChip.vue
        │       ├── ResearchDraftCard.vue
        │       ├── ProcessEventsPanel.vue
        │       └── WorkflowTimeline.vue
        └── admin/                # 管理后台
            ├── login.vue
            ├── dashboard.vue
            ├── knowledge-bases.vue
            ├── kb-detail.vue
            └── components/AdminLayout.vue
```

> **已移除**：`views/chat/`（V1 标准 RAG 对话页）已从代码库删除，相关能力由 V2 专家模式统一承接。

---

### V2 Agent 对话流程

```text
  ChatBox ──sendMessage──► useAgentChat ──queryStream──► agent-v2.ts
       ◄── message.delta / reasoning.delta / workflow.step ── SSE ◄── POST /api/v2/agent-runtime/query
       ◄── follow_up.created ──► MessageList / ArtifactSidebar 更新

用户输入 → ChatBox → useAgentChat.sendMessage()
    → AgentAPI.queryStream() (POST /api/v2/agent-runtime/query, SSE)
        → onMessageDelta / onReasoningDelta / onWorkflowStep
        → onFollowUpCreated → FollowUpSuggestions 渲染
    → MessageList + ArtifactSidebar + HistorySidebar 联动更新
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
| `/` | — | 重定向至 `/app` 或 `/m` | 公开 |
| `/app/:threadId?` | `ne-full/index.vue` | 桌面端专家模式 | 公开 |
| `/m/:threadId?` | `ne-full/index.vue` | 移动端专家模式 | 公开 |
| `/chat/:threadId?` | — | 重定向至 `/app` 或 `/m` | 公开 |
| `/ne-full/:threadId?` | — | 重定向至 `/app` 或 `/m` | 公开 |
| `/login` | `login/UserLogin.vue` | 用户登录 | 仅未登录 |
| `/my-files` | `ne-full/MyFilesView.vue` | 个人文件库 | 需登录 |
| `/explore` | `explore/index.vue` | 探索式学习（桌面） | 公开 |
| `/m/explore` | `explore/index.vue` | 探索式学习（移动） | 公开 |
| `/admin/login` | `admin/login.vue` | 管理员登录 | 公开 |
| `/admin/dashboard` | `admin/dashboard.vue` | 统计看板 | 需 admin |
| `/admin/kbs` | `admin/knowledge-bases.vue` | 知识库列表 | 需 admin |
| `/admin/kb/:id` | `admin/kb-detail.vue` | 知识库详情 | 需 admin |

路由守卫：`/admin/*` 需 admin 角色；`/my-files` 需登录；普通用户访问 admin 会重定向到 `/app`。

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

> V1 标准对话模块文档 [chat_module.md](./chat_module.md) 已标记为**历史参考**（对应页面已移除）。

---

© 2026 核电子学智能助手项目组
