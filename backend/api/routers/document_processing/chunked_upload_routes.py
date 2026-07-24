"""分块上传路由"""

import asyncio
import uuid

from fastapi import APIRouter, Depends, File, Form, HTTPException, Query, UploadFile
from sqlalchemy.ext.asyncio import AsyncSession

from backend.api.dependencies.auth import ApiKeyAuth, get_db_session, verify_api_key
from backend.api.routers.document_processing.models import (
    ChunkedMergeResponse,
    ChunkedUploadInitResponse,
    ChunkedUploadStatusResponse,
    validate_file_type,
)
from backend.api.upload_utils import UploadTooLargeError, read_upload_with_limit
from backend.config.loggers import get_logger
from backend.config.settings import settings
from backend.db.crud.document_crud import document_crud
from backend.db.crud.task_crud import task_crud
from backend.kafka.client import get_event_producer, get_task_producer
from backend.kafka.exceptions import MessageSendException
from backend.kafka.schemas import (
    EventData,
    EventMessage,
    EventMessageBody,
    EventType,
    MessageHeader,
    MessageType,
    ProcessingOptions,
    TaskMessage,
    TaskMessageBody,
)
from backend.storage import chunked_upload_manager

router = APIRouter()
logger = get_logger(__name__)


def _validate_upload_id(upload_id: str) -> str:
    try:
        normalized = str(uuid.UUID(str(upload_id)))
    except (ValueError, AttributeError, TypeError) as exc:
        raise HTTPException(status_code=400, detail="无效的上传会话 ID") from exc
    if normalized != str(upload_id).lower():
        raise HTTPException(status_code=400, detail="无效的上传会话 ID")
    return normalized


@router.post("/upload/chunked/init", response_model=ChunkedUploadInitResponse)
async def init_chunked_upload(
    filename: str = Query(..., description="文件名"),
    total_size: int = Query(..., description="文件总大小(字节)"),
    content_type: str | None = Query(None, description="内容类型"),
    auth: ApiKeyAuth = Depends(verify_api_key),
):
    """初始化分块上传"""
    logger.info(f"初始化分块上传: filename={filename}, total_size={total_size}")

    try:
        validate_file_type(filename, content_type)

        if total_size <= 0 or total_size > settings.max_file_size:
            raise HTTPException(
                status_code=400,
                detail=f"文件大小超过限制: 最大 {settings.max_file_size / 1024 / 1024:.0f}MB",
            )

        session = await asyncio.to_thread(
            chunked_upload_manager.init_upload,
            filename=filename,
            total_size=total_size,
            content_type=content_type,
            user_id=auth.user_id,
        )

        from datetime import datetime, timedelta

        expires_at = datetime.now() + timedelta(hours=settings.upload_expire_hours)

        return ChunkedUploadInitResponse(
            upload_id=session["upload_id"],
            filename=session["filename"],
            total_size=session["total_size"],
            total_chunks=session["total_chunks"],
            chunk_size=session["chunk_size"],
            expires_at=expires_at.isoformat(),
        )

    except HTTPException:
        raise
    except Exception as e:
        logger.exception(f"初始化分块上传失败: {e}")
        raise HTTPException(status_code=500, detail="初始化分块上传失败") from e


@router.post("/upload/chunked/{upload_id}")
async def upload_chunk(
    upload_id: str,
    chunk_index: int = Query(..., description="分块索引"),
    chunk: UploadFile = File(...),
    auth: ApiKeyAuth = Depends(verify_api_key),
):
    """上传分块"""
    try:
        upload_id = _validate_upload_id(upload_id)
        try:
            content = await read_upload_with_limit(chunk, settings.chunk_size)
        except UploadTooLargeError as exc:
            raise HTTPException(status_code=413, detail="上传分块超过允许大小") from exc

        result = await asyncio.to_thread(
            chunked_upload_manager.save_chunk,
            upload_id=upload_id,
            chunk_index=chunk_index,
            content=content,
            user_id=auth.user_id,
        )

        if not result["success"]:
            raise HTTPException(
                status_code=400, detail=result.get("error", "上传分块失败")
            )

        return {
            "success": True,
            "chunk_index": chunk_index,
            "uploaded": result.get("uploaded", 0),
            "total": result.get("total", 0),
            "progress": result.get("progress", 0),
            "duplicate": result.get("duplicate", False),
        }

    except HTTPException:
        raise
    except Exception as e:
        logger.exception(f"上传分块失败: {e}")
        raise HTTPException(status_code=500, detail="上传分块失败") from e


