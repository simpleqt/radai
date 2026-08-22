"""题库图片存储边界：稳定 object_name + HMAC 签名代理 URL。

与 ``chart_image_store`` 同款纪律：
- MinIO 优先、本地目录降级（同一 URL 服务两种后端）；
- 对外只暴露代理 URL（``/api/v2/files/qb-image``），不暴露 MinIO 凭据/裸对象路径；
- 签名密钥为空时 fail-closed（校验恒失败，与 kb-image/chart 的安全纪律一致）；
- 稳定 object_name 是持久身份，签名（exp/sig）只是短期展示态，绝不入库。
"""

from __future__ import annotations

import asyncio
import hashlib
import hmac
import os
import re
import time
from pathlib import Path
from urllib.parse import quote, urlencode

from backend.config.loggers import get_logger
from backend.config.settings import settings

logger = get_logger(__name__)

LOCAL_QB_DIR = Path(__file__).resolve().parents[2] / "data" / "question-bank"
_QB_URL_TTL_SECONDS = int(
    os.getenv("QUESTION_BANK_IMAGE_URL_TTL_SECONDS", str(24 * 60 * 60))
)

# object_name 只允许 question-bank/ 前缀下的安全路径段，防穿越。
_OBJECT_NAME_RE = re.compile(r"^question-bank/[A-Za-z0-9_\-]{1,64}(?:/[A-Za-z0-9_\-]{1,64}){1,3}\.(?:png|jpg|jpeg|gif|webp)$", re.IGNORECASE)

_EXT_CONTENT_TYPES = {
    ".png": "image/png",
    ".jpg": "image/jpeg",
    ".jpeg": "image/jpeg",
    ".gif": "image/gif",
    ".webp": "image/webp",
}


def is_valid_object_name(object_name: str) -> bool:
    return bool(_OBJECT_NAME_RE.fullmatch(str(object_name or "").strip()))


def extract_qb_image_object_name(url: str) -> str | None:
    """从 qb-image 代理 URL（相对或绝对）提取稳定 object_name。

    注册表/历史恢复用它把签名 URL 还原成持久身份（稳定 object_name 才入
    ``registered_images``，exp/sig 只是短期展示态）。非法或过期的 URL 提不出
    合法 object_name 时返回 None。
    """
    from urllib.parse import parse_qs, urlsplit

    raw = str(url or "").strip()
    if not raw:
        return None
    try:
        parts = urlsplit(raw if "://" in raw else f"http://localhost{raw}")
        if not parts.path.rstrip("/").endswith("/qb-image"):
            return None
        names = parse_qs(parts.query).get("name") or []
        if len(names) != 1:
            return None
        object_name = names[0].lstrip("/")
        return object_name if is_valid_object_name(object_name) else None
    except ValueError:
        return None


def content_type_for(object_name: str) -> str:
    suffix = Path(str(object_name)).suffix.lower()
    return _EXT_CONTENT_TYPES.get(suffix, "application/octet-stream")


def _signature_secret() -> bytes:
    from backend.services.url_signing import url_signing_secret

    secret = url_signing_secret()
    return secret.encode("utf-8")


def build_qb_image_signature(object_name: str, expires_at: int) -> str:
    payload = f"qb-image\n{object_name}\n{int(expires_at)}".encode()
    return hmac.new(_signature_secret(), payload, hashlib.sha256).hexdigest()


def verify_qb_image_signature(
    object_name: str,
    expires_at: int | None,
    signature: str | None,
) -> bool:
    if not expires_at or not signature or int(expires_at) < int(time.time()):
        return False
    if not _signature_secret():
        # 密钥未配置时 HMAC 本地可伪造，必须 fail-closed（kb-image 同款纪律）。
        return False
    # compare_digest 对含非 ASCII 字符的 str 会抛 TypeError（可被远程触发的
    # 500）；签名是 hex 摘要，格式不符直接拒绝。
    if not re.fullmatch(r"[0-9a-fA-F]{64}", str(signature)):
        return False
    expected = build_qb_image_signature(object_name, int(expires_at))
    return hmac.compare_digest(expected, signature)


