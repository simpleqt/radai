import asyncio
import mimetypes
from pathlib import Path
from urllib.parse import quote, unquote

from fastapi import APIRouter, Depends, HTTPException, Query, Request
from fastapi.responses import FileResponse, StreamingResponse
from pydantic import BaseModel, Field, field_validator
from sqlalchemy.ext.asyncio import AsyncSession

from backend.api.routers.auth import get_current_user
from backend.config import settings
from backend.core.config import get_paths
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

router = APIRouter(prefix="/files", tags=["文件中心"])

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
    xff = request.headers.get("X-Forwarded-For", "").strip()
    if xff:
        return xff.split(",")[0].strip()
    if request.client:
        return request.client.host
    return None


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
    mime_type: str = Query(default="application/octet-stream"),
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
    except Exception:
        raise HTTPException(status_code=404, detail="工件对象不存在")

    display_name = Path(filename or "artifact").name or "artifact"
    try:
        display_name.encode("ascii")
        disposition = f'attachment; filename="{display_name}"'
    except UnicodeEncodeError:
        encoded = quote(display_name, safe="")
        disposition = f"attachment; filename=\"artifact\"; filename*=UTF-8''{encoded}"

    return StreamingResponse(
        iter_minio_response(response),
        media_type=mime_type or "application/octet-stream",
        headers={"Content-Disposition": disposition},
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
    for candidate in candidates:
        try:
            response = await asyncio.to_thread(client.get_object, bucket, candidate)
            break
        except Exception:
            continue
    if response is None:
        raise HTTPException(status_code=404, detail="图片对象不存在")

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
        if _is_allowed_local_user_file_path(
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
        except Exception:
            raise HTTPException(status_code=404, detail="文件对象不存在")

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
