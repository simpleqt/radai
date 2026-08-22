"""
对话文件上传路由

POST /api/v2/chat-files/upload  - 上传并解析文件
GET  /api/v2/chat-files/{thread_id}  - 列出会话中的文件
DELETE /api/v2/chat-files/{file_id}  - 删除文件
"""

from __future__ import annotations

import asyncio
import hmac
import time
import uuid
from pathlib import Path
from urllib.parse import quote, urlparse, urlunparse

import httpx
from fastapi import (
    APIRouter,
    Depends,
    File,
    Form,
    HTTPException,
    Query,
    Request,
    UploadFile,
)
from fastapi.responses import StreamingResponse
from pydantic import BaseModel
from sqlalchemy import func, select
from sqlalchemy.ext.asyncio import AsyncSession

from backend.api.routers.auth import get_current_user
from backend.api.upload_utils import UploadTooLargeError, read_upload_with_limit
from backend.config.loggers import get_logger
from backend.config.runtime_security import public_base_url
from backend.db.connection import get_async_session
from backend.db.models import SessionFile, User, UserFile
from backend.services.chat_file_metadata import (
    CHAT_FILE_IMAGE_URL_TTL_SECONDS,
)
from backend.services.chat_file_metadata import (
    build_chat_image_signature as _build_chat_image_signature,
)
from backend.services.remote_fetch import (
    resolve_public_remote_target as _resolve_public_remote_target,
)
from backend.services.user_file_service import (
    ALLOWED_UPLOAD_EXTENSIONS,
    UserFileParseError,
    cleanup_uploaded_minio_objects,
    prepare_uploaded_user_file,
)
from backend.storage import (
    _get_minio_client,
    get_user_minio_bucket_name,
    upload_bytes_to_minio,
)
from backend.storage.streaming import iter_minio_response

logger = get_logger(__name__)

router = APIRouter(prefix="/chat-files", tags=["对话文件"])

# 文件大小上限：50MB
MAX_FILE_SIZE = 50 * 1024 * 1024

# 每次上传最多文件数
MAX_FILES_PER_REQUEST = 10
MAX_REMOTE_IMAGE_SIZE = 20 * 1024 * 1024
MAX_REMOTE_IMAGES_PER_FILE = 20
ALLOWED_EXTENSIONS = ALLOWED_UPLOAD_EXTENSIONS


# ── 响应模型 ─────────────────────────────────────────────

class ChatFileUploadResult(BaseModel):
    file_id: str
    file_name: str
    file_size: int
    # multimodal / short_doc / long_doc / failed
    file_category: str
    # pending / completed / failed
    parse_status: str
    content_length: int = 0
    # vision / context / indexed / none
    strategy: str
    message: str
    error: str | None = None
    # 多模态文件的 MIME 类型
    mime_type: str | None = None
    # 短文档内容（用于前端立即预览）
    content_preview: str | None = None
    table_analysis_enabled: bool = False


class ChatFileListItem(BaseModel):
    file_id: str
    file_name: str
    file_size: int
    file_category: str
    strategy: str
    content_length: int = 0
    table_analysis_enabled: bool = False


def _build_chat_file_image_url(
    request: Request,
    file_id: str,
    image_name: str,
    *,
    owner_user_id: str,
    thread_id: str,
    expires_in: int = CHAT_FILE_IMAGE_URL_TTL_SECONDS,
) -> str:
    base_url = public_base_url(request)
    expires_at = int(time.time()) + max(60, expires_in)
    sig = _build_chat_image_signature(
        file_id=file_id,
        image_name=image_name,
        owner_user_id=owner_user_id,
        thread_id=thread_id,
        expires_at=expires_at,
    )
    return (
        f"{base_url}/api/v2/chat-files/{quote(file_id)}/images/{quote(image_name, safe='')}"
        f"?exp={expires_at}&sig={sig}"
    )


