# 知识库管理接口设计方案

> **文档状态**：📦 历史整合草案。`clear_failed_docs`、`delete_by_filename`、`list_files` 等 **多数未暴露 HTTP**。  
> **图库**：正文示意图已按 **NebulaGraph**（每 KB Space）校正（2026-05-30）；勿使用文中旧 `bolt://7687` 示例。  
> **现行**：[plans/知识库管理接口说明.md](./知识库管理接口说明.md) · [docs/api_quick_reference.md](../docs/api_quick_reference.md) · Swagger `/docs`

## 1. 概述

本文档整合了知识库管理接口的设计方案，提供以下核心功能：

### 1.1 功能列表

| 功能 | 接口路径 | 说明 |
|-----|---------|------|
| 查询可用模型列表 | `GET /api/v1/rag/models` | 获取当前系统可用的 LLM 模型列表 |
| RAG 健康检查 | `GET /api/v1/rag/health` | 检查 RAG 服务是否正常运行 |
| 根据 doc_id 删除文档 | `POST /api/v1/rag/delete_doc` | 根据文档ID删除知识库中的数据 |
| 清除失败文档状态 | `POST /api/v1/rag/clear_failed_docs` | 清除处理失败/处理中的文档状态 |
| 根据文件名删除知识库内容 | `POST /api/v1/rag/delete_by_filename` | 根据文件名删除所有相关数据 |
| 根据文件名查询已入库文件 | `POST /api/v1/rag/list_files` | 查询已入库到知识库中的文件信息 |
| 获取知识库统计信息 | `GET /api/v1/rag/stats` | 获取知识库整体统计信息 |

### 1.2 涉及的存储系统

知识库数据存储在以下系统中：

| 存储系统 | 存储内容 | 说明 |
|---------|---------|------|
| MySQL | `file_info` 表 | 记录文件的解析和入库状态 |
| Redis | `full_docs` | 完整文档内容 |
| Redis | `doc_status` | 文档处理状态 |
| Redis | `text_chunks` | 文本分块数据 |
| Milvus | `entities` | 实体向量 |
| Milvus | `relationships` | 关系向量 |
| Milvus | `chunks` | 分块向量 |
| NebulaGraph | 节点 | 实体节点（每 KB 独立 Space） |
| NebulaGraph | 边 | 关系边 |

---

## 2. 系统架构

```text
┌────────────────┐  ┌────────────────┐
│ end            │  │ A              │
└────────────────┘  └────────────────┘
           │
           ▼
           │
           ▼
┌──────────────────────────────────────┐
│ B                                    │
└──────────────────────────────────────┘
           │
           ▼
           │
           ▼
           │
           ▼
┌────────────────┐  ┌────────────────┐  ┌────────────────┐
│ RagService     │  │ D              │  │ E              │
└────────────────┘  └────────────────┘  └────────────────┘
           │
           ▼
           │
           ▼
           │
           ▼
           │
           ▼
┌────────────────┐  ┌────────────────┐  ┌────────────────┐  ┌────────────────┐
│ MySQL          │  │ Redis          │  │ Milvus         │  │ NebulaGraph    │
└────────────────┘  └────────────────┘  └────────────────┘  └────────────────┘

```

---

## 3. API 接口设计

### 3.1 现有接口

#### 3.1.1 查询可用模型列表

获取当前系统可用的 LLM 模型列表。

**请求**

```http
GET /api/v1/rag/models
Authorization: Bearer <api_key>
```

**响应示例**

```json
{
  "models": [
    {
      "id": "qwen3-max",
      "name": "通义千问 Max",
      "provider": "dashscope",
      "description": "通义千问最强模型"
    }
  ],
  "default_model": "qwen3-max"
}
```

---

#### 3.1.2 RAG 健康检查

检查 RAG 服务是否正常运行。

**请求**

```http
GET /api/v1/rag/health
Authorization: Bearer <api_key>
```

**响应示例**

```json
{
  "status": "healthy"
}
```

---

#### 3.1.3 根据 doc_id 删除文档

根据文档ID删除知识库中的数据。

**请求**

```http
POST /api/v1/rag/delete_doc
Authorization: Bearer <api_key>

{
  "doc_id": "xxx",
  "delete_llm_cache": false
}
```

**参数说明**

| 参数 | 类型 | 必填 | 说明 |
|------|------|------|------|
| doc_id | string | 是 | 要删除的文档ID |
| delete_llm_cache | boolean | 否 | 是否删除 LLM 缓存，默认 false |

