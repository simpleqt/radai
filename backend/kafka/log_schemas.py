"""
日志 Kafka 消息 Schema
结构化日志条目，发送至 application-logs topic
"""

import uuid
from datetime import datetime
from enum import Enum
from typing import Any

from pydantic import BaseModel, Field


class LogLevel(str, Enum):
    DEBUG = "DEBUG"
    INFO = "INFO"
    WARNING = "WARNING"
    ERROR = "ERROR"
    CRITICAL = "CRITICAL"


class LogEntrySchema(BaseModel):
    """发送到 Kafka 的结构化日志条目"""

    log_id: str = Field(default_factory=lambda: str(uuid.uuid4()))

    # UTC ISO8601 时间 + 毫秒时间戳（便于消费端排序）
    timestamp: str = Field(
        default_factory=lambda: datetime.utcnow().isoformat() + "Z"
    )
    timestamp_ms: int = Field(
        default_factory=lambda: int(datetime.utcnow().timestamp() * 1000)
    )

    # 日志内容
    level: LogLevel
    level_no: int           # logging.levelno，便于消费端数值过滤
    message: str
    logger_name: str        # record.name，如 backend.api.routers.rag

    # 来源定位
    service_name: str       # main-api / guest-api / worker / ingest-worker
    host: str
    process_id: int
    thread_name: str
    filename: str
    lineno: int
    func_name: str

    # 链路追踪（由 LoggerAdapter extra 注入，可选）
    trace_id: str | None = None
    user_id: str | None = None
    request_id: str | None = None

    # 异常堆栈（可选）
    exc_text: str | None = None

    # 业务扩展字段（如 task_id、document_id、kb_id 等）
    extra: dict[str, Any] = Field(default_factory=dict)
