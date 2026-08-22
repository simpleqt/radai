# 对话文件上传与智能处理系统设计文档

> **版本**: v1.0
> **日期**: 2026-05-11
> **状态**: ✅ 核心链路已实现（2026-07-09）
> **架构基线**：本文中的设计示例已按当前平台执行链路校正。工具实现位于 `backend/application/tools/providers/`，统一由 `ToolAssemblyPipeline` 按 profile、workflow 和请求上下文装配；不得再向 `backend/custom/agents` 增加依赖。
> 已实现：`/api/v2/chat-files` 上传、`/api/v1/my-files` 文件库、`useFileUpload`、`FilePreviewChip`、Agent 侧文件元数据、`ChatFileIndex` + `chat_file_search`。
> 已实现补充：PDF/DOCX/PPTX 图片提取、文档图片上传到 MinIO、短文档图片 `image_url` 多模态注入、长文档检索图片 token 注册、MinerU `upload_images=true` 远程图片 URL 保留。
> 当前边界：长文档热索引仍是进程内 LRU，但 `ChatFileIndexManager` 可在 miss 时从 `UserFile + SessionFile` 持久化元数据重建索引；MinerU OCR 仍依赖 `settings.mineru_api_url`。
> **实现说明**：实际上传走 `/api/v2/chat-files/upload`，不是历史设计稿中的 `/api/v2/agent/chat-upload` 或单路由 `/chat-upload`。

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
| 长文档 BM25 会话索引 | ✅ | `ChatFileIndex` + `strategy=indexed` + `chat_file_search`；索引 key 为 `(user_id, thread_id)` |
| MinerU 复杂 PDF 回退 | ⚠️ | `chat_file_parser.py` 支持 MinerU OCR 回退（依赖网关 URL 配置） |
| 索引跨进程恢复 | ✅ | 热索引进程内；miss 时从 `UserFile + SessionFile` 重建 long_doc 索引 |
| 单路由 `/agent/chat-upload` | 不采用 | 当前拆分为 `chat-files`、`my-files`、`files` 三类路由 |

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
                         │ POST /api/v2/chat-files/upload
                         │ (multipart/form-data)
                         ▼
┌─────────────────────────────────────────────────────────┐
│                     Backend (FastAPI)                     │
│                                                          │
│  ┌──────────────────────────────────────────────┐       │
│  │         ChatFileRouter                         │       │
│  │   POST /api/v2/chat-files/upload              │       │
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

