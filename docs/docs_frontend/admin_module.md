# 管理后台模块 (Admin Module)

> **最后同步**：2026-07-23（对照 `frontend/src/views/admin/**`、`frontend/src/api/{admin-management,admin-platform,admin-runtime,skills,prompt-management,datasets,kb}.ts` 以及后端管理路由）

管理后台模块位于 `/admin` 路由下，定位为“智能体管理平台”统一管理控制台，覆盖登录认证、统计看板、知识库、提示词模板、Skills、实验数据集、模型与智能体配置、运行监控（Trace/Replay）、会话审计、用户/API Key/任务/系统健康和自研入库运维。CSH-AI / FMCA 管理后台为子集（无 platform/runtime/system；CSH 另有 groundwater-projects）。

**认证**：`POST /api/v1/auth/login`（与专家模式共用 Bearer Token）；管理父路由使用 `meta.requiresAuth=true`，路由守卫再检查 `authState.user.role === 'admin'`。

---

## 📂 文件结构

```text
views/admin/
├── login.vue                 # 管理员登录页
├── dashboard.vue             # 仪表盘看板
├── knowledge-bases.vue       # 知识库列表页
├── kb-detail.vue             # 知识库详情页 (核心页面)
├── prompt-management.vue     # 提示词 / 入库抽取 Schema 管理
├── skills.vue                # Managed Skills 上传、版本、授权、绑定
├── datasets.vue              # 实验数据集上传、预览、绑定
├── platform.vue              # 模型、智能体、会话管理
├── runtime.vue               # 运行监控（Trace / Replay）
├── system.vue                # 用户、API Key、审计、任务、系统健康、自研入库运维
└── components/
    ├── AdminLayout.vue       # 管理后台布局框架
    ├── SchemaStructuredEditor.vue
    ├── TreeSelector.vue
    └── KnowledgeGraphNebula.vue
```

**API 层**:
```text
api/
├── admin.ts                  # 认证 API (login/logout/verify)
├── admin-management.ts       # 系统管理、用户、API Key、任务、自研入库运维
├── admin-platform.ts         # 模型、智能体、会话管理
├── admin-runtime.ts          # 运行监控 / Trace / Replay
├── prompt-management.ts      # 提示词模板 / 抽取 Schema
├── skills.ts                 # Skill 上传、生命周期、授权、智能体绑定
├── datasets.ts               # 实验数据集管理
└── kb.ts                     # 知识库 CRUD API (KBDetailAPI, 20+ 端点)
```

---

## 1. 布局框架 (`AdminLayout.vue`)

### 组件结构
```
AdminLayout.vue
├── <aside> 侧边栏 (固定左侧, w-64)
│   ├── Logo 区域 (Bot 图标 + "智能体管理平台")
│   ├── 导航菜单
│   │   ├── 控制台      → /admin/dashboard
│   │   ├── 知识库管理  → /admin/kbs
│   │   ├── 提示词管理  → /admin/prompts
│   │   ├── Skills 管理 → /admin/skills
│   │   ├── 实验数据    → /admin/datasets
│   │   ├── 平台运维    → /admin/platform
│   │   ├── 运行监控    → /admin/runtime
│   │   ├── 系统管理    → /admin/system
│   │   └── 智能对话    → /app
│   └── 用户信息 + 注销按钮
└── <div> 主内容区
    ├── <header> 移动端汉堡菜单 (lg:hidden)
    └── <main> 路由视图 (带 fade 过渡动画)
```

### 导航项
```typescript
const navItems = [
  { name: '控制台',     path: '/admin/dashboard', icon: BarChart3 },
  { name: '知识库管理', path: '/admin/kbs',       icon: Database },
  { name: '提示词管理', path: '/admin/prompts',   icon: FileText },
  { name: 'Skills 管理', path: '/admin/skills',   icon: Sparkles },
  { name: '实验数据',   path: '/admin/datasets',  icon: TableProperties },
  { name: '平台运维',   path: '/admin/platform',  icon: ServerCog },
  { name: '运行监控',   path: '/admin/runtime',   icon: Activity },
  { name: '系统管理',   path: '/admin/system',    icon: Settings },
  { name: '智能对话',   path: '/app',             icon: MessageSquare },
];
```

