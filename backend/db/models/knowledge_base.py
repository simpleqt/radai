"""知识库模型定义"""

import enum
import uuid
from datetime import datetime

from sqlalchemy import (
    BigInteger,
    Boolean,
    Column,
    Date,
    DateTime,
    Enum,
    ForeignKey,
    Index,
    Integer,
    SmallInteger,
    String,
    Text,
)
from sqlalchemy.orm import relationship

from backend.db.models import Base


class KnowledgeBaseStatus(enum.Enum):
    """知识库状态"""

    CREATING = "creating"
    ACTIVE = "active"
    INACTIVE = "inactive"
    DELETING = "deleting"


class KnowledgeBase(Base):
    """知识库管理表"""

    __tablename__ = "knowledge_bases"

    kb_id = Column(
        String(36), primary_key=True, default=lambda: str(uuid.uuid4())
    )
    kb_code = Column(
        String(50),
        unique=True,
        nullable=False,
        index=True,
        comment="知识库编码，用于存储空间命名",
    )
    kb_name = Column(String(100), nullable=False, comment="知识库名称")
    subject = Column(String(50), nullable=True, comment="学科分类")
    description = Column(Text, nullable=True, comment="描述")
    owner_id = Column(String(36), nullable=True, index=True, comment="所有者ID")

    nebula_space = Column(String(50), nullable=True, comment="NebulaGraph Space名称")
    milvus_collection_prefix = Column(
        String(50), nullable=True, comment="Milvus Collection前缀"
    )
    redis_key_prefix = Column(String(50), nullable=True, comment="Redis Key前缀")

    doc_count = Column(Integer, default=0, comment="文档数量")
    entity_count = Column(Integer, default=0, comment="实体数量")
    relation_count = Column(Integer, default=0, comment="关系数量")
    chunk_count = Column(Integer, default=0, comment="块数量")

    status = Column(
        Enum(KnowledgeBaseStatus, values_callable=lambda obj: [e.value for e in obj]),
        default=KnowledgeBaseStatus.CREATING,
        index=True,
        comment="知识库状态",
    )
    is_default = Column(Boolean, default=False, comment="是否为默认知识库")

    created_at = Column(DateTime, default=datetime.now, index=True)
    updated_at = Column(DateTime, default=datetime.now, onupdate=datetime.now)

    documents = relationship(
        "KnowledgeBaseDocument",
        back_populates="knowledge_base",
        cascade="all, delete-orphan",
    )
    directories = relationship(
        "KnowledgeBaseDirectory",
        back_populates="knowledge_base",
        cascade="all, delete-orphan",
    )

    __table_args__ = (
        Index("idx_kb_code", "kb_code"),
        Index("idx_kb_status", "status"),
        Index("idx_kb_owner", "owner_id"),
        Index("idx_kb_created", "created_at"),
        {"comment": "知识库管理表"},
    )

    def __repr__(self):
        return f"<KnowledgeBase(kb_id={self.kb_id}, kb_code={self.kb_code}, kb_name={self.kb_name})>"


class KnowledgeBaseDocument(Base):
    """知识库文档关联表"""

    __tablename__ = "kb_documents"

    id = Column(BigInteger, primary_key=True, autoincrement=True)
    kb_id = Column(
        String(36),
        ForeignKey("knowledge_bases.kb_id"),
        nullable=False,
        index=True,
        comment="知识库ID",
    )
    doc_id = Column(String(64), nullable=False, comment="文档ID (LightRAG doc_id)")
    file_name = Column(String(255), nullable=False)
    file_path = Column(String(500), nullable=True)
    file_size = Column(BigInteger, nullable=True)
    document_date = Column(Date, nullable=True, comment="文档业务日期(批次级)")
    category_path = Column(String(500), nullable=True, comment="自定义分类路径，如 '期刊名/年份' 或 '参数类/子类'")

    parse_status = Column(
        SmallInteger,
        default=0,
        index=True,
        comment="解析状态: 0-待处理, 1-成功, 2-失败",
    )
    storage_status = Column(
        SmallInteger,
        default=0,
        index=True,
        comment="入库状态: 0-待入库, 1-已入库, 2-失败",
    )
    error_message = Column(Text, nullable=True)

    chunk_count = Column(Integer, default=0)
    entity_count = Column(Integer, default=0)
    relation_count = Column(Integer, default=0)

    created_at = Column(DateTime, default=datetime.now)
    updated_at = Column(DateTime, default=datetime.now, onupdate=datetime.now)

    knowledge_base = relationship("KnowledgeBase", back_populates="documents")

    __table_args__ = (
        Index("uk_kb_doc", "kb_id", "doc_id", unique=True),
        Index("idx_kb_doc_parse", "parse_status"),
        Index("idx_kb_doc_storage", "storage_status"),
        Index("idx_kb_doc_date", "kb_id", "document_date"),
        Index("idx_kb_doc_category", "kb_id", "category_path"),
        {"comment": "知识库文档关联表"},
    )

    def __repr__(self):
        return f"<KnowledgeBaseDocument(kb_id={self.kb_id}, doc_id={self.doc_id}, file_name={self.file_name})>"


