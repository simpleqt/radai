# 对话文件上传与智能处理系统设计文档

> **版本**: v1.0
> **日期**: 2026-05-11
> **状态**: ⚠️ 部分实现（2026-06-08）
> **架构基线**：本文中的设计示例已按当前平台执行链路校正。工具实现位于 `backend/application/tools/providers/`，统一由 `ToolAssemblyPipeline` 按 profile、workflow 和请求上下文装配；不得再向 `backend/custom/agents` 增加依赖。
> 已实现：`/api/v2/chat-files` 上传、`useFileUpload`、`FilePreviewChip`、Agent 侧文件元数据、`ChatFileIndex` + `chat_file_search`（`chat_files.py` 上传后建索引）。
> 已实现补充：PDF/DOCX/PPTX 图片提取、文档图片上传到 MinIO、短文档图片 `image_url` 多模态注入、长文档检索图片 token 注册、MinerU `upload_images=true` 远程图片 URL 保留。
> 待完成：索引跨进程持久化、完整 `/chat-upload` 单路由封装。MinerU OCR 仍依赖网关 URL 配置。
> **实现说明**：实际上传走 `/api/v2/chat-files/upload`，非设计稿中的 `/api/v2/agent/chat-upload`。

### 设计 vs 实现对照

| 设计项 | 实现状态 | 实际路径/组件 |
| :--- | :--- | :--- |
| 对话附件上传 | ✅ | `POST /api/v2/chat-files/upload` |
| 前端上传组件 | ✅ | `useFileUpload`、`FilePreviewChip` |
| Agent 读取附件元数据 | ✅ | query 请求携带 `attachments` |
| 图片多模态直传 | ✅ | VLM 模型理解 |
| 短文档注入上下文 | ✅ | 解析后 inline 注入 |
| 文档内嵌图片提取与上传 | ✅ | `chat_file_parser.py` + `chat_files.py` |
| 文档图片 URL 替换 | ✅ | 本地图片引用在入库前替换为 MinIO / MinerU URL |
| 短文档图片多模态注入 | ✅ | 标准 Agent Runtime 请求上下文注入 `multimodal_images` |
| 长文档检索图片引用 | ✅ | `backend/application/tools/providers/chat_file_search_tool.py` + `image_url_registry.py` |
| 长文档 BM25 会话索引 | ✅ | `ChatFileIndex` + `strategy=indexed` + `chat_file_search`（见 `ToolAssemblyPipeline`） |
| MinerU 复杂 PDF 回退 | ⚠️ | `chat_file_parser.py` 支持 MinerU OCR 回退（依赖网关 URL 配置） |
| 单路由 `/agent/chat-upload` | ❌ | 拆分为 `chat-files` 独立路由 |

---

## 一、需求概述

在现有"核电子学智能助手"对话界面中，新增文件上传功能。系统根据文件类型和模型能力，自动选择最优处理策略：

| 文件类型 | 处理策略 |
|---------|---------|
| 图片（jpg/png/gif/webp） | 多模态模型直接理解 |
| 短文档（≤5000字） | 解析后直接注入模型上下文 |
| 长文档（>5000字） | 解析后建立会话级轻量索引（BM25+TF-IDF），Agent 按需召回 |
| 复杂版式文档（扫描PDF等） | 调用 MinerU 进行深度解析 |

---

## 二、整体架构

```
┌─────────────────────────────────────────────────────────┐
│                     Frontend (Vue3)                      │
│  ┌──────────┐  ┌──────────┐  ┌────────────────────────┐ │
│  │ ChatBox  │  │FileUpload│  │  FilePreviewChip       │ │
│  │(已有组件)│──│ (新组件)  │──│  (新组件: 附件气泡)    │ │
│  └────┬─────┘  └────┬─────┘  └────────────────────────┘ │
│       │              │                                    │
│       ▼              ▼                                    │
│  ┌─────────────────────────────────────────┐             │
│  │      useAgentChat (增强: 支持附件)       │             │
│  │  sendMessage(text, { attachments })     │             │
│  └─────────────────────┬───────────────────┘             │
└────────────────────────┼─────────────────────────────────┘
                         │ POST /api/v2/agent/chat-upload
                         │ (multipart/form-data)
                         ▼
┌─────────────────────────────────────────────────────────┐
│                     Backend (FastAPI)                     │
│                                                          │
│  ┌──────────────────────────────────────────────┐       │
│  │         ChatFileRouter (新路由)                │       │
│  │   POST /chat-upload  →  上传+分类+解析        │       │
│  └───────────────────────┬──────────────────────┘       │
│                          │                               │
│         ┌────────────────┼───────────────────┐          │
│         ▼                ▼                   ▼          │
│  ┌────────────┐  ┌──────────────┐  ┌────────────────┐  │
│  │ 多模态直传  │  │ 文档解析引擎  │  │ MinerU OCR     │  │
│  │(图片→模型) │  │(Python解析)  │  │(复杂文档回退)  │  │
│  └────────────┘  └──────┬───────┘  └───────┬────────┘  │
│                         │                   │           │
│                         ▼                   ▼           │
│              ┌─────────────────────────┐                │
│              │   内容长度路由决策       │                │
│              │  ≤5000字 → 注入上下文   │                │
│              │  >5000字 → 会话级索引   │                │
│              └──────────┬──────────────┘                │
│                         │                               │
│              ┌──────────▼──────────────┐                │
│              │  ChatFileIndex (新模块)  │                │
│              │  BM25 召回 + TF-IDF 重排 │                │
│              │  纯Python, 注册Agent工具 │                │
│              └─────────────────────────┘                │
└─────────────────────────────────────────────────────────┘
```

---

## 三、文件类型分类与处理策略

### 3.1 文件类型定义

