"""文件存储管理模块"""

import asyncio
import fcntl
import hashlib
import hmac
import json
import os
import re
import shutil
import threading
import time
import uuid
from collections.abc import Generator
from contextlib import asynccontextmanager, contextmanager
from datetime import datetime, timedelta
from pathlib import Path
from typing import Any
from urllib.parse import quote, unquote, urlparse, urlunparse

from backend.config import settings
from backend.config.loggers import get_logger
from backend.services.minio_paths import normalize_minio_endpoint

logger = get_logger(__name__)

_minio_client = None
_minio_client_lock = threading.Lock()


def _bounded_float_env(name: str, default: float, *, minimum: float) -> float:
    try:
        return max(minimum, float(os.getenv(name, str(default)) or default))
    except (TypeError, ValueError):
        return default


def _bounded_int_env(name: str, default: int, *, minimum: int = 1) -> int:
    try:
        return max(minimum, int(os.getenv(name, str(default)) or default))
    except (TypeError, ValueError):
        return default


async def _await_thread_completion(func, /, *args, **kwargs):
    """Run blocking storage I/O with bounded normal and cancellation waits."""
    operation = asyncio.create_task(asyncio.to_thread(func, *args, **kwargs))
    operation_timeout = _bounded_float_env(
        "STORAGE_BLOCKING_TIMEOUT_SECONDS",
        120.0,
        minimum=0.1,
    )
    cancel_drain_timeout = _bounded_float_env(
        "STORAGE_CANCEL_DRAIN_TIMEOUT_SECONDS",
        5.0,
        minimum=0.0,
    )

    def consume_result(completed: asyncio.Task) -> None:
        try:
            completed.result()
        except (asyncio.CancelledError, Exception):
            pass

    try:
        return await asyncio.wait_for(
            asyncio.shield(operation),
            timeout=operation_timeout,
        )
    except asyncio.TimeoutError as exc:  # noqa: UP041 -- Python 3.10 compatibility
        operation.add_done_callback(consume_result)
        raise TimeoutError(
            f"对象存储操作超过 {operation_timeout:.1f}s"
        ) from exc
    except asyncio.CancelledError:
        try:
            await asyncio.wait_for(
                asyncio.shield(operation),
                timeout=cancel_drain_timeout,
            )
        except (asyncio.TimeoutError, asyncio.CancelledError):  # noqa: UP041
            operation.add_done_callback(consume_result)
            logger.warning(
                "对象存储操作在取消后 %.1fs 内未结束，停止等待",
                cancel_drain_timeout,
            )
        except Exception as exc:
            logger.warning("被取消的对象存储操作最终失败: %s", exc)
        raise


def get_user_minio_bucket_name() -> str:
    """Return user-data bucket, falling back to default bucket for compatibility."""
    bucket = (getattr(settings, "minio_user_bucket_name", None) or "").strip()
    if bucket:
        return bucket
    return settings.minio_bucket_name


def _get_minio_client():
    global _minio_client
    if _minio_client is not None:
        return _minio_client
    with _minio_client_lock:
        if _minio_client is not None:
            return _minio_client
        from minio import Minio
        from urllib3 import PoolManager, Retry, Timeout

        endpoint = normalize_minio_endpoint(settings.minio_endpoint)
        secure = settings.minio_secure
        connect_timeout = _bounded_float_env(
            "MINIO_CONNECT_TIMEOUT_SECONDS",
            3.0,
            minimum=0.1,
        )
        read_timeout = _bounded_float_env(
            "MINIO_READ_TIMEOUT_SECONDS",
            60.0,
            minimum=0.1,
        )
        client = Minio(
            endpoint,
            access_key=settings.minio_access_key,
            secret_key=settings.minio_secret_key,
            secure=secure,
            http_client=PoolManager(
                num_pools=_bounded_int_env("MINIO_HTTP_POOL_COUNT", 20),
                maxsize=_bounded_int_env("MINIO_HTTP_POOL_MAXSIZE", 100),
                block=True,
                timeout=Timeout(connect=connect_timeout, read=read_timeout),
                retries=Retry(total=2, backoff_factor=0.2),
            ),
        )
        try:
            if not client.bucket_exists(settings.minio_bucket_name):
                client.make_bucket(settings.minio_bucket_name)
            user_bucket = get_user_minio_bucket_name()
            if (
                user_bucket != settings.minio_bucket_name
                and not client.bucket_exists(user_bucket)
            ):
                client.make_bucket(user_bucket)
        except Exception:
            logger.warning("MinIO bucket 初始化失败", exc_info=True)
        _minio_client = client
        return _minio_client


def download_minio_object_to_path(
    minio_path: str,
    target_path: str | Path,
    *,
    bucket_name: str | None = None,
    chunk_size: int = 1024 * 1024,
) -> Path:
    """Stream one object to an atomic local file without materializing bytes."""

    client = _get_minio_client()
    destination = Path(target_path)
    destination.parent.mkdir(parents=True, exist_ok=True)
    temporary = destination.with_name(
        f".{destination.name}.{os.getpid()}.{uuid.uuid4().hex}.part"
    )
    candidates = [minio_path]
    normalized = _normalize_minio_object_name(minio_path)
    if normalized and normalized not in candidates:
        candidates.append(normalized)
    bucket = bucket_name or settings.minio_bucket_name
    last_error: Exception | None = None

    try:
        for candidate in candidates:
            response = None
            try:
                response = client.get_object(bucket, candidate)
                with temporary.open("wb") as output:
                    while True:
                        chunk = response.read(max(64 * 1024, int(chunk_size)))
                        if not chunk:
                            break
                        output.write(chunk)
                os.replace(temporary, destination)
                return destination
            except Exception as exc:
                last_error = exc
                try:
                    temporary.unlink()
                except FileNotFoundError:
                    pass
            finally:
                if response is not None:
                    response.close()
                    response.release_conn()
    finally:
        try:
            temporary.unlink()
        except FileNotFoundError:
            pass

    assert last_error is not None
    raise last_error


