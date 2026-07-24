"""Canonical state enums for the self-ingestion pipeline.

单一定义源，避免 pipeline / delete_service / retriever / repair_worker
各自硬编码字符串导致命名漂移（如 staged vs staging、active vs visible）。
"""

from __future__ import annotations

# ---- SelfIngestJob.status ----
JOB_PENDING = "pending"
JOB_PROCESSING = "processing"
JOB_COMMITTING = "committing"
JOB_COMPLETED = "completed"
JOB_PARTIAL_SUCCESS = "partial_success"
JOB_FAILED = "failed"
JOB_REPAIRING = "repairing"

JOB_ACTIVE_STATUSES = (JOB_PROCESSING, JOB_COMMITTING)
JOB_TERMINAL_FAILED_STATUSES = (JOB_FAILED, JOB_PARTIAL_SUCCESS)

# ---- SelfIngestJob.stage ----
STAGE_STARTED = "started"
STAGE_PARSING = "parsing"
STAGE_CHUNKING = "chunking"
STAGE_EXTRACTING = "extracting"
STAGE_EMBEDDING = "embedding"
STAGE_STAGING = "staging"
STAGE_COMMITTING = "committing"
STAGE_COMMITTING_MYSQL = "committing_mysql"
STAGE_COMMITTING_MILVUS = "committing_milvus"
STAGE_COMMITTING_NEBULA = "committing_nebula"
STAGE_COMMITTING_REDIS = "committing_redis"
STAGE_MARKING_VISIBLE = "marking_visible"
STAGE_COMMITTED = "committed"
STAGE_FAILED = "failed"
STAGE_CLEANUP_COMPLETED = "cleanup_completed"

# ---- SelfIngestChunk.doc_status / KnowledgeBaseDocument.doc_status ----
# 严格对齐方案附录 H.2/H.3 与 18.2/18.3 检索过滤口径。
DOC_STAGED = "staged"          # 已写入但未对外可见
DOC_VISIBLE = "visible"        # 已对外可见（Phase-1 简化值，等同 active）
DOC_ACTIVE = "active"          # 方案目标可见态（兼容保留）
DOC_SUPERSEDED = "superseded"  # 被新版本替代
DOC_DELETED = "deleted"        # 已软删除
DOC_FAILED = "failed"          # 入库失败
DOC_ARCHIVED = "archived"      # 已归档

# 检索允许返回的 doc_status 集合（DirectRetriever v2 过滤口径）
RETRIEVAL_VISIBLE_DOC_STATUSES = (DOC_VISIBLE, DOC_ACTIVE)

# ---- commit_status（Milvus / Redis 标量字段） ----
COMMIT_STAGED = "staged"
COMMIT_WRITING = "writing"
COMMIT_COMMITTED = "committed"
COMMIT_FAILED = "failed"

# ---- EntityNode.status / RelationEdge.status ----
ENTITY_ACTIVE = "active"
ENTITY_WEAK_EVIDENCE = "weak_evidence"
ENTITY_CANDIDATE = "candidate"
ENTITY_INACTIVE = "inactive"
ENTITY_ORPHANED = "orphaned"   # 方案 19.1 第6条：support_count=0

RELATION_ACTIVE = "active"
RELATION_WEAK_EVIDENCE = "weak_evidence"
RELATION_INACTIVE = "inactive"

# 实体/关系证据 occurrence 状态
OCCURRENCE_ACTIVE = "active"
OCCURRENCE_SUPERSEDED = "superseded"
OCCURRENCE_DELETED = "deleted"
OCCURRENCE_INACTIVE = "inactive"

# 检索允许返回的实体/关系状态集合
RETRIEVAL_ENTITY_STATUSES = (ENTITY_ACTIVE, ENTITY_WEAK_EVIDENCE)
RETRIEVAL_RELATION_STATUSES = (RELATION_ACTIVE, RELATION_WEAK_EVIDENCE)

# ---- embedding_status ----
EMBEDDING_PENDING = "pending"
EMBEDDING_INDEXED = "indexed"
EMBEDDING_MYSQL_ONLY = "mysql_only"
EMBEDDING_FAILED = "failed"