# 可直接用 Python/文本路径解析的文档
PYTHON_PARSEABLE_EXTENSIONS = {
    ".txt", ".md", ".csv", ".json", ".py", ".html", ".xml", ".log",
    ".yaml", ".yml", ".toml", ".ini", ".cfg", ".rst", ".kml",
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

# Python 解析失败时优先回退 MinerU 的文件类型
MINERU_FALLBACK_EXTENSIONS = {".pdf", ".doc", ".ppt"}
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
        ├── 原始文件上传用户 MinIO bucket
        ├── 提取到的图片上传用户 MinIO bucket
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
   - 原始文件始终上传到用户 MinIO bucket。
   - 解析器提取出的图片会单独上传到 `users/{user_id}/threads/{thread_id}/chat-files/{file_id}/images/{img.name}`。
   - 文档 markdown / HTML 中的本地图片引用会在入库前替换为可访问 URL。
   - 最终写入 `metadata.image_urls`，供 Agent 和检索工具复用。
3. 标准 Agent Runtime 附件预处理链路
   - 短文档文本直接拼入 `<uploaded_document>`。
   - 同时从 `metadata.image_urls` 中读取图片，按上限注入 `multimodal_images`。
4. `backend/application/tools/providers/chat_file_search_tool.py`
   - 长文档检索时注册图片 URL 为 `[IMAGE:xxx]` token。
   - 流式输出阶段再由 `StreamingTokenResolver` 还原为真实 URL。

---

## 四、数据库与持久化设计

当前实现没有新增 `chat_uploaded_files` 或 `chat_file_chunks` 表，也没有 Redis BM25 索引 key。对话附件复用用户文件中心模型：

### 4.1 `user_files`

一条上传文件对应一行 `UserFile`，核心字段：

| 字段 | 用途 |
|---|---|
| `file_id` | `uf_...` 文件 ID |
| `owner_user_id` | 文件所有者，用于隔离 |
| `thread_id` | 对话附件所属外部 thread；个人文件可为空 |
| `source_type` | `minio` 或 `local` |
| `storage_bucket` / `storage_object` | 用户 MinIO bucket 与对象路径 |
| `local_abs_path` | 生成工件登记到本地时使用 |
| `display_name`、`mime_type`、`size_bytes` | 展示与下载元数据 |
| `status` | `active` / `deleted` |
| `metadata_` | 解析结果、策略、文档内容、图片 URL、表格分析元数据 |

对话附件的 `metadata_` 主要包含：

```json
{
  "file_category": "multimodal | short_doc | long_doc",
  "strategy": "vision | context | indexed",
  "content": "解析后文本，long_doc 也会保留用于索引重建",
  "content_length": 12345,
  "parse_mode": "auto | mineru",
  "parse_method": "none | direct | python | mineru",
  "image_urls": [
    {
      "url": "/api/v2/chat-files/{file_id}/images/{name}?exp=...&sig=...",
      "name": "image_0.png",
      "caption": "文档图片 1",
      "content_type": "image/png",
      "storage_object": "users/{uid}/threads/{tid}/chat-files/{file_id}/images/image_0.png"
    }
  ]
}
```

### 4.2 `session_files`

`SessionFile` 表保存会话与文件的绑定关系：

| 字段 | 用途 |
|---|---|
| `session_id` | 外部 thread_id |
| `user_id` | 当前用户 ID |
| `file_id` | 绑定的 `UserFile.file_id` |
| `attached_from` | `upload` 或 `my_files` |

列表、删除和索引重建均通过 `SessionFile.user_id + session_id` 限定当前用户当前会话。

### 4.3 索引持久化语义

`ChatFileIndex` 本身仍是进程内 LRU 热索引；不写 chunk 表，不依赖 Redis/Milvus/embedding。为了支持进程重启或 LRU 驱逐后的恢复，`ChatFileIndexManager.get(thread_id, user_id=...)` 会在 miss 时：

1. 查询 `UserFile` join `SessionFile`。
2. 限定 `SessionFile.user_id == user_id`、`UserFile.owner_user_id == user_id`、`UserFile.status == "active"`。
3. 只加载 `metadata_.file_category == "long_doc"` 且 `metadata_.content` 非空的文件。
4. 重新分块并构建 BM25/TF-IDF 索引。

---

## 五、后端设计

### 5.1 新增 API 端点

#### `POST /api/v2/chat-files/upload`

对话文件上传接口，支持同时上传多个文件。

**请求 (multipart/form-data)**:

| 字段 | 类型 | 必填 | 说明 |
|------|------|------|------|
| `files` | File[] | 是 | 上传的文件列表 |
| `thread_id` | string | 是 | 会话线程 ID |
| `parse_mode` | string | 否 | `auto` 或 `mineru`，默认 `auto` |

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
      "parse_status": "completed",
      "strategy": "vision",
      "message": "图片将直接发送给模型分析"
    }
  ],
  "thread_id": "thread-id"
}
```

#### `GET /api/v2/chat-files/{thread_id}`

获取会话中已上传的文件列表。

#### `DELETE /api/v2/chat-files/{file_id}`

删除会话中的某个上传文件绑定。当前实现删除 `session_files` 绑定关系，不物理删除 `user_files` 或 MinIO 原始对象；如果文件是 long_doc，会从当前用户当前会话的内存索引中移除。

#### `GET /api/v2/chat-files/{file_id}/images/{image_name}`

返回对话文档图片。URL 必须携带 `exp` 与 `sig`，签名覆盖 `file_id`、`image_name`、`owner_user_id`、`thread_id` 和过期时间。

### 5.1b 相关文件中心端点

| 路径 | 用途 |
|---|---|
| `POST /api/v1/files/upload` | 上传到个人文件夹，走 `storage_scope="personal"` |
| `GET /api/v1/my-files` | 列当前用户个人文件 |
| `POST /api/v1/sessions/{session_id}/files/attach` | 将个人文件绑定到会话；long_doc 会建立会话索引 |
| `GET /api/v2/files/{file_id}/download` | 下载文件，校验 owner 或显式授权 |

### 5.2 文档解析服务：`ChatFileParser`

```python
# backend/services/chat_file_parser.py

