"""基础力学课程智能体 · 题库管理 ORM 定义。

题目（``QuestionBankQuestion``）是一等业务实体：内容 + 标注（难度/知识点/章节）+
图片强绑定（``images_json`` 持有稳定 MinIO object_name，入库时绑定而非检索时关联）。
``QuestionBankBatch`` 暂存解析结果，教师确认后才写题目表；``QuestionBankAttempt``
是学生做题记录，兼作教学分析的练习数据源。
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


class QuestionBankBatch(Base):
    """教师一次 docx 上传的批次（解析 → 确认两段式）。"""

    __tablename__ = "question_bank_batches"

    id = Column(String(36), primary_key=True, default=lambda: str(uuid.uuid4()))
    file_name = Column(String(255), nullable=False)
    file_key = Column(String(512), nullable=True)
    # parsing（LLM 辅助异步预留）/ parsed（待确认）/ confirmed / failed
    status = Column(String(16), nullable=False, default="parsed")
    total_count = Column(Integer, nullable=False, default=0)
    valid_count = Column(Integer, nullable=False, default=0)
    parsed_json = Column(JSON, nullable=True)
    error = Column(String(500), nullable=True)
    created_by = Column(String(36), nullable=False)
    created_at = Column(DateTime, default=datetime.now, nullable=False)
    confirmed_at = Column(DateTime, nullable=True)

    __table_args__ = (Index("idx_qbb_creator", "created_by", "created_at"),)

    def __repr__(self) -> str:
        return f"<QuestionBankBatch(id={self.id}, status={self.status})>"


class QuestionBankQuestion(Base):
    """一道题：题干/选项/答案/解析 + 标注 + 图片强绑定。"""

    __tablename__ = "question_bank_questions"

    id = Column(String(36), primary_key=True, default=lambda: str(uuid.uuid4()))
    # 选择/填空/判断/计算/作图/简答
    question_type = Column(String(16), nullable=False)
    stem = Column(Text, nullable=False)
    options_json = Column(JSON, nullable=True)
    answer = Column(Text, nullable=True)
    analysis = Column(Text, nullable=True)
    score = Column(String(32), nullable=True)
    # easy/medium/hard，可空=待标注
    difficulty = Column(String(16), nullable=True)
    chapter = Column(String(128), nullable=True)
    knowledge_points_json = Column(JSON, nullable=True)
    # [{object_name, caption, seq}] —— 稳定 object_name 即持久身份
    images_json = Column(JSON, nullable=True)
    # published/disabled（软删）
    status = Column(String(16), nullable=False, default="published")
    source_batch_id = Column(String(36), nullable=True)
    source_name = Column(String(255), nullable=True)
    stem_hash = Column(String(64), nullable=False)
    created_by = Column(String(36), nullable=False)
    created_at = Column(DateTime, default=datetime.now, nullable=False)
    updated_at = Column(DateTime, nullable=True)

    __table_args__ = (
        Index("idx_qbq_status_type", "status", "question_type"),
        Index("idx_qbq_difficulty", "difficulty"),
        Index("idx_qbq_chapter", "chapter"),
        Index("idx_qbq_batch", "source_batch_id"),
        Index("idx_qbq_hash", "stem_hash"),
    )

    def __repr__(self) -> str:
        return f"<QuestionBankQuestion(id={self.id}, type={self.question_type})>"


class QuestionBankKnowledgePoint(Base):
    """知识点受控字典：标注、学生筛选、分析聚合共用同一枚举。"""

    __tablename__ = "question_bank_knowledge_points"

    id = Column(String(36), primary_key=True, default=lambda: str(uuid.uuid4()))
    name = Column(String(128), nullable=False, unique=True)
    chapter = Column(String(128), nullable=True)
    parent_id = Column(String(36), nullable=True)
    sort = Column(Integer, nullable=False, default=0)
    created_by = Column(String(36), nullable=True)
    created_at = Column(DateTime, default=datetime.now, nullable=False)

    def __repr__(self) -> str:
        return f"<QuestionBankKnowledgePoint(id={self.id}, name={self.name})>"


class QuestionBankAttempt(Base):
    """学生一次作答（判分在服务端；主观题 is_correct 为空待自评）。"""

    __tablename__ = "question_bank_attempts"

    id = Column(String(36), primary_key=True, default=lambda: str(uuid.uuid4()))
    # 学生 user_id（登录用户为 36 字符；加宽到 64 与提问采集一致）
    user_id = Column(String(64), nullable=False)
    username = Column(String(50), nullable=False, default="")
    question_id = Column(String(36), nullable=False)
    answer_json = Column(JSON, nullable=True)
    # 与迁移 SQL 的 TINYINT(1) 对齐（Boolean 在 MySQL 方言产出 TINYINT(1)、
    # SQLite 产出 BOOLEAN）；此前 ORM 声明 Integer 会让 create_all 建出 INT，
    # 与迁移产物结构分叉。读取得到 bool，代码仍按真值判断。
    is_correct = Column(Boolean, nullable=True)
    # practice=做题模式 / browse=解答模式浏览
    mode = Column(String(16), nullable=False, default="practice")
    created_at = Column(DateTime, default=datetime.now, nullable=False)

    __table_args__ = (
        Index("idx_qba_user_time", "user_id", "created_at"),
        Index("idx_qba_question", "question_id"),
        Index("idx_qba_time", "created_at"),
    )

    def __repr__(self) -> str:
        return f"<QuestionBankAttempt(id={self.id}, user={self.user_id})>"
