# MD 文件入库设计说明

> **状态**：已实现（2026-07-09 对齐当前代码）
> **当前入口**：上传 `.md` 文件 → `POST /api/v1/uploads/batch` 或分块上传 → `POST /api/v1/uploads/ingest` → ingest worker 消费 `kb_pending_files.status=1`。
> **未实现接口**：不存在 `POST /api/kb/{kb_id}/documents/md` 或 `/md/batch` 这类“直接提交 Markdown 文本”的生产路由；如需接入外部系统，应先按文件上传链路写入 MinIO 与 `kb_pending_files`。

## 1. 当前链路

Markdown 入库与 PDF/DOCX/TXT 使用同一套待入库队列。上传阶段只完成对象存储和 `kb_pending_files` 记录，真正入库由独立 worker 或启动任务处理器消费。

```text
POST /api/v1/uploads/batch
  -> MinIO: pending/{kb_id}/{file_id}/{file_name}
  -> kb_pending_files(status=0, ingestion_backend=declared_backend)

POST /api/v1/uploads/ingest
  -> kb_pending_files(status=1, upload_id=task_id, ingest_job_id=task_id)

PendingIngestUseCase
  -> Redis lock: kb_ingest_lock:{file_id}
  -> resolve_ingestion_backend()
     ├─ legacy_lightrag + .md -> legacy-md-direct -> RAGService.process_md_content() -> MDIngestor
     ├─ legacy_lightrag + other -> legacy-mineru -> RAGService.process_multimodal()
     ├─ self_ingestion -> SelfIngestionPipeline.ingest_pending_file()
     └─ blocked -> status=3，不回退 legacy
```

## 2. 后端分流规则

| KB / Pending 后端 | Markdown 处理方式 | 说明 |
| --- | --- | --- |
| `legacy_lightrag` | `PendingIngestUseCase._process_md_file()` | 通过 MIME `text/markdown` / `text/x-markdown` 或 `.md` 扩展名识别，跳过 MinerU，读取 MinIO 内容后调用 `RAGService.process_md_content()` 与 `MDIngestor`。 |
| `self_ingestion` | `SelfIngestionPipeline` | 走 `ParserRouter` / `markdown_parser.py` → `BlockBuilder` → LLM/VLM 抽取 → Staging → `CommitWriter`，不使用 legacy `MDIngestor`。 |
| `blocked` | 不入库 | KB 声明 `self_ingestion` 但全局开关、KB 开关或白名单不满足时，`kb_pending_files.status=3`，`error_message` 写明 blocked，避免污染 legacy 索引。 |

Markdown 文件判定来自 `backend/application/ingestion/pending_ingest.py`：

- `file_type` 或 `mime_type` 为 `text/markdown`、`text/x-markdown`
- 或文件名以 `.md` 结尾

## 3. 数据状态

### 3.1 `kb_pending_files.status`

| 值 | API 状态 | 含义 |
| --- | --- | --- |
| `0` | `pending` | 已上传，尚未启动入库 |
| `1` | `processing` | 已启动，等待或正在被 worker 消费 |
| `2` | `completed` | 入库完成或 self 链路判定 `skip_existing` |
| `3` | `failed` | 入库失败或 blocked，详情见 `error_message` |

上传接口会在记录中写入 `source_hash`、`object_bucket`、`object_key`、`original_uri`、`mime_type`、`file_ext`、`ingestion_backend`、`category_path` 等元数据。`POST /uploads/ingest` 会更新 `document_date`、`upload_id=task_id`、`ingest_job_id=task_id`、`status=1`，并按 KB 当前声明锁定 `ingestion_backend`。

### 3.2 legacy 文档状态

legacy Markdown 入库成功后，`KnowledgeBaseDocument` 会被补齐对象存储元数据，并设置：

- `ingestion_backend=legacy_lightrag`
- `doc_status=visible`
- `storage_status=1`
- `committed_at`、`visible_at`、`ingest_finished_at`

### 3.3 self_ingestion 文档状态

self 链路使用稳定 `doc_id` 与 `commit_version`，同内容且已有可见文档会 `skip_existing`。新版本提交时旧版本在新版本可见前保持可查，切换后旧 chunk 变为 `superseded`。

关键状态来自 `backend/rag/ingestion/states.py`：

