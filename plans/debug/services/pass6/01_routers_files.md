# 第六遍：`backend/api/routers/files.py`

**前缀**：`/api/v2/files`  
**依赖 services**：`user_file_service`（鉴权、列表、审计）

---

## 端点一览

| 方法 | 路径 | 鉴权 | 说明 |
|------|------|------|------|
| GET | `` | ✓ `get_current_user` | 按 `thread_id` 列文件 |
| GET | `/{file_id}/meta` | ✓ | 元数据 + 审计 |
| GET | `/{file_id}/download` | ✓ | 本地或 MinIO 流式 |
| GET | `/download-audit` | ✓ | 审计列表 |
| POST | `/export-docx` | **无** | 任意内容转 docx |

---

## P0：export-docx 未鉴权

```python
@router.post("/export-docx")
async def export_docx(payload: ExportDocxRequest):
```

- 无 `Depends(get_current_user)`。
- 前端 `MessageList.vue` 直接 `fetch('/api/v2/files/export-docx')`，无 token。
- **风险**：未授权滥用 CPU/内存生成 docx；潜在 DoS（超大 `content`）。

**修复**：加登录鉴权 + `content` 长度上限 + rate limit。

---

## P1：路由注册顺序导致 download-audit 误匹配

当前顺序：

1. `GET /{file_id}/meta`
2. `GET /{file_id}/download`
3. `GET /download-audit`

请求 `GET /api/v2/files/download-audit` 会先命中 **`file_id=download-audit`** 的 `meta` 路由，返回 404/403 而非审计列表。

**修复**：将 `/download-audit` 注册在 `/{file_id}/*` **之前**。

---

## P1：list_files 与 SessionFile 模型不一致

调用 `list_active_files_for_thread_for_user`：

```sql
WHERE owner_user_id = ? AND thread_id = ? AND status = 'active'
```

个人库文件 `thread_id` 常为 NULL；仅通过 `my_files` attach 的文件在 `session_files` 有绑定但 `user_files.thread_id` 可能仍为空 → **会话文件列表漏项**。

**修复**：改为 JOIN `session_files`（与 `chat_files` GET 一致）。

---

## P2：download 审计在 StreamingResponse 返回前写入

MinIO 分支：`write_download_audit` 在创建 generator **之前**执行，用户断连也会记 allow——可接受。

本地 `FileResponse`：同样先审计再返回——OK。

---

## P2：同步 MinIO `get_object` + generator

在 async 路由中同步迭代 MinIO，高并发阻塞 event loop（与 services 无关，路由层）。

---

## P2：list 返回 `count=len(items)` 非总数

与 `my_files` 类似，无总命中数。

---

## P3：export-docx 与 files 域耦合

导出 Word 非「用户文件」职责，宜迁 `conversations` 或 `reports` 路由。

---

## 测试建议

| 用例 | 期望 |
|------|------|
| POST export-docx 无 token | 401 |
| GET /files/download-audit 带 token | 200 列表 |
| list_files 仅 session_files 有绑定、thread_id 空 | 应出现文件（修复后） |

---

*第六遍 — files.py*
