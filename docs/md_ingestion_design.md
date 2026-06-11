# MD文件入库接口设计方案

> **状态**：✅ **已实现**（2026-05-27 同步）  
> **实现路径**：上传 `.md` → `POST /api/v1/uploads/batch` → 触发入库 → `PendingIngestProcessor` 走 `md-direct` 路由（跳过 MinerU）→ `MDIngestor`。  
> **API 文档**：[md_ingestion_api.md](./md_ingestion_api.md) · **模块说明**：[docs_backend/rag_module.md](./docs_backend/rag_module.md)  
> **未实现**：设计稿中的 `POST /api/kb/{kb_id}/documents/md` 直接 POST MD 文本接口（当前仅支持文件上传路径）。

## 1. 背景与现状

当前系统仅支持 **PDF文件** 的入库，完整流程如下：

```
用户上传PDF → MinIO存储 → PendingFile记录(status=0)
  → 触发入库(status=1) → Worker拉取 → MinerU解析PDF→MD
    → MDIngestor入库RAG → KnowledgeBaseDocument落库
```

核心链路依赖 MinerU 做 PDF→MD 的转换。对于 **已存在的MD文件**（如技术文档、开发者笔记），可以直接跳过 MinerU 解析步骤，大幅提升入库效率和资源利用率。

## 2. 架构设计

### 2.1 整体架构

```
┌──────────────────────────────────────────────────────────────────────┐
│                             客户端                                      │
└──────────┬──────────────────────────────────────────────────────┬────┘
           │ 方式A: 上传.md文件                                   │ 方式B: 直接POST MD内容
           ▼                                                      ▼
┌──────────────────────┐    ┌──────────────────────────────────────────┐
│  POST /uploads/batch │    │  POST /api/kb/{kb_id}/documents/md      │
│  (已有接口,支持.md)    │    │  (新增接口,直接接收MD文本)                 │
└──────────┬───────────┘    └──────────────┬───────────────────────────┘
           │                               │
           ▼                               ▼
┌──────────────────────────────────────────────────────────────────────┐
│                        PendingFile (status=0)                        │
│               file_type 区分: "text/markdown" / "application/pdf"    │
└──────────────────────────────────┬───────────────────────────────────┘
                                   │ 触发入库 (status=1)
                                   ▼
┌──────────────────────────────────────────────────────────────────────┐
│                     PendingIngestProcessor.process_file()            │
│                            ↓ 判断 file_type                          │
├──────────────────────┬───────────────────────────────────────────────┤
│  file_type=pdf       │  file_type=text/markdown                      │
│  ┌───────────────┐   │  ┌──────────────────────┐                     │
│  │ MinerU解析    │   │  │ 跳过MinerU           │                     │
│  │ PDF→MD内容    │   │  │ 直接读取MD内容        │                     │
│  └───────┬───────┘   │  └──────────┬───────────┘                     │
└──────────┼───────────┴─────────────┼─────────────────────────────────┘
           │                         │
           ▼                         ▼
┌──────────────────────────────────────────────────────────────────────┐
│                      MDIngestor (复用现有)                            │
│               ingest_from_content(md_content, ...)                   │
│         1. 解析<img>标签提取图片  2. 图片下载&base64                 │
│         3. insert_text_content   4. ImageModalProcessor处理图片      │
└──────────────────────────────────┬───────────────────────────────────┘
                                   ▼
┌──────────────────────────────────────────────────────────────────────┐
│                  KnowledgeBaseDocument 落库                          │
│               PendingFile → status=2 (入库完成)                      │
└──────────────────────────────────────────────────────────────────────┘
```

### 2.2 两种MD入库方式

#### 方式A：上传 `.md` 文件（复用已有上传链路）

**适用场景**：用户有本地的 `.md` 文件需要上传入库

**复用接口**：`POST /uploads/batch`

当前 `ALLOWED_EXTENSIONS` 已包含 `.md`：
```python
ALLOWED_EXTENSIONS = {
    ".md": "text/markdown",
    ".pdf": "application/pdf",
    ...
}
```

**改动点**：`PendingIngestProcessor.process_file()` 中根据 `file_type` 分流

#### 方式B：直接POST MD内容（新增接口）

**适用场景**：
- 其他系统通过API推送MD内容
- 用户从网页编辑器中粘贴Markdown
- 已有MD内容需要批量导入

## 3. 新增 API 接口设计