**响应示例**

```json
{
  "result": {
    "success": true,
    "doc_id": "xxx"
  }
}
```

---

#### 3.1.4 清除失败文档状态

清除处理失败/处理中的文档状态，防止下次入库时自动重试。

**请求**

```http
POST /api/v1/rag/clear_failed_docs
Authorization: Bearer <api_key>

{
  "include_processing": true
}
```

**参数说明**

| 参数 | 类型 | 必填 | 说明 |
|------|------|------|------|
| include_processing | boolean | 否 | 是否同时清除 PROCESSING 状态的文档，默认 true |

**响应示例**

```json
{
  "success": true,
  "cleared": 5,
  "doc_ids": ["id1", "id2", "id3", "id4", "id5"],
  "status_counts": {
    "FAILED": 3,
    "PROCESSING": 2
  },
  "message": "已清除 5 个文档状态"
}
```

---

### 3.2 待实现接口

#### 3.2.1 根据文件名删除知识库内容

根据文件名（支持部分匹配）删除知识库中的所有相关数据，包括 Redis、向量数据库、图数据库和 MySQL 数据库中的记录。

**请求**

```http
POST /api/v1/rag/delete_by_filename
Authorization: Bearer <api_key>

{
  "file_name": "示例文档.pdf",
  "exact_match": false,
  "delete_llm_cache": true,
  "dry_run": false
}
```

**参数说明**

| 参数 | 类型 | 必填 | 默认值 | 说明 |
|------|------|------|--------|------|
| file_name | string | 是 | - | 要删除的文件名（支持部分匹配） |
| exact_match | boolean | 否 | false | 是否精确匹配 |
| delete_llm_cache | boolean | 否 | true | 是否删除 LLM 缓存 |
| dry_run | boolean | 否 | false | 预览模式，只显示将要删除的数据，不实际删除 |

**响应示例**

```json
{
  "success": true,
  "file_name": "示例文档.pdf",
  "deleted": {
    "doc_ids": ["doc-xxx", "doc-yyy"],
    "mysql": {
      "file_info_records": 1
    },
    "redis": {
      "text_chunks": 150,
      "full_docs": 1,
      "doc_status": 1
    },
    "milvus": {
      "entities": 50,
      "chunks": 150,
      "relationships": 30
    },
    "neo4j": {
      "entities": 50,
      "relationships": 30
    }
  },
  "message": "成功删除知识库中的相关数据"
}
```

#### 删除流程说明

```text
┌──────────────────────────┐ ┌──────────────────────────┐ ┌──────────────────────────┐ ┌──────────────────────────┐ ┌──────────────────────────┐ ┌──────────────────────────┐ ┌──────────────────────────┐
│          Client          │ │           API            │ │KnowledgeManagementService│ │          MySQL           │ │          Redis           │ │          Milvus          │ │       NebulaGraph        │
└──────────────────────────┘ └──────────────────────────┘ └──────────────────────────┘ └──────────────────────────┘ └──────────────────────────┘ └──────────────────────────┘ └──────────────────────────┘

  ┌─ LOOP: 每个 FileInfo
  └─ END
  └─ END

```

#### 各存储系统清理逻辑

1. **MySQL 数据库 (`file_info` 表)**
   - 根据 `file_name` 模糊匹配或精确匹配删除记录

2. **Redis**
   - `text_chunks`: 扫描包含指定文件名的 chunk 内容
   - `full_docs`: 扫描文档内容中包含指定文件名的记录
   - `doc_status`: 删除与文件名匹配的文档状态

3. **Milvus 向量数据库**
   - 按 KB 的 collection 删除（示例默认库：`rag_nuclear_electronics_entities` / `_chunks` / `_relationships`）
   - 删除 `source_id` 或 `file_path` 包含文件名的实体、分块、关系向量

4. **NebulaGraph 图数据库**
   - 删除 `file_path` 或 `source_id` 属性包含文件名的实体
   - 删除与这些实体相关的关系

---

#### 3.2.2 根据文件名查询已入库文件

根据文件名（支持部分匹配）查询已入库到知识库中的文件信息。

**请求**

```http
POST /api/v1/rag/list_files
Authorization: Bearer <api_key>

{
  "file_name": "示例",
  "exact_match": false,
  "storage_status": null,
  "skip": 0,
  "limit": 10
}
```

