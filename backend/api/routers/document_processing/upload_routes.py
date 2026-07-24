"""基本上传路由"""

import asyncio
import uuid
from pathlib import Path

from fastapi import APIRouter, Depends, File, Form, HTTPException, UploadFile
from sqlalchemy.ext.asyncio import AsyncSession

from backend.api.dependencies.auth import ApiKeyAuth, get_db_session, verify_api_key
from backend.api.routers.document_processing.models import (
    BatchTaskResponse,
    TaskCreateResponse,
    TaskStatus,
    validate_file_type,
)
from backend.api.upload_utils import (
    UploadTooLargeError,
    spool_file_with_limit,
)
from backend.config.loggers import get_logger
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
from backend.storage import storage_manager

router = APIRouter()
logger = get_logger(__name__)


@router.post("/upload", response_model=TaskCreateResponse)
async def upload_document(
    file: UploadFile = File(...),
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
    """上传并处理文档"""
    logger.info(f"接收文档上传: {file.filename}")
    file_path: str | None = None

    try:
        doc_type = validate_file_type(file.filename, file.content_type)

        try:
            _storage_id, storage_path = storage_manager.generate_storage_path(
                file.filename,
                "documents",
            )
            file_path, file_size, file_hash = await asyncio.to_thread(
                spool_file_with_limit,
                file.file,
                100 * 1024 * 1024,
                target_path=storage_path,
            )
        except UploadTooLargeError as exc:
            raise HTTPException(status_code=413, detail="文件大小超过 100MB 限制") from exc

        options = {
            "ocr_enabled": ocr_enabled,
            "table_extract": table_extract,
            "formula_extract": formula_extract,
            "image_extract": image_extract,
            "vision_enhance": vision_enhance,
            "rag_enabled": rag_enabled,
            "output_format": output_format,
        }

        doc = await document_crud.create(
            session=session,
            filename=file.filename,
            document_type=doc_type.value,
            file_size=file_size,
            file_path=file_path,
            user_id=auth.user_id,
            file_hash=file_hash,
        )

        task = await task_crud.create(
            session=session,
            doc_id=doc.doc_id,
            user_id=auth.user_id,
            options=options,
        )

        try:
            await _send_task_to_kafka(
                task=task,
                doc=doc,
                file_path=file_path,
                filename=file.filename,
                file_size=file_size,
                doc_type=doc_type,
                options=options,
                user_id=auth.user_id,
            )
            logger.info(f"Kafka messages sent for task: {task.task_id}")

        except MessageSendException as e:
            logger.error(f"Failed to send Kafka message for task {task.task_id}: {e}")
            raise HTTPException(
                status_code=500, detail="文档上传成功，但任务提交到消息队列失败"
            ) from e

        logger.info(
            "文档上传成功: doc_id=%s, task_id=%s",
            doc.doc_id,
            task.task_id,
        )

        return TaskCreateResponse(
            task_id=task.task_id,
            doc_id=doc.doc_id,
            status=TaskStatus.PENDING,
            message="文档上传成功，任务已创建",
        )

    except HTTPException:
        if file_path:
            await asyncio.to_thread(Path(file_path).unlink, missing_ok=True)
        raise
    except Exception as e:
        logger.exception(f"文档上传失败: {e}")
        if file_path:
            await asyncio.to_thread(Path(file_path).unlink, missing_ok=True)
        raise HTTPException(status_code=500, detail="文档上传失败") from e


@router.post("/batch", response_model=BatchTaskResponse)
async def batch_upload_documents(
    files: list[UploadFile] = File(...),
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
    """批量上传文档"""
    logger.info(f"接收批量文档上传: {len(files)} 个文件")

    if not files:
        raise HTTPException(status_code=400, detail="至少需要上传一个文件")
    if len(files) > 20:
        raise HTTPException(status_code=400, detail="单次最多上传 20 个文件")

    batch_id = str(uuid.uuid4())
    tasks = []

    for file in files:
        file_path: str | None = None
        doc = None
        try:
            doc_type = validate_file_type(file.filename, file.content_type)

            try:
                _storage_id, storage_path = storage_manager.generate_storage_path(
                    file.filename,
                    "documents",
                )
                file_path, file_size, _sha256 = await asyncio.to_thread(
                    spool_file_with_limit,
                    file.file,
                    100 * 1024 * 1024,
                    target_path=storage_path,
                )
            except UploadTooLargeError:
                logger.warning(f"文件 {file.filename} 超过大小限制，跳过")
                tasks.append(
                    TaskCreateResponse(
                        task_id="",
                        doc_id="",
                        status=TaskStatus.FAILED,
                        message="文件大小超过 100MB 限制",
                    )
                )
                continue

            options = {
                "ocr_enabled": ocr_enabled,
                "table_extract": table_extract,
                "formula_extract": formula_extract,
                "image_extract": image_extract,
                "vision_enhance": vision_enhance,
                "rag_enabled": rag_enabled,
                "output_format": output_format,
            }

            doc = await document_crud.create(
                session=session,
                filename=file.filename,
                document_type=doc_type.value,
                file_size=file_size,
                file_path=file_path,
                user_id=auth.user_id,
                file_hash=_sha256,
            )

            task = await task_crud.create(
                session=session,
                doc_id=doc.doc_id,
                user_id=auth.user_id,
                batch_id=batch_id,
                options=options,
            )

            try:
                await _send_task_to_kafka(
                    task=task,
                    doc=doc,
                    file_path=file_path,
                    filename=file.filename,
                    file_size=file_size,
                    doc_type=doc_type,
                    options=options,
                    user_id=auth.user_id,
                    description=f"Batch upload: document {file.filename} uploaded and task created",
                    extra_info={
                        "filename": file.filename,
                        "file_size": file_size,
                        "batch_id": task.batch_id or "N/A",
                    },
                )
                logger.info(f"Kafka messages sent for batch task: {task.task_id}")
            except MessageSendException:
                logger.exception(
                    "Failed to send Kafka message for batch task %s",
                    task.task_id,
                )
                task.status = TaskStatus.FAILED.value
                task.error = "任务提交到消息队列失败"
                tasks.append(
                    TaskCreateResponse(
                        task_id=task.task_id,
                        doc_id=doc.doc_id,
                        status=TaskStatus.FAILED,
                        message="任务提交到消息队列失败",
                    )
                )
                continue

            tasks.append(
                TaskCreateResponse(
                    task_id=task.task_id,
                    doc_id=doc.doc_id,
                    status=TaskStatus.PENDING,
                    message="任务已创建",
                )
            )

        except HTTPException as e:
            logger.warning(f"文件 {file.filename} 验证失败: {e.detail}")
            if file_path and doc is None:
                await asyncio.to_thread(Path(file_path).unlink, missing_ok=True)
            tasks.append(
                TaskCreateResponse(
                    task_id="", doc_id="", status=TaskStatus.FAILED, message=e.detail
                )
            )
        except Exception as e:
            logger.exception(f"处理文件 {file.filename} 时出错: {e}")
            if file_path and doc is None:
                await asyncio.to_thread(Path(file_path).unlink, missing_ok=True)
            tasks.append(
                TaskCreateResponse(
                    task_id="", doc_id="", status=TaskStatus.FAILED, message="文件处理失败"
                )
            )

    return BatchTaskResponse(
        batch_id=batch_id,
        total=len(files),
        tasks=tasks,
    )


async def _send_task_to_kafka(
    task,
    doc,
    file_path: str,
    filename: str,
    file_size: int,
    doc_type,
    options: dict,
    user_id: str,
    description: str = "Document upload completed and task created",
    extra_info: dict = None,
):
    """
    发送任务消息到 Kafka

    Args:
        task: 任务对象
        doc: 文档对象
        file_path: 文件路径
        filename: 文件名
        file_size: 文件大小
        doc_type: 文档类型
        options: 处理选项
        user_id: 用户ID
        description: 事件描述
        extra_info: 额外信息
    """
    task_producer = get_task_producer()
    event_producer = get_event_producer()

    task_message = TaskMessage(
        header=MessageHeader(
            message_type=MessageType.TASK_SUBMITTED,
            source="upload-api",
            user_id=user_id,
            correlation_id=task.task_id,
        ),
        body=TaskMessageBody(
            task_id=task.task_id,
            document_id=doc.doc_id,
            file_path=file_path,
            file_name=filename,
            file_size=file_size,
            file_type=doc_type.value,
            processing_options=ProcessingOptions(**options),
        ),
    )
    await task_producer.submit_task(task_message)

    if extra_info is None:
        extra_info = {"filename": filename, "file_size": file_size}

    event_message = EventMessage(
        header=MessageHeader(
            message_type=MessageType.EVENT,
            source="upload-api",
            user_id=user_id,
            correlation_id=task.task_id,
        ),
        body=EventMessageBody(
            event_type=EventType.TASK_CREATED,
            task_id=task.task_id,
            document_id=doc.doc_id,
            data=EventData(
                description=description,
                extra_info=extra_info,
            ),
        ),
    )
    try:
        await event_producer.send_event(event_message)
    except MessageSendException:
        # The processing task has already been accepted.  A telemetry-event
        # failure must not mark it failed or remove the worker's input file.
        logger.exception(
            "Task-created event delivery failed after task submission: %s",
            task.task_id,
        )

    return True
