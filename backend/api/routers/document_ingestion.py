"""
文档入库路由 - 提供数据库文档入库的API接口
"""

import asyncio
from typing import List, Optional

from fastapi import APIRouter, Depends, HTTPException, Query
from pydantic import BaseModel, Field

from backend.api.dependencies.auth import ApiKeyAuth, require_admin
from backend.config.loggers import get_logger
from backend.db.connection import get_session
from backend.db.crud.file_info_crud import FileInfoCRUD

router = APIRouter()
logger = get_logger(__name__)


async def _run_file_info_crud_in_thread(func):
    """在线程池中执行同步 FileInfoCRUD，避免阻塞事件循环。"""

    def _runner():
        session = get_session()
        try:
            crud = FileInfoCRUD(session)
            return func(crud)
        finally:
            session.close()

    return await asyncio.to_thread(_runner)


class PendingDocument(BaseModel):
    """待入库文档信息"""
    task_id: str = Field(..., description="任务ID")
    file_name: str = Field(..., description="文件名")
    parse_status: int = Field(..., description="解析状态")
    storage_status: int = Field(..., description="存储状态")
    md_file_path: str = Field(..., description="MD文件路径")


class PendingDocumentsResponse(BaseModel):
    """待入库文档列表响应"""
    total: int = Field(..., description="总数")
    documents: List[PendingDocument] = Field(default_factory=list, description="文档列表")


class DocumentListItem(BaseModel):
    """文档列表项"""
    task_id: str = Field(..., description="任务ID")
    file_name: str = Field(..., description="文件名")
    parse_status: int = Field(..., description="解析状态(0:未解析,1:已解析)")
    storage_status: int = Field(..., description="存储状态(0:未入库,1:已入库)")
    md_file_path: str = Field(..., description="MD文件路径")


class DocumentListResponse(BaseModel):
    """文档列表响应"""
    total: int = Field(..., description="总记录数")
    page: int = Field(..., description="当前页码")
    page_size: int = Field(..., description="每页数量")
    total_pages: int = Field(..., description="总页数")
    documents: List[DocumentListItem] = Field(default_factory=list, description="文档列表")


class DocumentIngestRequest(BaseModel):
    """文档入库请求"""
    task_id: Optional[str] = Field(default=None, description="指定任务ID，不指定则处理所有待入库")
    limit: int = Field(default=10, ge=1, le=100, description="最大处理数量")
    dry_run: bool = Field(default=False, description="预览模式，不实际入库")


class DocumentIngestResult(BaseModel):
    """单个文档入库结果"""
    task_id: str = Field(..., description="任务ID")
    file_name: Optional[str] = Field(default=None, description="文件名")
    success: bool = Field(..., description="是否成功")
    doc_id: Optional[str] = Field(default=None, description="文档ID")
    text_length: Optional[int] = Field(default=None, description="文本长度")
    images_count: Optional[int] = Field(default=None, description="图片数量")
    elapsed_time: Optional[float] = Field(default=None, description="耗时(秒)")
    error: Optional[str] = Field(default=None, description="错误信息")


class DocumentIngestResponse(BaseModel):
    """文档入库响应"""
    success: bool = Field(..., description="整体是否成功")
    processed_count: int = Field(..., description="处理数量")
    successful_count: int = Field(..., description="成功数量")
    failed_count: int = Field(..., description="失败数量")
    results: List[DocumentIngestResult] = Field(default_factory=list, description="详细结果")
    dry_run: bool = Field(default=False, description="是否为预览模式")


class DocumentStatusResponse(BaseModel):
    """文档状态响应"""
    task_id: str = Field(..., description="任务ID")
    exists: bool = Field(..., description="是否存在")
    file_name: Optional[str] = Field(default=None, description="文件名")
    parse_status: Optional[int] = Field(default=None, description="解析状态")
    storage_status: Optional[int] = Field(default=None, description="存储状态")
    md_file_path: Optional[str] = Field(default=None, description="MD文件路径")


@router.get("/pending", response_model=PendingDocumentsResponse)
async def list_pending_documents(
    limit: int = Query(default=100, ge=1, le=1000, description="返回最大数量"),
    auth: ApiKeyAuth = Depends(require_admin),
):
    """
    获取待入库文档列表

    查询条件: parse_status=1 AND storage_status=0
    """
    try:
        pending_files = await _run_file_info_crud_in_thread(
            lambda crud: crud.get_pending_files(limit=limit)
        )

        documents = [
            PendingDocument(
                task_id=f.task_id,
                file_name=f.file_name,
                parse_status=f.parse_status,
                storage_status=f.storage_status,
                md_file_path=f.md_file_path,
            )
            for f in pending_files
        ]

        return PendingDocumentsResponse(
            total=len(documents),
            documents=documents
        )

    except Exception as e:
        logger.exception(f"获取待入库文档列表失败: {e}")
        raise HTTPException(status_code=500, detail="获取待入库文档列表失败")


