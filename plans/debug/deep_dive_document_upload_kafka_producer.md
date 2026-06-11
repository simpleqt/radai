# 深度审查：文档上传 API → Kafka 生产端

> 文件：`api/routers/document_processing/upload_routes.py`、`chunked_upload_routes.py`  
> 消费端见 [deep_dive_kafka_worker_pipeline.md](./deep_dive_kafka_worker_pipeline.md)  
> 只读审查。

---

## 1. 路由概览

| 路由 | 认证 | 存储 | Kafka |
|------|------|------|-------|
| POST `/upload` | `verify_api_key` | `storage_manager.save_file(..., "documents")` | ✅ task + event |
| POST `/batch` | 同上 | **`tempfile.gettempdir()`** 直写 | ✅（失败仅 log） |
| 分块 init/upload/merge | 同上 | `chunked_upload_manager` | merge 后同左 |

前缀由 `document_processing` 包挂载（通常 `/api/v1/documents` 类路径，以 `http_server` 为准）。

---

## 2. 单文件上传（`upload_document`）

### 2.1 流程

1. `validate_file_type`  
2. 读入内存，上限 **100MB**  
3. `storage_manager.save_file` → `doc_id`, `file_path`  
4. `document_crud.create` + `task_crud.create`  
5. `_send_task_to_kafka`  
6. Kafka 失败 → **HTTP 500**（DB 已有记录 — **孤儿任务** UP-1）

### 2.2 `_send_task_to_kafka`

构造：

- `TaskMessage`：`MessageType.TASK_SUBMITTED`，`body.file_path`、`ProcessingOptions`  
- `EventMessage`：`EventType.TASK_CREATED`

经 `get_task_producer().submit_task` + `get_event_producer().send_event`。

---

## 3. 批量上传（`batch_upload_documents`）

| 差异 | 风险 |
|------|------|
| 文件落 **系统临时目录** 而非 `storage_manager` | **UP-2 high**：与单文件路径策略不一致，Worker 多机必挂 |
| Kafka 发送失败只 `logger.error`，仍返回 PENDING | **UP-3 medium**：客户端以为成功，队列无消息 |
| 部分失败混在 `tasks[]` | 需客户端逐条检查 status |

---

## 4. 分块上传（`chunked_upload_routes.py`）

- `init`：校验 `settings.max_file_size`、`upload_expire_hours`  
- 分片写入 `chunked_upload_manager`（内存/磁盘实现见 storage 模块）  
- `merge`：合并后创建 doc/task 并 `_send_task_to_kafka`（与单文件类似）

**审查要点**：merge 后 `file_path` 必须对 Worker 可见；过期 session 清理策略未在本文件体现。

---

## 5. 与 Worker 契约

| 字段 | 生产者 | 消费者 |
|------|--------|--------|
| `task_id` | task_crud | 幂等键（重复 skip） |
| `file_path` | 本地绝对路径 | `Path.exists()` |
| `file_type` | `doc_type.value` | `DocumentProcessingUseCase` |
| `processing_options` | Form 布尔项 | 解析/OCR/RAG 开关 |

---

## 6. 安全与鉴权

| 项 | 说明 |
|----|------|
| API Key | `verify_api_key` — 适合服务间，非浏览器 Bearer |
| 用户隔离 | `auth.user_id` 写入 doc/task |
| 大小限制 | 100MB（单文件）；分块用 settings |

无病毒扫描、无内容审计钩子（产品层决策）。

---

## 7. 问题汇总

| ID | 严重度 | 问题 |
|----|--------|------|
| UP-1 | medium | Kafka 失败后 DB 任务 PENDING 无自动补偿 |
| UP-2 | **high** | batch 使用 tempfile，与单文件 storage_manager 不一致 |
| UP-3 | medium | batch Kafka 失败仍返回成功态任务 |
| UP-4 | low | 全文件读入内存 — 100MB × 并发内存压力 |
| UP-5 | info | `task_id` 在 upload 路由 L68 生成但未使用（doc 用 storage 的 id）— 死代码/混淆 |

---

## 8. 整改建议

1. batch 统一走 `storage_manager.save_file`。  
2. Kafka 失败：标记 task FAILED 或 outbox 重试。  
3. 文档化「API 与 Worker 必须同机或共享卷」。  
4. 大文件优先分块接口，避免 batch 多文件同时读入内存。

---

## 9. 测试建议

- 单文件 upload → Worker 完成 → result topic  
- batch 2 文件 1 超大：部分 FAILED  
- Kafka down：单文件 500 + DB 状态  
- 分块 merge 后 file_path 在 worker 容器内可访问
