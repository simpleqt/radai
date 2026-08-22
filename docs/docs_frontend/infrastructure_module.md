# 前端基础设施模块 (Infrastructure Module)

> **最后同步**：2026-07-23（`frontend/src/router`、`utils/request.ts`、`api/**`、四端 Vite 配置）

本文档详细说明 RadAI 前端（`frontend/`）基础设施层：路由、HTTP 封装、分块上传、全局样式与 Vite 配置。CSH-AI（`frontend_csh_ai/`，5174）与 FMCA（`frontend_fmca/`，5176）结构平行，同样调用 `/api/v2/agent-runtime`，分别通过 `agent_code=csh` / `fmca` 区分领域；CSH 额外包含地下水模块，FMCA 不绑定知识库。访客端（`guest/`）是独立 Vite 应用（JS、无 Vue Router），通过 alias 复用 `frontend/src` 的对话组件与请求层。

---

## 📂 文件结构

```text
src/
├── main.ts                # 应用入口
├── App.vue                # 根组件
├── style.css              # 全局样式 (Tailwind + KaTeX + 主题)
├── vite-env.d.ts          # Vite 类型声明
├── router/
│   └── index.ts           # 路由配置 + 守卫
├── utils/
│   └── request.ts         # HTTP 请求封装
└── composables/
    └── useUpload.ts       # 分块上传组合式函数
```

**配置文件**:
```text
frontend/
├── vite.config.ts         # Vite 构建配置
├── tsconfig.json          # TypeScript 基础配置
├── tsconfig.app.json      # 应用 TS 配置
├── tsconfig.node.json     # Node 端 TS 配置
└── index.html             # 入口 HTML
```

---

## 1. 应用入口 (`main.ts`)

```typescript
import { createApp } from 'vue'
import './style.css'
import App from './App.vue'
import router from './router'

const app = createApp(App)
app.use(router)
app.mount('#app')
```

**启动流程**:
1. `createApp(App)` — 创建 Vue 应用实例
2. `app.use(router)` — 注册 Vue Router 插件
3. `app.mount('#app')` — 挂载到 `index.html` 中的 `#app` 元素

> **注意**: 当前未使用 Pinia/Vuex 状态管理，全局状态仅通过 `request.ts` 中的 `authState` ref 管理。

---

## 2. 根组件 (`App.vue`)

```vue
<template>
  <router-view></router-view>
</template>
```

极简根组件，仅包含 `<router-view>` 插槽。所有页面布局由各路由组件自行管理。

---

## 3. 路由系统 (`router/index.ts`)

### 路由配置

```typescript
const routes = [
  { path: '/', redirect: () => (detectDefaultMode() === 'mobile' ? '/m' : '/app') },
  { path: '/app/:threadId?', component: () => import('@/views/ne-full/index.vue') },
  { path: '/m/:threadId?', component: () => import('@/views/ne-full/index.vue') },
  { path: '/explore', component: () => import('@/views/explore/index.vue') },
  { path: '/m/explore', component: () => import('@/views/explore/index.vue') },
  { path: '/custom-learning/path/:planId?', component: () => import('@/views/custom-learning/path.vue') },
  { path: '/m/custom-learning/path/:planId?', component: () => import('@/views/custom-learning/path.vue') },
  { path: '/custom-learning', component: () => import('@/views/custom-learning/index.vue') },
  { path: '/m/custom-learning', component: () => import('@/views/custom-learning/index.vue') },
  { path: '/learning-style', component: () => import('@/views/learning-style/index.vue') },
  { path: '/m/learning-style', component: () => import('@/views/learning-style/index.vue') },
  { path: '/settings', component: () => import('@/views/settings/index.vue'), meta: { requiresAuth: true } },
  { path: '/m/settings', component: () => import('@/views/settings/index.vue'), meta: { requiresAuth: true } },
  { path: '/login', component: () => import('@/views/login/UserLogin.vue'), meta: { guest: true } },
  { path: '/my-files', component: () => import('@/views/ne-full/MyFilesView.vue'), meta: { requiresAuth: true } },
  { path: '/chat/:threadId?', redirect: /* → /app 或 /m */ },
  { path: '/ne-full/:threadId?', redirect: /* → /app 或 /m */ },
  { path: '/admin/login', component: () => import('@/views/admin/login.vue'), meta: { guest: true } },
  {
    path: '/admin',
    component: AdminLayout,
    redirect: '/admin/dashboard',
    meta: { requiresAuth: true },
    children: [
      { path: 'dashboard', component: () => import('@/views/admin/dashboard.vue') },
      { path: 'kbs', component: () => import('@/views/admin/knowledge-bases.vue') },
      { path: 'prompts', component: () => import('@/views/admin/prompt-management.vue') },
      { path: 'skills', component: () => import('@/views/admin/skills.vue') },
      { path: 'datasets', component: () => import('@/views/admin/datasets.vue') },
      { path: 'platform', component: () => import('@/views/admin/platform.vue') },
      { path: 'runtime', component: () => import('@/views/admin/runtime.vue') },
      { path: 'system', component: () => import('@/views/admin/system.vue') },
      { path: 'kb/:id', component: () => import('@/views/admin/kb-detail.vue') },
    ]
  }
]
```

