"""任务管理路由"""

import asyncio

from fastapi import APIRouter, Depends, HTTPException, Query
from sqlalchemy.ext.asyncio import AsyncSession

from backend.api.dependencies.auth import ApiKeyAuth, get_db_session, verify_api_key
from backend.api.routers.document_processing.models import (
    TaskResultResponse,
    TaskStatus,
    TaskStatusResponse,
)
from backend.config.loggers import get_logger
from backend.db.crud.document_crud import document_crud
from backend.db.crud.extraction_crud import extraction_crud
from backend.db.crud.task_crud import task_crud
from backend.storage import (
    get_presigned_url,
    get_user_minio_bucket_name,
    upload_bytes_to_minio,
)

router = APIRouter()
logger = get_logger(__name__)


@router.get("/tasks/{task_id}/status", response_model=TaskStatusResponse)
async def get_task_status(
    task_id: str,
    auth: ApiKeyAuth = Depends(verify_api_key),
    session: AsyncSession = Depends(get_db_session),
):
    """获取任务状态"""
    task = await task_crud.get_by_id(session, task_id)

    if not task:
        raise HTTPException(status_code=404, detail=f"任务不存在: {task_id}")

    return TaskStatusResponse(
        task_id=task.task_id,
        doc_id=task.doc_id,
        status=TaskStatus(task.status),
        progress=task.progress or 0,
        message=task.error,
    )


@router.get("/tasks/{task_id}/result", response_model=TaskResultResponse)
async def get_task_result(
    task_id: str,
    md_url_only: bool = Query(False),
    auth: ApiKeyAuth = Depends(verify_api_key),
    session: AsyncSession = Depends(get_db_session),
):
    """获取任务结果"""
    task = await task_crud.get_by_id(session, task_id)

    if not task:
        raise HTTPException(status_code=404, detail=f"任务不存在: {task_id}")

    extraction_result = None
    if task.status == TaskStatus.COMPLETED.value:
        extraction_result = await extraction_crud.get_by_task_id(session, task_id)

    result_dict = None
    if extraction_result:
        import json

        result_data = (
            json.loads(extraction_result.result_data)
            if extraction_result.result_data
            else {}
        )

        if md_url_only:
            md_download_url = result_data.get("md_download_url")
            md_object_key = result_data.get("md_object_key")
            markdown = result_data.get("markdown") or ""

            user_id = task.user_id or auth.user_id
            user_bucket = get_user_minio_bucket_name()

            if not md_download_url and md_object_key:
                md_download_url = get_presigned_url(md_object_key, bucket_name=user_bucket)
            if not md_download_url and markdown:
                # Serialize the one-time object materialization across API workers.
                # Re-read after acquiring the row lock because another request may
                # have completed the upload while this request was waiting.
                try:
                    locked_result = await asyncio.wait_for(
                        extraction_crud.get_by_task_id_for_update(session, task_id),
                        timeout=10,
                    )
                except (asyncio.TimeoutError, TimeoutError) as exc:  # noqa: UP041
                    await session.rollback()
                    raise HTTPException(
                        status_code=503,
                        detail="结果正在生成，请稍后重试",
                    ) from exc
                if locked_result is not None:
                    extraction_result = locked_result
                    result_data = (
                        json.loads(locked_result.result_data)
                        if locked_result.result_data
                        else {}
                    )
                    md_download_url = result_data.get("md_download_url")
                    md_object_key = result_data.get("md_object_key")
                    markdown = result_data.get("markdown") or ""
                    if not md_download_url and md_object_key:
                        md_download_url = get_presigned_url(
                            md_object_key,
                            bucket_name=user_bucket,
                        )

            if not md_download_url and markdown:
                object_name = f"users/{user_id}/results/{task.doc_id}/{task.task_id}.md"
                uploaded_key = await upload_bytes_to_minio(
                    markdown.encode("utf-8"),
                    object_name,
                    content_type="text/markdown",
                    bucket_name=user_bucket,
                )
                if uploaded_key:
                    md_object_key = uploaded_key
                    md_download_url = get_presigned_url(uploaded_key, bucket_name=user_bucket)

            if md_object_key and md_object_key != result_data.get("md_object_key"):
                result_data["md_object_key"] = md_object_key
            if md_download_url and md_download_url != result_data.get(
                "md_download_url"
            ):
                result_data["md_download_url"] = md_download_url
                extraction_result.result_data = json.dumps(
                    result_data,
                    ensure_ascii=False,
                )
                session.add(extraction_result)
                await session.commit()

            result_dict = {"md_download_url": md_download_url or ""}
        elif "markdown" in result_data:
            result_dict = {
                "markdown": result_data.get("markdown", ""),
                "page_count": extraction_result.page_count,
                "processing_time": extraction_result.processing_time,
                "confidence_score": extraction_result.confidence_score,
                "success": result_data.get("success", True),
                "document_type": result_data.get("document_type", "unknown"),
                "quality_score": result_data.get("quality_score", 0.0),
                "retry_count": result_data.get("retry_count", 0),
                "language": result_data.get("language", "unknown"),
                "messages": result_data.get("messages", []),
                "md_object_key": result_data.get("md_object_key"),
                "md_download_url": result_data.get("md_download_url"),
            }
        else:
            result_dict = {
                "page_count": extraction_result.page_count,
                "text_blocks_count": extraction_result.text_blocks_count,
                "tables_count": extraction_result.tables_count,
                "formulas_count": extraction_result.formulas_count,
                "images_count": extraction_result.images_count,
                "processing_time": extraction_result.processing_time,
                "confidence_score": extraction_result.confidence_score,
                "extracted_text": extraction_result.text_content or "",
                "tables": result_data.get("tables", []),
                "formulas": result_data.get("formulas", []),
                "images": result_data.get("images", []),
                "toc": result_data.get("toc", []),
            }

    fallback_result = None
    if isinstance(task.result, dict):
        public_items = {
            key: value
            for key, value in task.result.items()
            if not str(key).startswith("_")
        }
        fallback_result = public_items or None

    return TaskResultResponse(
        task_id=task.task_id,
        doc_id=task.doc_id,
        status=TaskStatus(task.status),
        result=result_dict or fallback_result,
        output_path=task.output_path,
        error=task.error,
    )


