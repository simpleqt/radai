# 任务四：RAG Service 修改

> **文档状态**：📦 任务归档（2026-03 多库改造）。现行检索为 **DirectRetriever + retrieve_context**，见 [docs/知识库算法和架构.md](../docs/知识库算法和架构.md)。

## 任务概述
修改现有的 RAG Service，使其支持多知识库切换和管理，同时保持向后兼容。

## 前置条件
- 任务一（数据库设计）完成
- 任务二（NebulaGraph 存储）完成
- 任务三（知识库管理器）完成

---

## 现有架构分析

### 1. RagService 单例模式

**文件**: [backend/rag/service.py](backend/rag/service.py)

```python
@lru_cache(maxsize=1)
def get_rag_service() -> RagService:
    return RagService()
```

当前 RagService 使用 `@lru_cache` 实现单例模式，全局只有一个实例。

### 2. RagService 核心属性

```python
class RagService:
    def __init__(self):
        self._initialized = False
        self._http: httpx.AsyncClient | None = None
        self._rag: RAGAnything | None = None          # 单一 RAG 实例
        self._rag_work_dir: str | None = None
        self._runtime_settings: Any | None = None
        self._linearrag_http_ready = False
        self._query_llm_model_func: Callable[..., Any] | None = None
        self._query_llm_max_async: int = 50
```

### 3. RagService 核心方法

| 方法 | 功能 | 是否需要修改 |
|-----|------|-------------|
| `initialize()` | 初始化 RAG 实例 | **需要修改** |
| `aquery()` | 查询 | **需要修改** |
| `aquery_rich()` | 富查询（含图片） | **需要修改** |
| `aquery_context_only()` | 仅检索上下文 | **需要修改** |
| `astream_query()` | 流式查询 | **需要修改** |
| `insert_content_list()` | 插入内容列表 | **需要修改** |
| `insert_docx()` | 插入 DOCX | **需要修改** |
| `process_multimodal()` | 处理多模态文档 | **需要修改** |
| `delete_doc_by_id()` | 删除文档 | **需要修改** |
| `clear_failed_docs()` | 清除失败文档 | **需要修改** |
| `reset()` | 重置服务 | **需要修改** |
| `close()` | 关闭服务 | **需要修改** |
| `_build_llm_model_func()` | 构建 LLM 函数 | 保持不变 |
| `_build_embedding_func()` | 构建嵌入函数 | 保持不变 |

### 4. API 路由分析

**文件**: [backend/api/routers/rag.py](backend/api/routers/rag.py)

现有 API 端点：

| 端点 | 方法 | 请求模型 | 需要修改 |
|-----|------|---------|---------|
| `/query` | POST | `RagQueryRequest` | 添加 kb_id 参数 |
| `/insert` | POST | `RagInsertRequest` | 添加 kb_id 参数 |
| `/insert_docx` | POST | `RagInsertDocxRequest` | 添加 kb_id 参数 |
| `/process_multimodal` | POST | `RagProcessMultimodalRequest` | 添加 kb_id 参数 |
| `/delete_doc` | POST | `RagDeleteDocRequest` | 添加 kb_id 参数 |
| `/reset` | POST | `RagResetRequest` | 添加 kb_id 参数 |
| `/health` | GET | - | 保持不变 |
| `/models` | GET | - | 保持不变 |

---

## 任务详情

### 1. 修改 RagService 类结构

**文件**: `backend/rag/service.py`

#### 1.1 添加新属性

```python
class RagService:
    def __init__(self):
        self._initialized = False
        self._http: httpx.AsyncClient | None = None
        self._rag: RAGAnything | None = None
        self._rag_work_dir: str | None = None
        self._runtime_settings: Any | None = None
        self._linearrag_http_ready = False
        self._query_llm_model_func: Callable[..., Any] | None = None
        self._query_llm_max_async: int = 50
        
        # 新增：多知识库支持
        self._kb_manager: KnowledgeBaseManager | None = None
        self._default_kb_id: str | None = None
        self._default_kb_code: str = "default"
        self._session_factory: Any | None = None
```