def _image_object_owned_by(object_name: str | None, owner_user_id: str) -> bool:
    """Only trust a DB storage_object that stays under this owner's prefixes."""

    normalized = str(object_name or "").strip().lstrip("/")
    if not normalized or any(
        part in {"", ".", ".."} for part in normalized.split("/")
    ):
        return False
    return normalized.startswith(
        (f"users/{owner_user_id}/", f"chat_files/{owner_user_id}/")
    )


def _verify_chat_file_image_url(
    *,
    file_id: str,
    image_name: str,
    owner_user_id: str,
    thread_id: str,
    exp: int | None,
    sig: str | None,
) -> bool:
    if not exp or not sig:
        return False
    if exp < int(time.time()):
        return False
    # Fail closed on an empty signing key: an empty secret makes the HMAC
    # locally forgeable (matching verify_chart_signature).
    from backend.config.settings import settings

    from backend.services.url_signing import url_signing_secret

    # 实际 HMAC 密钥来自 url_signing_secret()（URL_SIGNING_SECRET →
    # internal_api_secret → minio_secret_key）；守卫必须判同一来源，
    # 否则"配了独立签名密钥、minio 密钥为空"时签名成功验证全拒。
    if not (url_signing_secret() or "").strip():
        return False
    expected = _build_chat_image_signature(
        file_id=file_id,
        image_name=image_name,
        owner_user_id=owner_user_id,
        thread_id=thread_id,
        expires_at=exp,
    )
    return hmac.compare_digest(expected, sig)


async def _materialize_remote_image_urls(
    request: Request,
    *,
    image_urls: list[dict],
    owner_user_id: str,
    thread_id: str,
    file_id: str,
    bucket_name: str,
    record_uploaded_object,
) -> list[dict]:
    if not image_urls:
        return []

    materialized: list[dict] = []
    async with httpx.AsyncClient(trust_env=False, timeout=60.0, follow_redirects=False) as client:
        for idx, item in enumerate(image_urls[:MAX_REMOTE_IMAGES_PER_FILE], start=1):
            raw_url = str(item.get("url") or "").strip()
            pinned = await _resolve_public_remote_target(raw_url)
            if pinned is None:
                continue
            pinned_ip, pinned_host, pinned_port = pinned
            try:
                parsed = urlparse(raw_url)
                # 连接到校验通过的 IP(不让 httpx 二次解析主机名),消除
                # DNS-rebinding TOCTOU;Host 头 + TLS SNI 仍用原主机名以保证
                # 虚拟主机路由与证书校验正确。
                host_netloc = (
                    f"[{pinned_ip}]" if ":" in pinned_ip else pinned_ip
                )
                pinned_url = urlunparse(
                    parsed._replace(netloc=f"{host_netloc}:{pinned_port}")
                )
                default_port = 443 if parsed.scheme == "https" else 80
                host_header = (
                    pinned_host
                    if pinned_port == default_port
                    else f"{pinned_host}:{pinned_port}"
                )
                async with client.stream(
                    "GET",
                    pinned_url,
                    headers={"Host": host_header},
                    extensions={"sni_hostname": pinned_host},
                ) as response:
                    response.raise_for_status()
                    content_type_header = str(response.headers.get("content-type") or "")
                    # 缺失 content-type 一律拒绝:否则无 content-type 的内网端点
                    # 响应会被兜底为 image/png 存储并经签名 URL 回传(数据外带)。
                    if not content_type_header.lower().startswith("image/"):
                        raise ValueError("远程资源不是图片")
                    declared_size = int(response.headers.get("content-length") or 0)
                    if declared_size > MAX_REMOTE_IMAGE_SIZE:
                        raise ValueError("远程图片超过大小限制")
                    chunks: list[bytes] = []
                    total = 0
                    async for chunk in response.aiter_bytes(256 * 1024):
                        total += len(chunk)
                        if total > MAX_REMOTE_IMAGE_SIZE:
                            raise ValueError("远程图片超过大小限制")
                        chunks.append(chunk)
                    content = b"".join(chunks)
                if not content:
                    continue
                parsed = urlparse(raw_url)
                guessed_name = Path(parsed.path).name or f"remote_image_{idx}.png"
                image_name = Path(
                    str(item.get("name") or guessed_name or f"remote_image_{idx}.png")
                ).name or f"remote_image_{idx}.png"
                content_type = content_type_header or str(
                    item.get("content_type") or "image/png"
                )
                if not content_type.lower().startswith("image/"):
                    raise ValueError("远程资源不是图片")
                storage_object = (
                    f"users/{owner_user_id}/threads/{thread_id}/chat-files/{file_id}/images/{image_name}"
                )
                record_uploaded_object(storage_object)
                uploaded = await upload_bytes_to_minio(
                    content=content,
                    object_name=storage_object,
                    content_type=content_type,
                    bucket_name=bucket_name,
                )
                if not uploaded:
                    raise RuntimeError("远程图片上传对象存储失败")
                materialized.append({
                    "url": _build_chat_file_image_url(
                        request,
                        file_id,
                        image_name,
                        owner_user_id=owner_user_id,
                        thread_id=thread_id,
                    ),
                    "name": image_name,
                    "caption": item.get("caption", f"文档图片 {idx}"),
                    "content_type": content_type,
                    "storage_object": storage_object,
                    "source_url": raw_url,
                })
            except Exception as exc:
                logger.warning(
                    "[ChatFiles] MinerU 远程图片落库失败: file_id=%s, url=%s, error=%s",
                    file_id,
                    raw_url[:200],
                    exc,
                )
    return materialized


