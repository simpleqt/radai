# MD 文件入库接口文档

> **状态**：✅ **已实现** — 复用 `/api/v1/uploads/batch` 上传，Worker 自动识别 `.md` 并走 `MDIngestor` 直入库。  
> **设计背景**：[md_ingestion_design.md](./md_ingestion_design.md)

## 1. 概述

MD 文件入库复用现有的上传和入库接口，底层自动识别 `.md` 文件，跳过 MinerU 解析步骤，直接入库，大幅提升效率。

**与 PDF 入库的区别**：

| 环节 | PDF | MD |
|------|-----|-----|
| 解析 | MinerU 解析 PDF→MD（5~30 秒） | **跳过**，直接读 MD 内容 |
| 其余流程 | 完全一致 | 完全一致 |

---

## 2. 调用流程

```
① 上传 .md 文件 → ② 启动入库 → ③ 轮询状态直到完成
```

---

## 3. 接口详情

### 3.1 上传 MD 文件

> 复用接口，与上传 PDF 完全一致。

**请求**

```
POST /api/v1/uploads/batch
Content-Type: multipart/form-data
Authorization: Bearer <api_key>
```

**参数（Form 字段）**

| 字段 | 类型 | 必填 | 说明 |
|------|------|------|------|
| `kb_id` | string | 是 | 知识库 ID |
| `category_path` | string | 否 | 分类路径，如 `"技术文档/AI"` |
| `files` | file[] | 是 | 文件列表，支持 `.md` |

**curl 示例**

```bash
curl -X POST 'http://<api_server_ip>:8082/api/v1/uploads/batch' \
  -H 'Authorization: Bearer <api_key>' \
  -F 'kb_id=kb-123e4567-e89b-12d3-a456-426614174000' \
  -F 'category_path=技术文档/AI' \
  -F 'files=@/path/to/技术文档.md' \
  -F 'files=@/path/to/设计说明.md'
```

**响应**

```json
{
  "success": true,
  "upload_id": "upload-abc123",
  "files": [
    {
      "file_id": "file-def456",
      "file_name": "技术文档.md",
      "file_size": 12345,
      "minio_path": "pending/kb-xxx/file-def456/技术文档.md",
      "status": "pending"
    },
    {
      "file_id": "file-ghi789",
      "file_name": "设计说明.md",
      "file_size": 67890,
      "minio_path": "pending/kb-xxx/file-ghi789/设计说明.md",
      "status": "pending"
    }
  ],
  "total": 2,
  "message": "批量上传成功，共 2 个文件等待入库"
}
```

> 上传后文件处于 **待入库** 状态（`status=0`），尚未入库到 RAG 系统。

---

### 3.2 查看待入库文件

**请求**

```
GET /api/v1/uploads/pending?kb_id=<kb_id>&status=0
Authorization: Bearer <api_key>
```

**curl 示例**

```bash
curl -X GET 'http://<api_server_ip>:8082/api/v1/uploads/pending?kb_id=kb-xxx&status=0' \
  -H 'Authorization: Bearer <api_key>'
```

**响应**

```json
{
  "items": [
    {
      "file_id": "file-def456",
      "kb_id": "kb-xxx",
      "file_name": "技术文档.md",
      "file_size": 12345,
      "file_type": "text/markdown",
      "status": 0,
      "error_message": null,
      "document_date": null,
      "category_path": "技术文档/AI",
      "created_at": "2026-05-06T10:30:00"
    }
  ],
  "total": 1
}
```

**`status` 含义**

| 值 | 说明 |
|----|------|
| 0 | 待入库（已上传，尚未启动入库） |
| 1 | 入库中（Worker 正在处理） |
| 2 | 已入库（成功，文档可在 RAG 中查询） |
| 3 | 失败（error_message 记录了错误） |

---

### 3.3 启动入库

**请求**

```
POST /api/v1/uploads/ingest
Content-Type: application/json
Authorization: Bearer <api_key>
```

**Body**

```json
{
  "kb_id": "kb-123e4567-e89b-12d3-a456-426614174000",
  "file_ids": ["file-def456", "file-ghi789"],
  "document_date": "2026-05-06",
  "category_path": "技术文档/AI"
}
```

| 字段 | 类型 | 必填 | 说明 |
|------|------|------|------|
| `kb_id` | string | 是 | 知识库 ID |
| `file_ids` | string[] | 是 | 待入库文件 ID 列表（从上传响应中获取） |
| `document_date` | string | 否 | 文档业务日期（`YYYY-MM-DD`） |
| `category_path` | string | 否 | 分类路径，传入后会覆盖上传时的设置 |

**curl 示例**

```bash
curl -X POST 'http://<api_server_ip>:8082/api/v1/uploads/ingest' \
  -H 'Authorization: Bearer <api_key>' \
  -H 'Content-Type: application/json' \
  -d '{
    "kb_id": "kb-123e4567-e89b-12d3-a456-426614174000",
    "file_ids": ["file-def456", "file-ghi789"],
    "document_date": "2026-05-06",
    "category_path": "技术文档/AI"
  }'
```

**响应**

```json
{
  "success": true,
  "task_id": "task-abc123",
  "files_count": 2,
  "message": "入库任务已创建，已提交给独立 worker 处理"
}
```