### 桌面 / 移动双端

| 路径 | 说明 |
| :--- | :--- |
| `/app/:threadId?` | 桌面端专家模式 |
| `/m/:threadId?` | 移动端专家模式（同一 `ne-full/index.vue`） |
| `/explore`、`/m/explore` | 探索式学习（路由公开；接口是否要求登录由后端决定） |
| `/custom-learning`、`/m/custom-learning` | 自定义学习首页 |
| `/custom-learning/path/:planId?` | 学习路径图（路由公开） |
| `/learning-style`、`/m/learning-style` | 学习偏好测评（路由公开） |
| `/settings`、`/m/settings` | 用户设置（需登录） |
| `/` | 按 UA / 屏宽 / `localStorage.ui_mode` 重定向 |

`detectDefaultMode()` 逻辑：`ui_mode=mobile|desktop` 优先；否则 UA 含 mobile 或屏宽 ≤900px 视为移动。

旧路径 `/chat`、`/ne-full` 自动重定向至 `/app` 或 `/m`，**不再作为独立页面**。

### 路由模式

- **History 模式**: `createWebHistory()` — URL 无 `#` 前缀
- **懒加载**: 除 `AdminLayout` 外，组件均 `() => import()` 动态导入

### 路由守卫

```typescript
router.beforeEach((to, from, next) => {
  // 1. 设置 document.title
  // 2. /app ↔ /m 模式校正（防止桌面/移动路径混用）
  // 3. /admin/* 需登录 + admin 角色
  // 4. meta.requiresAuth 页面（/settings、/my-files）需登录 → 未登录重定向 /login?redirect=...
  // 5. meta.guest 页面：已登录用户重定向 /app 或 /admin/dashboard
});
```

| 条件 | 行为 |
| :--- | :--- |
| 访问 `/admin/*` 未登录 | → `/admin/login` |
| 已登录非 admin 访问 `/admin/*` | → `/app` |
| `/my-files`、`/settings` 未登录 | → `/login?redirect=...` |
| `/admin/login` | 始终可访问（允许切换账号） |
| `meta.guest` + 已登录 | → `/app` 或 `/admin/dashboard` |
| `/app` 在移动模式 / `/m` 在桌面模式 | 自动校正路径 |

> `/explore`、`/custom-learning`、`/learning-style` 当前没有 `requiresAuth` meta。文档和页面逻辑应避免假定它们一定先经过前端登录守卫。

### 路由元信息

| 字段 | 类型 | 说明 |
| :--- | :--- | :--- |
| `title` | `string` | 页面标题 |
| `requiresAuth` | `boolean` | 需登录（普通用户或 admin） |
| `guest` | `boolean` | 仅未登录可访问（登录页） |

---

## 4. HTTP 请求封装 (`utils/request.ts`)

### 认证状态管理