def sha256_file(path: str | Path, *, chunk_size: int = 1024 * 1024) -> str:
    digest = hashlib.sha256()
    with Path(path).open("rb") as source:
        while chunk := source.read(max(64 * 1024, int(chunk_size))):
            digest.update(chunk)
    return digest.hexdigest()


def _normalize_minio_object_name(minio_path: str) -> str:
    """Normalize a MinIO path or public URL into a raw object key.

    This accepts the canonical object key (e.g. pending/kb/file.pdf) and also
    tolerates legacy values accidentally stored as public /files/ URLs or as
    bucket-prefixed paths.
    """
    raw_path = (minio_path or "").strip()
    if not raw_path:
        return raw_path

    parsed = urlparse(raw_path)
    if parsed.scheme and parsed.netloc:
        raw_path = parsed.path

    raw_path = raw_path.lstrip("/")
    if not raw_path:
        return raw_path

    bucket_name = (settings.minio_bucket_name or "").strip("/")
    if raw_path.startswith("files/"):
        raw_path = raw_path[len("files/"):]
    elif bucket_name and raw_path.startswith(f"{bucket_name}/"):
        raw_path = raw_path[len(bucket_name) + 1 :]

    if (parsed.scheme and parsed.netloc or raw_path.startswith("files/") or (bucket_name and raw_path.startswith(f"{bucket_name}/"))) and "%" in raw_path:
        raw_path = unquote(raw_path)

    return raw_path.lstrip("/")


async def upload_bytes_to_minio(
    content: bytes,
    object_name: str,
    content_type: str = "application/octet-stream",
    bucket_name: str | None = None,
) -> str | None:
    """异步上传到 MinIO，避免阻塞事件循环"""
    try:
        client = await asyncio.to_thread(_get_minio_client)
        from io import BytesIO

        data = BytesIO(content)
        target_bucket = bucket_name or settings.minio_bucket_name

        def _sync_put():
            client.put_object(
                target_bucket,
                object_name,
                data,
                length=len(content),
                content_type=content_type,
            )

        # A thread cannot be force-cancelled. Wait for it to settle so callers
        # can safely run compensating deletion without racing a late upload.
        await _await_thread_completion(_sync_put)
        return object_name
    except Exception:
        logger.warning(
            "MinIO 异步上传失败: bucket=%s object=%s",
            bucket_name or settings.minio_bucket_name,
            object_name,
            exc_info=True,
        )
        return None


def upload_bytes_to_minio_sync(
    content: bytes,
    object_name: str,
    content_type: str = "application/octet-stream",
    bucket_name: str | None = None,
) -> str | None:
    """同步上传到 MinIO（仅用于非异步上下文）"""
    try:
        client = _get_minio_client()
        from io import BytesIO

        data = BytesIO(content)
        target_bucket = bucket_name or settings.minio_bucket_name
        client.put_object(
            target_bucket,
            object_name,
            data,
            length=len(content),
            content_type=content_type,
        )
        return object_name
    except Exception:
        logger.warning(
            "MinIO 同步上传失败: bucket=%s object=%s",
            bucket_name or settings.minio_bucket_name,
            object_name,
            exc_info=True,
        )
        return None


def get_presigned_url(
    object_name: str, expiry_seconds: int = 24 * 60 * 60, bucket_name: str | None = None
) -> str | None:
    try:
        client = _get_minio_client()
        target_bucket = bucket_name or settings.minio_bucket_name
        url = client.presigned_get_object(
            target_bucket,
            object_name,
            expires=timedelta(seconds=expiry_seconds),
        )
        return url
    except Exception:
        logger.warning(
            "生成 MinIO 预签名 URL 失败: bucket=%s object=%s",
            bucket_name or settings.minio_bucket_name,
            object_name,
            exc_info=True,
        )
        return None