```python
# 多模态文件：直接传给支持 vision 的模型
MULTIMODAL_EXTENSIONS = {".jpg", ".jpeg", ".png", ".gif", ".webp", ".bmp", ".svg"}

# 可直接用 Python 解析的文档
PYTHON_PARSEABLE = {
    ".txt": "text/plain",
    ".md": "text/markdown",
    ".csv": "text/csv",
    ".json": "application/json",
    ".py": "text/x-python",
    ".html": "text/html",
    ".xml": "text/xml",
}

# 需要专用解析器的文档
RICH_DOCUMENT_EXTENSIONS = {
    ".pdf": "application/pdf",
    ".docx": "application/vnd.openxmlformats-officedocument.wordprocessingml.document",
    ".doc": "application/msword",
    ".pptx": "application/vnd.openxmlformats-officedocument.presentationml.presentation",
    ".ppt": "application/vnd.ms-powerpoint",
    ".xlsx": "application/vnd.openxmlformats-officedocument.spreadsheetml.sheet",
    ".xls": "application/vnd.ms-excel",
}

# 当 Python 解析器无法处理时，回退到 MinerU 的文件类型
MINERU_FALLBACK_EXTENSIONS = {".pdf", ".doc", ".ppt", ".xls"}
```

### 3.2 处理流程决策树

```
用户上传文件
  │
  ├── 图片类型（jpg/png/gif/webp/bmp/svg）
  │     │
  │     ├── 当前模型 supports_vision == true
  │     │     └── 以 base64/URL 形式作为多模态消息内容传给模型
  │     │
  │     └── 当前模型不支持 vision
  │           └── 返回提示"当前模型不支持图片理解，请切换模型"
  │
  ├── 纯文本类型（txt/md/csv/json/py/html/xml）
  │     └── 直接读取文件内容 → 进入【长度路由】
  │
  ├── 富文档类型（pdf/docx/pptx/xlsx）
  │     │
  │     ├── 尝试 Python 解析（PyPDF2/python-docx/openpyxl/python-pptx）
  │     │     │
  │     │     ├── 解析成功且内容非空
  │     │     │     ├── 提取文本
  │     │     │     ├── 提取嵌入图片（PDF/DOCX/PPTX）
  │     │     │     └── 进入【长度路由】
  │     │     │
  │     │     └── 解析失败或内容为空（扫描PDF等）
  │     │           └── 回退到 MinerU OCR 解析 → 进入【长度路由】
  │     │
  │     └── 其他不可解析格式
  │           └── 直接回退到 MinerU OCR
  │
  └── 上传后处理
        │
        ├── 原始文件上传 MinIO
        ├── 提取到的图片上传 MinIO
        ├── MinerU 远程图片 URL 合并入 metadata.image_urls
        ├── 文档中的本地图片引用替换为可访问 URL
        └── 进入【长度路由】
  
  └──【长度路由】
        │
        ├── 内容长度 ≤ 5000 字
        │     └── 短文档模式：
        │           ├── 文本直接拼入用户消息上下文
        │           └── 最多 N 张文档图片作为 `image_url` 多模态内容传给模型
        │
        └── 内容长度 > 5000 字
              └── 长文档模式：
                    ├── 内容分块存入内存级 ChatFileIndex
                    ├── 构建 BM25 索引 + TF-IDF 向量（纯Python，<100ms）
                    └── 注册 chat_file_search 工具到当前会话的 Agent 工具集
                          └── 检索结果中的文档图片通过 `[IMAGE:xxx]` token 暴露给模型
```

### 3.3 文档图片处理现状

当前代码中的文档图片链路如下：

1. `backend/services/chat_file_parser.py`
   - Python 解析路径支持提取 PDF / DOCX / PPTX 内嵌图片。
   - MinerU 路径同时支持两种图片来源：
     - `images` 字段中的 base64 图片，转为二进制后继续上传。
     - `images` 字段中的远程 URL，直接保留到 `ParseResult.image_urls`。
2. `backend/api/routers/chat_files.py`
   - 原始文件始终上传到 MinIO。
   - 解析器提取出的图片会单独上传到 `chat_files/{user_id}/{file_id}/images/{img.name}`。
   - 文档 markdown / HTML 中的本地图片引用会在入库前替换为可访问 URL。
   - 最终写入 `metadata.image_urls`，供 Agent 和检索工具复用。
3. 标准 Agent Runtime 附件预处理链路
   - 短文档文本直接拼入 `<uploaded_document>`。
   - 同时从 `metadata.image_urls` 中读取图片，按上限注入 `multimodal_images`。
4. `backend/application/tools/providers/chat_file_search_tool.py`
   - 长文档检索时注册图片 URL 为 `[IMAGE:xxx]` token。
   - 流式输出阶段再由 `StreamingTokenResolver` 还原为真实 URL。

---

## 四、数据库设计

### 4.1 新增表：`chat_uploaded_files`

会话级上传文件记录表，追踪用户在对话中上传的每个文件及其处理状态。

