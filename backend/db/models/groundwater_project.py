"""Groundwater project catalog and user authorization models."""

from datetime import datetime

from sqlalchemy import Boolean, Column, DateTime, ForeignKey, Index, JSON, String, UniqueConstraint
from sqlalchemy.orm import relationship

from backend.db.models.base import Base


class GroundwaterProject(Base):
    __tablename__ = "groundwater_projects"

    project_id = Column(String(64), primary_key=True)
    name = Column(String(255), nullable=False)
    region = Column(String(255), nullable=True)
    storage_path = Column(String(1024), nullable=False)
    status = Column(String(20), nullable=False, default="active", index=True)
    is_builtin = Column(Boolean, nullable=False, default=False)
    data_version = Column(String(32), nullable=False, default="1")
    quality_status = Column(String(32), nullable=True)
    manifest_snapshot = Column(JSON, nullable=True)
    created_by = Column(String(36), ForeignKey("users.user_id"), nullable=True, index=True)
    created_at = Column(DateTime, default=datetime.now, nullable=False)
    updated_at = Column(DateTime, default=datetime.now, onupdate=datetime.now, nullable=False)

    bindings = relationship(
        "GroundwaterProjectUser",
        back_populates="project",
        cascade="all, delete-orphan",
    )


class GroundwaterProjectUser(Base):
    __tablename__ = "groundwater_project_users"

    id = Column(String(36), primary_key=True)
    project_id = Column(
        String(64),
        ForeignKey("groundwater_projects.project_id", ondelete="CASCADE"),
        nullable=False,
        index=True,
    )
    user_id = Column(
        String(36),
        ForeignKey("users.user_id", ondelete="CASCADE"),
        nullable=False,
        index=True,
    )
    permission = Column(String(20), nullable=False, default="viewer")
    granted_by = Column(String(36), ForeignKey("users.user_id"), nullable=True)
    created_at = Column(DateTime, default=datetime.now, nullable=False)

    project = relationship("GroundwaterProject", back_populates="bindings")

    __table_args__ = (
        UniqueConstraint("project_id", "user_id", name="uq_groundwater_project_user"),
        Index("idx_groundwater_project_user_lookup", "user_id", "project_id"),
    )
