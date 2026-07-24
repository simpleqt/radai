from uuid import NAMESPACE_URL, uuid5

from backend.config.loggers import get_logger
from backend.config.settings import settings
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
        event_id: str | None = None,
        event_timestamp: int | None = None,
    ) -> None:
        """
        发布成功结果到 Kafka

        Args:
            task_id: 任务ID
            document_id: 文档ID
            user_id: 用户ID
            output: 处理结果输出
        """
        extracted_text = str(
            output.get("extracted_text")
            or output.get("text_content")
            or output.get("markdown")
            or ""
        )
        extracted_text = extracted_text[: max(0, settings.kafka_result_text_max_chars)]
        max_items = max(0, settings.kafka_result_max_items)
        tables = (output.get("tables") or [])[:max_items]
        formulas = (output.get("formulas") or [])[:max_items]
        images = (output.get("images") or [])[:max_items]
        event_seed = event_id or f"{task_id}:{document_id}"

        def item_id(kind: str, index: int) -> str:
            return str(uuid5(NAMESPACE_URL, f"{event_seed}:{kind}:{index}"))

        def as_int(value, default: int = 0) -> int:
            try:
                return int(value)
            except (TypeError, ValueError):
                return default

        def as_float(value, default: float) -> float:
            try:
                return float(value)
            except (TypeError, ValueError):
                return default

        def table_data(table: dict) -> list[list[str]]:
            raw_data = table.get("data", table.get("rows", []))
            if not isinstance(raw_data, list):
                raw_data = [raw_data]
            return [
                [str(cell) for cell in row]
                if isinstance(row, (list, tuple))
                else [str(row)]
                for row in raw_data
            ]

        def bounding_box(formula) -> list[float] | None:
            if not isinstance(formula, dict):
                return None
            raw_box = formula.get("bounding_box")
            if not isinstance(raw_box, (list, tuple)):
                return None
            try:
                return [float(value) for value in raw_box]
            except (TypeError, ValueError):
                return None

        # 构建提取结果对象
        extraction_result = ExtractionResult(
            extracted_text=extracted_text,
            tables=[
                TableResult(
                    table_id=item_id("table", index),
                    data=table_data(table),
                    page=as_int(table.get("page")),
                    metadata={
                        "source": "table_extraction",
                        **(
                            {"caption": str(table["caption"])}
                            if table.get("caption") is not None
                            else {}
                        ),
                    },
                )
                for index, table in enumerate(tables)
                if isinstance(table, dict)
            ],
            formulas=[
                FormulaResult(
                    formula_id=item_id("formula", index),
                    latex=(
                        str(formula.get("latex", formula.get("content", "")))
                        if isinstance(formula, dict)
                        else str(formula)
                    ),
                    page=(
                        as_int(formula.get("page"))
                        if isinstance(formula, dict)
                        else 0
                    ),
                    formula_type=(
                        str(formula.get("formula_type", "unknown"))
                        if isinstance(formula, dict)
                        else "unknown"
                    ),
                    bounding_box=bounding_box(formula),
                )
                for index, formula in enumerate(formulas)
            ],
            images=[
                ImageResult(
                    image_id=item_id("image", index),
                    page=as_int(img.get("page")),
                    image_path=str(img.get("path") or ""),
                    caption=(
                        str(img["caption"])
                        if img.get("caption") is not None
                        else None
                    ),
                    metadata={"source": "image_extraction"},
                )
                for index, img in enumerate(images)
                if isinstance(img, dict)
            ],
            metadata=ExtractionMetadata(
                page_count=as_int(output.get("page_count")),
                language=str(output.get("language") or "unknown"),
                quality_score=as_float(output.get("quality_score"), 0.9),
                processing_time_ms=as_int(output.get("processing_time_ms")),
            ),
        )

        # 构建结果消息
        result_msg = ResultMessage(
            header=MessageHeader(
                **({"message_id": event_id} if event_id else {}),
                **({"timestamp": event_timestamp} if event_timestamp is not None else {}),
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