```typescript
export const authState = ref({
  token: localStorage.getItem('auth_token') || null,
  user: JSON.parse(localStorage.getItem('user_info') || 'null')
});
```

- 使用 Vue `ref` 创建响应式状态，任何组件均可导入使用。
- 初始化时从 `localStorage` 恢复，实现页面刷新后保持登录。

### Bearer Token 认证

```typescript
export async function request(path: string, options: RequestInit = {}) {
  const headers = new Headers(options.headers || {});
  
  if (authState.value.token) {
    headers.set('Authorization', `Bearer ${authState.value.token}`);
  }
  // ...
}
```

当前请求层只注入 `Authorization: Bearer <token>`。`setAuth()` 会清理历史遗留的 `localStorage.api_key`，前端不再发送 `X-API-Key`。

### 请求拦截逻辑

```
request(path, options)
  ├── URL 规则: http 或 /api/* 原样使用；其他路径加 /api/v1 前缀
  ├── 注入认证头: Authorization
  ├── 设置 Content-Type (非 FormData 时)
  ├── 发送 fetch 请求
  ├── 401/403 → logout() → 重定向登录页
  └── 非 200 → 抛出 Error(data.message || data.detail)
```

### 认证操作

```typescript
setAuth(token, user)                 // 设置认证状态 + localStorage 持久化
logout({ redirectTo }?)              // 清除认证；当前在 admin 页默认回 /admin/login，否则回 /login
startAuthAutoLogoutMonitor()         // 每 60s 和页面可见性变化时验证 /api/v1/auth/verify
downloadWithAuth(url, options?)      // 带 Bearer 下载 Blob，可保存或新窗口打开
```

---

## 5. 分块上传 (`composables/useUpload.ts`)

### 导出接口

```typescript
export function useUpload(kbId: string) {
  return {
    queue,        // ref<UploadFile[]>   上传队列
    isUploading,  // ref<boolean>        是否正在上传
    addFiles,     // (files: FileList | File[]) => void
    removeItem,   // (id: string) => void
    clearQueue,   // () => void
  }
}
```

### UploadFile 类型

```typescript
interface UploadFile {
  id: string;
  file: File;
  name: string;
  size: number;
  status: 'pending' | 'uploading' | 'completed' | 'error';
  progress: number;       // 0-100
  upload_id?: string;     // 分块上传的会话 ID
  file_id?: string;       // 上传完成后的文件 ID
  error?: string;
}
```

### 上传策略

| 文件大小 | 策略 | 端点 |
| :--- | :--- | :--- |
| ≤ 10MB | 直传 | `POST /api/v1/uploads/batch` (FormData) |
| > 10MB | 分块上传 | 三步流程 (init → chunks → complete) |

### 分块上传流程

```
uploadChunked(item)
  ├── 1. 初始化
  │   └── POST /api/v1/uploads/init
  │       Body: { kb_id, filename, file_size, content_type }
  │       → 返回 { upload_id, file_id }
  │
  ├── 2. 逐片上传
  │   └── for i = 0 to totalChunks-1:
  │       POST /api/v1/uploads/chunks/{upload_id}/{i}
  │       Body: FormData { chunk: file.slice(start, end) }
  │       → 更新 progress = ((i+1) / totalChunks) * 100
  │
  └── 3. 完成合并
      └── POST /api/v1/uploads/complete/{upload_id}?total_chunks=N
```

### 并发控制

```typescript
const maxConcurrent = 3;

const processQueue = async () => {
  while (queue.value.some(f => f.status === 'pending')) {
    const pending = queue.value.filter(f => f.status === 'pending').slice(0, maxConcurrent);
    await Promise.all(pending.map(f => uploadFile(f)));
  }
};
```

- 最多 3 个文件同时上传。
- 每个文件内部按顺序上传分块（非并行分块）。

---

## 6. 全局样式 (`style.css`)

### 导入结构

```css
@import "tailwindcss";              /* Tailwind CSS 4.0 */
@import "katex/dist/katex.min.css"; /* KaTeX 数学公式样式 */
```

### 主题变量

