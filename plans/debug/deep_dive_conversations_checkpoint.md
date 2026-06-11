# 深度审查：会话 API + Checkpointer 一致性

> 文件：`api/routers/conversations.py`、`custom/utils/checkpointer.py`、`api/http_server.py` lifespan  
> 只读审查。

---

## 1. 组件关系

```text
┌────────────────────┐
│ 前端 ConversationAPI │
└────────────────────┘

  前端 ConversationAPI ──────────────► API

  API ──────────────► (Conversation 表)

  (Conversation 表) ──────────────► (data/checkpoints.db SQLite)

  (data/checkpoints.db SQLite) ──────────────► NEFullFeatureAgent

  API ──(internal_thread_key)──► (data/checkpoints.db SQLite)

```

| 概念 | 外部 ID | 内部 ID |
|------|---------|---------|
| 用户可见 `thread_id` | UUID 等 | — |
| Checkpointer / LangGraph | — | `u_{user_id}__t_{thread_id}` |
| `SessionFile.session_id` | **外部** thread_id | — |
| `ChatFileIndexManager` 键 | **外部** thread_id | — |
| `ScopedStorage` | 外部 thread_id | — |
| `delete_thread_dir` | internal key | — |

**结论**：会话文件与索引用**外部** thread_id；图状态用**内部** key — 设计一致，但新人易混淆。

---

## 2. `conversations.py` 端点

| 方法 | 路径 | 职责 | 质量 |
|------|------|------|------|
| GET | `/api/v2/conversations` | 列表分页 | ok |
| PATCH | `/{thread_id}` | 重命名；无记录则创建 | ok |
| DELETE | `/{thread_id}` | 软删 + 清理 CP/MinIO/沙箱 | 见 §3 |
| GET | `/{thread_id}/messages` | 从 checkpoint 读 messages | 见 §4 |
| POST | `/{thread_id}/rollback` | 裁剪 messages | 见 §5 |

---

## 3. 删除会话（L98–156）

### 3.1 清理链

1. `Conversation.is_deleted = True`（MySQL）
2. SQLite：`DELETE FROM checkpoints|checkpoint_blobs|checkpoint_writes WHERE thread_id=?`（**internal**）
3. `ScopedStorage.delete_all_files()`（MinIO 线程附件）
4. `get_paths().delete_thread_dir(internal_thread_id)`（本地沙箱）

### 3.2 问题

| ID | 严重度 | 问题 |
|----|--------|------|
| CV-1 | medium | **未清理** `ChatFileIndexManager` 内存索引 — LRU 可能仍保留已删会话索引直至驱逐 |
| CV-2 | medium | **未清理** `UserFile`/`SessionFile` 绑定 — 孤儿 DB 记录与 MinIO `chat_files/` 对象 |
| CV-3 | low | SQL `DELETE FROM {table_name}` 动态表名但白名单固定 — ok |
| CV-4 | low | 清理失败仅 warning，仍返回 success |

---

## 4. 获取消息（L159–246）

### 4.1 数据来源

- `checkpointer.aget(config)` → `channel_values.messages`
- **非** MySQL 消息表 — 与前端 `ConversationAPI.getMessages` 一致

### 4.2 过滤规则（防 UI 污染）

| 过滤条件 | 目的 |
|----------|------|
| `[IMAGE:\d+]` 残留 token | 图片解析失败残留 |
| `metadata.lc_source == summarization` | JIT 压缩消息 |
| 内容以 summary/State Elevation 等开头 | 系统注入 |

**质量**：过滤逻辑集中在此，Agent 侧应保证写入 checkpoint 时已 resolve token — 双轨防御 ✅

### 4.3 问题

| ID | 严重度 | 问题 |
|----|--------|------|
| CV-5 | low | 失败返回 `[]` 与「无会话」不可区分 |
| CV-6 | info | `role` 混用 type/ai/assistant — 前端 `normalizeHistoryMessages` 需兼容 |

---

## 5. 回滚（L280–355）

### 5.1 算法

`_trim_messages_by_user_turns`：保留前 N 个 user 轮及其后的 assistant/tool 消息（到下一 user 前截断）。

### 5.2 实现细节

- 硬编码 `NEFullFeatureAgent(checkpointer=checkpointer)` + `agent._graph.aget_state` / `aupdate_state`
- `Overwrite` 重置 `messages` 与 `artifacts`
- `as_node` 解析：防 LangGraph `Ambiguous update`

### 5.3 问题

| ID | 严重度 | 问题 |
|----|--------|------|
| CV-7 | **high** | **CSH 用户**回滚仍走 `NEFullFeatureAgent` 图 — 与 CSH 对话图可能不一致 |
| CV-8 | medium | 与 `useAgentChat.checkAndResumeInterruptedStream` 联动：rollback 后重发，可能双倍副作用 |
| CV-9 | low | 未同步清理 `Conversation.metadata_`（research_workflow / background_tasks） |

---

## 6. Checkpointer 基础设施

### 6.1 初始化（`http_server.py`）

- 单文件 `backend/data/checkpoints.db`
- 全局 `app.state.checkpointer`：`AsyncSqliteSaver`
- `get_checkpointer_from_request` 仅取引用

### 6.2 多实例 / 多 Worker

| 风险 | 说明 |
|------|------|
| SQLite 写锁 | 高并发写入 checkpoint 瓶颈 |
| 多进程 | 多 worker 共享同一 db 文件需 WAL 与锁测试 |
| 与 TaskManager | 后台任务同进程假设 |

### 6.3 `checkpointer.py`

仅 14 行 — **无** 封装 get/set/delete 工具，删除 SQL 散落在 conversations.py。

---

## 7. 与前端对齐

| 前端 | 后端 |
|------|------|
| `loadHistory` → getMessages | GET messages |
| `rollbackConversation` | POST rollback |
| pending 恢复 rollback + resend | 依赖 keep_user_turns 语义一致 |

`useAgentChat` 在 pending 恢复时 `keepUserTurns - 1` — 与后端 `_trim_messages_by_user_turns` 语义需人工验证边界（N 轮 inclusive/exclusive）。

---

## 8. 整改建议（设计）

1. `delete_conversation`：调用 `get_index_manager().remove(thread_id)`；可选清理 SessionFile  
2. `rollback`：抽象 `get_agent_graph_for_user(product)` 或传入 agent 类参数  
3. 抽 `CheckpointRepository` 统一 delete/get/trim  
4. 文档化 external vs internal thread_id  
5. 多 worker 部署说明 + PostgreSQL checkpointer 路线图  
