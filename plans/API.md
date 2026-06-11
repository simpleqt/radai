# 项目 API 文档

> **⚠️ 历史文档**：侧重早期「文档处理 Worker」路由；产品主入口为 **V2 Agent / 知识库 / RAG**。  
> **现行速查** → [docs/api_quick_reference.md](../docs/api_quick_reference.md)  
> **Swagger** → `http://<api_host>:<api_port>/docs`（访客 API `:8083/docs`）  
> **模块说明** → [docs/docs_backend/README.md](../docs/docs_backend/README.md)

本文档描述主后端（FastAPI，`backend/api/http_server.py`）中与 **文档解析/抽取** 相关的 HTTP API；服务名在健康检查中可能仍显示 `document-processing-agent`。

## 基本信息

- 服务地址：`http://<SERVER_HOST>:<SERVER_PORT>`
- 默认端口：`8082`
- OpenAPI/Swagger：
  - Swagger UI：`/docs`
  - OpenAPI JSON：`/openapi.json`

## 认证

除健康检查与配置接口外，其余 API 均需要 API Key 认证。

- Header 方式：`X-API-Key: <api_key>`
- 或 Bearer：`Authorization: Bearer <api_key>`

认证逻辑实现见 [auth.py](file://backend/api/dependencies/auth.py#L17-L44)。

## 通用说明

- Content-Type
  - 上传文件：`multipart/form-data`
  - JSON 请求：`application/json`
- 错误返回：`{"detail": "..."}`（HTTP 状态码为 4xx/5xx）

## 健康检查 / 配置

### GET `/health`

用于探活。

响应示例：

```json
{"status":"healthy","service":"document-processing-agent"}
```

### GET `/config`

返回当前服务使用的模型配置。

响应字段（示例）：

```json
{
  "llm_provider": "deepseek",
  "llm_model": "deepseek-chat",
  "vlm_model": "glm-4.6v"
}
```

## 文档处理（`/api/v1/documents`）

路由入口见 [document_processing/__init__.py](file://backend/api/routers/document_processing/__init__.py#L11-L32)。

### 1) 基本上传

#### POST `/api/v1/documents/upload`

上传文件并创建处理任务（Kafka 投递 + Worker 处理）。

请求（multipart/form-data）：

- `file`：文件（必填）
- `ocr_enabled`：是否启用 OCR（默认 `true`）
- `table_extract`：表格抽取（默认 `true`）
- `formula_extract`：公式抽取（默认 `true`）
- `image_extract`：图片抽取（默认 `true`）
- `vision_enhance`：视觉增强（默认 `true`）
- `output_format`：输出格式（默认 `markdown`）

响应（示例）：

```json
{
  "task_id": "73295e07-cfef-41f6-bb92-c0b1b25c5147",
  "doc_id": "44f1596e-5c02-4670-bdce-a7e8a9994284",
  "status": "pending",
  "message": "文档上传成功，任务已创建"
}
```

curl 示例：

```bash
curl -X POST 'http://<api_host>:<api_port>/api/v1/documents/upload' \
  -H 'X-API-Key: <api_key>' \
  -F 'file=@/path/to/file.pdf;type=application/pdf' \
  -F 'ocr_enabled=true' \
  -F 'table_extract=true' \
  -F 'formula_extract=true' \
  -F 'image_extract=true' \
  -F 'vision_enhance=true' \
  -F 'output_format=markdown'
```

实现见 [upload_routes.py](file://backend/api/routers/document_processing/upload_routes.py#L40-L127)。

#### POST `/api/v1/documents/batch`

批量上传文件并为每个文件创建任务。

请求（multipart/form-data）：

- `files`：文件列表（必填，多个）
- 其余参数与 `/upload` 相同

实现见 [upload_routes.py](file://backend/api/routers/document_processing/upload_routes.py#L129-L252)。

### 2) 任务管理

实现见 [task_routes.py](file://backend/api/routers/document_processing/task_routes.py#L21-L154)。

#### GET `/api/v1/documents/tasks/{task_id}/status`

获取任务状态。

响应字段（示例）：

```json
{
  "task_id": "73295e07-cfef-41f6-bb92-c0b1b25c5147",
  "doc_id": "44f1596e-5c02-4670-bdce-a7e8a9994284",
  "status": "completed",
  "progress": 0,
  "message": null
}
```

#### GET `/api/v1/documents/tasks/{task_id}/result`

获取任务结果。

Query 参数：
- `md_url_only` (bool, default=False): 如果为 true，将尝试返回 Markdown 文件的下载链接（`md_download_url`），而不是在 `result` 中包含完整的 `markdown` 文本内容。如果结果尚未上传到对象存储，系统会自动上传并生成链接。

说明：
- 当 `status=completed` 时，优先从 `extraction_results` 取结构化结果，并返回 `result` 字段。
- 如果结果中包含 `markdown`，会额外返回 `quality_score`、`language`、`messages` 等。

响应字段（包含 markdown 的示例结构）：

```json
{
  "task_id": "xxx",
  "doc_id": "yyy",
  "status": "completed",
  "result": {
    "markdown": "...",
    "page_count": 0,
    "processing_time": 0.0,
    "confidence_score": 0.90575,
    "success": true,
    "document_type": "pdf",
    "quality_score": 0.90575,
    "retry_count": 0,
    "language": "unknown",
    "messages": []
  },
  "output_path": null,
  "error": null
}
```

#### GET `/api/v1/documents/tasks`

任务列表（分页）。

Query 参数：

- `status`：可选（例如 `pending/processing/completed/failed`，以服务枚举为准）
- `page`：默认 1
- `page_size`：默认 20，最大 100

#### DELETE `/api/v1/documents/tasks/{task_id}`

删除任务（具体行为以实现为准，常用于清理历史任务）。

### 3) 文档信息

实现见 [document_routes.py](file://backend/api/routers/document_processing/document_routes.py#L20-L80)。

#### GET `/api/v1/documents/{doc_id}`

获取文档信息（会校验 `doc.user_id == auth.user_id`）。

#### GET `/api/v1/documents/`

获取文档列表（分页）。

Query 参数：

- `status`：可选
- `page`：默认 1
- `page_size`：默认 20，最大 100

### 4) 分块上传（大文件）

实现见 [chunked_upload_routes.py](file://backend/api/routers/document_processing/chunked_upload_routes.py#L23-L239)。

#### POST `/api/v1/documents/upload/chunked/init`

初始化分块上传会话。

Query 参数：

- `filename`：文件名（必填）
- `total_size`：总字节数（必填）
- `content_type`：可选

返回：
- `upload_id`
- `chunk_size`
- `total_chunks`
- `expires_at` 等

#### POST `/api/v1/documents/upload/chunked/{upload_id}?chunk_index=N`

上传单个分块。

请求（multipart/form-data）：
- `chunk`：分块内容

#### GET `/api/v1/documents/upload/chunked/{upload_id}/status`

获取分块上传状态。

#### GET `/api/v1/documents/upload/chunked/{upload_id}/progress`

获取上传进度（用于进度回调）。

#### POST `/api/v1/documents/upload/chunked/{upload_id}/complete`

合并分块并创建处理任务。

请求（multipart/form-data）：
- `ocr_enabled/table_extract/formula_extract/image_extract/vision_enhance/output_format` 同 `/upload`

响应包含：
- `doc_id`
- `task_id`
- `file_path`

#### DELETE `/api/v1/documents/upload/chunked/{upload_id}`

取消上传会话并清理临时分块。

## 内容抽取（`/api/v1/extraction`）

路由入口见 [http_server.py](file://backend/api/http_server.py#L86-L103)，实现见 [extraction.py](file://backend/api/routers/extraction.py#L145-L376)。

注意：当前抽取接口使用内存 `EXTRACTION_STORE` 保存模拟结果，适用于联调与接口形状验证；如需接入真实抽取逻辑，需要替换 `generate_mock_extraction_result`。

### POST `/api/v1/extraction/extract`

执行内容抽取。

请求体（JSON）：

```json
{
  "doc_id": "xxxx",
  "extraction_types": ["all"],
  "mode": "balanced",
  "include_coords": true,
  "include_confidence": true
}
```

### POST `/api/v1/extraction/batch`

批量抽取。

请求体（JSON）：

```json
{
  "doc_ids": ["a", "b"],
  "extraction_types": ["all"],
  "mode": "balanced"
}
```

### GET `/api/v1/extraction/results/{extraction_id}`

获取抽取结果对象。

### GET `/api/v1/extraction/results/{extraction_id}/text?format=text|markdown|json`

获取抽取文本。

### GET `/api/v1/extraction/results/{extraction_id}/tables?format=json|markdown|html`

获取表格列表。

### GET `/api/v1/extraction/results/{extraction_id}/formulas?format=json|latex&include_inline=true|false`

获取公式列表或 LaTeX 拼接文本。

### GET `/api/v1/extraction/results/{extraction_id}/images?include_metadata=true|false`

获取图片列表。

### DELETE `/api/v1/extraction/results/{extraction_id}`

删除抽取结果。

