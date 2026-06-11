# 文档入库 API 文档

> **文档状态**：🔧 运维/集成参考（端点与代码一致）。  
> **注意**：管理后台 UI 默认走 **`POST /api/v1/uploads/ingest`**，与本节 `/api/v1/ingestion/*` 并存；见 [docs/docs_frontend/admin_module.md](../docs/docs_frontend/admin_module.md)。  
> **Markdown 直入库**：[docs/md_ingestion_api.md](../docs/md_ingestion_api.md)  
> **速查**：[docs/api_quick_reference.md](../docs/api_quick_reference.md)

## 概述

从 MySQL `file_info` 查询待入库 MD 并触发入库的 HTTP API（`backend/api/routers/document_ingestion.py`）。

**基础 URL**：`http://<host>:8082/api/v1/ingestion`

**认证**：`X-API-Key` 或 `Authorization: Bearer <key>`（勿在文档/仓库中写真实密钥，使用 `backend/.env`）

---

## API 端点

### 1. 获取待入库文档列表

获取数据库中 `parse_status=1` 且 `storage_status=0` 的待入库文档列表。

**请求**

```http
GET /pending?limit=100
```

**参数**

| 参数名 | 类型 | 必填 | 默认值 | 描述 |
|--------|------|------|--------|------|
| limit | int | 否 | 100 | 返回的最大文档数量 |

**请求头**

```
X-API-Key: <your-api-key>
```

**响应示例**

```json
{
  "total": 1,
  "documents": [
    {
      "task_id": "2026030500",
      "file_name": "航空燃料超临界热裂解过程中焦炭的形成",
      "parse_status": 1,
      "storage_status": 0,
      "md_file_path": "http://<minio_server_ip>:9005/processed-markdown-file/..."
    }
  ]
}
```

**字段说明**

| 字段名 | 类型 | 描述 |
|--------|------|------|
| total | int | 待入库文档总数 |
| documents | array | 文档列表 |
| documents[].task_id | string | 任务ID（主键） |
| documents[].file_name | string | 文件名 |
| documents[].parse_status | int | 解析状态（0=未解析，1=已解析） |
| documents[].storage_status | int | 存储状态（0=未入库，1=已入库） |
| documents[].md_file_path | string | MD文件URL |

---

### 1.1 获取文档列表（分页+过滤）

获取所有文档列表，支持分页和多种过滤条件。

**请求**

```http
GET /list?page=1&page_size=20&storage_status=1&parse_status=1&file_name=关键词
```

**参数**

| 参数名 | 类型 | 必填 | 默认值 | 描述 |
|--------|------|------|--------|------|
| page | int | 否 | 1 | 页码，从1开始 |
| page_size | int | 否 | 20 | 每页数量(1-100) |
| parse_status | int | 否 | - | 解析状态过滤(0=未解析,1=已解析) |
| storage_status | int | 否 | - | 存储状态过滤(0=未入库,1=已入库) |
| file_name | string | 否 | - | 文件名模糊搜索 |

**请求头**

```
X-API-Key: <your-api-key>
```

**响应示例**

```json
{
  "total": 100,
  "page": 1,
  "page_size": 20,
  "total_pages": 5,
  "documents": [
    {
      "task_id": "2026030500",
      "file_name": "航空燃料超临界热裂解过程中焦炭的形成",
      "parse_status": 1,
      "storage_status": 1,
      "md_file_path": "http://<minio_server_ip>:9005/processed-markdown-file/..."
    }
  ]
}
```

**字段说明**

| 字段名 | 类型 | 描述 |
|--------|------|------|
| total | int | 满足条件的文档总数 |
| page | int | 当前页码 |
| page_size | int | 每页数量 |
| total_pages | int | 总页数 |
| documents | array | 文档列表 |

---

### 2. 获取文档状态

查询指定文档的当前状态。

**请求**

```http
GET /status/{task_id}
```

**路径参数**

