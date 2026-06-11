"""文档处理路由 - Pydantic 模型定义"""

from enum import Enum
from pathlib import Path

from fastapi import HTTPException
from pydantic import BaseModel, ConfigDict


class DocumentType(str, Enum):
    """文档类型"""

    PDF = "pdf"
    DOCX = "docx"
    HTML = "html"
    LATEX = "latex"
    MARKDOWN = "markdown"
    PPT = "ppt"
    IMAGE = "image"


def validate_file_type(filename: str, content_type: str | None = None) -> DocumentType:
    """验证文件类型"""
    ext = Path(filename).suffix.lower()

    type_mapping = {
        ".pdf": DocumentType.PDF,
        ".docx": DocumentType.DOCX,
        ".doc": DocumentType.DOCX,
        ".html": DocumentType.HTML,
        ".htm": DocumentType.HTML,
        ".tex": DocumentType.LATEX,
        ".md": DocumentType.MARKDOWN,
        ".ppt": DocumentType.PPT,
        ".pptx": DocumentType.PPT,
        ".png": DocumentType.IMAGE,
        ".jpg": DocumentType.IMAGE,
        ".jpeg": DocumentType.IMAGE,
        ".gif": DocumentType.IMAGE,
        ".bmp": DocumentType.IMAGE,
    }

    if ext in type_mapping:
        return type_mapping[ext]

    if content_type:
        if "pdf" in content_type:
            return DocumentType.PDF
        if "word" in content_type or "document" in content_type:
            return DocumentType.DOCX
        if "html" in content_type:
            return DocumentType.HTML
        if "image" in content_type:
            return DocumentType.IMAGE

    raise HTTPException(status_code=400, detail=f"不支持的文件类型: {filename}")


class TaskStatus(str, Enum):
    """任务状态"""

    PENDING = "pending"
    PROCESSING = "processing"
    COMPLETED = "completed"
    FAILED = "failed"


class DocumentInfo(BaseModel):
    """文档信息"""

    model_config = ConfigDict(from_attributes=True)

    doc_id: str
    filename: str
    document_type: DocumentType
    file_size: int
    upload_time: str
    status: str


class ProcessingOptions(BaseModel):
    """处理选项"""

    ocr_enabled: bool = True
    table_extract: bool = True
    formula_extract: bool = True
    image_extract: bool = True
    vision_enhance: bool = True
    rag_enabled: bool = False
    output_format: str = "markdown"


class TaskCreateResponse(BaseModel):
    """任务创建响应"""

    task_id: str
    doc_id: str
    status: TaskStatus
    message: str


class TaskStatusResponse(BaseModel):
    """任务状态响应"""

    task_id: str
    doc_id: str
    status: TaskStatus
    progress: int = 0
    message: str | None = None
    estimated_time: int | None = None


class TaskResultResponse(BaseModel):
    """任务结果响应"""

    task_id: str
    doc_id: str
    status: TaskStatus
    result: dict | None = None
    output_path: str | None = None
    error: str | None = None


class BatchTaskResponse(BaseModel):
    """批量任务响应"""

    batch_id: str
    total: int
    tasks: list[TaskCreateResponse]


class ChunkedUploadInitResponse(BaseModel):
    """分块上传初始化响应"""

    upload_id: str
    filename: str
    total_size: int
    total_chunks: int
    chunk_size: int
    expires_at: str


class ChunkedUploadStatusResponse(BaseModel):
    """分块上传状态响应"""

    upload_id: str
    filename: str
    total_size: int
    total_chunks: int
    uploaded_chunks: int
    progress: float
    is_complete: bool


class ChunkedMergeResponse(BaseModel):
    """分块合并响应"""

    success: bool
    message: str
    doc_id: str | None = None
    task_id: str | None = None
    file_path: str | None = None
