"""SQLAlchemy 数据模型"""

import enum
import uuid
from datetime import datetime

from sqlalchemy import (
    JSON,
    BigInteger,
    Boolean,
    Column,
    DateTime,
    Float,
    ForeignKey,
    Index,
    Integer,
    String,
    Text,
    UniqueConstraint,
)
from sqlalchemy.dialects.mysql import MEDIUMTEXT
from sqlalchemy.orm import DeclarativeBase, relationship


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


class User(Base):
    """用户表"""

    __tablename__ = "users"

    user_id = Column(String(36), primary_key=True, default=lambda: str(uuid.uuid4()))
    username = Column(String(50), unique=True, nullable=False, index=True)
    password = Column(String(500), nullable=True)
    email = Column(String(100), unique=True, nullable=True)
    api_key = Column(String(100), unique=True, nullable=False, index=True)
    role = Column(String(20), default="user")
    is_active = Column(Boolean, default=True)
    created_at = Column(DateTime, default=datetime.now)
    updated_at = Column(DateTime, default=datetime.now, onupdate=datetime.now)
    last_login = Column(DateTime, nullable=True)

    documents = relationship("Document", back_populates="user", lazy="dynamic")
    tasks = relationship("Task", back_populates="user", lazy="dynamic")

    def __repr__(self):
        return f"<User(user_id={self.user_id}, username={self.username})>"


class ApiKey(Base):
    """API Key 表"""

    __tablename__ = "api_keys"

    id = Column(Integer, primary_key=True, autoincrement=True)
    api_key = Column(String(100), unique=True, nullable=False, index=True)
    user_id = Column(String(36), ForeignKey("users.user_id"), nullable=True)
    key_name = Column(String(50), nullable=True)
    is_active = Column(Boolean, default=True)
    rate_limit = Column(Integer, default=100)
    created_at = Column(DateTime, default=datetime.now)
    expires_at = Column(DateTime, nullable=True)
    last_used_at = Column(DateTime, nullable=True)
    usage_count = Column(BigInteger, default=0)

    def __repr__(self):
        return f"<ApiKey(api_key={self.api_key[:8]}..., user_id={self.user_id})>"


class Conversation(Base):
    """会话历史表"""

    __tablename__ = "conversations"

    id = Column(String(36), primary_key=True, default=lambda: str(uuid.uuid4()))
    user_id = Column(String(36), ForeignKey("users.user_id"), nullable=False, index=True)
    thread_id = Column(String(100), nullable=False, index=True)
    title = Column(String(255), default="新对话")
    metadata_ = Column("metadata", JSON, nullable=True)
    is_deleted = Column(Boolean, default=False)
    created_at = Column(DateTime, default=datetime.now, index=True)
    updated_at = Column(DateTime, default=datetime.now, onupdate=datetime.now)

    user = relationship("User", backref="conversations")

    __table_args__ = (
        UniqueConstraint("user_id", "thread_id", name="uq_conversation_user_thread"),
    )

    def __repr__(self):
        return f"<Conversation(id={self.id}, thread_id={self.thread_id})>"


class Document(Base):
    """文档信息表"""

    __tablename__ = "documents"

    doc_id = Column(String(36), primary_key=True, default=lambda: str(uuid.uuid4()))
    user_id = Column(String(36), ForeignKey("users.user_id"), nullable=True, index=True)
    filename = Column(String(255), nullable=False)
    document_type = Column(String(20), nullable=False)
    file_size = Column(BigInteger, nullable=False)
    file_path = Column(String(500), nullable=True)
    file_hash = Column(String(64), nullable=True, index=True)
    mime_type = Column(String(100), nullable=True)
    upload_time = Column(DateTime, default=datetime.now, index=True)
    status = Column(String(20), default="pending", index=True)
    page_count = Column(Integer, default=0)
    error_message = Column(Text, nullable=True)
    created_at = Column(DateTime, default=datetime.now)
    updated_at = Column(DateTime, default=datetime.now, onupdate=datetime.now)

    user = relationship("User", back_populates="documents")
    tasks = relationship(
        "Task", back_populates="document", cascade="all, delete-orphan"
    )
    extraction_results = relationship(
        "ExtractionResult", back_populates="document", cascade="all, delete-orphan"
    )

    __table_args__ = (
        Index("idx_doc_user_status", "user_id", "status"),
        Index("idx_doc_upload_time", "upload_time"),
    )

    def __repr__(self):
        return f"<Document(doc_id={self.doc_id}, filename={self.filename})>"