```sql
CREATE TABLE chat_uploaded_files (
    id            VARCHAR(36)  PRIMARY KEY,           -- UUID
    thread_id     VARCHAR(100) NOT NULL,              -- 所属会话线程
    user_id       VARCHAR(36)  NOT NULL,              -- 上传用户
    file_name     VARCHAR(255) NOT NULL,              -- 原始文件名
    file_size     BIGINT       NOT NULL,              -- 文件字节数
    mime_type     VARCHAR(100),                        -- MIME 类型
    file_category VARCHAR(20)  NOT NULL,              -- multimodal / short_doc / long_doc
    storage_path  VARCHAR(500),                        -- MinIO 对象路径
    
    -- 解析信息
    parse_method  VARCHAR(20)  DEFAULT 'direct',      -- direct / python / mineru
    parse_status  VARCHAR(20)  DEFAULT 'pending',     -- pending / parsing / completed / failed
    content_text  MEDIUMTEXT,                          -- 解析后的纯文本（短文档直接存储）
    content_length INT         DEFAULT 0,              -- 文本字符数
    chunk_count   INT          DEFAULT 0,              -- 长文档分块数
    error_message TEXT,                                -- 解析错误信息
    
    -- 索引信息（长文档）
    index_status  VARCHAR(20)  DEFAULT 'none',        -- none / building / ready / failed
    bm25_index_key VARCHAR(255),                       -- Redis 中 BM25 索引的 key
    
    created_at    DATETIME     DEFAULT CURRENT_TIMESTAMP,
    updated_at    DATETIME     DEFAULT CURRENT_TIMESTAMP ON UPDATE CURRENT_TIMESTAMP,
    
    INDEX idx_thread_id (thread_id),
    INDEX idx_user_id (user_id),
    INDEX idx_parse_status (parse_status)
) ENGINE=InnoDB DEFAULT CHARSET=utf8mb4;
```

### 4.2 新增表：`chat_file_chunks`

长文档的分块内容存储表，支持混合检索。

```sql
CREATE TABLE chat_file_chunks (
    id            VARCHAR(36)  PRIMARY KEY,
    file_id       VARCHAR(36)  NOT NULL,              -- 关联 chat_uploaded_files.id
    thread_id     VARCHAR(100) NOT NULL,
    chunk_index   INT          NOT NULL,              -- 块序号
    content       MEDIUMTEXT   NOT NULL,              -- 块内容
    token_count   INT          DEFAULT 0,             -- token 数
    embedding     BLOB,                                -- 向量嵌入（可选，使用 numpy 序列化）
    
    created_at    DATETIME     DEFAULT CURRENT_TIMESTAMP,
    
    INDEX idx_file_id (file_id),
    INDEX idx_thread_id (thread_id),
    INDEX idx_chunk_order (file_id, chunk_index)
) ENGINE=InnoDB DEFAULT CHARSET=utf8mb4;
```

---

## 五、后端设计

### 5.1 新增 API 端点

#### `POST /api/v2/agent/chat-upload`

对话文件上传接口，支持同时上传多个文件。

**请求 (multipart/form-data)**:

| 字段 | 类型 | 必填 | 说明 |
|------|------|------|------|
| `files` | File[] | 是 | 上传的文件列表 |
| `thread_id` | string | 是 | 会话线程 ID |

**响应**:

```json
{
  "success": true,
  "files": [
    {
      "file_id": "uuid",
      "file_name": "论文.pdf",
      "file_category": "long_doc",
      "parse_status": "completed",
      "content_length": 35000,
      "strategy": "indexed",
      "message": "文档已解析并建立索引，可通过对话检索"
    },
    {
      "file_id": "uuid",
      "file_name": "diagram.png",
      "file_category": "multimodal",
      "parse_status": "ready",
      "strategy": "vision",
      "message": "图片将直接发送给模型分析"
    }
  ]
}
```

#### `GET /api/v2/agent/chat-files/{thread_id}`

获取会话中已上传的文件列表。

#### `DELETE /api/v2/agent/chat-files/{file_id}`

删除会话中的某个上传文件及其索引。

### 5.2 文档解析服务：`ChatFileParser`

```python
# backend/services/chat_file_parser.py

class ChatFileParser:
    """对话文件解析服务 - 三级解析策略"""

    # 短文档阈值（字符数）
    SHORT_DOC_THRESHOLD = 5000
    # 分块大小（字符数）
    CHUNK_SIZE = 800
    CHUNK_OVERLAP = 100

    async def parse_file(self, file_path: str, mime_type: str) -> ParseResult:
        """解析文件，返回解析结果"""
        ext = Path(file_path).suffix.lower()

        # 策略1：多模态文件不解析文本
        if ext in MULTIMODAL_EXTENSIONS:
            return ParseResult(
                category="multimodal",
                method="none",
                content=None,
            )

        # 策略2：纯文本直接读取
        if ext in PYTHON_PARSEABLE:
            content = await self._read_text_file(file_path)
            return self._categorize_by_length(content, method="direct")

        # 策略3：富文档先尝试 Python 解析
        if ext in RICH_DOCUMENT_EXTENSIONS:
            content = await self._try_python_parse(file_path, ext)
            if content and len(content.strip()) > 50:
                return self._categorize_by_length(content, method="python")

            # 策略4：回退到 MinerU
            content = await self._parse_with_mineru(file_path)
            if content:
                return self._categorize_by_length(content, method="mineru")

            return ParseResult(
                category="failed",
                method="mineru",
                content=None,
                error="文档解析失败，无法提取有效内容",
            )

        # 未知类型：尝试 MinerU
        content = await self._parse_with_mineru(file_path)
        if content:
            return self._categorize_by_length(content, method="mineru")

        return ParseResult(category="failed", method="unknown", content=None)

    async def _try_python_parse(self, file_path: str, ext: str) -> str | None:
        """Python 库解析富文档"""
        try:
            if ext == ".pdf":
                return await self._parse_pdf(file_path)
            elif ext in (".docx",):
                return await self._parse_docx(file_path)
            elif ext in (".xlsx", ".xls"):
                return await self._parse_excel(file_path)
            elif ext in (".pptx", ".ppt"):
                return await self._parse_pptx(file_path)
        except Exception as e:
            logger.warning(f"Python 解析失败 [{ext}]: {e}")
            return None
        return None

    async def _parse_with_mineru(self, file_path: str) -> str | None:
        """调用 MinerU OCR 解析文档"""
        from backend.core.tools.ocr.mineru_ocr import MinerUOCR
        from backend.config.settings import settings

        ocr = MinerUOCR(api_url=settings.mineru_api_url)
        try:
            result = await ocr.process_file_async(file_path)
            if result.is_success() and result.markdown:
                return result.markdown
        except Exception as e:
            logger.error(f"MinerU 解析失败: {e}")
        finally:
            await ocr.close()
        return None

    def _categorize_by_length(self, content: str, method: str) -> ParseResult:
        """根据内容长度分类为短文档或长文档"""
        length = len(content)
        if length <= self.SHORT_DOC_THRESHOLD:
            return ParseResult(
                category="short_doc",
                method=method,
                content=content,
                content_length=length,
            )
        return ParseResult(
            category="long_doc",
            method=method,
            content=content,
            content_length=length,
        )
```

