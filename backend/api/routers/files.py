import asyncio
import mimetypes
from pathlib import Path
from urllib.parse import quote, unquote

from fastapi import APIRouter, Depends, HTTPException, Query, Request
from fastapi.responses import FileResponse, StreamingResponse
from minio import S3Error
from pydantic import BaseModel, Field, field_validator
from sqlalchemy.ext.asyncio import AsyncSession

from backend.api.routers.auth import get_current_user
from backend.config import settings
from backend.config.loggers import get_logger
from backend.core.config import get_paths
from backend.core.utils.client_ip import UNKNOWN_CLIENT_IP, resolve_client_ip
from backend.db.connection import get_async_session
from backend.db.models import User
from backend.services.user_file_service import (
    list_active_files_for_thread_for_user,
    list_download_audit_for_user,
    resolve_user_file_for_download,
    write_download_audit,
)
from backend.storage import (
    _get_minio_client,
    get_user_minio_bucket_name,
    verify_kb_image_proxy_signature,
)
from backend.storage.streaming import iter_minio_response


def _minio_not_found(exc: Exception) -> bool:
    """区分「对象真的不存在」与连接/超时等暂时性故障。"""
    return isinstance(exc, S3Error) and exc.code in {"NoSuchKey", "NoSuchObject"}


router = APIRouter(prefix="/files", tags=["文件中心"])
logger = get_logger(__name__)

_IMAGE_SUFFIXES = frozenset(
    {".jpg", ".jpeg", ".png", ".gif", ".webp", ".bmp", ".tif", ".tiff"}
)


def _minio_image_object_candidates(object_name: str) -> list[str]:
    normalized = str(object_name or "").strip().lstrip("/")

    def is_valid(value: str) -> bool:
        return bool(value) and not any(
            part in {"", ".", ".."} for part in value.split("/")
        )

    if not is_valid(normalized):
        return []

    candidates: list[str] = []
    seen: set[str] = set()
    current = normalized
    for _ in range(4):
        if not is_valid(current):
            return []
        if current not in seen:
            seen.add(current)
            candidates.append(current)
        decoded = unquote(current)
        if decoded == current:
            break
        current = decoded
    return candidates


def _client_ip(request: Request) -> str | None:
    resolved = resolve_client_ip(request)
    return None if resolved == UNKNOWN_CLIENT_IP else resolved


_SAFE_ARTIFACT_MEDIA_TYPES = {
    ".csv": "text/csv",
    ".docx": (
        "application/vnd.openxmlformats-officedocument.wordprocessingml.document"
    ),
    ".json": "application/json",
    ".md": "text/markdown",
    ".pdf": "application/pdf",
    ".pptx": (
        "application/vnd.openxmlformats-officedocument.presentationml.presentation"
    ),
    ".txt": "text/plain",
    ".xlsx": "application/vnd.openxmlformats-officedocument.spreadsheetml.sheet",
}


def _artifact_media_type(object_name: str) -> str:
    """Derive the response type from the stored object, never from the request."""

    suffix = Path(str(object_name or "")).suffix.lower()
    if suffix in _IMAGE_SUFFIXES:
        return mimetypes.guess_type(f"image{suffix}")[0] or "application/octet-stream"
    return _SAFE_ARTIFACT_MEDIA_TYPES.get(suffix, "application/octet-stream")


def _is_allowed_minio_artifact_object(object_name: str, user_id: str) -> bool:
    normalized = str(object_name or "").strip().lstrip("/")
    if not normalized:
        return False
    allowed_prefixes = (f"users/{user_id}/threads/",)
    return any(normalized.startswith(prefix) for prefix in allowed_prefixes)


def _is_allowed_user_file_object(object_name: str, owner_user_id: str) -> bool:
    normalized = str(object_name or "").strip().lstrip("/")
    if not normalized or any(part in {"", ".", ".."} for part in normalized.split("/")):
        return False
    return normalized.startswith(f"users/{owner_user_id}/")


