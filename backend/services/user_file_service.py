"""用户文件服务：文件登记、权限校验与下载审计。"""

from __future__ import annotations

import mimetypes
import uuid
from dataclasses import dataclass
from pathlib import Path

from sqlalchemy import text
from sqlalchemy.ext.asyncio import AsyncSession
from sqlalchemy.orm import Session

from backend.config.loggers import get_logger
from backend.db.connection import get_async_session_factory, get_session_factory
from backend.core.config import get_paths, parse_internal_thread_key

logger = get_logger(__name__)


@dataclass
class UserFileResolved:
    file_id: str
    owner_user_id: str
    thread_id: str | None
    display_name: str
    mime_type: str | None
    size_bytes: int | None
    source_type: str
    storage_bucket: str | None
    storage_object: str | None
    local_abs_path: str | None
    status: str


def _build_download_url(file_id: str) -> str:
    return f"/api/v2/files/{file_id}/download"


async def list_active_files_for_thread_for_user(
    *,
    session: AsyncSession,
    current_user_id: str,
    thread_id: str,
    limit: int = 50,
    offset: int = 0,
) -> list[dict]:
    """列出当前用户在指定会话下可见的 active 文件。"""
    safe_thread_id = (thread_id or "").strip()
    if not safe_thread_id:
        return []

    safe_limit = max(1, min(int(limit or 50), 200))
    safe_offset = max(0, int(offset or 0))

    sql = """
        SELECT
            uf.file_id,
            uf.thread_id,
            uf.display_name,
            uf.mime_type,
            uf.size_bytes,
            uf.source_type,
            uf.storage_bucket,
            uf.storage_object,
            uf.local_abs_path,
            uf.created_at,
            uf.updated_at
        FROM user_files uf
        WHERE uf.owner_user_id = :current_user_id
          AND uf.thread_id = :thread_id
          AND uf.status = 'active'
        ORDER BY uf.id DESC
        LIMIT :limit OFFSET :offset
    """
    result = await session.execute(
        text(sql),
        {
            "current_user_id": current_user_id,
            "thread_id": safe_thread_id,
            "limit": safe_limit,
            "offset": safe_offset,
        },
    )
    rows = result.mappings().all()

    items: list[dict] = []
    for row in rows:
        file_id = str(row["file_id"])
        items.append(
            {
                "file_id": file_id,
                "thread_id": str(row["thread_id"]) if row.get("thread_id") else None,
                "display_name": str(row["display_name"]),
                "mime_type": str(row["mime_type"]) if row.get("mime_type") else None,
                "size_bytes": int(row["size_bytes"]) if row.get("size_bytes") is not None else None,
                "source_type": str(row["source_type"]),
                "storage_bucket": str(row["storage_bucket"]) if row.get("storage_bucket") else None,
                "storage_object": str(row["storage_object"]) if row.get("storage_object") else None,
                "local_abs_path": str(row["local_abs_path"]) if row.get("local_abs_path") else None,
                "download_url": _build_download_url(file_id),
                "created_at": row["created_at"].isoformat() if row.get("created_at") else None,
                "updated_at": row["updated_at"].isoformat() if row.get("updated_at") else None,
            }
        )

    return items


async def register_local_artifact_for_thread(
    *,
    internal_thread_id: str,
    artifact_filename: str,
) -> tuple[str, str] | None:
    """将线程输出目录中的工件登记为用户文件，并返回(file_id, download_url)。"""
    user_id, external_thread_id = parse_internal_thread_key(internal_thread_id)
    if not user_id:
        return None

    safe_name = (artifact_filename or "").strip().lstrip("/")
    if not safe_name:
        return None

    outputs_dir = get_paths().thread_artifacts_dir(internal_thread_id).resolve()
    local_path = (outputs_dir / safe_name).resolve()
    try:
        local_path.relative_to(outputs_dir)
    except ValueError:
        logger.warning("[UserFileService] 非法工件路径，已拒绝登记: %s", artifact_filename)
        return None

    if not local_path.exists() or not local_path.is_file():
        logger.warning("[UserFileService] 工件文件不存在，无法登记: %s", local_path)
        return None

    mime_type, _ = mimetypes.guess_type(str(local_path))
    file_size = local_path.stat().st_size

    factory = get_async_session_factory()
    async with factory() as session:
        existing = await session.execute(
            text(
                """
                SELECT file_id
                FROM user_files
                WHERE owner_user_id = :owner_user_id
                  AND thread_id = :thread_id
                  AND local_abs_path = :local_abs_path
                  AND status = 'active'
                ORDER BY id DESC
                LIMIT 1
                """
            ),
            {
                "owner_user_id": user_id,
                "thread_id": external_thread_id,
                "local_abs_path": str(local_path),
            },
        )
        row = existing.mappings().first()
        if row and row.get("file_id"):
            file_id = str(row["file_id"])
            return file_id, _build_download_url(file_id)

        file_id = f"uf_{uuid.uuid4().hex}"
        await session.execute(
            text(
                """
                INSERT INTO user_files (
                    file_id,
                    owner_user_id,
                    thread_id,
                    source_type,
                    local_abs_path,
                    display_name,
                    mime_type,
                    size_bytes,
                    status
                ) VALUES (
                    :file_id,
                    :owner_user_id,
                    :thread_id,
                    'local',
                    :local_abs_path,
                    :display_name,
                    :mime_type,
                    :size_bytes,
                    'active'
                )
                """
            ),
            {
                "file_id": file_id,
                "owner_user_id": user_id,
                "thread_id": external_thread_id,
                "local_abs_path": str(local_path),
                "display_name": Path(safe_name).name,
                "mime_type": mime_type,
                "size_bytes": file_size,
            },
        )
        await session.commit()
        return file_id, _build_download_url(file_id)


