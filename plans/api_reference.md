# 知识库 API 文档

> **⚠️ 历史参考** — 多库 CRUD 与检索以 Swagger 及 [docs/api_quick_reference.md](../docs/api_quick_reference.md) 为准。  
> **检索生产路径**：`POST /api/v1/rag/retrieve`（DirectRetriever + Profile，非 LightRAG mix 默认）。

## 概述

本文档描述知识库管理和 RAG 服务的 REST API 接口（任务五时期草案）。

**基础 URL**：`http://<api_host>:<api_port>/api/v1`（部署环境替换 host）

**认证方式**: 
- Header: `X-API-Key: <your-api-key>`
- 或 Header: `Authorization: Bearer <your-api-key>`

---

## 知识库管理 API

### 1. 创建知识库

**POST** `/knowledge_bases`

创建一个新的知识库。

**请求体**:
```json
{
  "kb_code": "my_knowledge_base",
  "kb_name": "我的知识库",
  "subject": "计算机科学",
  "description": "知识库描述",
  "is_default": false
}
```

**响应**:
```json
{
  "kb_id": "uuid-string",
  "kb_code": "my_knowledge_base",
  "kb_name": "我的知识库",
  "subject": "计算机科学",
  "description": "知识库描述",
  "status": "active",
  "is_default": false,
  "doc_count": 0,
  "entity_count": 0,
  "relation_count": 0,
  "created_at": "2026-03-19T20:44:28",
  "updated_at": "2026-03-19T20:44:38"
}
```

**示例**:
```bash
curl -X POST "http://<api_server_ip>:8082/api/v1/knowledge_bases" \
  -H "Content-Type: application/json" \
  -H "X-API-Key: <your_api_key>" \
  -d '{
    "kb_code": "test_multimodal",
    "kb_name": "多模态测试库",
    "subject": "multimodal",
    "description": "测试多模态RAG入库功能"
  }'
```

---

### 2. 列出知识库

**GET** `/knowledge_bases`

获取知识库列表，支持分页和过滤。

**查询参数**:
| 参数 | 类型 | 说明 |
|------|------|------|
| status | string | 状态过滤: active/inactive/deleting |
| owner_id | string | 所有者过滤 |
| skip | int | 分页偏移 (默认: 0) |
| limit | int | 每页数量 (默认: 20, 最大: 100) |

**响应**:
```json
{
  "items": [...],
  "total": 10
}
```

**示例**:
```bash
curl -X GET "http://<api_server_ip>:8082/api/v1/knowledge_bases" \
  -H "X-API-Key: <your_api_key>"
```

---

### 3. 获取知识库详情

**GET** `/knowledge_bases/{kb_id}`

获取指定知识库的详细信息。

**示例**:
```bash
curl -X GET "http://<api_server_ip>:8082/api/v1/knowledge_bases/<kb_id_example>" \
  -H "X-API-Key: <your_api_key>"
```

---

### 4. 更新知识库

**PUT** `/knowledge_bases/{kb_id}`

更新知识库信息（名称、学科、描述）。

**请求体**:
```json
{
  "kb_name": "新名称",
  "description": "新描述"
}
```

---

### 5. 删除知识库

**DELETE** `/knowledge_bases/{kb_id}`

删除知识库及其所有数据（不可恢复）。

**示例**:
```bash
curl -X DELETE "http://<api_server_ip>:8082/api/v1/knowledge_bases/<kb_id_example>" \
  -H "X-API-Key: <your_api_key>"
```

**响应**:
```json
{
  "success": true,
  "message": "知识库 xxx 已删除"
}
```

---

### 6. 切换知识库

**POST** `/knowledge_bases/{kb_id}/switch`

切换当前会话的知识库。

---

### 7. 获取知识库统计

**POST** `/knowledge_bases/{kb_id}/stats`

更新并返回知识库的统计信息。

---

## 文档管理 API

### 1. 上传文档到知识库

**POST** `/knowledge_bases/{kb_id}/documents`

上传文档到指定知识库，支持异步处理。

**支持的文件格式**: pdf, docx, txt, md, png, jpg, jpeg

**请求**: multipart/form-data
- `file`: 上传的文件

**响应**:
```json
{
  "success": true,
  "kb_id": "uuid-string",
  "doc_id": "doc-uuid",
  "file_name": "document.pdf",
  "message": "文件上传成功，正在后台处理入库"
}
```

**示例**:
```bash
curl -X POST "http://<api_server_ip>:8082/api/v1/knowledge_bases/<kb_id_example>/documents" \
  -H "X-API-Key: <your_api_key>" \
  -F "file=@/path/to/document.pdf"
```

---

### 2. 列出知识库文档

**GET** `/knowledge_bases/{kb_id}/documents`

获取知识库中的文档列表。

