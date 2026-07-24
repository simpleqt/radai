"""实验数据集 ORM 模型"""

import uuid
from datetime import datetime

from sqlalchemy import (
    JSON,
    BigInteger,
    Boolean,
    Column,
    DateTime,
    Index,
    String,
    Text,
    UniqueConstraint,
)

from backend.db.models import Base


class ExperimentDataset(Base):
    """实验数据集注册表

    存储服务器预构建的实验数据集元数据，支持智能体自主发现和查询。
    """

    __tablename__ = "experiment_datasets"

    id = Column(
        String(36),
        primary_key=True,
        default=lambda: str(uuid.uuid4()),
        comment="数据集唯一 ID",
    )
    code = Column(
        String(100),
        unique=True,
        nullable=False,
        index=True,
        comment="数据集编码（英文，智能体用于引用）",
    )
    name = Column(String(255), nullable=False, comment="数据集名称")
    description = Column(Text, nullable=True, comment="数据集描述")
    file_path = Column(String(512), nullable=False, comment="文件路径（相对 datasets/ 目录或绝对路径）")
    file_type = Column(String(10), default="xlsx", comment="文件类型: xlsx/csv/xls")
    file_size = Column(BigInteger, nullable=True, comment="文件大小(bytes)")
    storage_type = Column(String(20), default="local", comment="存储类型: local/minio")

    # 自动解析的元数据
    sheet_names = Column(JSON, nullable=True, comment="工作表列表")
    column_schema = Column(JSON, nullable=True, comment="各表列定义 {sheet: [{name, dtype, non_null, sample}]}")
    row_counts = Column(JSON, nullable=True, comment="各表行数 {sheet: count}")

    # 分类与管理
    domain = Column(String(50), nullable=True, comment="领域: environmental/chemistry/physics/biology")
    tags = Column(JSON, default=list, comment="标签列表")
    is_public = Column(Boolean, default=True, comment="是否对所有用户可见")
    created_by = Column(String(36), nullable=True, comment="创建者用户 ID")
    status = Column(String(20), default="active", comment="状态: active/archived/processing")

    # Agent 绑定（与 agent_kb_bindings 设计对齐）
    # 为空列表时对所有 agent 可见（公共数据集）；非空时仅限列表内的 agent_code
    agent_codes = Column(JSON, default=list, comment="绑定的智能体编码列表，为空表示全局可见")

    created_at = Column(DateTime, default=datetime.now, comment="创建时间")
    updated_at = Column(DateTime, default=datetime.now, onupdate=datetime.now, comment="更新时间")

    __table_args__ = (
        Index("idx_dataset_code", "code"),
        Index("idx_dataset_domain", "domain"),
        Index("idx_dataset_status", "status"),
        {"comment": "实验数据集注册表"},
    )

    def __repr__(self) -> str:
        return f"<ExperimentDataset(code={self.code!r}, name={self.name!r})>"

    def to_dict(self) -> dict:
        return {
            "id": self.id,
            "code": self.code,
            "name": self.name,
            "description": self.description,
            "file_type": self.file_type,
            "file_size": self.file_size,
            "storage_type": self.storage_type,
            "sheet_names": self.sheet_names or [],
            "column_schema": self.column_schema or {},
            "row_counts": self.row_counts or {},
            "domain": self.domain,
            "tags": self.tags or [],
            "is_public": self.is_public,
            "agent_codes": self.agent_codes or [],
            "status": self.status,
            "created_at": self.created_at.isoformat() if self.created_at else None,
        }


class ExperimentDatasetAcl(Base):
    """Explicit grants for non-public experiment datasets."""

    __tablename__ = "experiment_dataset_acl"

    id = Column(BigInteger, primary_key=True, autoincrement=True)
    dataset_id = Column(String(36), nullable=False, index=True)
    subject_type = Column(String(16), nullable=False, default="user")
    subject_id = Column(String(64), nullable=False)
    permission = Column(String(16), nullable=False, default="read")
    effect = Column(String(8), nullable=False, default="allow")
    expires_at = Column(DateTime, nullable=True)
    created_by = Column(String(36), nullable=True)
    created_at = Column(DateTime, nullable=False, default=datetime.now)

    __table_args__ = (
        UniqueConstraint(
            "dataset_id",
            "subject_type",
            "subject_id",
            "permission",
            name="uk_experiment_dataset_acl",
        ),
        Index("idx_experiment_dataset_acl_subject", "subject_type", "subject_id"),
        Index("idx_experiment_dataset_acl_dataset", "dataset_id"),
        {"comment": "Explicit experiment-dataset access grants"},
    )