class Task(Base):
    """任务信息表"""

    __tablename__ = "tasks"

    task_id = Column(String(36), primary_key=True, default=lambda: str(uuid.uuid4()))
    doc_id = Column(
        String(36), ForeignKey("documents.doc_id"), nullable=False, index=True
    )
    user_id = Column(String(36), ForeignKey("users.user_id"), nullable=True, index=True)
    batch_id = Column(String(36), nullable=True, index=True)
    status = Column(String(20), default="pending", index=True)
    progress = Column(Integer, default=0)
    priority = Column(Integer, default=0)
    options = Column(JSON, nullable=True)
    result = Column(JSON, nullable=True)
    output_path = Column(String(500), nullable=True)
    error = Column(Text, nullable=True)
    error_detail = Column(MEDIUMTEXT, nullable=True)
    processing_time = Column(Float, nullable=True)
    retry_count = Column(Integer, default=0)
    max_retries = Column(Integer, default=3)
    created_at = Column(DateTime, default=datetime.now, index=True)
    updated_at = Column(DateTime, default=datetime.now, onupdate=datetime.now)
    started_at = Column(DateTime, nullable=True)
    completed_at = Column(DateTime, nullable=True)

    document = relationship("Document", back_populates="tasks")
    user = relationship("User", back_populates="tasks")

    __table_args__ = (
        Index("idx_task_doc_status", "doc_id", "status"),
        Index("idx_task_user_status", "user_id", "status"),
        Index("idx_task_created", "created_at"),
    )

    def __repr__(self):
        return f"<Task(task_id={self.task_id}, status={self.status})>"


class ExtractionResult(Base):
    """抽取结果表"""

    __tablename__ = "extraction_results"

    extraction_id = Column(
        String(36), primary_key=True, default=lambda: str(uuid.uuid4())
    )
    task_id = Column(String(36), ForeignKey("tasks.task_id"), nullable=True, index=True)
    doc_id = Column(
        String(36), ForeignKey("documents.doc_id"), nullable=False, index=True
    )
    user_id = Column(String(36), ForeignKey("users.user_id"), nullable=True, index=True)

    document_type = Column(String(20), nullable=True)
    page_count = Column(Integer, default=0)

    text_blocks_count = Column(Integer, default=0)
    tables_count = Column(Integer, default=0)
    formulas_count = Column(Integer, default=0)
    images_count = Column(Integer, default=0)

    result_data = Column(MEDIUMTEXT, nullable=True)
    text_content = Column(MEDIUMTEXT, nullable=True)
    markdown_content = Column(MEDIUMTEXT, nullable=True)

    processing_time = Column(Float, nullable=True)
    confidence_score = Column(Float, nullable=True)

    created_at = Column(DateTime, default=datetime.now, index=True)

    document = relationship("Document", back_populates="extraction_results")

    __table_args__ = (
        Index("idx_extraction_doc", "doc_id"),
        Index("idx_extraction_task", "task_id"),
        Index("idx_extraction_created", "created_at"),
    )

    def __repr__(self):
        return f"<ExtractionResult(extraction_id={self.extraction_id}, doc_id={self.doc_id})>"


class TaskLog(Base):
    """任务日志表"""

    __tablename__ = "task_logs"

    id = Column(BigInteger, primary_key=True, autoincrement=True)
    task_id = Column(
        String(36), ForeignKey("tasks.task_id"), nullable=False, index=True
    )
    log_level = Column(String(10), default="INFO")
    message = Column(Text, nullable=False)
    extra_data = Column(JSON, nullable=True)
    created_at = Column(DateTime, default=datetime.now, index=True)

    __table_args__ = (Index("idx_log_task_time", "task_id", "created_at"),)

    def __repr__(self):
        return f"<TaskLog(task_id={self.task_id}, level={self.log_level})>"


