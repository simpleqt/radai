# RadAI 前端

核电子学智能助手 Vue 3 前端，专家模式对话 + 知识库管理后台。

**详细文档** → [`docs/docs_frontend/README.md`](../docs/docs_frontend/README.md)

---

## 快速开始

```bash
npm install
npm run dev      # http://localhost:5173
```

开发服务器将 `/api` 代理至主后端 `:8082`。

---

## 主要路由

| 路径 | 说明 |
| :--- | :--- |
| `/app/:threadId?` | 桌面端专家模式 |
| `/m/:threadId?` | 移动端专家模式 |
| `/login` | 用户登录 |
| `/my-files` | 个人文件库 |
| `/admin/*` | 知识库管理后台 |

---

## 技术栈

Vue 3 · TypeScript · Vite 8 · Tailwind CSS 4 · Vue Router · Marked · KaTeX

---

## 相关

- CSH-AI 前端：`frontend_csh_ai/`（端口 5174）
- 访客前端：`guest/`（端口 8085）