# ── 端点实现 ─────────────────────────────────────────────

@router.post("/upload", response_model=dict)
async def upload_chat_files(
    request: Request,
    thread_id: str = Form(..., description="会话线程 ID"),
    parse_mode: str = Form("auto", description="解析方式：auto | mineru"),
    files: list[UploadFile] = File(..., description="上传的文件列表"),
    user: User = Depends(get_current_user),
    session: AsyncSession = Depends(get_async_session),
):
    """上传并解析对话文件

    自动根据文件类型选择处理策略：
    - 图片 → 多模态（strategy=vision）
    - 短文档（≤5000字）→ 直接注入上下文（strategy=context）
    - 长文档（>5000字）→ 建立会话级索引（strategy=indexed）
    """
    from backend.services.chat_file_index import get_index_manager

    if len(files) > MAX_FILES_PER_REQUEST:
        raise HTTPException(
            status_code=400,
            detail=f"单次最多上传 {MAX_FILES_PER_REQUEST} 个文件",
        )

    index_manager = get_index_manager()
    results: list[ChatFileUploadResult] = []
    effective_parse_mode = str(parse_mode or "auto").strip().lower()
    if effective_parse_mode not in {"auto", "mineru"}:
        raise HTTPException(status_code=400, detail="parse_mode 仅支持 auto 或 mineru")

    for upload_file in files:
        filename = upload_file.filename or "unknown"
        ext = Path(filename).suffix.lower()

        if ext not in ALLOWED_EXTENSIONS:
            results.append(
                ChatFileUploadResult(
                    file_id=str(uuid.uuid4()),
                    file_name=filename,
                    file_size=0,
                    file_category="failed",
                    parse_status="failed",
                    strategy="none",
                    message=f"不支持的文件类型: {ext}",
                    error=f"不支持的文件类型: {ext}",
                )
            )
            continue

        try:
            data = await read_upload_with_limit(upload_file, MAX_FILE_SIZE)
        except UploadTooLargeError:
            results.append(
                ChatFileUploadResult(
                    file_id=str(uuid.uuid4()),
                    file_name=filename,
                    file_size=MAX_FILE_SIZE + 1,
                    file_category="failed",
                    parse_status="failed",
                    strategy="none",
                    message="文件过大，上限 50MB",
                    error="文件大小超过限制",
                )
            )
            continue
        mime_type = upload_file.content_type

        async def _index_long_doc(
            file_id: str,
            parse_result,
            file_name: str = filename,
        ) -> None:
            if parse_result.category != "long_doc" or not parse_result.content:
                return
            try:
                index = await index_manager.get_or_create_async(
                    thread_id, user_id=user.user_id
                )
                chunk_count = await asyncio.to_thread(
                    index.add_document,
                    file_id=file_id,
                    content=parse_result.content,
                    file_name=file_name,
                )
                logger.info(
                    "[ChatFiles] 索引构建完成: file_id=%s, chunks=%d",
                    file_id,
                    chunk_count,
                )
            except Exception as e:
                logger.error("[ChatFiles] 索引构建失败: %s", e)

        async def _load_remote_images(
            file_id: str,
            bucket_name: str,
            parse_result,
            record_uploaded_object,
        ) -> list[dict]:
            return await _materialize_remote_image_urls(
                request,
                image_urls=parse_result.image_urls,
                owner_user_id=user.user_id,
                thread_id=thread_id,
                file_id=file_id,
                bucket_name=bucket_name,
                record_uploaded_object=record_uploaded_object,
            )

        try:
            stored_file = await prepare_uploaded_user_file(
                session=session,
                owner_user_id=user.user_id,
                data=data,
                filename=filename,
                mime_type=mime_type,
                storage_scope="chat",
                thread_id=thread_id,
                parse_mode=effective_parse_mode,
                image_url_builder=lambda file_id, image_name: _build_chat_file_image_url(
                    request,
                    file_id,
                    image_name,
                    owner_user_id=user.user_id,
                    thread_id=thread_id,
                ),
                on_parsed=_index_long_doc,
                extra_image_url_loader=_load_remote_images,
                replace_content_image_urls=True,
                include_parse_metadata=True,
            )
        except UserFileParseError as exc:
            file_size = len(data)
            file_id = f"uf_{uuid.uuid4().hex}"
            error_msg = exc.message or "解析服务异常"
            results.append(
                ChatFileUploadResult(
                    file_id=file_id,
                    file_name=filename,
                    file_size=file_size,
                    file_category="failed",
                    parse_status="failed",
                    strategy="none",
                    message=f"文件解析失败: {error_msg}",
                    error=error_msg,
                )
            )
            continue

        try:
            # Binding SessionFile record
            db_session_file = SessionFile(
                session_id=thread_id,
                user_id=user.user_id,
                file_id=stored_file.file_id,
                attached_from="upload",
            )
            session.add(db_session_file)
            await session.commit()
        except BaseException:
            try:
                await session.rollback()
            except Exception as rollback_exc:
                logger.exception(
                    "[ChatFiles] 数据库回滚失败: file_id=%s error=%s",
                    stored_file.file_id,
                    rollback_exc,
                )
            await cleanup_uploaded_minio_objects(
                stored_file.storage_bucket,
                stored_file.uploaded_object_keys,
            )
            raise

        # 构建 message
        msg_map = {
            "multimodal": "图片已就绪，将直接发送给模型 analysis",
            "short_doc": f"文档已解析（{stored_file.content_length} 字），将直接注入对话上下文",
            "long_doc": f"文档已解析（{stored_file.content_length} 字），已建立检索索引",
        }
        message = msg_map.get(stored_file.category, "文件已处理")

        results.append(
            ChatFileUploadResult(
                file_id=stored_file.file_id,
                file_name=filename,
                file_size=stored_file.size_bytes,
                file_category=stored_file.category,
                parse_status="completed",
                content_length=stored_file.content_length,
                strategy=stored_file.strategy,
                message=message,
                mime_type=stored_file.mime_type,
                table_analysis_enabled=stored_file.table_analysis_enabled,
                content_preview=(
                    (stored_file.content or "")[:200]
                    if stored_file.category == "short_doc"
                    else None
                ),
            )
        )

    logger.info(
        "[ChatFiles] 上传完成: thread_id=%s, 文件数=%d, 成功=%d",
        thread_id,
        len(files),
        sum(1 for r in results if r.parse_status == "completed"),
    )

    return {
        "success": True,
        "files": [r.model_dump() for r in results],
        "thread_id": thread_id,
    }