class ChatFileParser:
    """对话文件解析服务 - 当前入口接收 bytes，不要求调用方先落本地文件。"""

    async def parse(
        self,
        file_bytes: bytes,
        filename: str,
        mime_type: str | None = None,
        parse_mode: str = "auto",
    ) -> ParseResult:
        ext = Path(filename).suffix.lower()
        effective_parse_mode = str(parse_mode or "auto").strip().lower()

        # 图片不解析文本，直接标记为 multimodal。
        if ext in MULTIMODAL_EXTENSIONS:
            return ParseResult(
                category="multimodal",
                method="none",
                content=None,
                mime_type=mime_type or _guess_mime(ext),
            )

        # 纯文本 / KML / JSON / CSV 等直接解析。
        if ext in PYTHON_PARSEABLE_EXTENSIONS:
            content = await self._parse_text_file(file_bytes, ext, filename=filename)
            if content is not None:
                return self._route_by_length(content, method="direct")
            return ParseResult(category="failed", method="direct", error="文本文件读取失败")

        # 富文档可按 parse_mode 强制 MinerU；否则先 Python 快速解析，再回退 MinerU。
        if ext in RICH_DOCUMENT_EXTENSIONS:
            if effective_parse_mode == "mineru":
                return await self._try_mineru(file_bytes, filename)
            return await self._parse_rich_document(file_bytes, filename, ext)

        # 未知格式最后尝试 MinerU。
        return await self._try_mineru(file_bytes, filename)

    def _route_by_length(self, content: str, method: str, images=None) -> ParseResult:
        cleaned = clean_surrogates(clean_document_images(content))
        truncated = cleaned[:MAX_CONTENT_CHARS]
        category = "short_doc" if len(truncated) <= SHORT_DOC_THRESHOLD else "long_doc"
        return ParseResult(
            category=category,
            method=method,
            content=truncated,
            content_length=len(truncated),
            images=images or [],
        )
```

### 5.3 会话级文档索引：`ChatFileIndex`（轻量方案）

当前实现位于 `backend/services/chat_file_index.py`。索引 key 是 `(user_id, thread_id)`，避免不同用户使用相同外部 thread_id 时共享检索上下文。

> **实现原则**：对话级文件检索不需要知识库级别的向量基础设施。当前采用模式匹配、关键词精确匹配、BM25 召回、TF-IDF 余弦轻量重排和文档开头回退的纯 Python 方案；不调用 embedding API，不需要 Milvus/Redis 存储索引。

#### 为什么不用 Embedding 向量检索？

| 维度 | Embedding 方案 | 当前轻量方案 |
|------|---------------|-------------------|
| 索引构建 | 每块调 API 算向量，数百块≈10-30s | 纯内存分词统计，<100ms |
| 查询延迟 | 调 API 算 query 向量 + numpy 距离计算 | 纯 Python 字符串运算，<10ms |
| 外部依赖 | embedding API 服务必须在线 | 无，纯 CPU |
| 内存占用 | 每块 1536*4B ≈ 6KB 向量，额外 numpy | 仅存文本和词频字典 |
| 适用场景 | 知识库级万级文档 | 对话级几十到几百块 |

#### 当前检索策略

```
查询 query
  │
  ├── prepare_search_inputs()
  │     提取关键词、短语、图/表相关词，并为总结类问题补充摘要检索词
  │
  ├── 模式匹配
  │     图片、图表、Figure、Table 等问题优先走正则匹配
  │
  ├── keywords 精确匹配
  │     大小写不敏感子串匹配，支持模型传入中英文术语
  │
  ├── BM25 + TF-IDF
  │     使用已有 BM25Index 过采样候选，再用稀疏 TF-IDF 余弦重排
  │
  ├── 上下文窗口
  │     命中块自动携带相邻块，提供连贯上下文
  │
  └── 回退
        无命中或通用总结类查询返回各文档开头片段