#### 1.2 添加初始化方法

```python
async def initialize(self) -> None:
    """初始化服务（向后兼容，使用默认知识库）"""
    if self._initialized:
        return
    
    await self._initialize_kb_manager()
    
    async with self._get_session() as session:
        await self._ensure_default_kb(session)
        if self._default_kb_id:
            self._rag = await self._kb_manager.get_rag_instance(
                session, self._default_kb_id
            )
    
    self._initialized = True
    logger.info(f"RAG service initialized with default KB: {self._default_kb_id}")

async def _initialize_kb_manager(self) -> None:
    """初始化知识库管理器"""
    from backend.rag.knowledge_base import get_kb_manager
    
    self._kb_manager = get_kb_manager()
    
    from backend.db.connection import get_async_session_factory
    self._session_factory = get_async_session_factory()

@contextlib.asynccontextmanager
async def _get_session(self):
    """获取数据库会话"""
    if self._session_factory is None:
        raise RuntimeError("Session factory not initialized")
    async with self._session_factory() as session:
        try:
            yield session
            await session.commit()
        except Exception:
            await session.rollback()
            raise

async def _ensure_default_kb(self, session: AsyncSession) -> str:
    """确保默认知识库存在"""
    if self._default_kb_id:
        return self._default_kb_id
    
    kb = await self._kb_manager.get_knowledge_base_by_code(
        session, self._default_kb_code
    )
    
    if not kb:
        kb = await self._kb_manager.create_knowledge_base(
            session=session,
            kb_code=self._default_kb_code,
            kb_name="默认知识库",
            description="系统默认知识库",
            is_default=True,
        )
    
    self._default_kb_id = kb.kb_id
    return self._default_kb_id
```

### 2. 修改查询方法

#### 2.1 修改 aquery 方法

```python
async def aquery(
    self, 
    query: str, 
    mode: str = "mix",
    kb_id: str | None = None,
    **kwargs: Any
) -> str:
    """
    执行查询
    
    Args:
        query: 查询文本
        mode: 查询模式
        kb_id: 知识库 ID（可选，默认使用默认知识库）
    """
    rag = await self._get_rag_instance(kb_id)
    
    from lightrag import QueryParam
    
    query_param_kwargs = {
        k: v for k, v in kwargs.items() 
        if k in QueryParam.__dataclass_fields__
    }
    query_param = QueryParam(mode=mode, **query_param_kwargs)
    
    if self._query_llm_model_func is not None:
        query_param.model_func = self._query_llm_model_func
    
    return await rag.aquery(query, param=query_param)

async def _get_rag_instance(self, kb_id: str | None = None) -> RAGAnything:
    """
    获取 RAG 实例
    
    Args:
        kb_id: 知识库 ID，为空则使用默认知识库
        
    Returns:
        RAGAnything 实例
    """
    if kb_id is None:
        await self.initialize()
        if self._rag is not None:
            return self._rag
        kb_id = self._default_kb_id
    
    async with self._get_session() as session:
        return await self._kb_manager.get_rag_instance(session, kb_id)
```

#### 2.2 修改 aquery_rich 方法

```python
async def aquery_rich(
    self, 
    query: str, 
    mode: str = "mix",
    kb_id: str | None = None,
    **kwargs: Any
) -> Dict[str, Any]:
    """
    执行查询并返回丰富的结果
    
    Args:
        query: 查询文本
        mode: 查询模式
        kb_id: 知识库 ID（可选）
    """
    rag = await self._get_rag_instance(kb_id)
    
    from lightrag import QueryParam
    
    query_param_kwargs = {
        k: v for k, v in kwargs.items() 
        if k in QueryParam.__dataclass_fields__
    }
    query_param = QueryParam(mode=mode, **query_param_kwargs)
    
    if self._query_llm_model_func is not None:
        query_param.model_func = self._query_llm_model_func
    
    answer = await rag.aquery(query, param=query_param)
    images = await self._get_images_for_query(query, mode=mode, rag=rag)

    return {"answer": answer, "images": images}
```

