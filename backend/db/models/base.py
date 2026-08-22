"""Declarative base and shared enums for all ORM models."""

import enum

from sqlalchemy.orm import DeclarativeBase


class Base(DeclarativeBase):
    pass


class DocumentType(enum.Enum):
    """文档类型"""

    PDF = "pdf"
    DOCX = "docx"
    HTML = "html"
    LATEX = "latex"
    MARKDOWN = "markdown"
    PPT = "ppt"
    IMAGE = "image"


class TaskStatus(enum.Enum):
    """任务状态"""

    PENDING = "pending"
    PROCESSING = "processing"
    COMPLETED = "completed"
    FAILED = "failed"
    CANCELLED = "cancelled"
