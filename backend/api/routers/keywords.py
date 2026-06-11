"""
关键词索引管理 API 路由
"""

import asyncio
from datetime import datetime
from typing import Optional
from uuid import uuid4

from fastapi import APIRouter, Depends, HTTPException
from pydantic import BaseModel
from sqlalchemy.ext.asyncio import AsyncSession

from backend.api.dependencies.auth import ApiKeyAuth, require_admin
from backend.config.loggers import get_logger
from backend.db.connection import get_async_session_factory
from backend.rag.knowledge_base.manager import get_kb_manager
from backend.rag.keywords.index_manager import get_keyword_index_manager
from backend.rag.keywords.models import ExtractionMode

logger = get_logger(__name__)

router = APIRouter(prefix="/keywords", tags=["关键词管理"])

_keyword_init_tasks: dict[str, dict] = {}


class BuildIndexRequest(BaseModel):
    kb_id: str


class BuildIndexResponse(BaseModel):
    status: str
    entity_count: int
    message: str


class ExtractKeywordsRequest(BaseModel):
    kb_id: str
    query: str
    mode: str = "hybrid"


class ExtractKeywordsResponse(BaseModel):
    kb_id: str
    kb_code: str
    high_level_keywords: list[str]
    low_level_keywords: list[str]
    entity_scores: dict[str, float]
    extraction_time_ms: float


class IndexStatsResponse(BaseModel):
    kb_id: str
    kb_code: str
    entity_count: int
    vector_count: int
    bm25_doc_count: int
    bm25_term_count: int
    last_updated: Optional[str]
    status: str


class InitKeywordRequest(BaseModel):
    force_rebuild: bool = True
    init_bm25: bool = True
    warmup_low_level: bool = True
    include_vector: bool = False


class InitKeywordResponse(BaseModel):
    task_id: str
    status: str
    message: str


class InitKeywordStatusResponse(BaseModel):
    task_id: str
    status: str
    phase: str
    message: str
    created_at: str
    updated_at: str
    result: Optional[dict] = None
    error: Optional[str] = None


async def get_session():
    factory = get_async_session_factory()
    async with factory() as session:
        yield session


@router.post("/init/{kb_id}", response_model=InitKeywordResponse)
async def init_keyword_components(
    kb_id: str,
    request: InitKeywordRequest,
    _auth: ApiKeyAuth = Depends(require_admin),
    session: AsyncSession = Depends(get_session),
):
    """初始化关键词能力（仅 BM25 + 低级关键词图）。"""
    if request.include_vector:
        raise HTTPException(status_code=400, detail="初始化接口不支持 include_vector=true")

    kb_manager = get_kb_manager()
    kb = await kb_manager.get_knowledge_base(session, kb_id)
    if not kb:
        raise HTTPException(status_code=404, detail="知识库不存在")

    task_id = uuid4().hex
    now = datetime.now().isoformat()
    _keyword_init_tasks[task_id] = {
        "task_id": task_id,
        "status": "running",
        "phase": "queued",
        "message": "初始化任务已创建",
        "created_at": now,
        "updated_at": now,
        "result": None,
        "error": None,
    }

    async def run_task() -> None:
        factory = get_async_session_factory()

        def update_state(phase: str, message: str, extra: dict) -> None:
            task = _keyword_init_tasks[task_id]
            task["phase"] = phase
            task["message"] = message
            task["updated_at"] = datetime.now().isoformat()
            if extra:
                base_result = task.get("result") or {}
                base_result.update(extra)
                task["result"] = base_result

        try:
            async with factory() as task_session:
                result = await kb_manager.initialize_keyword_components(
                    task_session,
                    kb_id,
                    force_rebuild=request.force_rebuild,
                    init_bm25=request.init_bm25,
                    warmup_low_level=request.warmup_low_level,
                    progress_callback=update_state,
                )
            task = _keyword_init_tasks[task_id]
            task["status"] = "success"
            task["phase"] = "done"
            task["message"] = "关键词初始化完成"
            task["updated_at"] = datetime.now().isoformat()
            task["result"] = result
        except Exception as e:
            logger.error(f"关键词初始化失败: kb_id={kb_id}, error={e}", exc_info=True)
            task = _keyword_init_tasks[task_id]
            task["status"] = "failed"
            task["phase"] = "failed"
            task["message"] = "关键词初始化失败"
            task["updated_at"] = datetime.now().isoformat()
            task["error"] = str(e)

    asyncio.create_task(run_task())

    return InitKeywordResponse(
        task_id=task_id,
        status="accepted",
        message="关键词初始化任务已提交",
    )


@router.get("/init/status/{task_id}", response_model=InitKeywordStatusResponse)
async def get_init_keyword_status(
    task_id: str,
    _auth: ApiKeyAuth = Depends(require_admin),
):
    """查询关键词初始化任务状态。"""
    task = _keyword_init_tasks.get(task_id)
    if not task:
        raise HTTPException(status_code=404, detail="任务不存在")

    return InitKeywordStatusResponse(**task)


@router.post("/build", response_model=BuildIndexResponse)
async def build_keyword_index(
    request: BuildIndexRequest,
    _auth: ApiKeyAuth = Depends(require_admin),
    session: AsyncSession = Depends(get_session),
):
    """
    构建知识库的关键词索引
    
    从 Milvus 获取实体数据，构建 BM25 索引和向量索引
    """
    try:
        kb_manager = get_kb_manager()
        
        success = await kb_manager.build_keyword_index(session, request.kb_id)
        
        if success:
            kb = await kb_manager.get_knowledge_base(session, request.kb_id)
            return BuildIndexResponse(
                status="success",
                entity_count=kb.entity_count if kb else 0,
                message="关键词索引构建成功",
            )
        else:
            raise HTTPException(status_code=500, detail="关键词索引构建失败")
            
    except ValueError as e:
        raise HTTPException(status_code=400, detail=str(e))
    except Exception as e:
        logger.error(f"构建关键词索引失败: {e}")
        raise HTTPException(status_code=500, detail=str(e))