### 5.3 会话级文档索引：`ChatFileIndex`（轻量方案）

> **设计原则**：对话级文件检索不需要知识库级别的重型基础设施。
> 采用 **BM25 关键词召回 + TF-IDF 余弦轻量重排** 的纯 Python 方案，
> **零外部依赖**（不调 embedding API、不需要 Milvus/Redis），索引构建瞬间完成。

#### 为什么不用 Embedding 向量检索？

| 维度 | Embedding 方案 | 轻量方案（本设计） |
|------|---------------|-------------------|
| 索引构建 | 每块调 API 算向量，数百块≈10-30s | 纯内存分词统计，<100ms |
| 查询延迟 | 调 API 算 query 向量 + numpy 距离计算 | 纯 Python 字符串运算，<10ms |
| 外部依赖 | embedding API 服务必须在线 | 无，纯 CPU |
| 内存占用 | 每块 1536*4B ≈ 6KB 向量，额外 numpy | 仅存文本和词频字典 |
| 适用场景 | 知识库级万级文档 | 对话级几十到几百块 |

#### 检索策略：BM25 + TF-IDF 余弦重排 + 上下文窗口扩展

```
查询 query
  │
  ├── 第1路：BM25 关键词召回（top_k × 3 过采样）
  │     复用项目已有的 BM25Index，中文滑动窗口分词
  │
  ├── 第2路：TF-IDF 余弦相似度轻量重排
  │     对 BM25 召回的候选块，用 TF-IDF 向量做余弦相似度精排
  │     纯 Python 实现，无需外部 API
  │
  ├── 融合：加权合并
  │     final_score = 0.6 × norm(bm25) + 0.4 × tfidf_cosine
  │
  └── 上下文窗口扩展
        命中的块自动携带前后各1个相邻块，提供连贯上下文
```