### 响应式设计
- 桌面端 (`lg:`): 侧边栏常驻并使用 `sticky` + `h-screen` 固定在视口内，主内容区自适应宽度。
- 移动端: 侧边栏默认隐藏 (`-translate-x-full`)，通过汉堡菜单切换。
- 布局根节点使用 `h-screen overflow-hidden`。侧边栏导航和主内容 `<main>` 各自滚动，用户信息区、Logo 区和移动端 Header 使用 `shrink-0` 保持不被长菜单或长页面挤压。

### 路由过渡动画
```css
.fade-enter-from { opacity: 0; transform: translateY(10px); }
.fade-leave-to   { opacity: 0; transform: translateY(-10px); }
```

---

## 2. 登录页 (`login.vue`)

### 认证流程
```
用户输入 username + password
  → AuthAPI.login(username, password)
  → 返回 { token, user }
  → setAuth(token, user)
    → authState.token = token
    → authState.user = user
    → localStorage 持久化 (auth_token, user_info)，并清理旧 api_key
  → router.push('/admin/dashboard')
```

### 默认值
- 用户名: `admin`
- 密码: `admin`

> **安全提示**: 默认凭据仅用于开发环境，生产环境必须修改。

---

## 3. 仪表盘 (`dashboard.vue`)

### 组件结构
```
dashboard.vue
├── <div> 标题 + "新建库任务" 按钮
├── <div> 统计卡片网格 (4 列)
│   ├── 注册用户 (total_users / active_users)
│   ├── 活跃会话 (total_conversations)
│   ├── 待处理任务 (pending_tasks)
│   └── 系统健康 (systemHealth 汇总)
└── <div> 二级网格 (2 列)
    ├── 最近更新的知识库 (表格)
    └── 最新入库动态 (列表)
└── <div> 最近审计与系统状态摘要
```

### 数据加载
```typescript
loadDashboardData()
  ├── request('/knowledge_bases') → 获取所有知识库
  │   ├── 统计: total_kbs, total_docs, total_entities, total_relations
  │   └── recentKBs: 按 updated_at 降序取前 5
  ├── AdminManagementAPI.overview() → 用户、会话、任务、最近审计
  ├── AdminManagementAPI.systemHealth() → MySQL/Redis/MinIO 等健康状态
  └── KBDetailAPI.recentDocuments() → 获取最近入库文档
```

### 文档状态映射
| parse_status | 显示 | 样式 |
| :--- | :--- | :--- |
| 0 | 待解析 | amber |
| 1 | 解析完成 | emerald |
| 2 | 解析失败 | red |

---

## 4. 知识库列表 (`knowledge-bases.vue`)

### 组件结构
```
knowledge-bases.vue
├── <div> 标题 + 搜索框 + "新建知识库" 按钮
├── 加载中骨架屏
├── 空状态 (Database 图标 + 引导文案)
├── 知识库卡片网格 (3 列)
│   └── 每张卡片:
│       ├── 图标 + 删除按钮 (hover 显示)
│       ├── 知识库名称 + 编码 + 学科
│       ├── 描述 (2 行截断)
│       ├── 统计: 文档数 + 实体数
│       └── "管理详情" 按钮 → /admin/kb/:id
└── 创建弹窗 (Modal)
    ├── 名称 (kb_name)
    ├── 编码 (kb_code)
    ├── 学科 (subject)
    └── 描述 (description)
```

### 搜索过滤
```typescript
kbs.filter(k => k.kb_name.toLowerCase().includes(searchQuery.toLowerCase()))
```
- 前端过滤，仅匹配知识库名称。

### 创建知识库

创建弹窗（`knowledge-bases.vue`）支持选择入库/检索后端：

