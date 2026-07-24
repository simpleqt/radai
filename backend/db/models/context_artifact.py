"""Context artifact metadata ORM model."""

from __future__ import annotations

import uuid
from datetime import datetime

from sqlalchemy import BigInteger, Column, DateTime, Index, JSON, String, Text

from backend.db.models import Base


class ContextArtifactRecord(Base):
    """Metadata sidecar for large context payloads stored outside prompts."""

    __tablename__ = "context_artifacts"

    artifact_id = Column(String(36), primary_key=True, default=lambda: str(uuid.uuid4()))
    raw_ref = Column(String(768), nullable=False, unique=True)
    storage_backend = Column(String(32), nullable=False, default="local", index=True)
    bucket = Column(String(255), nullable=True)
    object_key = Column(String(1024), nullable=True)
    thread_id = Column(String(128), nullable=False, index=True)
    run_id = Column(String(128), nullable=True, index=True)
    unit_id = Column(String(128), nullable=True, index=True)
    source_tool = Column(String(128), nullable=False, index=True)
    size_chars = Column(BigInteger, nullable=False, default=0)
    sha256 = Column(String(64), nullable=False, index=True)
    suffix = Column(String(32), nullable=False, default=".txt")
    attributes = Column(JSON, nullable=True)
    preview = Column(Text, nullable=True)
    created_at = Column(DateTime, default=datetime.now, index=True)

    __table_args__ = (
        Index("idx_context_artifacts_thread_created", "thread_id", "created_at"),
        Index("idx_context_artifacts_run_unit", "run_id", "unit_id"),
        {"comment": "智能体上下文大结果工件元数据表"},
    )