| 对象 | 状态 |
| --- | --- |
| `SelfIngestJob.status` | `pending`、`processing`、`committing`、`completed`、`partial_success`、`failed`、`repairing` |
| `SelfIngestJob.stage` | `started`、`parsing`、`chunking`、`extracting`、`embedding`、`staging`、`committing_mysql`、`committing_milvus`、`committing_redis`、`committing_nebula`、`marking_visible`、`committed`、`failed` |
| `SelfIngestChunk.doc_status` / `KnowledgeBaseDocument.doc_status` | `staged`、`visible`、`active`、`superseded`、`deleted`、`failed`、`archived` |
| `commit_status` | `staged`、`writing`、`committed`、`failed` |

外部索引补写失败的细分阶段（如 `milvus_visible`、`milvus_entities`、`redis`、`nebula`）记录在 `SelfIngestJob.metrics_json.external_failures` 与 commit log 中，repair worker 依据这些名称从 staging payload 重放。

## 4. self_ingestion 的 Staging / Commit 要点

self Markdown 入库不直接写可见索引，而是先生成 `StagingPayload`：

```text
MinIO: staging/{kb_id}/{job_id}/payload.json
MySQL: kb_self_ingest_jobs.staging_uri
Redis: {kb_code}:v2:commit_log:{job_id}
```

`CommitWriter` 是四类存储的唯一提交入口，当前顺序为：

1. MySQL 写入 `kb_self_chunks` staged/invisible。
2. Milvus `{kb_code}_chunks_v2` 写入 chunk 向量，`is_visible=false`。
3. MySQL 标记新 chunk/document 为 `visible`，旧版本 chunk 置 `superseded`，`kb_pending_files.status=2`。
4. 并行补外部索引：Milvus chunk visible、Milvus `{kb_code}_entities_v2`、Redis BM25/metadata、NebulaGraph `entity_v2` / `relation_v2` / `evidence_v2`。
5. 外部索引若部分失败，job 为 `partial_success`，repair worker 可从 staging payload 重放失败阶段。

MySQL 当前保存 job、document、chunk 与 chunk extra 元数据；实体、关系、证据与质量明细不写 MySQL 图谱表，正式图谱进入 NebulaGraph v2，实体向量进入 Milvus entities_v2，candidate/dropped 审计保留在 staging payload / job metrics。

## 5. 与 PDF / DOCX / TXT 的关系

| 文件类型 | legacy_lightrag | self_ingestion |
| --- | --- | --- |
| `.md` | 跳过 MinerU，直接 `MDIngestor` | `markdown_parser.py` → BlockBuilder |
| `.txt` | `process_multimodal()` 路径 | `text_parser.py` → BlockBuilder |
| `.pdf` | MinerU / RAGAnything | `mineru_pdf_parser.py`，失败时非 PDF 才允许纯文本回退 |
| `.docx` | `process_multimodal()` 路径 | `docx_parser.py` / pipeline DOCX 解析 |
| 图片 | legacy 多模态处理 | image parser + VLM/文本 LLM 抽取 |

## 6. 设计约束

- 上传接口允许的文件类型以 `backend/api/routers/uploads.py::ALLOWED_EXTENSIONS` 为准；批量上传最多 20 个文件，默认单文件上限为 `MAX_FILE_SIZE`（默认 5GB，可由配置覆盖）。
- 管理端单文件上传 `/api/v1/knowledge_bases/{kb_id}/documents` 是管理员接口，当前限制 100MB，支持 `pdf/docx/txt/md/png/jpg/jpeg`。
- 入库 worker 必须使用 `PendingIngestUseCase` 统一分流，不能绕过 `resolve_ingestion_backend()`。
- self 链路 blocked 时不得回退 legacy。
- 删除、图谱、分块预览、检索都必须按文档或 KB 的 backend 路由。

## 7. 相关实现

- `backend/api/routers/uploads.py`
- `backend/api/routers/knowledge_base_documents.py`
- `backend/application/ingestion/pending_ingest.py`
- `backend/worker/components/ingest_pending_processor.py`
- `backend/rag/service.py`
- `backend/rag/raganything/md_ingestor.py`
- `backend/rag/ingestion/pipeline.py`
- `backend/rag/ingestion/commit_writer.py`
- `backend/rag/ingestion/staging_manager.py`
