"""Helpers for resolving uploaded table files for analysis tools."""

from __future__ import annotations

from pathlib import Path
from typing import Any

from sqlalchemy import select

from backend.config.loggers import get_logger
from backend.db.connection import get_session_factory
from backend.db.models import SessionFile, UserFile

logger = get_logger(__name__)

TABLE_ANALYSIS_EXTENSIONS: set[str] = {".csv", ".xls", ".xlsx"}
TABLE_ANALYSIS_MIME_HINTS: set[str] = {
    "text/csv",
    "application/csv",
    "application/vnd.ms-excel",
    "application/vnd.openxmlformats-officedocument.spreadsheetml.sheet",
}


def is_table_analysis_file(filename: str, mime_type: str | None = None) -> bool:
    """Return whether a file should be treated as a structured table source."""
    ext = Path(str(filename or "")).suffix.lower()
    if ext in TABLE_ANALYSIS_EXTENSIONS:
        return True
    mime = str(mime_type or "").split(";")[0].strip().lower()
    return bool(mime and mime in TABLE_ANALYSIS_MIME_HINTS)


def build_table_analysis_metadata(
    *,
    file_id: str,
    file_name: str,
    mime_type: str | None = None,
    storage_bucket: str | None = None,
    storage_object: str | None = None,
    local_abs_path: str | None = None,
) -> dict[str, Any] | None:
    """Build metadata for table-capable files, or None for non-table files."""
    if not is_table_analysis_file(file_name, mime_type):
        return None
    return {
        "enabled": True,
        "source_type": "chat_file",
        "file_id": file_id,
        "file_name": file_name,
        "ext": Path(file_name).suffix.lower(),
        "storage_bucket": storage_bucket,
        "storage_object": storage_object,
        "local_abs_path": local_abs_path,
    }


def _row_is_table_enabled(row: UserFile) -> bool:
    metadata = row.metadata_ or {}
    table_meta = metadata.get("table_analysis") or {}
    if isinstance(table_meta, dict) and table_meta.get("enabled"):
        return True
    return is_table_analysis_file(row.display_name, row.mime_type)


def resolve_table_file_source(file_id: str) -> tuple[str, dict[str, Any]]:
    """Resolve a user file id to a source accepted by the Excel engine.

    Returns:
        (source, info). source is either a local absolute path or a MinIO object
        name. The Excel engine already knows how to download MinIO objects into
        its local cache.
    """
    normalized_id = str(file_id or "").strip()
    if not normalized_id:
        raise ValueError("file_id 不能为空")

    factory = get_session_factory()
    with factory() as session:
        row = session.execute(
            select(UserFile).where(
                UserFile.file_id == normalized_id,
                UserFile.status == "active",
            )
        ).scalars().first()

    if not row:
        raise FileNotFoundError(f"文件不存在或不可用: {normalized_id}")
    if not _row_is_table_enabled(row):
        raise ValueError(f"文件不是可分析表格: {row.display_name}")

    metadata = row.metadata_ or {}
    table_meta = metadata.get("table_analysis") if isinstance(metadata, dict) else {}
    table_meta = table_meta if isinstance(table_meta, dict) else {}

    local_path = row.local_abs_path or table_meta.get("local_abs_path")
    if local_path and Path(str(local_path)).exists():
        source = str(local_path)
    else:
        storage_object = row.storage_object or table_meta.get("storage_object")
        storage_bucket = row.storage_bucket or table_meta.get("storage_bucket")
        if storage_object and storage_bucket:
            source = f"minio://{storage_bucket}/{str(storage_object).lstrip('/')}"
        else:
            source = storage_object

    if not source:
        raise FileNotFoundError(f"文件缺少可分析来源: {row.display_name}")

    info = {
        "file_id": row.file_id,
        "file_name": row.display_name,
        "mime_type": row.mime_type,
        "storage_bucket": row.storage_bucket or table_meta.get("storage_bucket"),
        "storage_object": row.storage_object or table_meta.get("storage_object"),
        "local_abs_path": local_path,
        "source": source,
    }
    return str(source), info


def list_table_files_by_thread(thread_id: str, user_id: str | None = None) -> list[dict[str, Any]]:
    """List table-capable files attached to a conversation thread."""
    normalized_thread = str(thread_id or "").strip()
    if not normalized_thread:
        return []

    factory = get_session_factory()
    with factory() as session:
        stmt = (
            select(UserFile)
            .join(SessionFile, SessionFile.file_id == UserFile.file_id)
            .where(SessionFile.session_id == normalized_thread)
            .where(UserFile.status == "active")
            .order_by(SessionFile.created_at.asc(), UserFile.created_at.asc())
        )
        if user_id:
            stmt = stmt.where(SessionFile.user_id == user_id)
        rows = session.execute(stmt).scalars().all()

    items: list[dict[str, Any]] = []
    for row in rows:
        if not _row_is_table_enabled(row):
            continue
        metadata = row.metadata_ or {}
        table_meta = metadata.get("table_analysis") if isinstance(metadata, dict) else {}
        table_meta = table_meta if isinstance(table_meta, dict) else {}
        items.append({
            "file_id": row.file_id,
            "file_name": row.display_name,
            "mime_type": row.mime_type,
            "storage_bucket": row.storage_bucket or table_meta.get("storage_bucket"),
            "storage_object": row.storage_object or table_meta.get("storage_object"),
            "local_abs_path": row.local_abs_path or table_meta.get("local_abs_path"),
        })
    return items
