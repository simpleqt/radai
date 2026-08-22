"""基础力学课程智能体 · 教师提问分析 ORM 定义。

``CourseQuestionRecord`` 在提问入口同步旁路写入（fire-and-forget，不阻塞
回答流），仅采集 ``agent_code=fmca`` 的学生提问；``CourseQuestionReport``
是教师触发的汇总分析快照，避免反复查看时重跑模型。
"""

import uuid
from datetime import datetime

from sqlalchemy import (
    JSON,
    Boolean,
    Column,
    DateTime,
    Index,
    Integer,
    String,
    Text,
)

from backend.db.models.base import Base


class CourseQuestionRecord(Base):
    """学生向基础力学智能体提交的一条提问快照。"""

    __tablename__ = "course_question_records"

    id = Column(String(36), primary_key=True, default=lambda: str(uuid.uuid4()))
    # 访客 user_id（guest_<32hex>，38 字符）长于 UUID，需 64 宽度。
    # 单列索引声明省略：idx_qr_user_time 的最左前缀已覆盖 user_id 查找，
    # create_all 与迁移 SQL 保持同一组索引。
    user_id = Column(String(64), nullable=False)
    username = Column(String(50), nullable=False, default="")
    agent_code = Column(String(32), nullable=False)
    thread_id = Column(String(100), nullable=False)
    question = Column(Text, nullable=False)
    question_len = Column(Integer, nullable=False)
    workflow = Column(String(32), nullable=True)
    model_name = Column(String(128), nullable=True)
    is_guest = Column(Boolean, nullable=False, default=False)
    created_at = Column(DateTime, default=datetime.now, nullable=False)

    __table_args__ = (
        Index("idx_qr_agent_time", "agent_code", "created_at"),
        Index("idx_qr_user_time", "user_id", "created_at"),
    )

    def __repr__(self):
        return f"<CourseQuestionRecord(id={self.id}, agent_code={self.agent_code})>"


class CourseQuestionReport(Base):
    """教师触发的一次提问汇总分析报告快照。"""

    __tablename__ = "course_question_reports"

    id = Column(String(36), primary_key=True, default=lambda: str(uuid.uuid4()))
    agent_code = Column(String(32), nullable=False)
    created_by = Column(String(36), nullable=False)
    range_start = Column(DateTime, nullable=False)
    range_end = Column(DateTime, nullable=False)
    question_count = Column(Integer, nullable=False, default=0)
    status = Column(String(16), nullable=False, default="running")
    result_json = Column(JSON, nullable=True)
    model_name = Column(String(128), nullable=True)
    error = Column(String(500), nullable=True)
    created_at = Column(DateTime, default=datetime.now, nullable=False)
    finished_at = Column(DateTime, nullable=True)

    __table_args__ = (Index("idx_qrpt_agent", "agent_code", "created_at"),)

    def __repr__(self):
        return f"<CourseQuestionReport(id={self.id}, status={self.status})>"
