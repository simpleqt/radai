"""
个人文件夹与文件管理路由

POST   /api/v1/files/upload                  - 上传文件到个人文件夹
GET    /api/v1/my-files                      - 获取文件列表（支持检索与过滤）
PUT    /api/v1/my-files/{file_id}            - 修改文件名
DELETE /api/v1/my-files/{file_id}            - 软删除文件
POST   /api/v1/sessions/{session_id}/files/attach  - 将文件库的文件绑定到会话
POST   /api/v1/generated-files/save          - 将生成的文件保存到个人文件夹
"""

from __future__ import annotations

import asyncio
import hashlib
import hmac
import time
import uuid
from pathlib import Path
from typing import Optional
from urllib.parse import quote

from fastapi import APIRouter, Depends, File, HTTPException, Query, Request, UploadFile
from fastapi.responses import StreamingResponse
from pydantic import BaseModel
from sqlalchemy import func, or_, select, update
from sqlalchemy.ext.asyncio import AsyncSession

from backend.api.routers.auth import get_current_user
from backend.api.upload_utils import UploadTooLargeError, read_upload_with_limit
from backend.config.loggers import get_logger
from backend.config.settings import settings
from backend.core.config import get_paths
from backend.db.connection import get_async_session
from backend.db.models import SessionFile, User, UserFile
from backend.services.chat_file_index import get_index_manager
from backend.services.user_file_service import (
    ALLOWED_UPLOAD_EXTENSIONS,
    UserFileParseError,
    cleanup_uploaded_minio_objects,
    prepare_uploaded_user_file,
)
from backend.storage import _get_minio_client, get_user_minio_bucket_name
from backend.storage.streaming import iter_minio_response

logger = get_logger(__name__)

router = APIRouter(tags=["个人文件夹"])

# 文件大小上限：50MB
MAX_FILE_SIZE = 50 * 1024 * 1024

ALLOWED_EXTENSIONS = ALLOWED_UPLOAD_EXTENSIONS


# ── 请求与响应模型 ─────────────────────────────────────────────


class RenameFileRequest(BaseModel):
    file_name: str


class AttachFileRequest(BaseModel):
    file_id: str


class SaveGeneratedFileRequest(BaseModel):
    file_name: str
    storage_bucket: Optional[str] = None
    storage_object: Optional[str] = None
    local_abs_path: Optional[str] = None
    mime_type: Optional[str] = None
    size_bytes: Optional[int] = None
    generator_task_id: Optional[str] = None


class PersonalFileItem(BaseModel):
    file_id: str
    display_name: str
    size_bytes: int
    mime_type: Optional[str]
    source_type: str
    created_at: str
    download_url: str
    file_category: str
    strategy: str
    table_analysis_enabled: bool = False


def _build_personal_file_image_url(
    request: Request, file_id: str, image_name: str
) -> str:
    base_url = str(request.base_url).rstrip("/")
    expires_at = int(time.time()) + 7 * 24 * 60 * 60
    signature = _build_personal_image_signature(file_id, image_name, expires_at)
    return (
        f"{base_url}/api/v1/my-files/{quote(file_id)}/images/{quote(image_name, safe='')}"
        f"?exp={expires_at}&sig={signature}"
    )


def _build_personal_image_signature(
    file_id: str, image_name: str, expires_at: int
) -> str:
    secret = (settings.minio_secret_key or "").encode("utf-8")
    payload = f"{file_id}\n{image_name}\n{expires_at}".encode("utf-8")
    return hmac.new(secret, payload, hashlib.sha256).hexdigest()


def _valid_personal_image_signature(
    file_id: str,
    image_name: str,
    exp: int | None,
    sig: str | None,
) -> bool:
    if not exp or not sig or exp < int(time.time()):
        return False
    expected = _build_personal_image_signature(file_id, image_name, exp)
    return hmac.compare_digest(expected, sig)


def _validated_generated_local_path(raw_path: str, user_id: str) -> Path:
    cleaned = str(raw_path or "").strip()
    if not cleaned:
        raise HTTPException(status_code=403, detail="生成文件不在当前用户工件目录内")
    try:
        candidate = Path(cleaned).resolve(strict=True)
    except (OSError, RuntimeError) as exc:
        raise HTTPException(
            status_code=403, detail="生成文件不在当前用户工件目录内"
        ) from exc
    try:
        user_root = (get_paths().base_dir / "users" / user_id / "threads").resolve()
        relative = candidate.relative_to(user_root)
    except (OSError, RuntimeError, ValueError) as exc:
        raise HTTPException(
            status_code=403, detail="生成文件不在当前用户工件目录内"
        ) from exc
    if (
        not candidate.is_file()
        or len(relative.parts) < 4
        or relative.parts[1:3] != ("files", "artifacts")
    ):
        raise HTTPException(status_code=403, detail="生成文件不在当前用户工件目录内")
    if candidate.is_symlink() or Path(cleaned).is_symlink():
        raise HTTPException(status_code=403, detail="生成文件不在当前用户工件目录内")
    return candidate


