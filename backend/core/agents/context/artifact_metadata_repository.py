"""Best-effort persistence for context artifact metadata."""

from __future__ import annotations

import logging
from datetime import datetime
from typing import Any

from backend.core.agents.context.artifact_store import ContextArtifactMetadata

logger = logging.getLogger(__name__)


def record_context_artifact_metadata(
    metadata: ContextArtifactMetadata,
    *,
    preview: str | None = None,
) -> None:
    """Persist artifact metadata when the deployment enables MySQL recording.

    The context artifact itself is authoritative; this table is an index for
    audit/search/debugging. Failures are deliberately non-fatal because large
    context persistence must not break an agent run if the metadata table has
    not been migrated yet.
    """

    try:
        from backend.db.connection import get_session
        from backend.db.models import ContextArtifactRecord

        session = get_session()
    except Exception:
        logger.debug("[ContextArtifact] metadata session unavailable", exc_info=True)
        return

    try:
        existing = (
            session.query(ContextArtifactRecord)
            .filter(ContextArtifactRecord.raw_ref == metadata.raw_ref)
            .one_or_none()
        )
        record = existing or ContextArtifactRecord(raw_ref=metadata.raw_ref)
        attributes: dict[str, Any] = dict(metadata.attributes or {})
        record.storage_backend = str(attributes.get("storage") or "local")
        record.bucket = attributes.get("bucket")
        record.object_key = attributes.get("object_name") or attributes.get("object_key")
        record.thread_id = metadata.thread_id
        record.run_id = attributes.get("run_id")
        record.unit_id = attributes.get("unit_id")
        record.source_tool = metadata.source_tool
        record.size_chars = metadata.size_chars
        record.sha256 = metadata.sha256
        record.suffix = metadata.suffix
        record.attributes = attributes
        record.preview = (preview or "")[:1200] or None
        record.created_at = datetime.fromtimestamp(metadata.created_at)
        if existing is None:
            session.add(record)
        session.commit()
    except Exception:
        session.rollback()
        logger.debug("[ContextArtifact] metadata write skipped", exc_info=True)
    finally:
        session.close()