| 字段 | 选项 | 说明 |
| :--- | :--- | :--- |
| `ingestion_backend` | `legacy_lightrag`（默认）/ `self_ingestion` | 自研入库需后端 `SELF_INGESTION_ENABLED` |
| `retrieval_backend` | `legacy_lightrag` / `self_ingestion` | 选 self 入库时默认联动 self 检索 |
| `self_ingestion_enabled` | bool | 与全局开关共同授权 |
| `self_retriever_enabled` | bool | 当前由 `retrieval_backend !== legacy_lightrag` 推导 |
| `extraction_schema_id` | string | 可选，来自 `KBDetailAPI.listExtractionSchemas()` → `/api/v1/knowledge_bases/extraction_schemas` |

```typescript
KBDetailAPI.create(newKB) → POST /api/v1/knowledge_bases
// newKB 含 ingestion_backend、retrieval_backend、self_ingestion_enabled、self_retriever_enabled
```

知识库详情页（`kb-detail.vue`）与系统管理页（`system.vue`）支持编辑后端配置及 `switch-backend` 切换。自研库文档删除、重排 chunk 等走独立 API 分支。

详见 [入库实现方案.md](../入库实现方案.md)。

### 删除知识库
```typescript
KBDetailAPI.delete(id) → DELETE /api/v1/knowledge_bases/:id
// 需二次确认
```

---

## 5. 知识库详情 (`kb-detail.vue`) ⭐核心页面

这是管理后台最复杂的页面，集成了文档管理、目录浏览、上传入库、批量操作与搜索优化功能。

### 组件结构
```
kb-detail.vue
├── <div> 顶部导航
│   ├── 返回按钮 + 面包屑
│   ├── 知识库名称 (Database 图标)
│   └── 搜索框 + "上传文档" 按钮
├── <div> 路径面包屑 (根目录 > 子目录 > ...)
└── <main> 主内容 (4 列网格)
    ├── <aside> 侧边栏 (1 列)
    │   ├── 数据库指标卡片 (文档/实体/关系)
    │   ├── 快速提示卡片
    │   └── 搜索优化卡片 (关键词初始化)
    └── <div> 文档列表 (3 列)
        ├── 目录行 (Folder/Clock 图标 + 虚拟标签)
        ├── 文档行 (选择框 + 文件名 + 状态 + 时间 + 操作)
        └── 分页控件
```

### 5.1 目录浏览

**目录树加载**:
```typescript
KBDetailAPI.getDirectories(kbId) → GET /api/v1/knowledge_bases/:id/directories
→ directoryTree.value = res.tree
```

**当前层级目录计算**:
```typescript
currentLevelDirs = computed(() => {
  if (!currentPath.value) return directoryTree.value.filter(d => !d.path.includes('/'));
  // 按 currentPath 逐级查找子节点
});
```

**面包屑导航**:
```typescript
breadcrumbs = computed(() => {
  // 将 "a/b/c" 拆分为 [{name:'a', path:'a'}, {name:'b', path:'a/b'}, ...]
});
```

**目录类型**:
| 类型 | 图标 | 标识 | 操作 |
| :--- | :--- | :--- | :--- |
| 物理目录 | Folder (amber) | `is_virtual=false` | 重命名、删除 |
| 虚拟日期目录 | Clock (indigo) | `is_virtual=true` | 可按 `YYYY-MM-DD` 重命名或移入物理目录；不可删除/排序 |

### 5.2 文档列表

**分页加载**:
```typescript
KBDetailAPI.documents(kbId, {
  skip: (currentPage - 1) * pageSize,
  limit: pageSize,
  category_path: currentPath || ''
})
```

**文档状态**:
| parse_status | 显示 | 颜色 |
| :--- | :--- | :--- |
| 0 | 待解析 | amber |
| 1 | 已解析 | emerald |
| 2 | 解析失败 | red |

**批量选择**:
```typescript
selectedDocIds = ref(new Set<string>())
toggleSelect(docId)     // 切换单选
toggleSelectAll()       // 全选/取消全选
```