```python
# backend/services/chat_file_index.py

import math
import re
from collections import Counter
from dataclasses import dataclass, field

from backend.rag.keywords.bm25_index import BM25Index

@dataclass
class ChunkMeta:
    file_id: str
    file_name: str
    chunk_index: int
    content: str

@dataclass
class SearchResult:
    content: str
    file_name: str
    file_id: str
    chunk_index: int
    score: float

class ChatFileIndex:
    """会话级文档轻量检索索引

    BM25 召回 + TF-IDF 余弦重排，纯 Python 实现。
    不依赖任何外部服务（无 embedding API / Milvus / Redis）。
    """

    def __init__(self, thread_id: str):
        self.thread_id = thread_id
        self.bm25 = BM25Index()
        self._chunks: list[ChunkMeta] = []
        self._idf: dict[str, float] = {}       # 全局 IDF
        self._chunk_tfidf: list[dict[str, float]] = []  # 每块的 TF-IDF 向量

    # ── 索引构建 ─────────────────────────────────────────

    def add_document(
        self,
        file_id: str,
        content: str,
        file_name: str,
        chunk_size: int = 800,
        chunk_overlap: int = 100,
    ) -> int:
        """分块并建立索引，同步完成，通常 <100ms"""
        chunks = _split_text(content, chunk_size, chunk_overlap)

        for i, text in enumerate(chunks):
            self._chunks.append(ChunkMeta(
                file_id=file_id,
                file_name=file_name,
                chunk_index=i,
                content=text,
            ))
            self.bm25.add_document(text)

        self._rebuild_tfidf()
        return len(chunks)

    def _rebuild_tfidf(self):
        """重建全局 IDF 和每块 TF-IDF 向量"""
        n = len(self._chunks)
        if n == 0:
            return

        doc_freq: dict[str, int] = {}
        tokenized: list[list[str]] = []

        for chunk in self._chunks:
            tokens = _tokenize_simple(chunk.content)
            tokenized.append(tokens)
            for term in set(tokens):
                doc_freq[term] = doc_freq.get(term, 0) + 1

        self._idf = {
            term: math.log((n + 1) / (df + 1)) + 1
            for term, df in doc_freq.items()
        }

        self._chunk_tfidf = []
        for tokens in tokenized:
            tf = Counter(tokens)
            total = max(len(tokens), 1)
            tfidf = {
                term: (count / total) * self._idf.get(term, 1.0)
                for term, count in tf.items()
            }
            self._chunk_tfidf.append(tfidf)

    # ── 检索 ─────────────────────────────────────────────

    def search(
        self,
        query: str,
        top_k: int = 5,
        bm25_weight: float = 0.6,
        tfidf_weight: float = 0.4,
        expand_window: int = 1,
    ) -> list[SearchResult]:
        """混合检索：BM25 召回 + TF-IDF 余弦重排 + 上下文窗口"""
        if not self._chunks:
            return []

        # 第1步：BM25 过采样召回
        bm25_results = self.bm25.search(query, top_k=top_k * 3)
        if not bm25_results:
            return []

        candidate_indices = [idx for idx, _ in bm25_results]
        bm25_scores = {idx: score for idx, score in bm25_results}

        # 第2步：TF-IDF 余弦相似度对候选精排
        query_tfidf = self._compute_query_tfidf(query)
        tfidf_scores: dict[int, float] = {}
        for idx in candidate_indices:
            tfidf_scores[idx] = _cosine_sim(query_tfidf, self._chunk_tfidf[idx])

        # 第3步：分数归一化 + 加权融合
        bm25_max = max(bm25_scores.values()) if bm25_scores else 1.0
        tfidf_max = max(tfidf_scores.values()) if tfidf_scores else 1.0

        fused: dict[int, float] = {}
        for idx in candidate_indices:
            bm25_norm = bm25_scores.get(idx, 0) / max(bm25_max, 1e-9)
            tfidf_norm = tfidf_scores.get(idx, 0) / max(tfidf_max, 1e-9)
            fused[idx] = bm25_weight * bm25_norm + tfidf_weight * tfidf_norm

        ranked = sorted(fused.items(), key=lambda x: -x[1])[:top_k]

        # 第4步：上下文窗口扩展（携带前后相邻块）
        results = []
        seen_files: dict[str, set[int]] = {}
        for idx, score in ranked:
            chunk = self._chunks[idx]
            fid = chunk.file_id
            if fid not in seen_files:
                seen_files[fid] = set()

            expanded = self._expand_context(idx, expand_window, seen_files[fid])
            seen_files[fid].update(
                range(max(0, idx - expand_window), min(len(self._chunks), idx + expand_window + 1))
            )
            results.append(SearchResult(
                content=expanded,
                file_name=chunk.file_name,
                file_id=fid,
                chunk_index=chunk.chunk_index,
                score=score,
            ))
        return results

    def _compute_query_tfidf(self, query: str) -> dict[str, float]:
        tokens = _tokenize_simple(query)
        tf = Counter(tokens)
        total = max(len(tokens), 1)
        return {
            term: (count / total) * self._idf.get(term, 1.0)
            for term, count in tf.items()
        }

    def _expand_context(
        self, center_idx: int, window: int, already_seen: set[int]
    ) -> str:
        """扩展上下文窗口，将前后块拼入"""
        parts = []
        file_id = self._chunks[center_idx].file_id
        for offset in range(-window, window + 1):
            i = center_idx + offset
            if i < 0 or i >= len(self._chunks):
                continue
            if self._chunks[i].file_id != file_id:
                continue
            if i in already_seen and i != center_idx:
                continue
            parts.append(self._chunks[i].content)
        return "\n\n".join(parts)

    @property
    def chunk_count(self) -> int:
        return len(self._chunks)

    @property
    def file_count(self) -> int:
        return len({c.file_id for c in self._chunks})

# ── 工具函数 ─────────────────────────────────────────────

def _tokenize_simple(text: str) -> list[str]:
    """轻量分词：中文按字/bigram，英文按单词"""
    words = []
    for m in re.finditer(r'[\u4e00-\u9fff]+', text):
        s = m.group()
        words.extend(s)                       # 单字
        for i in range(len(s) - 1):           # bigram
            words.append(s[i:i+2])
    for m in re.finditer(r'[a-zA-Z]{2,}', text):
        words.append(m.group().lower())
    for m in re.finditer(r'\d+(?:\.\d+)?', text):
        words.append(m.group())
    return words

def _cosine_sim(a: dict[str, float], b: dict[str, float]) -> float:
    """两个稀疏 TF-IDF 向量的余弦相似度"""
    if not a or not b:
        return 0.0
    common = set(a) & set(b)
    if not common:
        return 0.0
    dot = sum(a[k] * b[k] for k in common)
    norm_a = math.sqrt(sum(v * v for v in a.values()))
    norm_b = math.sqrt(sum(v * v for v in b.values()))
    if norm_a < 1e-9 or norm_b < 1e-9:
        return 0.0
    return dot / (norm_a * norm_b)

def _split_text(
    text: str, chunk_size: int = 800, chunk_overlap: int = 100,
) -> list[str]:
    """智能分块 - 尊重段落和句子边界"""
    paragraphs = re.split(r'\n{2,}', text)
    chunks: list[str] = []
    current = ""

    for para in paragraphs:
        if len(current) + len(para) <= chunk_size:
            current += ("\n\n" + para) if current else para
        else:
            if current:
                chunks.append(current.strip())
            if len(para) > chunk_size:
                sentences = re.split(r'(?<=[。！？.!?])\s*', para)
                current = ""
                for sent in sentences:
                    if len(current) + len(sent) <= chunk_size:
                        current += sent
                    else:
                        if current:
                            chunks.append(current.strip())
                        current = sent
            else:
                current = para

    if current.strip():
        chunks.append(current.strip())
    return chunks
```

### 5.4 Agent 工具注册：`chat_file_search`

在工具装配流水线 `ToolAssemblyPipeline` 中，根据当前会话是否有长文档索引，动态注册检索工具。

```python
# 在 backend/application/tools/providers/chat_file_search_tool.py 中定义

from backend.services.chat_file_index import ChatFileIndexManager

_index_manager = ChatFileIndexManager(max_threads=100)

def create_scoped_chat_file_search_tool(thread_id: str):
    """为指定会话创建带作用域的文档检索工具"""

    @tool("chat_file_search")
    def chat_file_search(
        query: str,
        top_k: int = 5,
    ) -> str:
        """搜索用户在当前对话中上传的文档内容。

        当用户上传了较长的文档时，使用此工具在文档中搜索相关信息。
        使用关键词匹配 + 轻量重排，自动找到最相关的文档片段。
        每个片段会自动携带前后文上下文，确保信息连贯。

        Args:
            query: 搜索查询内容，描述你想在文档中查找的信息
            top_k: 返回的最相关片段数量，默认5
        """
        index = _index_manager.get(thread_id)
        if not index or index.chunk_count == 0:
            return "当前对话中没有已上传的文档。"

        results = index.search(query, top_k=top_k)
        if not results:
            return "在上传的文档中未找到与查询相关的内容。"

        parts = []
        for i, r in enumerate(results, 1):
            parts.append(
                f"### 片段 {i} (来自: {r.file_name}, 相关度: {r.score:.2f})\n\n{r.content}"
            )

        header = f"从 {index.file_count} 个上传文档中检索到 {len(results)} 个相关片段：\n\n"
        return header + "\n\n---\n\n".join(parts)

    return chat_file_search

def has_thread_file_index(thread_id: str) -> bool:
    """检查指定会话是否有文件索引"""
    index = _index_manager.get(thread_id)
    return index is not None and index.chunk_count > 0
```