def build_qb_image_url(object_name: str) -> str:
    expires_at = int(time.time()) + max(_QB_URL_TTL_SECONDS, 60)
    query = urlencode(
        {
            "name": object_name,
            "exp": str(expires_at),
            "sig": build_qb_image_signature(object_name, expires_at),
        }
    )
    return f"/api/v2/files/qb-image?{query}"


def build_object_name(
    *, batch_id: str, question_id: str, seq: int, ext: str
) -> str:
    suffix = str(ext or "").strip().lstrip(".").lower() or "png"
    if suffix == "jpeg":
        suffix = "jpg"
    if suffix not in {"png", "jpg", "gif", "webp"}:
        # 白名单外格式 fail-closed：静默改标 .png 会让浏览器按 PNG 解码
        # 失败（裂图），且字节内容与扩展名不符。两个入库入口（解析器与
        # 手工贴图路由）都已按同一白名单预校验，此处兜住未来漂移。
        raise ValueError(f"不支持的题图格式: {suffix or '(空)'}")
    return f"question-bank/{batch_id}/{question_id}/{int(seq)}.{suffix}"


async def store_qb_image(
    data: bytes,
    *,
    batch_id: str,
    question_id: str,
    seq: int,
    ext: str,
) -> str:
    """持久化一张题图并返回稳定 object_name（MinIO 优先，本地降级）。"""
    object_name = build_object_name(
        batch_id=batch_id, question_id=question_id, seq=seq, ext=ext
    )
    if await _save_to_minio(data, object_name):
        return object_name
    await asyncio.to_thread(_save_locally, data, object_name)
    return object_name


async def fetch_qb_image(object_name: str) -> bytes | None:
    object_name = str(object_name or "").strip()
    if not is_valid_object_name(object_name):
        return None
    remote = await _fetch_from_minio(object_name)
    if remote:
        return remote
    local_path = (LOCAL_QB_DIR / object_name).resolve()
    try:
        local_path.relative_to(LOCAL_QB_DIR.resolve())
    except ValueError:
        return None
    return await asyncio.to_thread(
        lambda: local_path.read_bytes() if local_path.exists() else None
    )


async def _save_to_minio(data: bytes, object_name: str) -> bool:
    try:
        from backend.storage import get_user_minio_bucket_name, upload_bytes_to_minio

        result = await asyncio.wait_for(
            upload_bytes_to_minio(
                data,
                object_name,
                content_type=content_type_for(object_name),
                bucket_name=get_user_minio_bucket_name(),
            ),
            timeout=5.0,
        )
        return bool(result)
    except Exception as exc:  # noqa: BLE001
        logger.warning(
            "[QuestionBank] 题图 MinIO 上传失败（降级本地）: %s", type(exc).__name__
        )
        return False


def _save_locally(data: bytes, object_name: str) -> None:
    target = LOCAL_QB_DIR / object_name
    target.parent.mkdir(parents=True, exist_ok=True)
    target.write_bytes(data)


async def _fetch_from_minio(object_name: str) -> bytes | None:
    try:
        from backend.storage import _get_minio_client, get_user_minio_bucket_name

        def _sync_fetch() -> bytes:
            client = _get_minio_client()
            response = client.get_object(
                get_user_minio_bucket_name(), object_name
            )
            try:
                return response.read()
            finally:
                response.close()
                response.release_conn()

        return await asyncio.wait_for(asyncio.to_thread(_sync_fetch), timeout=5.0)
    except Exception as exc:  # noqa: BLE001
        logger.warning(
            "[QuestionBank] 题图 MinIO 下载失败: %s error=%s",
            object_name,
            type(exc).__name__,
        )
        return None


def encode_object_name_for_url(object_name: str) -> str:
    return quote(str(object_name or ""), safe="")