@router.get("/list", response_model=DocumentListResponse)
async def list_documents(
    page: int = Query(default=1, ge=1, description="页码"),
    page_size: int = Query(default=20, ge=1, le=100, description="每页数量"),
    parse_status: Optional[int] = Query(default=None, description="解析状态过滤(0:未解析,1:已解析)"),
    storage_status: Optional[int] = Query(default=None, description="存储状态过滤(0:未入库,1:已入库)"),
    file_name: Optional[str] = Query(default=None, description="文件名模糊搜索"),
    auth: ApiKeyAuth = Depends(require_admin),
):
    """
    获取文档列表（支持分页和过滤）
    """
    try:
        skip = (page - 1) * page_size
        documents, total = await _run_file_info_crud_in_thread(
            lambda crud: crud.get_all_files(
                skip=skip,
                limit=page_size,
                parse_status=parse_status,
                storage_status=storage_status,
                file_name_like=file_name,
            )
        )

        total_pages = (total + page_size - 1) // page_size if total > 0 else 1

        return DocumentListResponse(
            total=total,
            page=page,
            page_size=page_size,
            total_pages=total_pages,
            documents=[
                DocumentListItem(
                    task_id=f.task_id,
                    file_name=f.file_name,
                    parse_status=f.parse_status,
                    storage_status=f.storage_status,
                    md_file_path=f.md_file_path,
                )
                for f in documents
            ]
        )

    except Exception as e:
        logger.exception(f"获取文档列表失败: {e}")
        raise HTTPException(status_code=500, detail="获取文档列表失败")


@router.get("/status/{task_id}", response_model=DocumentStatusResponse)
async def get_document_status(
    task_id: str,
    auth: ApiKeyAuth = Depends(require_admin),
):
    """
    获取指定文档的状态
    """
    try:
        file_info = await _run_file_info_crud_in_thread(
            lambda crud: crud.get_by_task_id(task_id)
        )

        if not file_info:
            return DocumentStatusResponse(
                task_id=task_id,
                exists=False
            )

        return DocumentStatusResponse(
            task_id=task_id,
            exists=True,
            file_name=file_info.file_name,
            parse_status=file_info.parse_status,
            storage_status=file_info.storage_status,
            md_file_path=file_info.md_file_path,
        )

    except Exception as e:
        logger.exception(f"获取文档状态失败: {e}")
        raise HTTPException(status_code=500, detail="获取文档状态失败")


@router.post("/ingest", response_model=DocumentIngestResponse)
async def ingest_documents(
    request: DocumentIngestRequest,
    auth: ApiKeyAuth = Depends(require_admin),
):
    """
    执行文档入库

    从数据库查询待入库文档，使用MDIngestor进行入库处理
    """
    from backend.rag.document_processor import DocumentProcessor

    processor = None
    try:
        processor = DocumentProcessor()

        if request.task_id:
            # 处理单个文档
            logger.info(f"处理单个文档: {request.task_id}")
            result = await processor.process_document_by_task_id(
                task_id=request.task_id,
                dry_run=request.dry_run
            )
            results = [result]
        else:
            # 批量处理
            logger.info(f"批量处理待入库文档，限制: {request.limit}")
            results = await processor.process_pending_documents(
                limit=request.limit,
                dry_run=request.dry_run
            )

        # 统计结果
        successful = [r for r in results if r.get('success')]
        failed = [r for r in results if not r.get('success')]

        # 转换为响应模型
        ingest_results = [
            DocumentIngestResult(
                task_id=r.get('task_id', ''),
                file_name=r.get('file_name'),
                success=r.get('success', False),
                doc_id=r.get('doc_id'),
                text_length=r.get('text_length'),
                images_count=r.get('images_count'),
                elapsed_time=r.get('elapsed_time'),
                error=r.get('error'),
            )
            for r in results
        ]

        return DocumentIngestResponse(
            success=len(failed) == 0,
            processed_count=len(results),
            successful_count=len(successful),
            failed_count=len(failed),
            results=ingest_results,
            dry_run=request.dry_run,
        )

    except Exception as e:
        logger.exception(f"文档入库失败: {e}")
        raise HTTPException(status_code=500, detail="文档入库失败")
    finally:
        if processor:
            processor.close()


@router.post("/ingest/{task_id}", response_model=DocumentIngestResponse)
async def ingest_single_document(
    task_id: str,
    dry_run: bool = Query(default=False, description="预览模式"),
    auth: ApiKeyAuth = Depends(require_admin),
):
    """
    入库指定文档

    根据task_id处理单个文档
    """
    request = DocumentIngestRequest(
        task_id=task_id,
        dry_run=dry_run
    )
    return await ingest_documents(request, auth)


@router.get("/stats")
async def get_ingestion_stats(
    auth: ApiKeyAuth = Depends(require_admin),
):
    """
    获取入库统计信息
    """
    try:
        counts = await _run_file_info_crud_in_thread(
            lambda crud: crud.get_statistics()
        )
        total_count = counts["total"]
        stored_count = counts["stored"]
        parsed_count = counts["parsed"]
        pending_count = counts["pending"]

        return {
            "total_documents": total_count,
            "parsed_documents": parsed_count,
            "stored_documents": stored_count,
            "pending_ingestion": pending_count,
            "storage_rate": stored_count / total_count if total_count > 0 else 0,
        }

    except Exception as e:
        logger.exception(f"获取统计信息失败: {e}")
        raise HTTPException(status_code=500, detail="获取统计信息失败")
