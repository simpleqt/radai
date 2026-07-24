"""Shared authentication resolution for API dependencies and request context."""

from __future__ import annotations

import asyncio
import hashlib
import json
import os
import time
from collections import OrderedDict
from dataclasses import dataclass
from datetime import datetime

from sqlalchemy import select
from sqlalchemy.exc import SQLAlchemyError

from backend.application.auth_security import token_session_is_current
from backend.application.exceptions import AuthenticationUnavailable

API_KEY_SCOPE_FULL = "full"
API_KEY_SCOPE_MCP_ONLY = "mcp_only"
MCP_API_PATH_PREFIXES = ("/mcp/sse", "/api/v1/mcp")


def api_key_scope_allows_path(access_scope: str, path: str) -> bool:
    """Return whether an API-key scope may access an HTTP path.

    Unknown scopes fail closed so a malformed database value cannot silently
    become a full-access key.
    """

    normalized_scope = str(access_scope or "").strip().lower()
    if normalized_scope == API_KEY_SCOPE_FULL:
        return True
    if normalized_scope != API_KEY_SCOPE_MCP_ONLY:
        return False
    normalized_path = "/" + str(path or "").lstrip("/")
    return any(
        normalized_path == prefix or normalized_path.startswith(f"{prefix}/")
        for prefix in MCP_API_PATH_PREFIXES
    )


@dataclass(frozen=True)
class ResolvedAuth:
    user_id: str
    role: str = "user"
    api_key: str = ""
    username: str | None = None
    source: str = ""
    access_scope: str = API_KEY_SCOPE_FULL
    mcp_allowed_tools: tuple[str, ...] | None = None
    rate_limit_per_minute: int = 0


class AuthService:
    """Resolve session tokens and API keys without tying callers to FastAPI."""

    def __init__(self) -> None:
        self._api_key_cache: OrderedDict[
            str, tuple[float, ResolvedAuth | None]
        ] = OrderedDict()
        self._api_key_cache_lock = asyncio.Lock()

    async def _get_cached_api_key(self, digest: str) -> tuple[bool, ResolvedAuth | None]:
        now = time.monotonic()
        async with self._api_key_cache_lock:
            cached = self._api_key_cache.get(digest)
            if cached is None:
                return False, None
            expires_at, auth = cached
            if expires_at <= now:
                self._api_key_cache.pop(digest, None)
                return False, None
            self._api_key_cache.move_to_end(digest)
            return True, auth

    async def _cache_api_key(
        self, digest: str, auth: ResolvedAuth | None, *, ttl: float
    ) -> None:
        max_entries = max(16, int(os.getenv("AUTH_API_KEY_CACHE_MAX_ENTRIES", "2048")))
        async with self._api_key_cache_lock:
            self._api_key_cache[digest] = (time.monotonic() + max(0.1, ttl), auth)
            self._api_key_cache.move_to_end(digest)
            while len(self._api_key_cache) > max_entries:
                self._api_key_cache.popitem(last=False)

    async def resolve_api_key(self, api_key: str) -> ResolvedAuth | None:
        value = (api_key or "").strip()
        if not value:
            return None
        digest = hashlib.sha256(value.encode("utf-8")).hexdigest()
        cache_hit, cached_auth = await self._get_cached_api_key(digest)
        if cache_hit:
            return cached_auth

        from backend.db.connection import read_only_session_context
        from backend.db.models import ApiKey, User

        try:
            async with read_only_session_context() as session:
                result = await session.execute(
                    select(
                        ApiKey.is_active,
                        ApiKey.expires_at,
                        ApiKey.user_id,
                        ApiKey.access_scope,
                        ApiKey.mcp_allowed_tools,
                        ApiKey.rate_limit,
                        User.role,
                        User.is_active,
                    )
                    .outerjoin(User, User.user_id == ApiKey.user_id)
                    .where(ApiKey.api_key == value)
                )
                row = result.first()
        except SQLAlchemyError as exc:
            raise AuthenticationUnavailable() from exc
        cache_ttl = max(0.1, float(os.getenv("AUTH_API_KEY_CACHE_TTL_SECONDS", "5")))
        if not row:
            await self._cache_api_key(digest, None, ttl=min(cache_ttl, 1.0))
            return None

        key_is_active = row[0]
        key_expires_at = row[1]
        key_user_id = row[2]
        key_access_scope = str(row[3] or API_KEY_SCOPE_FULL).strip().lower()
        raw_mcp_allowed_tools = row[4]
        # Keep compatibility with extension/test session adapters that still
        # return the pre-rate-limit seven-column projection.
        if len(row) >= 8:
            key_rate_limit = int(row[5] or 0)
            user_role = row[6]
            user_is_active = row[7]
        else:
            key_rate_limit = 0
            user_role = row[5]
            user_is_active = row[6]

        mcp_allowed_tools: tuple[str, ...] | None
        if raw_mcp_allowed_tools is None:
            mcp_allowed_tools = None
        elif isinstance(raw_mcp_allowed_tools, list):
            mcp_allowed_tools = tuple(
                dict.fromkeys(
                    str(name).strip()
                    for name in raw_mcp_allowed_tools
                    if str(name).strip()
                )
            )
        else:
            # Malformed permission data must never expand access.
            mcp_allowed_tools = ()

        if not key_is_active:
            await self._cache_api_key(digest, None, ttl=min(cache_ttl, 1.0))
            return None
        if key_expires_at and key_expires_at <= datetime.now():
            await self._cache_api_key(digest, None, ttl=min(cache_ttl, 1.0))
            return None
        if key_user_id and user_is_active is False:
            await self._cache_api_key(digest, None, ttl=min(cache_ttl, 1.0))
            return None

        resolved = ResolvedAuth(
            api_key=value,
            user_id=key_user_id or "",
            role=user_role or "user",
            source="api_key",
            access_scope=key_access_scope,
            mcp_allowed_tools=mcp_allowed_tools,
            rate_limit_per_minute=key_rate_limit,
        )
        if key_expires_at:
            cache_ttl = min(
                cache_ttl,
                max(0.1, (key_expires_at - datetime.now()).total_seconds()),
            )
        await self._cache_api_key(digest, resolved, ttl=cache_ttl)
        return resolved

    async def resolve_bearer_token(self, token: str) -> ResolvedAuth | None:
        value = (token or "").strip()
        if not value:
            return None

        from backend.db.redis import get_redis

        redis_client = get_redis()
        try:
            try:
                raw = await redis_client.get(f"auth:token:{value}")
            except Exception as exc:
                raise AuthenticationUnavailable() from exc
            if not raw:
                return None

            try:
                token_info = json.loads(raw)
            except Exception:
                return None
            try:
                if not await token_session_is_current(redis_client, token_info):
                    return None
            except Exception as exc:
                raise AuthenticationUnavailable() from exc
        finally:
            await redis_client.aclose()

        user_id = str(token_info.get("user_id") or "").strip()
        if not user_id:
            return None

        return ResolvedAuth(
            api_key="",
            user_id=user_id,
            role=str(token_info.get("role") or "user"),
            username=token_info.get("username") or token_info.get("display_name"),
            source="session_token",
        )

    async def resolve_credentials(
        self,
        *,
        x_api_key: str | None = None,
        bearer: str | None = None,
    ) -> ResolvedAuth | None:
        api_key = (x_api_key or "").strip()
        token = (bearer or "").strip()

        if api_key:
            return await self.resolve_api_key(api_key)
        if token:
            token_auth = await self.resolve_bearer_token(token)
            if token_auth:
                return token_auth
            return await self.resolve_api_key(token)
        return None


auth_service = AuthService()