### 5.5 工具装配集成

在 `ToolAssemblyPipeline.assemble()` 中增加会话文件检索工具的注入逻辑：

```python
# backend/application/tools/tool_assembly_pipeline.py

def assemble(self) -> list[BaseTool]:
    # ... 现有逻辑 ...

    # 注入会话文件检索工具（如果当前线程有上传文档索引）
    if self._thread_id and has_thread_file_index(self._thread_id):
        from backend.application.tools.providers.chat_file_search_tool import (
            create_scoped_chat_file_search_tool,
        )
        file_search = create_scoped_chat_file_search_tool(self._thread_id)
        tools_by_name[file_search.name] = file_search
        logger.info("[ToolAssembly] 已注入 chat_file_search 工具（会话文件检索）")

    return list(tools_by_name.values())
```

### 5.6 查询增强 - 短文档上下文注入

对于短文档，不需要检索工具，直接将内容拼入用户消息：

```python
# 实际代码已扩展为：短文档文本 + 文档图片分开注入

runtime_question += (
    f'\n\n<uploaded_document name="{meta["file_name"]}">\n'
    f'{meta["content"]}\n'
    f'</uploaded_document>'
)

for img_info in meta.get("image_urls", [])[:10]:
    multimodal_images.append({
        "image_url": img_info["url"],
        "caption": img_info.get("caption", ""),
        "file_name": meta["file_name"],
    })
```

---

## 六、前端设计

### 6.1 新增组件

#### `FileUploadButton.vue` - 上传按钮与拖拽区

集成在 `ChatBox.vue` 的输入框左侧，支持：

- 点击选择文件
- 拖拽文件到输入区域
- 粘贴图片（Ctrl+V）
- 多文件同时上传
- 上传进度条

#### `FilePreviewChip.vue` - 附件预览气泡

显示在输入框上方，展示已选择/上传的文件：

```
┌─────────────────────────────────────────────────────┐
│  ┌────────────┐  ┌──────────────┐                   │
│  │ 📄 论文.pdf │  │ 🖼️ 架构图.png │   [+ 添加文件]   │
│  │   解析完成 ✓ │  │   就绪 ✓     │                   │
│  └────────────┘  └──────────────┘                   │
│                                                      │
│  ┌─────────────────────────────────────────────┐    │
│  │ 输入您的问题... (Enter 发送, Shift+Enter 换行) │    │
│  └─────────────────────────────────────────────┘    │
│                                                      │
│  [联网] [技能] [知识库]            [模型选择] [发送]  │
└─────────────────────────────────────────────────────┘
```

### 6.2 ChatBox.vue 增强

```typescript
// 新增的 props 和 events
interface FileAttachment {
  id: string;
  file: File;
  name: string;
  size: number;
  type: string;               // MIME type
  category?: 'multimodal' | 'short_doc' | 'long_doc' | 'parsing';
  status: 'pending' | 'uploading' | 'parsing' | 'ready' | 'error';
  progress: number;            // 0-100
  previewUrl?: string;         // 图片预览
  errorMessage?: string;
}

// ChatBox 新增 emit
emit('send', text: string, attachments: FileAttachment[])
```

### 6.3 API 层扩展

```typescript
// frontend/src/api/agent-v2.ts 新增

export interface ChatFileUploadResult {
  file_id: string;
  file_name: string;
  file_category: 'multimodal' | 'short_doc' | 'long_doc';
  parse_status: 'completed' | 'failed';
  content_length?: number;
  strategy: 'vision' | 'context' | 'indexed';
  message: string;
}

export const ChatFileAPI = {
  async upload(
    threadId: string,
    files: File[],
    onProgress?: (percent: number) => void,
  ): Promise<{ success: boolean; files: ChatFileUploadResult[] }> {
    const formData = new FormData();
    formData.append('thread_id', threadId);
    files.forEach(f => formData.append('files', f));

    const res = await fetch('/api/v2/agent/chat-upload', {
      method: 'POST',
      headers: { Authorization: `Bearer ${token}` },
      body: formData,
    });
    return res.json();
  },

  async listFiles(threadId: string): Promise<ChatFileUploadResult[]> {
    const res = await fetch(`/api/v2/agent/chat-files/${threadId}`);
    return res.json();
  },

  async deleteFile(fileId: string): Promise<void> {
    await fetch(`/api/v2/agent/chat-files/${fileId}`, { method: 'DELETE' });
  },
};
```

### 6.4 useAgentChat 增强

`sendMessage` 方法扩展，支持附件：

```typescript
// 在 queryStream payload 中新增字段
const payload = {
  question,
  thread_id: threadId,
  // 新增
  file_ids: attachments
    .filter(a => a.status === 'ready')
    .map(a => a.id),
  multimodal_file_ids: attachments
    .filter(a => a.category === 'multimodal')
    .map(a => a.id),
};
```

---

## 七、多模态处理详细设计

### 7.1 图片消息格式

当用户上传图片，或短文档中携带可访问图片 URL 且模型支持 vision 时，构建多模态消息：

```python
# 后端构建消息
def build_multimodal_message(
    question: str,
    image_files: list[ChatUploadedFile],
) -> list[dict]:
    """构建多模态消息内容块"""
    content_blocks = []

    # 文本部分
    content_blocks.append({
        "type": "text",
        "text": question,
    })

    # 图片部分：支持直接上传图片、以及文档中提取出的 URL 图片
    for img_file in image_files:
        image_url = img_file["image_url"]
        content_blocks.append({
            "type": "image_url",
            "image_url": {"url": image_url},
        })

    return content_blocks
```

