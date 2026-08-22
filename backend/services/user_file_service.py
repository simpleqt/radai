"""用户文件服务：文件登记、权限校验与下载审计。"""

from __future__ import annotations

import asyncio
import hashlib
import mimetypes
import uuid
from dataclasses import dataclass
from pathlib import Path
from typing import Any, Awaitable, Callable

from sqlalchemy import text
from sqlalchemy.exc import IntegrityError
from sqlalchemy.ext.asyncio import AsyncSession
from backend.config.loggers import get_logger
from backend.core.config import get_paths, parse_internal_thread_key
from backend.db.connection import get_async_session_factory, get_session_factory
from backend.db.models import UserFile
from backend.services.chat_file_parser import (
    ChatFileParser,
    ParseResult,
    clean_surrogates,
    compress_image_if_needed,
    replace_local_images_with_urls,
)
from backend.storage import (
    _get_minio_client,
    get_user_minio_bucket_name,
    upload_bytes_to_minio,
)

logger = get_logger(__name__)

ALLOWED_UPLOAD_EXTENSIONS: set[str] = {
    ".jpg", ".jpeg", ".png", ".gif", ".webp", ".bmp", ".svg",
    ".txt", ".md", ".csv", ".json", ".py", ".html", ".xml", ".kml",
    ".log", ".yaml", ".yml", ".toml", ".rst",
    ".pdf", ".docx", ".doc", ".pptx", ".ppt", ".xlsx", ".xls",
}

UPLOAD_STRATEGY_BY_CATEGORY = {
    "multimodal": "vision",
    "short_doc": "context",
    "long_doc": "indexed",
}

ImageUrlBuilder = Callable[[str, str], str]
UploadedObjectRecorder = Callable[[str], None]
ExtraImageUrlLoader = Callable[
    [str, str, ParseResult, UploadedObjectRecorder], Awaitable[list[dict]]
]
ParsedCallback = Callable[[str, ParseResult], Awaitable[None]]


def _safe_object_component(name: str, *, fallback: str) -> str:
    """净化用户提供的文件名,使其可安全用作 MinIO 对象键的一段。

    multipart filename 完全由客户端控制,可含 ``..``、前导 ``/``、控制字符;
    直接拼进 ``users/{owner}/...`` 对象键时,唯一防线只剩 S3/MinIO 服务端对
    坏路径组件的拒绝(各后端/版本行为不一)。取 basename、剔除路径分隔与控制
    字符、限长,得到确定安全的一段(R4-SV-3)。
    """
    base = Path(str(name or "")).name
    cleaned = "".join(
        ch for ch in base if ch.isprintable() and ch not in '/\\"\r\n\t'
    ).strip().strip(".")
    return cleaned[:200] or fallback


def _artifact_key(*parts: str) -> str:
    canonical = "\0".join(str(part or "") for part in parts)
    return hashlib.sha256(canonical.encode("utf-8")).hexdigest()


async def _commit_artifact_insert_async(
    session: AsyncSession,
    statement,
    params: dict[str, Any],
    artifact_key: str,
) -> str:
    try:
        await session.execute(statement, params)
        await session.commit()
        return str(params["file_id"])
    except IntegrityError:
        await session.rollback()
        existing_id = await session.scalar(
            text(
                "SELECT file_id FROM user_files "
                "WHERE artifact_key = :artifact_key LIMIT 1"
            ),
            {"artifact_key": artifact_key},
        )
        if not existing_id:
            raise
        return str(existing_id)


def _commit_artifact_insert_sync(
    session,
    statement,
    params: dict[str, Any],
    artifact_key: str,
) -> str:
    try:
        session.execute(statement, params)
        session.commit()
        return str(params["file_id"])
    except IntegrityError:
        session.rollback()
        existing_id = session.scalar(
            text(
                "SELECT file_id FROM user_files "
                "WHERE artifact_key = :artifact_key LIMIT 1"
            ),
            {"artifact_key": artifact_key},
        )
        if not existing_id:
            raise
        return str(existing_id)