def get_public_url(object_name: str) -> str | None:
    try:
        base = (settings.minio_public_base_url or settings.minio_endpoint or "").rstrip("/")
        bucket = (settings.minio_bucket_name or "").strip("/")
        obj = (object_name or "").lstrip("/")
        if not base or not bucket or not obj:
            return None
        # MinIO object keys may contain literal %XX sequences (e.g. %2B, %E5%9F%BA).
        # When MinIO HTTP server receives a URL, it decodes %XX once before
        # looking up the object key. To match keys with literal %, we must
        # double-encode: % -> %25, so that after HTTP decode we get the original %.
        #
        # STRATEGY: Instead of unquote()+quote() (which corrupts incomplete %XX
        # sequences like %E5%8... into %EF%BF%BD%258...), we directly encode
        # the raw object name. We split the path by %XX sequences and encode
        # the non-percent parts with quote(), while preserving %XX as-is
        # (they will become %25XX in the URL for MinIO HTTP double-encoding).
        #
        # This avoids the unquote() step that can produce replacement characters
        # (U+FFFD) for incomplete percent-encoding, and avoids triple-encoding
        # already double-encoded %25XX sequences.
        encoded_obj = _encode_minio_path_for_url(obj)
        parsed = urlparse(base)
        base_path = (parsed.path or "").rstrip("/")

        # 如果 base 已包含 bucket（例如 https://domain/files），避免重复拼接 /files/files
        if base_path == f"/{bucket}" or base_path.endswith(f"/{bucket}"):
            final_path = f"{base_path}/{encoded_obj}" if base_path else f"/{encoded_obj}"
        else:
            final_path = f"{base_path}/{bucket}/{encoded_obj}" if base_path else f"/{bucket}/{encoded_obj}"

        return urlunparse((parsed.scheme, parsed.netloc, final_path, "", "", ""))
    except Exception:
        return None


def _encode_minio_path_for_url(path: str) -> str:
    """Encode a MinIO object key for HTTP URL access.

    MinIO HTTP server decodes %XX once before key lookup, so literal % in
    the key must be double-encoded (% -> %25). However, we must NOT use
    unquote()+quote() because:
    1. Incomplete %XX sequences (e.g. %E5%8 truncated) produce U+FFFD
       replacement chars via unquote(), which quote() then encodes as
       %EF%BF%BD — corrupting the URL permanently.
    2. Already double-encoded %25XX would be decoded to %XX by unquote(),
       then re-encoded as %25XX by quote() — correct but fragile.

    Instead, we split the path into segments between %XX sequences,
    quote() only the plain-text segments (for spaces, CJK chars, etc.),
    and convert each % in %XX sequences to %25 for double-encoding.

    Examples:
      "基于串联读取分阅方法研究" → "%E5%9F%BA%E4%BA%8E..." (quote handles CJK)
      "Electronics%2Bfor" → "Electronics%252Bfor" (%→%25 for double-encoding)
      "path%20with%20spaces" → "path%2520with%2520spaces" (%→%25)
      "already%2520double" → "already%252520double" (each %→%25, including %25)
    """
    # If the path contains no % characters, just quote it normally.
    if '%' not in path:
        return quote(path, safe='/')

    # Split path into alternating plain-text and %XX segments.
    # e.g. "abc%20def%2Bghi" → ["abc", "%20", "def", "%2B", "ghi"]
    parts = re.split(r'(%[0-9A-Fa-f]{2})', path)

    result_parts = []
    for part in parts:
        if part.startswith('%') and len(part) == 3:
            # This is a %XX sequence — double-encode the % sign
            result_parts.append('%25' + part[1:])
        elif part:
            # Plain text segment — quote it (handles CJK, spaces, etc.)
            result_parts.append(quote(part, safe='/'))

    return ''.join(result_parts)


def normalize_minio_url(url: str) -> str:
    """将 MinIO 公开 URL 中单层编码的路径统一修正为双层编码。

    MinIO object key 里如果含有 '%'（例如目录名已是 ``%E5%9F%BA``），
    HTTP URL 里的 '%' 必须再次编码为 '%25'，否则 MinIO HTTP 服务端解码一次后
    得到的 key 与实际 key 不匹配，导致 404。

    规则（幂等）：
    - 路径含 ``%25XX``（已双编码）→ 不做处理直接返回。
    - 路径含 ``%XX``（且不含 ``%25XX``）→ 调用 _encode_minio_path_for_url 重新编码。
    - 非 HTTP(S) 的字符串 / 空字符串 → 不做处理直接返回。

    Examples::

        # 单编码 → 双编码
        normalize_minio_url("https://host/files/uuid/%E5%9F%BA/img.jpg")
        # → "https://host/files/uuid/%25E5%259F%25BA/img.jpg"

        # 已双编码 → 原样返回
        normalize_minio_url("https://host/files/uuid/%25E5%259F%25BA/img.jpg")
        # → "https://host/files/uuid/%25E5%259F%25BA/img.jpg"
    """
    if not url:
        return url
    if not url.startswith(("http://", "https://")):
        return url

    parsed = urlparse(url)
    path = parsed.path

    # 已经有双编码标志 (%25XX)，不做修改
    if re.search(r"%25[0-9A-Fa-f]{2}", path):
        return url

    # 有普通 %XX 编码但没有双编码 → 需要修正
    if not re.search(r"%[0-9A-Fa-f]{2}", path):
        return url  # 全是 ASCII，无需处理

    encoded_path = "/" + _encode_minio_path_for_url(path.lstrip("/"))
    return urlunparse(
        (parsed.scheme, parsed.netloc, encoded_path, parsed.params, parsed.query, parsed.fragment)
    )


