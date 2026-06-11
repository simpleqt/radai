"""文件上传路由"""

import uuid
from datetime import date, datetime
from io import BytesIO
from typing import List, Optional

from fastapi import APIRouter, Depends, File, Form, HTTPException, UploadFile
from pydantic import BaseModel, Field, field_validator
from sqlalchemy import select, update
from sqlalchemy.ext.asyncio import AsyncSession

from backend.api.dependencies.auth import ApiKeyAuth, verify_api_key, get_db_session
from backend.config import settings
from backend.config.loggers import get_logger
from backend.db.models import PendingFile
from backend.storage import _get_minio_client, get_public_url

router = APIRouter(prefix="/uploads", tags=["文件上传"])
logger = get_logger(__name__)

ALLOWED_EXTENSIONS = {
    ".pdf": "application/pdf",
    ".doc": "application/msword",
    ".docx": "application/vnd.openxmlformats-officedocument.wordprocessingml.document",
    ".txt": "text/plain",
    ".md": "text/markdown",
    ".xlsx": "application/vnd.openxmlformats-officedocument.spreadsheetml.sheet",
    ".xls": "application/vnd.ms-excel",
    ".ppt": "application/vnd.ms-powerpoint",
    ".pptx": "application/vnd.openxmlformats-officedocument.presentationml.presentation",
}

MAX_FILE_SIZE = 100 * 1024 * 1024


class UploadInitRequest(BaseModel):
    kb_id: str = Field(..., description="知识库ID")
    filename: str = Field(..., description="文件名")
    file_size: int = Field(..., description="文件大小")
    content_type: Optional[str] = Field(None, description="内容类型")
    category_path: Optional[str] = Field(None, description="自定义分类路径")


class UploadInitResponse(BaseModel):
    success: bool
    upload_id: str
    file_id: str
    minio_path: str


class ChunkUploadResponse(BaseModel):
    success: bool
    chunk_index: int
    uploaded_chunks: int
    total_chunks: int
    progress: float


class CompleteUploadResponse(BaseModel):
    success: bool
    file_id: str
    minio_url: str
    message: str


class PendingFileResponse(BaseModel):
    file_id: str
    kb_id: str
    file_name: str
    file_size: int
    file_type: Optional[str]
    status: int
    error_message: Optional[str]
    document_date: Optional[str] = None
    category_path: Optional[str] = None
    created_at: str

    class Config:
        from_attributes = True


class PendingFileListResponse(BaseModel):
    items: List[PendingFileResponse]
    total: int


class PendingFileMoveRequest(BaseModel):
    file_ids: List[str]
    category_path: Optional[str] = None


class BatchUploadFile(BaseModel):
    file_id: str
    file_name: str
    file_size: int
    minio_path: str
    status: str


class BatchUploadResponse(BaseModel):
    success: bool
    upload_id: str
    files: List[BatchUploadFile]
    total: int
    message: str


class IngestRequest(BaseModel):
    kb_id: str
    file_ids: List[str]
    document_date: Optional[date] = Field(None, description="文档业务日期(YYYY-MM-DD)")
    category_path: Optional[str] = Field(None, description="自定义分类路径")
    options: Optional[dict] = None

    @field_validator("document_date")
    @classmethod
    def validate_document_date(cls, value: Optional[date]) -> Optional[date]:
        return value


class IngestFileStatus(BaseModel):
    file_id: str
    file_name: str
    status: str
    doc_id: Optional[str] = None
    error: Optional[str] = None


class IngestStatusResponse(BaseModel):
    task_id: str
    kb_id: str
    status: str
    total: int
    completed: int
    failed: int
    progress: float
    files: List[IngestFileStatus]


class IngestResponse(BaseModel):
    success: bool
    task_id: str
    files_count: int
    message: str


_ingest_tasks = {}


def get_file_extension(filename: str) -> str:
    return "." + filename.rsplit(".", 1)[-1].lower() if "." in filename else ""


def validate_file(filename: str, file_size: int) -> tuple[bool, str]:
    ext = get_file_extension(filename)
    if ext not in ALLOWED_EXTENSIONS:
        return False, f"不支持的文件类型: {ext}"
    if file_size > MAX_FILE_SIZE:
        return False, f"文件大小超过限制: {file_size / 1024 / 1024:.1f}MB > {MAX_FILE_SIZE / 1024 / 1024}MB"
    return True, ""


