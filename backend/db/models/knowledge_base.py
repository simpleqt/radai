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
    JSON,
    SmallInteger,
    String,
    Text,
    UniqueConstraint,
)
from sqlalchemy.orm import relationship
from sqlalchemy.dialects.mysql import MEDIUMTEXT

from backend.db.models.base import Base
from backend.domain.knowledge_base.backend_policy import LEGACY_BACKEND

# MySQL 主键保持 BIGINT;SQLite 只有 INTEGER PRIMARY KEY 才走 rowid 自增,
# 与 skill.py 的既有模式一致,使内存 SQLite 契约测试可以建表插行。
_AUTOINCREMENT_ID = BigInteger().with_variant(Integer, "sqlite")


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

    ingestion_backend = Column(
        String(32),
        nullable=False,
        default=LEGACY_BACKEND,
        comment="入库后端: legacy_lightrag/self_ingestion",
    )
    retrieval_backend = Column(
        String(32),
        nullable=False,
        default=LEGACY_BACKEND,
        comment="查询后端: legacy_lightrag/self_ingestion/hybrid",
    )
    self_ingestion_enabled = Column(
        Boolean,
        nullable=False,
        default=False,
        comment="是否允许该知识库使用自研入库",
    )
    self_retriever_enabled = Column(
        Boolean,
        nullable=False,
        default=False,
        comment="是否允许该知识库使用自研查询",
    )

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
        Index("idx_kb_ingestion_backend", "ingestion_backend"),
        Index("idx_kb_retrieval_backend", "retrieval_backend"),
        {"comment": "知识库管理表"},
    )

    def __repr__(self):
        return f"<KnowledgeBase(kb_id={self.kb_id}, kb_code={self.kb_code}, kb_name={self.kb_name})>"


class KnowledgeBaseAcl(Base):
    """Explicit per-user/role grants for owner-private knowledge bases."""

    __tablename__ = "knowledge_base_acl"

    id = Column(_AUTOINCREMENT_ID, primary_key=True, autoincrement=True)
    kb_id = Column(String(36), nullable=False, index=True)
    subject_type = Column(String(16), nullable=False, default="user")
    subject_id = Column(String(64), nullable=False)
    permission = Column(String(16), nullable=False, default="read")
    effect = Column(String(8), nullable=False, default="allow")
    expires_at = Column(DateTime, nullable=True)
    created_by = Column(String(36), nullable=True)
    created_at = Column(DateTime, nullable=False, default=datetime.now)

    __table_args__ = (
        UniqueConstraint(
            "kb_id",
            "subject_type",
            "subject_id",
            "permission",
            name="uk_knowledge_base_acl",
        ),
        Index("idx_knowledge_base_acl_subject", "subject_type", "subject_id"),
        Index("idx_knowledge_base_acl_kb", "kb_id"),
        {"comment": "Explicit knowledge-base access grants"},
    )


