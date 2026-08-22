"""自定义学习 ORM 模型。"""

import uuid
from datetime import datetime

from sqlalchemy import (
    JSON,
    Boolean,
    Column,
    DateTime,
    ForeignKey,
    Index,
    Integer,
    String,
    Text,
)
from sqlalchemy.orm import relationship

from backend.db.models.base import Base


class LearningPlan(Base):
    """用户自定义学习计划。"""

    __tablename__ = "learning_plans"

    plan_id = Column(String(36), primary_key=True, default=lambda: str(uuid.uuid4()))
    user_id = Column(String(36), ForeignKey("users.user_id", ondelete="CASCADE"), nullable=False, index=True)
    template_id = Column(String(100), nullable=True, index=True)
    title = Column(String(255), nullable=False)
    summary = Column(Text, nullable=True)
    domain = Column(String(50), nullable=False, default="ne")
    goal = Column(Text, nullable=True)
    baseline = Column(String(50), nullable=True)
    depth = Column(String(50), nullable=True)
    duration_days = Column(Integer, nullable=False, default=7)
    daily_minutes = Column(Integer, nullable=False, default=90)
    kb_ids = Column(JSON, nullable=True)
    style_snapshot = Column(JSON, nullable=True)
    status = Column(String(20), nullable=False, default="active", index=True)
    progress = Column(Integer, nullable=False, default=0)
    mastery = Column(Integer, nullable=False, default=0)
    accent = Column(String(20), nullable=True)
    surface = Column(String(20), nullable=True)
    course_type = Column(String(100), nullable=True)
    current_node_id = Column(String(36), nullable=True, index=True)
    metadata_ = Column("metadata", JSON, nullable=True)
    started_at = Column(DateTime, nullable=True)
    completed_at = Column(DateTime, nullable=True)
    archived_at = Column(DateTime, nullable=True)
    created_at = Column(DateTime, default=datetime.now, index=True)
    updated_at = Column(DateTime, default=datetime.now, onupdate=datetime.now)

    user = relationship("User", backref="learning_plans")
    nodes = relationship(
        "LearningPlanNode",
        back_populates="plan",
        cascade="all, delete-orphan",
        order_by="LearningPlanNode.order_index",
    )
    attempts = relationship(
        "LearningNodeAttempt",
        back_populates="plan",
        cascade="all, delete-orphan",
    )
    adjustments = relationship(
        "LearningPlanAdjustment",
        back_populates="plan",
        cascade="all, delete-orphan",
    )

    __table_args__ = (
        Index("idx_learning_plan_user_status", "user_id", "status", "updated_at"),
        {"comment": "自定义学习计划表"},
    )


class LearningPlanNode(Base):
    """自定义学习计划节点。"""

    __tablename__ = "learning_plan_nodes"

    node_id = Column(String(36), primary_key=True, default=lambda: str(uuid.uuid4()))
    plan_id = Column(String(36), ForeignKey("learning_plans.plan_id", ondelete="CASCADE"), nullable=False, index=True)
    parent_node_id = Column(String(36), nullable=True)
    order_index = Column(Integer, nullable=False, default=0)
    title = Column(String(255), nullable=False)
    subtitle = Column(String(255), nullable=True)
    objective = Column(Text, nullable=True)
    explanation = Column(Text, nullable=True)
    quiz_questions = Column(JSON, nullable=True)
    prerequisites = Column(JSON, nullable=True)
    resources = Column(JSON, nullable=True)
    tasks = Column(JSON, nullable=True)
    checks = Column(JSON, nullable=True)
    resource = Column(Text, nullable=True)
    task = Column(Text, nullable=True)
    check = Column(Text, nullable=True)
    day = Column(String(50), nullable=True)
    node_type = Column(String(20), nullable=False, default="concept")
    difficulty = Column(String(20), nullable=True)
    estimated_minutes = Column(Integer, nullable=False, default=60)
    unlock_rule = Column(JSON, nullable=True)
    status = Column(String(20), nullable=False, default="locked", index=True)
    mastery = Column(Integer, nullable=False, default=0)
    study_signals = Column(JSON, nullable=True)
    explore_cache = Column(JSON, nullable=True)
    due_at = Column(DateTime, nullable=True)
    completed_at = Column(DateTime, nullable=True)
    created_at = Column(DateTime, default=datetime.now)
    updated_at = Column(DateTime, default=datetime.now, onupdate=datetime.now)

    plan = relationship("LearningPlan", back_populates="nodes")
    attempts = relationship(
        "LearningNodeAttempt",
        back_populates="node",
        cascade="all, delete-orphan",
    )

    __table_args__ = (
        Index("idx_learning_node_plan_order", "plan_id", "order_index"),
        Index("idx_learning_node_plan_status", "plan_id", "status"),
        {"comment": "自定义学习计划节点表"},
    )


class LearningNodeAttempt(Base):
    """节点学习或测验记录。"""

    __tablename__ = "learning_node_attempts"

    attempt_id = Column(String(36), primary_key=True, default=lambda: str(uuid.uuid4()))
    plan_id = Column(String(36), ForeignKey("learning_plans.plan_id", ondelete="CASCADE"), nullable=False, index=True)
    node_id = Column(String(36), ForeignKey("learning_plan_nodes.node_id", ondelete="CASCADE"), nullable=False, index=True)
    user_id = Column(String(36), ForeignKey("users.user_id", ondelete="CASCADE"), nullable=False, index=True)
    action_type = Column(String(20), nullable=False)
    payload = Column(JSON, nullable=True)
    score = Column(Integer, nullable=True)
    diagnosis = Column(JSON, nullable=True)
    duration_seconds = Column(Integer, nullable=True)
    mastery_before = Column(Integer, nullable=True)
    mastery_after = Column(Integer, nullable=True)
    source = Column(String(30), nullable=False, default="manual")
    created_at = Column(DateTime, default=datetime.now, index=True)

    plan = relationship("LearningPlan", back_populates="attempts")
    node = relationship("LearningPlanNode", back_populates="attempts")
    user = relationship("User", backref="learning_node_attempts")

    __table_args__ = (
        Index("idx_learning_attempt_node_user_created", "node_id", "user_id", "created_at"),
        {"comment": "自定义学习节点学习记录表"},
    )


class LearningPlanAdjustment(Base):
    """计划调整建议记录。"""

    __tablename__ = "learning_plan_adjustments"

    adjustment_id = Column(String(36), primary_key=True, default=lambda: str(uuid.uuid4()))
    plan_id = Column(String(36), ForeignKey("learning_plans.plan_id", ondelete="CASCADE"), nullable=False, index=True)
    trigger_type = Column(String(50), nullable=False)
    before_snapshot = Column(JSON, nullable=True)
    after_snapshot = Column(JSON, nullable=True)
    reason = Column(Text, nullable=True)
    accepted = Column(Boolean, nullable=False, default=False)
    created_at = Column(DateTime, default=datetime.now, index=True)

    plan = relationship("LearningPlan", back_populates="adjustments")

    __table_args__ = (
        Index("idx_learning_adjustment_plan_created", "plan_id", "created_at"),
        {"comment": "自定义学习计划调整记录表"},
    )