### 3. 修改文档入库方法

#### 3.1 修改 insert_content_list 方法

```python
async def insert_content_list(
    self,
    content_list: List[Dict[str, Any]],
    file_path: str = "unknown_document",
    kb_id: str | None = None,
    **kwargs: Any
) -> Dict[str, Any]:
    """
    插入内容列表到指定知识库
    
    Args:
        content_list: 内容列表
        file_path: 文件路径
        kb_id: 知识库 ID（可选）
    """
    rag = await self._get_rag_instance(kb_id)
    result = await rag.insert_content_list(
        content_list=content_list, 
        file_path=file_path, 
        **kwargs
    )
    
    # 记录文档到知识库
    if result.get("success") and result.get("doc_id"):
        await self._record_document(
            kb_id or self._default_kb_id,
            result["doc_id"],
            file_path
        )
    
    return result

async def _record_document(
    self,
    kb_id: str,
    doc_id: str,
    file_path: str
) -> None:
    """记录文档到知识库"""
    try:
        async with self._get_session() as session:
            from pathlib import Path
            file_name = Path(file_path).name if file_path else doc_id
            
            await self._kb_manager.add_document(
                session=session,
                kb_id=kb_id,
                doc_id=doc_id,
                file_name=file_name,
                file_path=file_path,
            )
    except Exception as e:
        logger.warning(f"记录文档失败: {e}")
```

#### 3.2 修改 insert_docx 方法

```python
async def insert_docx(
    self, 
    file_path: str, 
    kb_id: str | None = None,
    **kwargs: Any
) -> Dict[str, Any]:
    """
    插入 DOCX 文档到指定知识库
    
    Args:
        file_path: DOCX 文件路径
        kb_id: 知识库 ID（可选）
    """
    rag = await self._get_rag_instance(kb_id)
    
    resolved = Path(file_path).expanduser()
    if not resolved.exists():
        raise FileNotFoundError(f"DOCX 文件不存在: {file_path}")

    output_dir = kwargs.pop("output_dir", None)
    parse_method = kwargs.pop("parse_method", None)
    display_stats = kwargs.pop("display_stats", None)
    parser = kwargs.pop("parser", None)

    parse_param_keys = {
        "lang", "device", "start_page", "end_page",
        "formula", "table", "backend", "source", "timeout",
    }
    parse_kwargs: dict[str, Any] = {}
    for k in list(kwargs.keys()):
        if k in parse_param_keys:
            parse_kwargs[k] = kwargs.pop(k)

    original_parser = getattr(getattr(rag, "config", None), "parser", None)
    try:
        if parser:
            rag.config.parser = str(parser)
        content_list, doc_id = await rag.parse_document(
            file_path=str(resolved),
            output_dir=output_dir,
            parse_method=parse_method,
            display_stats=display_stats,
            **parse_kwargs,
        )
    finally:
        if original_parser and parser:
            rag.config.parser = original_parser

    result = await rag.insert_content_list(
        content_list=content_list,
        file_path=str(resolved),
        doc_id=doc_id,
        **kwargs,
    )
    
    # 记录文档
    if result.get("success") and doc_id:
        await self._record_document(
            kb_id or self._default_kb_id,
            doc_id,
            str(resolved)
        )
    
    return result
```

#### 3.3 修改 process_multimodal 方法

```python
async def process_multimodal(
    self, 
    file_path: str, 
    kb_id: str | None = None,
    **kwargs: Any
) -> Dict[str, Any]:
    """
    处理多模态文档
    
    Args:
        file_path: 文件路径
        kb_id: 知识库 ID（可选）
    """
    rag = await self._get_rag_instance(kb_id)
    
    resolved = Path(file_path).expanduser()
    if not resolved.exists():
        raise FileNotFoundError(f"文件不存在: {file_path}")

    # ... 其余逻辑保持不变，使用 rag 实例 ...

    result = {
        "success": True,
        "doc_id": doc_id,
        "file_path": str(resolved),
        "multimodal_count": len(multimodal_items),
    }
    
    # 记录文档
    if doc_id:
        await self._record_document(
            kb_id or self._default_kb_id,
            doc_id,
            str(resolved)
        )
    
    return result
```