> 此时文件状态变为 `status=1`（入库中），独立 Worker 进程会自动拉取并处理。

---

### 3.4 查看入库进度

**请求**

```
GET /api/v1/uploads/ingest/status/{task_id}
Authorization: Bearer <api_key>
```

**curl 示例**

```bash
curl -X GET 'http://<api_server_ip>:8082/api/v1/uploads/ingest/status/task-abc123' \
  -H 'Authorization: Bearer <api_key>'
```

**响应**

```json
{
  "task_id": "task-abc123",
  "kb_id": "kb-123e4567-e89b-12d3-a456-426614174000",
  "status": "completed",
  "total": 2,
  "completed": 2,
  "failed": 0,
  "progress": 100.0,
  "files": [
    {
      "file_id": "file-def456",
      "file_name": "技术文档.md",
      "status": "completed",
      "doc_id": null,
      "error": null
    },
    {
      "file_id": "file-ghi789",
      "file_name": "设计说明.md",
      "status": "completed",
      "doc_id": null,
      "error": null
    }
  ]
}
```

**`status` 取值**

| 值 | 说明 |
|----|------|
| `queued` | 已提交，Worker 尚未开始处理 |
| `processing` | Worker 正在处理部分文件 |
| `completed` | 全部文件入库成功 |
| `partial` | 部分文件入库失败 |

**单个文件 `status` 取值**

| 值 | 说明 |
|----|------|
| `pending` | 等待 Worker 处理 |
| `processing` | 正在入库 |
| `completed` | 入库成功 |
| `failed` | 入库失败（`error` 字段有详情） |

---

## 4. 完整调用示例（Python）

```python
import requests

BASE = "http://<api_server_ip>:8082/api/v1"
HEADERS = {"Authorization": "Bearer <api_key>"}
KB_ID = "kb-123e4567-e89b-12d3-a456-426614174000"

# ① 上传 MD 文件
with open("技术文档.md", "rb") as f:
    resp = requests.post(
        f"{BASE}/uploads/batch",
        headers=HEADERS,
        files={"files": ("技术文档.md", f, "text/markdown")},
        data={"kb_id": KB_ID, "category_path": "技术文档/AI"},
    )
data = resp.json()
print(f"上传完成: {data['message']}")

# ② 获取 file_ids
file_ids = [f["file_id"] for f in data["files"]]

# ③ 启动入库
resp = requests.post(
    f"{BASE}/uploads/ingest",
    headers={**HEADERS, "Content-Type": "application/json"},
    json={
        "kb_id": KB_ID,
        "file_ids": file_ids,
        "document_date": "2026-05-06",
    },
)
task = resp.json()
print(f"入库任务已创建: {task['task_id']}")

# ④ 轮询状态直到完成
import time
while True:
    resp = requests.get(
        f"{BASE}/uploads/ingest/status/{task['task_id']}",
        headers=HEADERS,
    )
    status = resp.json()
    print(f"进度: {status['progress']:.0f}% - {status['status']}")
    if status["status"] in ("completed", "partial"):
        break
    time.sleep(2)

print("入库完成!")
```

---

## 5. MD 文件入库的内部机制

```
Worker 拉取到 status=1 的 .md 文件
  │
  ├─ _is_md_file() 判断
  │   ├─ file_type == "text/markdown"  → ✅
  │   └─ file_name 以 ".md" 结尾       → ✅ (兜底)
  │
  ├─ MinIO 下载 MD 内容 → 内存 decode("utf-8")
  │
  ├─ RagService.process_md_content()   ← 跳过 MinerU
  │   ├─ MDIngestor 解析 <img> 标签提取图片
  │   ├─ 下载图片 → base64 → ImageModalProcessor
  │   ├─ insert_text_content (semantic chunking)
  │   └─ _finalize_document_after_ingest (实体/关系抽取)
  │
  └─ PendingFile.status → 2 (已入库)
```

**关键特点**：
- MD 文件不写本地磁盘缓存，全程内存操作
- 图片处理（多模态实体抽取）和 PDF 入库完全一致
- 只需保证 MD 中的 `<img>` 标签的 `src` 指向可访问的图片 URL

---

## 6. 注意事项

| 项 | 说明 |
|-----|------|
| 文件大小限制 | 单文件 ≤ 100MB |
| 编码要求 | MD 文件需要 UTF-8 编码 |
| 图片引用 | `<img>` 标签的 `src` 需要是可访问的 HTTP/HTTPS URL |
| Worker 进程 | 入库依赖 `ingest_worker.py` 进程，需确保已启动 |
| 并发安全 | Worker 使用 Redis 分布式锁，多个 Worker 实例可并行处理 |
| MD 纯文本 | 没有 `<img>` 标签的纯文本 MD 也可以正常入库 |

---

## 7. Worker 启动

```bash
# 激活虚拟环境
source <project_venv>/bin/activate

# 启动 ingest worker
python -m backend.worker.ingest_worker
```

环境变量可调参数：

```bash
INGEST_WORKER_POLL_INTERVAL=2      # 轮询间隔（秒），默认 2
INGEST_WORKER_BATCH_SIZE=5         # 每批最大处理数，默认 5
```
