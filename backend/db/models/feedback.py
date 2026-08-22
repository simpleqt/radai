"""回答反馈 ORM 定义。

按 (user_id, thread_id, message_key) 幂等 upsert：同一用户对同一条回答
只保留最新一次反馈。message_key 是前端消息标识（历史消息为 checkpoint
消息 id，直播消息为客户端生成 id），用于按学科/模型聚合质量信号，
不承诺跨会话重放级精确回链。
"""

import uuid
from datetime import datetime

from sqlalchemy import (
    JSON,
    Column,
    DateTime,
    ForeignKey,
    Index,
    String,
    Text,
    UniqueConstraint,
)
from sqlalchemy.orm import relationship

from backend.db.models.base import Base


class MessageFeedback(Base):
    """用户对单条智能体回答的赞/踩反馈。"""

    __tablename__ = "message_feedback"

    id = Column(String(36), primary_key=True, default=lambda: str(uuid.uuid4()))
    user_id = Column(
        String(36),
        ForeignKey("users.user_id", ondelete="CASCADE"),
        nullable=False,
        index=True,
    )
    thread_id = Column(String(100), nullable=False)
    agent_code = Column(String(32), nullable=False, default="ne")
    message_key = Column(String(128), nullable=False)
    rating = Column(String(8), nullable=False)  # up / down
    reasons = Column(JSON, nullable=True)
    comment = Column(Text, nullable=True)
    model_name = Column(String(128), nullable=True)
    question_excerpt = Column(String(500), nullable=True)
    answer_excerpt = Column(String(1000), nullable=True)
    created_at = Column(DateTime, default=datetime.now, index=True)
    updated_at = Column(DateTime, default=datetime.now, onupdate=datetime.now)

    user = relationship("User", backref="message_feedback")

    __table_args__ = (
        UniqueConstraint(
            "user_id", "thread_id", "message_key", name="uq_feedback_user_message"
        ),
        Index("idx_feedback_agent_rating", "agent_code", "rating", "created_at"),
    )

    def __repr__(self):
        return (
            f"<MessageFeedback(id={self.id}, thread_id={self.thread_id}, "
            f"rating={self.rating})>"
        )