def _is_allowed_local_user_file_path(path: str, owner_user_id: str) -> bool:
    """Only serve runtime artifacts created below this user's workspace."""
    cleaned = str(path or "").strip()
    if not cleaned:
        return False
    try:
        candidate = Path(cleaned).resolve(strict=True)
    except (OSError, RuntimeError, ValueError):
        return False
    if candidate.is_symlink() or Path(cleaned).is_symlink():
        return False
    try:
        user_root = (
            get_paths().base_dir / "users" / owner_user_id / "threads"
        ).resolve()
        relative = candidate.relative_to(user_root)
    except (OSError, RuntimeError, ValueError):
        return False
    parts = relative.parts
    return (
        candidate.is_file() and len(parts) >= 4 and parts[1:3] == ("files", "artifacts")
    )


@router.get("/minio-artifact/download")
async def download_minio_artifact(
    request: Request,
    object_name: str = Query(..., min_length=1),
    filename: str = Query(default="artifact"),
    user: User = Depends(get_current_user),
):
    normalized_object = object_name.strip().lstrip("/")
    if not _is_allowed_minio_artifact_object(normalized_object, user.user_id):
        raise HTTPException(status_code=403, detail="无权访问该工件")

    client = _get_minio_client()
    bucket = get_user_minio_bucket_name()
    try:
        response = await asyncio.to_thread(
            client.get_object, bucket, normalized_object
        )
    except Exception as exc:
        logger.warning(
            "MinIO 工件下载失败: bucket=%s object=%s",
            bucket,
            normalized_object,
            exc_info=True,
        )
        # 暂时性故障（连接/超时）不能伪装成 404，否则客户端会把对象
        # 当成已删除处理。
        if not _minio_not_found(exc):
            raise HTTPException(status_code=502, detail="对象存储暂不可用，请稍后重试") from exc
        raise HTTPException(status_code=404, detail="工件对象不存在") from exc

    display_name = Path(filename or "artifact").name or "artifact"
    try:
        display_name.encode("ascii")
        # 引号/反斜杠会从 quoted-string 逃逸，伪造额外 filename 参数
        display_name = display_name.replace('"', "'").replace("\\", "_")
        disposition = f'attachment; filename="{display_name}"'
    except UnicodeEncodeError:
        encoded = quote(display_name, safe="")
        disposition = f"attachment; filename=\"artifact\"; filename*=UTF-8''{encoded}"

    return StreamingResponse(
        iter_minio_response(response),
        # A caller-supplied content type would let a user turn their own stored
        # bytes into browser-rendered HTML/SVG on this origin.  Derive it from
        # the stored object name instead and always download.
        media_type=_artifact_media_type(normalized_object),
        headers={
            "Content-Disposition": disposition,
            "X-Content-Type-Options": "nosniff",
        },
    )


@router.get("/kb-image")
async def get_kb_image(
    object_name: str = Query(..., min_length=1, max_length=4096),
    exp: int | None = Query(default=None),
    sig: str | None = Query(default=None),
):
    """Serve a public knowledge-base image with a browser-safe MIME type."""
    if not verify_kb_image_proxy_signature(object_name, exp, sig):
        raise HTTPException(status_code=403, detail="图片访问签名无效或已过期")
    candidates = _minio_image_object_candidates(object_name)
    if not candidates:
        raise HTTPException(status_code=400, detail="图片对象路径无效")

    suffix = Path(unquote(candidates[-1])).suffix.lower()
    if suffix not in _IMAGE_SUFFIXES:
        raise HTTPException(status_code=400, detail="不支持的图片类型")

    client = _get_minio_client()
    bucket = (settings.minio_bucket_name or "").strip("/")
    response = None
    last_error: Exception | None = None
    saw_transient = False
    for candidate in candidates:
        try:
            response = await asyncio.to_thread(client.get_object, bucket, candidate)
            break
        except Exception as exc:
            last_error = exc
            if not _minio_not_found(exc):
                saw_transient = True
            continue
    if response is None:
        logger.warning(
            "知识库图片对象读取失败: bucket=%s candidates=%s",
            bucket,
            candidates,
            exc_info=(
                (type(last_error), last_error, last_error.__traceback__)
                if last_error is not None
                else None
            ),
        )
        # 候选中只要出现过暂时性故障（连接/超时）就按 502 处理：
        # 按「最后一个错误」判定会把「A 连接错误、B 不存在」误判成 404。
        if saw_transient:
            raise HTTPException(
                status_code=502, detail="对象存储暂不可用，请稍后重试"
            ) from last_error
        raise HTTPException(status_code=404, detail="图片对象不存在") from last_error

    media_type = mimetypes.guess_type(f"image{suffix}")[0] or "application/octet-stream"
    return StreamingResponse(
        iter_minio_response(response),
        media_type=media_type,
        headers={
            "Cache-Control": "public, max-age=86400",
            "Content-Disposition": f'inline; filename="image{suffix}"',
        },
    )


