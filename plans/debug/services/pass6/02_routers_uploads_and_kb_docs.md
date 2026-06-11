# 第六遍：`uploads.py` 与 `knowledge_base_documents.py`

与 `startup_task_processor` / `directory_service` / `PendingFile` 链路相关。

---

## A. `backend/api/routers/uploads.py`

### 职责

| 能力 | 说明 |
|------|------|
| 分块上传 | init → chunk → complete |
| 批量上传 | batch |
| 待入库列表 | pending CRUD |
| 启动入库 | `POST /ingest` → status=0→1 |
| 任务状态 | `_ingest_tasks` **进程内存** |

### 与 StartupTaskProcessor 关系

```
POST /api/v1/ingest  →  DB status=1
         ↓
   ingest_worker（推荐）  或  StartupTaskProcessor（可选）
         ↓
   PendingIngestProcessor.process_file
```

文档响应写「已提交给独立 worker」→ 与 `STARTUP_TASK_PROCESSOR_ENABLED` 默认 false **一致**。

### 问题表

| ID | 级别 | 描述 |
|----|------|------|
| UP-01 | P0 | 与 pass3：三消费端互斥（文档） |
| UP-02 | P0 | 缓存 `file_name` 冲突（processor 侧） |
| UP-03 | P1 | `_ingest_tasks` 多 worker 内存不一致，status API 不可信 |
| UP-04 | P1 | `MAX_FILE_SIZE=100MB` vs chat/my_files 50MB |
| UP-05 | P2 | 每轮 Redis connect/disconnect（processor） |
| UP-06 | P2 | complete 上传失败仍可能留 chunk session 内存 `_chunk_sessions` |

### ALLOWED_EXTENSIONS

含 `.doc/.xls/.ppt` 等；入库走 RAG multimodal，**不经过** `ChatFileParser`——与对话上传解析链分离。

---

## B. `knowledge_base_documents.py`

### directory_service 调用点

| 位置 | 方法 |
|------|------|
| `GET .../documents/tree` (deprecated) | `get_directory_tree` |
| 文档改 category | `update_doc_counts_for_path` |
| 批量移动 | 同上 |
| `POST .../rename-category` | `rename_directory` |

### P1：rename-category 与多段 new_path

```python
success, affected, final_path = await directory_service.rename_directory(
    old_path=old_path,
    new_name=new_path.split("/")[-1],  # 仅最后一段
)
```

- 文档示例 `new_path="PRL"`（单段）OK。
- 若客户端传 `new_path="期刊/2024"`，实际只把 `old_path` 末段改为 `"2024"`，**不会**变成 `期刊/2024`。
- 与接口说明「前缀替换」部分场景**不符**。

**建议**：废弃此 API 或改为调用 `move_directory` + 完整路径逻辑；引导只用 `directories/{path}/rename`。

### P1：deprecated tree 与 directories 树重复

- `GET /documents/tree` deprecated
- `GET /knowledge_bases/{kb_id}/directories` 正式

两套均调 `get_directory_tree`，`uncategorized_count` 计算方式不同（kb_docs 版额外插入「未分类」节点）→ 前端若混用可见不一致。

### P2：KB 直传 allowed_extensions 与 uploads 不一致

| 路由 | 扩展名 |
|------|--------|
| `upload_document_to_kb` | pdf, docx, txt, md, png, jpg, jpeg |
| `uploads` batch | + doc, xls, ppt 等 |

同 KB 两条上传路径能力不一致。

### P2：upload_document_to_kb 全量 read 内存

`content = await file.read()` 无 100MB 校验（uploads 有 validate_file）。

### P2：batch_move 后 doc_counts

对每个 `old_path` 调 `update_doc_counts_for_path`，未 `commit` 在中间——`directory_service` 内每次方法末尾 commit，多次 commit OK。

### P3：delete 批量未触发目录计数刷新

`batch_delete_documents` 删文档后**未**调 `recalculate_doc_counts` 或 `update_doc_counts_for_path` → 目录树计数陈旧直到手动 recalculate。

---

## C. 知识库 vs 对话文件边界（架构）

```text
┌───────────────────────────────────────────────────────────────┐
│                              对话域                              │
│ chat_files / my_files  │  ChatFileParser  │  ChatFileIndex 内存 │
└───────────────────────────────────────────────────────────────┘
                                │
                               ▼
┌───────────────────────────────────────────────────────────────┐
│                             知识库域                              │
│ uploads / kb_docs upload                                      │
└───────────────────────────────────────────────────────────────┘

```

审查 `backend/services` 时勿将 KB 入库问题误标为 ChatFile 问题。

---

## 测试建议

| 用例 | 组件 |
|------|------|
| ingest status=1 仅一处 consumer 成功 | uploads + worker |
| rename-category new_path 多段 | kb_docs |
| 批量删文档后 tree doc_count | kb_docs + directory |

---

*第六遍 — uploads & kb_docs*
