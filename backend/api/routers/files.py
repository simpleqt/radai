import asyncio
from pathlib import Path
from urllib.parse import quote

from fastapi import APIRouter, Depends, HTTPException, Query, Request
from fastapi.responses import FileResponse, StreamingResponse
from pydantic import BaseModel

from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from backend.api.routers.auth import get_current_user
from backend.db.connection import get_async_session
from backend.db.models import User
from backend.services.user_file_service import (
    list_active_files_for_thread_for_user,
    list_download_audit_for_user,
    resolve_user_file_for_download,
    write_download_audit,
)
from backend.storage import _get_minio_client, get_user_minio_bucket_name

router = APIRouter(prefix="/files", tags=["文件中心"])


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
    allowed_prefixes = (
        f"users/{user_id}/threads/",
        "users/anonymous/threads/",
    )
    return any(normalized.startswith(prefix) for prefix in allowed_prefixes)


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
        response = client.get_object(bucket, normalized_object)
    except Exception as exc:
        raise HTTPException(status_code=404, detail=f"工件对象不存在: {exc}")

    def iterator():
        try:
            for chunk in response.stream(64 * 1024):
                yield chunk
        finally:
            response.close()
            response.release_conn()

    display_name = Path(filename or "artifact").name or "artifact"
    try:
        display_name.encode("ascii")
        disposition = f'attachment; filename="{display_name}"'
    except UnicodeEncodeError:
        encoded = quote(display_name, safe="")
        disposition = f"attachment; filename=\"artifact\"; filename*=UTF-8''{encoded}"

    return StreamingResponse(
        iterator(),
        media_type=mime_type or "application/octet-stream",
        headers={"Content-Disposition": disposition},
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
        if local_path.exists() and local_path.is_file():
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
        client = _get_minio_client()
        bucket = file_row.storage_bucket or get_user_minio_bucket_name()
        try:
            response = client.get_object(bucket, file_row.storage_object)
        except Exception as exc:
            raise HTTPException(status_code=404, detail=f"文件对象不存在: {exc}")

        def iterator():
            try:
                for chunk in response.stream(64 * 1024):
                    yield chunk
            finally:
                response.close()
                response.release_conn()

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
            disposition = f"attachment; filename=\"download\"; filename*=UTF-8''{encoded}"
        headers = {
            "Content-Disposition": disposition,
        }
        return StreamingResponse(iterator(), media_type=media_type, headers=headers)

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
    title: str
    content: str

@router.post("/export-docx")
async def export_docx(
    payload: ExportDocxRequest,
):
    try:
        from backend.application.tools.providers.deep_research_report_tool import _render_report_docx_bytes
        docx_bytes = await asyncio.to_thread(
            _render_report_docx_bytes,
            title=payload.title,
            content=payload.content
        )
    except Exception as exc:
        raise HTTPException(status_code=500, detail=f"生成 Word 文档失败: {exc}")

    from io import BytesIO
    buffer = BytesIO(docx_bytes)

    display_name = f"{payload.title or 'export'}.docx"
    encoded_name = quote(display_name)
    headers = {
        "Content-Disposition": f"attachment; filename*=UTF-8''{encoded_name}",
        "Access-Control-Expose-Headers": "Content-Disposition"
    }
    return StreamingResponse(
        buffer,
        media_type="application/vnd.openxmlformats-officedocument.wordprocessingml.document",
        headers=headers
    )
