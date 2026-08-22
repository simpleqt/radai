"""探索资源域:资源标题清洗、下载 URL、MIME/Disposition、KB 文档存储解析与下载。(拆自 explore/service.py)"""

from __future__ import annotations

from backend.config.loggers import get_logger

from pathlib import Path
from sqlalchemy import or_
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession
from typing import Any
from urllib.parse import quote
import asyncio
import mimetypes
import re

logger = get_logger(__name__)


def _strip_resource_noise(value: Any, *, max_len: int | None = None) -> str:
    """清理知识库召回片段中的 HTML、链接和元信息噪音。"""
    text = str(value or "")
    text = re.sub(r"<!doctype[^>]*>", " ", text, flags=re.I)
    text = re.sub(r"<script[\s\S]*?</script>", " ", text, flags=re.I)
    text = re.sub(r"<style[\s\S]*?</style>", " ", text, flags=re.I)
    text = re.sub(r"<[^>]+>", " ", text)
    text = re.sub(r"https?://\S+", " ", text)
    text = re.sub(r"&nbsp;|&#160;", " ", text, flags=re.I)
    text = re.sub(r"&amp;", "&", text, flags=re.I)
    text = re.sub(r"&lt;|&gt;|&quot;|&#39;", " ", text, flags=re.I)
    text = re.sub(
        r"\b(charset|viewport|initial-scale|device-width|base href|content=|meta name)\b[^，。；\n]*",
        " ",
        text,
        flags=re.I,
    )
    text = re.sub(r"\s+", " ", text).strip(" -_:\t\r\n")
    if max_len and len(text) > max_len:
        return f"{text[:max_len].rstrip()}..."
    return text


def _looks_like_machine_id(value: str) -> bool:
    text = (value or "").strip()
    return bool(
        re.match(r"^doc[-_][a-z0-9-]{12,}$", text, flags=re.I)
        or re.match(r"^[a-f0-9]{20,}$", text, flags=re.I)
        or re.match(r"^chunk[-_][a-z0-9-]{10,}$", text, flags=re.I)
    )


def _derive_resource_title(*candidates: Any) -> str:
    for candidate in candidates:
        text = _strip_resource_noise(candidate, max_len=72)
        if text and not _looks_like_machine_id(text) and not text.lower().startswith("visual analysis"):
            return text
    return "知识库文档"


def _title_from_content(content: str) -> str:
    text = _strip_resource_noise(content, max_len=96)
    if not text:
        return ""
    for sep in ("。", "；", ";", "\n"):
        if sep in text:
            text = text.split(sep, 1)[0]
            break
    text = text.strip(" -_：:")
    if text.lower().startswith("visual analysis"):
        text = text.replace("Visual Analysis:", "", 1).strip(" -_：:")
    return _strip_resource_noise(text, max_len=64)


def build_resource_download_url(kb_id: str | None, doc_id: str | None) -> str:
    """构建知识库文档下载 URL（非用户文件中心 file_id）。"""
    kb = str(kb_id or "").strip()
    doc = str(doc_id or "").strip()
    if not kb or not doc:
        return ""
    return f"/api/v2/explore/resources/download?kb_id={quote(kb)}&doc_id={quote(doc)}"


def _guess_mime_type(filename: str) -> str:
    mime, _ = mimetypes.guess_type(filename)
    return mime or "application/octet-stream"


def _allowed_local_roots() -> list[Path]:
    # 与 pending 入库缓存（pending_ingest 的 data/cache）同一根：
    # DB 里的绝对 file_path 只可能来自该缓存，其余一律拒绝。
    from pathlib import Path as _Path

    from backend.config import settings

    roots = []
    for candidate in (settings.cache_path,):
        try:
            root = _Path(candidate).resolve()
        except Exception:  # noqa: BLE001
            continue
        if root.exists():
            roots.append(root)
    fallback = _Path(__file__).resolve().parents[2] / "data" / "cache"
    if fallback.exists():
        roots.append(fallback.resolve())
    return roots


def _build_content_disposition(filename: str) -> str:
    display_name = (filename or "download").strip() or "download"
    try:
        display_name.encode("ascii")
        # 引号/反斜杠会从 quoted-string 逃逸，伪造额外 filename 参数
        safe_name = display_name.replace('"', "'").replace("\\", "_")
        return f'attachment; filename="{safe_name}"'
    except UnicodeEncodeError:
        encoded = quote(display_name, safe="")
        return f'attachment; filename="download"; filename*=UTF-8\'\'{encoded}'


