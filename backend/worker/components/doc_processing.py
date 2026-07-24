import asyncio
from typing import Any

from backend.config.loggers import get_logger
from backend.db import async_session_context
from backend.db.crud import extraction_crud
from backend.kafka.schemas import TaskMessage

logger = get_logger(__name__)


class DocumentProcessingUseCase:
    """文档处理用例

    前端已完成文档处理，后端只需从MySQL获取处理好的结果
    """

    async def execute(self, task: TaskMessage) -> dict[str, Any]:
        """
        执行文档处理

        从数据库获取前端已处理好的文档结果

        Args:
            task: 任务消息

        Returns:
            处理结果字典
        """
        task_id = task.body.task_id
        document_id = task.body.document_id
        file_type = task.body.file_type

        logger.info(f"获取文档处理结果: task_id={task_id}, doc_id={document_id}")

        # 从数据库获取已处理的提取结果
        async with async_session_context() as session:
            extraction_result = await asyncio.wait_for(
                extraction_crud.get_by_doc_id(session, document_id),
                timeout=30.0,
            )

            if not extraction_result:
                raise Exception(f"未找到文档处理结果: document_id={document_id}")

            # 解析 result_data
            import json
            result_data = {}
            if extraction_result.result_data:
                try:
                    result_data = json.loads(extraction_result.result_data)
                except json.JSONDecodeError:
                    logger.warning(f"解析 result_data 失败: {extraction_result.result_data[:100]}")

            # 构建输出结果
            output = {
                "success": True,
                "markdown": result_data.get("markdown", extraction_result.text_content or ""),
                "text_content": extraction_result.text_content or "",
                "document_type": extraction_result.document_type or file_type,
                "pages": [],
                "text_blocks": [],
                "tables": [],
                "formulas": [],
                "images": [],
                "quality_score": extraction_result.confidence_score or 0.9,
                "processing_time_ms": (extraction_result.processing_time or 0) * 1000,
                "language": result_data.get("language", "unknown"),
                "messages": result_data.get("messages", []),
                "md_object_key": result_data.get("md_object_key"),
                "md_download_url": result_data.get("md_download_url"),
            }

            logger.info(f"成功获取文档处理结果: task_id={task_id}, doc_id={document_id}")

            return output