### 7.2 模型能力检测

利用现有 `AgentInfo.available_models` 中的 `supports_vision` 字段：

```typescript
// 前端检测
const currentModelSupportsVision = computed(() => {
  const models = agentInfo.value?.available_models || [];
  const active = models.find(m => m.name === selectedModel.value);
  return active?.supports_vision ?? false;
});
```

---

## 八、MinerU 集成详细设计

### 8.1 当前实现补充

- `ChatFileParser._try_mineru()` 调用 `get_task_data_async(..., include_fields=["md", "content_list", "images"], upload_images=True)`。
- 若 MinerU 返回的 `images` 为远程 URL，系统不再丢弃，而是直接保存在 `ParseResult.image_urls`。
- 若 MinerU 返回的 markdown 中仍包含本地图片路径，上传阶段会用 `replace_local_images_with_urls()` 做替换。
- 这意味着短文档上下文、长文档检索、流式图片引用三条链路都基于同一份 `metadata.image_urls`。

### 8.1 解析流程

复用现有的 `MinerUOCR` 类 (`backend/core/tools/ocr/mineru_ocr.py`)：

```
文件上传 → 存储到 MinIO → 下载到本地临时目录
    → MinerUOCR.process_file_async()
        → 提交任务 (POST /api/v1/tasks/submit)
        → 轮询状态 (GET /api/v1/tasks/{id})
        → 获取结果 (GET /api/v1/tasks/{id}/data)
            → 返回 Markdown 内容
    → 清理临时文件
```

### 8.2 解析降级策略

```python
async def parse_rich_document(file_path: str, ext: str) -> str | None:
    """富文档解析 - 三级降级策略"""

    # 第1级：Python 库快速解析
    content = await try_python_parse(file_path, ext)
    if content and is_valid_content(content):
        logger.info(f"Python 解析成功: {len(content)} 字")
        return content

    # 第2级：MinerU pipeline 模式
    content = await try_mineru_parse(file_path, backend="pipeline")
    if content and is_valid_content(content):
        logger.info(f"MinerU pipeline 解析成功: {len(content)} 字")
        return content

    # 第3级：MinerU VLM 模式（GPU 加速，精度更高）
    content = await try_mineru_parse(file_path, backend="vlm-vllm-engine")
    if content and is_valid_content(content):
        logger.info(f"MinerU VLM 解析成功: {len(content)} 字")
        return content

    return None

def is_valid_content(content: str) -> bool:
    """判断解析内容是否有效"""
    text = content.strip()
    if len(text) < 20:
        return False
    # 计算乱码比例
    garbage_ratio = sum(1 for c in text if ord(c) > 0xFFFF) / max(len(text), 1)
    return garbage_ratio < 0.1
```

---

## 九、轻量混合检索详细设计

### 9.1 设计理念

对话级文件检索与知识库级检索的本质区别：

| 维度 | 知识库检索 | 对话文件检索（本设计） |
|------|-----------|---------------------|
| 文档规模 | 数千~数万文档 | 1~20 个文件 |
| 块数量 | 数十万块 | 数十~数百块 |
| 查询频率 | 高频并发 | 单会话串行 |
| 生命周期 | 持久化 | 会话级临时 |
| 精度要求 | 高（语义理解） | 中（关键词+浅层语义即可） |

**结论**：不需要 Embedding API / Milvus / Redis，纯 Python 内存方案足矣。

### 9.2 两路检索 + 融合策略

```
查询 → ┬── BM25 关键词召回（过采样 3x）
       │     复用项目已有的 BM25Index
       │     中文滑动窗口分词，精确术语匹配
       │
       └── TF-IDF 余弦相似度精排
             对 BM25 候选做余弦重排
             纯 Python dict 运算，无 numpy 向量

       → 加权融合：0.6 × BM25_norm + 0.4 × TF-IDF_cosine
       → 上下文窗口扩展（前后各1块）
       → 返回 top_k 结果
```

| 组件 | 作用 | 复杂度 | 依赖 |
|------|------|--------|------|
| **BM25**（第1路） | 精确关键词召回，找到包含查询词的块 | O(query_tokens × doc_freq) | 已有 `BM25Index` |
| **TF-IDF 余弦**（第2路） | 轻量语义重排，基于词频分布相似度精选 | O(候选数 × 公共词数) | 纯 Python |
| **上下文窗口** | 命中块前后扩展1块，保证上下文连贯 | O(1) | 无 |

### 9.3 为什么选 BM25 + TF-IDF 而不是其他组合？

**BM25 的优势**：
- 已有成熟实现（`backend/rag/keywords/bm25_index.py`），支持中文滑动窗口分词
- 对核电子学领域术语（如 "能谱仪"、"前置放大器"、"ADC"）精确匹配效果好
- 增量添加文档，无需全量重建

**TF-IDF 余弦的优势**（相比 Embedding）：
- 零延迟：不需要调用外部 API
- 零依赖：不需要 Milvus/FAISS/numpy 大量内存
- 够用：对于 BM25 已经召回的候选集，做精排已经足够
- 能捕捉 BM25 遗漏的"词频分布相似但非精确匹配"的情况

**上下文窗口扩展的价值**：
- 对话文件检索的用户意图通常是"找到某段内容"，单个 800 字块可能截断上下文
- 自动携带前后块，避免 Agent 需要多次调用工具拼凑上下文

### 9.4 分块策略

- **块大小**：800 字（在精度和上下文窗口大小之间取平衡）
- **重叠**：100 字（确保跨块边界的术语不丢失）
- **切分规则**：优先按段落边界切，段落超长则按句子边界切
- 具体实现见 5.3 节的 `_split_text` 函数

