# 16 · 会话、工件与用户隔离

> 📊 **图表：** [00_architecture_diagrams.md](./00_architecture_diagrams.md) 图 18、33（会话 ER）

---

## L1 初级 🟢

### Q1：`build_internal_thread_key` 是什么？

**一句话结论：** 将 `(user_id, 客户端 thread_id)` 哈希/拼接为 **checkpointer 内部 key**，实现多用户 thread 命名空间隔离。

#### 在本项目中的实现

- `backend/core/agents/thread_key.py`
- conversations 读消息、Agent 写 checkpoint 均使用 internal key

#### 常见误区

- ❌ 客户端 thread_id 全局唯一即可 → ✅ 必须绑定 user_id

---

### Q2：会话元数据与消息正文分别存在哪？

**一句话结论：** **MySQL `conversations`** 存标题/元数据；**SQLite checkpoint** 存 `messages` 全文与 Graph state。

```text
实体关系:

  User ||--o{ Conversation : owns
  Conversation ||--|| Checkpoint : internal_thread_id

  ┌─ Conversation ────────
  │  string thread_id
  │  string title
  │  json metadata_
  └────────────────────────

  ┌─ Checkpoint ──────────
  │  string sqlite_file
  │  blob channel_values
  └────────────────────────


```

---

### Q3：什么是 ScopedStorage？解决什么问题？

**一句话结论：** MinIO 对象按 **`user_id/thread_id` 前缀**隔离上传与工件，防止跨用户读文件。

#### 在本项目中的实现

- 文档：`docs/docs_backend/user_isolation_module.md`
- 删除会话时 `ScopedStorage.delete_all_files()` 清理前缀

---

### Q4：Agent 输出的 artifact（报告/PPT）如何给前端下载？

**一句话结论：** 生成工具写入 MinIO/本地 + SSE **`artifact` 事件**带元数据；HTTP **`/artifacts/`** 路由提供下载，路径防穿越校验。

#### 参考

- `docs/docs_backend/artifacts_module.md`
- `ThreadState.artifacts` reducer 去重记录 artifact id

---

## L2 中级 🟡

### Q5：恢复历史消息时过滤了哪些内容？

**一句话结论：** `GET /conversations/{id}/messages` 从 checkpoint 反序列化后过滤 **JIT 摘要、State Elevation 占位、未解析 IMAGE token**，避免 UI 展示内部中间态。

| 过滤对象 | 原因 |
|----------|------|
| `lc_source=summarization` | 摘要非用户可见原文 |
| `[State Elevation:` 前缀 | 内部占位 |
| 裸 `[IMAGE:xxx]` | 需转 URL 再展示 |

---

### Q6：访客会话与主站会话 API 有何不同？

**一句话结论：** 访客用 **`guest_conversations.py` + `guest_checkpoints.db`**，路由更少；**不与主站 thread 互通**。

```text
┌────────────┐
│ 8083 guest │
└────────────┘

  8083 guest ──────────────► guest_checkpoints.db

  guest_checkpoints.db ──────────────► 8082 main

  8082 main ──────────────► checkpoints.db

  checkpoints.db ──────────────► (users 可选共用)

```

---

### Q7：个人文件库（my_files）与会话附件（chat_files）区别？

**一句话结论：** **my_files** 是用户级持久文件库；**chat_files** 绑定当前 thread，用 BM25 轻量索引（`chat_file_index.py`），会话删除时一并清理。

| 维度 | my_files | chat_files |
|------|----------|------------|
| 生命周期 | 用户主动管理 | 随会话 |
| 检索 | 库 API + chat_file_search | chat_file_search |
| 索引 | 可持久 | 内存 LRU |

---

### Q8：Agent-KB 绑定表如何工作？

**一句话结论：** `Agent` + `AgentKnowledgeBase` 多对多表定义**默认 kb_ids**；管理 API CRUD；`init_agent_bindings.py` 初始化 NE/CSH 默认绑定。

#### 在本项目中的实现

- `db/models/agent.py`
- `docs/docs_backend/agent_kb_binding_module.md`

---

## L3 高级 🔴

### Q9：删除会话时 checkpoint 删不干净会怎样？

**一句话结论：** 残留 SQLite 行 → **GDPR/隐私风险** + 磁盘涨；残留 MinIO → 存储成本；需 DELETE 级联事务日志 + 定期 audit。

#### 排查

- 对比 MySQL `is_deleted` 与 SQLite `thread_id` 行数
- MinIO 孤儿前缀扫描

---

### Q10：回滚与 ContextPruning state 是否一致？

**一句话结论：** rollback 主要截断 **messages**；`loaded_skills` / `retrieved_evidence` 若仍在 channel_values 中可能**与截断后对话不一致**——需确认 rollback 是否一并清 state 字段（面试可答为潜在边界 case）。

#### 延伸追问

- 回滚后 follow_up_suggestions 缓存是否失效？

---

### Q11：多租户下 Skill 文件如何防路径遍历？

**一句话结论：** `ScopedSkillsProvider` 限定可读目录 + `read_local_skill_file` 校验 skill 名不在 `..` 路径；仅 `skills/public` 下已注册 Skill 可见。

---

### Q12：工件 URL  presigned 与直连 `/artifacts` 如何选？

**一句话结论：** 大文件/临时访问用 **MinIO presigned**；Agent 线程内小文件可走 **artifacts HTTP**；均需鉴权与过期时间。

---

### Q13：用户隔离面试综合：列出 5 个隔离边界

**一句话结论：**

| 边界 | 机制 |
|------|------|
| HTTP | Token / role |
| thread | internal_thread_key |
| 文件 | ScopedStorage 前缀 |
| Skill | ScopedSkillsProvider |
| KB | Agent-KB 绑定 + kb_ids 参数 |

```text
┌────────────────┐  ┌────────────────┐
│ User A         │  │ User B         │
└────────────────┘  └────────────────┘
           │
           ▼
           │
           ▼
┌────────────────┐  ┌────────────────┐
│ TA             │  │ TB             │
└────────────────┘  └────────────────┘
           │
           ▼
           │
           ▼
           │
           ▼
           │
           ▼
┌────────────────┐  ┌──────────────────┐  ┌────────────────┐  ┌──────────────────┐
│ (checkpoint A) │  │ (MinIO prefix A) │  │ (checkpoint B) │  │ (MinIO prefix B) │
└────────────────┘  └──────────────────┘  └────────────────┘  └──────────────────┘

```