async def register_minio_artifact_for_thread(
    *,
    internal_thread_id: str,
    artifact_filename: str,
    storage_bucket: str,
    storage_object: str,
    mime_type: str | None = "text/markdown",
) -> tuple[str, str] | None:
    """将 MinIO 对象登记为用户文件，并返回(file_id, download_url)。"""
    user_id, external_thread_id = parse_internal_thread_key(internal_thread_id)
    if not user_id:
        return None

    safe_name = (artifact_filename or "").strip().lstrip("/")
    object_name = (storage_object or "").strip().lstrip("/")
    bucket_name = (storage_bucket or "").strip()
    if not safe_name or not object_name or not bucket_name:
        return None

    factory = get_async_session_factory()
    async with factory() as session:
        existing = await session.execute(
            text(
                """
                SELECT file_id
                FROM user_files
                WHERE owner_user_id = :owner_user_id
                  AND thread_id = :thread_id
                  AND source_type = 'minio'
                  AND storage_bucket = :storage_bucket
                  AND storage_object = :storage_object
                  AND status = 'active'
                ORDER BY id DESC
                LIMIT 1
                """
            ),
            {
                "owner_user_id": user_id,
                "thread_id": external_thread_id,
                "storage_bucket": bucket_name,
                "storage_object": object_name,
            },
        )
        row = existing.mappings().first()
        if row and row.get("file_id"):
            file_id = str(row["file_id"])
            return file_id, _build_download_url(file_id)

        file_id = f"uf_{uuid.uuid4().hex}"
        await session.execute(
            text(
                """
                INSERT INTO user_files (
                    file_id,
                    owner_user_id,
                    thread_id,
                    source_type,
                    storage_bucket,
                    storage_object,
                    display_name,
                    mime_type,
                    status
                ) VALUES (
                    :file_id,
                    :owner_user_id,
                    :thread_id,
                    'minio',
                    :storage_bucket,
                    :storage_object,
                    :display_name,
                    :mime_type,
                    'active'
                )
                """
            ),
            {
                "file_id": file_id,
                "owner_user_id": user_id,
                "thread_id": external_thread_id,
                "storage_bucket": bucket_name,
                "storage_object": object_name,
                "display_name": Path(safe_name).name,
                "mime_type": mime_type,
            },
        )
        await session.commit()
        return file_id, _build_download_url(file_id)