### 4. 修改删除方法

#### 4.1 修改 delete_doc_by_id 方法

```python
async def delete_doc_by_id(
    self, 
    doc_id: str, 
    delete_llm_cache: bool = False,
    kb_id: str | None = None,
) -> Dict[str, Any]:
    """
    从指定知识库删除文档
    
    Args:
        doc_id: 文档 ID
        delete_llm_cache: 是否删除 LLM 缓存
        kb_id: 知识库 ID（可选）
    """
    rag = await self._get_rag_instance(kb_id)
    result = await rag.lightrag.adelete_by_doc_id(
        doc_id, delete_llm_cache=delete_llm_cache
    )
    
    # 从知识库记录中移除
    if result:
        try:
            async with self._get_session() as session:
                await self._kb_manager.remove_document(
                    session=session,
                    kb_id=kb_id or self._default_kb_id,
                    doc_id=doc_id,
                )
        except Exception as e:
            logger.warning(f"移除文档记录失败: {e}")
    
    payload: Any
    if isinstance(result, dict):
        payload = result
    elif hasattr(result, "model_dump"):
        payload = result.model_dump()
    elif hasattr(result, "__dict__"):
        payload = dict(result.__dict__)
    else:
        payload = str(result)
    return {"success": True, "doc_id": doc_id, "result": payload}
```

#### 4.2 修改 clear_failed_docs 方法

```python
async def clear_failed_docs(
    self, 
    include_processing: bool = True,
    kb_id: str | None = None,
) -> Dict[str, Any]:
    """
    清除失败/处理中文档的状态
    
    Args:
        include_processing: 是否同时清除 PROCESSING 状态
        kb_id: 知识库 ID（可选）
    """
    rag = await self._get_rag_instance(kb_id)
    lightrag = rag.lightrag
    
    if lightrag is None:
        return {"success": False, "error": "LightRAG 未初始化"}

    # ... 其余逻辑保持不变 ...
```

### 5. 修改重置和关闭方法

#### 5.1 修改 reset 方法

```python
async def reset(
    self,
    new_work_dir: str | None = None,
    clear_old_work_dir: bool = True,
    kb_id: str | None = None,
) -> Dict[str, Any]:
    """
    重置指定知识库
    
    Args:
        new_work_dir: 新工作目录
        clear_old_work_dir: 是否清空旧目录
        kb_id: 知识库 ID（可选，为空则重置默认知识库）
    """
    if kb_id is None:
        kb_id = self._default_kb_id
    
    # 清理缓存的 RAG 实例
    if self._kb_manager and kb_id in self._kb_manager._rag_instances:
        instance = self._kb_manager._rag_instances[kb_id]
        try:
            await instance.rag.finalize_storages()
        except Exception as e:
            logger.warning(f"RAG finalize failed: {e}")
        del self._kb_manager._rag_instances[kb_id]
    
    # 如果重置的是默认知识库，重置 _rag 实例
    if kb_id == self._default_kb_id:
        self._rag = None
        self._initialized = False
    
    return {
        "success": True,
        "kb_id": kb_id,
        "message": f"知识库 {kb_id} 已重置",
    }
```

#### 5.2 修改 close 方法

```python
async def close(self) -> None:
    """关闭服务，清理所有资源"""
    # 关闭知识库管理器中的所有 RAG 实例
    if self._kb_manager:
        await self._kb_manager.close()
    
    self._rag = None
    if self._http is not None:
        await self._http.aclose()
    self._initialized = False
```

### 6. 新增跨知识库查询方法

