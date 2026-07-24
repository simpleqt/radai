"""文件上传路由"""

import asyncio
import hashlib
import os
import uuid
from datetime import date
from io import BytesIO
from pathlib import Path
from typing import List, Optional

from fastapi import APIRouter, Depends, File, Form, HTTPException, Query, UploadFile
from pydantic import BaseModel, Field, field_validator
from sqlalchemy import func, select, update
from sqlalchemy.ext.asyncio import AsyncSession

from backend.api.dependencies.auth import (
    ApiKeyAuth,
    get_db_session,
    require_kb_access,
    verify_api_key,
)
from backend.api.upload_utils import (
    UploadTooLargeError,
    read_upload_with_limit,
    spool_file_with_limit,
)
from backend.config import settings
from backend.config.loggers import get_logger
from backend.db.models import KnowledgeBase, PendingFile
from backend.domain.knowledge_base.backend_policy import (
    LEGACY_BACKEND,
    SELF_BACKEND,
    declared_ingestion_backend,
)
from backend.rag.ingestion.delete_service import self_delete_service
from backend.storage import ChunkedUploadManager, _get_minio_client, get_presigned_url

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

# 自研入库 ParserRouter 实际支持的类型；上传阶段即拦截，避免“上传成功、入库才炸”。
SELF_INGESTION_EXTENSIONS = {
    ".pdf": "application/pdf",
    ".docx": "application/vnd.openxmlformats-officedocument.wordprocessingml.document",
    ".txt": "text/plain",
    ".md": "text/markdown",
    ".png": "image/png",
    ".jpg": "image/jpeg",
    ".jpeg": "image/jpeg",
    ".gif": "image/gif",
    ".webp": "image/webp",
    ".bmp": "image/bmp",
    ".tiff": "image/tiff",
}

DEFAULT_MAX_FILE_SIZE = 600 * 1024 * 1024
MAX_FILE_SIZE = int(getattr(settings, "max_file_size", DEFAULT_MAX_FILE_SIZE) or DEFAULT_MAX_FILE_SIZE)
CHUNK_SIZE = 10 * 1024 * 1024
MAX_BATCH_FILES = 20

_legacy_chunk_manager = ChunkedUploadManager()
_legacy_chunk_manager.temp_dir = Path(settings.upload_temp_dir) / "kb-pending"
_legacy_chunk_manager.chunk_size = CHUNK_SIZE
_legacy_chunk_manager._ensure_directories()


class UploadInitRequest(BaseModel):
    kb_id: str = Field(..., min_length=1, max_length=36, description="知识库ID")
    filename: str = Field(..., min_length=1, max_length=255, description="文件名")
    file_size: int = Field(..., gt=0, le=MAX_FILE_SIZE, description="文件大小")
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


def get_file_extension(filename: str) -> str:
    return "." + filename.rsplit(".", 1)[-1].lower() if "." in filename else ""


def _sha256_bytes(content: bytes) -> str:
    return hashlib.sha256(content).hexdigest()


def _sha256_file(path: str) -> str:
    digest = hashlib.sha256()
    with open(path, "rb") as fh:
        for chunk in iter(lambda: fh.read(1024 * 1024), b""):
            digest.update(chunk)
    return digest.hexdigest()


def _minio_uri(minio_path: str) -> str:
    return f"minio://{settings.minio_bucket_name}/{minio_path}"


def _allowed_extensions_for_backend(ingestion_backend: str | None) -> dict[str, str]:
    if ingestion_backend == SELF_BACKEND:
        return SELF_INGESTION_EXTENSIONS
    return ALLOWED_EXTENSIONS


def validate_file(
    filename: str,
    file_size: int,
    *,
    ingestion_backend: str | None = None,
) -> tuple[bool, str]:
    ext = get_file_extension(filename)
    allowed = _allowed_extensions_for_backend(ingestion_backend)
    if ext not in allowed:
        if ingestion_backend == SELF_BACKEND and ext in ALLOWED_EXTENSIONS:
            return (
                False,
                f"自研入库不支持 {ext}，请转换为 pdf/docx/txt/md 或图片后上传"
                f"（支持: {', '.join(sorted(SELF_INGESTION_EXTENSIONS))}）",
            )
        return False, f"不支持的文件类型: {ext}"
    if file_size <= 0:
        return False, "文件不能为空"
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