@router.get(
    "/upload/chunked/{upload_id}/status", response_model=ChunkedUploadStatusResponse
)
async def get_chunked_upload_status(
    upload_id: str,
    auth: ApiKeyAuth = Depends(verify_api_key),
):
    """获取分块上传状态"""
    upload_id = _validate_upload_id(upload_id)
    progress = await asyncio.to_thread(
        chunked_upload_manager.get_upload_progress,
        upload_id,
        user_id=auth.user_id,
    )

    if not progress:
        raise HTTPException(status_code=404, detail="上传会话不存在")

    return ChunkedUploadStatusResponse(
        upload_id=progress["upload_id"],
        filename=progress["filename"],
        total_size=progress["total_size"],
        total_chunks=progress["total_chunks"],
        uploaded_chunks=progress["uploaded_chunks"],
        progress=progress["progress"],
        is_complete=progress["uploaded_chunks"] >= progress["total_chunks"],
    )


@router.post(
    "/upload/chunked/{upload_id}/complete", response_model=ChunkedMergeResponse
)
async def complete_chunked_upload(
    upload_id: str,
    ocr_enabled: bool = Form(True),
    table_extract: bool = Form(True),
    formula_extract: bool = Form(True),
    image_extract: bool = Form(True),
    vision_enhance: bool = Form(True),
    rag_enabled: bool = Form(False),
    output_format: str = Form("markdown"),
    auth: ApiKeyAuth = Depends(verify_api_key),
    session: AsyncSession = Depends(get_db_session),
):
    """完成分块上传并创建处理任务"""
    logger.info(f"完成分块上传: upload_id={upload_id}")

    merged = False
    committed = False
    try:
        upload_id = _validate_upload_id(upload_id)
        session_info = await asyncio.to_thread(
            chunked_upload_manager.get_upload_session,
            upload_id,
            user_id=auth.user_id,
        )
        if session_info is None:
            raise HTTPException(status_code=404, detail="上传会话不存在")
        success, message, file_path = await asyncio.to_thread(
            chunked_upload_manager.complete_upload,
            upload_id=upload_id,
            storage_type="documents",
            user_id=auth.user_id,
            cleanup_on_success=False,
        )

        if not success:
            raise HTTPException(status_code=400, detail=message)
        merged = True

        doc_type = validate_file_type(session_info["filename"])

        from pathlib import Path

        file_size = await asyncio.to_thread(Path(file_path).stat)
        file_size = file_size.st_size

        doc = await document_crud.create(
            session=session,
            filename=session_info["filename"],
            document_type=doc_type.value,
            file_size=file_size,
            file_path=file_path,
            user_id=auth.user_id,
        )

        options = {
            "ocr_enabled": ocr_enabled,
            "table_extract": table_extract,
            "formula_extract": formula_extract,
            "image_extract": image_extract,
            "vision_enhance": vision_enhance,
            "rag_enabled": rag_enabled,
            "output_format": output_format,
        }

        task = await task_crud.create(
            session=session,
            doc_id=doc.doc_id,
            user_id=auth.user_id,
            options=options,
        )

        task_producer = get_task_producer()
        event_producer = get_event_producer()
        task_message = TaskMessage(
            header=MessageHeader(
                message_type=MessageType.TASK_SUBMITTED,
                source="chunked-upload-api",
                user_id=auth.user_id,
                correlation_id=task.task_id,
            ),
            body=TaskMessageBody(
                task_id=task.task_id,
                document_id=doc.doc_id,
                file_path=file_path,
                file_name=session_info["filename"],
                file_size=file_size,
                file_type=doc_type.value,
                processing_options=ProcessingOptions(**options),
            ),
        )
        try:
            await task_producer.submit_task(task_message)
            event_message = EventMessage(
                header=MessageHeader(
                    message_type=MessageType.EVENT,
                    source="chunked-upload-api",
                    user_id=auth.user_id,
                    correlation_id=task.task_id,
                ),
                body=EventMessageBody(
                    event_type=EventType.TASK_CREATED,
                    task_id=task.task_id,
                    document_id=doc.doc_id,
                    data=EventData(
                        description="Chunked upload completed and task created",
                        extra_info={
                            "filename": session_info["filename"],
                            "file_size": file_size,
                        },
                    ),
                ),
            )
            try:
                await event_producer.send_event(event_message)
            except MessageSendException:
                logger.exception(
                    "Task-created event delivery failed after task submission: %s",
                    task.task_id,
                )
        except MessageSendException as e:
            logger.error(f"Failed to send Kafka message for task {task.task_id}: {e}")
            raise HTTPException(
                status_code=500, detail="分块上传合并成功，但任务提交到消息队列失败"
            ) from e

        await session.commit()
        committed = True
        cleaned = await asyncio.to_thread(
            chunked_upload_manager.cancel_upload,
            upload_id,
            user_id=auth.user_id,
        )
        if not cleaned:
            logger.warning("分块上传已提交，但临时会话清理失败: upload_id=%s", upload_id)

        return ChunkedMergeResponse(
            success=True,
            message="分块上传完成，任务已创建",
            doc_id=doc.doc_id,
            task_id=task.task_id,
            file_path=file_path,
        )

    except HTTPException:
        if merged and not committed:
            await session.rollback()
            await asyncio.to_thread(
                chunked_upload_manager.reset_completed_upload,
                upload_id,
                user_id=auth.user_id,
            )
        raise
    except Exception as e:
        if merged and not committed:
            await session.rollback()
            await asyncio.to_thread(
                chunked_upload_manager.reset_completed_upload,
                upload_id,
                user_id=auth.user_id,
            )
        logger.exception(f"完成分块上传失败: {e}")
        raise HTTPException(status_code=500, detail="完成分块上传失败") from e


