# CSH-AI 前端

成理水文地质智学助手 Vue 3 前端，结构与 RadAI 基本一致，对接 `/api/v2/csh_agent`。

**详细文档** → [`docs/docs_frontend/csh_ai_module.md`](../docs/docs_frontend/csh_ai_module.md)

---

## 快速开始

```bash
npm install
npm run dev      # http://localhost:5174
```

或通过根目录脚本同时启动：

```bash
bash run_frontends.sh   # RadAI:5173 + CSH-AI:5174
```

---

## 与 RadAI 的差异

| 项 | RadAI | CSH-AI |
| :--- | :--- | :--- |
| Agent API | `/api/v2/agent` | `/api/v2/csh_agent` |
| 端口 | 5173 | 5174 |
| 默认知识库 | 核电子学 + 期刊 | 水文地质垂直库 |

其余路由、组件、会话/附件 API 与 RadAI 相同。
