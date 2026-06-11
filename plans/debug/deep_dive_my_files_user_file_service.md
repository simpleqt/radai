# 深度审查：个人文件夹 + UserFile 双轨模型

> 文件：`api/routers/my_files.py`、`api/routers/chat_files.py`、`api/routers/files.py`、`services/user_file_service.py`  
> 对照：[个人文件夹文件管理与上传复用设计方案.md](../个人文件夹文件管理与上传复用设计方案.md)  
> 只读审查。

---

## 1. 数据模型双轨

`UserFile` 同时存在两种「与会话关联」方式：

| 机制 | 字段/表 | 谁写入 |
|------|---------|--------|
| **A. 列级** | `user_files.thread_id` | `chat_files` 上传、`user_file_service` 登记工件 |
| **B. 关联表** | `session_files(session_id, file_id)` | `chat_files` 上传、`my_files` attach |

```text
┌───────────────────────────────────────────────────────────────┐
│                            个人文件夹路径                            │
│ POST /api/v1/files/upload  │  UserFile 无 thread_id  │  U2  │  │
└───────────────────────────────────────────────────────────────┘
                                │
                               ▼
┌───────────────────────────────────────────────────────────────┐
│                            对话直传路径                             │
│ C1  │  UserFile.thread_id=session  │  SessionFile             │
└───────────────────────────────────────────────────────────────┘
                                │
                               ▼
┌───────────────────────────────────────────────────────────────┐
│                           列表 API 分裂                           │
│ GET /api/v1/my-files  │  GET /api/v2/chat-files/thread  │  GE │
└───────────────────────────────────────────────────────────────┘

连接:
  POST /api/v1/files/upload ──────► UserFile 无 thread_id
  U2 ──────► SessionFile
  U2 ──────► ChatFileIndex 若 long_doc
  C1 ──────► UserFile.thread_id=session
  C1 ──────► SessionFile
  GET /api/v1/my-files ──────► UserFile 无 thread_id
  GET /api/v2/files?thread_id ──────► UserFile 无 thread_id

```

**风险 MF-1（medium）**：仅从个人文件夹上传、未 attach 的文件，`GET /api/v2/files?thread_id=` **查不到**（`list_active_files_for_thread_for_user` 只过滤 `uf.thread_id`）。

**风险 MF-2（low）**：同一文件 attach 后，列表 API 三套口径可能数量不一致。

---

## 2. `my_files.py`（436 行）端点审查

| 端点 | 实现质量 | 问题 |
|------|----------|------|
| POST `/api/v1/files/upload` | 解析 + MinIO + UserFile | 不写 `thread_id`、不写 SessionFile — **符合**「先进个人库」 |
| GET `/api/v1/my-files` | keyword/source/file_type 过滤 | **MF-3** `file_type` 未参与 `count_stmt`；**MF-4** total 用 `len(scalars().all())` 全表拉内存 |
| PUT rename | owner 校验 | ok |
| DELETE soft | status=deleted | **MF-5** `SessionFile` 置 `session_id=""` 而非 DELETE，可能留下脏关联 |
| POST attach | 建 SessionFile + long_doc 索引 | **MF-6** 未校验 `session_id` 是否属于当前用户会话 |
| POST generated-files/save | 仅元数据入库 | **MF-7** 无内容解析、无存储对象存在性校验；`metadata.content` 恒空 |

### 2.1 与设计方案对齐

| 方案能力 | 状态 |
|----------|------|
| 上传默认进个人库 | ✅ upload |
| 从我的文件 attach 到会话 | ✅ attach + 索引 |
| 关键词搜索 | ⚠️ 仅 `display_name LIKE` |
| 标签/全文检索 | ❌ |
| 生成文件自动归档 | ⚠️ `save_generated_file` 骨架在，无 Agent 自动调用链审计 |
| 与 chat_files 复用 Parser | ✅ 共用 `ChatFileParser` |

---

## 3. `user_file_service.py`（675 行）

### 3.1 职责分区

| 函数 | 用途 |
|------|------|
| `list_active_files_for_thread_for_user` | 按 `user_files.thread_id` 列列表 |
| `register_local_artifact_for_thread` | 沙箱 outputs 登记（async） |
| `register_minio_artifact_for_thread` | MinIO 工件登记 |
| `*_sync` | 同步版（工具回调线程） |
| `resolve_user_file_for_download` | owner 或 `user_file_permissions` 授权 |
| `write_download_audit` / `list_download_audit_for_user` | 下载审计 |

### 3.2 安全（对比 chat_files）

| API | 归属校验 |
|-----|----------|
| `files.py` → `resolve_user_file_for_download` | ✅ owner + grant |
| `chat_files.get_file_meta` | ❌ 无 user（见 [deep_dive_chat_files_index.md](./deep_dive_chat_files_index.md) CF-1） |
| `agent.py` import `get_file_meta` | ❌ 同上 |

**结论**：下载链路安全；**Agent 注入链路**与 **REST meta** 不一致。

### 3.3 质量

| ID | 严重度 | 问题 |
|----|--------|------|
| UFS-1 | low | 大量 raw SQL 与 ORM 路径并存，迁移时易漂移 |
| UFS-2 | low | `register_*` 与 `my_files` 上传路径字段填充不一致（thread_id 有无） |
| UFS-3 | info | `user_file_permissions` 表已支持共享，方案 V1 声明「不共享」— 代码超前 |

---

## 4. `metadata_` 存全文

个人上传与对话上传均把 `parse_result.content`（及 multimodal base64）写入 `UserFile.metadata` JSON。

| 影响 |
|------|
| 大行、备份膨胀 |
| long_doc attach 时从 metadata 重建索引 — **依赖 DB 有 content** |
| 删除会话未清 metadata 中敏感内容 |

---

## 5. 扩展名白名单

`my_files` / `chat_files` / `uploads` 三套 `ALLOWED_EXTENSIONS` — 见 [cross_cutting_duplication.md](./cross_cutting_duplication.md)。

---

## 6. 整改建议（设计层）

1. **统一列表**：`list_thread_files(user_id, thread_id)` 以 `session_files` 为准，可选 UNION `thread_id` 列。  
2. **attach**：校验 `Conversation` 存在且 `user_id` 匹配。  
3. **软删**：DELETE `session_files` 或级联，避免 `session_id=""`。  
4. **Agent**：`get_file_meta(fid, user_id)` 与 `files.py` 共用 `resolve_user_file_for_download`。  
5. **分页 count**：`SELECT COUNT(*)` 替代 `len(all())`。  
6. **生成文件**：save 时校验 bucket/object 归属前缀 `chat_files/{user_id}/`。

---

## 7. 测试矩阵（建议）

| 场景 | 期望 |
|------|------|
| 个人库上传 → attach → agent 带 file_ids | 可检索 long_doc |
| 仅个人库上传未 attach | `chat-files` 列表无；`my-files` 有 |
| 软删后 attach 同一 file_id | 404 |
| 下载他人 file_id | 403（files.py） |
| agent 注入他人 file_id | 403（修复后） |