@router.get("/tasks", response_model=list[TaskStatusResponse])
async def list_tasks(
    status: TaskStatus | None = Query(None),
    page: int = Query(1, ge=1),
    page_size: int = Query(20, ge=1, le=100),
    auth: ApiKeyAuth = Depends(verify_api_key),
    session: AsyncSession = Depends(get_db_session),
):
    """获取任务列表"""
    status_filter = status.value if status else None

    tasks = await task_crud.get_by_user(
        session=session,
        user_id=auth.user_id,
        status=status_filter,
        page=page,
        page_size=page_size,
    )

    return [
        TaskStatusResponse(
            task_id=task.task_id,
            doc_id=task.doc_id,
            status=TaskStatus(task.status),
            progress=task.progress or 0,
            message=task.error,
        )
        for task in tasks
    ]


@router.delete("/tasks/{task_id}")
async def delete_task(
    task_id: str,
    auth: ApiKeyAuth = Depends(verify_api_key),
    session: AsyncSession = Depends(get_db_session),
):
    """删除任务"""
    task = await task_crud.get_by_id(session, task_id)

    if not task:
        raise HTTPException(status_code=404, detail=f"任务不存在: {task_id}")

    doc = await document_crud.get_by_id(session, task.doc_id)
    if doc and doc.file_path:
        try:
            from pathlib import Path

            file_path = Path(doc.file_path)
            if await asyncio.to_thread(file_path.exists):
                await asyncio.to_thread(file_path.unlink)
            else:
                logger.warning(f"文件不存在，跳过删除: {doc.file_path}")
        except Exception as e:
            logger.warning(f"删除临时文件失败: {doc.file_path}, {e}")

    await task_crud.delete(session, task_id)
    await document_crud.delete(session, task.doc_id)

    return {"message": "任务已删除"}
