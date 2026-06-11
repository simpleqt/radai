# 管理后台模块 (Admin Module)

> **最后同步**：2026-05-31（对照 `views/admin/`、`api/kb.ts`）

管理后台模块位于 `/admin` 路由下，提供知识库的全生命周期管理，包括登录认证、统计看板、知识库 CRUD、文档上传入库、目录管理、批量操作与搜索优化工具。

**认证**：`POST /api/v1/auth/login`（与专家模式共用 Token）；管理路由 `meta.requiresAdmin`。

---

## 📂 文件结构

```text
views/admin/
├── login.vue                 # 管理员登录页
├── dashboard.vue             # 仪表盘看板
├── knowledge-bases.vue       # 知识库列表页
├── kb-detail.vue             # 知识库详情页 (核心页面)
└── components/
    └── AdminLayout.vue       # 管理后台布局框架
```

**API 层**:
```text
api/
├── admin.ts                  # 认证 API (login/logout/verify)
└── kb.ts                     # 知识库 CRUD API (KBDetailAPI, 20+ 端点)
```

---

## 1. 布局框架 (`AdminLayout.vue`)

### 组件结构
```
AdminLayout.vue
├── <aside> 侧边栏 (固定左侧, w-64)
│   ├── Logo 区域 (Database 图标 + "知识库管理平台")
│   ├── 导航菜单
│   │   ├── 控制台      → /admin/dashboard
│   │   ├── 知识库管理  → /admin/kbs
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
  { name: '智能对话',   path: '/app',            icon: MessageSquare },
];
```

### 响应式设计
- 桌面端 (`lg:`): 侧边栏常驻，主内容区自适应宽度。
- 移动端: 侧边栏默认隐藏 (`-translate-x-full`)，通过汉堡菜单切换。

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
  → 返回 { token, user: { api_key, ... } }
  → setAuth(token, apiKey, user)
    → authState.token = token
    → authState.apiKey = apiKey
    → authState.user = user
    → localStorage 持久化 (auth_token, api_key, user_info)
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
│   ├── 知识资源库 (total_kbs)    [Database 图标, indigo]
│   ├── 解析文档 (total_docs)     [FileText 图标, blue]
│   ├── 抽取实体 (total_entities) [Share2 图标, emerald]
│   └── 推断关系 (total_relations)[Share2 图标, amber]
└── <div> 二级网格 (2 列)
    ├── 最近更新的知识库 (表格)
    └── 最新入库动态 (列表)
```

### 数据加载
```typescript
loadDashboardData()
  ├── request('/knowledge_bases') → 获取所有知识库
  │   ├── 统计: total_kbs, total_docs, total_entities, total_relations
  │   └── recentKBs: 按 updated_at 降序取前 5
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
```typescript
KBDetailAPI.create(newKB) → POST /api/v1/knowledge_bases
```

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
| 虚拟目录 | Clock (indigo) | `is_virtual=true` | 无操作 |

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
| 重命名 | `KBDetailAPI.renameDirectory()` | 仅物理目录可重命名 |
| 删除 | `KBDetailAPI.deleteDirectory()` | 可选是否同时删除目录下文档 |

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
| `ingestStatus` | `GET /api/v1/uploads/ingest/status/:taskId` | 入库任务状态（内存任务表，可轮询） |
| （分块上传） | `POST /api/v1/uploads/init` 等 | 大文件分块，见 `useUpload.ts` |
| `recentDocuments` | `GET /api/v1/documents/recent` | 最近入库文档 |
| `getDirectories` | `GET /api/v1/knowledge_bases/:id/directories` | 目录树 |
| `batchMoveDocuments` | `POST /api/v1/knowledge_bases/:id/documents/batch-move` | 批量移动文档 |
| `batchDeleteDocuments` | `POST /api/v1/knowledge_bases/:id/documents/batch-delete` | 批量删除文档 |
| `batchMovePending` | `PATCH /api/v1/uploads/pending/batch-move` | 批量移动待处理文件 |
| `renameDirectory` | `POST /api/v1/knowledge_bases/:id/directories/:path/rename` | 重命名目录 |
| `deleteDirectory` | `DELETE /api/v1/knowledge_bases/:id/directories/:path` | 删除目录 |
| `updateDocument` | `PATCH /api/v1/knowledge_bases/:id/documents/:docId` | 更新文档 |
| `initKeywords` | `POST /api/v1/keywords/init/:kbId` | 初始化关键词提取 |
| `getKeywordInitStatus` | `GET /api/v1/keywords/init/status/:taskId` | 关键词初始化状态 |

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

- [ ] **知识库编辑**: 当前仅支持创建和删除；`KBDetailAPI.update` 已存在，UI 待接入。
- [ ] **入库进度实时追踪**: `ingestStatus` API 已有，详情页可轮询展示 Worker 解析进度。
- [ ] **文档预览**: 支持在线预览 PDF/Markdown 文档内容。
- [ ] **拖拽排序**: 文档和目录支持拖拽调整顺序。
- [ ] **操作日志**: 记录管理员的创建/删除/入库操作，便于审计。
- [x] **批量删除**: `kb-detail.vue` 已调用 `batchDeleteDocuments`（2026-05 代码已落地）。
- [ ] **知识图谱可视化**: 在详情页展示实体-关系 Nebula 子图预览。
- [ ] **权限分级**: 区分超级管理员和普通管理员，限制敏感操作。
