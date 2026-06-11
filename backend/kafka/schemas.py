"""
Kafka 消息 Schema 定义
遵循 KAFKA_INTEGRATION_PLAN.md 中的数据格式设计
"""

import uuid
from datetime import datetime
from enum import Enum
from typing import Any

from pydantic import BaseModel, Field


class MessageType(str, Enum):
    """消息类型枚举"""

    TASK_SUBMITTED = "task_submitted"
    TASK_STARTED = "task_started"
    TASK_COMPLETED = "task_completed"
    TASK_FAILED = "task_failed"
    TASK_CANCELLED = "task_cancelled"
    TASK_RETRY = "task_retry"
    STATUS_UPDATE = "status_update"
    PROGRESS_UPDATE = "progress_update"
    RESULT = "result"
    EVENT = "event"


class TaskStatusEnum(str, Enum):
    """任务状态枚举"""

    PENDING = "pending"
    PROCESSING = "processing"
    COMPLETED = "completed"
    FAILED = "failed"
    CANCELLED = "cancelled"


class TaskPriority(str, Enum):
    """任务优先级枚举"""

    LOW = "low"
    NORMAL = "normal"
    HIGH = "high"
    URGENT = "urgent"


class RetryReason(str, Enum):
    """重试原因枚举"""

    TRANSIENT_FAILURE = "transient_failure"
    RESOURCE_BUSY = "resource_busy"
    RATE_LIMITED = "rate_limited"
    TIMEOUT = "timeout"


class ErrorCode(str, Enum):
    """错误代码枚举"""

    OCR_SERVICE_ERROR = "OCR_SERVICE_ERROR"
    VLM_SERVICE_ERROR = "VLM_SERVICE_ERROR"
    PARSE_ERROR = "PARSE_ERROR"
    VALIDATION_ERROR = "VALIDATION_ERROR"
    STORAGE_ERROR = "STORAGE_ERROR"
    DATABASE_ERROR = "DATABASE_ERROR"
    TIMEOUT = "TIMEOUT"
    INVALID_INPUT = "INVALID_INPUT"
    PERMISSION_DENIED = "PERMISSION_DENIED"
    RESOURCE_NOT_FOUND = "RESOURCE_NOT_FOUND"
    INTERNAL_ERROR = "INTERNAL_ERROR"
    UNKNOWN = "UNKNOWN"


class MessageHeader(BaseModel):
    """消息通用头部"""

    message_id: str = Field(default_factory=lambda: str(uuid.uuid4()))
    correlation_id: str | None = None
    timestamp: int = Field(
        default_factory=lambda: int(datetime.now().timestamp() * 1000)
    )
    message_type: MessageType
    version: str = "1.0"
    source: str
    user_id: str


class ProcessingOptions(BaseModel):
    """处理选项"""

    ocr_enabled: bool = True
    table_extract: bool = True
    formula_extract: bool = True
    image_extract: bool = True
    vision_enhance: bool = True
    rag_enabled: bool = False
    output_format: str = "markdown"


class TaskMessageBody(BaseModel):
    """任务消息体"""

    task_id: str
    document_id: str
    file_path: str
    file_name: str
    file_size: int
    file_type: str
    processing_options: ProcessingOptions = Field(default_factory=ProcessingOptions)
    priority: TaskPriority = TaskPriority.NORMAL
    retry_count: int = 0
    max_retries: int = 3
    created_at: str = Field(default_factory=lambda: datetime.now().isoformat())
    deadline: str | None = None


class TaskMessage(BaseModel):
    """任务提交消息"""

    header: MessageHeader
    body: TaskMessageBody


class RetryMessageBody(BaseModel):
    """任务重试消息体"""

    task_id: str
    document_id: str
    retry_attempt: int
    previous_error: str
    reason: RetryReason
    scheduled_at: str | None = None
    original_task: dict[str, Any] | None = None


class RetryMessage(BaseModel):
    """任务重试消息"""

    header: MessageHeader
    body: RetryMessageBody


class StatusUpdateDetails(BaseModel):
    """状态更新详情"""

    current_step: str | None = None
    estimated_remaining_seconds: int | None = None