class UserFileParseError(Exception):
    """上传文件解析失败。"""

    def __init__(self, message: str):
        super().__init__(message)
        self.message = message


class UserFileServiceError(Exception):
    """用户文件服务持久化失败。"""


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


@dataclass
class StoredUploadedUserFile:
    user_file: UserFile
    file_id: str
    display_name: str
    size_bytes: int
    mime_type: str | None
    category: str
    strategy: str
    content: str | None
    content_length: int
    table_analysis_enabled: bool
    metadata: dict[str, Any]
    parse_result: ParseResult
    storage_bucket: str
    uploaded_object_keys: tuple[str, ...]


def _build_download_url(file_id: str) -> str:
    return f"/api/v2/files/{file_id}/download"


async def cleanup_uploaded_minio_objects(
    bucket_name: str,
    object_keys: list[str] | tuple[str, ...],
) -> None:
    """Best-effort rollback for objects created by one upload operation."""
    if not any(str(key or "").strip().lstrip("/") for key in object_keys):
        return
    try:
        # 首次惰性初始化会做 bucket_exists/make_bucket 网络往返并持线程锁,
        # 不能留在事件循环上(R4-SV-15)。
        client = await asyncio.to_thread(_get_minio_client)
    except Exception as exc:
        logger.exception(
            "[UserFileService] 初始化 MinIO 回滚客户端失败: bucket=%s error=%s",
            bucket_name,
            exc,
        )
        return
    seen: set[str] = set()
    for raw_key in reversed(object_keys):
        object_key = str(raw_key or "").strip().lstrip("/")
        if not object_key or object_key in seen:
            continue
        seen.add(object_key)
        try:
            await asyncio.to_thread(client.remove_object, bucket_name, object_key)
        except Exception as exc:
            logger.exception(
                "[UserFileService] 回滚 MinIO 对象失败: bucket=%s object=%s error=%s",
                bucket_name,
                object_key,
                exc,
            )