```python
async def query_multiple_kbs(
    self,
    query_text: str,
    kb_ids: List[str],
    mode: str = "mix",
    **kwargs: Any
) -> Dict[str, Dict[str, Any]]:
    """
    跨知识库查询
    
    在多个知识库中执行相同查询，返回各知识库的结果
    
    Args:
        query_text: 查询文本
        kb_ids: 知识库 ID 列表
        mode: 查询模式
        
    Returns:
        Dict[kb_id, query_result]
    """
    results = {}
    
    async def query_one(kb_id: str) -> tuple[str, Dict[str, Any]]:
        try:
            result = await self.aquery_rich(
                query_text, 
                mode=mode, 
                kb_id=kb_id,
                **kwargs
            )
            return kb_id, {"success": True, **result}
        except Exception as e:
            return kb_id, {"success": False, "error": str(e)}
    
    # 并发查询
    tasks = [query_one(kb_id) for kb_id in kb_ids]
    query_results = await asyncio.gather(*tasks, return_exceptions=False)
    
    for kb_id, result in query_results:
        results[kb_id] = result
    
    return results
```

---

## API 路由修改

### 1. 修改请求模型

**文件**: `backend/api/routers/rag.py`

```python
class RagQueryRequest(BaseModel):
    query: str = Field(..., description="用户查询")
    mode: str = Field(default="mix", description="查询模式")
    kb_id: str | None = Field(default=None, description="知识库 ID")
    kwargs: Dict[str, Any] = Field(default_factory=dict, description="额外参数")


class RagInsertRequest(BaseModel):
    content_list: List[Dict[str, Any]] = Field(..., description="OCR content_list")
    file_path: str = Field(default="unknown_document", description="来源文件路径")
    kb_id: str | None = Field(default=None, description="知识库 ID")
    kwargs: Dict[str, Any] = Field(default_factory=dict, description="额外参数")


class RagInsertDocxRequest(BaseModel):
    file_path: str = Field(..., description="DOCX 文件路径")
    kb_id: str | None = Field(default=None, description="知识库 ID")
    kwargs: Dict[str, Any] = Field(default_factory=dict, description="额外参数")


class RagProcessMultimodalRequest(BaseModel):
    file_path: str = Field(..., description="文件路径")
    kb_id: str | None = Field(default=None, description="知识库 ID")
    kwargs: Dict[str, Any] = Field(default_factory=dict, description="额外参数")


class RagDeleteDocRequest(BaseModel):
    doc_id: str = Field(..., description="需要删除的 doc_id")
    delete_llm_cache: bool = Field(default=False, description="是否删除 LLM 缓存")
    kb_id: str | None = Field(default=None, description="知识库 ID")


class RagResetRequest(BaseModel):
    new_work_dir: str | None = Field(default=None, description="新的 RAG_WORK_DIR")
    clear_old_work_dir: bool = Field(default=True, description="是否清空旧工作目录")
    kb_id: str | None = Field(default=None, description="知识库 ID")
```

### 2. 新增跨知识库查询接口

```python
class RagMultiQueryRequest(BaseModel):
    query: str = Field(..., description="用户查询")
    kb_ids: List[str] = Field(..., description="知识库 ID 列表")
    mode: str = Field(default="mix", description="查询模式")
    kwargs: Dict[str, Any] = Field(default_factory=dict, description="额外参数")


class RagMultiQueryResponse(BaseModel):
    results: Dict[str, Dict[str, Any]] = Field(
        default_factory=dict, 
        description="各知识库的查询结果"
    )


@router.post("/query_multiple", response_model=RagMultiQueryResponse)
async def rag_query_multiple(
    request: RagMultiQueryRequest,
    auth: ApiKeyAuth = Depends(verify_api_key),
):
    """跨知识库查询"""
    service = get_rag_service()
    try:
        results = await service.query_multiple_kbs(
            query_text=request.query,
            kb_ids=request.kb_ids,
            mode=request.mode,
            **request.kwargs,
        )
        return RagMultiQueryResponse(results=results)
    except Exception as e:
        logger.exception(f"跨知识库查询失败: {e}")
        raise HTTPException(status_code=500, detail=f"跨知识库查询失败: {str(e)}")
```

