# 第三遍补充：`startup_task_processor.py`

## 1. 知识库入库三条并行链路（架构级）

```text
┌──────────────────────────────────────┐
│ upload                               │
└──────────────────────────────────────┘
           │
           ▼
┌──────────────────────────────────────┐
│ ingest                               │
└──────────────────────────────────────┘
           │
           ▼
           │
           ▼
           │
           ▼
┌────────────────┐  ┌────────────────┐  ┌────────────────┐
│ W1             │  │ W2             │  │ W3             │
└────────────────┘  └────────────────┘  └────────────────┘
           │
           ▼
           │
           ▼
           │
           ▼
           │
           ▼
           │
           ▼
           │
           ▼
┌────────────────────┐  ┌─────────────────────────────────────────────────────┐
│ (kb_pending_files) │  │ rag_service.process_multimodal / process_md_content │
└────────────────────┘  └─────────────────────────────────────────────────────┘

```

| 组件 | 启用条件 | 处理 status | Redis 锁 |
|------|----------|-------------|----------|
| `worker/ingest_worker.py` | 独立部署 | 1 | 是（组件内） |
| `PendingIngestProcessor` | worker 调用 | 1 | 是 |
| `StartupTaskProcessor` | `STARTUP_TASK_PROCESSOR_ENABLED=true` | 1 | 是 |

**第三遍结论（P0-ARCH）**：三套逻辑重复；`uploads.start_ingest` 注释写「已提交给独立 worker」，与 API 内嵌 StartupTaskProcessor **可能双跑**。

**运维建议**：生产只启用 **一种** consumer；默认 API 内嵌为 `false` 是正确的。

---

## 2. 状态流转（权威）

| 阶段 | status | 触发方 |
|------|--------|--------|
| 上传完成 | 0 | `uploads` complete/batch |
| 用户点击入库 | 1 | `POST /ingest` |
| 成功 | 2 | worker / startup processor |
| 失败 | 3 | 异常处理 |

`StartupTaskProcessor` **不处理 status=0**；无人调 `start_ingest` 则文件永不入库。

---

## 3. 与 `PendingIngestProcessor` 行为差异

| 能力 | StartupTaskProcessor | PendingIngestProcessor |
|------|----------------------|------------------------|
| MD 直接入库 | ✗ 一律 multimodal | ✓ `process_md_content` |
| PDF | `process_multimodal` | 同左 |
| 缓存文件名 | `file_name` | `file_name`（**同样冲突**） |
| 批量拉取 | 每轮最多 100 | worker 按 file_id 列表 |

若同时启用 Startup 与 ingest_worker，MD 文件在 Startup 路径可能走 **MinerU 多模态** 而非 MD 直传，行为不一致。

---

## 4. `initialize` 与 RAG 预热

`http_server` lifespan 中：

- `STARTUP_RAG_PREWARM_ENABLED` 可能并行预热 RAG；
- `StartupTaskProcessor.initialize()` 调用 `get_rag_service()`。

需确认双重初始化是否线程安全（通常单例可接受）。

---

## 5. 失败与重试

| 机制 | 实现 |
|------|------|
| `retry_count` 字段 | **未使用** |
| status=3 后再入库 | 需 API 将 status 改回 1 |
| Redis 锁 7200s | 崩溃后 2h 内无法重试 |

---

## 6. 资源消耗模型

单文件处理：

1. MinIO 全量读入内存  
2. 写 `data/cache/{file_name}`  
3. `process_multimodal`（CPU/GPU/外部 OCR）  
4. 删缓存  

并发 `max_concurrent_files`（默认 5）× 大 PDF → 内存峰值 ≈ 5 × 文件大小（若 RAG 内部再复制）。

---

## 7. 第三遍新增问题

### P0-ARCH 三消费端重复

见第 1 节；应在架构文档中标注**互斥部署**。

### P1-NEW 与 `uploads` 任务状态内存 Map

`_ingest_tasks[task_id]` 存于 API 进程内存，多 worker 下 `GET ingest/status` **不一致**；与 `StartupTaskProcessor` 无直接耦合，但同属入库 UX。

### P2-NEW `document_date` 类型

`PendingFile.document_date` 为 `Date`；传入 `process_multimodal(document_date=...)` 需核对 RAG 签名（`str` vs `date`）。

### P2-NEW 成功后未写 `KnowledgeBaseDocument`

入库成功仅 `PendingFile.status=2`；文档行是否由 RAG 内部创建——失败时可能出现 status=2 但 KB 无文档（需 RAG 层验证）。

---

## 8. 启用检查清单

部署 `STARTUP_TASK_PROCESSOR_ENABLED=true` 前：

- [ ] 已关闭独立 `ingest_worker`  
- [ ] 单实例或接受重复拉取 + 依赖 Redis 锁  
- [ ] 修复缓存路径含 `file_id`  
- [ ] `data/cache` 磁盘配额  
- [ ] MinIO bucket 与 `settings.minio_bucket_name` 一致  

---

## 9. 第三遍结论

本模块在**默认关闭**时风险可控；一旦启用，必须与 `ingest_worker` 互斥，并修复与 `PendingIngestProcessor` 的 MD 路由差异及缓存命名。
