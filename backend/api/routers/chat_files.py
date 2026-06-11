"""
对话文件上传路由

POST /api/v2/chat-files/upload  - 上传并解析文件
GET  /api/v2/chat-files/{thread_id}  - 列出会话中的文件
DELETE /api/v2/chat-files/{file_id}  - 删除文件
"""

from __future__ import annotations

import base64
import hashlib
import hmac
import time
import uuid
from pathlib import Path
from urllib.parse import quote, urlparse
from typing import List, Optional

import httpx
from fastapi import APIRouter, Depends, File, Form, HTTPException, Request, UploadFile
from fastapi.responses import StreamingResponse
from pydantic import BaseModel
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from backend.api.routers.auth import get_current_user
from backend.config.loggers import get_logger
from backend.config.settings import settings
from backend.db.connection import get_async_session, get_session_factory
from backend.db.models import User, UserFile, SessionFile
from backend.storage import _get_minio_client, get_user_minio_bucket_name, upload_bytes_to_minio

logger = get_logger(__name__)

router = APIRouter(prefix="/chat-files", tags=["对话文件"])

# 文件大小上限：50MB
MAX_FILE_SIZE = 50 * 1024 * 1024

# 每次上传最多文件数
MAX_FILES_PER_REQUEST = 10
CHAT_FILE_IMAGE_URL_TTL_SECONDS = 7 * 24 * 60 * 60

ALLOWED_EXTENSIONS: set[str] = {
    # 图片
    ".jpg", ".jpeg", ".png", ".gif", ".webp", ".bmp", ".svg",
    # 纯文本
    ".txt", ".md", ".csv", ".json", ".py", ".html", ".xml",
    ".log", ".yaml", ".yml", ".toml", ".rst",
    # 富文档
    ".pdf", ".docx", ".doc", ".pptx", ".ppt", ".xlsx", ".xls",
}


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
    error: Optional[str] = None
    # 多模态文件的 MIME 类型
    mime_type: Optional[str] = None
    # 短文档内容（用于前端立即预览）
    content_preview: Optional[str] = None
    table_analysis_enabled: bool = False


class ChatFileListItem(BaseModel):
    file_id: str
    file_name: str
    file_size: int
    file_category: str
    strategy: str
    content_length: int = 0
    table_analysis_enabled: bool = False


# Helper to convert DB model to old dictionary metadata format
def _db_row_to_meta(row: UserFile) -> dict:
    meta = {
        "file_id": row.file_id,
        "thread_id": row.thread_id,
        "user_id": row.owner_user_id,
        "file_name": row.display_name,
        "file_size": row.size_bytes or 0,
        "mime_type": row.mime_type,
    }
    m_json = row.metadata_ or {}
    meta["file_category"] = m_json.get("file_category", "short_doc")
    meta["strategy"] = m_json.get("strategy", "context")
    meta["content"] = m_json.get("content", "")
    meta["content_length"] = m_json.get("content_length", 0)
    meta["storage_bucket"] = row.storage_bucket
    meta["storage_object"] = row.storage_object
    meta["local_abs_path"] = row.local_abs_path
    if "table_analysis" in m_json:
        meta["table_analysis"] = m_json["table_analysis"]
    if "image_base64" in m_json:
        meta["image_base64"] = m_json["image_base64"]
    if "image_urls" in m_json:
        refreshed_urls: list[dict] = []
        for item in m_json["image_urls"] or []:
            current = dict(item)
            image_name = str(current.get("name") or "").strip()
            if image_name:
                existing_url = str(current.get("url") or "").strip()
                parsed = urlparse(existing_url)
                if parsed.scheme and parsed.netloc:
                    base_url = f"{parsed.scheme}://{parsed.netloc}"
                    expires_at = int(time.time()) + CHAT_FILE_IMAGE_URL_TTL_SECONDS
                    sig = _build_chat_image_signature(
                        file_id=row.file_id,
                        image_name=image_name,
                        owner_user_id=row.owner_user_id,
                        thread_id=row.thread_id,
                        expires_at=expires_at,
                    )
                    current["url"] = (
                        f"{base_url}/api/v2/chat-files/{quote(row.file_id)}/images/{quote(image_name, safe='')}"
                        f"?exp={expires_at}&sig={sig}"
                    )
            refreshed_urls.append(current)
        meta["image_urls"] = refreshed_urls
    return meta