async def _prepare_uploaded_user_file(
    *,
    session: AsyncSession,
    owner_user_id: str,
    data: bytes,
    filename: str,
    mime_type: str | None,
    storage_scope: str,
    image_url_builder: ImageUrlBuilder,
    thread_id: str | None = None,
    parse_mode: str = "auto",
    parser: ChatFileParser | None = None,
    on_parsed: ParsedCallback | None = None,
    extra_image_url_loader: ExtraImageUrlLoader | None = None,
    replace_content_image_urls: bool = False,
    include_parse_metadata: bool = False,
    uploaded_object_keys: list[str],
) -> StoredUploadedUserFile:
    """解析、上传并登记用户文件；调用方负责 commit。"""
    if storage_scope not in {"personal", "chat"}:
        raise ValueError(f"Unsupported storage_scope: {storage_scope}")
    if storage_scope == "chat" and not thread_id:
        raise ValueError("thread_id is required for chat file uploads")

    file_id = f"uf_{uuid.uuid4().hex}"
    file_size = len(data)
    parser = parser or ChatFileParser()

    try:
        parse_result = await parser.parse(
            data,
            filename,
            mime_type,
            parse_mode=parse_mode,
        )
    except Exception as exc:
        logger.exception("[UserFileService] 文件解析异常: %s", exc)
        parse_result = None

    if parse_result is None or parse_result.category == "failed":
        error_msg = parse_result.error if parse_result else "解析服务异常"
        raise UserFileParseError(error_msg)

    if on_parsed is not None:
        await on_parsed(file_id, parse_result)

    strategy = UPLOAD_STRATEGY_BY_CATEGORY.get(parse_result.category, "none")
    bucket_name = get_user_minio_bucket_name()
    if storage_scope == "chat":
        base_prefix = f"users/{owner_user_id}/threads/{thread_id}/chat-files/{file_id}"
    else:
        base_prefix = f"users/{owner_user_id}/personal-files/{file_id}"
    safe_filename = _safe_object_component(filename, fallback=f"{file_id}.bin")
    storage_object = f"{base_prefix}/{safe_filename}"

    # Record ownership before the blocking upload starts. The storage helper
    # waits for an in-flight thread on cancellation, so compensation can then
    # safely delete this unique key even when the await is interrupted.
    uploaded_object_keys.append(storage_object)
    uploaded = await upload_bytes_to_minio(
        content=data,
        object_name=storage_object,
        content_type=mime_type or "application/octet-stream",
        bucket_name=bucket_name,
    )
    if not uploaded:
        raise UserFileServiceError("文件上传对象存储失败")

    image_urls: list[dict] = []
    for raw_image in parse_result.images or []:
        image = await asyncio.to_thread(compress_image_if_needed, raw_image)
        safe_image_name = _safe_object_component(
            image.name, fallback=f"image_{len(image_urls)}.png"
        )
        image_object = f"{base_prefix}/images/{safe_image_name}"
        uploaded_object_keys.append(image_object)
        image_uploaded = await upload_bytes_to_minio(
            content=image.data,
            object_name=image_object,
            content_type=image.content_type,
            bucket_name=bucket_name,
        )
        if not image_uploaded:
            raise UserFileServiceError("文件图片上传对象存储失败")
        # name/url 必须与已净化的对象键一致,否则回取会按存储名重算出不同的
        # 对象键而落空(R4-SV-3)。
        image_urls.append(
            {
                "url": image_url_builder(file_id, safe_image_name),
                "name": safe_image_name,
                "caption": image.caption,
                "content_type": image.content_type,
                "storage_object": image_object,
            }
        )

    if extra_image_url_loader is not None and getattr(parse_result, "image_urls", None):
        image_urls.extend(
            await extra_image_url_loader(
                file_id,
                bucket_name,
                parse_result,
                uploaded_object_keys.append,
            )
        )

    metadata_payload: dict[str, Any] = {
        "file_category": parse_result.category,
        "strategy": strategy,
        "content": parse_result.content,
        "content_length": parse_result.content_length,
    }
    if include_parse_metadata:
        metadata_payload["parse_mode"] = parse_mode
        metadata_payload["parse_method"] = parse_result.method

    from backend.services.table_analysis_files import build_table_analysis_metadata

    table_analysis_meta = build_table_analysis_metadata(
        file_id=file_id,
        file_name=filename,
        mime_type=parse_result.mime_type or mime_type,
        storage_bucket=bucket_name,
        storage_object=storage_object,
    )
    if table_analysis_meta:
        metadata_payload["table_analysis"] = table_analysis_meta

    if replace_content_image_urls and metadata_payload.get("content") and image_urls:
        image_url_map: dict[str, str] = {}
        for image_info in image_urls:
            url = image_info.get("url")
            if not url:
                continue
            for key in {
                image_info.get("name", ""),
                Path(image_info.get("name", "")).name,
            }:
                if key:
                    image_url_map.setdefault(key, url)
        if image_url_map:
            metadata_payload["content"] = replace_local_images_with_urls(
                str(metadata_payload["content"]),
                image_url_map,
            )

    if image_urls:
        metadata_payload["image_urls"] = image_urls

    metadata_payload = clean_surrogates(metadata_payload)

    db_file = UserFile(
        file_id=file_id,
        owner_user_id=owner_user_id,
        thread_id=thread_id,
        source_type="minio",
        storage_bucket=bucket_name,
        storage_object=storage_object,
        display_name=filename,
        mime_type=parse_result.mime_type or mime_type,
        size_bytes=file_size,
        status="active",
        metadata_=metadata_payload,
    )
    session.add(db_file)

    return StoredUploadedUserFile(
        user_file=db_file,
        file_id=file_id,
        display_name=filename,
        size_bytes=file_size,
        mime_type=parse_result.mime_type or mime_type,
        category=parse_result.category,
        strategy=strategy,
        content=parse_result.content,
        content_length=parse_result.content_length,
        table_analysis_enabled=bool(table_analysis_meta),
        metadata=metadata_payload,
        parse_result=parse_result,
        storage_bucket=bucket_name,
        uploaded_object_keys=tuple(uploaded_object_keys),
    )


