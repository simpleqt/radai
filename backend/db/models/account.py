"""用户、API Key 与管理员审计 ORM 定义"""

import uuid
from datetime import datetime

from sqlalchemy import (
    JSON,
    BigInteger,
    Boolean,
    Column,
    DateTime,
    ForeignKey,
    Index,
    Integer,
    String,
)
from sqlalchemy.orm import relationship

from backend.db.models.base import Base


class User(Base):
    """用户表"""

    __tablename__ = "users"

    user_id = Column(String(36), primary_key=True, default=lambda: str(uuid.uuid4()))
    username = Column(String(50), unique=True, nullable=False, index=True)
    display_name = Column(String(50), nullable=True)
    telephone = Column(String(20), unique=True, nullable=True, index=True)
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
    # Legacy verbatim key. Retained nullable for the migration/rollback window;
    # new rows authenticate through ``api_key_hash`` and leave this empty.
    api_key = Column(String(100), unique=True, nullable=True, index=True)
    api_key_hash = Column(
        String(64),
        unique=True,
        nullable=True,
        index=True,
        comment="原始 Key 的 SHA-256，用于查找（不可逆）",
    )
    api_key_prefix = Column(
        String(32),
        nullable=True,
        comment="展示用前缀，非凭据",
    )
    user_id = Column(String(36), ForeignKey("users.user_id"), nullable=True)
    key_name = Column(String(50), nullable=True)
    access_scope = Column(
        String(20),
        nullable=False,
        default="full",
        server_default="full",
        comment="访问范围: full/mcp_only",
    )
    mcp_allowed_tools = Column(
        JSON,
        nullable=True,
        comment="MCP 工具白名单；NULL 表示不限制",
    )
    is_active = Column(Boolean, default=True)
    rate_limit = Column(Integer, default=100)
    created_at = Column(DateTime, default=datetime.now)
    expires_at = Column(DateTime, nullable=True)
    last_used_at = Column(DateTime, nullable=True)
    usage_count = Column(BigInteger, default=0)

    def __repr__(self):
        label = self.api_key_prefix or (self.api_key or "")[:8]
        return f"<ApiKey(api_key={label}..., user_id={self.user_id})>"


class AdminAuditLog(Base):
    """全局管理员操作审计表。"""

    __tablename__ = "admin_audit_logs"

    audit_id = Column(BigInteger, primary_key=True, autoincrement=True)
    actor_user_id = Column(
        String(36), ForeignKey("users.user_id"), nullable=True, index=True
    )
    action = Column(String(64), nullable=False, index=True)
    target_type = Column(String(64), nullable=False, index=True)
    target_id = Column(String(128), nullable=True, index=True)
    status = Column(String(20), nullable=False, default="success", index=True)
    method = Column(String(10), nullable=True)
    path = Column(String(512), nullable=True)
    ip_address = Column(String(64), nullable=True)
    user_agent = Column(String(512), nullable=True)
    detail_json = Column(JSON, nullable=True)
    created_at = Column(DateTime, default=datetime.now, index=True)

    actor = relationship("User", backref="admin_audit_logs")

    __table_args__ = (
        Index("idx_admin_audit_actor_created", "actor_user_id", "created_at"),
        Index("idx_admin_audit_target", "target_type", "target_id"),
    )

    def __repr__(self):
        return f"<AdminAuditLog(audit_id={self.audit_id}, action={self.action})>"