**查询参数**:
| 参数 | 类型 | 说明 |
|------|------|------|
| parse_status | int | 解析状态过滤 (0-3) |
| storage_status | int | 存储状态过滤 (0-3) |
| skip | int | 分页偏移 |
| limit | int | 每页数量 |

---

### 3. 删除文档

**DELETE** `/knowledge_bases/{kb_id}/documents/{doc_id}`

从知识库中删除指定文档。

---

## RAG 查询 API

### 1. 知识库检索（仅检索，不生成）

**POST** `/rag/retrieve`

仅检索知识库，返回上下文和相关图片，不生成答案。

**完整检索流程**:
1. **关键词提取**：向量搜索 + BM25 + 图拓扑融合算法
2. **阶段1：召回**：获取原始数据（实体、关系、文本块）
3. **阶段2：PPR 图过滤**：基于高级关键词的个性化 PageRank 过滤
4. **Rerank 加权融合**：PPR 分数 + Rerank 分数加权融合
5. **MMR 多样性重排序**：去除冗余，保证多样性
6. **阶段3：资产组装**：生成最终 Prompt

**请求体**:
```json
{
  "query": "What is mRAG-gim framework?",
  "kb_id": "uuid-string",
  "top_k": 30,
  "chunk_top_k": 10
}
```

**参数说明**:
| 参数 | 类型 | 必填 | 说明 |
|------|------|------|------|
| query | string | 是 | 用户查询 |
| kb_id | string | 否 | 知识库 ID（不传则使用默认知识库） |
| top_k | int | 否 | 检索数量，默认 30 |
| chunk_top_k | int | 否 | 文档片段数量，默认 10 |

**响应**:
```json
{
  "context": "检索到的上下文内容...",
  "images": [
    {
      "marker": "IMAGE_1",
      "url": "http://<api_server_ip>:9000/knowledge-base/path/to/image.png",
      "path": "/path/to/image.png"
    }
  ],
  "entity_count": 10,
  "relation_count": 5,
  "chunk_count": 3
}
```

**响应字段说明**:
| 字段 | 类型 | 说明 |
|------|------|------|
| context | string | 检索到的上下文内容，包含实体、关系和文档片段 |
| images | array | 相关图片列表 |
| images[].marker | string | 图片标识（如 IMAGE_1） |
| images[].url | string | 图片访问 URL |
| images[].path | string | 图片原始路径 |
| entity_count | int | 检索到的实体数量 |
| relation_count | int | 检索到的关系数量 |
| chunk_count | int | 文档片段数量 |

**示例**:
```bash
curl -X POST "http://<api_server_ip>:8082/api/v1/rag/retrieve" \
  -H "Content-Type: application/json" \
  -H "X-API-Key: <your_api_key>" \
  -d '{
    "query": "What is mRAG-gim framework?",
    "kb_id": "<kb_id_example>"
  }'
```

---

### 2. 知识库流式查询（检索 + 生成）

**POST** `/rag/query_stream`

检索知识库并流式生成答案。

**完整检索流程**:
1. **关键词提取**：向量搜索 + BM25 + 图拓扑融合算法
2. **阶段1：召回**：获取原始数据（实体、关系、文本块）
3. **阶段2：PPR 图过滤**：基于高级关键词的个性化 PageRank 过滤
4. **Rerank 加权融合**：PPR 分数 + Rerank 分数加权融合
5. **MMR 多样性重排序**：去除冗余，保证多样性
6. **阶段3：资产组装**：生成最终 Prompt
7. **流式生成答案**：调用 LLM 流式生成

**请求体**:
```json
{
  "query": "What is mRAG-gim framework?",
  "kb_id": "uuid-string",
  "model_id": "qwen3-max",
  "top_k": 30,
  "chunk_top_k": 10,
  "temperature": 0.7,
  "max_tokens": 4096
}
```

**参数说明**:
| 参数 | 类型 | 必填 | 说明 |
|------|------|------|------|
| query | string | 是 | 用户查询 |
| kb_id | string | 否 | 知识库 ID（不传则使用默认知识库） |
| model_id | string | 否 | 模型 ID（不传则使用默认模型） |
| top_k | int | 否 | 检索数量，默认 30 |
| chunk_top_k | int | 否 | 文档片段数量，默认 10 |
| temperature | float | 否 | 生成温度，默认 0.7 |
| max_tokens | int | 否 | 最大生成 token 数，默认 4096 |

**响应格式**:

返回 SSE (Server-Sent Events) 流式响应：

```
data: {"type": "status", "content": "正在提取关键词..."}

data: {"type": "status", "content": "正在检索知识库..."}

data: {"type": "context", "images": [{"path": "/path/to/image.png", "url": "http://..."}]}

data: {"type": "status", "content": "正在生成回答..."}

data: {"type": "chunk", "content": "回答内容片段..."}

data: [DONE]
```