```

### 5.4 Agent 工具注册：`chat_file_search`

工具提供方位于 `backend/application/tools/providers/chat_file_search_tool.py`：

- `create_scoped_chat_file_search_tool(thread_id, *, user_id)`：创建绑定到当前用户和当前会话的 LangChain tool。
- `has_thread_file_index(thread_id, *, user_id)`：通过 `get_index_manager().has_files()` 判断该用户该会话是否存在 long_doc 索引。
- tool 入参为 `query`、可选 `keywords` 和 `top_k`；`top_k` 被限制在 1 到 10。
- 检索结果会清理本地图片引用，并通过 `image_url_registry` 将文档图片注册为 `[IMAGE:...]` token，流式输出阶段再还原为真实 URL。

### 5.5 工具装配集成

`backend/application/tools/tool_assembly_pipeline.py` 会在装配工具时检查：

- `chat_file_search` 是否在当前 agent/profile/workflow 允许工具集里。
- 请求上下文是否包含 `thread_id` 与 `user_id`。
- `has_thread_file_index(thread_id, user_id=user_id)` 是否为真。

三者都满足时，才注入当前用户当前会话专属的 `chat_file_search` 工具。

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

### 6.1 当前组件

当前前端落点在 `frontend/src/views/ne-full/`：

| 文件 | 作用 |
|---|---|
| `components/ChatBox.vue` | 输入框、附件菜单、粘贴图片、视觉能力提示和发送事件 |
| `components/FilePreviewChip.vue` | 已选择/已上传附件预览、错误状态与删除入口 |
| `components/MyFilesPickerDialog.vue` | 从个人文件夹选择已有文件并绑定到当前会话 |
| `composables/useFileUpload.ts` | 本地附件状态、`parse_mode`、上传、删除、清空、ready 文件 ID |
| `api/agent-v2.ts` | `ChatFileAPI.upload/listFiles/deleteFile` |

`ChatBox.vue` 的附件入口支持本地选择、粘贴图片、从“我的文件”选择和多文件上传；`FilePreviewChip.vue` 显示在输入框上方。

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

### 6.2 附件状态模型

```typescript
// frontend/src/views/ne-full/composables/useFileUpload.ts
interface FileAttachment {
  id: string;
  file: File;
  name: string;
  size: number;
  mimeType: string;
  category: 'multimodal' | 'short_doc' | 'long_doc' | 'failed' | 'pending';
  status: 'pending' | 'uploading' | 'ready' | 'error';
  strategy: 'vision' | 'context' | 'indexed' | 'none';
  progress: number;
  previewUrl?: string;
  errorMessage?: string;
  contentLength?: number;
}
```

### 6.3 API 层

```typescript
// frontend/src/api/agent-v2.ts

export interface ChatFileUploadResult {
  file_id: string;
  file_name: string;
  file_category: 'multimodal' | 'short_doc' | 'long_doc';
  parse_status: 'completed' | 'failed';
  content_length: number;
  strategy: 'vision' | 'context' | 'indexed' | 'none';
  message: string;
  error?: string;
  mime_type?: string;
}

export type ChatFileParseMode = 'auto' | 'mineru';

