"""用户学习偏好测评 ORM 定义"""

import uuid
from datetime import datetime

from sqlalchemy import (
    JSON,
    Boolean,
    Column,
    DateTime,
    ForeignKey,
    Integer,
    String,
)
from sqlalchemy.orm import relationship

from backend.db.models.base import Base


class LearningStyleAttempt(Base):
    """用户学习偏好测评记录。"""

    __tablename__ = "learning_style_attempts"

    attempt_id = Column(String(36), primary_key=True, default=lambda: str(uuid.uuid4()))
    user_id = Column(
        String(36),
        ForeignKey("users.user_id", ondelete="CASCADE"),
        nullable=False,
        index=True,
    )
    questionnaire_version = Column(String(32), nullable=False)
    status = Column(String(20), nullable=False, default="in_progress", index=True)
    answers = Column(JSON, nullable=False, default=dict)
    scores = Column(JSON, nullable=True)
    result = Column(JSON, nullable=True)
    current_dimension = Column(Integer, nullable=False, default=0)
    completed_at = Column(DateTime, nullable=True)
    created_at = Column(DateTime, default=datetime.now, index=True)
    updated_at = Column(DateTime, default=datetime.now, onupdate=datetime.now)

    user = relationship("User", backref="learning_style_attempts")


class LearningStyleProfile(Base):
    """用户当前采用的学习偏好结果。"""

    __tablename__ = "learning_style_profiles"

    user_id = Column(
        String(36), ForeignKey("users.user_id", ondelete="CASCADE"), primary_key=True
    )
    latest_attempt_id = Column(
        String(36),
        ForeignKey("learning_style_attempts.attempt_id", ondelete="CASCADE"),
        nullable=False,
        index=True,
    )
    scores = Column(JSON, nullable=False)
    result = Column(JSON, nullable=False)
    apply_to_ai = Column(Boolean, nullable=False, default=False)
    created_at = Column(DateTime, default=datetime.now)
    updated_at = Column(DateTime, default=datetime.now, onupdate=datetime.now)

    user = relationship("User", backref="learning_style_profile", uselist=False)
    latest_attempt = relationship("LearningStyleAttempt")