def upload_to_minio(content: bytes, minio_path: str, content_type: str = "application/octet-stream") -> bool:
    try:
        client = _get_minio_client()
        data = BytesIO(content)
        # KB 上传链路使用公共桶（settings.minio_bucket_name）是设计例外，
        # 不走用户作用域桶；用户隔离策略由 KB 权限体系单独保障。
        client.put_object(
            settings.minio_bucket_name,
            minio_path,
            data,
            length=len(content),
            content_type=content_type,
        )
        logger.info(f"文件已上传到 MinIO: {minio_path}, 大小: {len(content)} bytes")
        return True
    except Exception as e:
        logger.error(f"上传到 MinIO 失败: {minio_path}, 错误: {type(e).__name__}: {e}")
        import traceback
        logger.error(traceback.format_exc())
        return False


def get_minio_public_url(minio_path: str) -> str:
    return get_public_url(minio_path) or f"{settings.minio_endpoint.rstrip('/')}/{settings.minio_bucket_name}/{minio_path}"


@router.post("/init", response_model=UploadInitResponse)
async def init_chunked_upload(
    request: UploadInitRequest,
    auth: ApiKeyAuth = Depends(verify_api_key),
):
    valid, error = validate_file(request.filename, request.file_size)
    if not valid:
        raise HTTPException(status_code=400, detail=error)

    file_id = str(uuid.uuid4())
    upload_id = str(uuid.uuid4())
    minio_path = f"pending/{request.kb_id}/{file_id}/{request.filename}"
    
    total_chunks = (request.file_size + MAX_FILE_SIZE - 1) // MAX_FILE_SIZE if request.file_size > 0 else 1
    
    _chunk_sessions[upload_id] = {
        "chunks": {},
        "total_chunks": total_chunks,
        "file_id": file_id,
        "kb_id": request.kb_id,
        "filename": request.filename,
        "minio_path": minio_path,
        "content_type": request.content_type,
        "file_size": request.file_size,
    }
    
    logger.info(f"初始化分块上传: upload_id={upload_id}, file_id={file_id}, total_chunks={total_chunks}")

    return UploadInitResponse(
        success=True,
        upload_id=upload_id,
        file_id=file_id,
        minio_path=minio_path,
    )


_chunk_sessions = {}


@router.post("/chunks/{upload_id}/{chunk_index}", response_model=ChunkUploadResponse)
async def upload_chunk(
    upload_id: str,
    chunk_index: int,
    chunk: UploadFile = File(...),
    auth: ApiKeyAuth = Depends(verify_api_key),
):
    if upload_id not in _chunk_sessions:
        raise HTTPException(status_code=404, detail="上传会话不存在，请先调用 init 接口")

    content = await chunk.read()
    _chunk_sessions[upload_id]["chunks"][chunk_index] = content

    session_data = _chunk_sessions[upload_id]
    uploaded = len(session_data["chunks"])
    total = session_data.get("total_chunks", uploaded)
    progress = (uploaded / total * 100) if total > 0 else 0

    return ChunkUploadResponse(
        success=True,
        chunk_index=chunk_index,
        uploaded_chunks=uploaded,
        total_chunks=total,
        progress=progress,
    )


@router.post("/complete/{upload_id}", response_model=CompleteUploadResponse)
async def complete_upload(
    upload_id: str,
    total_chunks: int = 0,
    auth: ApiKeyAuth = Depends(verify_api_key),
    session: AsyncSession = Depends(get_db_session),
):
    if upload_id not in _chunk_sessions:
        raise HTTPException(status_code=404, detail="上传会话不存在")

    chunk_session = _chunk_sessions[upload_id]
    logger.info(f"完成上传: upload_id={upload_id}, chunks={len(chunk_session['chunks'])}, expected={total_chunks}")

    if len(chunk_session["chunks"]) != total_chunks:
        raise HTTPException(
            status_code=400,
            detail=f"分块不完整: {len(chunk_session['chunks'])}/{total_chunks}",
        )

    content = b"".join(chunk_session["chunks"][i] for i in range(total_chunks))
    minio_path = chunk_session.get("minio_path", f"pending/{upload_id}")
    content_type = chunk_session.get("content_type", "application/octet-stream")
    
    logger.info(f"准备上传到 MinIO: path={minio_path}, size={len(content)}, type={content_type}")

    if not upload_to_minio(content, minio_path, content_type):
        raise HTTPException(status_code=500, detail="上传到存储失败")

    file_id = chunk_session.get("file_id", str(uuid.uuid4()))
    kb_id = chunk_session.get("kb_id", "")
    filename = chunk_session.get("filename", "unknown")

    pending_file = PendingFile(
        upload_id=upload_id,
        kb_id=kb_id,
        file_id=file_id,
        file_name=filename,
        file_size=len(content),
        file_type=content_type,
        minio_path=minio_path,
        status=0,
        created_by=auth.user_id,
    )
    session.add(pending_file)
    await session.commit()

    del _chunk_sessions[upload_id]
    
    logger.info(f"文件上传完成: file_id={file_id}, filename={filename}")

    minio_url = get_minio_public_url(minio_path)

    return CompleteUploadResponse(
        success=True,
        file_id=file_id,
        minio_url=minio_url,
        message="文件上传成功，等待入库",
    )