**事件类型**:
| 类型 | 说明 |
|------|------|
| status | 状态更新（关键词提取、检索、生成等阶段） |
| context | 检索上下文和图片信息 |
| chunk | 生成的文本片段 |
| error | 错误信息 |

**示例**:
```bash
curl -X POST "http://<api_server_ip>:8082/api/v1/rag/query_stream" \
  -H "Content-Type: application/json" \
  -H "X-API-Key: <your_api_key>" \
  -d '{
    "query": "What is mRAG-gim framework?",
    "kb_id": "<kb_id_example>"
  }'
```

---

### 3. 多知识库查询

**POST** `/rag/query_multiple`

同时查询多个知识库。

**请求体**:
```json
{
  "query": "查询内容",
  "kb_ids": ["kb-id-1", "kb-id-2"],
  "mode": "hybrid"
}
```

---

### 4. 插入文本内容

**POST** `/rag/insert`

插入文本内容到知识库。

**请求体**:
```json
{
  "content": "文本内容",
  "kb_id": "uuid-string"
}
```

---

### 5. 插入 DOCX 文档

**POST** `/rag/insert_docx`

插入 DOCX 文档到知识库。

**请求体**:
```json
{
  "file_path": "/path/to/document.docx",
  "kb_id": "uuid-string"
}
```

---

### 6. 处理多模态文档

**POST** `/rag/process_multimodal`

处理多模态文档（PDF等）。

**请求体**:
```json
{
  "file_path": "/path/to/document.pdf",
  "kb_id": "uuid-string"
}
```

---

### 7. 删除文档

**POST** `/rag/delete_doc`

从RAG系统中删除文档。

**请求体**:
```json
{
  "doc_id": "document-id",
  "kb_id": "uuid-string"
}
```

---

### 8. 重置服务

**POST** `/rag/reset`

重置RAG服务状态。

---

### 9. 获取文档状态

**GET** `/rag/docs_status`

获取所有文档的处理状态。

---

### 10. 获取可用模型

**GET** `/rag/models`

获取系统可用的LLM模型列表。

---

### 11. 健康检查

**GET** `/rag/health`

检查服务健康状态。

---

## 关键词索引 API

### 1. 获取索引统计

**GET** `/keywords/stats/{kb_id}`

获取指定知识库的关键词索引统计信息。

---

### 2. 重建索引

**POST** `/keywords/rebuild/{kb_id}`

重建指定知识库的关键词索引。

---

## 完整工作流示例

### 1. 创建知识库并上传文档

```bash
# 1. 创建知识库
KB_ID=$(curl -s -X POST "http://<api_server_ip>:8082/api/v1/knowledge_bases" \
  -H "Content-Type: application/json" \
  -H "X-API-Key: <your_api_key>" \
  -d '{
    "kb_code": "my_kb",
    "kb_name": "我的知识库",
    "subject": "test"
  }' | jq -r '.kb_id')

echo "Created KB: $KB_ID"

# 2. 上传文档
curl -X POST "http://<api_server_ip>:8082/api/v1/knowledge_bases/$KB_ID/documents" \
  -H "X-API-Key: <your_api_key>" \
  -F "file=@document.pdf"

# 3. 等待处理完成后查询
sleep 60
curl -X POST "http://<api_server_ip>:8082/api/v1/rag/query" \
  -H "Content-Type: application/json" \
  -H "X-API-Key: <your_api_key>" \
  -d "{
    \"query\": \"文档中的问题\",
    \"kb_id\": \"$KB_ID\",
    \"mode\": \"hybrid\"
  }"
```

### 2. 删除知识库

```bash
curl -X DELETE "http://<api_server_ip>:8082/api/v1/knowledge_bases/$KB_ID" \
  -H "X-API-Key: <your_api_key>"
```

---

## 错误响应

所有API在出错时返回统一格式：

```json
{
  "detail": "错误描述信息"
}
```

常见HTTP状态码：
| 状态码 | 说明 |
|--------|------|
| 400 | 请求参数错误 |
| 401 | 认证失败 |
| 404 | 资源不存在 |
| 500 | 服务器内部错误 |

---

## 配置说明

### 向量数据库 (Milvus)

环境变量配置（在 `.env` 文件中）：
```
MILVUS_URI=grpc://host:19530
MILVUS_USER=root
MILVUS_PASSWORD=password
MILVUS_DB_NAME=default
```

### 缓存/KV存储 (Redis)

```
REDIS_HOST=host
REDIS_PORT=6379
REDIS_PASSWORD=password
REDIS_DB=0
```

### 图数据库 (NebulaGraph)

```
NEBULA_HOST=host
NEBULA_GRAPH_PORT=9669
NEBULA_USER=root
NEBULA_PASSWORD=nebula
```