class KnowledgeBaseDocument(Base):
    """知识库文档关联表"""

    __tablename__ = "kb_documents"

    id = Column(_AUTOINCREMENT_ID, primary_key=True, autoincrement=True)
    kb_id = Column(
        String(36),
        ForeignKey("knowledge_bases.kb_id"),
        nullable=False,
        index=True,
        comment="知识库ID",
    )
    doc_id = Column(String(64), nullable=False, comment="文档ID (兼容 LightRAG doc_id)")
    file_name = Column(String(255), nullable=False)
    file_path = Column(String(500), nullable=True)
    file_size = Column(BigInteger, nullable=True)
    source_hash = Column(String(64), nullable=True, index=True, comment="源文件 SHA-256")
    object_bucket = Column(String(128), nullable=True, comment="对象存储桶")
    object_key = Column(String(500), nullable=True, comment="对象存储 Key")
    object_storage_uri = Column(String(700), nullable=True, comment="对象存储 URI")
    mime_type = Column(String(100), nullable=True, comment="文件 MIME 类型")
    file_ext = Column(String(20), nullable=True, comment="文件扩展名")
    document_date = Column(Date, nullable=True, comment="文档业务日期(批次级)")
    category_path = Column(String(500), nullable=True, comment="自定义分类路径，如 '期刊名/年份' 或 '参数类/子类'")

    ingestion_backend = Column(
        String(32),
        nullable=False,
        default=LEGACY_BACKEND,
        comment="入库后端: legacy_lightrag/self_ingestion",
    )
    doc_status = Column(String(32), nullable=True, comment="自研入库文档状态")
    commit_version = Column(Integer, nullable=False, default=1, comment="文档提交版本")
    latest_job_id = Column(String(64), nullable=True, index=True, comment="最近一次入库任务ID")
    lineage_json = Column(JSON, nullable=True, comment="解析/切分/写入血缘")
    extra_metadata_json = Column(JSON, nullable=True, comment="扩展元数据")

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
    sort_order = Column(Integer, default=0, comment="同级展示排序（自研入库知识库）")

    created_at = Column(DateTime, default=datetime.now)
    updated_at = Column(DateTime, default=datetime.now, onupdate=datetime.now)
    uploaded_at = Column(DateTime, nullable=True, comment="上传完成时间")
    stored_at = Column(DateTime, nullable=True, comment="对象存储写入时间")
    ingest_started_at = Column(DateTime, nullable=True, comment="入库开始时间")
    ingest_finished_at = Column(DateTime, nullable=True, comment="入库完成时间")
    committed_at = Column(DateTime, nullable=True, comment="索引提交时间")
    visible_at = Column(DateTime, nullable=True, comment="查询可见时间")
    deleted_at = Column(DateTime, nullable=True, index=True, comment="软删除时间")
    archived_at = Column(DateTime, nullable=True, comment="归档时间")

    knowledge_base = relationship("KnowledgeBase", back_populates="documents")

    __table_args__ = (
        Index("uk_kb_doc", "kb_id", "doc_id", unique=True),
        Index("idx_kb_doc_parse", "parse_status"),
        Index("idx_kb_doc_storage", "storage_status"),
        Index("idx_kb_doc_date", "kb_id", "document_date"),
        Index("idx_kb_doc_category", "kb_id", "category_path"),
        Index("idx_kb_doc_backend", "kb_id", "ingestion_backend"),
        Index("idx_kb_doc_sort", "kb_id", "category_path", "sort_order"),
        Index("idx_kb_doc_hash", "kb_id", "source_hash"),
        Index("idx_kb_doc_visible", "kb_id", "visible_at"),
        # 管理端文档列表默认按 created_at DESC 分页（WHERE kb_id=），
        # 此前无覆盖索引走全库 filesort。
        Index("idx_kb_doc_created", "kb_id", "created_at"),
        {"comment": "知识库文档关联表"},
    )

    def __repr__(self):
        return f"<KnowledgeBaseDocument(kb_id={self.kb_id}, doc_id={self.doc_id}, file_name={self.file_name})>"


class KnowledgeBaseDirectory(Base):
    """知识库目录表 - 支持多层目录管理与元数据存储"""

    __tablename__ = "kb_directories"

    id = Column(_AUTOINCREMENT_ID, primary_key=True, autoincrement=True)
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

    id = Column(_AUTOINCREMENT_ID, primary_key=True, autoincrement=True)
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
    file_type = Column(String(100), nullable=True, comment="文件类型(MIME)")
    minio_path = Column(String(500), nullable=False, comment="MinIO存储路径")
    source_hash = Column(String(64), nullable=True, index=True, comment="源文件 SHA-256")
    object_bucket = Column(String(128), nullable=True, comment="对象存储桶")
    object_key = Column(String(500), nullable=True, comment="对象存储 Key")
    original_uri = Column(String(700), nullable=True, comment="源文件对象 URI")
    mime_type = Column(String(100), nullable=True, comment="文件 MIME 类型")
    file_ext = Column(String(20), nullable=True, comment="文件扩展名")
    ingestion_backend = Column(
        String(32),
        nullable=True,
        comment="任务创建时锁定的入库后端，空值兼容旧任务",
    )
    ingest_job_id = Column(String(64), nullable=True, index=True, comment="入库任务ID")
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
    last_ingest_started_at = Column(DateTime, nullable=True, comment="最近一次入库开始时间")
    last_ingest_finished_at = Column(DateTime, nullable=True, comment="最近一次入库结束时间")

    knowledge_base = relationship("KnowledgeBase")

    __table_args__ = (
        Index("idx_pending_kb", "kb_id"),
        Index("idx_pending_upload", "upload_id"),
        Index("idx_pending_status", "status"),
        Index("idx_pending_doc_date", "document_date"),
        Index("idx_pending_created", "created_at"),
        Index("idx_pending_backend", "ingestion_backend"),
        Index("idx_pending_hash", "kb_id", "source_hash"),
        # 用户面 /uploads/pending 列表按 (created_by, created_at desc) 轮询，
        # 此前只有单列 created_at 索引，行数增长后全表扫描+filesort。
        Index("idx_pending_creator_time", "created_by", "created_at"),
        {"comment": "待入库文件缓存表"},
    )

    def __repr__(self):
        return f"<PendingFile(file_id={self.file_id}, file_name={self.file_name}, status={self.status})>"