@router.post("/batch", response_model=BatchUploadResponse)
async def batch_upload(
    kb_id: str = Form(...),
    category_path: Optional[str] = Form(None, description="自定义分类路径"),
    files: List[UploadFile] = File(...),
    auth: ApiKeyAuth = Depends(verify_api_key),
    session: AsyncSession = Depends(get_db_session),
):
    upload_id = str(uuid.uuid4())
    uploaded_files = []

    for file in files:
        file_id = str(uuid.uuid4())
        content = await file.read()

        valid, error = validate_file(file.filename or "unknown", len(content))
        if not valid:
            logger.warning(f"文件验证失败: {file.filename}, {error}")
            continue

        ext = get_file_extension(file.filename or "")
        content_type = ALLOWED_EXTENSIONS.get(ext, file.content_type or "application/octet-stream")
        minio_path = f"pending/{kb_id}/{file_id}/{file.filename}"

        if not upload_to_minio(content, minio_path, content_type):
            logger.error(f"上传到 MinIO 失败: {file.filename}")
            continue

        pending_file = PendingFile(
            upload_id=upload_id,
            kb_id=kb_id,
            file_id=file_id,
            file_name=file.filename or "unknown",
            file_size=len(content),
            file_type=content_type,
            minio_path=minio_path,
            category_path=category_path or None,
            status=0,
            created_by=auth.user_id,
        )
        session.add(pending_file)

        uploaded_files.append(BatchUploadFile(
            file_id=file_id,
            file_name=file.filename or "unknown",
            file_size=len(content),
            minio_path=minio_path,
            status="pending",
        ))

    await session.commit()
    logger.info(f"批量上传完成: upload_id={upload_id}, files={len(uploaded_files)}")

    return BatchUploadResponse(
        success=True,
        upload_id=upload_id,
        files=uploaded_files,
        total=len(uploaded_files),
        message=f"批量上传成功，共 {len(uploaded_files)} 个文件等待入库",
    )


@router.get("/pending", response_model=PendingFileListResponse)
async def list_pending_files(
    kb_id: Optional[str] = None,
    status: Optional[int] = None,
    skip: int = 0,
    limit: int = 50,
    auth: ApiKeyAuth = Depends(verify_api_key),
    session: AsyncSession = Depends(get_db_session),
):
    query = select(PendingFile)

    if kb_id:
        query = query.where(PendingFile.kb_id == kb_id)
    if status is not None:
        query = query.where(PendingFile.status == status)

    query = query.order_by(PendingFile.created_at.desc()).offset(skip).limit(limit)

    result = await session.execute(query)
    items = result.scalars().all()

    count_query = select(PendingFile)
    if kb_id:
        count_query = count_query.where(PendingFile.kb_id == kb_id)
    if status is not None:
        count_query = count_query.where(PendingFile.status == status)

    count_result = await session.execute(count_query)
    total = len(count_result.scalars().all())

    return PendingFileListResponse(
        items=[
            PendingFileResponse(
                file_id=item.file_id,
                kb_id=item.kb_id,
                file_name=item.file_name,
                file_size=item.file_size,
                file_type=item.file_type,
                status=item.status,
                error_message=item.error_message,
                document_date=item.document_date.isoformat() if item.document_date else None,
                category_path=item.category_path if hasattr(item, "category_path") else None,
                created_at=item.created_at.isoformat() if item.created_at else "",
            )
            for item in items
        ],
        total=total,
    )


@router.patch("/pending/batch-move")
async def batch_move_pending_files(
    request: PendingFileMoveRequest,
    auth: ApiKeyAuth = Depends(verify_api_key),
    session: AsyncSession = Depends(get_db_session),
):
    if not request.file_ids:
        raise HTTPException(status_code=400, detail="file_ids 不能为空")

    new_path = request.category_path or None
    result = await session.execute(
        update(PendingFile)
        .where(PendingFile.file_id.in_(request.file_ids))
        .values(category_path=new_path)
    )
    await session.commit()

    updated = result.rowcount or 0
    logger.info(f"批量移动待入库文件: {updated} 条 → '{new_path}'")
    return {"success": True, "updated": updated, "category_path": new_path}