def upload_file_to_minio(
    local_path: str,
    minio_path: str,
    content_type: str = "application/octet-stream",
) -> bool:
    """从临时文件流式上传，避免合并分块时再次占用整文件内存。"""
    try:
        client = _get_minio_client()
        client.fput_object(
            settings.minio_bucket_name,
            minio_path,
            local_path,
            content_type=content_type,
        )
        return True
    except Exception as exc:
        logger.error("上传临时文件到 MinIO 失败: %s, 错误: %s", minio_path, exc)
        return False


def _safe_filename(filename: str) -> str:
    safe_name = Path(filename).name.strip()
    if not safe_name or safe_name in {".", ".."} or safe_name != filename.strip():
        raise HTTPException(status_code=400, detail="文件名不合法")
    return safe_name


def _validate_upload_id(upload_id: str) -> None:
    try:
        uuid.UUID(upload_id)
    except (ValueError, TypeError, AttributeError) as exc:
        raise HTTPException(status_code=400, detail="upload_id 格式不合法") from exc


def get_minio_public_url(minio_path: str) -> str:
    return get_presigned_url(minio_path, expiry_seconds=15 * 60) or ""


@router.post("/init", response_model=UploadInitResponse)
async def init_chunked_upload(
    request: UploadInitRequest,
    auth: ApiKeyAuth = Depends(verify_api_key),
    session: AsyncSession = Depends(get_db_session),
):
    await asyncio.to_thread(_legacy_chunk_manager.cleanup_expired_sessions)
    filename = _safe_filename(request.filename)
    kb = await require_kb_access(session, request.kb_id, auth, access="write")
    ingestion_backend = declared_ingestion_backend(settings, kb=kb)
    valid, error = validate_file(
        filename, request.file_size, ingestion_backend=ingestion_backend
    )
    if not valid:
        raise HTTPException(status_code=400, detail=error)

    file_id = str(uuid.uuid4())
    minio_path = f"pending/{request.kb_id}/{file_id}/{filename}"
    
    total_chunks = (request.file_size + CHUNK_SIZE - 1) // CHUNK_SIZE
    
    upload_session = _legacy_chunk_manager.init_upload(
        filename=filename,
        total_size=request.file_size,
        content_type=request.content_type,
        user_id=auth.user_id,
        metadata={
            "file_id": file_id,
            "kb_id": request.kb_id,
            "minio_path": minio_path,
            "category_path": request.category_path,
        },
    )
    upload_id = upload_session["upload_id"]
    
    logger.info(f"初始化分块上传: upload_id={upload_id}, file_id={file_id}, total_chunks={total_chunks}")

    return UploadInitResponse(
        success=True,
        upload_id=upload_id,
        file_id=file_id,
        minio_path=minio_path,
    )


