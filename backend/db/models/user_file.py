"""用户文件、跨用户授权与会话文件关联 ORM 定义"""

import uuid
from datetime import datetime

from sqlalchemy import (
    JSON,
    BigInteger,
    Column,
    DateTime,
    ForeignKey,
    Index,
    String,
    UniqueConstraint,
)
from sqlalchemy.orm import relationship

from backend.db.models.base import Base


class UserFile(Base):
    """用户文件表"""

    __tablename__ = "user_files"

    id = Column(BigInteger, primary_key=True, autoincrement=True)
    file_id = Column(
        String(64),
        nullable=False,
        unique=True,
        index=True,
        default=lambda: f"uf_{uuid.uuid4().hex}",
    )
    owner_user_id = Column(
        String(36), ForeignKey("users.user_id"), nullable=False, index=True
    )
    thread_id = Column(String(100), nullable=True)
    source_type = Column(String(20), default="local")  # 'local', 'minio', 'mixed'
    storage_bucket = Column(String(128), nullable=True)
    storage_object = Column(String(512), nullable=True)
    local_abs_path = Column(String(1024), nullable=True)
    artifact_key = Column(String(64), nullable=True, unique=True)
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


class UserFilePermission(Base):
    """Explicit grants for reading/downloading another user's file."""

    __tablename__ = "user_file_permissions"

    id = Column(BigInteger, primary_key=True, autoincrement=True)
    file_id = Column(String(64), nullable=False, index=True)
    grantee_user_id = Column(String(36), nullable=False, index=True)
    permission = Column(String(16), nullable=False, default="read")
    expires_at = Column(DateTime, nullable=True)
    granted_by = Column(String(36), nullable=True)
    created_at = Column(DateTime, nullable=False, default=datetime.now)

    __table_args__ = (
        UniqueConstraint(
            "file_id",
            "grantee_user_id",
            "permission",
            name="uk_user_file_permission",
        ),
        Index("idx_user_file_permission_grantee", "grantee_user_id", "file_id"),
    )


class SessionFile(Base):
    """会话文件关联表"""

    __tablename__ = "session_files"

    id = Column(String(36), primary_key=True, default=lambda: str(uuid.uuid4()))
    session_id = Column(String(100), nullable=False, index=True)  # thread_id
    user_id = Column(
        String(36), ForeignKey("users.user_id"), nullable=False, index=True
    )
    file_id = Column(
        String(64),
        ForeignKey("user_files.file_id", ondelete="CASCADE"),
        nullable=False,
        index=True,
    )
    attached_from = Column(
        String(50), default="my_files"
    )  # "upload" / "my_files" / "generated_auto"
    created_at = Column(DateTime, default=datetime.now)

    user = relationship("User", backref="session_files")
    user_file = relationship(
        "UserFile",
        backref="session_associations",
        foreign_keys=[file_id],
        primaryjoin="SessionFile.file_id == UserFile.file_id",
    )

    def __repr__(self):
        return f"<SessionFile(session_id={self.session_id}, file_id={self.file_id})>"