### 5.3 文件上传入库

**上传流程**（`frontend/src/composables/useUpload.ts`）:
```
点击"上传文档" → useUpload.addFiles(files)
  → ≤10MB: POST /api/v1/uploads/batch（multipart，支持 .pdf/.docx/.md 等）
  → >10MB: POST /uploads/init
         → POST /uploads/chunks/{upload_id}/{index}
         → POST /uploads/complete/{upload_id}?total_chunks=N
  → 设置 document_date、category_path
  → KBDetailAPI.ingest() → POST /api/v1/uploads/ingest
  → 可选轮询 GET /api/v1/uploads/ingest/status/{task_id}
```

**Markdown 直入库**：`.md` 走同一上传接口；Worker `ingest_pending_processor` 识别后 `route=md-direct`，跳过 MinerU。详见 [md_ingestion_api.md](../md_ingestion_api.md)。

> 另有一套管理端入库 API：`POST /api/v1/ingestion/ingest`（`document_ingestion.py`），管理后台 UI 当前使用 `/uploads/ingest`。

### 5.4 批量操作

| 操作 | API | 说明 |
| :--- | :--- | :--- |
| 批量移动 | `KBDetailAPI.batchMoveDocuments()` | 移动选中文档到目标目录 |
| 批量删除 | `KBDetailAPI.batchDeleteDocuments()` | 批量删除文档及关联向量 |
| 单个删除 | `KBDetailAPI.deleteDocument()` | 删除文档及关联向量数据 |

### 5.5 搜索优化 (关键词初始化)

**初始化流程**:
```
点击"初始化关键词提取"
  → KBDetailAPI.initKeywords(kbId, {
      force_rebuild: false,
      init_bm25: true,
      warmup_low_level: true,
      include_vector: false
    })
  → 返回 task_id
  → pollKeywordInitStatus(taskId) (1.5s 间隔轮询)
    → running: 显示阶段 + 消息
    → success: 显示统计 (实体/BM25文档/词项/图节点/图边)
    → failed: 显示错误信息
```

### 5.6 目录管理

| 操作 | API | 说明 |
| :--- | :--- | :--- |
| 重命名 | `KBDetailAPI.renameDirectory()` | 物理目录可直接重命名；虚拟日期目录只能改为有效的 `YYYY-MM-DD`，会同步更新文档日期 |
| 移入目录 | `KBDetailAPI.moveDirectory()` | 拖拽物理目录或虚拟日期目录到另一个物理目录；后端会阻止移动到自身子目录 |
| 删除 | `KBDetailAPI.deleteDirectory()` | 仅物理目录可删除，可选是否同时删除目录下文档 |

---

## 6. API 端点汇总

### 认证 API (`api/admin.ts`)

| 方法 | 端点 | 说明 |
| :--- | :--- | :--- |
| `AuthAPI.login` | `POST /api/v1/auth/login` | 登录获取 token |
| `AuthAPI.logout` | `POST /api/v1/auth/logout` | 登出 |
| `AuthAPI.verify` | `GET /api/v1/auth/verify` | 验证 token 有效性 |

### 知识库 API (`api/kb.ts` - KBDetailAPI)