class FileInfo(Base):
    """文件信息表 - 用于文档处理器"""

    __tablename__ = "file_info"

    task_id = Column(String(64), primary_key=True, nullable=False)
    file_name = Column(String(255), nullable=False, unique=True)
    parse_status = Column(Integer, default=0, nullable=False, index=True)
    storage_status = Column(Integer, default=0, nullable=False, index=True)
    md_file_path = Column(String(500), nullable=False, default="")

    __table_args__ = (
        Index("idx_file_info_parse_storage", "parse_status", "storage_status"),
        Index("idx_file_info_task_id", "task_id"),
    )

    def __repr__(self):
        return f"<FileInfo(task_id={self.task_id}, file_name={self.file_name}, parse_status={self.parse_status}, storage_status={self.storage_status})>"


class UserFile(Base):
    """用户文件表"""

    __tablename__ = "user_files"

    id = Column(BigInteger, primary_key=True, autoincrement=True)
    file_id = Column(String(64), nullable=False, unique=True, index=True, default=lambda: f"uf_{uuid.uuid4().hex}")
    owner_user_id = Column(String(36), ForeignKey("users.user_id"), nullable=False, index=True)
    thread_id = Column(String(100), nullable=True)
    source_type = Column(String(20), default="local")  # 'local', 'minio', 'mixed'
    storage_bucket = Column(String(128), nullable=True)
    storage_object = Column(String(512), nullable=True)
    local_abs_path = Column(String(1024), nullable=True)
    display_name = Column(String(255), nullable=False)
    mime_type = Column(String(128), nullable=True)
    size_bytes = Column(BigInteger, nullable=True)
    sha256 = Column(String(64), nullable=True)
    status = Column(String(20), default="active")  # 'active', 'deleted', 'quarantined'
    metadata_ = Column("metadata", JSON, nullable=True)
    created_at = Column(DateTime, default=datetime.now)
    updated_at = Column(DateTime, default=datetime.now, onupdate=datetime.now)

    user = relationship("User", backref="user_files")

    def __repr__(self):
        return f"<UserFile(file_id={self.file_id}, display_name={self.display_name})>"


class SessionFile(Base):
    """会话文件关联表"""

    __tablename__ = "session_files"

    id = Column(String(36), primary_key=True, default=lambda: str(uuid.uuid4()))
    session_id = Column(String(100), nullable=False, index=True)  # thread_id
    user_id = Column(String(36), ForeignKey("users.user_id"), nullable=False, index=True)
    file_id = Column(
        String(64),
        ForeignKey("user_files.file_id", ondelete="CASCADE"),
        nullable=False,
        index=True,
    )
    attached_from = Column(String(50), default="my_files")  # "upload" / "my_files" / "generated_auto"
    created_at = Column(DateTime, default=datetime.now)

    user = relationship("User", backref="session_files")
    user_file = relationship("UserFile", backref="session_associations", foreign_keys=[file_id], primaryjoin="SessionFile.file_id == UserFile.file_id")

    def __repr__(self):
        return f"<SessionFile(session_id={self.session_id}, file_id={self.file_id})>"


from backend.db.models.knowledge_base import (
    KnowledgeBase,
    KnowledgeBaseDirectory,
    KnowledgeBaseDocument,
    KnowledgeBaseStatus,
    PendingFile,
    PendingFileStatus,
)
from backend.db.models.llm_model import LlmModel, LlmModelAccess
from backend.db.models.agent import Agent, AgentKnowledgeBase
from backend.db.models.dataset import ExperimentDataset
from backend.db.models.groundwater_project import (
    GroundwaterProject,
    GroundwaterProjectUser,
)

__all__ = [
    "Base",
    "DocumentType",
    "TaskStatus",
    "User",
    "ApiKey",
    "Document",
    "Task",
    "ExtractionResult",
    "TaskLog",
    "FileInfo",
    "KnowledgeBase",
    "KnowledgeBaseDirectory",
    "KnowledgeBaseDocument",
    "KnowledgeBaseStatus",
    "PendingFile",
    "PendingFileStatus",
    "Conversation",
    "LlmModel",
    "LlmModelAccess",
    "UserFile",
    "SessionFile",
    "Agent",
    "AgentKnowledgeBase",
    "ExperimentDataset",
    "GroundwaterProject",
    "GroundwaterProjectUser",
]