class SelfIngestJob(Base):
    """自研入库任务表"""

    __tablename__ = "kb_self_ingest_jobs"

    job_id = Column(String(64), primary_key=True, comment="入库任务ID")
    kb_id = Column(
        String(36),
        ForeignKey("knowledge_bases.kb_id"),
        nullable=False,
        index=True,
        comment="知识库ID",
    )
    file_id = Column(String(64), nullable=False, index=True, comment="待入库文件ID")
    doc_id = Column(String(64), nullable=True, index=True, comment="稳定文档ID")
    source_hash = Column(String(64), nullable=True, index=True, comment="源文件 SHA-256")
    status = Column(String(32), nullable=False, default="pending", index=True)
    stage = Column(String(64), nullable=True, comment="当前阶段")
    error_message = Column(Text, nullable=True)
    chunk_count = Column(Integer, nullable=False, default=0)
    vector_count = Column(Integer, nullable=False, default=0)
    retry_count = Column(Integer, nullable=False, default=0, comment="已重试次数")
    max_retry = Column(Integer, nullable=False, default=3, comment="最大重试次数")
    commit_log_json = Column(JSON, nullable=True, comment="各阶段提交审计日志")
    metrics_json = Column(JSON, nullable=True, comment="入库耗时与产出指标")
    started_at = Column(DateTime, nullable=True)
    finished_at = Column(DateTime, nullable=True)
    created_at = Column(DateTime, default=datetime.now, index=True)
    updated_at = Column(DateTime, default=datetime.now, onupdate=datetime.now)
    extra_json = Column(JSON, nullable=True)
    staging_uri = Column(
        String(500), nullable=True,
        comment="MinIO staging payload URI: staging/{kb_id}/{job_id}/payload.json",
    )

    knowledge_base = relationship("KnowledgeBase")

    __table_args__ = (
        Index("idx_self_job_kb_status", "kb_id", "status"),
        Index("idx_self_job_file_status", "file_id", "status"),
        Index("idx_self_job_retry", "status", "retry_count"),
        {"comment": "自研入库任务表"},
    )


class SelfIngestChunk(Base):
    """自研入库 Chunk 表"""

    __tablename__ = "kb_self_chunks"

    id = Column(_AUTOINCREMENT_ID, primary_key=True, autoincrement=True)
    kb_id = Column(
        String(36),
        ForeignKey("knowledge_bases.kb_id"),
        nullable=False,
        index=True,
        comment="知识库ID",
    )
    doc_id = Column(String(64), nullable=False, index=True, comment="稳定文档ID")
    chunk_id = Column(String(128), nullable=False, comment="稳定 Chunk ID")
    job_id = Column(String(64), nullable=True, index=True, comment="入库任务ID")
    file_name = Column(String(255), nullable=False)
    file_path = Column(String(500), nullable=True)
    chunk_index = Column(Integer, nullable=False)
    block_type = Column(String(32), nullable=False, default="text")
    content = Column(MEDIUMTEXT, nullable=False)
    content_hash = Column(String(64), nullable=False, index=True)
    stable_chunk_fingerprint = Column(String(64), nullable=False, index=True)
    token_count = Column(Integer, nullable=False, default=0)
    char_start = Column(Integer, nullable=True)
    char_end = Column(Integer, nullable=True)
    source_hash = Column(String(64), nullable=True, index=True)
    document_date = Column(Date, nullable=True, comment="文档业务日期")
    category_path = Column(String(500), nullable=True)
    commit_version = Column(Integer, nullable=False, default=1)
    is_visible = Column(Boolean, nullable=False, default=False, index=True)
    is_active = Column(Boolean, nullable=False, default=True, index=True)
    doc_status = Column(String(32), nullable=False, default="staged", index=True)
    embedding_status = Column(String(32), nullable=False, default="pending")
    milvus_collection = Column(String(128), nullable=True)
    extra_json = Column(JSON, nullable=True)
    created_at = Column(DateTime, default=datetime.now, index=True)
    updated_at = Column(DateTime, default=datetime.now, onupdate=datetime.now)
    visible_at = Column(DateTime, nullable=True, index=True)
    deleted_at = Column(DateTime, nullable=True, index=True)

    knowledge_base = relationship("KnowledgeBase")

    __table_args__ = (
        Index("uk_self_chunk_id", "kb_id", "chunk_id", unique=True),
        Index("idx_self_chunk_doc_visible", "kb_id", "doc_id", "is_visible", "is_active"),
        Index("idx_self_chunk_status", "kb_id", "doc_status"),
        Index("idx_self_chunk_hash", "kb_id", "content_hash"),
        {"comment": "自研入库 Chunk 表"},
    )