def get_minio_image_proxy_url(url: str) -> str | None:
    """Build a same-origin image URL for a public MinIO ``/files/`` path.

    The proxy is needed because legacy image objects may have literal ``%XX``
    sequences in their keys and may be stored as ``application/octet-stream``.
    Browsers cannot reliably resolve that combination directly from MinIO.
    """
    raw = (url or "").strip()
    if not raw:
        return None

    parsed = urlparse(raw)
    if parsed.scheme and parsed.scheme not in {"http", "https"}:
        return None

    path = parsed.path if parsed.scheme else raw.split("?", 1)[0].split("#", 1)[0]
    bucket = (settings.minio_bucket_name or "").strip("/")
    prefixes = [prefix for prefix in (f"/{bucket}/" if bucket else "", "/files/") if prefix]
    object_ref = ""
    for prefix in dict.fromkeys(prefixes):
        if path.startswith(prefix):
            object_ref = path[len(prefix):]
            break
    if not object_ref:
        return None

    expires_at = int(time.time()) + 7 * 24 * 60 * 60
    signature = _build_kb_image_proxy_signature(object_ref, expires_at)
    return (
        f"/api/v2/files/kb-image?object_name={quote(object_ref, safe='')}"
        f"&exp={expires_at}&sig={signature}"
    )


def _build_kb_image_proxy_signature(object_name: str, expires_at: int) -> str:
    secret = (settings.minio_secret_key or "").encode("utf-8")
    payload = f"{str(object_name).lstrip('/')}\n{int(expires_at)}".encode("utf-8")
    return hmac.new(secret, payload, hashlib.sha256).hexdigest()


def verify_kb_image_proxy_signature(
    object_name: str,
    expires_at: int | None,
    signature: str | None,
) -> bool:
    if not expires_at or not signature or expires_at < int(time.time()):
        return False
    expected = _build_kb_image_proxy_signature(object_name, expires_at)
    return hmac.compare_digest(expected, signature)