| 方法 | 端点 | 说明 |
| :--- | :--- | :--- |
| `list` | `GET /api/v1/knowledge_bases` | 列表 (支持分页参数) |
| `create` | `POST /api/v1/knowledge_bases` | 创建知识库 |
| `get` | `GET /api/v1/knowledge_bases/:id` | 获取详情 |
| `update` | `PUT /api/v1/knowledge_bases/:id` | 更新知识库 |
| `delete` | `DELETE /api/v1/knowledge_bases/:id` | 删除知识库 |
| `documents` | `GET /api/v1/knowledge_bases/:id/documents` | 文档列表 (分页+目录过滤) |
| `deleteDocument` | `DELETE /api/v1/knowledge_bases/:id/documents/:docId` | 删除文档 |
| `ingest` | `POST /api/v1/uploads/ingest` | 触发入库（PendingFile → Worker） |
| `ingestStatus` | `GET /api/v1/uploads/ingest/status/:taskId` | 入库任务状态（基于 PendingFile/upload_id 与 Worker 进度，可轮询） |
| （分块上传） | `POST /api/v1/uploads/init` 等 | 大文件分块，见 `useUpload.ts` |
| `recentDocuments` | `GET /api/v1/documents/recent` | 最近入库文档 |
| `getDirectories` | `GET /api/v1/knowledge_bases/:id/directories` | 目录树 |
| `batchMoveDocuments` | `POST /api/v1/knowledge_bases/:id/documents/batch-move` | 批量移动文档 |
| `batchDeleteDocuments` | `POST /api/v1/knowledge_bases/:id/documents/batch-delete` | 批量删除文档 |
| `batchMovePending` | `PATCH /api/v1/uploads/pending/batch-move` | 批量移动待处理文件 |
| `renameDirectory` | `POST /api/v1/knowledge_bases/:id/directories/:path/rename` | 重命名目录 |
| `moveDirectory` | `POST /api/v1/knowledge_bases/:id/directories/:path/move` | 移动物理目录或虚拟日期目录 |
| `deleteDirectory` | `DELETE /api/v1/knowledge_bases/:id/directories/:path` | 删除目录 |
| `updateDocument` | `PATCH /api/v1/knowledge_bases/:id/documents/:docId` | 更新文档 |
| `initKeywords` | `POST /api/v1/keywords/init/:kbId` | 初始化关键词提取 |
| `getKeywordInitStatus` | `GET /api/v1/keywords/init/status/:taskId` | 关键词初始化状态 |

---

## 7. 新增管理页总览

### 7.1 提示词管理 (`prompt-management.vue`)

当前只开放一个分类：`extraction_schema`（入库抽取 Schema）。页面左侧展示分类，中间按名称、模板 ID、领域和描述搜索模板，右侧通过 `SchemaStructuredEditor.vue` 编辑 YAML，并在保存前做结构化校验。

| 前端调用 | 后端端点 | 权限 | 说明 |
| :--- | :--- | :--- | :--- |
| `PromptManagementAPI.categories` | `GET /api/v1/prompts/categories` | admin | 返回“入库抽取 Schema”分类 |
| `templates` | `GET /api/v1/prompts/templates?category=extraction_schema` | admin | 列出模板摘要，含实体/关系数量 |
| `getTemplate` | `GET /api/v1/prompts/templates/{category}/{template_id}` | admin | 读取可编辑 YAML |
| `createTemplate` | `POST /api/v1/prompts/templates` | admin | 创建模板；未传 `content` 时后端生成默认 YAML |
| `updateTemplate` | `PUT /api/v1/prompts/templates/{category}/{template_id}` | admin | 更新 YAML 内容 |

模板响应包含 `category`、`template_id`、`label`、`subject`、`version`、`template_version`、`content_format`、`metadata.entity_type_count`、`metadata.relation_type_count`；详情响应额外包含 `content`。保存时后端校验 `schema_id` 与 `template_id` 一致。

### 7.2 Skills 管理 (`skills.vue`)

Skills 管理页对接托管 Skill 控制面，覆盖 ZIP 上传、版本提交/发布/驳回/下载、启停/归档、ACL 授权和智能体绑定。前端使用 `SkillAdminAPI`，后端主路由为 `/api/v1/skills`，智能体绑定由 `/api/v1/agents/{agent_code}/skills` 提供。

