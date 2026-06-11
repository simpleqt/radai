# 第三遍补充：`user_file_service.py`

## 1. 数据模型协作

```text
实体关系:

  UserFile ||--o{ SessionFile : file_id
  UserFile ||--o{ user_file_permissions : optional

  ┌─ UserFile ────────────
  │  string file_id PK
  │  string owner_user_id
  │  string thread_id 可空
  │  string source_type
  │  string status
  │  json metadata_
  └────────────────────────

  ┌─ SessionFile ─────────
  │  string session_id
  │  string file_id
  │  string attached_from
  └────────────────────────

  ┌─ user_file_permissions ────
  │  string file_id
  │  string grantee_user_id
  └────────────────────────


```

| 字段 | 个人文件夹上传 | chat_files 上传 | 工件登记 |
|------|----------------|-----------------|----------|
| `thread_id` | **NULL** | 有值 | external_thread_id |
| `metadata_.content` | 常有 | 常有 | 通常无 |
| `size_bytes` | ✓ | ✓ | local✓ / minio✗ |

---

## 2. 函数调用图

```
files 路由
  ├── list_active_files_for_thread_for_user(session, ...)
  ├── resolve_user_file_for_download(session, ...)
  ├── write_download_audit(session, ...)  → 请求结束 commit
  └── list_download_audit_for_user(session, ...)

artifact_utils (同步 Agent 线程)
  └── register_minio_artifact_for_thread_sync(...)
        └── 自建 session + commit

（async register_* 暂无 grep 命中调用方，可能预留）
```

---

## 3. 第三遍：跨模块缺陷（路由联动）

### P0-NEW `my_files` 软删除破坏 SessionFile

```python
del_stmt = update(SessionFile).where(...).values(session_id="")
```

- 非 DELETE，产生 `session_id=""` 脏行。
- **未**调用 `get_index_manager().get(session).remove_document`。
- 已删除文件内容仍可能通过内存索引被 Agent 检索。

**建议**：`DELETE FROM session_files` + 遍历相关 session 清理索引。

---

### P1-NEW `list_active_files_for_thread_for_user` vs 个人文件模型

查询条件：

```sql
WHERE owner_user_id = ? AND thread_id = ? AND status = 'active'
```

个人库上传 `thread_id IS NULL`，**不会出现在** `GET /api/v2/files?thread_id=` 列表。

设计若期望「会话文件 = SessionFile 联结」，应改为 JOIN `session_files`，而非仅 `user_files.thread_id`。

---

### P1-NEW `sha256` 列从未写入

`UserFile` 模型有 `sha256` 字段，本模块 INSERT **均未设置**，去重仅靠路径/MinIO 键。

---

### P2-NEW 权限表与 Session 成员

`resolve_user_file_for_download` 只查 owner + `user_file_permissions`，**不**查用户是否通过 `SessionFile` 绑定了该文件。

若产品期望「会话内成员可读上传文件」，当前**不支持**（除非另写 grant）。

---

### P2-NEW `register_*` 竞态

两请求同时登记同一路径：可能双 INSERT（无唯一索引 on `local_abs_path` / `(bucket, object)`）→ 重复 `file_id`。

**建议**：DB 唯一约束 `(owner, thread, local_abs_path)` active 部分索引。

---

## 4. 安全再评估

| 项 | 结论 |
|----|------|
| 路径穿越 | ✓ `relative_to` |
| IDOR 下载 | ✓ resolve 校验 |
| 审计 | ✓ |
| 列表泄露路径 | ✗ 见 P1 `local_abs_path` |
| 工件登记任意用户 thread | 依赖 `parse_internal_thread_key`，伪造 internal_id 风险取决于 key 格式 |

---

## 5. 同步/异步双实现维护清单

合并时应保持字段一致：

- [ ] `size_bytes` minio 分支  
- [ ] `mime_type` 默认值  
- [ ] `status='active'`  
- [ ] 去重 SQL 条件  
- [ ] commit 时机  

---

## 6. 第三遍结论

下载鉴权实现扎实；**会话文件列表 SQL 模型、软删除与索引一致性、minio size、DB 唯一约束** 为第三遍新增强优先级项。