class ChunkedUploadManager:
    """分块上传管理器"""

    def __init__(self):
        self.temp_dir = Path(settings.upload_temp_dir)
        self.chunk_size = settings.chunk_size
        self.expire_hours = settings.upload_expire_hours
        self._ensure_directories()

    def _ensure_directories(self):
        """确保临时目录存在"""
        self.temp_dir.mkdir(parents=True, exist_ok=True)
        logger.info(f"分块上传临时目录已准备: {self.temp_dir}")

    def _get_upload_session_dir(self, upload_id: str) -> Path:
        """获取上传会话目录"""
        try:
            normalized = str(uuid.UUID(str(upload_id)))
        except (ValueError, AttributeError, TypeError) as exc:
            raise ValueError("无效的上传会话 ID") from exc
        if normalized != str(upload_id).lower():
            raise ValueError("无效的上传会话 ID")
        return self.temp_dir / normalized

    @contextmanager
    def _session_lock(self, upload_id: str):
        session_dir = self._get_upload_session_dir(upload_id)
        if not session_dir.exists():
            yield False
            return
        lock_path = session_dir / ".lock"
        with open(lock_path, "a+b") as lock_file:
            fcntl.flock(lock_file.fileno(), fcntl.LOCK_EX)
            try:
                yield True
            finally:
                fcntl.flock(lock_file.fileno(), fcntl.LOCK_UN)

    def _read_session_unlocked(self, upload_id: str) -> dict[str, Any] | None:
        info_file = self._get_upload_session_dir(upload_id) / "session.json"
        if not info_file.exists():
            return None
        with open(info_file, encoding="utf-8") as f:
            return self._normalize_session(json.load(f))

    def _write_session_unlocked(self, upload_id: str, session: dict[str, Any]) -> None:
        session_dir = self._get_upload_session_dir(upload_id)
        info_file = session_dir / "session.json"
        temp_file = session_dir / f"session.{os.getpid()}.tmp"
        with open(temp_file, "w", encoding="utf-8") as f:
            json.dump(self._serialize_session(session), f, ensure_ascii=False, indent=2)
            f.flush()
            os.fsync(f.fileno())
        os.replace(temp_file, info_file)

    @staticmethod
    def _owned_by(session: dict[str, Any] | None, user_id: str) -> bool:
        return bool(session) and str(session.get("user_id") or "") == str(user_id or "")

    def _serialize_session(self, session: dict[str, Any]) -> dict[str, Any]:
        """将会话对象转换为可 JSON 序列化的结构。"""
        serialized = dict(session)
        uploaded_chunks = serialized.get("uploaded_chunks", set())
        if isinstance(uploaded_chunks, set):
            serialized["uploaded_chunks"] = sorted(uploaded_chunks)
        elif isinstance(uploaded_chunks, list):
            serialized["uploaded_chunks"] = sorted(set(uploaded_chunks))
        else:
            serialized["uploaded_chunks"] = []
        return serialized

    def _normalize_session(self, session: dict[str, Any]) -> dict[str, Any]:
        """将持久化会话转换为运行时结构。"""
        normalized = dict(session)
        uploaded_chunks = normalized.get("uploaded_chunks", [])
        if isinstance(uploaded_chunks, list):
            normalized["uploaded_chunks"] = set(uploaded_chunks)
        elif isinstance(uploaded_chunks, set):
            normalized["uploaded_chunks"] = uploaded_chunks
        else:
            normalized["uploaded_chunks"] = set()
        return normalized

    def init_upload(
        self,
        filename: str,
        total_size: int,
        content_type: str = None,
        *,
        user_id: str,
        metadata: dict[str, Any] | None = None,
    ) -> dict[str, Any]:
        """初始化分块上传

        Args:
            filename: 文件名
            total_size: 文件总大小
            content_type: 内容类型

        Returns:
            上传会话信息
        """
        if total_size <= 0:
            raise ValueError("文件大小必须大于 0")
        if not str(user_id or "").strip():
            raise ValueError("上传会话必须绑定用户")
        upload_id = str(uuid.uuid4())
        session_dir = self._get_upload_session_dir(upload_id)
        session_dir.mkdir(parents=True, exist_ok=True)

        total_chunks = (total_size + self.chunk_size - 1) // self.chunk_size

        session_info = {
            "upload_id": upload_id,
            "filename": filename,
            "total_size": total_size,
            "total_chunks": total_chunks,
            "content_type": content_type,
            "user_id": str(user_id),
            "uploaded_chunks": set(),
            "created_at": datetime.now().isoformat(),
            "chunk_size": self.chunk_size,
            "metadata": dict(metadata or {}),
            "state": "open",
        }

        self._write_session_unlocked(upload_id, session_info)

        logger.info(
            f"分块上传已初始化: upload_id={upload_id}, filename={filename}, chunks={total_chunks}"
        )

        return session_info

    def get_upload_session(
        self,
        upload_id: str,
        *,
        user_id: str,
    ) -> dict[str, Any] | None:
        """获取上传会话信息"""
        with self._session_lock(upload_id) as exists:
            if not exists:
                return None
            session = self._read_session_unlocked(upload_id)
            return session if self._owned_by(session, user_id) else None

    def save_chunk(
        self,
        upload_id: str,
        chunk_index: int,
        content: bytes,
        *,
        user_id: str,
    ) -> dict[str, Any]:
        """保存分块

        Args:
            upload_id: 上传ID
            chunk_index: 分块索引
            content: 分块内容

        Returns:
            保存结果
        """
        try:
            with self._session_lock(upload_id) as exists:
                if not exists:
                    return {"success": False, "error": "上传会话不存在"}
                session = self._read_session_unlocked(upload_id)
                if not self._owned_by(session, user_id):
                    return {"success": False, "error": "上传会话不存在"}
                if session.get("state", "open") != "open":
                    return {"success": False, "error": "上传会话正在完成"}
                if chunk_index < 0 or chunk_index >= session["total_chunks"]:
                    return {"success": False, "error": "分块索引超出范围"}
                remaining = session["total_size"] - chunk_index * session["chunk_size"]
                max_chunk_size = min(session["chunk_size"], remaining)
                if not content or len(content) > max_chunk_size:
                    return {"success": False, "error": "分块大小无效"}
                if chunk_index in session["uploaded_chunks"]:
                    progress = (
                        len(session["uploaded_chunks"]) / session["total_chunks"] * 100
                    )
                    return {
                        "success": True,
                        "message": "分块已存在",
                        "duplicate": True,
                        "uploaded": len(session["uploaded_chunks"]),
                        "total": session["total_chunks"],
                        "progress": progress,
                    }

                session_dir = self._get_upload_session_dir(upload_id)
                chunk_file = session_dir / f"chunk_{chunk_index:06d}"
                with open(chunk_file, "wb") as f:
                    f.write(content)

                session["uploaded_chunks"].add(chunk_index)
                self._write_session_unlocked(upload_id, session)

                progress = len(session["uploaded_chunks"]) / session["total_chunks"] * 100

            logger.debug(
                f"分块已保存: upload_id={upload_id}, chunk={chunk_index}, progress={progress:.1f}%"
            )

            return {
                "success": True,
                "chunk_index": chunk_index,
                "uploaded": len(session["uploaded_chunks"]),
                "total": session["total_chunks"],
                "progress": progress,
            }

        except Exception as e:
            logger.error(
                f"保存分块失败: upload_id={upload_id}, chunk={chunk_index}, {e}"
            )
            return {"success": False, "error": str(e)}

    def complete_upload(
        self,
        upload_id: str,
        storage_type: str = "documents",
        *,
        user_id: str,
        cleanup_on_success: bool = True,
    ) -> tuple[bool, str, str | None]:
        """完成上传，合并所有分块

        Args:
            upload_id: 上传ID
            storage_type: 存储类型

        Returns:
            (是否成功, 消息, 文件路径)
        """
        try:
            with self._session_lock(upload_id) as exists:
                if not exists:
                    return False, "上传会话不存在", None
                session = self._read_session_unlocked(upload_id)
                if not self._owned_by(session, user_id):
                    return False, "上传会话不存在", None
                if session.get("state") == "completed":
                    return False, "上传会话正在完成", None
                if session.get("state", "open") != "open":
                    return False, "上传会话正在完成", None
                if len(session["uploaded_chunks"]) < session["total_chunks"]:
                    missing = set(range(session["total_chunks"])) - session["uploaded_chunks"]
                    return False, f"还有 {len(missing)} 个分块未上传", None

                session_dir = self._get_upload_session_dir(upload_id)
                filename = session["filename"]
                file_id, final_path = storage_manager.generate_storage_path(
                    filename,
                    storage_type,
                )
                with open(final_path, "wb") as outfile:
                    for i in range(session["total_chunks"]):
                        chunk_file = session_dir / f"chunk_{i:06d}"
                        with open(chunk_file, "rb") as infile:
                            outfile.write(infile.read())

                total_size = os.path.getsize(final_path)

                if total_size != session["total_size"]:
                    os.remove(final_path)
                    return (
                        False,
                        f"文件大小不匹配: 期望 {session['total_size']}, 实际 {total_size}",
                        None,
                    )

                if cleanup_on_success:
                    # The route captures session metadata before this call. Cleanup is
                    # intentionally the final operation while the lock is held.
                    shutil.rmtree(session_dir)
                else:
                    session["state"] = "completed"
                    session["merged_path"] = final_path
                    self._write_session_unlocked(upload_id, session)

            logger.info(
                f"分块上传完成: upload_id={upload_id}, file_id={file_id}, path={final_path}"
            )

            return True, "文件上传成功", final_path

        except Exception as e:
            logger.error(f"合并分块失败: upload_id={upload_id}, {e}")
            return False, f"合并分块失败: {str(e)}", None

    def reset_completed_upload(self, upload_id: str, *, user_id: str) -> bool:
        """下游持久化失败后恢复分块会话，使客户端可以安全重试。"""
        try:
            with self._session_lock(upload_id) as exists:
                if not exists:
                    return False
                session = self._read_session_unlocked(upload_id)
                if not self._owned_by(session, user_id):
                    return False
                merged_path = str(session.pop("merged_path", "") or "")
                session["state"] = "open"
                self._write_session_unlocked(upload_id, session)
                if merged_path:
                    try:
                        os.unlink(merged_path)
                    except FileNotFoundError:
                        pass
            return True
        except Exception as exc:
            logger.error("恢复分块上传会话失败: upload_id=%s, %s", upload_id, exc)
            return False

    def cancel_upload(self, upload_id: str, *, user_id: str) -> bool:
        """取消上传"""
        try:
            with self._session_lock(upload_id) as exists:
                if not exists:
                    return False
                session = self._read_session_unlocked(upload_id)
                if not self._owned_by(session, user_id):
                    return False
                shutil.rmtree(self._get_upload_session_dir(upload_id))
            logger.info(f"上传已取消: upload_id={upload_id}")
            return True
        except Exception as e:
            logger.error(f"取消上传失败: upload_id={upload_id}, {e}")
            return False

    def cleanup_upload_session(self, upload_id: str):
        """清理上传会话"""
        session_dir = self._get_upload_session_dir(upload_id)
        if session_dir.exists():
            shutil.rmtree(session_dir)

    def cleanup_expired_sessions(self):
        """清理过期的上传会话"""
        if not self.temp_dir.exists():
            return

        cutoff_time = datetime.now() - timedelta(hours=self.expire_hours)
        cleaned_count = 0

        for session_dir in self.temp_dir.iterdir():
            if session_dir.is_dir():
                try:
                    info_file = session_dir / "session.json"
                    if info_file.exists():
                        with open(info_file, encoding="utf-8") as f:
                            session = json.load(f)

                        created_at = datetime.fromisoformat(
                            session.get("created_at", "")
                        )

                        if created_at < cutoff_time:
                            shutil.rmtree(session_dir)
                            cleaned_count += 1

                except Exception as e:
                    logger.warning(f"清理过期会话时出错: {session_dir}, {e}")

        if cleaned_count > 0:
            logger.info(f"已清理 {cleaned_count} 个过期上传会话")

        return cleaned_count

    def get_upload_progress(
        self,
        upload_id: str,
        *,
        user_id: str,
    ) -> dict[str, Any] | None:
        """获取上传进度"""
        session = self.get_upload_session(upload_id, user_id=user_id)

        if not session:
            return None

        return {
            "upload_id": upload_id,
            "filename": session["filename"],
            "total_size": session["total_size"],
            "total_chunks": session["total_chunks"],
            "uploaded_chunks": len(session["uploaded_chunks"]),
            "progress": len(session["uploaded_chunks"]) / session["total_chunks"] * 100,
            "created_at": session["created_at"],
        }