def register_local_artifact_for_thread_sync(
    *,
    internal_thread_id: str,
    artifact_filename: str,
) -> tuple[str, str] | None:
    """同步版本：将线程输出目录中的工件登记为用户文件。"""
    user_id, external_thread_id = parse_internal_thread_key(internal_thread_id)
    if not user_id:
        return None

    safe_name = (artifact_filename or "").strip().lstrip("/")
    if not safe_name:
        return None

    outputs_dir = get_paths().thread_artifacts_dir(internal_thread_id).resolve()
    local_path = (outputs_dir / safe_name).resolve()
    try:
        local_path.relative_to(outputs_dir)
    except ValueError:
        logger.warning("[UserFileService] 非法工件路径，已拒绝登记: %s", artifact_filename)
        return None

    if not local_path.exists() or not local_path.is_file():
        logger.warning("[UserFileService] 工件文件不存在，无法登记: %s", local_path)
        return None

    mime_type, _ = mimetypes.guess_type(str(local_path))
    file_size = local_path.stat().st_size

    factory = get_session_factory()
    with factory() as session:
        existing = session.execute(
            text(
                """
                SELECT file_id
                FROM user_files
                WHERE owner_user_id = :owner_user_id
                  AND thread_id = :thread_id
                  AND local_abs_path = :local_abs_path
                  AND status = 'active'
                ORDER BY id DESC
                LIMIT 1
                """
            ),
            {
                "owner_user_id": user_id,
                "thread_id": external_thread_id,
                "local_abs_path": str(local_path),
            },
        )
        row = existing.mappings().first()
        if row and row.get("file_id"):
            file_id = str(row["file_id"])
            return file_id, _build_download_url(file_id)

        file_id = f"uf_{uuid.uuid4().hex}"
        session.execute(
            text(
                """
                INSERT INTO user_files (
                    file_id,
                    owner_user_id,
                    thread_id,
                    source_type,
                    local_abs_path,
                    display_name,
                    mime_type,
                    size_bytes,
                    status
                ) VALUES (
                    :file_id,
                    :owner_user_id,
                    :thread_id,
                    'local',
                    :local_abs_path,
                    :display_name,
                    :mime_type,
                    :size_bytes,
                    'active'
                )
                """
            ),
            {
                "file_id": file_id,
                "owner_user_id": user_id,
                "thread_id": external_thread_id,
                "local_abs_path": str(local_path),
                "display_name": Path(safe_name).name,
                "mime_type": mime_type,
                "size_bytes": file_size,
            },
        )
        session.commit()
        return file_id, _build_download_url(file_id)


def register_minio_artifact_for_thread_sync(
    *,
    internal_thread_id: str,
    artifact_filename: str,
    storage_bucket: str,
    storage_object: str,
    mime_type: str | None = "text/markdown",
) -> tuple[str, str] | None:
    """同步版本：将 MinIO 对象登记为用户文件。"""
    user_id, external_thread_id = parse_internal_thread_key(internal_thread_id)
    if not user_id:
        return None

    safe_name = (artifact_filename or "").strip().lstrip("/")
    object_name = (storage_object or "").strip().lstrip("/")
    bucket_name = (storage_bucket or "").strip()
    if not safe_name or not object_name or not bucket_name:
        return None

    factory = get_session_factory()
    with factory() as session:
        existing = session.execute(
            text(
                """
                SELECT file_id
                FROM user_files
                WHERE owner_user_id = :owner_user_id
                  AND thread_id = :thread_id
                  AND source_type = 'minio'
                  AND storage_bucket = :storage_bucket
                  AND storage_object = :storage_object
                  AND status = 'active'
                ORDER BY id DESC
                LIMIT 1
                """
            ),
            {
                "owner_user_id": user_id,
                "thread_id": external_thread_id,
                "storage_bucket": bucket_name,
                "storage_object": object_name,
            },
        )
        row = existing.mappings().first()
        if row and row.get("file_id"):
            file_id = str(row["file_id"])
            return file_id, _build_download_url(file_id)

        file_id = f"uf_{uuid.uuid4().hex}"
        session.execute(
            text(
                """
                INSERT INTO user_files (
                    file_id,
                    owner_user_id,
                    thread_id,
                    source_type,
                    storage_bucket,
                    storage_object,
                    display_name,
                    mime_type,
                    status
                ) VALUES (
                    :file_id,
                    :owner_user_id,
                    :thread_id,
                    'minio',
                    :storage_bucket,
                    :storage_object,
                    :display_name,
                    :mime_type,
                    'active'
                )
                """
            ),
            {
                "file_id": file_id,
                "owner_user_id": user_id,
                "thread_id": external_thread_id,
                "storage_bucket": bucket_name,
                "storage_object": object_name,
                "display_name": Path(safe_name).name,
                "mime_type": mime_type,
            },
        )
        session.commit()
        return file_id, _build_download_url(file_id)