**参数说明**

| 参数 | 类型 | 必填 | 默认值 | 说明 |
|------|------|------|--------|------|
| file_name | string | 否 | - | 文件名关键字，用于模糊匹配 |
| exact_match | boolean | 否 | false | 是否精确匹配 |
| storage_status | integer | 否 | - | 入库状态过滤 (0: 未入库, 1: 已入库) |
| skip | integer | 否 | 0 | 跳过的记录数 |
| limit | integer | 否 | 10 | 返回的记录数 |

**响应示例**

```json
{
  "success": true,
  "total": 5,
  "files": [
    {
      "task_id": "task_123",
      "file_name": "示例文档1.pdf",
      "parse_status": 1,
      "storage_status": 1,
      "md_file_path": "/path/to/md/example1.md",
      "doc_id": "doc-xxx",
      "doc_status": "COMPLETED",
      "storage_details": {
        "entities_count": 150,
        "chunks_count": 45,
        "relationships_count": 230
      },
      "created_at": "2024-01-01T00:00:00Z",
      "updated_at": "2024-01-01T00:00:00Z"
    }
  ]
}
```

#### 字段说明

| 字段 | 类型 | 说明 |
|------|------|------|
| task_id | string | 任务ID |
| file_name | string | 文件名 |
| parse_status | integer | 解析状态 (0: 未解析, 1: 已解析) |
| storage_status | integer | 入库状态 (0: 未入库, 1: 已入库) |
| md_file_path | string | Markdown 文件路径 |
| doc_id | string | 文档ID |
| doc_status | string | 文档处理状态 |
| storage_details | object | 存储详情（实体数、分块数、关系数） |
| created_at | string | 创建时间 |
| updated_at | string | 更新时间 |

---

#### 3.2.3 获取知识库统计信息

获取知识库整体统计信息。

**请求**

```http
GET /api/v1/rag/stats
Authorization: Bearer <api_key>
```

**响应示例**

```json
{
  "total_documents": 100,
  "stored_documents": 80,
  "pending_documents": 15,
  "failed_documents": 5,
  "storage_summary": {
    "neo4j": {
      "nodes": 15000,
      "relationships": 25000
    },
    "milvus": {
      "entities": 15000,
      "chunks": 5000,
      "relationships": 25000
    },
    "redis": {
      "full_docs": 80,
      "text_chunks": 5000
    }
  }
}
```

---

## 4. 服务层设计

### 4.1 KnowledgeManagementService

新建服务类 [`backend/rag/knowledge_management.py`](backend/rag/knowledge_management.py)：

```python
class KnowledgeManagementService:
    """知识库管理服务"""
    
    def __init__(
        self,
        rag_service: RagService,
        file_info_crud: FileInfoCRUD
    ):
        self.rag_service = rag_service
        self.file_info_crud = file_info_crud
    
    async def delete_by_filename(
        self,
        file_name: str,
        exact_match: bool = False,
        delete_llm_cache: bool = True,
        dry_run: bool = False
    ) -> Dict[str, Any]:
        """
        按文件名删除知识库内容
        
        删除流程：
        1. 从 MySQL FileInfo 表查找匹配的文件
        2. 从 Redis doc_status 获取 doc_id
        3. 删除 NebulaGraph 实体和关系
        4. 删除 Milvus 向量
        5. 删除 Redis 数据
        6. 更新 MySQL FileInfo 状态
        """
        pass
    
    async def query_by_filename(
        self,
        file_name: str = None,
        exact_match: bool = False,
        storage_status: int = None,
        skip: int = 0,
        limit: int = 20
    ) -> Dict[str, Any]:
        """
        按文件名查询已入库文件
        """
        pass
    
    async def get_stats(self) -> Dict[str, Any]:
        """
        获取知识库统计信息
        """
        pass
    
    # 内部方法
    async def _delete_from_neo4j(self, doc_ids: List[str]) -> Dict[str, int]:
        """删除 NebulaGraph 数据"""
        pass
    
    async def _delete_from_milvus(self, doc_ids: List[str]) -> Dict[str, int]:
        """删除 Milvus 数据"""
        pass
    
    async def _delete_from_redis(self, doc_ids: List[str]) -> Dict[str, int]:
        """删除 Redis 数据"""
        pass
```

---

## 5. 数据访问层设计

### 5.1 FileInfoCRUD 新增方法

