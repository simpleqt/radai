# MD 文件入库接口文档

> **状态**：已实现（2026-07-09 对齐当前代码）
> **设计说明**：[md_ingestion_design.md](./md_ingestion_design.md)
> **重要说明**：当前生产接口只支持“上传 Markdown 文件后入库”，不支持直接 POST Markdown 文本内容的 `/documents/md` 接口。

## 1. 调用流程

```text
1. 上传 .md 文件
2. 启动入库
3. 轮询 task_id 状态
4. 可选：self_ingestion 库通过管理 API 查看 job / repair
```

## 2. 上传 MD 文件

### 2.1 批量上传

```http
POST /api/v1/uploads/batch
Content-Type: multipart/form-data
Authorization: Bearer <token>
```

| 字段 | 类型 | 必填 | 说明 |
| --- | --- | --- | --- |
| `kb_id` | string | 是 | 知识库 ID |
| `category_path` | string | 否 | 分类路径 |
| `files` | file[] | 是 | 文件列表，批量最多 20 个，支持 `.md` |

```bash
curl -X POST 'http://localhost:8082/api/v1/uploads/batch' \
  -H 'Authorization: Bearer <token>' \
  -F 'kb_id=kb-123e4567-e89b-12d3-a456-426614174000' \
  -F 'category_path=技术文档/AI' \
  -F 'files=@/path/to/技术文档.md;type=text/markdown'
```

响应：

```json
{
  "success": true,
  "upload_id": "upload-batch-id",
  "files": [
    {
      "file_id": "file-id",
      "file_name": "技术文档.md",
      "file_size": 12345,
      "minio_path": "pending/kb-xxx/file-id/技术文档.md",
      "status": "pending"
    }
  ],
  "total": 1,
  "message": "批量上传成功，共 1 个文件等待入库"
}
```

上传后 `kb_pending_files.status=0`，文件只进入待入库队列。

### 2.2 分块上传（大文件）

大文件可使用同一路由前缀的分块接口：

```text
POST /api/v1/uploads/init
POST /api/v1/uploads/chunks/{upload_id}/{chunk_index}
POST /api/v1/uploads/complete/{upload_id}
```

`complete` 后同样生成 `kb_pending_files.status=0` 的待入库记录。

### 2.3 管理端单文件上传

管理员也可以使用：

```http
POST /api/v1/knowledge_bases/{kb_id}/documents
```

该接口当前支持 `pdf/docx/txt/md/png/jpg/jpeg`，文件上限 100MB，上传后仍需启动入库。

## 3. 查看待入库文件

```http
GET /api/v1/uploads/pending?kb_id=<kb_id>&status=0
Authorization: Bearer <token>
```

响应：

```json
{
  "items": [
    {
      "file_id": "file-id",
      "kb_id": "kb-xxx",
      "file_name": "技术文档.md",
      "file_size": 12345,
      "file_type": "text/markdown",
      "status": 0,
      "error_message": null,
      "document_date": null,
      "category_path": "技术文档/AI",
      "created_at": "2026-07-09T10:30:00"
    }
  ],
  "total": 1
}
```

`kb_pending_files.status`：

| 值 | 轮询返回 | 含义 |
| --- | --- | --- |
| `0` | `pending` | 已上传，尚未启动入库 |
| `1` | `processing` | 已启动，worker 等待或处理中 |
| `2` | `completed` | 入库完成 |
| `3` | `failed` | 入库失败或 self 链路 blocked |

## 4. 启动入库

```http
POST /api/v1/uploads/ingest
Content-Type: application/json
Authorization: Bearer <token>
```

请求：

```json
{
  "kb_id": "kb-123e4567-e89b-12d3-a456-426614174000",
  "file_ids": ["file-id"],
  "document_date": "2026-07-09",
  "category_path": "技术文档/AI"
}
```

| 字段 | 类型 | 必填 | 说明 |
| --- | --- | --- | --- |
| `kb_id` | string | 是 | 知识库 ID |
| `file_ids` | string[] | 是 | 待入库文件 ID |
| `document_date` | date | 否 | 文档业务日期，`YYYY-MM-DD` |
| `category_path` | string | 否 | 传入时覆盖上传阶段的分类路径；传 `""` 可置空 |
| `options` | object | 否 | 预留字段，当前 worker 不依赖 |

响应：

```json
{
  "success": true,
  "task_id": "task-id",
  "files_count": 1,
  "message": "入库任务已创建，已提交给独立 worker 处理"
}
```

接口会把选中文件更新为：

- `status=1`
- `upload_id=task_id`
- `ingest_job_id=task_id`
- `ingestion_backend=declared_ingestion_backend(kb)`
- `document_date` / `category_path`

## 5. 轮询入库状态

```http
GET /api/v1/uploads/ingest/status/{task_id}
Authorization: Bearer <token>
```

响应：

