"""公网远程资源抓取的 SSRF 防护。

三个图片抓取点（RAG 查询期 VLM 增强、自研入库内嵌图片下载、legacy
modalprocessors 下载）此前对任意 http(s) URL 直接 GET 且跟随重定向，
构成内网 GET 探测面。本模块把 chat_files 已验证的防护（scheme 白名单、
getaddrinfo 全部 IP 必须 ``is_global``、连接到校验过的 IP 消除 DNS
rebinding TOCTOU、不自动跟随重定向）抽成公共实现，供各处复用。

服务端自签的 MinIO 预签名 URL 是可信内部目标——但豁免必须**绑定主机**
（与 ``settings.minio_endpoint`` 一致）：只看 query 参数形态的豁免会被
「任意 URL 加一个 ``?X-Amz-Signature=1``」整体绕过。
"""

from __future__ import annotations

import asyncio
import ipaddress
import socket
import time
from urllib.parse import urljoin, urlparse

import httpx

from backend.config import settings
from backend.config.loggers import get_logger

logger = get_logger(__name__)

_PRESIGNED_QUERY_KEYS = {"x-amz-signature", "x-amz-credential", "x-amz-algorithm"}
_MAX_REDIRECT_HOPS = 1
_CHUNK_SIZE = 256 * 1024
_RESOLVE_TIMEOUT_SECONDS = 10.0


def is_minio_authority_url(raw_url: str) -> bool:
    """URL 的主机/端口是否与配置的 MinIO 端点一致（可信内部目标判定）。"""
    try:
        parsed = urlparse(str(raw_url or ""))
    except ValueError:
        return False
    if not parsed.hostname:
        return False
    authority = _minio_authority()
    if authority is None:
        return False
    try:
        port = parsed.port or (443 if parsed.scheme == "https" else 80)
    except ValueError:
        return False
    return (parsed.hostname.lower(), int(port)) == authority


def _minio_authority() -> tuple[str, int] | None:
    """返回配置的 MinIO (host, port)，解析失败返回 None。"""
    endpoint = (settings.minio_endpoint or "").strip()
    if not endpoint:
        return None
    candidate = endpoint if "://" in endpoint else f"http://{endpoint}"
    try:
        parsed = urlparse(candidate)
        if not parsed.hostname:
            return None
        port = parsed.port or (443 if parsed.scheme == "https" else 80)
        return parsed.hostname.lower(), int(port)
    except ValueError:
        return None


def looks_server_presigned(raw_url: str) -> bool:
    """识别「服务端自签的 MinIO 预签名 URL」——可信内部目标。

    必须同时满足：query 含 SigV4 签名键，且主机/端口与配置的
    ``minio_endpoint`` 完全一致。缺少主机绑定的形态豁免会被攻击者
    用一个伪造 query 参数绕过整个 SSRF 防护。
    """
    try:
        parsed = urlparse(str(raw_url or ""))
    except ValueError:
        return False
    if not parsed.query or not parsed.hostname:
        return False
    keys = {
        part.split("=", 1)[0].lower()
        for part in parsed.query.split("&")
        if part
    }
    if not keys & _PRESIGNED_QUERY_KEYS:
        return False
    authority = _minio_authority()
    if authority is None:
        return False
    try:
        port = parsed.port or (443 if parsed.scheme == "https" else 80)
    except ValueError:
        return False
    return (parsed.hostname.lower(), int(port)) == authority


async def resolve_public_remote_target(
    raw_url: str,
) -> tuple[str, str, int] | None:
    """Validate a remote URL and return a pinned (ip, host, port).

    Returns ``None`` for any non-public / malformed target. The caller must
    connect to the returned IP (not re-resolve the hostname) so a low-TTL
    DNS-rebinding attacker cannot pass this check with a public IP and then
    have httpx connect to an internal address (TOCTOU).
    """
    try:
        parsed = urlparse(raw_url)
        if parsed.scheme not in {"http", "https"} or not parsed.hostname:
            return None
        if parsed.username or parsed.password:
            return None
        port = parsed.port or (443 if parsed.scheme == "https" else 80)
        # 系统解析器在多 nameserver 顺序失败时可挂数十秒；给解析本身设界。
        infos = await asyncio.wait_for(
            asyncio.get_running_loop().getaddrinfo(
                parsed.hostname,
                port,
                type=socket.SOCK_STREAM,
            ),
            timeout=_RESOLVE_TIMEOUT_SECONDS,
        )
    except (OSError, ValueError, asyncio.TimeoutError):
        return None
    pinned_ip: str | None = None
    for info in infos:
        try:
            address = ipaddress.ip_address(info[4][0])
        except ValueError:
            return None
        if not address.is_global:
            return None
        if pinned_ip is None:
            pinned_ip = str(address)
    if not pinned_ip:
        return None
    return pinned_ip, parsed.hostname, int(port)


