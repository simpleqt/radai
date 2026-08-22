"""CAS 单点登录域:service URL 构建/白名单校验、登录/登出/验证地址、ticket 验证与 exchange 签发。(拆自 api/routers/auth.py)"""

from __future__ import annotations

from backend.config.loggers import get_logger

from backend.config.settings import settings
from backend.db.models import User
from fastapi import HTTPException
from fastapi import Request
from redis.asyncio import Redis
from urllib.parse import parse_qsl
from urllib.parse import urlencode
from urllib.parse import urljoin
from urllib.parse import urlparse
from urllib.parse import urlunparse
import httpx
import json
import secrets
import xml.etree.ElementTree as ET
from backend.api.routers.auth_credentials import (
    _serialize_user,
)

logger = get_logger(__name__)


CAS_TARGET_URL_COOKIE = "cas_target_url"


CAS_EXCHANGE_TTL_SECONDS = 60


CAS_EXCHANGE_KEY_PREFIX = "auth:cas_exchange:"


# A CAS validate response is a short document; anything larger is either a
# misconfigured endpoint or an attempt to make the parser do the work.
CAS_VALIDATE_RESPONSE_MAX_CHARS = 64 * 1024


CAS_DISPLAY_NAME_KEYS = (
    "name",
    "displayname",
    "realname",
    "cn",
    "truename",
    "nickname",
    "xm",
)


def _merge_query(url: str, params: dict[str, str]) -> str:
    parsed = urlparse(url)
    query = dict(parse_qsl(parsed.query, keep_blank_values=True))
    for key, value in params.items():
        if value is None:
            continue
        query[key] = value
    new_query = urlencode(query)
    return urlunparse(
        (
            parsed.scheme,
            parsed.netloc,
            parsed.path,
            parsed.params,
            new_query,
            parsed.fragment,
        )
    )


def _validate_cas_service_url(configured: str) -> str:
    parsed = urlparse(configured)
    if (
        parsed.scheme.lower() not in {"http", "https"}
        or not parsed.hostname
        or parsed.username
        or parsed.password
        or parsed.fragment
    ):
        raise HTTPException(status_code=500, detail="CAS 回调地址配置无效")
    return configured


def _configured_cas_service_urls() -> list[str]:
    """Parse CAS_SERVICE_URL as one URL or a comma-separated allowlist."""
    raw = (settings.cas_service_url or "").strip()
    if not raw:
        return []
    urls: list[str] = []
    for part in raw.split(","):
        candidate = part.strip()
        if not candidate:
            continue
        urls.append(_validate_cas_service_url(candidate))
    return urls


def _request_host_candidates(request: Request) -> list[str]:
    """Collect Host / X-Forwarded-Host values for allowlist matching only."""
    candidates: list[str] = []
    for header_name in ("host", "x-forwarded-host"):
        raw = (request.headers.get(header_name) or "").strip().lower()
        if not raw:
            continue
        # X-Forwarded-Host may be a chain; only consider the first hop.
        host = raw.split(",", 1)[0].strip()
        if host and host not in candidates:
            candidates.append(host)
    return candidates


def _is_safe_redirect_target(target: str, request: Request) -> bool:
    """只允许站内相对路径或配置中明确声明的公开源。"""
    del request  # Never derive trust from a user-controlled Host header.
    if not target:
        return False
    if target.startswith("/") and not target.startswith("//"):
        return True

    parsed = urlparse(target)
    if not parsed.scheme or not parsed.netloc:
        return False

    target_origin = (parsed.scheme.lower(), parsed.netloc.lower())
    trusted_origins: set[tuple[str, str]] = set()
    for configured_url in (
        *_configured_cas_service_urls(),
        settings.cas_frontend_success_url,
        settings.cas_frontend_logout_redirect,
    ):
        configured_parsed = urlparse(str(configured_url or "").strip())
        if configured_parsed.scheme in {"http", "https"} and configured_parsed.netloc:
            trusted_origins.add(
                (configured_parsed.scheme.lower(), configured_parsed.netloc.lower())
            )
    return target_origin in trusted_origins


def _build_cas_service_url(request: Request) -> str:
    """Return an allowlisted CAS callback URL for this request.

    ``CAS_SERVICE_URL`` may list multiple public callbacks (comma-separated) for
    shared backends serving several frontends. The request Host /
    X-Forwarded-Host is only used to *select* among that allowlist; unknown
    hosts fall back to the first configured URL and never invent a new callback.
    """
    configured_urls = _configured_cas_service_urls()
    if not configured_urls:
        raise HTTPException(status_code=500, detail="CAS 回调地址未配置")

    host_candidates = _request_host_candidates(request)
    if host_candidates:
        for service_url in configured_urls:
            netloc = urlparse(service_url).netloc.lower()
            if netloc in host_candidates:
                return service_url

    return configured_urls[0]


def _absolute_public_url(target: str, request: Request) -> str:
    """Resolve a relative configured target against the trusted CAS origin."""
    if not target.startswith("/") or target.startswith("//"):
        return target
    service = urlparse(_build_cas_service_url(request))
    origin = urlunparse((service.scheme, service.netloc, "/", "", "", ""))
    return urljoin(origin, target.lstrip("/"))


def _get_cas_login_url() -> str:
    explicit = (settings.cas_login_url or "").strip()
    if explicit:
        return explicit

    base = (settings.cas_base_url or "").strip().rstrip("/")
    if base:
        return f"{base}/login"
    raise HTTPException(status_code=500, detail="CAS 登录地址未配置")


