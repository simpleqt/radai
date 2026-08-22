# 标准对话模块 (Chat Module) — 历史参考

> **最后同步**：2026-07-09
> **状态：已移除**  
> `frontend/src/views/chat/` 目录已从代码库删除。V1 RAG 标准对话能力由 V2 专家模式（`/app/:threadId?`、`/m/:threadId?`）统一承接。
> 本文档仅保留作迁移参考，**请勿按本文档开发新功能**。

---

## 变更说明

| 项目 | 旧行为 | 当前行为 |
| :--- | :--- | :--- |
| 路由 | `/chat` 独立页面 | `/chat/:threadId?` 按 UI 模式重定向至 `/app/:threadId?` 或 `/m/:threadId?` |
| 后端接口 | `POST /api/v1/rag/agents/nuclear_electronics/stream` | `POST /api/v2/agent-runtime/query` |
| 前端逻辑 | `useLegacyChat.ts` | `views/ne-full/composables/useAgentChat.ts` |
| 组件目录 | `views/chat/` | `views/ne-full/`，其中 `ChatBox` 内置模型/技能/KB/搜索/附件/语音控制 |
| SSE 协议 | V1 `chunk/metadata/error` | 标准 `AgentEvent` envelope：`message.delta`、`reasoning.delta`、`workflow.step`、`plan.*`、`coordination.*`、`follow_up.created`，并兼容旧事件 |
| 文件能力 | 无或页面内简单上传 | 对话附件走 `/api/v2/chat-files/upload`，个人文件库走 `/api/v1/my-files` / `/api/v1/files/upload` |

如需了解当前对话实现，请参阅 [ne_full_module.md](./ne_full_module.md) 与 [README.md](./README.md)。

---

## 原 V1 架构（归档）

原标准对话模块曾位于 `/chat` 路由，对接后端 V1 RAG Agent，支持模型选择与 SSE 流式响应。

```text
views/chat/          # 已删除
├── index.vue
└── composables/
    └── useLegacyChat.ts
```

原 V1 数据流：

```
用户输入 → useLegacyChat.sendMessage()
    → fetch /api/v1/rag/agents/nuclear_electronics/stream (SSE)
        → type=chunk: 追加 content
        → type=metadata: 最终 Markdown 覆盖
        → type=error: 抛出异常
```

---

© 2026 核电子学智能助手项目组