### 3.1 直接MD内容入库

```
POST /api/kb/{kb_id}/documents/md
```

**Request Body:**
```json
{
  "content": "# 文档标题\n\n这是文档内容...\n\n![图片说明](https://example.com/img.png)",
  "file_name": "技术文档.md",
  "document_date": "2026-05-06",
  "category_path": "技术文档/AI",
  "doc_id": "可选，不传自动生成"
}
```

**Response:**
```json
{
  "success": true,
  "doc_id": "doc-xxxx",
  "file_name": "技术文档.md",
  "text_length": 12345,
  "images_count": 3,
  "chunk_count": 15,
  "entity_count": 42,
  "relation_count": 68,
  "elapsed_time": 2.35
}
```

### 3.2 批量MD内容入库

```
POST /api/kb/{kb_id}/documents/md/batch
```

**Request Body:**
```json
{
  "documents": [
    {
      "content": "# 文档1...",
      "file_name": "文档1.md",
      "category_path": "分类A"
    },
    {
      "content": "# 文档2...",
      "file_name": "文档2.md",
      "category_path": "分类B"
    }
  ],
  "document_date": "2026-05-06"
}
```

### 3.3 参数说明

| 参数 | 类型 | 必填 | 说明 |
|------|------|------|------|
| `content` | string | 是 | Markdown格式的文本内容 |
| `file_name` | string | 是 | 文件名（用于显示和记录） |
| `document_date` | date | 否 | 文档业务日期 |
| `category_path` | string | 否 | 自定义分类路径，如 `"期刊/2024"` |
| `doc_id` | string | 否 | 文档ID（自动生成以 `doc-` 为前缀） |

## 4. 核心改动点

### 4.1 Worker层：PendingIngestProcessor 新增MD分流

**当前代码**：[ingest_pending_processor.py](../backend/worker/components/ingest_pending_processor.py)

当前 `process_file()` 对所有文件都调用 `RagService.process_multimodal()`，内部强制走 MinerU 解析。

**改动方案**：新增 `process_md_direct()` 方法，跳过 MinerU，直接从 MinIO 读取 MD 内容后调用 MDIngestor。

```python
class PendingIngestProcessor:
    """处理 kb_pending_files 中 status=1 的文件。"""

    async def process_file(self, file_id: str) -> bool:
        # ... 现有加锁逻辑 ...

        # 根据文件类型分流
        is_md = (
            pending_file.file_type == "text/markdown"
            or pending_file.file_name.lower().endswith(".md")
        )

        if is_md:
            return await self._process_md_file(pending_file)
        else:
            return await self._process_pdf_file(pending_file)

    async def _process_md_file(self, pending_file: PendingFile) -> bool:
        """处理MD文件：跳过MinerU，直接读取MD内容入库"""
        md_content = self._download_md_content_from_minio(pending_file.minio_path)
        # 直接调用MDIngestor
        ingest_result = await self.rag_service.process_md_content(
            md_content=md_content,
            kb_id=pending_file.kb_id,
            doc_id=doc_id,
            file_name=pending_file.file_name,
            document_date=pending_file.document_date,
            category_path=pending_file.category_path,
        )
        # ... 更新状态逻辑 ...
```

### 4.2 Service层：RagService 新增 process_md_content()

**当前代码**：[service.py](../backend/rag/service.py) 的 `process_multimodal()` 方法

借鉴 `process_multimodal()` 的逻辑，但去掉 MinerU 调用：