async def resolve_user_file_for_download(
    *,
    session: AsyncSession,
    file_id: str,
    current_user_id: str,
) -> tuple[UserFileResolved | None, bool, str]:
    """解析下载权限。

    返回: (file_record, allowed, reason)
    reason 取值示例: ok/not_found/inactive/not_owner_no_grant
    """
    result = await session.execute(
        text(
            """
            SELECT
                uf.file_id,
                uf.owner_user_id,
                uf.thread_id,
                uf.display_name,
                uf.mime_type,
                uf.size_bytes,
                uf.source_type,
                uf.storage_bucket,
                uf.storage_object,
                uf.local_abs_path,
                uf.status
            FROM user_files uf
            WHERE uf.file_id = :file_id
            LIMIT 1
            """
        ),
        {"file_id": file_id},
    )
    row = result.mappings().first()
    if not row:
        return None, False, "not_found"

    file_row = UserFileResolved(
        file_id=str(row["file_id"]),
        owner_user_id=str(row["owner_user_id"]),
        thread_id=str(row["thread_id"]) if row.get("thread_id") else None,
        display_name=str(row["display_name"]),
        mime_type=(str(row["mime_type"]) if row.get("mime_type") else None),
        size_bytes=(int(row["size_bytes"]) if row.get("size_bytes") is not None else None),
        source_type=str(row["source_type"]),
        storage_bucket=(str(row["storage_bucket"]) if row.get("storage_bucket") else None),
        storage_object=(str(row["storage_object"]) if row.get("storage_object") else None),
        local_abs_path=(str(row["local_abs_path"]) if row.get("local_abs_path") else None),
        status=str(row["status"]),
    )

    if file_row.status != "active":
        return file_row, False, "inactive"

    if file_row.owner_user_id == current_user_id:
        return file_row, True, "ok"

    grant_result = await session.execute(
        text(
            """
            SELECT 1
            FROM user_file_permissions p
            WHERE p.file_id = :file_id
              AND p.grantee_user_id = :grantee_user_id
              AND p.permission IN ('read', 'download')
              AND (p.expires_at IS NULL OR p.expires_at > NOW())
            LIMIT 1
            """
        ),
        {"file_id": file_id, "grantee_user_id": current_user_id},
    )
    if grant_result.first():
        return file_row, True, "ok"

    return file_row, False, "not_owner_no_grant"


async def write_download_audit(
    *,
    session: AsyncSession,
    file_id: str,
    user_id: str | None,
    decision: str,
    reason: str,
    ip: str | None,
    user_agent: str | None,
) -> None:
    await session.execute(
        text(
            """
            INSERT INTO file_download_audit (
                file_id,
                user_id,
                decision,
                reason,
                ip,
                user_agent
            ) VALUES (
                :file_id,
                :user_id,
                :decision,
                :reason,
                :ip,
                :user_agent
            )
            """
        ),
        {
            "file_id": file_id,
            "user_id": user_id,
            "decision": decision,
            "reason": reason,
            "ip": ip,
            "user_agent": user_agent,
        },
    )


async def list_download_audit_for_user(
    *,
    session: AsyncSession,
    current_user_id: str,
    limit: int = 50,
    offset: int = 0,
    file_id: str | None = None,
) -> list[dict]:
    """查询与当前用户相关的下载审计记录。

    返回两类记录：
    1) 当前用户本人发起的下载请求；
    2) 他人访问当前用户拥有文件的下载请求。
    """
    safe_limit = max(1, min(int(limit or 50), 200))
    safe_offset = max(0, int(offset or 0))

    sql = """
        SELECT
            a.id,
            a.file_id,
            a.user_id,
            a.decision,
            a.reason,
            a.ip,
            a.user_agent,
            a.created_at,
            uf.owner_user_id,
            uf.thread_id,
            uf.display_name,
            uf.mime_type,
            uf.size_bytes
        FROM file_download_audit a
        JOIN user_files uf ON uf.file_id = a.file_id
        WHERE (a.user_id = :current_user_id OR uf.owner_user_id = :current_user_id)
    """
    params: dict[str, object] = {
        "current_user_id": current_user_id,
        "limit": safe_limit,
        "offset": safe_offset,
    }

    if file_id:
        sql += " AND a.file_id = :file_id "
        params["file_id"] = file_id

    sql += " ORDER BY a.id DESC LIMIT :limit OFFSET :offset "

    result = await session.execute(text(sql), params)
    rows = result.mappings().all()

    records: list[dict] = []
    for row in rows:
        actor_user_id = str(row["user_id"]) if row.get("user_id") else None
        owner_user_id = str(row["owner_user_id"]) if row.get("owner_user_id") else None
        records.append(
            {
                "id": int(row["id"]),
                "file_id": str(row["file_id"]),
                "display_name": str(row["display_name"]),
                "thread_id": (str(row["thread_id"]) if row.get("thread_id") else None),
                "mime_type": (str(row["mime_type"]) if row.get("mime_type") else None),
                "size_bytes": (int(row["size_bytes"]) if row.get("size_bytes") is not None else None),
                "actor_user_id": actor_user_id,
                "owner_user_id": owner_user_id,
                "is_owner_event": owner_user_id == current_user_id,
                "decision": str(row["decision"]),
                "reason": (str(row["reason"]) if row.get("reason") else None),
                "ip": (str(row["ip"]) if row.get("ip") else None),
                "user_agent": (str(row["user_agent"]) if row.get("user_agent") else None),
                "created_at": (
                    row["created_at"].isoformat() if row.get("created_at") else None
                ),
            }
        )

    return records
