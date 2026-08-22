"""MySQL 会话历史 ORM 定义(迁移基线;活跃目录见 runtime-local SQLite)"""

import uuid
from datetime import datetime

from sqlalchemy import (
    JSON,
    Boolean,
    Column,
    DateTime,
    ForeignKey,
    Index,
    String,
    UniqueConstraint,
)
from sqlalchemy.orm import relationship

from backend.db.models.base import Base


class Conversation(Base):
    """会话历史表"""

    __tablename__ = "conversations"

    id = Column(String(36), primary_key=True, default=lambda: str(uuid.uuid4()))
    user_id = Column(
        String(36), ForeignKey("users.user_id"), nullable=False, index=True
    )
    thread_id = Column(String(100), nullable=False, index=True)
    title = Column(String(255), default="新对话")
    metadata_ = Column("metadata", JSON, nullable=True)
    is_deleted = Column(Boolean, default=False)
    created_at = Column(DateTime, default=datetime.now, index=True)
    updated_at = Column(DateTime, default=datetime.now, onupdate=datetime.now)

    user = relationship("User", backref="conversations")

    __table_args__ = (
        UniqueConstraint("user_id", "thread_id", name="uq_conversation_user_thread"),
        Index(
            "idx_conversation_user_history",
            "user_id",
            "is_deleted",
            "updated_at",
            "id",
        ),
    )

    def __repr__(self):
        return f"<Conversation(id={self.id}, thread_id={self.thread_id})>"