在 [`backend/db/crud/file_info_crud.py`](backend/db/crud/file_info_crud.py) 中新增：

```python
def get_by_file_name(
    self,
    file_name: str,
    exact_match: bool = False
) -> List[FileInfo]:
    """
    按文件名查询文件信息
    
    Args:
        file_name: 文件名
        exact_match: 是否精确匹配
    
    Returns:
        FileInfo 列表
    """
    if exact_match:
        stmt = select(FileInfo).where(FileInfo.file_name == file_name)
    else:
        stmt = select(FileInfo).where(FileInfo.file_name.like(f"%{file_name}%"))
    result = self.session.execute(stmt)
    return result.scalars().all()

def get_storage_details(self, task_id: str) -> Dict[str, Any]:
    """
    获取文件的存储详情
    
    包括实体数、分块数、关系数等
    """
    pass
```

---

## 6. API 路由实现

### 6.1 更新现有路由文件

在 [`backend/api/routers/rag.py`](backend/api/routers/rag.py) 中添加新接口：

```python
from backend.rag.knowledge_management import get_knowledge_management_service

# 请求/响应模型定义...

@router.post("/delete_by_filename")
async def delete_by_filename(
    request: DeleteByFilenameRequest,
    auth: ApiKeyAuth = Depends(verify_api_key)
):
    """按文件名删除知识库内容"""
    pass

@router.post("/list_files")
async def list_files(
    request: ListFilesRequest,
    auth: ApiKeyAuth = Depends(verify_api_key)
):
    """按文件名查询已入库文件"""
    pass

@router.get("/stats")
async def get_stats(
    auth: ApiKeyAuth = Depends(verify_api_key)
):
    """获取知识库统计信息"""
    pass
```

---

## 7. 文件结构

```
backend/
├── api/
│   └── routers/
│       └── rag.py                  # 更新：添加新接口
├── db/
│   └── crud/
│       └── file_info_crud.py       # 更新：添加新方法
└── rag/
    ├── knowledge_management.py     # 新建：知识库管理服务
    └── service.py                  # 复用：RagService
```

---

## 8. 安全考虑

1. **认证**: 所有接口需要 API Key 认证
2. **授权**: 可扩展添加角色权限控制
3. **审计**: 记录删除操作日志
4. **软删除**: 可选支持软删除模式
5. **备份**: 建议在删除前备份重要数据

---

## 9. 错误处理

所有接口在出错时返回统一的错误格式：

```json
{
  "detail": "错误信息描述"
}
```

| 错误码 | 说明 |
|-------|------|
| 400 | 请求参数错误 |
| 401 | 认证失败 |
| 404 | 文件不存在 |
| 500 | 服务器内部错误 |
| 503 | 存储服务不可用 |

---

## 10. 测试计划

### 10.1 单元测试

- `test_knowledge_management_service.py` - 服务层测试
- `test_file_info_crud.py` - CRUD 测试

### 10.2 集成测试

- 测试删除流程完整性
- 测试查询功能准确性
- 测试并发删除安全性

### 10.3 API 测试

- 使用 Postman/FastAPI TestClient 测试接口

---

## 11. 实施计划

| 阶段 | 任务 | 说明 |
|-----|------|------|
| Phase 1 | 实现服务层 | 创建 `KnowledgeManagementService` 类 |
| Phase 2 | 更新数据访问层 | 为 `FileInfoCRUD` 添加新方法 |
| Phase 3 | 实现 API 路由 | 在 `rag.py` 中添加新接口 |
| Phase 4 | 编写测试 | 单元测试和集成测试 |
| Phase 5 | 更新文档 | 更新 API 文档和使用说明 |

---

## 12. 核心组件引用

- **RAG 服务**: [`backend/rag/service.py`](backend/rag/service.py) - `RagService` 类
- **文件信息 CRUD**: [`backend/db/crud/file_info_crud.py`](backend/db/crud/file_info_crud.py) - `FileInfoCRUD` 类
- **RAG 路由**: [`backend/api/routers/rag.py`](backend/api/routers/rag.py)

---

## 13. 数据存储组件配置

- **NebulaGraph**: `<minio_server_ip>:9669`（示例；以 `NEBULA_HOST` / `NEBULA_GRAPH_PORT` 为准）
- **Milvus**: `http://<minio_server_ip>:19530`
- **Redis**: 通过 RagService 初始化时配置