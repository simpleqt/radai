# 第五遍：`backend/api/routers/my_files.py`

**行数**：~436  
**依赖 services**：`ChatFileParser`, `get_index_manager`, `clean_surrogates`

---

## 端点矩阵

| 方法 | 路径 | 说明 |
|------|------|------|
| POST | `/api/v1/files/upload` | 个人库上传，**无** SessionFile |
| GET | `/api/v1/my-files` | 列表+筛选 |
| PUT | `/api/v1/my-files/{file_id}` | 重命名 |
| DELETE | `/api/v1/my-files/{file_id}` | 软删 + 污染 session_files |
| POST | `/api/v1/sessions/{session_id}/files/attach` | 绑会话 + 建索引 |
| POST | `/api/v1/generated-files/save` | 生成文件入库 |

---

## 关键问题（路由层）

| ID | 级别 | 描述 |
|----|------|------|
| MF-01 | P0 | DELETE 使用 `session_id=""` 而非 DELETE 行 |
| MF-02 | P0 | 软删不清理 ChatFileIndex |
| MF-03 | P1 | `list` total 用 `len(all())` 全表加载 |
| MF-04 | P1 | attach 无 `last_used_at` |
| MF-05 | P1 | 上传路径 `chat_files/{user}/...` 命名混淆 |
| MF-06 | P2 | `file_type` 过滤靠文件名后缀 |
| MF-07 | P2 | attach 索引异常仅 log |
| MF-08 | P2 | `generated-files/save` metadata 空 content |

---

## attach 流程（与 services）

```
SELECT UserFile active
→ dup SessionFile?
→ INSERT SessionFile
→ if long_doc and metadata.content:
      index_manager.get_or_create(session_id)
      index.add_document(...)
→ commit
```

- `session_id` 来自路径，与 chat 的 `thread_id` 应为同一标识体系。  
- **不**更新 `UserFile.thread_id`。

---

## 与 chat_files 对比

| | my_files upload | chat_files upload |
|--|-----------------|-------------------|
| SessionFile | ✗ | ✓ |
| Index | ✗ | ✓ long_doc |
| 失败 | 422 整请求失败 | 单文件 failed 项 |

---

## 建议 PR

与 [P0 PR-4](../../../backend_services_p0_pr_guide.md#pr-4个人文件软删除--会话索引一致性p0-06) 合并修改本文件。

附加：

- `COUNT(*)` 分页  
- attach 响应 `{ indexed: bool, chunk_count }`  
- `UPDATE user_files SET last_used_at=NOW()`  

---

*第五遍 — my_files 路由*