### 3. 修改现有端点

```python
@router.post("/query", response_model=RagQueryResponse)
async def rag_query(
    request: RagQueryRequest,
    auth: ApiKeyAuth = Depends(verify_api_key),
):
    service = get_rag_service()
    try:
        kwargs = dict(request.kwargs or {})
        kwargs.setdefault("vlm_enhanced", False)
        result = await service.aquery_rich(
            request.query, 
            mode=request.mode,
            kb_id=request.kb_id,
            **kwargs
        )
        return RagQueryResponse(**result)
    except Exception as e:
        logger.exception(f"RAG 查询失败: {e}")
        raise HTTPException(status_code=500, detail=f"RAG 查询失败: {str(e)}")


@router.post("/insert", response_model=RagInsertResponse)
async def rag_insert(
    request: RagInsertRequest,
    auth: ApiKeyAuth = Depends(verify_api_key),
):
    service = get_rag_service()
    try:
        result = await service.insert_content_list(
            content_list=request.content_list,
            file_path=request.file_path,
            kb_id=request.kb_id,
            **request.kwargs,
        )
        return RagInsertResponse(result=result)
    except Exception as e:
        logger.exception(f"RAG 入库失败: {e}")
        raise HTTPException(status_code=500, detail=f"RAG 入库失败: {str(e)}")
```

---

## 向后兼容性处理

### 1. 默认知识库自动创建

```python
async def _ensure_default_kb(self, session: AsyncSession) -> str:
    """
    确保默认知识库存在
    
    如果不存在，自动创建一个名为 "default" 的知识库
    """
    if self._default_kb_id:
        return self._default_kb_id
    
    kb = await self._kb_manager.get_knowledge_base_by_code(
        session, self._default_kb_code
    )
    
    if not kb:
        logger.info("默认知识库不存在，自动创建...")
        kb = await self._kb_manager.create_knowledge_base(
            session=session,
            kb_code=self._default_kb_code,
            kb_name="默认知识库",
            description="系统自动创建的默认知识库",
            is_default=True,
        )
    
    self._default_kb_id = kb.kb_id
    return self._default_kb_id
```

### 2. API 参数可选

所有新增的 `kb_id` 参数都设置为可选，默认值为 `None`：

```python
kb_id: str | None = Field(default=None, description="知识库 ID")
```

当 `kb_id` 为 `None` 时，自动使用默认知识库。

### 3. 旧数据迁移

```python
async def migrate_existing_data(self) -> Dict[str, Any]:
    """
    迁移现有数据到默认知识库
    
    将现有 LightRAG 中的文档记录迁移到 MySQL
    """
    await self.initialize()
    
    async with self._get_session() as session:
        kb = await self._kb_manager.get_knowledge_base_by_code(
            session, self._default_kb_code
        )
        
        if not kb:
            return {"success": False, "error": "默认知识库不存在"}
        
        # 获取所有已处理文档
        processed_docs = await self.get_processed_docs()
        
        migrated = 0
        for doc in processed_docs:
            try:
                await self._kb_manager.add_document(
                    session=session,
                    kb_id=kb.kb_id,
                    doc_id=doc["doc_id"],
                    file_name=Path(doc["file_path"]).name,
                    file_path=doc["file_path"],
                )
                migrated += 1
            except Exception as e:
                logger.warning(f"迁移文档失败: {doc['doc_id']}, {e}")
        
        return {
            "success": True,
            "migrated": migrated,
            "total": len(processed_docs),
        }
```

---

## 需要修改的文件

| 文件 | 修改内容 |
|-----|---------|
| `backend/rag/service.py` | 引入知识库管理器，修改所有方法支持 kb_id 参数 |
| `backend/rag/__init__.py` | 导出新的类和方法 |
| `backend/api/routers/rag.py` | 修改请求模型，添加 kb_id 参数，新增跨知识库查询接口 |
| `backend/.env` | 添加默认知识库配置 |

---

## 配置更新