@router.delete("/upload/chunked/{upload_id}")
async def cancel_chunked_upload(
    upload_id: str,
    auth: ApiKeyAuth = Depends(verify_api_key),
):
    """取消分块上传"""
    upload_id = _validate_upload_id(upload_id)
    success = await asyncio.to_thread(
        chunked_upload_manager.cancel_upload,
        upload_id,
        user_id=auth.user_id,
    )

    if success:
        return {"message": "上传已取消"}
    else:
        raise HTTPException(status_code=404, detail="上传会话不存在")


@router.get("/upload/chunked/{upload_id}/progress")
async def get_upload_progress(
    upload_id: str,
    auth: ApiKeyAuth = Depends(verify_api_key),
):
    """获取上传进度（用于进度回调）"""
    upload_id = _validate_upload_id(upload_id)
    progress = await asyncio.to_thread(
        chunked_upload_manager.get_upload_progress,
        upload_id,
        user_id=auth.user_id,
    )

    if not progress:
        raise HTTPException(status_code=404, detail="上传会话不存在")

    return {
        "upload_id": progress["upload_id"],
        "filename": progress["filename"],
        "total_size": progress["total_size"],
        "total_chunks": progress["total_chunks"],
        "uploaded_chunks": progress["uploaded_chunks"],
        "progress": round(progress["progress"], 2),
        "bytes_uploaded": progress["uploaded_chunks"]
        * progress.get("chunk_size", settings.chunk_size),
        "is_complete": progress["uploaded_chunks"] >= progress["total_chunks"],
    }