def _get_cas_logout_url() -> str:
    explicit = (settings.cas_logout_url or "").strip()
    if explicit:
        return explicit

    base = (settings.cas_base_url or "").strip().rstrip("/")
    if base:
        return f"{base}/logout"
    raise HTTPException(status_code=500, detail="CAS 注销地址未配置")


def _get_cas_validate_url() -> str:
    explicit = (settings.cas_validate_url or "").strip()
    if explicit:
        return explicit

    base = (settings.cas_base_url or "").strip().rstrip("/")
    if base:
        return f"{base}/serviceValidate"
    raise HTTPException(status_code=500, detail="CAS 票据校验地址未配置")


def _declares_doctype(xml_text: str) -> bool:
    """Report whether the prolog declares a DTD.

    The DTD is what enables entity expansion and external-entity resolution, and
    a CAS validate response never needs one. Only the prolog is inspected so the
    literal string inside ordinary element text cannot trigger a false reject.
    """

    index = 0
    length = len(xml_text)
    while index < length:
        char = xml_text[index]
        if char.isspace():
            index += 1
            continue
        if not xml_text.startswith("<", index):
            # Character data before the root element: malformed, let the parser
            # produce the error.
            return False
        if xml_text.startswith("<?", index):
            end = xml_text.find("?>", index)
            if end < 0:
                return False
            index = end + 2
            continue
        if xml_text.startswith("<!--", index):
            end = xml_text.find("-->", index)
            if end < 0:
                return False
            index = end + 3
            continue
        return xml_text[index : index + 9].upper() == "<!DOCTYPE"
    return False


def _pick_cas_display_name(attributes: dict[str, str]) -> str | None:
    if not attributes:
        return None

    normalized = {
        str(key or "").strip().lower(): str(value or "").strip()
        for key, value in attributes.items()
        if str(value or "").strip()
    }
    for key in CAS_DISPLAY_NAME_KEYS:
        value = normalized.get(key)
        if value:
            return value
    return None


def _parse_cas_validate_response(
    xml_text: str,
) -> tuple[bool, str | None, str | None, dict[str, str]]:
    """返回: (是否成功, 统一账号, 失败原因, 属性字典)。

    响应来自外部 CAS 服务，属于不可信输入：先限制长度并拒绝 DTD，避免实体展开
    与外部实体把票据校验变成解析放大/文件读取的攻击面。
    """
    if len(xml_text or "") > CAS_VALIDATE_RESPONSE_MAX_CHARS:
        return False, None, "CAS 返回内容过大", {}
    if _declares_doctype(xml_text or ""):
        return False, None, "CAS 返回了不允许的 XML 文档类型声明", {}
    try:
        root = ET.fromstring(xml_text)
    except ET.ParseError:
        return False, None, "CAS 返回了无效 XML", {}

    username: str | None = None
    attributes: dict[str, str] = {}

    for elem in root.iter():
        tag = elem.tag.split("}")[-1]
        if tag == "user":
            value = (elem.text or "").strip()
            if value and not username:
                username = value
            continue

        if tag in {"serviceResponse", "authenticationSuccess", "attributes"}:
            continue

        value = (elem.text or "").strip()
        if value:
            attributes[tag] = value

        if tag == "authenticationFailure":
            return False, None, (elem.text or "CAS 认证失败").strip(), {}

    if username:
        return True, username, None, attributes

    return False, None, "CAS 未返回用户信息", {}


async def _verify_cas_ticket(ticket: str, service_url: str) -> tuple[str, str | None]:
    validate_url = _get_cas_validate_url()
    timeout = httpx.Timeout(10.0, connect=5.0)

    async with httpx.AsyncClient(
        trust_env=False, timeout=timeout, verify=settings.cas_verify_ssl
    ) as client:
        response = await client.get(
            validate_url,
            params={
                "service": service_url,
                "ticket": ticket,
            },
        )

    if response.status_code != 200:
        raise HTTPException(status_code=401, detail="CAS 票据校验失败")

    ok, username, reason, attributes = _parse_cas_validate_response(response.text)
    if not ok or not username:
        raise HTTPException(status_code=401, detail=reason or "CAS 认证失败")

    return username, _pick_cas_display_name(attributes)


async def _issue_cas_exchange(
    redis_client: Redis,
    token: str,
    user: User,
) -> str:
    code = secrets.token_urlsafe(32)
    payload = {
        "token": token,
        "user": _serialize_user(user),
    }
    await redis_client.setex(
        f"{CAS_EXCHANGE_KEY_PREFIX}{code}",
        CAS_EXCHANGE_TTL_SECONDS,
        json.dumps(payload),
    )
    return code


def _is_trusted_existing_cas_user(user: User) -> bool:
    """Recognize only accounts that this CAS flow previously provisioned.

    The legacy users table has no provider/subject columns.  The generated
    ``cas_`` API-key prefix and the absence of a local password are therefore
    the only persisted provider markers available without a schema migration.
    Fail closed for password accounts and accounts created by another
    authentication flow.  An administrator may later promote a
    CAS-provisioned account (for example to ``admin``); the role update does
    not change the provider marker, so the account must remain trusted.
    """
    return (
        not bool(user.password)
        and str(getattr(user, "api_key", "") or "").startswith("cas_")
    )