**文件**: `backend/.env`

```bash
# 默认知识库编码
DEFAULT_KB_CODE=default

# RAG 实例缓存大小
KB_MAX_CACHED_INSTANCES=10
```

---

## 验收标准

- [ ] 现有功能不受影响（向后兼容）
- [ ] 支持指定知识库入库
- [ ] 支持指定知识库查询
- [ ] 支持跨知识库查询
- [ ] 默认知识库自动创建
- [ ] 文档记录正确关联到知识库
- [ ] 单元测试通过
- [ ] API 文档更新

---

## 依赖关系

- 依赖任务一（数据库设计）
- 依赖任务二（NebulaGraph 存储）
- 依赖任务三（知识库管理器）

---

## 预计工期

2 天

---

## 测试策略

### 1. 单元测试

```python
import pytest
from unittest.mock import AsyncMock, MagicMock, patch

@pytest.mark.asyncio
async def test_aquery_with_kb_id():
    """测试指定知识库查询"""
    service = RagService()
    
    with patch.object(service, '_get_rag_instance') as mock_get_rag:
        mock_rag = AsyncMock()
        mock_rag.aquery.return_value = "test answer"
        mock_get_rag.return_value = mock_rag
        
        result = await service.aquery("test query", kb_id="kb_123")
        
        mock_get_rag.assert_called_once_with("kb_123")
        assert result == "test answer"


@pytest.mark.asyncio
async def test_insert_with_kb_id():
    """测试指定知识库入库"""
    service = RagService()
    
    with patch.object(service, '_get_rag_instance') as mock_get_rag, \
         patch.object(service, '_record_document') as mock_record:
        mock_rag = AsyncMock()
        mock_rag.insert_content_list.return_value = {
            "success": True,
            "doc_id": "doc_123"
        }
        mock_get_rag.return_value = mock_rag
        
        result = await service.insert_content_list(
            content_list=[{"text": "test"}],
            file_path="test.txt",
            kb_id="kb_123"
        )
        
        assert result["success"] is True
        mock_record.assert_called_once()


@pytest.mark.asyncio
async def test_backward_compatibility():
    """测试向后兼容性"""
    service = RagService()
    
    with patch.object(service, 'initialize') as mock_init, \
         patch.object(service, '_get_rag_instance') as mock_get_rag:
        mock_init.return_value = None
        mock_rag = AsyncMock()
        mock_rag.aquery.return_value = "test answer"
        service._rag = mock_rag
        service._initialized = True
        
        # 不传 kb_id，应使用默认知识库
        result = await service.aquery("test query")
        
        assert result == "test answer"
```

### 2. 集成测试

```python
@pytest.mark.asyncio
async def test_multi_kb_workflow():
    """测试多知识库完整工作流"""
    service = get_rag_service()
    
    # 创建知识库
    async with service._get_session() as session:
        kb1 = await service._kb_manager.create_knowledge_base(
            session=session,
            kb_code="test_kb_1",
            kb_name="测试知识库1",
        )
        kb2 = await service._kb_manager.create_knowledge_base(
            session=session,
            kb_code="test_kb_2",
            kb_name="测试知识库2",
        )
    
    # 入库到不同知识库
    result1 = await service.insert_content_list(
        content_list=[{"text": "知识库1的内容"}],
        file_path="kb1.txt",
        kb_id=kb1.kb_id,
    )
    
    result2 = await service.insert_content_list(
        content_list=[{"text": "知识库2的内容"}],
        file_path="kb2.txt",
        kb_id=kb2.kb_id,
    )
    
    # 查询不同知识库
    answer1 = await service.aquery("内容", kb_id=kb1.kb_id)
    answer2 = await service.aquery("内容", kb_id=kb2.kb_id)
    
    # 跨知识库查询
    multi_result = await service.query_multiple_kbs(
        query_text="内容",
        kb_ids=[kb1.kb_id, kb2.kb_id],
    )
    
    assert kb1.kb_id in multi_result
    assert kb2.kb_id in multi_result
```