class StatusMessageBody(BaseModel):
    """状态消息体"""

    task_id: str
    document_id: str
    status: TaskStatusEnum
    stage: str | None = None
    progress: float = 0.0
    message: str = ""
    details: StatusUpdateDetails | None = None
    updated_at: str = Field(default_factory=lambda: datetime.now().isoformat())


class StatusMessage(BaseModel):
    """状态更新消息"""

    header: MessageHeader
    body: StatusMessageBody


class ProgressByStage(BaseModel):
    """分阶段进度"""

    classification: float = 0.0
    ocr: float = 0.0
    table_extraction: float = 0.0
    formula_extraction: float = 0.0
    enhancement: float = 0.0
    output: float = 0.0


class ProgressData(BaseModel):
    """进度数据"""

    overall: float = 0.0
    by_stage: ProgressByStage = Field(default_factory=ProgressByStage)


class ProgressMessageBody(BaseModel):
    """进度消息体"""

    task_id: str
    progress: ProgressData
    updated_at: str = Field(default_factory=lambda: datetime.now().isoformat())


class ProgressMessage(BaseModel):
    """进度更新消息"""

    header: MessageHeader
    body: ProgressMessageBody


class TableResult(BaseModel):
    """表格结果"""

    table_id: str
    page: int
    data: list[list[str]]
    metadata: dict[str, Any] = Field(default_factory=dict)


class FormulaResult(BaseModel):
    """公式结果"""

    formula_id: str
    page: int
    latex: str
    formula_type: str
    bounding_box: list[float] | None = None


class ImageResult(BaseModel):
    """图片结果"""

    image_id: str
    page: int
    image_path: str
    caption: str | None = None
    metadata: dict[str, Any] = Field(default_factory=dict)


class ExtractionMetadata(BaseModel):
    """抽取元数据"""

    page_count: int
    language: str
    quality_score: float
    processing_time_ms: int


class ExtractionResult(BaseModel):
    """抽取结果"""

    extracted_text: str
    tables: list[TableResult] = Field(default_factory=list)
    formulas: list[FormulaResult] = Field(default_factory=list)
    images: list[ImageResult] = Field(default_factory=list)
    metadata: ExtractionMetadata


class ErrorDetails(BaseModel):
    """错误详情"""

    service: str | None = None
    timeout_ms: int | None = None
    resource: str | None = None


class ErrorInfo(BaseModel):
    """错误信息"""

    code: ErrorCode
    message: str
    details: ErrorDetails | None = None


class ResultMessageBody(BaseModel):
    """结果消息体"""

    task_id: str
    document_id: str
    status: TaskStatusEnum
    result: ExtractionResult | None = None
    error: ErrorInfo | None = None
    output_path: str | None = None
    processing_time_ms: int | None = None
    retryable: bool = False
    completed_at: str | None = None
    failed_at: str | None = None


class ResultMessage(BaseModel):
    """处理结果消息"""

    header: MessageHeader
    body: ResultMessageBody


class EventType(str, Enum):
    """事件类型枚举"""

    TASK_CREATED = "task_created"
    TASK_STARTED = "task_started"
    TASK_COMPLETED = "task_completed"
    TASK_FAILED = "task_failed"
    TASK_CANCELLED = "task_cancelled"
    TASK_RETRY = "task_retry"


class EventData(BaseModel):
    """事件数据"""

    description: str
    extra_info: dict[str, Any] = Field(default_factory=dict)


class EventMessageBody(BaseModel):
    """事件消息体"""

    event_type: EventType
    task_id: str
    document_id: str
    data: EventData
    occurred_at: str = Field(default_factory=lambda: datetime.now().isoformat())


class EventMessage(BaseModel):
    """事件消息"""

    header: MessageHeader
    body: EventMessageBody


class DeadLetterMessage(BaseModel):
    """死信消息"""

    original_message: dict[str, Any]
    error_count: int
    last_error: str
    first_failure_at: str
    last_failure_at: str
    topic: str
    partition: int
    offset: int