| 参数名 | 类型 | 必填 | 描述 |
|--------|------|------|------|
| task_id | string | 是 | 任务ID |

**请求头**

```
X-API-Key: <your-api-key>
```

**响应示例 - 文档存在**

```json
{
  "task_id": "2026030500",
  "exists": true,
  "file_name": "航空燃料超临界热裂解过程中焦炭的形成",
  "parse_status": 1,
  "storage_status": 0,
  "md_file_path": "http://<minio_server_ip>:9005/processed-markdown-file/..."
}
```

**响应示例 - 文档不存在**

```json
{
  "task_id": "2026030500",
  "exists": false
}
```

---

### 3. 执行文档入库

执行文档入库操作，将待入库的 MD 文件处理并插入到 RAG 知识库中。

**请求**

```http
POST /ingest
```

**请求头**

```
X-API-Key: <your-api-key>
Content-Type: application/json
```

**请求体**

```json
{
  "task_id": null,
  "limit": 10,
  "dry_run": false
}
```

**参数说明**

| 参数名 | 类型 | 必填 | 默认值 | 描述 |
|--------|------|------|--------|------|
| task_id | string | 否 | null | 指定处理单个文档，不传则处理所有待入库文档 |
| limit | int | 否 | 10 | 最大处理数量（仅批量处理时有效） |
| dry_run | bool | 否 | false | 预览模式，为 true 时只返回信息不实际入库 |

**响应示例 - 成功**

```json
{
  "success": true,
  "processed_count": 1,
  "successful_count": 1,
  "failed_count": 0,
  "results": [
    {
      "task_id": "2026030500",
      "file_name": "航空燃料超临界热裂解过程中焦炭的形成",
      "success": true,
      "doc_id": "2026030500",
      "text_length": 13637,
      "images_count": 6,
      "elapsed_time": 45.2,
      "error": null
    }
  ],
  "dry_run": false
}
```

**响应示例 - 失败**

```json
{
  "success": false,
  "processed_count": 1,
  "successful_count": 0,
  "failed_count": 1,
  "results": [
    {
      "task_id": "2026030500",
      "file_name": "航空燃料超临界热裂解过程中焦炭的形成",
      "success": false,
      "doc_id": null,
      "text_length": null,
      "images_count": null,
      "elapsed_time": null,
      "error": "入库失败原因..."
    }
  ],
  "dry_run": false
}
```

**字段说明**

| 字段名 | 类型 | 描述 |
|--------|------|------|
| success | bool | 整体是否成功（所有文档都成功时为 true） |
| processed_count | int | 处理的文档数量 |
| successful_count | int | 成功入库的文档数量 |
| failed_count | int | 失败的文档数量 |
| results | array | 每个文档的处理结果详情 |
| results[].task_id | string | 任务ID |
| results[].file_name | string | 文件名 |
| results[].success | bool | 该文档是否入库成功 |
| results[].doc_id | string | 入库后的文档ID |
| results[].text_length | int | 文本内容长度 |
| results[].images_count | int | 处理的图片数量 |
| results[].elapsed_time | float | 处理耗时（秒） |
| results[].error | string | 错误信息（失败时） |
| dry_run | bool | 是否为预览模式 |

---

### 4. 单个文档入库

入库指定的单个文档。

**请求**

```http
POST /ingest/{task_id}?dry_run=false
```

**路径参数**

| 参数名 | 类型 | 必填 | 描述 |
|--------|------|------|------|
| task_id | string | 是 | 任务ID |

**查询参数**

| 参数名 | 类型 | 必填 | 默认值 | 描述 |
|--------|------|------|--------|------|
| dry_run | bool | 否 | false | 预览模式 |

**请求头**

```
X-API-Key: <your-api-key>
```

**响应**

与批量入库接口相同。

---

### 5. 获取入库统计

获取文档入库的统计信息。

**请求**

```http
GET /stats
```

**请求头**

```
X-API-Key: <your-api-key>
```