def _validated_generated_minio_object(
    bucket: str | None,
    object_name: str,
    user_id: str,
) -> tuple[str, str]:
    expected_bucket = get_user_minio_bucket_name()
    normalized = str(object_name or "").strip().lstrip("/")
    if (
        (bucket or expected_bucket) != expected_bucket
        or not normalized.startswith(f"users/{user_id}/")
        or any(part in {"", ".", ".."} for part in normalized.split("/"))
    ):
        raise HTTPException(
            status_code=403, detail="生成文件对象不在当前用户存储范围内"
        )
    return expected_bucket, normalized


# ── 接口端点实现 ─────────────────────────────────────────────


@router.post("/api/v1/files/upload")
async def upload_personal_file(
    request: Request,
    file: UploadFile = File(..., description="上传的文件"),
    user: User = Depends(get_current_user),
    session: AsyncSession = Depends(get_async_session),
):
    """上传文件到个人文件夹"""
    filename = file.filename or "unknown"
    ext = Path(filename).suffix.lower()

    if ext not in ALLOWED_EXTENSIONS:
        raise HTTPException(status_code=400, detail=f"不支持的文件类型: {ext}")

    try:
        data = await read_upload_with_limit(file, MAX_FILE_SIZE)
    except UploadTooLargeError as exc:
        raise HTTPException(status_code=413, detail="文件大小超过 50MB 限制") from exc
    mime_type = file.content_type
    try:
        stored_file = await prepare_uploaded_user_file(
            session=session,
            owner_user_id=user.user_id,
            data=data,
            filename=filename,
            mime_type=mime_type,
            storage_scope="personal",
            image_url_builder=lambda file_id, image_name: _build_personal_file_image_url(
                request,
                file_id,
                image_name,
            ),
        )
    except UserFileParseError as exc:
        error_msg = exc.message or "解析服务异常"
        raise HTTPException(status_code=422, detail=f"文件解析失败: {error_msg}")

    try:
        await session.commit()
    except BaseException:
        try:
            await session.rollback()
        except Exception as rollback_exc:
            logger.exception(
                "[MyFiles] 数据库回滚失败: file_id=%s error=%s",
                stored_file.file_id,
                rollback_exc,
            )
        await cleanup_uploaded_minio_objects(
            stored_file.storage_bucket,
            stored_file.uploaded_object_keys,
        )
        raise

    return {
        "success": True,
        "file": {
            "file_id": stored_file.file_id,
            "display_name": filename,
            "size_bytes": stored_file.size_bytes,
            "mime_type": mime_type,
            "download_url": f"/api/v2/files/{stored_file.file_id}/download",
            "file_category": stored_file.category,
            "strategy": stored_file.strategy,
            "table_analysis_enabled": stored_file.table_analysis_enabled,
        },
    }


@router.get("/api/v1/my-files/{file_id}/images/{image_name}")
async def get_personal_file_image(
    file_id: str,
    image_name: str,
    exp: int | None = None,
    sig: str | None = None,
    session: AsyncSession = Depends(get_async_session),
):
    """Return a time-limited, signed personal-file image."""
    if not _valid_personal_image_signature(file_id, image_name, exp, sig):
        raise HTTPException(status_code=403, detail="图片访问签名无效或已过期")
    stmt = select(UserFile).where(
        UserFile.file_id == file_id, UserFile.status == "active"
    )
    result = await session.execute(stmt)
    row = result.scalars().first()
    if not row:
        raise HTTPException(status_code=404, detail="文件不存在")

    metadata = row.metadata_ or {}
    target = None
    for item in metadata.get("image_urls", []) or []:
        if item.get("name") == image_name:
            target = item
            break
    if not target:
        raise HTTPException(status_code=404, detail="图片不存在")

    candidate_objects = [
        target.get("storage_object"),
        f"users/{row.owner_user_id}/personal-files/{file_id}/images/{image_name}",
        f"chat_files/{row.owner_user_id}/{file_id}/images/{image_name}",
    ]

    client = _get_minio_client()
    bucket = row.storage_bucket or get_user_minio_bucket_name()
    response = None
    for object_name in candidate_objects:
        if not object_name:
            continue
        try:
            response = await asyncio.to_thread(client.get_object, bucket, object_name)
            break
        except Exception:
            response = None
    if response is None:
        raise HTTPException(status_code=404, detail="图片对象不存在")

    media_type = target.get("content_type") or "image/png"

    return StreamingResponse(
        iter_minio_response(response),
        media_type=media_type,
        headers={
            "Cache-Control": "public, max-age=86400",
            "Content-Disposition": f'inline; filename="{image_name}"',
        },
    )


