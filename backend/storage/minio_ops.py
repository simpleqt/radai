"""MinIO 操作域:客户端单例、桶名、上传/下载、预签名与公开 URL、URL 归一化、KB 图片代理签名。(拆自 backend/storage/__init__.py)"""

from __future__ import annotations

from backend.config.loggers import get_logger

from backend.config import settings
from backend.services.minio_paths import normalize_minio_endpoint
from datetime import timedelta
from pathlib import Path
from urllib.parse import parse_qs
from urllib.parse import quote
from urllib.parse import unquote
from urllib.parse import urlparse
from urllib.parse import urlunparse
import asyncio
import hashlib
import hmac
import os
import re
import threading
import time
import uuid

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
        raise TimeoutError(f"对象存储操作超过 {operation_timeout:.1f}s") from exc
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
            if user_bucket != settings.minio_bucket_name and not client.bucket_exists(
                user_bucket
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
    # Keep the atomic-download name independent from the destination name.
    # Appending a PID and UUID to a valid long UTF-8 filename can exceed the
    # filesystem's per-component NAME_MAX limit before any bytes are written.
    temporary = destination.parent / f".download-{uuid.uuid4().hex}.part"
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
        raw_path = raw_path[len("files/") :]
    elif bucket_name and raw_path.startswith(f"{bucket_name}/"):
        raw_path = raw_path[len(bucket_name) + 1 :]

    if (
        parsed.scheme
        and parsed.netloc
        or raw_path.startswith("files/")
        or (bucket_name and raw_path.startswith(f"{bucket_name}/"))
    ) and "%" in raw_path:
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
        base = (settings.minio_public_base_url or settings.minio_endpoint or "").rstrip(
            "/"
        )
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
            final_path = (
                f"{base_path}/{encoded_obj}" if base_path else f"/{encoded_obj}"
            )
        else:
            final_path = (
                f"{base_path}/{bucket}/{encoded_obj}"
                if base_path
                else f"/{bucket}/{encoded_obj}"
            )

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
    if "%" not in path:
        return quote(path, safe="/")

    # Split path into alternating plain-text and %XX segments.
    # e.g. "abc%20def%2Bghi" → ["abc", "%20", "def", "%2B", "ghi"]
    parts = re.split(r"(%[0-9A-Fa-f]{2})", path)

    result_parts = []
    for part in parts:
        if part.startswith("%") and len(part) == 3:
            # This is a %XX sequence — double-encode the % sign
            result_parts.append("%25" + part[1:])
        elif part:
            # Plain text segment — quote it (handles CJK, spaces, etc.)
            result_parts.append(quote(part, safe="/"))

    return "".join(result_parts)


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
        (
            parsed.scheme,
            parsed.netloc,
            encoded_path,
            parsed.params,
            parsed.query,
            parsed.fragment,
        )
    )


def extract_kb_image_object_name(value: str) -> str | None:
    """Return the durable object key carried by a KB image proxy URL.

    ``exp`` and ``sig`` are deliberately ignored here.  They are transport
    credentials, not part of the image identity, and must never be relied on
    when a conversation is replayed in a later turn.
    """

    raw = (value or "").strip()
    if not raw:
        return None
    parsed = urlparse(raw)
    path = parsed.path or ""
    if path not in {"/api/v2/files/kb-image", "/files/kb-image"}:
        return None
    object_names = (
        parse_qs(parsed.query, keep_blank_values=False).get("object_name") or []
    )
    if not object_names:
        return None
    object_name = str(object_names[0] or "").strip().lstrip("/")
    if not object_name or any(
        part in {"", ".", ".."} for part in object_name.split("/")
    ):
        return None
    return object_name


def build_kb_image_proxy_url(
    object_name: str,
    *,
    expiry_seconds: int = 7 * 24 * 60 * 60,
) -> str | None:
    """Create a fresh browser URL for a durable KB image object key."""

    object_ref = str(object_name or "").strip().lstrip("/")
    if not object_ref or any(part in {"", ".", ".."} for part in object_ref.split("/")):
        return None
    expires_at = int(time.time()) + max(1, int(expiry_seconds))
    signature = _build_kb_image_proxy_signature(object_ref, expires_at)
    return (
        f"/api/v2/files/kb-image?object_name={quote(object_ref, safe='')}"
        f"&exp={expires_at}&sig={signature}"
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

    # Refresh legacy proxy URLs instead of preserving their expiring
    # signature.  Conversation history may contain URLs created days ago.
    existing_object_name = extract_kb_image_object_name(raw)
    if existing_object_name:
        return build_kb_image_proxy_url(existing_object_name)

    parsed = urlparse(raw)
    if parsed.scheme and parsed.scheme not in {"http", "https"}:
        return None

    path = parsed.path if parsed.scheme else raw.split("?", 1)[0].split("#", 1)[0]
    bucket = (settings.minio_bucket_name or "").strip("/")
    prefixes = [
        prefix for prefix in (f"/{bucket}/" if bucket else "", "/files/") if prefix
    ]
    object_ref = ""
    for prefix in dict.fromkeys(prefixes):
        if path.startswith(prefix):
            object_ref = path[len(prefix) :]
            break
    if not object_ref:
        return None

    return build_kb_image_proxy_url(object_ref)


def _build_kb_image_proxy_signature(object_name: str, expires_at: int) -> str:
    from backend.services.url_signing import url_signing_secret

    secret = url_signing_secret().encode("utf-8")
    payload = f"{str(object_name).lstrip('/')}\n{int(expires_at)}".encode("utf-8")
    return hmac.new(secret, payload, hashlib.sha256).hexdigest()


def verify_kb_image_proxy_signature(
    object_name: str,
    expires_at: int | None,
    signature: str | None,
) -> bool:
    if not expires_at or not signature or expires_at < int(time.time()):
        return False
    # Fail closed on an empty signing key: with an empty secret the HMAC is
    # locally reproducible and any client could forge a valid signature
    # (matching verify_chart_signature).
    from backend.services.url_signing import url_signing_secret

    # 实际 HMAC 密钥来自 url_signing_secret()（URL_SIGNING_SECRET →
    # internal_api_secret → minio_secret_key）；守卫必须判同一来源，
    # 否则"配了独立签名密钥、minio 密钥为空"时签名成功验证全拒。
    if not (url_signing_secret() or "").strip():
        return False
    expected = _build_kb_image_proxy_signature(object_name, expires_at)
    return hmac.compare_digest(expected, signature)
