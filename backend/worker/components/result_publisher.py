import json
from uuid import uuid4

from backend.config.loggers import get_logger
from backend.kafka.client import get_result_producer
from backend.kafka.schemas import (
    ExtractionMetadata,
    ExtractionResult,
    FormulaResult,
    ImageResult,
    MessageHeader,
    MessageType,
    ResultMessage,
    ResultMessageBody,
    TableResult,
    TaskStatusEnum,
)

logger = get_logger(__name__)


class ResultPublisher:
    """结果发布器"""

    async def publish_success(
        self,
        task_id: str,
        document_id: str,
        user_id: str,
        output: dict,
    ) -> None:
        """
        发布成功结果到 Kafka

        Args:
            task_id: 任务ID
            document_id: 文档ID
            user_id: 用户ID
            output: 处理结果输出
        """
        extracted_text = output.get("extracted_text", "")
        tables = output.get("tables", [])
        formulas = output.get("formulas", [])
        images = output.get("images", [])

        # 构建提取结果对象
        extraction_result = ExtractionResult(
            extracted_text=extracted_text,
            tables=[
                TableResult(
                    table_id=str(uuid4()),
                    content=json.dumps(table, ensure_ascii=False),
                    page=table.get("page", 0),
                    caption=table.get("caption"),
                    metadata={"source": "table_extraction"},
                )
                for table in tables
            ],
            formulas=[
                FormulaResult(
                    formula_id=str(uuid4()),
                    content=formula,
                    page=formula.get("page", 0),
                    caption=formula.get("caption"),
                    metadata={"source": "formula_extraction"},
                )
                for formula in formulas
            ],
            images=[
                ImageResult(
                    image_id=str(uuid4()),
                    page=img.get("page", 0),
                    image_path=img.get("path", ""),
                    caption=img.get("caption"),
                    metadata={"source": "image_extraction"},
                )
                for img in images
            ],
            metadata=ExtractionMetadata(
                page_count=output.get("page_count", 0),
                language=output.get("language", "unknown"),
                quality_score=output.get("quality_score", 0.9),
                processing_time_ms=output.get("processing_time_ms", 0),
            ),
        )

        # 构建结果消息
        result_msg = ResultMessage(
            header=MessageHeader(
                message_type=MessageType.RESULT,
                source="worker",
                user_id=user_id,
                correlation_id=task_id,
            ),
            body=ResultMessageBody(
                task_id=task_id,
                document_id=document_id,
                status=TaskStatusEnum.COMPLETED,
                result=extraction_result,
            ),
        )

        # 发送消息
        producer = get_result_producer()
        await producer.send_result(result_msg)