```css
@theme {
  --color-primary: #3b82f6;
  --color-primary-foreground: #ffffff;
  --font-family-sans: 'Inter', 'Outfit', -apple-system, BlinkMacSystemFont, ...;
}
```

### 基础层 (`@layer base`)

| 规则 | 说明 |
| :--- | :--- |
| `:root` | CSS 变量 (`--radius`, `--background`, `--foreground`) |
| `body` | 全局背景色 + 抗锯齿字体 |
| `::-webkit-scrollbar` | 自定义滚动条 (1.5px 宽, 圆角) |
| `.fade-*` | 全局淡入淡出过渡 |

### 组件层 (`@layer components`)

| 类名 | 用途 |
| :--- | :--- |
| `.prose` | 学术排版基础 (行高、文字颜色) |
| `.prose h1/h2/h3` | 标题样式 (粗体、间距) |
| `.prose p` | 段落间距 |
| `.prose code` | 行内代码 (indigo 色, 圆角背景) |
| `.prose pre` | 代码块 (深色背景, 圆角) |

---

## 7. Vite 构建配置 (`vite.config.ts`)

```typescript
const apiProxyTarget = process.env.VITE_API_PROXY_TARGET?.trim() || 'http://127.0.0.1:8082'
const knowledgeApiProxyTarget = process.env.VITE_KNOWLEDGE_API_PROXY_TARGET?.trim() || apiProxyTarget

export default defineConfig({
  plugins: [viteSecurityPlugin(), vue(), tailwindcss()],
  resolve: {
    alias: { '@': path.resolve(__dirname, './src') },
  },
  server: {
    host: true,
    port: 5173,
    allowedHosts: ['radai2.cdut.edu.cn', '202.115.138.57', 'localhost', '127.0.0.1'],
    proxy: {
      '/knowledge-api': {
        target: knowledgeApiProxyTarget,
        changeOrigin: true,
        rewrite: (requestPath) => requestPath.replace(/^\/knowledge-api/, ''),
      },
      '/api': {
        target: apiProxyTarget,
        changeOrigin: true,
      },
      '/files': {
        target: 'http://202.115.138.101:9004',
        changeOrigin: true,
      }
    }
  }
})
```

### 配置项说明

| 配置 | 值 | 说明 |
| :--- | :--- | :--- |
| `plugins` | `[viteSecurityPlugin(), vue(), tailwindcss()]` | 本地文件访问 guard + Vue SFC 编译 + Tailwind CSS 4.0 Vite 插件 |
| `resolve.alias.@` | `./src` | `@/` 路径别名指向 src 目录 |
| `server.host` | `true` | 监听所有网络接口 (0.0.0.0) |
| `server.port` | `5173` | 开发服务器端口 |
| `server.allowedHosts` | 域名/IP 白名单 | 限制开发服务器 Host |
| `proxy./api` | `VITE_API_PROXY_TARGET` 或 `127.0.0.1:8082` | 所有 `/api` 请求代理至 Agent/单体 API |
| `proxy./knowledge-api` | `VITE_KNOWLEDGE_API_PROXY_TARGET` 或 `proxy./api` | 移除 `/knowledge-api` 前缀后代理至 Knowledge/单体 API |
| `proxy./files` | `202.115.138.101:9004` | 静态文件 / 附件直链代理 |

> CSH-AI 前端 (`frontend_csh_ai/vite.config.ts`) 端口为 **5174**，代理配置相同，`allowedHosts` 额外包含 `csh-ai.cdut.edu.cn`/`CSH-AI.cdut.edu.cn`。访客端 (`guest/vite.config.js`) 端口为 **8085**，`@` 指向 `../frontend/src`、`@guest` 指向 `guest/src`，`/api` 默认代理到 `process.env.GUEST_BACKEND_URL || http://127.0.0.1:8083`，并通过 `define` 固定 `import.meta.env.VITE_FORCE_GUEST_MODE='true'`。

---

## 8. API 客户端层 (`src/api/`)