```python
async def process_md_content(
    self,
    md_content: str,
    kb_id: str | None = None,
    **kwargs,
) -> Dict[str, Any]:
    """
    直接处理MD内容并入库，绕过MinerU解析

    Args:
        md_content: Markdown文本内容
        kb_id: 知识库ID
        **kwargs: doc_id, file_name, document_date, category_path等
    """
    rag = await self._get_rag_instance(kb_id)
    file_name = kwargs.get("file_name", "untitled.md")
    doc_id = kwargs.get("doc_id")
    document_date = kwargs.get("document_date")
    category_path = kwargs.get("category_path")

    # 复用MDIngestor直接入库
    md_ingestor = MDIngestor(rag, config={
        'context_window': kwargs.get('context_window', 500),
    })
    try:
        result = await md_ingestor.ingest_from_content(
            md_content=md_content,
            doc_id=doc_id,
            file_path=file_name,
            base_url=None,
        )

        if result.get('success'):
            doc_id = result.get('doc_id')
            if doc_id:
                final_chunk_count, final_entity_count, final_relation_count = \
                    await self._finalize_document_after_ingest(
                        rag=rag, kb_id=kb_id, doc_id=doc_id,
                        file_path=file_name, file_name=file_name,
                        document_date=document_date, category_path=category_path,
                        chunk_count=int(result.get('chunk_count') or 0),
                    )
                result['chunk_count'] = final_chunk_count
                result['entity_count'] = final_entity_count
                result['relation_count'] = final_relation_count

        return {
            "success": result.get('success', False),
            "doc_id": result.get('doc_id'),
            "file_path": file_name,
            "file_name": file_name,
            "text_length": result.get('text_length', 0),
            "images_count": result.get('images_count', 0),
            "chunk_count": result.get('chunk_count', 0),
            "entity_count": result.get('entity_count', 0),
            "relation_count": result.get('relation_count', 0),
            "error": result.get('error'),
        }
    finally:
        await md_ingestor.close()
```

### 4.3 API层：新增MD内容入库路由

**新增文件**：`backend/api/routers/md_ingestion.py`

```python
"""MD内容入库路由 - 直接接收Markdown文本并入库"""

import uuid
from typing import List, Optional

from fastapi import APIRouter, Depends, HTTPException
from pydantic import BaseModel, Field

from backend.api.dependencies.auth import ApiKeyAuth, verify_api_key
from backend.config.loggers import get_logger
from backend.rag.service import get_rag_service

router = APIRouter(prefix="/api/kb/{kb_id}/documents/md", tags=["MD文件入库"])
logger = get_logger(__name__)

class MDDocument(BaseModel):
    """MD文档"""
    content: str = Field(..., min_length=1, description="Markdown内容")
    file_name: str = Field(..., description="文件名（建议以.md结尾）")
    category_path: Optional[str] = Field(None, description="自定义分类路径")
    doc_id: Optional[str] = Field(None, description="文档ID，不传自动生成")

class SingleMDIngestRequest(MDDocument):
    """单文档入库请求"""
    document_date: Optional[str] = Field(None, description="文档业务日期(YYYY-MM-DD)")
    pass

class BatchMDIngestRequest(BaseModel):
    """批量MD入库请求"""
    documents: List[MDDocument] = Field(..., min_length=1, max_length=100, description="文档列表")
    document_date: Optional[str] = Field(None, description="文档业务日期(YYYY-MM-DD)")

class MDIngestResult(BaseModel):
    """入库结果"""
    success: bool
    doc_id: Optional[str] = None
    file_name: Optional[str] = None
    text_length: Optional[int] = None
    images_count: Optional[int] = None
    chunk_count: Optional[int] = None
    entity_count: Optional[int] = None
    relation_count: Optional[int] = None
    elapsed_time: Optional[float] = None
    error: Optional[str] = None

class MDIngestResponse(BaseModel):
    """单文档入库响应"""
    success: bool
    data: Optional[MDIngestResult] = None
    error: Optional[str] = None

class BatchMDIngestResponse(BaseModel):
    """批量入库响应"""
    success: bool
    total: int
    successful: int
    failed: int
    results: List[MDIngestResult]

@router.post("", response_model=MDIngestResponse)
async def ingest_md_content(
    kb_id: str,
    request: SingleMDIngestRequest,
    auth: ApiKeyAuth = Depends(verify_api_key),
):
    """
    直接入库MD文本内容

    绕过 MinerU 解析步骤，直接将 Markdown 文本内容入库到RAG系统。
    支持MD中的图片引用（<img>标签），会自动下载并作为多模态内容处理。
    """
    service = get_rag_service()
    await service.initialize()

    try:
        result = await service.process_md_content(
            md_content=request.content,
            kb_id=kb_id,
            doc_id=request.doc_id or str(uuid.uuid4()),
            file_name=request.file_name,
            document_date=request.document_date,
            category_path=request.category_path,
        )

        return MDIngestResponse(
            success=result.get("success", False),
            data=MDIngestResult(
                success=result.get("success", False),
                doc_id=result.get("doc_id"),
                file_name=result.get("file_name"),
                text_length=result.get("text_length"),
                images_count=result.get("images_count"),
                chunk_count=result.get("chunk_count"),
                entity_count=result.get("entity_count"),
                relation_count=result.get("relation_count"),
                elapsed_time=result.get("elapsed_time"),
            ) if result.get("success") else None,
            error=result.get("error"),
        )
    except Exception as e:
        logger.exception(f"MD内容入库失败: {e}")
        raise HTTPException(status_code=500, detail=f"MD内容入库失败: {str(e)}")

@router.post("/batch", response_model=BatchMDIngestResponse)
async def batch_ingest_md_content(
    kb_id: str,
    request: BatchMDIngestRequest,
    auth: ApiKeyAuth = Depends(verify_api_key),
):
    """
    批量入库MD文本内容

    一次性入库多个Markdown文档，每个文档独立处理。
    其中一个失败不影响其他文档的入库。
    """
    service = get_rag_service()
    await service.initialize()

    results = []
    for doc in request.documents:
        try:
            result = await service.process_md_content(
                md_content=doc.content,
                kb_id=kb_id,
                doc_id=doc.doc_id or str(uuid.uuid4()),
                file_name=doc.file_name,
                document_date=request.document_date,
                category_path=doc.category_path,
            )
            results.append(MDIngestResult(
                success=result.get("success", False),
                doc_id=result.get("doc_id"),
                file_name=result.get("file_name"),
                text_length=result.get("text_length"),
                images_count=result.get("images_count"),
                chunk_count=result.get("chunk_count"),
                entity_count=result.get("entity_count"),
                relation_count=result.get("relation_count"),
                elapsed_time=result.get("elapsed_time"),
                error=result.get("error"),
            ))
        except Exception as e:
            results.append(MDIngestResult(
                success=False,
                file_name=doc.file_name,
                error=str(e),
            ))

    successful = sum(1 for r in results if r.success)
    return BatchMDIngestResponse(
        success=successful == len(results),
        total=len(results),
        successful=successful,
        failed=len(results) - successful,
        results=results,
    )
```

