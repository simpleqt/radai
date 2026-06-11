# P0 修复 PR 拆分指南

> 基于 [backend_services_fix_backlog.md](./backend_services_fix_backlog.md) 的 P0 项。  
> 目标：小 PR、可独立评审、降低回归风险。  
> **不含代码**，仅拆分与依赖说明。

---

## 依赖关系图

```text
┌────────────┐
│ P02        │
└────────────┘

  P02 ──────────────► P0-01 入库互斥

  P0-01 入库互斥 ──────────────► P06

  P06 ──────────────► P0-04 索引锁

  P0-04 索引锁 ──────────────► P03

```

---

## PR-1：入库缓存文件名唯一化（P0-02）

**范围**

- `backend/services/startup_task_processor.py`
- `backend/worker/components/ingest_pending_processor.py`

**改动要点**

- `cache_file_path = cache_dir / f"{file_id}_{safe_basename}"`
- `safe_basename` 仅保留文件名、去掉路径成分

**测试**

- 并发处理两个同名 `report.pdf`、不同 `file_id`，入库内容不串。

**依赖**：无  
**可并行**：与 PR-2、PR-3

---

## PR-2：入库消费端互斥与文档（P0-01）

**范围**

- `docs/docs_backend/` 或 `plans/` 运维说明
- 可选：`http_server.py` 启动日志强化警告
- **不**合并两套 processor 代码（可后续重构）

**改动要点**

- 明确：生产使用 `ingest_worker` **或** `STARTUP_TASK_PROCESSOR_ENABLED=true`，禁止同时开。
- 核对 `uploads.start_ingest` 注释与部署一致。

**测试**

- 单 worker 跑 ingest，双实例时 Redis 锁仅一方成功（集成测试可选）。

**依赖**：无  
**建议**：与 PR-1 同发版

---

## PR-3：目录 rename/move 安全路径更新（P0-05）

**范围**

- `backend/services/directory_service.py`（`rename_directory`、`move_directory`）

**改动要点**

- 替换 `func.replace` 为「精确匹配 + 前缀 `old_path/`」的 SQL 或 ORM 逐条更新。
- 增加单测：`a`→`b` 不误伤 `ab/child`。

**测试**

- 单元测试 3–5 个路径边界用例。

**依赖**：无  
**风险**：知识库数据迁移类，需 staging 验证

---

## PR-4：个人文件软删除 + 会话索引一致性（P0-06）

**范围**

- `backend/api/routers/my_files.py`
- 可选薄封装：`backend/services/chat_file_index.py` 增加 `remove_file_from_all_sessions(file_id)` 或路由内循环

**改动要点**

1. 软删除：`DELETE FROM session_files WHERE file_id=?`（或 ORM `delete`），禁止 `session_id=""`。
2. 查询该 `file_id` 所有 `session_id`，对每个 session 调用 `get_index_manager().get(sid)` → `remove_document(file_id)`。
3. 可选：块数为 0 时 `manager.remove(session_id)`。

**测试**

- attach long_doc → search 有结果 → 软删除 → 同 session search 无该文件内容。
- `session_files` 无空 `session_id` 行。

**依赖**：无  
**前端**：无需改（除非展示「已失效」）

---

## PR-5：实现 chunk_overlap（P0-03）

**范围**

- `backend/services/chat_file_index.py` — `split_text`、`add_document`

**改动要点**

- 滑动窗口：上一块尾部 overlap 字符拼入下一块头部（尊重句界可选）。
- 或删除未使用参数并更新 docstring / `personal_files_module.md`。

**测试**

- 构造跨边界关键词，overlap 前后检索命中。

**依赖**：建议早于 PR-6

---

## PR-6：ChatFileIndexManager 并发锁（P0-04）

**范围**

- `backend/services/chat_file_index.py` — `ChatFileIndexManager`、`ChatFileIndex`

**改动要点**

- `asyncio.Lock`：`get_or_create` / `add_document` / `remove_document` / `search` 同 thread 串行化；或每 thread_id 一把锁。
- 模块 docstring：多 Uvicorn worker 索引不共享。

**测试**

- 并发 `add_document` 同 thread 块数正确。
- 文档说明多 worker 需 sticky session 或接受限制。

**依赖**：可与 PR-5 同 PR

---

## PR-7：files 安全与路由（P0-07、P0-08）

**范围**

- `backend/api/routers/files.py`

**改动要点**

1. `export-docx` 增加 `Depends(get_current_user)`，`content` max length（如 500k 字符）。
2. 将 `@router.get("/download-audit")` 移到 `@router.get("/{file_id}/meta")` **之前**。

**测试**

- 无 token export → 401  
- `GET /files/download-audit` → 200  

**可并行**：Hotfix A

---

## 建议合并策略

| 发版 | PR |
|------|-----|
| Hotfix 0 | PR-7（安全，可最先合） |
| Hotfix 1 | PR-1 + PR-2（入库） |
| Hotfix 2 | PR-3（目录数据安全） |
| Hotfix 3 | PR-4（文件删除一致性） |
| 迭代 1 | PR-5 + PR-6（检索质量） |

---

## 回归清单（每个 PR 共用）

- [ ] `chat_files` 上传 short/long/multimodal 各 1
- [ ] `my_files` attach + 发送消息
- [ ] `lightweight_qa` 带 long_doc 提问
- [ ] KB 目录树加载、rename（staging）
- [ ] `ingest_worker` 单文件入库 status 0→1→2

---

*2026-05-30*