| 功能 | API | 当前实现 |
| :--- | :--- | :--- |
| 列表/详情 | `GET /skills`、`GET /skills/{skill_id}` | 按当前用户 `view` 权限过滤，可传 `status`、`owner_user_id` |
| 上传 | `POST /skills/uploads` | multipart ZIP；`visibility=public/private/restricted`；非管理员不能创建 public |
| 版本 | `GET /skills/{id}/versions`、`submit`、`publish/activate`、`reject`、`download` | 版本包不可变，下载返回 600 秒私有 URL |
| 生命周期 | `POST /skills/{id}/enable|disable`、`DELETE /skills/{id}` | 禁用/启用/逻辑归档 |
| 授权 | `GET/POST /skills/{id}/grants`、`DELETE /skills/{id}/grants/{grant_id}` | `view/use/edit/manage` + `allow/deny`，支持用户和角色主体 |
| 智能体绑定 | `GET/PUT /agents/{agent_code}/skills` | 绑定项含 `is_enabled`、`is_required`、`default_selected`、`is_selectable`、`sort_order` |

托管 Skill 的数据模型见 `backend/db/models/skill.py`：`skills`、`skill_versions`、`skill_acl`、`agent_skill_bindings`、`skill_audit_logs`。运行时由 `SkillRuntimeResolver` 强制校验“已发布 + 已绑定 + 有 use 权限 + domain/workflow 兼容”，前端传入的 `selected_skills` 不是可信授权依据。

### 7.3 实验数据 (`datasets.vue`)

实验数据集页管理 `ExperimentDataset`，支持上传 Excel/CSV/XLS、解析 sheet/列 schema、预览数据、编辑元数据、刷新 schema、删除数据集。

| 功能 | API | 权限 |
| :--- | :--- | :--- |
| 列表/详情 | `GET /api/v1/datasets`、`GET /api/v1/datasets/{id}` | 认证用户 |
| 按智能体可见列表 | `GET /api/v1/datasets/by-agent/{agent_code}` | 认证用户 |
| 上传注册 | `POST /api/v1/datasets/upload` | admin |
| 引用注册 | `POST /api/v1/datasets` | admin |
| 更新元数据/绑定 | `PUT /api/v1/datasets/{id}`、`PUT /api/v1/datasets/{id}/agents` | admin |
| 预览 | `GET /api/v1/datasets/{id}/preview` | 认证用户 |
| 刷新/删除 | `POST /api/v1/datasets/{id}/refresh`、`DELETE /api/v1/datasets/{id}` | admin |

核心字段：`code`、`name`、`description`、`file_path`、`file_type`、`file_size`、`storage_type`、`sheet_names`、`column_schema`、`row_counts`、`domain`、`tags`、`is_public`、`agent_codes`、`status`。`agent_codes=[]` 表示全局可见；非空时限制为指定智能体。

### 7.4 平台运维 (`platform.vue`)

平台运维页包含三个 Tab：

| Tab | 前端 API | 后端端点 | 说明 |
| :--- | :--- | :--- | :--- |
| 模型配置 | `AdminPlatformAPI.models/createModel/updateModel/toggleModel/deleteModel` | `/api/v1/models` | 管理 `llm_models`，当前 CRUD 固定写入 `usage_scope=chat`，列表默认 `usage_scope=chat` |
| 智能体 | `AdminPlatformAPI.agents/upsertAgent/setAgentKbs` + `SkillAdminAPI.setAgentSkills` | `/api/v1/agents/*`、`/api/v1/agents/{agent_code}/skills` | 维护智能体基本信息、KB 绑定和 Skill 绑定 |
| 会话管理 | `AdminPlatformAPI.conversationStats/conversations/conversation/deleteConversation` | `/api/v1/admin/conversations/*` | 查询会话统计、分页列表、消息详情和软删除 |

模型表 `llm_models` 字段覆盖 provider、LangChain 类路径 `use`、实际模型名 `model`、API Key/Base URL（支持 `$ENV` 和 env var 回退）、超时、重试、token、温度、思考/视觉/流式能力、Responses API 配置、启用与默认模型。API 返回时会掩码 `api_key`。

### 7.5 系统管理 (`system.vue`)

系统管理页包含 `overview`、`users`、`apiKeys`、`audits`、`tasks`、`selfIngestion`、`system` 七个 Tab。