@router.post("/chunks/{upload_id}/{chunk_index}", response_model=ChunkUploadResponse)
async def upload_chunk(
    upload_id: str,
    chunk_index: int,
    chunk: UploadFile = File(...),
    auth: ApiKeyAuth = Depends(verify_api_key),
):
    _validate_upload_id(upload_id)
    session_data = _legacy_chunk_manager.get_upload_session(
        upload_id,
        user_id=auth.user_id,
    )
    if not session_data:
        raise HTTPException(status_code=404, detail="上传会话不存在，请先调用 init 接口")
    if session_data.get("state") != "open":
        raise HTTPException(status_code=409, detail="上传会话正在完成")
    total = int(session_data["total_chunks"])
    if chunk_index < 0 or chunk_index >= total:
        raise HTTPException(status_code=400, detail="分块索引超出范围")
    expected_size = min(
        CHUNK_SIZE,
        int(session_data["total_size"]) - chunk_index * CHUNK_SIZE,
    )

    try:
        content = await read_upload_with_limit(chunk, expected_size)
    except UploadTooLargeError as exc:
        raise HTTPException(status_code=413, detail="分块大小超过限制") from exc
    if len(content) != expected_size:
        raise HTTPException(status_code=400, detail=f"分块大小不正确，应为 {expected_size} 字节")

    result = await asyncio.to_thread(
        _legacy_chunk_manager.save_chunk,
        upload_id,
        chunk_index,
        content,
        user_id=auth.user_id,
    )
    if not result["success"]:
        status_code = 409 if "正在完成" in result.get("error", "") else 400
        raise HTTPException(status_code=status_code, detail=result.get("error", "上传分块失败"))
    uploaded = int(result.get("uploaded", 0))
    total = int(result.get("total", total))
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
    _validate_upload_id(upload_id)
    chunk_session = _legacy_chunk_manager.get_upload_session(
        upload_id,
        user_id=auth.user_id,
    )
    if not chunk_session:
        raise HTTPException(status_code=404, detail="上传会话不存在")
    expected_chunks = int(chunk_session["total_chunks"])
    if total_chunks not in (0, expected_chunks):
        raise HTTPException(status_code=400, detail="total_chunks 与初始化信息不一致")
    success, message, merged_path = await asyncio.to_thread(
        _legacy_chunk_manager.complete_upload,
        upload_id,
        user_id=auth.user_id,
        cleanup_on_success=False,
    )
    if not success or not merged_path:
        raise HTTPException(status_code=400, detail=message)

    metadata = dict(chunk_session.get("metadata") or {})
    minio_path = str(metadata.get("minio_path") or f"pending/{upload_id}")
    content_type = chunk_session.get("content_type") or "application/octet-stream"
    file_size = int(chunk_session["total_size"])
    kb_id = str(metadata.get("kb_id") or "")
    kb = await require_kb_access(session, kb_id, auth, access="write")
    ingestion_backend = declared_ingestion_backend(settings, kb=kb)

    uploaded = await asyncio.to_thread(
        upload_file_to_minio,
        merged_path,
        minio_path,
        content_type,
    )
    if not uploaded:
        _legacy_chunk_manager.reset_completed_upload(upload_id, user_id=auth.user_id)
        raise HTTPException(status_code=500, detail="上传到存储失败")

    file_id = str(metadata.get("file_id") or uuid.uuid4())
    filename = str(chunk_session.get("filename") or "unknown")
    source_hash = await asyncio.to_thread(_sha256_file, merged_path)

    pending_file = PendingFile(
        upload_id=upload_id,
        kb_id=kb_id,
        file_id=file_id,
        file_name=filename,
        file_size=file_size,
        file_type=content_type,
        minio_path=minio_path,
        source_hash=source_hash,
        object_bucket=settings.minio_bucket_name,
        object_key=minio_path,
        original_uri=_minio_uri(minio_path),
        mime_type=content_type,
        file_ext=get_file_extension(filename),
        ingestion_backend=ingestion_backend,
        category_path=metadata.get("category_path") or None,
        status=0,
        created_by=auth.user_id,
    )
    try:
        session.add(pending_file)
        await session.commit()
    except Exception:
        await session.rollback()
        try:
            client = _get_minio_client()
            await asyncio.to_thread(
                client.remove_object,
                settings.minio_bucket_name,
                minio_path,
            )
        except Exception as cleanup_exc:
            logger.exception(
                "数据库写入失败后清理 MinIO 对象失败: %s",
                cleanup_exc,
            )
        _legacy_chunk_manager.reset_completed_upload(upload_id, user_id=auth.user_id)
        raise

    _legacy_chunk_manager.cancel_upload(upload_id, user_id=auth.user_id)
    try:
        os.unlink(merged_path)
    except FileNotFoundError:
        pass
    
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
    if not kb_id or len(kb_id) > 36:
        raise HTTPException(status_code=400, detail="kb_id 不合法")
    if not files:
        raise HTTPException(status_code=400, detail="至少需要上传一个文件")
    if len(files) > MAX_BATCH_FILES:
        raise HTTPException(status_code=400, detail=f"单次最多上传 {MAX_BATCH_FILES} 个文件")

    upload_id = str(uuid.uuid4())
    uploaded_files = []
    kb = await require_kb_access(session, kb_id, auth, access="write")
    ingestion_backend = declared_ingestion_backend(settings, kb=kb)
    allowed_exts = _allowed_extensions_for_backend(ingestion_backend)

    for file in files:
        filename = _safe_filename(file.filename or "")
        file_id = str(uuid.uuid4())
        temp_path: str | None = None
        try:
            await file.seek(0)
            temp_path, file_size, source_hash = await asyncio.to_thread(
                spool_file_with_limit,
                file.file,
                MAX_FILE_SIZE,
                temp_dir=settings.upload_temp_dir,
            )
        except UploadTooLargeError:
            logger.warning("文件超过大小限制: %s", filename)
            continue

        valid, error = validate_file(
            filename, file_size, ingestion_backend=ingestion_backend
        )
        if not valid:
            logger.warning(f"文件验证失败: {file.filename}, {error}")
            await asyncio.to_thread(os.unlink, temp_path)
            continue

        ext = get_file_extension(filename)
        content_type = allowed_exts.get(
            ext, file.content_type or "application/octet-stream"
        )
        minio_path = f"pending/{kb_id}/{file_id}/{filename}"

        try:
            uploaded = await asyncio.to_thread(
                upload_file_to_minio,
                temp_path,
                minio_path,
                content_type,
            )
        finally:
            await asyncio.to_thread(Path(temp_path).unlink, missing_ok=True)
        if not uploaded:
            logger.error(f"上传到 MinIO 失败: {file.filename}")
            continue

        pending_file = PendingFile(
            upload_id=upload_id,
            kb_id=kb_id,
            file_id=file_id,
            file_name=filename,
            file_size=file_size,
            file_type=content_type,
            minio_path=minio_path,
            source_hash=source_hash,
            object_bucket=settings.minio_bucket_name,
            object_key=minio_path,
            original_uri=_minio_uri(minio_path),
            mime_type=content_type,
            file_ext=ext,
            ingestion_backend=ingestion_backend,
            category_path=category_path or None,
            status=0,
            created_by=auth.user_id,
        )
        session.add(pending_file)

        uploaded_files.append(BatchUploadFile(
            file_id=file_id,
            file_name=filename,
            file_size=file_size,
            minio_path=minio_path,
            status="pending",
        ))

    try:
        await session.commit()
    except Exception:
        await session.rollback()
        client = _get_minio_client()
        cleanup_results = await asyncio.gather(
            *(
                asyncio.to_thread(
                    client.remove_object,
                    settings.minio_bucket_name,
                    uploaded.minio_path,
                )
                for uploaded in uploaded_files
            ),
            return_exceptions=True,
        )
        for uploaded, result in zip(uploaded_files, cleanup_results, strict=True):
            if isinstance(result, Exception):
                logger.error(
                    "批量上传回滚后清理对象失败: path=%s error=%s",
                    uploaded.minio_path,
                    result,
                )
        raise
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
    skip: int = Query(default=0, ge=0),
    limit: int = Query(default=50, ge=1, le=200),
    auth: ApiKeyAuth = Depends(verify_api_key),
    session: AsyncSession = Depends(get_db_session),
):
    owner_filter = PendingFile.created_by == auth.user_id
    query = select(PendingFile).where(owner_filter)

    if kb_id:
        query = query.where(PendingFile.kb_id == kb_id)
    if status is not None:
        query = query.where(PendingFile.status == status)

    query = query.order_by(PendingFile.created_at.desc()).offset(skip).limit(limit)

    result = await session.execute(query)
    items = result.scalars().all()

    count_query = select(func.count(PendingFile.id)).where(owner_filter)
    if kb_id:
        count_query = count_query.where(PendingFile.kb_id == kb_id)
    if status is not None:
        count_query = count_query.where(PendingFile.status == status)

    total = int((await session.execute(count_query)).scalar_one())

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
        .where(
            PendingFile.file_id.in_(request.file_ids),
            PendingFile.created_by == auth.user_id,
        )
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
            PendingFile.created_by == auth.user_id,
        )
    )
    pending_files = result.scalars().all()

    if not pending_files:
        raise HTTPException(status_code=404, detail="未找到待入库文件")

    kb_result = await session.execute(
        select(KnowledgeBase).where(KnowledgeBase.kb_id == request.kb_id)
    )
    kb = kb_result.scalar_one_or_none()
    await require_kb_access(session, request.kb_id, auth, access="write")
    ingestion_backend = declared_ingestion_backend(settings, kb=kb)

    update_data = {
        "status": 1,
        "document_date": request.document_date,
        "upload_id": task_id,
        "ingest_job_id": task_id,
        "ingestion_backend": ingestion_backend,
    }
    if request.category_path is not None:
        update_data["category_path"] = request.category_path or None

    await session.execute(
        update(PendingFile)
        .where(
            PendingFile.file_id.in_(request.file_ids),
            PendingFile.kb_id == request.kb_id,
            PendingFile.created_by == auth.user_id,
        )
        .values(**update_data)
    )
    await session.commit()

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
    result = await session.execute(
        select(PendingFile).where(
            PendingFile.upload_id == task_id,
            PendingFile.created_by == auth.user_id,
        )
    )
    pending_files = result.scalars().all()
    if not pending_files:
        raise HTTPException(status_code=404, detail="任务不存在")

    status_map = {
        0: "pending",
        1: "processing",
        2: "completed",
        3: "failed",
    }

    files_status = []
    completed = 0
    failed = 0
    for pf in pending_files:
        file_status = status_map.get(pf.status, "pending")
        error = pf.error_message
        file_name = pf.file_name

        if file_status == "completed":
            completed += 1
        elif file_status == "failed":
            failed += 1

        files_status.append(
            IngestFileStatus(
                file_id=pf.file_id,
                file_name=file_name,
                status=file_status,
                doc_id=None,
                error=error,
            )
        )

    total = len(pending_files)
    if completed + failed >= total and total > 0:
        overall_status = "completed" if failed == 0 else "partial"
    else:
        overall_status = "processing"

    return IngestStatusResponse(
        task_id=task_id,
        kb_id=pending_files[0].kb_id,
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
        select(PendingFile).where(
            PendingFile.file_id == file_id,
            PendingFile.created_by == auth.user_id,
        )
    )
    pending_file = result.scalar_one_or_none()

    if not pending_file:
        raise HTTPException(status_code=404, detail="文件不存在")

    await require_kb_access(
        session,
        pending_file.kb_id,
        auth,
        access="write",
    )

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
                # 严格隔离：按文档 ingestion_backend 分流删除，
                # self 文档走 self_delete_service，绝不调用 LightRAG delete；
                # legacy 文档走 rag_service.delete_doc_by_id，绝不触碰 kb_self_*。
                doc_backend = getattr(kb_doc, "ingestion_backend", None) or LEGACY_BACKEND
                if doc_backend == SELF_BACKEND:
                    await self_delete_service.soft_delete(
                        session, kb_id=pending_file.kb_id, doc_id=kb_doc.doc_id,
                    )
                    logger.info(f"已删除自研入库文档: {kb_doc.doc_id}")
                else:
                    rag_service = get_rag_service()
                    await rag_service.delete_doc_by_id(
                        doc_id=kb_doc.doc_id,
                        kb_id=pending_file.kb_id,
                    )
                    logger.info(f"已删除 legacy 入库文档: {kb_doc.doc_id}")
            except Exception as e:
                logger.exception("删除入库文档失败: %s", e)
                raise HTTPException(
                    status_code=502,
                    detail="索引删除失败，文件记录已保留，可稍后重试",
                ) from e

    try:
        client = _get_minio_client()
        await asyncio.to_thread(
            client.remove_object,
            settings.minio_bucket_name,
            pending_file.minio_path,
        )
    except Exception as e:
        logger.exception("删除 MinIO 文件失败: %s", e)
        raise HTTPException(
            status_code=502,
            detail="对象存储删除失败，文件记录已保留，可稍后重试",
        ) from e

    await session.delete(pending_file)
    await session.commit()

    return {"success": True, "message": "文件已删除"}