class StorageManager:
    """文件存储管理器"""

    def __init__(self):
        self.base_path = Path(settings.storage_base_path)
        self.documents_path = Path(settings.documents_path)
        self.results_path = Path(settings.results_path)
        self.cache_path = Path(settings.cache_path)

        self._ensure_directories()

    def _ensure_directories(self):
        """确保存储目录存在"""
        directories = [
            self.base_path,
            self.documents_path,
            self.results_path,
            self.cache_path,
        ]

        for directory in directories:
            directory.mkdir(parents=True, exist_ok=True)
            logger.info(f"存储目录已准备: {directory}")

    def generate_storage_path(
        self, filename: str, storage_type: str = "documents"
    ) -> tuple[str, str]:
        """生成存储路径

        Args:
            filename: 原始文件名
            storage_type: 存储类型 (documents/results/cache)

        Returns:
            (文件ID, 完整路径)
        """
        file_id = str(uuid.uuid4())
        file_ext = Path(filename).suffix.lower()
        storage_dir = self._get_storage_dir(storage_type)
        stored_filename = f"{file_id}{file_ext}"

        storage_path = storage_dir / stored_filename

        return file_id, str(storage_path)

    def _get_storage_dir(self, storage_type: str) -> Path:
        """获取存储目录"""
        type_mapping = {
            "documents": self.documents_path,
            "results": self.results_path,
            "cache": self.cache_path,
        }
        return type_mapping.get(storage_type, self.documents_path)

    async def save_file(
        self, content: bytes, filename: str, storage_type: str = "documents"
    ) -> tuple[str, str]:
        """保存文件

        Args:
            content: 文件内容
            filename: 原始文件名
            storage_type: 存储类型

        Returns:
            (文件ID, 存储路径)
        """
        file_id, storage_path = self.generate_storage_path(filename, storage_type)

        async with self._atomic_write(storage_path) as temp_path:
            await asyncio.to_thread(Path(temp_path).write_bytes, content)

        file_hash = await asyncio.to_thread(self.calculate_file_hash, storage_path)
        logger.info(
            f"文件已保存: {storage_path} (ID: {file_id}, Hash: {file_hash[:16]}...)"
        )

        return file_id, storage_path

    @asynccontextmanager
    async def _atomic_write(self, target_path: str):
        """原子写入上下文管理器

        先写入临时文件，成功后移动到目标位置
        """
        temp_path = f"{target_path}.tmp.{os.getpid()}"
        try:
            yield temp_path
            await asyncio.to_thread(shutil.move, temp_path, target_path)
        except Exception as e:
            if os.path.exists(temp_path):
                await asyncio.to_thread(os.remove, temp_path)
            raise e

    def calculate_file_hash(self, file_path: str) -> str:
        """计算文件哈希值

        Args:
            file_path: 文件路径

        Returns:
            文件哈希值 (SHA256)
        """
        sha256_hash = hashlib.sha256()

        with open(file_path, "rb") as f:
            for chunk in iter(lambda: f.read(8192), b""):
                sha256_hash.update(chunk)

        return sha256_hash.hexdigest()

    async def get_file(self, file_path: str) -> bytes | None:
        """读取文件内容

        Args:
            file_path: 文件路径

        Returns:
            文件内容
        """
        if not os.path.exists(file_path):
            logger.warning(f"文件不存在: {file_path}")
            return None

        return await asyncio.to_thread(Path(file_path).read_bytes)

    async def delete_file(self, file_path: str) -> bool:
        """删除文件

        Args:
            file_path: 文件路径

        Returns:
            是否删除成功
        """
        if not os.path.exists(file_path):
            logger.warning(f"文件不存在，无法删除: {file_path}")
            return False

        try:
            os.remove(file_path)
            logger.info(f"文件已删除: {file_path}")
            return True
        except Exception as e:
            logger.error(f"删除文件失败: {file_path}, {e}")
            return False

    def file_exists(self, file_path: str) -> bool:
        """检查文件是否存在"""
        return os.path.exists(file_path)

    def get_file_size(self, file_path: str) -> int:
        """获取文件大小"""
        if not os.path.exists(file_path):
            return 0
        return os.path.getsize(file_path)

    def get_file_info(self, file_path: str) -> dict:
        """获取文件信息

        Args:
            file_path: 文件路径

        Returns:
            文件信息字典
        """
        if not os.path.exists(file_path):
            return {}

        stat = os.stat(file_path)

        return {
            "path": file_path,
            "size": stat.st_size,
            "created_at": datetime.fromtimestamp(stat.st_ctime).isoformat(),
            "modified_at": datetime.fromtimestamp(stat.st_mtime).isoformat(),
            "hash": (
                self.calculate_file_hash(file_path)
                if os.path.isfile(file_path)
                else None
            ),
        }

    async def cleanup_expired_files(
        self, storage_type: str = "documents", max_age_days: int | None = None
    ):
        """清理过期文件

        Args:
            storage_type: 存储类型
            max_age_days: 最大保留天数
        """
        if max_age_days is None:
            max_age_days = settings.file_expire_days

        storage_dir = self._get_storage_dir(storage_type)

        if not storage_dir.exists():
            return

        cutoff_time = time.time() - (max_age_days * 24 * 60 * 60)
        def _cleanup_sync() -> tuple[int, int]:
            deleted_count = 0
            freed_space = 0
            for file_path in storage_dir.rglob("*"):
                if not file_path.is_file():
                    continue
                try:
                    file_stat = file_path.stat()
                    if file_stat.st_mtime < cutoff_time:
                        file_path.unlink()
                        deleted_count += 1
                        freed_space += file_stat.st_size
                except FileNotFoundError:
                    continue
                except Exception as exc:
                    logger.warning("清理文件时出错: %s, %s", file_path, exc)
            return deleted_count, freed_space

        deleted_count, freed_space = await asyncio.to_thread(_cleanup_sync)

        logger.info(
            f"清理完成: {storage_type}, "
            f"删除 {deleted_count} 个文件, "
            f"释放 {freed_space / 1024 / 1024:.2f} MB"
        )

        return {"deleted_count": deleted_count, "freed_space": freed_space}

    async def cleanup_cache(self, max_age_hours: int | None = None):
        """清理缓存文件

        Args:
            max_age_hours: 最大保留小时数
        """
        if max_age_hours is None:
            max_age_hours = settings.cache_expire_hours

        return await self.cleanup_expired_files("cache", max_age_hours / 24)

    async def cleanup_all(self):
        """清理所有过期文件"""
        results = {}

        for storage_type in ["documents", "results", "cache"]:
            results[storage_type] = await self.cleanup_expired_files(storage_type)

        return results

    def get_storage_stats(self, storage_type: str | None = None) -> dict[str, Any]:
        """获取存储统计信息

        Args:
            storage_type: 存储类型，None 表示所有

        Returns:
            统计信息字典
        """
        stats: dict[str, Any] = {
            "total_size": 0,
            "file_count": 0,
            "directories": {},
        }

        target_types = (
            [storage_type] if storage_type else ["documents", "results", "cache"]
        )

        for type_name in target_types:
            storage_dir = self._get_storage_dir(type_name)

            if storage_dir.exists():
                dir_size = 0
                file_count = 0

                for file_path in storage_dir.rglob("*"):
                    try:
                        if file_path.is_file():
                            file_size = file_path.stat().st_size
                            dir_size += file_size
                            file_count += 1
                    except Exception:
                        pass

                stats["directories"][type_name] = {
                    "path": str(storage_dir),
                    "size": dir_size,
                    "size_human": self._format_size(dir_size),
                    "file_count": file_count,
                }

                stats["total_size"] += dir_size
                stats["file_count"] += file_count

        stats["total_size_human"] = self._format_size(stats["total_size"])

        return stats

    def _format_size(self, size_bytes: int) -> str:
        """格式化文件大小"""
        size = float(size_bytes)
        for unit in ["B", "KB", "MB", "GB", "TB"]:
            if size < 1024:
                return f"{size:.2f} {unit}"
            size /= 1024

        return f"{size:.2f} PB"

    def check_disk_usage(self) -> dict[str, Any]:
        """检查磁盘使用情况

        Returns:
            磁盘使用信息
        """
        base_path = str(self.base_path)

        if not os.path.exists(base_path):
            return {"error": "存储目录不存在"}

        total, used, free = shutil.disk_usage(base_path)

        usage_ratio = used / total if total > 0 else 0
        warning_threshold = settings.disk_usage_warning_threshold

        return {
            "total": self._format_size(total),
            "used": self._format_size(used),
            "free": self._format_size(free),
            "usage_ratio": f"{usage_ratio * 100:.1f}%",
            "warning": usage_ratio >= warning_threshold,
            "threshold": f"{warning_threshold * 100:.0f}%",
        }

    async def cleanup_by_policy(self):
        """按策略自动清理"""
        stats = self.check_disk_usage()

        if stats.get("warning", False):
            logger.warning(f"磁盘空间不足: {stats['usage_ratio']}")

            aggressive_cleanup = await self.cleanup_all()
            logger.warning(f"已执行紧急清理: {aggressive_cleanup}")

        await self.cleanup_cache()

    def get_files_by_type(
        self, storage_type: str, extensions: list
    ) -> Generator[Path, None, None]:
        """按扩展名获取文件

        Args:
            storage_type: 存储类型
            extensions: 扩展名列表

        Yields:
            匹配的 文件路径
        """
        storage_dir = self._get_storage_dir(storage_type)

        if not storage_dir.exists():
            return

        extensions = [
            ext.lower() if ext.startswith(".") else f".{ext.lower()}"
            for ext in extensions
        ]

        for file_path in storage_dir.rglob("*"):
            if file_path.is_file() and file_path.suffix.lower() in extensions:
                yield file_path

    async def backup_files(self, backup_dir: str, storage_type: str = "documents"):
        """备份文件

        Args:
            backup_dir: 备份目录
            storage_type: 存储类型
        """
        storage_dir = self._get_storage_dir(storage_type)

        if not storage_dir.exists():
            logger.warning(f"存储目录不存在: {storage_dir}")
            return

        def _backup_sync() -> tuple[Path, int]:
            backup_path = (
                Path(backup_dir)
                / f"{storage_type}_{datetime.now().strftime('%Y%m%d_%H%M%S')}"
            )
            backup_path.mkdir(parents=True, exist_ok=True)
            backup_count = 0
            for file_path in storage_dir.rglob("*"):
                if not file_path.is_file():
                    continue
                relative_path = file_path.relative_to(storage_dir)
                dest_path = backup_path / relative_path
                dest_path.parent.mkdir(parents=True, exist_ok=True)
                shutil.copy2(file_path, dest_path)
                backup_count += 1
            return backup_path, backup_count

        backup_path, backup_count = await asyncio.to_thread(_backup_sync)
        logger.info(f"备份完成: {backup_path}, 共 {backup_count} 个文件")

        return str(backup_path)


storage_manager = StorageManager()
chunked_upload_manager = ChunkedUploadManager()


async def init_storage():
    """初始化存储系统"""
    logger.info("初始化存储系统...")

    await asyncio.to_thread(_get_minio_client)

    stats = await asyncio.to_thread(storage_manager.get_storage_stats)
    logger.info(f"存储统计: {stats}")

    disk_info = await asyncio.to_thread(storage_manager.check_disk_usage)
    logger.info(f"磁盘使用: {disk_info}")

    await storage_manager.cleanup_by_policy()

    await asyncio.to_thread(chunked_upload_manager.cleanup_expired_sessions)

    logger.info("存储系统初始化完成")


async def periodic_cleanup():
    """定期清理任务"""
    while True:
        try:
            await asyncio.sleep(3600)
            logger.info("执行定期清理...")
            await storage_manager.cleanup_by_policy()
            await asyncio.to_thread(chunked_upload_manager.cleanup_expired_sessions)
        except asyncio.CancelledError:
            logger.info("定期清理任务已停止")
            break
        except Exception as e:
            logger.error(f"定期清理出错: {e}")
