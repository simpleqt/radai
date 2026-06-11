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

import base64
import uuid
from pathlib import Path
from urllib.parse import quote
from typing import List, Optional

from fastapi import APIRouter, Depends, File, Form, HTTPException, Request, UploadFile
from fastapi.responses import StreamingResponse
from pydantic import BaseModel
from sqlalchemy import select, update
from sqlalchemy.ext.asyncio import AsyncSession

from backend.api.routers.auth import get_current_user
from backend.config.loggers import get_logger
from backend.db.connection import get_async_session
from backend.db.models import SessionFile, User, UserFile
from backend.services.chat_file_index import get_index_manager
from backend.services.chat_file_parser import ChatFileParser, clean_surrogates
from backend.storage import _get_minio_client, get_user_minio_bucket_name, upload_bytes_to_minio

logger = get_logger(__name__)

router = APIRouter(tags=["个人文件夹"])

# 文件大小上限：50MB
MAX_FILE_SIZE = 50 * 1024 * 1024

ALLOWED_EXTENSIONS: set[str] = {
    # 图片
    ".jpg", ".jpeg", ".png", ".gif", ".webp", ".bmp", ".svg",
    # 纯文本
    ".txt", ".md", ".csv", ".json", ".py", ".html", ".xml",
    ".log", ".yaml", ".yml", ".toml", ".rst",
    # 富文档
    ".pdf", ".docx", ".doc", ".pptx", ".ppt", ".xlsx", ".xls",
}


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