async def _resolve_kb_document_storage(
    session: AsyncSession,
    kb_id: str,
    doc_id: str,
) -> tuple[str, str, str]:
    """解析知识库文档的存储位置，返回 (storage_key, display_name, source_kind)。"""
    from backend.db.crud.knowledge_base_crud import KnowledgeBaseDocumentCRUD
    from backend.db.models import PendingFile

    doc_crud = KnowledgeBaseDocumentCRUD()
    doc = await doc_crud.get_by_doc_id(session, kb_id, doc_id)
    if not doc:
        raise ValueError("文档不存在")

    file_name = str(doc.file_name or "download").strip() or "download"
    file_path = str(doc.file_path or "").strip()

    if file_path:
        path_obj = Path(file_path)
        if path_obj.is_absolute() and path_obj.exists() and path_obj.is_file():
            # DB 绝对路径不能无条件信任：当前写入方都是服务端拼的
            # pending/ 相对键或受控缓存路径，但一旦有运维脚本把任意绝对
            # 路径写进该列，这里就是任意文件读取。限定到已知根下。
            allowed_roots = _allowed_local_roots()
            if not any(
                path_obj.resolve().is_relative_to(root) for root in allowed_roots
            ):
                raise ValueError("文档存储路径不在允许范围内")
            return str(path_obj), file_name, "local"

        normalized = file_path.replace("\\", "/").lstrip("/")
        if normalized.startswith(("pending/", "files/", "kb/", "uploads/")):
            return normalized, file_name, "minio"

    if file_path:
        pending_by_path = await session.execute(
            select(PendingFile)
            .where(PendingFile.kb_id == kb_id)
            .where(PendingFile.minio_path == file_path)
            .where(PendingFile.status == 2)
            .order_by(PendingFile.created_at.desc())
            .limit(1)
        )
        pending = pending_by_path.scalar_one_or_none()
        if pending:
            return pending.minio_path, file_name, "minio"

    pending_by_name = await session.execute(
        select(PendingFile)
        .where(PendingFile.kb_id == kb_id)
        .where(PendingFile.status == 2)
        .where(
            or_(
                PendingFile.file_name == file_name,
                PendingFile.file_name == file_path,
            )
        )
        .order_by(PendingFile.created_at.desc())
        .limit(1)
    )
    pending = pending_by_name.scalar_one_or_none()
    if pending:
        return pending.minio_path, file_name, "minio"

    if file_path:
        cache_candidate = Path(__file__).resolve().parents[2] / "data" / "cache" / Path(file_path).name
        if cache_candidate.exists() and cache_candidate.is_file():
            return str(cache_candidate), file_name, "local"

    raise ValueError("找不到文档原始文件")


async def download_kb_resource(
    session: AsyncSession,
    kb_id: str,
    doc_id: str,
    *,
    stream: bool = False,
) -> tuple[Any | Path, str, str, str]:
    """下载知识库文档，返回 (payload_or_path, filename, media_type, source_kind)。"""
    storage_key, filename, source_kind = await _resolve_kb_document_storage(session, kb_id, doc_id)
    # 定位查询后立即归还连接：MinIO 下载/流式回传期间不钉住 MySQL。
    await session.rollback()
    media_type = _guess_mime_type(filename)

    if source_kind == "local":
        return Path(storage_key), filename, media_type, source_kind

    from backend.config import settings
    from backend.storage import _get_minio_client, _normalize_minio_object_name

    def _read_minio() -> Any:
        client = _get_minio_client()
        candidates = [storage_key]
        normalized = _normalize_minio_object_name(storage_key)
        if normalized and normalized not in candidates:
            candidates.append(normalized)
        last_error: Exception | None = None
        for candidate in candidates:
            try:
                response = client.get_object(settings.minio_bucket_name, candidate)
                if stream:
                    return response
                try:
                    return response.read()
                finally:
                    try:
                        response.close()
                    finally:
                        response.release_conn()
            except Exception as exc:  # noqa: BLE001
                last_error = exc
        assert last_error is not None
        raise last_error

    content = await asyncio.to_thread(_read_minio)
    return content, filename, media_type, source_kind