@router.get("")
async def list_files(
    thread_id: str,
    request: Request,
    limit: int = 50,
    offset: int = 0,
    user: User = Depends(get_current_user),
    session: AsyncSession = Depends(get_async_session),
):
    items = await list_active_files_for_thread_for_user(
        session=session,
        current_user_id=user.user_id,
        thread_id=thread_id,
        limit=limit,
        offset=offset,
    )

    return {
        "items": items,
        "limit": max(1, min(int(limit or 50), 200)),
        "offset": max(0, int(offset or 0)),
        "count": len(items),
    }


@router.get("/{file_id}/meta")
async def get_file_meta(
    file_id: str,
    request: Request,
    user: User = Depends(get_current_user),
    session: AsyncSession = Depends(get_async_session),
):
    file_row, allowed, reason = await resolve_user_file_for_download(
        session=session,
        file_id=file_id,
        current_user_id=user.user_id,
    )

    if not file_row:
        raise HTTPException(status_code=404, detail="文件不存在")

    if not allowed:
        await write_download_audit(
            session=session,
            file_id=file_id,
            user_id=user.user_id,
            decision="deny",
            reason=f"meta:{reason}",
            ip=_client_ip(request),
            user_agent=request.headers.get("User-Agent"),
        )
        raise HTTPException(status_code=403, detail="无权访问该文件")

    await write_download_audit(
        session=session,
        file_id=file_id,
        user_id=user.user_id,
        decision="allow",
        reason="meta:ok",
        ip=_client_ip(request),
        user_agent=request.headers.get("User-Agent"),
    )

    return {
        "file_id": file_row.file_id,
        "title": file_row.display_name,
        "mime_type": file_row.mime_type,
        "size_bytes": file_row.size_bytes,
        "can_download": True,
        "download_url": f"/api/v2/files/{quote(file_row.file_id)}/download",
    }


