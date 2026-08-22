"""Managed Skill catalog, versions, ACLs, bindings, and audit records."""

from __future__ import annotations

import uuid
from datetime import datetime

from sqlalchemy import (
    JSON,
    BigInteger,
    Boolean,
    Column,
    DateTime,
    Index,
    Integer,
    String,
    Text,
    UniqueConstraint,
)

from backend.db.models.base import Base


def _uuid() -> str:
    return str(uuid.uuid4())


_AUTOINCREMENT_ID = BigInteger().with_variant(Integer, "sqlite")


class ManagedSkill(Base):
    __tablename__ = "skills"

    skill_id = Column(String(36), primary_key=True, default=_uuid)
    skill_key = Column(String(64), nullable=False, unique=True, index=True)
    display_name = Column(String(100), nullable=False)
    description = Column(String(1000), nullable=False)
    display_description = Column(String(1000), nullable=False, default="")
    domain = Column(String(64), nullable=False, index=True)
    source_type = Column(String(20), nullable=False, default="uploaded")
    visibility = Column(String(20), nullable=False, default="private")
    owner_user_id = Column(String(36), nullable=True, index=True)
    status = Column(String(20), nullable=False, default="draft", index=True)
    active_version_id = Column(String(36), nullable=True, index=True)
    created_by = Column(String(36), nullable=False)
    created_at = Column(DateTime, nullable=False, default=datetime.utcnow)
    updated_at = Column(
        DateTime, nullable=False, default=datetime.utcnow, onupdate=datetime.utcnow
    )

    __table_args__ = (
        Index("idx_skills_owner_status", "owner_user_id", "status"),
        {"comment": "Managed Skill catalog"},
    )


class SkillVersion(Base):
    __tablename__ = "skill_versions"

    version_id = Column(String(36), primary_key=True, default=_uuid)
    skill_id = Column(String(36), nullable=False, index=True)
    version = Column(String(32), nullable=False)
    status = Column(String(20), nullable=False, default="validated", index=True)
    package_bucket = Column(String(128), nullable=False)
    package_object = Column(String(512), nullable=False)
    package_sha256 = Column(String(64), nullable=False, index=True)
    package_size = Column(BigInteger, nullable=False)
    manifest_json = Column(JSON, nullable=False)
    file_index_json = Column(JSON, nullable=False)
    review_comment = Column(Text, nullable=True)
    created_by = Column(String(36), nullable=False)
    reviewed_by = Column(String(36), nullable=True)
    created_at = Column(DateTime, nullable=False, default=datetime.utcnow)
    published_at = Column(DateTime, nullable=True)

    __table_args__ = (
        UniqueConstraint("skill_id", "version", name="uk_skill_version"),
        Index("idx_skill_versions_skill_status", "skill_id", "status"),
        {"comment": "Immutable Skill package versions"},
    )


class SkillAcl(Base):
    __tablename__ = "skill_acl"

    id = Column(_AUTOINCREMENT_ID, primary_key=True, autoincrement=True)
    skill_id = Column(String(36), nullable=False, index=True)
    subject_type = Column(String(16), nullable=False)
    subject_id = Column(String(64), nullable=False)
    permission = Column(String(16), nullable=False)
    effect = Column(String(8), nullable=False, default="allow")
    expires_at = Column(DateTime, nullable=True)
    created_by = Column(String(36), nullable=False)
    created_at = Column(DateTime, nullable=False, default=datetime.utcnow)

    __table_args__ = (
        UniqueConstraint(
            "skill_id",
            "subject_type",
            "subject_id",
            "permission",
            name="uk_skill_acl",
        ),
        Index("idx_skill_acl_subject", "subject_type", "subject_id"),
        {"comment": "Skill user and role permissions"},
    )


class AgentSkillBinding(Base):
    __tablename__ = "agent_skill_bindings"

    id = Column(_AUTOINCREMENT_ID, primary_key=True, autoincrement=True)
    agent_code = Column(String(64), nullable=False, index=True)
    skill_id = Column(String(36), nullable=False, index=True)
    is_enabled = Column(Boolean, nullable=False, default=True)
    is_required = Column(Boolean, nullable=False, default=False)
    default_selected = Column(Boolean, nullable=False, default=False)
    is_selectable = Column(Boolean, nullable=False, default=True)
    sort_order = Column(BigInteger, nullable=False, default=0)
    created_by = Column(String(36), nullable=False)
    created_at = Column(DateTime, nullable=False, default=datetime.utcnow)
    updated_at = Column(
        DateTime, nullable=False, default=datetime.utcnow, onupdate=datetime.utcnow
    )

    __table_args__ = (
        UniqueConstraint("agent_code", "skill_id", name="uk_agent_skill"),
        Index("idx_agent_skill_enabled", "agent_code", "is_enabled"),
        {"comment": "Agent to Skill bindings"},
    )


class SkillAuditLog(Base):
    __tablename__ = "skill_audit_logs"

    audit_id = Column(_AUTOINCREMENT_ID, primary_key=True, autoincrement=True)
    skill_id = Column(String(36), nullable=True, index=True)
    version_id = Column(String(36), nullable=True, index=True)
    actor_user_id = Column(String(36), nullable=False, index=True)
    action = Column(String(32), nullable=False, index=True)
    target_type = Column(String(32), nullable=True)
    target_id = Column(String(128), nullable=True)
    detail_json = Column(JSON, nullable=True)
    request_id = Column(String(64), nullable=True)
    created_at = Column(DateTime, nullable=False, default=datetime.utcnow, index=True)

    __table_args__ = ({"comment": "Skill management audit trail"},)