| 文件 | 端点 | 说明 |
| :--- | :--- | :--- |
| `agent-v2.ts` | `/api/v2/agent-runtime`、`/api/v2/guest/query`、`/api/v2/chat-files` | Agent/Guest SSE、对话附件、后台任务 `status/stream/cancel` |
| `conversations.ts` | `/api/v2/conversations` | 会话 CRUD |
| `explore.ts` | `/api/v2/explore` | 探索式学习（含 detail SSE） |
| `auth.ts` | `/api/v1/auth`、`/api/v1/guest-sms` | 登录 / 登出 / Token 验证 / CAS URL / 访客短信登录注册（RadAI） |
| `user-settings.ts` | `/api/v1/auth/me` | 当前用户资料、资料更新、改密 |
| `learning-style.ts` | `/api/v1/learning-style` | 学习风格问卷、答题、画像偏好 |
| `custom-learning.ts` | `/api/v1/custom-learning` | 自定义学习计划、路径、复盘与调整 |
| `kb.ts` | `/api/v1/knowledge_bases`、`/api/v1/uploads`、`/api/v1/keywords` | 知识库 CRUD、目录、文档、Pending ingest、关键词初始化 |
| `admin-management.ts` | `/api/v1/admin/*` | 用户、API Key、审计、任务、系统健康、自研入库运维 |
| `admin-platform.ts` | `/api/v1/models`、`/api/v1/agents`、`/api/v1/admin/conversations` | 模型、智能体、会话管理 |
| `prompt-management.ts` | `/api/v1/prompts` | 提示词模板 / 抽取 Schema 管理 |
| `skills.ts` | `/api/v1/skills`、`/api/v1/agents/{agent}/skills` | Skill 上传、版本、授权、智能体绑定 |
| `datasets.ts` | `/api/v1/datasets` | 实验数据集管理 |
| `asr.ts` | `/api/v1/asr/transcribe` | 语音转写 |

CSH-AI 前端复用同类 `agent-v2.ts` 客户端，通过 `agent_code=csh` 或配置化 agent code 调用 `/api/v2/agent-runtime`，并新增 `groundwater.ts` 对接 `/api/v2/groundwater/admin/*`。

---

## 9. TypeScript 配置

### `tsconfig.json` (根配置)
```json
{ "files": [], "references": [
  { "path": "./tsconfig.app.json" },
  { "path": "./tsconfig.node.json" }
]}
```

### `tsconfig.app.json` (应用配置)
- `target`: ES2020
- `module`: ESNext
- `strict`: true
- `paths`: `{ "@/*": ["./src/*"] }`

### `tsconfig.node.json` (Node 端配置)
- 用于 `vite.config.ts` 等构建脚本

---

## 10. 构建脚本

```json
{
  "scripts": {
    "dev": "vite",
    "build": "vue-tsc -b && vite build",
    "preview": "vite preview"
  }
}
```

| 命令 | 说明 |
| :--- | :--- |
| `npm run dev` | 启动开发服务器 (HMR) |
| `npm run build` | 先 TypeScript 类型检查 (`vue-tsc -b`)，再生产构建 |
| `npm run preview` | 预览生产构建产物 |

---

## 11. 未来开发建议

- [ ] **状态管理引入**: 当前全局状态仅 `authState`，随着功能增长建议引入 Pinia。
- [ ] **请求拦截增强**: 全局 axios/fetch 拦截、重试与统一取消（Agent SSE 已在 `agent-v2.ts` + `useAgentChat` 使用 `AbortSignal`）。
- [ ] **环境变量**: RadAI/CSH 开发代理当前仍是硬编码主 API 与文件服务地址；访客端已支持 `GUEST_BACKEND_URL`。
- [ ] **i18n 国际化**: 引入 `vue-i18n` 支持中英文切换。
- [ ] **单元测试**: 引入 Vitest + @vue/test-utils 编写组件测试。
- [ ] **E2E 测试**: 引入 Playwright 编写端到端测试。
- [ ] **PWA 支持**: 添加 `vite-plugin-pwa` 实现离线缓存与安装提示。
- [ ] **错误边界**: 添加 Vue `onErrorCaptured` 全局错误处理，避免白屏。
