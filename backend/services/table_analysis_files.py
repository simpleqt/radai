"""Helpers for resolving uploaded table files for analysis tools."""

from __future__ import annotations

from datetime import datetime
from pathlib import Path
from typing import Any

from sqlalchemy import or_, select
from sqlalchemy.exc import SQLAlchemyError

from backend.config.loggers import get_logger
from backend.db.connection import get_session_factory
from backend.db.models import SessionFile, UserFile, UserFilePermission

logger = get_logger(__name__)

TABLE_ANALYSIS_EXTENSIONS: set[str] = {".csv", ".xls", ".xlsx"}
TABLE_ANALYSIS_MIME_HINTS: set[str] = {
    "text/csv",
    "application/csv",
    "application/vnd.ms-excel",
    "application/vnd.openxmlformats-officedocument.spreadsheetml.sheet",
}


class TableFileAccessError(PermissionError):
    """Raised when a runtime principal cannot read a requested table file."""


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


def _granted_file_ids(session, user_id: str, file_ids: list[str]) -> set[str]:
    if not user_id or not file_ids:
        return set()
    try:
        rows = session.execute(
            select(UserFilePermission.file_id).where(
                UserFilePermission.file_id.in_(file_ids),
                UserFilePermission.grantee_user_id == user_id,
                UserFilePermission.permission.in_(("read", "download", "use")),
                or_(
                    UserFilePermission.expires_at.is_(None),
                    UserFilePermission.expires_at > datetime.now(),
                ),
            )
        ).all()
        return {str(row.file_id) for row in rows}
    except SQLAlchemyError as exc:
        # Older installations may not have the permission table yet.  Access
        # remains owner-only until the migration is applied.
        logger.warning("[TableAnalysis] 文件授权查询失败，按无授权处理: %s", exc)
        return set()


def _can_access_row(
    row: UserFile,
    *,
    user_id: str | None,
    role: str,
    granted_ids: set[str],
) -> bool:
    if str(role or "user").lower() == "admin":
        return True
    normalized_user_id = str(user_id or "").strip()
    if (
        not normalized_user_id
        or normalized_user_id == "guest"
        or normalized_user_id.startswith("guest_")
    ):
        return False
    return row.owner_user_id == normalized_user_id or row.file_id in granted_ids


def resolve_table_file_source(
    file_id: str,
    *,
    user_id: str | None = None,
    role: str = "user",
) -> tuple[str, dict[str, Any]]:
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

        granted_ids = (
            _granted_file_ids(session, str(user_id or "").strip(), [normalized_id])
            if row and row.owner_user_id != str(user_id or "").strip()
            else set()
        )

    if not row:
        raise FileNotFoundError(f"文件不存在或不可用: {normalized_id}")
    if not _can_access_row(
        row,
        user_id=user_id,
        role=role,
        granted_ids=granted_ids,
    ):
        raise TableFileAccessError("无权访问该表格文件")
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
        "owner_user_id": row.owner_user_id,
        "file_name": row.display_name,
        "mime_type": row.mime_type,
        "storage_bucket": row.storage_bucket or table_meta.get("storage_bucket"),
        "storage_object": row.storage_object or table_meta.get("storage_object"),
        "local_abs_path": local_path,
        "source": source,
    }
    return str(source), info


def resolve_table_file_reference(
    reference: str,
    *,
    user_id: str | None,
    role: str = "user",
) -> tuple[str, dict[str, Any]]:
    """Resolve a legacy path/object reference only through an authorized row."""
    normalized = str(reference or "").strip()
    if not normalized:
        raise ValueError("文件引用不能为空")

    local_candidates = {normalized}
    try:
        local_candidates.add(str(Path(normalized).expanduser().resolve()))
    except OSError:
        pass

    bucket = ""
    object_name = normalized
    if normalized.startswith("minio://"):
        remainder = normalized[len("minio://"):]
        bucket, _, object_name = remainder.partition("/")

    factory = get_session_factory()
    with factory() as session:
        predicates = [UserFile.local_abs_path.in_(local_candidates)]
        if bucket:
            predicates.append(
                (UserFile.storage_bucket == bucket)
                & (UserFile.storage_object == object_name)
            )
        else:
            predicates.append(UserFile.storage_object == object_name)
        row = session.execute(
            select(UserFile).where(
                UserFile.status == "active",
                or_(*predicates),
            )
        ).scalars().first()

    if not row:
        raise FileNotFoundError("文件来源未注册或不可用")
    return resolve_table_file_source(row.file_id, user_id=user_id, role=role)


def list_table_files_by_ids(
    file_ids: list[str],
    user_id: str | None = None,
    *,
    role: str = "user",
) -> list[dict[str, Any]]:
    """List table-capable files by explicit ids from request metadata."""
    normalized_ids = []
    seen = set()
    for raw in file_ids or []:
        file_id = str(raw or "").strip()
        if file_id and file_id not in seen:
            normalized_ids.append(file_id)
            seen.add(file_id)
    if not normalized_ids or (not user_id and str(role).lower() != "admin"):
        return []

    factory = get_session_factory()
    with factory() as session:
        stmt = (
            select(UserFile)
            .where(UserFile.file_id.in_(normalized_ids))
            .where(UserFile.status == "active")
        )
        rows = session.execute(stmt).scalars().all()
        non_owned_ids = [
            row.file_id for row in rows if row.owner_user_id != str(user_id or "")
        ]
        granted_ids = _granted_file_ids(session, str(user_id or ""), non_owned_ids)

    row_by_id = {row.file_id: row for row in rows}
    items: list[dict[str, Any]] = []
    for file_id in normalized_ids:
        row = row_by_id.get(file_id)
        if (
            not row
            or not _can_access_row(
                row,
                user_id=user_id,
                role=role,
                granted_ids=granted_ids,
            )
            or not _row_is_table_enabled(row)
        ):
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


def list_table_files_by_thread(
    thread_id: str,
    user_id: str | None = None,
    *,
    role: str = "user",
) -> list[dict[str, Any]]:
    """List table-capable files attached to a conversation thread."""
    normalized_thread = str(thread_id or "").strip()
    if not normalized_thread or (not user_id and str(role).lower() != "admin"):
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
        non_owned_ids = [
            row.file_id for row in rows if row.owner_user_id != str(user_id or "")
        ]
        granted_ids = _granted_file_ids(session, str(user_id or ""), non_owned_ids)

    items: list[dict[str, Any]] = []
    for row in rows:
        if not _can_access_row(
            row,
            user_id=user_id,
            role=role,
            granted_ids=granted_ids,
        ) or not _row_is_table_enabled(row):
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