def _build_personal_file_image_url(request: Request, file_id: str, image_name: str) -> str:
    base_url = str(request.base_url).rstrip("/")
    return f"{base_url}/api/v1/my-files/{quote(file_id)}/images/{quote(image_name, safe='')}"


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

    data = await file.read()
    file_size = len(data)

    if file_size > MAX_FILE_SIZE:
        raise HTTPException(
            status_code=400,
            detail=f"文件过大: {file_size / 1024 / 1024:.1f}MB，上限 50MB",
        )

    file_id = f"uf_{uuid.uuid4().hex}"
    mime_type = file.content_type

    # 解析文件内容
    parser = ChatFileParser()
    try:
        parse_result = await parser.parse(data, filename, mime_type)
    except Exception as e:
        logger.exception("[PersonalFiles] 解析异常: %s", e)
        parse_result = None

    if parse_result is None or parse_result.category == "failed":
        error_msg = parse_result.error if parse_result else "解析服务异常"
        raise HTTPException(status_code=422, detail=f"文件解析失败: {error_msg}")

    # 确定处理策略
    strategy_map = {
        "multimodal": "vision",
        "short_doc": "context",
        "long_doc": "indexed",
    }
    strategy = strategy_map.get(parse_result.category, "none")

    # 上传到 MinIO
    bucket_name = get_user_minio_bucket_name()
    storage_object = f"users/{user.user_id}/personal-files/{file_id}/{filename}"
    
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
            img_object = f"users/{user.user_id}/personal-files/{file_id}/images/{img.name}"
            await upload_bytes_to_minio(
                content=img.data,
                object_name=img_object,
                content_type=img.content_type,
                bucket_name=bucket_name,
            )
            image_urls.append({
                "url": _build_personal_file_image_url(request, file_id, img.name),
                "name": img.name,
                "caption": img.caption,
                "content_type": img.content_type,
                "storage_object": img_object,
            })

    # 构造 metadata 负载
    metadata_payload = {
        "file_category": parse_result.category,
        "strategy": strategy,
        "content": parse_result.content,
        "content_length": parse_result.content_length,
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
    if parse_result.category == "multimodal":
        metadata_payload["image_base64"] = base64.b64encode(data).decode("utf-8")
    if image_urls:
        metadata_payload["image_urls"] = image_urls

    metadata_payload = clean_surrogates(metadata_payload)

    # 保存记录到数据库
    db_file = UserFile(
        file_id=file_id,
        owner_user_id=user.user_id,
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
    await session.commit()

    return {
        "success": True,
        "file": {
            "file_id": file_id,
            "display_name": filename,
            "size_bytes": file_size,
            "mime_type": mime_type,
            "download_url": f"/api/v2/files/{file_id}/download",
            "file_category": parse_result.category,
            "strategy": strategy,
            "table_analysis_enabled": bool(table_analysis_meta),
        }
    }


@router.get("/api/v1/my-files/{file_id}/images/{image_name}")
async def get_personal_file_image(
    file_id: str,
    image_name: str,
    session: AsyncSession = Depends(get_async_session),
):
    """公开返回个人文件图片，供浏览器/模型直接读取。"""
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

    candidate_objects = [
        target.get("storage_object"),
        f"users/{row.owner_user_id}/personal-files/{file_id}/images/{image_name}",
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
            "Content-Disposition": f'inline; filename=\"{image_name}\"',
        },
    )


@router.get("/api/v1/my-files", response_model=dict)
async def list_my_files(
    keyword: Optional[str] = None,
    source: Optional[str] = None,
    file_type: Optional[str] = None,
    limit: int = 20,
    offset: int = 0,
    user: User = Depends(get_current_user),
    session: AsyncSession = Depends(get_async_session),
):
    """获取个人文件夹中的文件列表（支持关键词搜索、来源与类型过滤）"""
    stmt = select(UserFile).where(
        UserFile.owner_user_id == user.user_id,
        UserFile.status == "active"
    )

    if keyword:
        stmt = stmt.where(UserFile.display_name.like(f"%{keyword}%"))

    if source:
        # source 对应 source_type
        stmt = stmt.where(UserFile.source_type == source)

    # 简单过滤类型
    if file_type:
        if file_type == "image":
            stmt = stmt.where(UserFile.mime_type.like("image/%"))
        elif file_type == "document":
            stmt = stmt.where(
                UserFile.display_name.like("%.pdf") |
                UserFile.display_name.like("%.docx") |
                UserFile.display_name.like("%.doc") |
                UserFile.display_name.like("%.txt") |
                UserFile.display_name.like("%.md")
            )
        elif file_type == "spreadsheet":
            stmt = stmt.where(
                UserFile.display_name.like("%.xlsx") |
                UserFile.display_name.like("%.xls") |
                UserFile.display_name.like("%.csv")
            )
        elif file_type == "presentation":
            stmt = stmt.where(
                UserFile.display_name.like("%.pptx") |
                UserFile.display_name.like("%.ppt")
            )

    # 计算总数
    count_stmt = select(UserFile).where(UserFile.owner_user_id == user.user_id, UserFile.status == "active")
    if keyword:
        count_stmt = count_stmt.where(UserFile.display_name.like(f"%{keyword}%"))
    if source:
        count_stmt = count_stmt.where(UserFile.source_type == source)
    
    # 获取总数与数据
    res_count = await session.execute(count_stmt)
    total_count = len(res_count.scalars().all())

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
                table_analysis_enabled=bool((m_json.get("table_analysis") or {}).get("enabled")),
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
        UserFile.status == "active"
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
        UserFile.status == "active"
    )
    result = await session.execute(stmt)
    db_file = result.scalars().first()

    if not db_file:
        raise HTTPException(status_code=404, detail="文件不存在或无权操作")

    db_file.status = "deleted"

    # 解绑所有关联的会话
    del_stmt = update(SessionFile).where(SessionFile.file_id == file_id).values(session_id="")
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
        UserFile.status == "active"
    )
    result = await session.execute(stmt)
    db_file = result.scalars().first()

    if not db_file:
        raise HTTPException(status_code=404, detail="要绑定的文件不存在")

    # 2. 检查是否已经绑定
    dup_stmt = select(SessionFile).where(
        SessionFile.session_id == session_id,
        SessionFile.file_id == payload.file_id
    )
    dup_res = await session.execute(dup_stmt)
    if dup_res.scalars().first():
        return {
            "success": True,
            "message": "文件已绑定到该会话，无需重复绑定",
            "file_id": payload.file_id
        }

    # 3. 创建绑定关系
    db_session_file = SessionFile(
        session_id=session_id,
        user_id=user.user_id,
        file_id=payload.file_id,
        attached_from="my_files"
    )
    session.add(db_session_file)

    # 4. 如果是长文档，需要在会话中构建检索索引
    m_json = db_file.metadata_ or {}
    if m_json.get("file_category") == "long_doc" and m_json.get("content"):
        try:
            index_manager = get_index_manager()
            index = index_manager.get_or_create(session_id)
            chunk_count = index.add_document(
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
        "session_id": session_id
    }


@router.post("/api/v1/generated-files/save")
async def save_generated_file(
    payload: SaveGeneratedFileRequest,
    user: User = Depends(get_current_user),
    session: AsyncSession = Depends(get_async_session),
):
    """保存系统生成的文件到个人文件夹"""
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
        file_name=payload.file_name,
        mime_type=payload.mime_type,
        storage_bucket=payload.storage_bucket,
        storage_object=payload.storage_object,
        local_abs_path=payload.local_abs_path,
    )
    if table_analysis_meta:
        metadata_payload["table_analysis"] = table_analysis_meta

    db_file = UserFile(
        file_id=file_id,
        owner_user_id=user.user_id,
        source_type="minio" if payload.storage_object else "local",
        storage_bucket=payload.storage_bucket,
        storage_object=payload.storage_object,
        local_abs_path=payload.local_abs_path,
        display_name=payload.file_name,
        mime_type=payload.mime_type or "application/octet-stream",
        size_bytes=payload.size_bytes or 0,
        status="active",
        metadata_=metadata_payload,
    )
    session.add(db_file)
    await session.commit()

    return {
        "success": True,
        "file_id": file_id,
        "display_name": payload.file_name,
        "download_url": f"/api/v2/files/{file_id}/download"
    }