export const ChatFileAPI = {
  async upload(
    threadId: string,
    files: File[],
    parseMode: ChatFileParseMode = 'auto',
    token?: string,
  ): Promise<{ success: boolean; files: ChatFileUploadResult[]; thread_id: string }> {
    const formData = new FormData();
    formData.append('thread_id', threadId);
    formData.append('parse_mode', parseMode);
    files.forEach(f => formData.append('files', f));

    const headers: Record<string, string> = {};
    if (token) headers.Authorization = `Bearer ${token}`;

    const res = await fetch('/api/v2/chat-files/upload', {
      method: 'POST',
      headers,
      body: formData,
    });
    return res.json();
  },

  async listFiles(threadId: string, token?: string): Promise<ChatFileUploadResult[]> {
    const headers: Record<string, string> = {};
    if (token) headers.Authorization = `Bearer ${token}`;
    const res = await fetch(`/api/v2/chat-files/${encodeURIComponent(threadId)}`, { headers });
    const data = await res.json();
    return data.items || [];
  },

  async deleteFile(fileId: string, token?: string): Promise<void> {
    const headers: Record<string, string> = {};
    if (token) headers.Authorization = `Bearer ${token}`;
    await fetch(`/api/v2/chat-files/${encodeURIComponent(fileId)}`, { method: 'DELETE', headers });
  },
};
```

### 6.4 Agent 请求

发送消息前，`useFileUpload.uploadPendingFiles(threadId)` 会把 pending 附件上传解析完毕；发送给 Agent Runtime 的 payload 携带 ready 附件 ID：

```typescript
const payload = {
  question,
  thread_id: threadId,
  metadata: {
    file_ids: attachments
      .filter(a => a.status === 'ready')
      .map(a => a.id),
  },
};
```

---

## 七、多模态处理详细设计

### 7.1 图片消息格式

当用户上传图片，或短文档中携带可访问图片 URL 且模型支持 vision 时，标准 Agent Runtime 会从附件元数据读取图片 URL，并注入 `multimodal_images`。当前系统不引入独立的对话上传文件模型；图片来源仍是 `UserFile.metadata_.image_urls` 和对话附件绑定。

短文档链路会同时处理两类内容：

- 文本：以 `<uploaded_document>` 形式拼入本轮问题上下文。
- 图片：从 `metadata.image_urls` 取可访问 URL，按上限传给支持 vision 的模型。

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

### 8.2 解析流程

复用现有的 `MinerUOCR` 类 (`backend/core/tools/ocr/mineru_ocr.py`)：

```
上传 bytes
  → ChatFileParser._try_mineru()
  → 写入 NamedTemporaryFile
  → MinerUOCR.submit_task_async(tmp_path)
  → wait_for_completion_async(task_id, timeout=300)
  → get_task_data_async(task_id, include_fields=["md", "content_list", "images"], upload_images=True)
  → 提取 markdown、base64 图片、远程图片 URL
  → 清理临时文件
```

### 8.3 解析降级策略

```python
async def _parse_rich_document(data: bytes, filename: str, ext: str) -> ParseResult:
    content = await asyncio.to_thread(self._python_parse_sync, data, ext)
    if content and _is_valid_content(content):
        images = await asyncio.to_thread(self._extract_document_images, data, ext)
        return self._route_by_length(content, method="python", images=images)

    if ext in MINERU_FALLBACK_EXTENSIONS or ext in {".docx", ".pptx", ".xlsx"}:
        result = await self._try_mineru(data, filename)
        if result.category != "failed":
            return result

    return ParseResult(category="failed", method="python", error="无法解析文档内容")

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
| 生命周期 | 持久化 | 元数据持久化；热索引进程内 LRU，可按 `(user_id, thread_id)` 重建 |
| 精度要求 | 高（语义理解） | 中（关键词+浅层语义即可） |

**结论**：不需要 Embedding API / Milvus / Redis。当前实现用纯 Python 热索引提供低延迟检索，并把 long_doc 文本保存在 `UserFile.metadata_.content` 中用于重建。

### 9.2 当前检索策略

```
用户问题 + 可选 keywords
  │
  ├── prepare_search_inputs()
  │     提取中英文术语、图/表编号、引号精确短语；
  │     通用总结问题改写为 abstract/introduction/... 等摘要检索词
  │
  ├── 模式匹配
  │     图片/图表/Figure/Table 等问题优先正则匹配结构化线索
  │
  ├── 关键词精确匹配
  │     keywords 大小写不敏感子串匹配
  │
  ├── BM25 + TF-IDF 融合
  │     BM25 过采样候选，TF-IDF 余弦重排
  │
  └── 顺序回退
        无有效命中或通用总结类问题返回文档开头片段
```