@router.get("/api/v1/my-files", response_model=dict)
async def list_my_files(
    keyword: Optional[str] = None,
    source: Optional[str] = None,
    file_type: Optional[str] = None,
    limit: int = Query(default=20, ge=1, le=200),
    offset: int = Query(default=0, ge=0),
    user: User = Depends(get_current_user),
    session: AsyncSession = Depends(get_async_session),
):
    """获取个人文件夹中的文件列表（支持关键词搜索、来源与类型过滤）"""
    conditions = [
        UserFile.owner_user_id == user.user_id,
        UserFile.status == "active",
    ]

    if keyword:
        conditions.append(UserFile.display_name.like(f"%{keyword}%"))

    if source:
        # source 对应 source_type
        conditions.append(UserFile.source_type == source)

    # 简单过滤类型
    if file_type:
        if file_type == "image":
            conditions.append(UserFile.mime_type.like("image/%"))
        elif file_type == "document":
            conditions.append(
                or_(
                    UserFile.display_name.like("%.pdf"),
                    UserFile.display_name.like("%.docx"),
                    UserFile.display_name.like("%.doc"),
                    UserFile.display_name.like("%.txt"),
                    UserFile.display_name.like("%.md"),
                )
            )
        elif file_type == "spreadsheet":
            conditions.append(
                or_(
                    UserFile.display_name.like("%.xlsx"),
                    UserFile.display_name.like("%.xls"),
                    UserFile.display_name.like("%.csv"),
                )
            )
        elif file_type == "presentation":
            conditions.append(
                or_(
                    UserFile.display_name.like("%.pptx"),
                    UserFile.display_name.like("%.ppt"),
                )
            )

    # 获取总数与数据
    count_stmt = select(func.count()).select_from(UserFile).where(*conditions)
    res_count = await session.execute(count_stmt)
    total_count = int(res_count.scalar() or 0)

    stmt = select(UserFile).where(*conditions)
    stmt = stmt.order_by(UserFile.created_at.desc()).limit(limit).offset(offset)
    result = await session.execute(stmt)
    files = result.scalars().all()

    items = []
    for f in files:
        m_json = f.metadata_ or {}
        items.append(
            PersonalFileItem(
                file_id=f.file_id,
                display_name=f.display_name,
                size_bytes=f.size_bytes or 0,
                mime_type=f.mime_type,
                source_type=f.source_type,
                created_at=f.created_at.isoformat() if f.created_at else "",
                download_url=f"/api/v2/files/{f.file_id}/download",
                file_category=m_json.get("file_category", "short_doc"),
                strategy=m_json.get("strategy", "context"),
                table_analysis_enabled=bool(
                    (m_json.get("table_analysis") or {}).get("enabled")
                ),
            )
        )

    return {
        "success": True,
        "items": items,
        "total": total_count,
        "limit": limit,
        "offset": offset,
    }


@router.put("/api/v1/my-files/{file_id}")
async def rename_my_file(
    file_id: str,
    payload: RenameFileRequest,
    user: User = Depends(get_current_user),
    session: AsyncSession = Depends(get_async_session),
):
    """重命名文件"""
    stmt = select(UserFile).where(
        UserFile.file_id == file_id,
        UserFile.owner_user_id == user.user_id,
        UserFile.status == "active",
    )
    result = await session.execute(stmt)
    db_file = result.scalars().first()

    if not db_file:
        raise HTTPException(status_code=404, detail="文件不存在或无权操作")

    db_file.display_name = payload.file_name
    await session.commit()

    return {"success": True, "file_id": file_id, "file_name": payload.file_name}


@router.delete("/api/v1/my-files/{file_id}")
async def soft_delete_my_file(
    file_id: str,
    user: User = Depends(get_current_user),
    session: AsyncSession = Depends(get_async_session),
):
    """软删除文件（标记为 deleted，解绑所有会话）"""
    stmt = select(UserFile).where(
        UserFile.file_id == file_id,
        UserFile.owner_user_id == user.user_id,
        UserFile.status == "active",
    )
    result = await session.execute(stmt)
    db_file = result.scalars().first()

    if not db_file:
        raise HTTPException(status_code=404, detail="文件不存在或无权操作")

    db_file.status = "deleted"

    # 解绑所有关联的会话
    del_stmt = (
        update(SessionFile).where(SessionFile.file_id == file_id).values(session_id="")
    )
    # 或者直接删除 session_files 记录
    # del_stmt = delete(SessionFile).where(SessionFile.file_id == file_id)
    await session.execute(del_stmt)

    await session.commit()

    return {"success": True, "file_id": file_id}