@router.get("/{thread_id}", response_model=dict)
async def list_chat_files(
    thread_id: str,
    skip: int = Query(0, ge=0),
    limit: int = Query(50, ge=1, le=200),
    user: User = Depends(get_current_user),
    session: AsyncSession = Depends(get_async_session),
):
    """列出会话中已上传的文件"""
    base_stmt = (
        select(UserFile)
        .join(SessionFile, SessionFile.file_id == UserFile.file_id)
        .where(
            SessionFile.session_id == thread_id,
            SessionFile.user_id == user.user_id,
            UserFile.status == "active"
        )
    )
    total = int(
        (
            await session.execute(
                select(func.count()).select_from(base_stmt.subquery())
            )
        ).scalar_one()
        or 0
    )
    stmt = base_stmt.order_by(SessionFile.created_at.desc()).offset(skip).limit(limit)
    result = await session.execute(stmt)
    user_files = result.scalars().all()

    items = []
    for row in user_files:
        m_json = row.metadata_ or {}
        items.append(
            ChatFileListItem(
                file_id=row.file_id,
                file_name=row.display_name,
                file_size=row.size_bytes or 0,
                file_category=m_json.get("file_category", "short_doc"),
                strategy=m_json.get("strategy", "context"),
                content_length=m_json.get("content_length", 0),
                table_analysis_enabled=bool((m_json.get("table_analysis") or {}).get("enabled")),
            ).model_dump()
        )
    return {"items": items, "count": len(items), "total": total}