class KnowledgeBaseDirectory(Base):
    """知识库目录表 - 支持多层目录管理与元数据存储"""

    __tablename__ = "kb_directories"

    id = Column(BigInteger, primary_key=True, autoincrement=True)
    kb_id = Column(
        String(36),
        ForeignKey("knowledge_bases.kb_id"),
        nullable=False,
        index=True,
        comment="知识库ID",
    )
    
    category_path = Column(String(500), nullable=False, comment="目录完整路径，如 '期刊/2024'")
    
    parent_path = Column(String(500), nullable=True, comment="父目录路径，根目录为 NULL")
    depth = Column(SmallInteger, default=1, comment="目录深度，根目录=1")
    name = Column(String(100), nullable=False, comment="目录名称（最后一段）")
    
    description = Column(Text, nullable=True, comment="目录描述")
    sort_order = Column(Integer, default=0, comment="排序序号")
    icon = Column(String(50), nullable=True, comment="目录图标")
    color = Column(String(20), nullable=True, comment="目录颜色标识")
    
    doc_count = Column(Integer, default=0, comment="直接文档数")
    total_doc_count = Column(Integer, default=0, comment="包含子目录的总文档数")
    
    created_by = Column(String(36), nullable=True, comment="创建用户ID")
    is_system = Column(Boolean, default=False, comment="是否系统目录（不可删除）")
    
    created_at = Column(DateTime, default=datetime.now)
    updated_at = Column(DateTime, default=datetime.now, onupdate=datetime.now)

    knowledge_base = relationship("KnowledgeBase", back_populates="directories")

    __table_args__ = (
        Index("uk_kb_directory", "kb_id", "category_path", unique=True),
        Index("idx_kb_dir_parent", "kb_id", "parent_path"),
        Index("idx_kb_dir_depth", "kb_id", "depth"),
        {"comment": "知识库目录表"},
    )

    def __repr__(self):
        return f"<KnowledgeBaseDirectory(kb_id={self.kb_id}, category_path={self.category_path}, name={self.name})>"


class PendingFileStatus(enum.Enum):
    """待入库文件状态"""

    PENDING = 0
    PROCESSING = 1
    COMPLETED = 2
    FAILED = 3


class PendingFile(Base):
    """待入库文件缓存表"""

    __tablename__ = "kb_pending_files"

    id = Column(BigInteger, primary_key=True, autoincrement=True)
    upload_id = Column(String(64), nullable=False, index=True, comment="上传批次ID")
    kb_id = Column(
        String(36),
        ForeignKey("knowledge_bases.kb_id"),
        nullable=False,
        index=True,
        comment="目标知识库ID",
    )
    file_id = Column(String(64), unique=True, nullable=False, comment="文件唯一标识")
    file_name = Column(String(255), nullable=False, comment="原始文件名")
    file_size = Column(BigInteger, nullable=False, default=0, comment="文件大小(字节)")
    file_type = Column(String(50), nullable=True, comment="文件类型(MIME)")
    minio_path = Column(String(500), nullable=False, comment="MinIO存储路径")
    document_date = Column(Date, nullable=True, comment="文档业务日期(批次级)")
    category_path = Column(String(500), nullable=True, comment="自定义分类路径")

    status = Column(
        SmallInteger,
        default=0,
        index=True,
        comment="状态: 0-待入库, 1-入库中, 2-已入库, 3-失败",
    )
    error_message = Column(Text, nullable=True, comment="错误信息")
    retry_count = Column(Integer, default=0, comment="重试次数")

    created_by = Column(String(36), nullable=True, comment="上传用户ID")
    created_at = Column(DateTime, default=datetime.now, index=True)
    updated_at = Column(DateTime, default=datetime.now, onupdate=datetime.now)

    knowledge_base = relationship("KnowledgeBase")

    __table_args__ = (
        Index("idx_pending_kb", "kb_id"),
        Index("idx_pending_upload", "upload_id"),
        Index("idx_pending_status", "status"),
        Index("idx_pending_doc_date", "document_date"),
        Index("idx_pending_created", "created_at"),
        {"comment": "待入库文件缓存表"},
    )

    def __repr__(self):
        return f"<PendingFile(file_id={self.file_id}, file_name={self.file_name}, status={self.status})>"
