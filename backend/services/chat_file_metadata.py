"""Shared metadata access for uploaded conversation files."""

from __future__ import annotations

import hashlib
import hmac
import time
from urllib.parse import quote, urlparse

from sqlalchemy import select

from backend.config.settings import settings
from backend.db.connection import get_session_factory
from backend.db.models import SessionFile, UserFile

CHAT_FILE_IMAGE_URL_TTL_SECONDS = 7 * 24 * 60 * 60


def build_chat_image_signature(
    *,
    file_id: str,
    image_name: str,
    owner_user_id: str,
    thread_id: str,
    expires_at: int,
) -> str:
    secret = (settings.minio_secret_key or "").encode("utf-8")
    payload = "\n".join(
        [file_id, image_name, owner_user_id, thread_id, str(expires_at)]
    ).encode("utf-8")
    return hmac.new(secret, payload, hashlib.sha256).hexdigest()


def db_row_to_meta(row: UserFile) -> dict:
    metadata = row.metadata_ or {}
    result = {
        "file_id": row.file_id,
        "thread_id": row.thread_id,
        "user_id": row.owner_user_id,
        "file_name": row.display_name,
        "file_size": row.size_bytes or 0,
        "mime_type": row.mime_type,
        "file_category": metadata.get("file_category", "short_doc"),
        "strategy": metadata.get("strategy", "context"),
        "content": metadata.get("content", ""),
        "content_length": metadata.get("content_length", 0),
        "storage_bucket": row.storage_bucket,
        "storage_object": row.storage_object,
        "local_abs_path": row.local_abs_path,
    }
    for key in ("table_analysis",):
        if key in metadata:
            result[key] = metadata[key]
    if "image_urls" in metadata:
        result["image_urls"] = _refresh_image_urls(row, metadata["image_urls"] or [])
    return result


def _refresh_image_urls(row: UserFile, image_urls: list[dict]) -> list[dict]:
    refreshed: list[dict] = []
    for item in image_urls:
        current = dict(item)
        image_name = str(current.get("name") or "").strip()
        existing_url = str(current.get("url") or "").strip()
        parsed = urlparse(existing_url)
        if image_name and parsed.scheme and parsed.netloc:
            expires_at = int(time.time()) + CHAT_FILE_IMAGE_URL_TTL_SECONDS
            signature = build_chat_image_signature(
                file_id=row.file_id,
                image_name=image_name,
                owner_user_id=row.owner_user_id,
                thread_id=row.thread_id,
                expires_at=expires_at,
            )
            current["url"] = (
                f"{parsed.scheme}://{parsed.netloc}/api/v2/chat-files/{quote(row.file_id)}"
                f"/images/{quote(image_name, safe='')}?exp={expires_at}&sig={signature}"
            )
        refreshed.append(current)
    return refreshed


def get_file_meta(file_id: str) -> dict | None:
    factory = get_session_factory()
    with factory() as session:
        row = session.execute(
            select(UserFile).where(
                UserFile.file_id == file_id,
                UserFile.status == "active",
            )
        ).scalars().first()
        return db_row_to_meta(row) if row else None


def get_files_by_thread(thread_id: str, user_id: str) -> list[dict]:
    factory = get_session_factory()
    with factory() as session:
        rows = session.execute(
            select(UserFile)
            .join(SessionFile, SessionFile.file_id == UserFile.file_id)
            .where(
                SessionFile.session_id == thread_id,
                SessionFile.user_id == user_id,
                UserFile.status == "active",
            )
        ).scalars().all()
        return [db_row_to_meta(row) for row in rows]