@router.delete("/{file_id}", response_model=dict)
async def delete_chat_file(
    file_id: str,
    user: User = Depends(get_current_user),
    session: AsyncSession = Depends(get_async_session),
):
    """删除会话文件（会话解绑：仅删除 session_files 中的绑定关系）"""
    from backend.services.chat_file_index import get_index_manager

    # Find the session file association
    stmt = select(SessionFile).where(
        SessionFile.file_id == file_id,
        SessionFile.user_id == user.user_id
    )
    res = await session.execute(stmt)
    session_file = res.scalars().first()
    if not session_file:
        raise HTTPException(status_code=404, detail="关联不存在或无权操作该文件")

    # Get file category from UserFile
    file_stmt = select(UserFile).where(UserFile.file_id == file_id)
    file_res = await session.execute(file_stmt)
    user_file = file_res.scalars().first()

    # Remove index if long_doc
    if user_file:
        m_json = user_file.metadata_ or {}
        if m_json.get("file_category") == "long_doc" and session_file.session_id:
            index = await get_index_manager().get_async(
                session_file.session_id,
                user_id=user.user_id,
            )
            if index:
                await asyncio.to_thread(index.remove_document, file_id)

    # Delete the binding
    await session.delete(session_file)
    await session.commit()
    return {"success": True, "file_id": file_id}


@router.get("/{file_id}/images/{image_name}")
async def get_chat_file_image(
    file_id: str,
    image_name: str,
    exp: int | None = None,
    sig: str | None = None,
    session: AsyncSession = Depends(get_async_session),
):
    """公开返回对话文档图片，供浏览器或模型直接读取。"""
    stmt = select(UserFile).where(UserFile.file_id == file_id, UserFile.status == "active")
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

    if not _verify_chat_file_image_url(
        file_id=file_id,
        image_name=image_name,
        owner_user_id=row.owner_user_id,
        thread_id=row.thread_id,
        exp=exp,
        sig=sig,
    ):
        raise HTTPException(status_code=403, detail="图片访问签名无效或已过期")

    # The DB-provided storage_object is the only untrusted candidate; a row
    # whose metadata points outside this owner's prefixes must not be read
    # (the two derived candidates below are owner-scoped by construction).
    db_object = target.get("storage_object")
    candidate_objects = [
        db_object if _image_object_owned_by(db_object, row.owner_user_id) else None,
        f"users/{row.owner_user_id}/threads/{row.thread_id}/chat-files/{file_id}/images/{image_name}",
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
            "Content-Disposition": f'inline; filename="{(image_name or "").replace(chr(34), chr(39)).replace(chr(92), "_")}"',
        },
    )