@router.get("/{file_id}/download")
async def download_file(
    file_id: str,
    request: Request,
    user: User = Depends(get_current_user),
    session: AsyncSession = Depends(get_async_session),
):
    file_row, allowed, reason = await resolve_user_file_for_download(
        session=session,
        file_id=file_id,
        current_user_id=user.user_id,
    )

    if not file_row:
        raise HTTPException(status_code=404, detail="文件不存在")

    if not allowed:
        await write_download_audit(
            session=session,
            file_id=file_id,
            user_id=user.user_id,
            decision="deny",
            reason=f"download:{reason}",
            ip=_client_ip(request),
            user_agent=request.headers.get("User-Agent"),
        )
        raise HTTPException(status_code=403, detail="无权访问该文件")

    media_type = file_row.mime_type or "application/octet-stream"

    if file_row.local_abs_path:
        local_path = Path(file_row.local_abs_path)
        if await asyncio.to_thread(
            _is_allowed_local_user_file_path,
            file_row.local_abs_path,
            file_row.owner_user_id,
        ):
            await write_download_audit(
                session=session,
                file_id=file_id,
                user_id=user.user_id,
                decision="allow",
                reason="download:local",
                ip=_client_ip(request),
                user_agent=request.headers.get("User-Agent"),
            )
            # 审计走独立会话；提交本请求只读事务归还连接——文件流式传输
            # 期间不钉住 MySQL 连接（commit 保持 file_row 可用）。
            await session.commit()
            return FileResponse(
                path=str(local_path),
                media_type=media_type,
                filename=file_row.display_name,
            )

    if file_row.storage_object:
        if (
            file_row.storage_bucket != get_user_minio_bucket_name()
            or not _is_allowed_user_file_object(
                file_row.storage_object,
                file_row.owner_user_id,
            )
        ):
            raise HTTPException(
                status_code=403, detail="文件存储位置不在用户授权范围内"
            )
        client = _get_minio_client()
        bucket = file_row.storage_bucket or get_user_minio_bucket_name()
        try:
            response = await asyncio.to_thread(
                client.get_object, bucket, file_row.storage_object
            )
        except Exception as exc:
            logger.warning(
                "用户文件对象下载失败: bucket=%s object=%s file_id=%s",
                bucket,
                file_row.storage_object,
                file_id,
                exc_info=True,
            )
            if not _minio_not_found(exc):
                raise HTTPException(status_code=502, detail="对象存储暂不可用，请稍后重试") from exc
            raise HTTPException(status_code=404, detail="文件对象不存在") from exc

        await write_download_audit(
            session=session,
            file_id=file_id,
            user_id=user.user_id,
            decision="allow",
            reason="download:minio",
            ip=_client_ip(request),
            user_agent=request.headers.get("User-Agent"),
        )

        # RFC 5987: use filename* for non-ASCII display names so browsers
        # correctly render Chinese filenames like "成都理工大学深度研究报告.docx".
        display_name = file_row.display_name or "download"
        try:
            display_name.encode("ascii")
            disposition = f'attachment; filename="{display_name}"'
        except UnicodeEncodeError:
            encoded = quote(display_name, safe="")
            disposition = (
                f"attachment; filename=\"download\"; filename*=UTF-8''{encoded}"
            )
        headers = {
            "Content-Disposition": disposition,
        }
        # 对象下载流期间不钉住 MySQL 连接（同本地路径：审计后提交只读事务）。
        await session.commit()
        return StreamingResponse(
            iter_minio_response(response),
            media_type=media_type,
            headers=headers,
        )

    await write_download_audit(
        session=session,
        file_id=file_id,
        user_id=user.user_id,
        decision="deny",
        reason="download:no_storage_target",
        ip=_client_ip(request),
        user_agent=request.headers.get("User-Agent"),
    )
    raise HTTPException(status_code=404, detail="文件未找到有效存储位置")


@router.get("/download-audit")
async def list_download_audit(
    limit: int = 50,
    offset: int = 0,
    file_id: str | None = None,
    user: User = Depends(get_current_user),
    session: AsyncSession = Depends(get_async_session),
):
    records = await list_download_audit_for_user(
        session=session,
        current_user_id=user.user_id,
        limit=limit,
        offset=offset,
        file_id=file_id,
    )
    return {
        "items": records,
        "limit": max(1, min(int(limit or 50), 200)),
        "offset": max(0, int(offset or 0)),
        "count": len(records),
    }


class ExportDocxRequest(BaseModel):
    title: str = Field(default="export", max_length=120)
    content: str = Field(..., min_length=1, max_length=200_000)

    @field_validator("title")
    @classmethod
    def normalize_title(cls, value: str) -> str:
        title = str(value or "export").strip() or "export"
        title = title.replace("\r", " ").replace("\n", " ")
        for ch in '\\/:*?"<>|':
            title = title.replace(ch, "_")
        return title[:120] or "export"


@router.post("/export-docx")
async def export_docx(
    payload: ExportDocxRequest,
    user: User = Depends(get_current_user),
):
    del user  # 鉴权用，避免公开重型文档渲染接口被滥用
    try:
        from backend.application.tools.providers.deep_research_report_tool import (
            _render_report_docx_bytes,
        )

        docx_bytes = await asyncio.to_thread(
            _render_report_docx_bytes, title=payload.title, content=payload.content
        )
    except Exception as exc:
        raise HTTPException(
            status_code=500, detail="生成 Word 文档失败，请稍后重试"
        ) from exc

    from io import BytesIO

    buffer = BytesIO(docx_bytes)

    display_name = f"{payload.title or 'export'}.docx"
    encoded_name = quote(display_name)
    headers = {
        "Content-Disposition": f"attachment; filename*=UTF-8''{encoded_name}",
        "Access-Control-Expose-Headers": "Content-Disposition",
    }
    return StreamingResponse(
        buffer,
        media_type="application/vnd.openxmlformats-officedocument.wordprocessingml.document",
        headers=headers,
    )