**响应示例**

```json
{
  "total_documents": 10,
  "parsed_documents": 8,
  "stored_documents": 5,
  "pending_ingestion": 3,
  "storage_rate": 0.5
}
```

**字段说明**

| 字段名 | 类型 | 描述 |
|--------|------|------|
| total_documents | int | 数据库中的总文档数 |
| parsed_documents | int | 已解析的文档数 |
| stored_documents | int | 已入库的文档数 |
| pending_ingestion | int | 待入库的文档数 |
| storage_rate | float | 入库率（已入库/总数） |

---

## 使用示例

### 命令行示例

```bash
# 1. 查看待入库文档列表
curl -H "X-API-Key: <your-api-key>" \
  http://<api_server_ip>:8082/api/v1/ingestion/pending

# 1.1 获取文档列表（分页+过滤）
curl -H "X-API-Key: <your-api-key>" \
  "http://<api_server_ip>:8082/api/v1/ingestion/list?page=1&page_size=20"

# 按状态过滤
curl -H "X-API-Key: <your-api-key>" \
  "http://<api_server_ip>:8082/api/v1/ingestion/list?storage_status=1"  # 已入库
curl -H "X-API-Key: <your-api-key>" \
  "http://<api_server_ip>:8082/api/v1/ingestion/list?storage_status=0"  # 未入库

# 文件名搜索
curl -H "X-API-Key: <your-api-key>" \
  "http://<api_server_ip>:8082/api/v1/ingestion/list?file_name=航空"

# 2. 预览入库（不实际执行）
curl -X POST -H "X-API-Key: <your-api-key>" -H "Content-Type: application/json" \
  -d '{"dry_run": true}' \
  http://<api_server_ip>:8082/api/v1/ingestion/ingest

# 3. 批量入库（最多10个）
curl -X POST -H "X-API-Key: <your-api-key>" -H "Content-Type: application/json" \
  -d '{"limit": 10}' \
  http://<api_server_ip>:8082/api/v1/ingestion/ingest

# 4. 单个文档入库
curl -X POST -H "X-API-Key: <your-api-key>" \
  http://<api_server_ip>:8082/api/v1/ingestion/ingest/2026030500

# 5. 查看统计信息
curl -H "X-API-Key: <your-api-key>" \
  http://<api_server_ip>:8082/api/v1/ingestion/stats
```

### Python 示例

```python
import requests

API_BASE = "http://<api_server_ip>:8082/api/v1/ingestion"
HEADERS = {"X-API-Key": "<your-api-key>"}

# 获取待入库文档列表
response = requests.get(f"{API_BASE}/pending", headers=HEADERS)
pending_docs = response.json()
print(f"待入库文档数: {pending_docs['total']}")

# 执行入库
response = requests.post(
    f"{API_BASE}/ingest",
    headers={**HEADERS, "Content-Type": "application/json"},
    json={"limit": 5}
)
result = response.json()
print(f"处理: {result['processed_count']}, 成功: {result['successful_count']}")
```

---

## 错误码

| HTTP 状态码 | 错误描述 |
|-------------|----------|
| 401 | 缺少 API Key 或 API Key 无效 |
| 500 | 服务器内部错误 |

---

## 注意事项

1. **入库流程**：
   - 系统从数据库查询 `parse_status=1` 且 `storage_status=0` 的文档
   - 下载 MD 文件并解析内容
   - 提取文本和图片（包括图片上下文和图注）
   - 将内容插入 RAG 知识库
   - 成功后更新数据库 `storage_status=1`

2. **幂等性**：
   - 同一文档可以多次入库，但建议先查询状态避免重复处理

3. **性能**：
   - 入库操作涉及下载、解析、向量化等步骤，耗时较长
   - 建议根据服务器负载调整 `limit` 参数

4. **预览模式**：
   - `dry_run=true` 时只返回文档信息，不实际执行入库
   - 可用于验证待入库文档列表
