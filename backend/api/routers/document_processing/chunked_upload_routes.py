"""分块上传路由"""

from fastapi import APIRouter, Depends, File, Form, HTTPException, Query, UploadFile
from sqlalchemy.ext.asyncio import AsyncSession

from backend.api.dependencies.auth import ApiKeyAuth, get_db_session, verify_api_key
from backend.api.routers.document_processing.models import (
    ChunkedMergeResponse,
    ChunkedUploadInitResponse,
    ChunkedUploadStatusResponse,
    validate_file_type,
)
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

        if total_size > settings.max_file_size:
            raise HTTPException(
                status_code=400,
                detail=f"文件大小超过限制: 最大 {settings.max_file_size / 1024 / 1024:.0f}MB",
            )

        session = chunked_upload_manager.init_upload(
            filename=filename,
            total_size=total_size,
            content_type=content_type,
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
        raise HTTPException(status_code=500, detail=f"初始化分块上传失败: {str(e)}")


@router.post("/upload/chunked/{upload_id}")
async def upload_chunk(
    upload_id: str,
    chunk_index: int = Query(..., description="分块索引"),
    chunk: UploadFile = File(...),
    auth: ApiKeyAuth = Depends(verify_api_key),
):
    """上传分块"""
    try:
        content = await chunk.read()

        result = chunked_upload_manager.save_chunk(
            upload_id=upload_id,
            chunk_index=chunk_index,
            content=content,
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
        raise HTTPException(status_code=500, detail=f"上传分块失败: {str(e)}")


@router.get(
    "/upload/chunked/{upload_id}/status", response_model=ChunkedUploadStatusResponse
)
async def get_chunked_upload_status(
    upload_id: str,
    auth: ApiKeyAuth = Depends(verify_api_key),
):
    """获取分块上传状态"""
    progress = chunked_upload_manager.get_upload_progress(upload_id)

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

    try:
        success, message, file_path = chunked_upload_manager.complete_upload(
            upload_id=upload_id,
            storage_type="documents",
        )

        if not success:
            raise HTTPException(status_code=400, detail=message)

        session_info = chunked_upload_manager.get_upload_session(upload_id)

        doc_type = validate_file_type(session_info["filename"])

        import os

        file_size = os.path.getsize(file_path)

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
            await event_producer.send_event(event_message)
        except MessageSendException as e:
            logger.error(f"Failed to send Kafka message for task {task.task_id}: {e}")
            raise HTTPException(
                status_code=500, detail="分块上传合并成功，但任务提交到消息队列失败"
            )

        return ChunkedMergeResponse(
            success=True,
            message="分块上传完成，任务已创建",
            doc_id=doc.doc_id,
            task_id=task.task_id,
            file_path=file_path,
        )

    except HTTPException:
        raise
    except Exception as e:
        logger.exception(f"完成分块上传失败: {e}")
        raise HTTPException(status_code=500, detail=f"完成分块上传失败: {str(e)}")


@router.delete("/upload/chunked/{upload_id}")
async def cancel_chunked_upload(
    upload_id: str,
    auth: ApiKeyAuth = Depends(verify_api_key),
):
    """取消分块上传"""
    success = chunked_upload_manager.cancel_upload(upload_id)

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
    progress = chunked_upload_manager.get_upload_progress(upload_id)

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