@router.post("/ingest", response_model=IngestResponse)
async def start_ingest(
    request: IngestRequest,
    auth: ApiKeyAuth = Depends(verify_api_key),
    session: AsyncSession = Depends(get_db_session),
):
    task_id = str(uuid.uuid4())

    result = await session.execute(
        select(PendingFile).where(
            PendingFile.file_id.in_(request.file_ids),
            PendingFile.kb_id == request.kb_id,
        )
    )
    pending_files = result.scalars().all()

    if not pending_files:
        raise HTTPException(status_code=404, detail="未找到待入库文件")

    update_data = {"status": 1, "document_date": request.document_date}
    if request.category_path is not None:
        update_data["category_path"] = request.category_path or None

    await session.execute(
        update(PendingFile)
        .where(PendingFile.file_id.in_(request.file_ids))
        .values(**update_data)
    )
    await session.commit()

    _ingest_tasks[task_id] = {
        "kb_id": request.kb_id,
        "status": "queued",
        "total": len(pending_files),
        "completed": 0,
        "failed": 0,
        "file_ids": [pf.file_id for pf in pending_files],
        "files": [
            {
                "file_id": pf.file_id,
                "file_name": pf.file_name,
                "status": "queued",
                "doc_id": None,
                "error": None,
            }
            for pf in pending_files
        ],
        "options": request.options or {},
        "document_date": request.document_date.isoformat() if request.document_date else None,
        "created_at": datetime.now(),
    }

    return IngestResponse(
        success=True,
        task_id=task_id,
        files_count=len(pending_files),
        message="入库任务已创建，已提交给独立 worker 处理",
    )


@router.get("/ingest/status/{task_id}", response_model=IngestStatusResponse)
async def get_ingest_status(
    task_id: str,
    auth: ApiKeyAuth = Depends(verify_api_key),
    session: AsyncSession = Depends(get_db_session),
):
    if task_id not in _ingest_tasks:
        raise HTTPException(status_code=404, detail="任务不存在")

    task = _ingest_tasks[task_id]

    file_ids = task.get("file_ids", [])
    if not file_ids:
        raise HTTPException(status_code=404, detail="任务文件不存在")

    result = await session.execute(
        select(PendingFile).where(PendingFile.file_id.in_(file_ids))
    )
    pending_files = {pf.file_id: pf for pf in result.scalars().all()}

    status_map = {
        0: "pending",
        1: "processing",
        2: "completed",
        3: "failed",
    }

    files_status = []
    completed = 0
    failed = 0
    for file_id in file_ids:
        pf = pending_files.get(file_id)
        if not pf:
            file_status = "failed"
            error = "文件记录不存在"
            file_name = file_id
        else:
            file_status = status_map.get(pf.status, "pending")
            error = pf.error_message
            file_name = pf.file_name

        if file_status == "completed":
            completed += 1
        elif file_status == "failed":
            failed += 1

        files_status.append(
            IngestFileStatus(
                file_id=file_id,
                file_name=file_name,
                status=file_status,
                doc_id=None,
                error=error,
            )
        )

    total = len(file_ids)
    if completed + failed >= total and total > 0:
        overall_status = "completed" if failed == 0 else "partial"
    else:
        overall_status = "processing"

    task["status"] = overall_status
    task["total"] = total
    task["completed"] = completed
    task["failed"] = failed

    return IngestStatusResponse(
        task_id=task_id,
        kb_id=task["kb_id"],
        status=overall_status,
        total=total,
        completed=completed,
        failed=failed,
        progress=(completed + failed) / total * 100 if total > 0 else 0,
        files=files_status,
    )


@router.delete("/pending/{file_id}")
async def delete_pending_file(
    file_id: str,
    auth: ApiKeyAuth = Depends(verify_api_key),
    session: AsyncSession = Depends(get_db_session),
):
    result = await session.execute(
        select(PendingFile).where(PendingFile.file_id == file_id)
    )
    pending_file = result.scalar_one_or_none()

    if not pending_file:
        raise HTTPException(status_code=404, detail="文件不存在")

    if pending_file.status == 1:
        raise HTTPException(status_code=400, detail="文件正在入库中，无法删除")

    if pending_file.status == 2:
        from backend.db.models import KnowledgeBaseDocument
        from backend.rag.service import get_rag_service
        
        doc_result = await session.execute(
            select(KnowledgeBaseDocument).where(
                KnowledgeBaseDocument.kb_id == pending_file.kb_id,
                KnowledgeBaseDocument.file_path == pending_file.minio_path,
            )
        )
        kb_doc = doc_result.scalar_one_or_none()
        
        if kb_doc:
            try:
                rag_service = get_rag_service()
                await rag_service.delete_doc_by_id(
                    doc_id=kb_doc.doc_id,
                    kb_id=pending_file.kb_id,
                )
                logger.info(f"已删除入库文档: {kb_doc.doc_id}")
            except Exception as e:
                logger.warning(f"删除入库文档失败: {e}")

    try:
        client = _get_minio_client()
        client.remove_object(settings.minio_bucket_name, pending_file.minio_path)
    except Exception as e:
        logger.warning(f"删除 MinIO 文件失败: {e}")

    await session.delete(pending_file)
    await session.commit()

    return {"success": True, "message": "文件已删除"}