def _build_chat_image_signature(
    *,
    file_id: str,
    image_name: str,
    owner_user_id: str,
    thread_id: str,
    expires_at: int,
) -> str:
    secret = (settings.minio_secret_key or "").encode("utf-8")
    payload = "\n".join([file_id, image_name, owner_user_id, thread_id, str(expires_at)]).encode("utf-8")
    return hmac.new(secret, payload, hashlib.sha256).hexdigest()


def _build_chat_file_image_url(
    request: Request,
    file_id: str,
    image_name: str,
    *,
    owner_user_id: str,
    thread_id: str,
    expires_in: int = CHAT_FILE_IMAGE_URL_TTL_SECONDS,
) -> str:
    base_url = str(request.base_url).rstrip("/")
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
) -> list[dict]:
    if not image_urls:
        return []

    materialized: list[dict] = []
    async with httpx.AsyncClient(timeout=60.0) as client:
        for idx, item in enumerate(image_urls, start=1):
            raw_url = str(item.get("url") or "").strip()
            if not raw_url.startswith(("http://", "https://")):
                continue
            try:
                response = await client.get(raw_url)
                response.raise_for_status()
                content = response.content
                if not content:
                    continue
                parsed = urlparse(raw_url)
                guessed_name = Path(parsed.path).name or f"remote_image_{idx}.png"
                image_name = Path(
                    str(item.get("name") or guessed_name or f"remote_image_{idx}.png")
                ).name or f"remote_image_{idx}.png"
                content_type = str(
                    item.get("content_type")
                    or response.headers.get("content-type")
                    or "image/png"
                )
                storage_object = (
                    f"users/{owner_user_id}/threads/{thread_id}/chat-files/{file_id}/images/{image_name}"
                )
                await upload_bytes_to_minio(
                    content=content,
                    object_name=storage_object,
                    content_type=content_type,
                    bucket_name=bucket_name,
                )
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
    files: List[UploadFile] = File(..., description="上传的文件列表"),
    user: User = Depends(get_current_user),
    session: AsyncSession = Depends(get_async_session),
):
    """上传并解析对话文件

    自动根据文件类型选择处理策略：
    - 图片 → 多模态（strategy=vision）
    - 短文档（≤5000字）→ 直接注入上下文（strategy=context）
    - 长文档（>5000字）→ 建立会话级索引（strategy=indexed）
    """
    from backend.services.chat_file_parser import (
        ChatFileParser,
        clean_surrogates,
        replace_local_images_with_urls,
    )
    from backend.services.chat_file_index import get_index_manager

    if len(files) > MAX_FILES_PER_REQUEST:
        raise HTTPException(
            status_code=400,
            detail=f"单次最多上传 {MAX_FILES_PER_REQUEST} 个文件",
        )

    parser = ChatFileParser()
    index_manager = get_index_manager()
    results: List[ChatFileUploadResult] = []
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

        data = await upload_file.read()
        file_size = len(data)

        if file_size > MAX_FILE_SIZE:
            results.append(
                ChatFileUploadResult(
                    file_id=str(uuid.uuid4()),
                    file_name=filename,
                    file_size=file_size,
                    file_category="failed",
                    parse_status="failed",
                    strategy="none",
                    message=f"文件过大: {file_size / 1024 / 1024:.1f}MB，上限 50MB",
                    error="文件大小超过限制",
                )
            )
            continue

        file_id = f"uf_{uuid.uuid4().hex}"
        mime_type = upload_file.content_type

        try:
            parse_result = await parser.parse(
                data,
                filename,
                mime_type,
                parse_mode=effective_parse_mode,
            )
        except Exception as e:
            logger.exception("[ChatFiles] 解析异常: %s", e)
            parse_result = None

        if parse_result is None or parse_result.category == "failed":
            error_msg = (parse_result.error if parse_result else "解析服务异常")
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

        # 确定策略
        strategy_map = {
            "multimodal": "vision",
            "short_doc": "context",
            "long_doc": "indexed",
        }
        strategy = strategy_map.get(parse_result.category, "none")

        # 长文档：建立索引
        if parse_result.category == "long_doc" and parse_result.content:
            try:
                index = index_manager.get_or_create(thread_id)
                chunk_count = index.add_document(
                    file_id=file_id,
                    content=parse_result.content,
                    file_name=filename,
                )
                logger.info(
                    "[ChatFiles] 索引构建完成: file_id=%s, chunks=%d",
                    file_id,
                    chunk_count,
                )
            except Exception as e:
                logger.error("[ChatFiles] 索引构建失败: %s", e)

        # Upload file to MinIO
        bucket_name = get_user_minio_bucket_name()
        storage_object = f"users/{user.user_id}/threads/{thread_id}/chat-files/{file_id}/{filename}"
        await upload_bytes_to_minio(
            content=data,
            object_name=storage_object,
            content_type=mime_type or "application/octet-stream",
            bucket_name=bucket_name,
        )

        # 上传提取的图片到 MinIO 并收集 URL
        image_urls: list[dict] = []
        if parse_result.images:
            from backend.services.chat_file_parser import compress_image_if_needed
            for img in parse_result.images:
                img = compress_image_if_needed(img)
                img_object = f"users/{user.user_id}/threads/{thread_id}/chat-files/{file_id}/images/{img.name}"
                await upload_bytes_to_minio(
                    content=img.data,
                    object_name=img_object,
                    content_type=img.content_type,
                    bucket_name=bucket_name,
                )
                image_urls.append({
                    "url": _build_chat_file_image_url(
                        request,
                        file_id,
                        img.name,
                        owner_user_id=user.user_id,
                        thread_id=thread_id,
                    ),
                    "name": img.name,
                    "caption": img.caption,
                    "content_type": img.content_type,
                    "storage_object": img_object,
                })
            logger.info(
                "[ChatFiles] 上传图片到 MinIO: file_id=%s, 图片数=%d",
                file_id, len(image_urls),
            )
        if getattr(parse_result, "image_urls", None):
            image_urls.extend(
                await _materialize_remote_image_urls(
                    request,
                    image_urls=parse_result.image_urls,
                    owner_user_id=user.user_id,
                    thread_id=thread_id,
                    file_id=file_id,
                    bucket_name=bucket_name,
                )
            )

        image_url_map: dict[str, str] = {}
        for img_info in image_urls:
            url = img_info.get("url")
            if not url:
                continue
            for key in {
                img_info.get("name", ""),
                Path(img_info.get("name", "")).name,
            }:
                if key:
                    image_url_map.setdefault(key, url)

        # 保存文件元信息到数据库
        # 多模态文件额外存储 base64 数据供构建多模态消息使用
        metadata_payload = {
            "file_category": parse_result.category,
            "strategy": strategy,
            "content": parse_result.content,
            "content_length": parse_result.content_length,
            "parse_mode": effective_parse_mode,
            "parse_method": parse_result.method,
        }
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
        if metadata_payload.get("content") and image_url_map:
            metadata_payload["content"] = replace_local_images_with_urls(
                metadata_payload["content"],
                image_url_map,
            )
        if parse_result.category == "multimodal":
            metadata_payload["image_base64"] = base64.b64encode(data).decode("utf-8")
        if image_urls:
            metadata_payload["image_urls"] = image_urls

        metadata_payload = clean_surrogates(metadata_payload)

        db_file = UserFile(
            file_id=file_id,
            owner_user_id=user.user_id,
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

        # Binding SessionFile record
        db_session_file = SessionFile(
            session_id=thread_id,
            user_id=user.user_id,
            file_id=file_id,
            attached_from="upload",
        )
        session.add(db_session_file)

        # Commit changes
        await session.commit()

        # 构建 message
        msg_map = {
            "multimodal": "图片已就绪，将直接发送给模型 analysis",
            "short_doc": f"文档已解析（{parse_result.content_length} 字），将直接注入对话上下文",
            "long_doc": f"文档已解析（{parse_result.content_length} 字），已建立检索索引",
        }
        message = msg_map.get(parse_result.category, "文件已处理")

        results.append(
            ChatFileUploadResult(
                file_id=file_id,
                file_name=filename,
                file_size=file_size,
                file_category=parse_result.category,
                parse_status="completed",
                content_length=parse_result.content_length,
                strategy=strategy,
                message=message,
                mime_type=parse_result.mime_type or mime_type,
                table_analysis_enabled=bool(table_analysis_meta),
                content_preview=(
                    (parse_result.content or "")[:200]
                    if parse_result.category == "short_doc"
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
    user: User = Depends(get_current_user),
    session: AsyncSession = Depends(get_async_session),
):
    """列出会话中已上传的文件"""
    stmt = (
        select(UserFile)
        .join(SessionFile, SessionFile.file_id == UserFile.file_id)
        .where(
            SessionFile.session_id == thread_id,
            SessionFile.user_id == user.user_id,
            UserFile.status == "active"
        )
        .order_by(SessionFile.created_at.desc())
    )
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
    return {"items": items, "count": len(items)}


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
            index = get_index_manager().get(session_file.session_id)
            if index:
                index.remove_document(file_id)

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

    candidate_objects = [
        target.get("storage_object"),
        f"users/{row.owner_user_id}/threads/{row.thread_id}/chat-files/{file_id}/images/{image_name}",
        f"chat_files/{row.owner_user_id}/{file_id}/images/{image_name}",
    ]

    client = _get_minio_client()
    bucket = row.storage_bucket or get_user_minio_bucket_name()
    response = None
    last_exc: Exception | None = None
    for object_name in candidate_objects:
        if not object_name:
            continue
        try:
            response = client.get_object(bucket, object_name)
            break
        except Exception as exc:
            last_exc = exc
            response = None
    if response is None:
        raise HTTPException(status_code=404, detail=f"图片对象不存在: {last_exc}")

    media_type = target.get("content_type") or "image/png"

    def iterator():
        try:
            for chunk in response.stream(64 * 1024):
                yield chunk
        finally:
            response.close()
            response.release_conn()

    return StreamingResponse(
        iterator(),
        media_type=media_type,
        headers={
            "Cache-Control": "public, max-age=86400",
            "Content-Disposition": f'inline; filename="{image_name}"',
        },
    )


# ── 内部工具函数（供 agent.py 使用） ─────────────────────

def get_file_meta(file_id: str) -> Optional[dict]:
    """获取文件元信息"""
    from backend.db.models import UserFile
    from sqlalchemy import select

    factory = get_session_factory()
    with factory() as session:
        stmt = select(UserFile).where(UserFile.file_id == file_id, UserFile.status == "active")
        row = session.execute(stmt).scalars().first()
        if not row:
            return None
        return _db_row_to_meta(row)


def get_files_by_thread(thread_id: str, user_id: str) -> list[dict]:
    """获取会话下所有文件元信息"""
    from backend.db.models import SessionFile, UserFile
    from sqlalchemy import select

    factory = get_session_factory()
    with factory() as session:
        stmt = (
            select(UserFile)
            .join(SessionFile, SessionFile.file_id == UserFile.file_id)
            .where(
                SessionFile.session_id == thread_id,
                SessionFile.user_id == user_id,
                UserFile.status == "active"
            )
        )
        rows = session.execute(stmt).scalars().all()
        return [_db_row_to_meta(row) for row in rows]
