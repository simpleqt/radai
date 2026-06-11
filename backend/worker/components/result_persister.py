from datetime import timedelta
import json

from backend.config.loggers import get_logger
from backend.config.settings import settings
from backend.db import async_session_context
from backend.db.crud import document_crud, extraction_crud, task_crud
from backend.kafka.schemas import TaskStatusEnum
from backend.storage import get_presigned_url, storage_manager, upload_bytes_to_minio

logger = get_logger(__name__)


class ResultPersister:
    """结果持久化器"""

    async def save_result(
        self,
        task_id: str,
        document_id: str,
        user_id: str,
        file_type: str,
        output: dict,
    ) -> str | None:
        """
        保存处理结果到数据库和文件

        Args:
            task_id: 任务ID
            document_id: 文档ID
            user_id: 用户ID
            file_type: 文件类型
            output: 处理结果输出

        Returns:
            本地文件路径（如果有）
        """
        markdown_content = output.get("markdown", "")
        output_path = None
        md_object_key = None
        md_download_url = None

        # 1. 保存到本地文件（如果配置启用）
        if settings.save_results_locally and markdown_content:
            try:
                file_id, output_path = await storage_manager.save_file(
                    markdown_content.encode("utf-8"),
                    f"{document_id}.md",
                    storage_type="results",
                )
                logger.info(f"结果已保存到本地文件: {output_path}")
            except Exception as e:
                logger.warning(f"保存结果到本地文件失败: {e}")
                output_path = None
        if markdown_content:
            try:
                object_name = f"users/{user_id}/results/{document_id}/{task_id}.md"
                # Use customized per-user bucket
                from backend.storage import get_user_minio_bucket_name, _get_minio_client
                from io import BytesIO
                
                client = _get_minio_client()
                bucket = get_user_minio_bucket_name()
                client.put_object(
                    bucket,
                    object_name,
                    BytesIO(markdown_content.encode("utf-8")),
                    length=len(markdown_content.encode("utf-8")),
                    content_type="text/markdown",
                )
                uploaded_key = object_name
                # Use custom uploaded_key locally above and below get_presigned_url internally uses settings.minio_bucket_name, but assuming get_presigned_url also requires bucket explicitly?
                # Actually wait, let's keep get_presigned_url but it probably lacks bucket param.
                # Just use normal presigned url generation:
                md_object_key = uploaded_key
                md_download_url = client.presigned_get_object(
                    bucket, uploaded_key, expires=timedelta(seconds=24 * 60 * 60)
                )
            except Exception as e:
                logger.warning(f"上传结果到 MinIO 失败: {e}")

        # 2. 保存到数据库
        async with async_session_context() as session:
            # 更新任务状态
            await task_crud.update_status(
                session,
                task_id,
                status=TaskStatusEnum.COMPLETED.value,
                output_path=output_path,
            )
            # 更新文档状态
            await document_crud.update_status(session, document_id, status="completed")

            # 提取结果数据
            tables = output.get("tables", [])
            formulas = output.get("formulas", [])
            images = output.get("images", [])

            # 创建提取结果记录
            await extraction_crud.create(
                session,
                doc_id=document_id,
                task_id=task_id,
                user_id=user_id,
                document_type=file_type,
                page_count=len(output.get("pages", [])),
                text_blocks_count=len(output.get("text_blocks", [])),
                tables_count=len(tables),
                formulas_count=len(formulas),
                images_count=len(images),
                result_data=json.dumps(
                    {
                        "markdown": markdown_content,
                        "success": output.get("success", True),
                        "document_type": output.get("document_type", "unknown"),
                        "quality_score": output.get("quality_score", 0.0),
                        "retry_count": output.get("retry_count", 0),
                        "language": output.get("language", "unknown"),
                        "messages": output.get("messages", []),
                        "md_object_key": md_object_key,
                        "md_download_url": md_download_url,
                    },
                    ensure_ascii=False,
                ),
                text_content=markdown_content,
                processing_time=output.get("processing_time_ms", 0) / 1000.0,
                confidence_score=output.get("quality_score", 0.9),
            )

            await session.commit()

        return output_path

    async def update_task_status(self, task_id: str, status: str):
        """更新任务状态"""
        async with async_session_context() as session:
            await task_crud.update_status(session, task_id, status=status)
            await session.commit()