```json
{
  "task_id": "task-id",
  "kb_id": "kb-xxx",
  "status": "processing",
  "total": 1,
  "completed": 0,
  "failed": 0,
  "progress": 0.0,
  "files": [
    {
      "file_id": "file-id",
      "file_name": "技术文档.md",
      "status": "processing",
      "doc_id": null,
      "error": null
    }
  ]
}
```

整体 `status`：

| 值 | 含义 |
| --- | --- |
| `processing` | 仍有文件未到终态 |
| `completed` | 所有文件成功 |
| `partial` | 至少一个文件失败 |

单文件 `status`：`pending`、`processing`、`completed`、`failed`。

注意：该轮询接口基于 `kb_pending_files`，不会返回 self_ingestion 的细粒度 `SelfIngestJob.stage`。需要细粒度阶段时使用管理员接口。

## 6. self_ingestion 管理接口

管理员接口前缀为：

```text
/api/v1/admin/self-ingestion
```

常用端点：

| 端点 | 用途 |
| --- | --- |
| `GET /health` | queue_depth 与 MySQL / Milvus / Redis / Nebula / Embedding / LLM / VLM 配置健康检查 |
| `GET /jobs?kb_id=&status=` | 查询 self 入库 job 列表 |
| `GET /jobs/stats?kb_id=` | job 状态统计 |
| `POST /jobs/{job_id}/repair` | 修复单个 job |
| `POST /repair/scan` | 扫描并修复可重放 job |
| `POST /kb/{kb_id}/switch-backend` | 切换 KB 后端开关 |
| `POST /shadow-rebuild` | 将 legacy 文档 shadow 入队到 self，不修改 legacy 索引 |

`SelfIngestJob.status` 使用小写枚举：`pending`、`processing`、`committing`、`completed`、`partial_success`、`failed`、`repairing`。

## 7. 完整 Python 示例

```python
import time
import requests

BASE = "http://localhost:8082/api/v1"
HEADERS = {"Authorization": "Bearer <token>"}
KB_ID = "kb-123e4567-e89b-12d3-a456-426614174000"

with open("技术文档.md", "rb") as f:
    upload_resp = requests.post(
        f"{BASE}/uploads/batch",
        headers=HEADERS,
        files={"files": ("技术文档.md", f, "text/markdown")},
        data={"kb_id": KB_ID, "category_path": "技术文档/AI"},
        timeout=60,
    )
upload_resp.raise_for_status()
uploaded = upload_resp.json()
file_ids = [item["file_id"] for item in uploaded["files"]]

ingest_resp = requests.post(
    f"{BASE}/uploads/ingest",
    headers={**HEADERS, "Content-Type": "application/json"},
    json={
        "kb_id": KB_ID,
        "file_ids": file_ids,
        "document_date": "2026-07-09",
    },
    timeout=30,
)
ingest_resp.raise_for_status()
task_id = ingest_resp.json()["task_id"]

while True:
    status_resp = requests.get(
        f"{BASE}/uploads/ingest/status/{task_id}",
        headers=HEADERS,
        timeout=30,
    )
    status_resp.raise_for_status()
    status = status_resp.json()
    print(status["status"], status["progress"])
    if status["status"] in {"completed", "partial"}:
        break
    time.sleep(2)
```

## 8. 内部机制

legacy Markdown：

```text
PendingIngestUseCase
  -> route=legacy-md-direct
  -> MinIO 下载 bytes
  -> UTF-8 decode
  -> RAGService.process_md_content()
  -> MDIngestor
  -> KnowledgeBaseDocument 元数据补齐
  -> kb_pending_files.status=2
```

self Markdown：

```text
PendingIngestUseCase
  -> route=self_ingestion
  -> SelfIngestionPipeline
  -> markdown_parser.py / BlockBuilder
  -> LLM/VLM 抽取
  -> StagingPayload: staging/{kb_id}/{job_id}/payload.json
  -> CommitWriter
     -> MySQL kb_self_chunks staged
     -> Milvus chunks_v2 invisible
     -> MySQL visible
     -> Milvus visible/entities_v2 + Redis BM25/metadata + NebulaGraph v2
  -> completed 或 partial_success
```

## 9. 运维注意事项

| 项 | 当前实现 |
| --- | --- |
| 文件大小 | `/uploads/batch` 默认 5GB，可配置；管理端 `/knowledge_bases/{kb_id}/documents` 为 100MB |
| 编码 | legacy `.md` 按 UTF-8 decode；self Markdown parser 也应使用 UTF-8 文本 |
| Worker | `python -m backend.worker.ingest_worker` 或 `bash scripts/ingest_worker_service.sh start` |
| 并发锁 | Redis `kb_ingest_lock:{file_id}`，TTL 7200 秒 |
| blocked | self KB 未满足开关/白名单时 `status=3`，不会回退 legacy |
| 删除 | 按文档 `ingestion_backend` 分流；self 删除清 MySQL 可见性、Redis BM25、Milvus，并 best-effort 处理 Nebula |