### 9.5 索引生命周期

```
用户上传长文档
  │
  ├── 同步构建索引（<100ms）
  │     ├── 文本分块
  │     ├── BM25 索引构建（增量 add_document）
  │     └── TF-IDF 全局 IDF + 每块向量计算
  │
  ├── Agent 调用 chat_file_search → 同步检索（<10ms）
  │
  ├── 用户继续上传 → 增量更新索引 + 重算 IDF
  │
  └── 会话切换/超时 → LRU 驱逐释放内存
```

### 9.6 内存管理 - LRU 索引池

```python
# backend/services/chat_file_index.py 中

from collections import OrderedDict

class ChatFileIndexManager:
    """全局索引管理器 - LRU 缓存

    控制同时在内存中的索引数量，避免 OOM。
    典型场景：100 个活跃会话 × 每会话平均 50 块 × 800字/块 ≈ 4MB，极轻量。
    """

    def __init__(self, max_threads: int = 100):
        self._indices: OrderedDict[str, ChatFileIndex] = OrderedDict()
        self._max_threads = max_threads

    def get_or_create(self, thread_id: str) -> ChatFileIndex:
        if thread_id in self._indices:
            self._indices.move_to_end(thread_id)
            return self._indices[thread_id]

        if len(self._indices) >= self._max_threads:
            evicted_id, _ = self._indices.popitem(last=False)
            logger.info(f"[ChatFileIndex] LRU 驱逐: {evicted_id}")

        index = ChatFileIndex(thread_id)
        self._indices[thread_id] = index
        return index

    def get(self, thread_id: str) -> ChatFileIndex | None:
        if thread_id in self._indices:
            self._indices.move_to_end(thread_id)
            return self._indices[thread_id]
        return None

    def remove(self, thread_id: str):
        self._indices.pop(thread_id, None)
```

### 9.7 性能预估

| 操作 | 文档大小 | 耗时 | 说明 |
|------|---------|------|------|
| 索引构建 | 5万字 / ~60块 | <50ms | 分块+分词+BM25+TF-IDF |
| 索引构建 | 50万字 / ~600块 | <300ms | 接近单文档上限 |
| 单次检索 | 60块索引 | <5ms | BM25 召回 + TF-IDF 重排 |
| 单次检索 | 600块索引 | <20ms | 候选集更大但仍然很快 |
| 内存占用 | 60块 | ~200KB | 文本+词频字典+BM25 结构 |
| 内存占用 | 600块 | ~2MB | 线性增长，完全可控 |

---

## 十、安全与限制

| 限制项 | 值 | 说明 |
|-------|---|------|
| 单文件最大 | 50MB | 避免服务器内存压力 |
| 每次上传最多 | 10 个文件 | 防止滥用 |
| 每会话最多 | 20 个文件 | 索引内存限制 |
| 单文档最大文本 | 500,000 字 | 过长文档截断 |
| 分块大小 | 800 字 | 检索粒度 |
| MinerU 超时 | 300s | 复杂文档解析超时 |
| 允许的文件类型 | 图片 + 文档 | 白名单机制 |

---

## 十一、实施计划

### 第一阶段：基础上传与短文档（3天）

1. 数据库表创建
2. `ChatFileParser` 文本解析服务
3. `POST /api/v2/agent/chat-upload` 接口
4. 前端 `FileUploadButton` + `FilePreviewChip` 组件
5. 短文档上下文注入
6. ChatBox 集成

### 第二阶段：多模态支持（2天）

1. 图片上传与预览
2. 多模态消息构建
3. 模型 vision 能力检测与提示
4. 粘贴图片支持

### 第三阶段：长文档索引与检索（2天）

1. `ChatFileIndex` 轻量检索模块（BM25 + TF-IDF，纯 Python）
2. `ChatFileIndexManager` LRU 索引池
3. `chat_file_search` Agent 工具
4. `ToolAssemblyPipeline` 动态注入

### 第四阶段：MinerU 集成与降级（2天）

1. MinerU 解析降级链路
2. 解析状态实时反馈（SSE）
3. 异常处理与用户提示

### 第五阶段：优化与测试（2天）

1. 性能优化（并发上传、异步解析）
2. 边界情况处理
3. 端到端测试
4. 文档更新

---

## 十二、关键文件变更清单

| 文件 | 操作 | 说明 |
|------|------|------|
| `backend/db/models/__init__.py` | 修改 | 新增 `ChatUploadedFile`、`ChatFileChunk` 模型 |
| `backend/services/chat_file_parser.py` | 新建 | 文档解析服务 |
| `backend/services/chat_file_index.py` | 新建 | 会话级轻量检索索引（BM25 + TF-IDF） |
| `backend/api/routers/chat_files.py` | 新建 | 对话文件上传 API 路由 |
| `backend/application/tools/providers/chat_file_search_tool.py` | 新建 | Agent 文档检索工具 |
| `backend/application/tools/tool_assembly_pipeline.py` | 修改 | 按请求上下文注入 `chat_file_search` 工具 |
| `frontend_csh_ai/src/api/agent-v2.ts` | 修改 | 新增 `ChatFileAPI` |
| `frontend_csh_ai/src/views/ne-full/components/ChatBox.vue` | 修改 | 集成文件上传按钮 |
| `frontend_csh_ai/src/views/ne-full/components/FileUploadButton.vue` | 新建 | 文件上传组件 |
| `frontend_csh_ai/src/views/ne-full/components/FilePreviewChip.vue` | 新建 | 文件预览气泡 |
| `frontend_csh_ai/src/views/ne-full/composables/useAgentChat.ts` | 修改 | 支持附件发送 |
| `frontend_csh_ai/src/views/ne-full/composables/useFileUpload.ts` | 新建 | 文件上传逻辑 |