### 4.4 注册新路由

在 `http_server.py` 中注册：

```python
from backend.api.routers.md_ingestion import router as md_ingestion_router
app.include_router(md_ingestion_router)
```

### 4.5 数据模型

**无改动**。`PendingFile` 和 `KnowledgeBaseDocument` 表已支持所需字段。

## 5. 与现有PDF流程的对比

| 环节 | PDF入库流程 | MD入库流程 | 优势 |
|------|-----------|-----------|------|
| 上传 | 上传PDF文件 | 上传.md文件 或 POST文本内容 | MD更灵活 |
| 解析 | MinerU解析PDF→MD（耗时5-30秒） | **跳过**，直接使用MD内容 | **节省解析时间** |
| 图片提取 | MinerU自动提取图片 | MDIngestor解析`<img>`标签 | 依赖MD中已有图片引用 |
| 文本切片 | MDIngestor处理 | MDIngestor处理（复用） | 完全相同 |
| 图片多模态 | ImageModalProcessor处理 | ImageModalProcessor处理（复用） | 完全相同 |
| 入库记录 | KnowledgeBaseDocument | KnowledgeBaseDocument（复用） | 完全相同 |

## 6. 实施步骤

| 步骤 | 内容 | 涉及文件 |
|------|------|---------|
| 1 | `RagService` 新增 `process_md_content()` 方法 | [service.py](../backend/rag/service.py) |
| 2 | `PendingIngestProcessor` 增加MD分流逻辑 | [ingest_pending_processor.py](../backend/worker/components/ingest_pending_processor.py) |
| 3 | 新增 `md_ingestion.py` API路由 | `backend/api/routers/md_ingestion.py` (新增) |
| 4 | 在 `http_server.py` 注册新路由 | [http_server.py](../backend/api/http_server.py) |
| 5 | 端到端测试 | MD内容入库 vs PDF入库 |

## 7. 总结

当前系统已有非常好的基础：

- [MDIngestor](../backend/rag/raganything/md_ingestor.py) 已支持 `ingest_from_content()` 直接处理MD文本
- [PendingFile](../backend/db/models/knowledge_base.py) 表已有 `file_type` 字段可区分文件类型
- [uploads.py](../backend/api/routers/uploads.py) 已支持 `.md` 文件扩展名

核心改动量很小——主要在 Worker 层增加一个类型分流判断，以及新增一个直接接收MD文本的API接口。上传 `.md` 文件时跳过 MinerU 只需修改 `PendingIngestProcessor` 中的处理逻辑即可。