def _pinned_request_parts(
    raw_url: str, pinned: tuple[str, str, int]
) -> tuple[str, dict[str, str], dict[str, str]]:
    pinned_ip, pinned_host, pinned_port = pinned
    parsed = urlparse(raw_url)
    # 连接到校验通过的 IP（不让 httpx 二次解析主机名），消除 DNS-rebinding
    # TOCTOU；Host 头 + TLS SNI 仍用原主机名保证虚拟主机路由与证书校验。
    host_netloc = f"[{pinned_ip}]" if ":" in pinned_ip else pinned_ip
    pinned_url = f"{parsed.scheme}://{host_netloc}:{pinned_port}{parsed.path}"
    if parsed.query:
        pinned_url = f"{pinned_url}?{parsed.query}"
    default_port = 443 if parsed.scheme == "https" else 80
    host_header = (
        pinned_host if pinned_port == default_port else f"{pinned_host}:{pinned_port}"
    )
    extensions = {"sni_hostname": pinned_host}
    return pinned_url, {"Host": host_header}, extensions


async def fetch_public_image_bytes(
    raw_url: str,
    *,
    max_bytes: int,
    timeout: float = 60.0,
) -> bytes | None:
    """按 SSRF 防护规则抓取公网图片（content-type 必须 image/*）。"""
    return await fetch_public_bytes(
        raw_url, max_bytes=max_bytes, timeout=timeout, content_type_prefix="image/"
    )


async def fetch_public_bytes(
    raw_url: str,
    *,
    max_bytes: int,
    timeout: float = 60.0,
    content_type_prefix: str | None = None,
) -> bytes | None:
    """按 SSRF 防护规则抓取公网资源，返回字节（不合规/失败返回 None）。

    - 连接 pinned IP + 原 Host/SNI（防 DNS rebinding，兼容虚拟主机证书）；
    - 不自动跟随重定向；对 3xx 只用 ``urljoin`` 解析 Location 并再校验
      一次目标（同样必须公网），至多一跳；
    - 流式读取并在累计超限时立即截断（大小检查不后置于完整下载）；
    - 总下载 deadline：httpx 的 timeout 只约束单次 read 间隔，慢滴流
      （每 59s 发 1 字节）可把连接拖到小时级，这里以单调时钟整体封顶；
    - ``content_type_prefix`` 非空时校验响应 content-type 前缀。
    """
    current_url = str(raw_url or "").strip()
    if not current_url:
        return None
    deadline = time.monotonic() + timeout
    async with httpx.AsyncClient(
        trust_env=False, timeout=timeout, follow_redirects=False
    ) as client:
        for _hop in range(_MAX_REDIRECT_HOPS + 1):
            pinned = await resolve_public_remote_target(current_url)
            if pinned is None:
                return None
            pinned_url, headers, extensions = _pinned_request_parts(
                current_url, pinned
            )
            try:
                async with client.stream(
                    "GET", pinned_url, headers=headers, extensions=extensions
                ) as response:
                    if response.status_code in {301, 302, 303, 307, 308}:
                        location = response.headers.get("location")
                        if not location:
                            return None
                        current_url = urljoin(current_url, location)
                        if not current_url.startswith(("http://", "https://")):
                            return None
                        continue
                    if response.status_code >= 400:
                        return None
                    if content_type_prefix is not None:
                        content_type = str(
                            response.headers.get("content-type") or ""
                        )
                        if not content_type.lower().startswith(content_type_prefix):
                            return None
                    try:
                        declared = int(response.headers.get("content-length") or 0)
                    except ValueError:
                        return None
                    if declared > max_bytes:
                        return None
                    chunks: list[bytes] = []
                    total = 0
                    async for chunk in response.aiter_bytes(_CHUNK_SIZE):
                        total += len(chunk)
                        if total > max_bytes:
                            return None
                        if time.monotonic() > deadline:
                            return None
                        chunks.append(chunk)
                    content = b"".join(chunks)
                    if not content:
                        return None
                    return content
            except (httpx.HTTPError, httpx.InvalidURL):
                # InvalidURL 不是 HTTPError 子类：URL 带控制字符时
                # client.stream() 直接抛出，逃逸会破坏"失败返回 None"契约。
                return None
    return None