async def prepare_uploaded_user_file(
    *,
    session: AsyncSession,
    owner_user_id: str,
    data: bytes,
    filename: str,
    mime_type: str | None,
    storage_scope: str,
    image_url_builder: ImageUrlBuilder,
    thread_id: str | None = None,
    parse_mode: str = "auto",
    parser: ChatFileParser | None = None,
    on_parsed: ParsedCallback | None = None,
    extra_image_url_loader: ExtraImageUrlLoader | None = None,
    replace_content_image_urls: bool = False,
    include_parse_metadata: bool = False,
) -> StoredUploadedUserFile:
    """Parse, upload, and register a file with compensating object cleanup."""
    uploaded_object_keys: list[str] = []
    bucket_name = get_user_minio_bucket_name()
    try:
        return await _prepare_uploaded_user_file(
            session=session,
            owner_user_id=owner_user_id,
            data=data,
            filename=filename,
            mime_type=mime_type,
            storage_scope=storage_scope,
            image_url_builder=image_url_builder,
            thread_id=thread_id,
            parse_mode=parse_mode,
            parser=parser,
            on_parsed=on_parsed,
            extra_image_url_loader=extra_image_url_loader,
            replace_content_image_urls=replace_content_image_urls,
            include_parse_metadata=include_parse_metadata,
            uploaded_object_keys=uploaded_object_keys,
        )
    except BaseException:
        await cleanup_uploaded_minio_objects(bucket_name, uploaded_object_keys)
        raise


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

    outputs_dir = await asyncio.to_thread(
        get_paths().thread_artifacts_dir(internal_thread_id).resolve
    )
    local_path = await asyncio.to_thread((outputs_dir / safe_name).resolve)
    try:
        local_path.relative_to(outputs_dir)
    except ValueError:
        logger.warning("[UserFileService] 非法工件路径，已拒绝登记: %s", artifact_filename)
        return None

    exists, is_file = await asyncio.gather(
        asyncio.to_thread(local_path.exists),
        asyncio.to_thread(local_path.is_file),
    )
    if not exists or not is_file:
        logger.warning("[UserFileService] 工件文件不存在，无法登记: %s", local_path)
        return None

    mime_type, _ = mimetypes.guess_type(str(local_path))
    file_size = (await asyncio.to_thread(local_path.stat)).st_size
    artifact_key = _artifact_key(
        "local", user_id, external_thread_id, str(local_path)
    )

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
        file_id = await _commit_artifact_insert_async(
            session,
            text(
                """
                INSERT INTO user_files (
                    file_id,
                    owner_user_id,
                    thread_id,
                    source_type,
                    local_abs_path,
                    artifact_key,
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
                    :artifact_key,
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
                "artifact_key": artifact_key,
                "display_name": Path(safe_name).name,
                "mime_type": mime_type,
                "size_bytes": file_size,
            },
            artifact_key,
        )
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
    artifact_key = _artifact_key(
        "minio", user_id, external_thread_id, bucket_name, object_name
    )

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
        file_id = _commit_artifact_insert_sync(
            session,
            text(
                """
                INSERT INTO user_files (
                    file_id,
                    owner_user_id,
                    thread_id,
                    source_type,
                    storage_bucket,
                    storage_object,
                    artifact_key,
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
                    :artifact_key,
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
                "artifact_key": artifact_key,
                "display_name": Path(safe_name).name,
                "mime_type": mime_type,
            },
            artifact_key,
        )
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
    """用独立事务写入下载审计，确保 allow/deny 记录必定落库。

    下载/元数据端点是 GET，其请求级会话（``session``）在安全方法上一律回滚
    （只读事务卫生），复用它会把审计 INSERT 一并回滚——``file_download_audit``
    对真实下载将永远为空。因此审计改走独立会话并显式提交；``session`` 形参保留
    以兼容既有调用点但不再用于写入。写入失败仅告警，不阻断下载本身。
    """
    factory = get_async_session_factory()
    try:
        async with factory() as audit_session:
            await audit_session.execute(
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
            await audit_session.commit()
    except Exception:
        logger.warning(
            "下载审计写入失败: file_id=%s decision=%s",
            file_id,
            decision,
            exc_info=True,
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