| Tab | 主要接口 |
| :--- | :--- |
| 概览 | `GET /api/v1/admin/overview` |
| 用户 | `GET/POST /api/v1/admin/users`、`PATCH /admin/users/{user_id}`、`POST /admin/users/{user_id}/enable|disable|reset-password` |
| API Key | `GET /admin/api-keys`、`POST /admin/users/{user_id}/api-keys`、`PATCH/DELETE /admin/api-keys/{key_id}`、`POST /admin/api-keys/{key_id}/rotate` |
| 审计 | `GET /admin/audit-logs`、`GET /admin/audit-logs/{audit_id}` |
| 任务 | `GET /admin/tasks`、`GET /admin/tasks/{task_id}`、`POST /admin/tasks/{task_id}/retry|cancel` |
| 自研入库运维 | `GET /admin/self-ingestion/health`、jobs/stats/quality-facts/repair/scan/shadow-rebuild、`POST /admin/self-ingestion/kb/{kb_id}/switch-backend` |
| 系统健康 | `GET /admin/system/health` |

所有 `/api/v1/admin/*` 接口均要求 `require_admin`。管理员创建用户时可设置 `username`、`password`、`email`、`role`、`is_active`；普通用户自助资料维护不在本页，见 [user_settings_requirements.md](./user_settings_requirements.md)。

---

## 8. 权限说明

- 管理后台路由 `/admin/*` 需 **admin 角色**（`user.role === 'admin'`）。
- 普通用户登录后访问 `/admin` 会被重定向至 `/app`。
- 管理员登录页 `/admin/login` 始终可访问，便于切换账号。
- 用户登录页 `/login` 供普通用户访问专家模式与个人文件库。

路由守卫详见 [infrastructure_module.md](./infrastructure_module.md)。

---

## 9. KnowledgeBase 类型

```typescript
interface KnowledgeBase {
  kb_id: string;
  kb_code: string;
  kb_name: string;
  subject?: string | null;
  description?: string | null;
  status: string;
  doc_count: number;
  entity_count: number;
  relation_count: number;
  ingestion_backend?: 'legacy_lightrag' | 'self_ingestion';
  retrieval_backend?: 'legacy_lightrag' | 'self_ingestion';
  self_ingestion_enabled?: boolean;
  self_retriever_enabled?: boolean;
  created_at: string;
  updated_at: string;
}
```

---

## 10. 相关文档

| 文档 | 说明 |
| :--- | :--- |
| [infrastructure_module.md](./infrastructure_module.md) | 路由守卫、请求封装 |
| [docs_backend/agent_kb_binding_module.md](../docs_backend/agent_kb_binding_module.md) | 智能体-KB 绑定 |
| `backend/api/routers/my_files.py` | 个人文件上传 API |
| [md_ingestion_api.md](../md_ingestion_api.md) | Markdown 直入库 |
| [docs_backend/rag_module.md](../docs_backend/rag_module.md) | 入库与索引同步 |
| [知识库算法和架构.md](../知识库算法和架构.md) | 新建 KB 后配置 `kb_code` → Retrieval Profile |

---

## 11. 未来开发建议

- [x] **知识库编辑**: `kb-detail.vue` 已接入 `KBDetailAPI.update`，可编辑基础信息与入库/检索后端。
- [ ] **入库进度实时追踪**: `ingestStatus` API 已有，详情页可轮询展示 Worker 解析进度。
- [ ] **文档预览**: 支持在线预览 PDF/Markdown 文档内容。
- [x] **目录/文档排序 API**: `KBDetailAPI.reorderDirectories`、`reorderDocuments` 已封装；UI 已有排序入口时应复用这两个接口。
- [ ] **操作日志**: 记录管理员的创建/删除/入库操作，便于审计。
- [x] **批量删除**: `kb-detail.vue` 已调用 `batchDeleteDocuments`（2026-05 代码已落地）。
- [x] **知识图谱可视化**: `KnowledgeGraphNebula.vue` 已在知识库详情中用于 Nebula 子图预览。
- [ ] **权限分级**: 区分超级管理员和普通管理员，限制敏感操作。