@router.post("/api/v1/sessions/{session_id}/files/attach")
async def attach_file_to_session(
    session_id: str,
    payload: AttachFileRequest,
    user: User = Depends(get_current_user),
    session: AsyncSession = Depends(get_async_session),
):
    """将个人文件夹中的已有文件，绑定到指定会话"""
    # 1. 验证文件是否存在且属于该用户
    stmt = select(UserFile).where(
        UserFile.file_id == payload.file_id,
        UserFile.owner_user_id == user.user_id,
        UserFile.status == "active",
    )
    result = await session.execute(stmt)
    db_file = result.scalars().first()

    if not db_file:
        raise HTTPException(status_code=404, detail="要绑定的文件不存在")

    # 2. 检查是否已经绑定
    dup_stmt = select(SessionFile).where(
        SessionFile.session_id == session_id, SessionFile.file_id == payload.file_id
    )
    dup_res = await session.execute(dup_stmt)
    if dup_res.scalars().first():
        return {
            "success": True,
            "message": "文件已绑定到该会话，无需重复绑定",
            "file_id": payload.file_id,
        }

    # 3. 创建绑定关系
    db_session_file = SessionFile(
        session_id=session_id,
        user_id=user.user_id,
        file_id=payload.file_id,
        attached_from="my_files",
    )
    session.add(db_session_file)

    # 4. 如果是长文档，需要在会话中构建检索索引
    m_json = db_file.metadata_ or {}
    if m_json.get("file_category") == "long_doc" and m_json.get("content"):
        try:
            index_manager = get_index_manager()
            index = index_manager.get_or_create(session_id, user_id=user.user_id)
            chunk_count = await asyncio.to_thread(
                index.add_document,
                file_id=payload.file_id,
                content=m_json.get("content"),
                file_name=db_file.display_name,
            )
            logger.info(
                "[PersonalFiles] 绑定长文档并成功建立索引: file_id=%s, chunks=%d",
                payload.file_id,
                chunk_count,
            )
        except Exception as e:
            logger.error("[PersonalFiles] 绑定建立索引失败: %s", e)

    await session.commit()

    return {
        "success": True,
        "message": "文件绑定会话成功",
        "file_id": payload.file_id,
        "session_id": session_id,
    }


@router.post("/api/v1/generated-files/save")
async def save_generated_file(
    payload: SaveGeneratedFileRequest,
    user: User = Depends(get_current_user),
    session: AsyncSession = Depends(get_async_session),
):
    """保存系统生成的文件到个人文件夹"""
    has_local = bool(str(payload.local_abs_path or "").strip())
    has_minio = bool(str(payload.storage_object or "").strip())
    if has_local == has_minio:
        raise HTTPException(status_code=400, detail="必须且只能提供一种生成文件来源")

    display_name = Path(str(payload.file_name or "")).name.strip()
    if not display_name or len(display_name) > 255:
        raise HTTPException(status_code=400, detail="文件名无效")

    storage_bucket: str | None = None
    storage_object: str | None = None
    local_abs_path: str | None = None
    size_bytes = 0
    if has_local:
        local_path = _validated_generated_local_path(
            payload.local_abs_path or "", user.user_id
        )
        local_abs_path = str(local_path)
        size_bytes = local_path.stat().st_size
    else:
        storage_bucket, storage_object = _validated_generated_minio_object(
            payload.storage_bucket,
            payload.storage_object or "",
            user.user_id,
        )
        try:
            stat = await asyncio.to_thread(
                _get_minio_client().stat_object,
                storage_bucket,
                storage_object,
            )
            size_bytes = int(getattr(stat, "size", 0) or 0)
        except Exception as exc:
            raise HTTPException(status_code=404, detail="生成文件对象不存在") from exc

    file_id = f"uf_{uuid.uuid4().hex}"

    # 构造 metadata 负载
    metadata_payload = {
        "file_category": "short_doc",
        "strategy": "context",
        "content": "",
        "content_length": 0,
    }
    from backend.services.table_analysis_files import build_table_analysis_metadata

    table_analysis_meta = build_table_analysis_metadata(
        file_id=file_id,
        file_name=display_name,
        mime_type=payload.mime_type,
        storage_bucket=storage_bucket,
        storage_object=storage_object,
        local_abs_path=local_abs_path,
    )
    if table_analysis_meta:
        metadata_payload["table_analysis"] = table_analysis_meta

    db_file = UserFile(
        file_id=file_id,
        owner_user_id=user.user_id,
        source_type="minio" if storage_object else "local",
        storage_bucket=storage_bucket,
        storage_object=storage_object,
        local_abs_path=local_abs_path,
        display_name=display_name,
        mime_type=payload.mime_type or "application/octet-stream",
        size_bytes=size_bytes,
        status="active",
        metadata_=metadata_payload,
    )
    session.add(db_file)
    await session.commit()

    return {
        "success": True,
        "file_id": file_id,
        "display_name": display_name,
        "download_url": f"/api/v2/files/{file_id}/download",
    }