| 组件 | 作用 | 复杂度 | 依赖 |
|------|------|--------|------|
| **模式匹配** | 针对图片、图表、表格、Figure/Table 编号 | O(chunks × patterns) | 正则 |
| **关键词匹配** | 精确术语命中，支持中英文混合 keywords | O(chunks × keywords) | 纯 Python |
| **BM25**（第1路） | 精确关键词召回，找到包含查询词的块 | O(query_tokens × doc_freq) | 已有 `BM25Index` |
| **TF-IDF 余弦**（第2路） | 轻量语义重排，基于词频分布相似度精选 | O(候选数 × 公共词数) | 纯 Python |
| **上下文窗口** | 命中块前后扩展1块，保证上下文连贯 | O(1) | 无 |
| **图片 token** | 检索结果注册文档图片为 `[IMAGE:...]` | O(每文件图片数) | `image_url_registry` |

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
- 具体实现见 `backend/services/chat_file_index.py:split_text()`

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

`ChatFileIndexManager` 控制同时驻留内存的索引数量，默认 `max_threads=100`。当前 key 不是单独的 `thread_id`，而是 `(user_id, thread_id)`：

- `get_or_create(thread_id, user_id=...)`：创建或取回当前用户当前会话索引。
- `get(thread_id, user_id=...)`：未命中时会从 `UserFile + SessionFile` 重建 long_doc 索引。
- `remove_document(file_id)`：删除对话附件绑定时，从当前索引移除该文件块。
- LRU 驱逐只释放热索引；持久化元数据仍在 DB，可按需重建。

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

## 十一、当前实现清单

| 能力 | 当前状态 |
|------|----------|
| 基础上传 | 已实现，`POST /api/v2/chat-files/upload`，单文件 50MB、每次最多 10 个 |
| 文件持久化 | 已实现，复用 `user_files` 与 `session_files`，不新增 `chat_uploaded_files` / `chat_file_chunks` |
| 短文档上下文 | 已实现，文本注入 `<uploaded_document>`，文档图片进入多模态图片列表 |
| 长文档检索 | 已实现，`ChatFileIndex` 热索引 + DB 元数据重建 |
| 文档图片 | 已实现，PDF/DOCX/PPTX 提取图片并上传用户 MinIO bucket |
| MinerU 回退 | 已实现，依赖 `settings.mineru_api_url` 与 MinerU 网关 |
| 前端附件 | 已实现，`ChatBox.vue` + `FilePreviewChip.vue` + `useFileUpload.ts` |
| 个人文件绑定 | 已实现，`/api/v1/my-files` 与 `/api/v1/sessions/{session_id}/files/attach` |

---

## 十二、关键文件变更清单

| 文件 | 当前职责 |
|------|----------|
| `backend/api/routers/chat_files.py` | 对话附件上传、列表、解绑、图片签名读取 |
| `backend/api/routers/my_files.py` | 个人文件上传、列表、绑定到会话、生成文件保存 |
| `backend/api/routers/files.py` | 文件元数据、下载、MinIO 工件下载与 KB 图片代理 |
| `backend/services/user_file_service.py` | 统一解析、存储、UserFile 落库和图片 URL 处理 |
| `backend/services/chat_file_parser.py` | 文本、富文档、图片和 MinerU 解析 |
| `backend/services/chat_file_index.py` | `(user_id, thread_id)` 热索引与 DB 重建 |
| `backend/application/tools/providers/chat_file_search_tool.py` | `chat_file_search` 工具和图片 token 注册 |
| `backend/application/tools/tool_assembly_pipeline.py` | 按 policy、user_id、thread_id 和索引状态注入工具 |
| `frontend/src/api/agent-v2.ts` | `ChatFileAPI` 与 Agent Runtime 请求 |
| `frontend/src/views/ne-full/components/ChatBox.vue` | 附件入口、粘贴图片、发送 |
| `frontend/src/views/ne-full/components/FilePreviewChip.vue` | 附件预览 |
| `frontend/src/views/ne-full/components/MyFilesPickerDialog.vue` | 个人文件选择并绑定 |
| `frontend/src/views/ne-full/composables/useFileUpload.ts` | 附件状态与上传流程 |