@router.post("/extract", response_model=ExtractKeywordsResponse)
async def extract_keywords(
    request: ExtractKeywordsRequest,
    _auth: ApiKeyAuth = Depends(require_admin),
    session: AsyncSession = Depends(get_session),
):
    """
    从指定知识库提取关键词
    
    使用混合检索（向量 + BM25）提取高级和低级关键词
    """
    try:
        kb_manager = get_kb_manager()
        
        kb = await kb_manager.get_knowledge_base(session, request.kb_id)
        if not kb:
            raise HTTPException(status_code=404, detail="知识库不存在")
        
        extractor = await kb_manager.get_keyword_extractor(session, request.kb_id)
        
        mode_map = {
            "vector": ExtractionMode.VECTOR,
            "bm25": ExtractionMode.BM25,
            "hybrid": ExtractionMode.HYBRID,
        }
        extraction_mode = mode_map.get(request.mode, ExtractionMode.HYBRID)
        
        result = await extractor.extract(request.query, extraction_mode)
        
        return ExtractKeywordsResponse(
            kb_id=request.kb_id,
            kb_code=kb.kb_code,
            high_level_keywords=result.high_level_keywords,
            low_level_keywords=result.low_level_keywords,
            entity_scores=result.entity_scores,
            extraction_time_ms=result.extraction_time_ms,
        )
        
    except ValueError as e:
        raise HTTPException(status_code=400, detail=str(e))
    except Exception as e:
        logger.error(f"提取关键词失败: {e}")
        raise HTTPException(status_code=500, detail=str(e))


@router.get("/stats/{kb_id}", response_model=IndexStatsResponse)
async def get_index_stats(
    kb_id: str,
    _auth: ApiKeyAuth = Depends(require_admin),
    session: AsyncSession = Depends(get_session),
):
    """
    获取知识库关键词索引的统计信息
    """
    try:
        kb_manager = get_kb_manager()
        
        kb = await kb_manager.get_knowledge_base(session, kb_id)
        if not kb:
            raise HTTPException(status_code=404, detail="知识库不存在")
        
        index_manager = await kb_manager.get_keyword_index_manager()
        stats = await index_manager.get_stats(kb.kb_code, kb_id)
        
        return IndexStatsResponse(
            kb_id=kb_id,
            kb_code=kb.kb_code,
            entity_count=stats.entity_count,
            vector_count=stats.vector_count,
            bm25_doc_count=stats.bm25_doc_count,
            bm25_term_count=stats.bm25_term_count,
            last_updated=stats.last_updated.isoformat() if stats.last_updated else None,
            status=stats.status,
        )
        
    except ValueError as e:
        raise HTTPException(status_code=400, detail=str(e))
    except Exception as e:
        logger.error(f"获取索引统计失败: {e}")
        raise HTTPException(status_code=500, detail=str(e))


@router.delete("/index/{kb_id}")
async def delete_keyword_index(
    kb_id: str,
    _auth: ApiKeyAuth = Depends(require_admin),
    session: AsyncSession = Depends(get_session),
):
    """
    删除知识库的关键词索引
    
    包括 Redis 缓存和 Milvus Collection
    """
    try:
        kb_manager = get_kb_manager()
        
        kb = await kb_manager.get_knowledge_base(session, kb_id)
        if not kb:
            raise HTTPException(status_code=404, detail="知识库不存在")
        
        index_manager = await kb_manager.get_keyword_index_manager()
        success = await index_manager.delete_index(kb.kb_code)
        
        if success:
            return {"status": "success", "message": "关键词索引删除成功"}
        else:
            raise HTTPException(status_code=500, detail="关键词索引删除失败")
            
    except ValueError as e:
        raise HTTPException(status_code=400, detail=str(e))
    except Exception as e:
        logger.error(f"删除关键词索引失败: {e}")
        raise HTTPException(status_code=500, detail=str(e))


@router.post("/invalidate/{kb_id}")
async def invalidate_keyword_index(
    kb_id: str,
    _auth: ApiKeyAuth = Depends(require_admin),
    session: AsyncSession = Depends(get_session),
):
    """
    使知识库的关键词索引缓存失效
    
    下次访问时会重新加载索引
    """
    try:
        kb_manager = get_kb_manager()
        
        kb = await kb_manager.get_knowledge_base(session, kb_id)
        if not kb:
            raise HTTPException(status_code=404, detail="知识库不存在")
        
        index_manager = await kb_manager.get_keyword_index_manager()
        await index_manager.invalidate_index(kb.kb_code)
        
        return {"status": "success", "message": "关键词索引缓存已失效"}
        
    except ValueError as e:
        raise HTTPException(status_code=400, detail=str(e))
    except Exception as e:
        logger.error(f"使索引缓存失效失败: {e}")
        raise HTTPException(status_code=500, detail=str(e))


@router.get("/cached")
async def list_cached_kb(_auth: ApiKeyAuth = Depends(require_admin)):
    """
    列出已缓存的知识库索引
    """
    try:
        index_manager = get_keyword_index_manager()
        cached_kb = await index_manager.list_cached_kb()
        
        return {
            "status": "success",
            "cached_kb": cached_kb,
            "count": len(cached_kb),
        }
        
    except Exception as e:
        logger.error(f"列出缓存知识库失败: {e}")
        raise HTTPException(status_code=500, detail=str(e))
