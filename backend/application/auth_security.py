"""Redis-backed authentication security primitives.

The helpers in this module are deliberately independent of FastAPI so every
authentication surface applies the same session and verification rules.
"""

from __future__ import annotations

import hashlib
import secrets
from typing import Any

from redis.asyncio import Redis

SESSION_EPOCH_PREFIX = "auth:session_epoch:"

_CONSUME_CODE_SCRIPT = r"""
-- auth-verification-code-v1
local raw = redis.call('GET', KEYS[1])
if not raw then
  return 'expired'
end

local decoded_ok, item = pcall(cjson.decode, raw)
if not decoded_ok or type(item) ~= 'table' then
  redis.call('DEL', KEYS[1])
  return 'expired'
end

local expected = tostring(item['code'] or '')
local submitted = tostring(ARGV[1] or '')
if expected == submitted then
  redis.call('DEL', KEYS[1])
  return 'ok'
end

local attempts = tonumber(item['attempts'] or 0) + 1
local max_attempts = tonumber(ARGV[2])
if attempts >= max_attempts then
  redis.call('DEL', KEYS[1])
  return 'exhausted'
end

item['attempts'] = attempts
local ttl = redis.call('PTTL', KEYS[1])
redis.call('SET', KEYS[1], cjson.encode(item))
if ttl > 0 then
  redis.call('PEXPIRE', KEYS[1], ttl)
else
  redis.call('EXPIRE', KEYS[1], tonumber(ARGV[3]))
end
return 'invalid'
"""

_CONSUME_CAPTCHA_SCRIPT = r"""
-- auth-captcha-v1
local raw = redis.call('GET', KEYS[1])
if not raw then
  return 'expired'
end

local decoded_ok, item = pcall(cjson.decode, raw)
if not decoded_ok or type(item) ~= 'table' then
  redis.call('DEL', KEYS[1])
  return 'expired'
end

local expected = tostring(item['answer'] or '')
local submitted = tostring(ARGV[1] or '')
if expected == submitted then
  redis.call('DEL', KEYS[1])
  return 'ok'
end

local attempts = tonumber(item['attempts'] or 0) + 1
local max_attempts = tonumber(ARGV[2])
if attempts >= max_attempts then
  redis.call('DEL', KEYS[1])
  return 'exhausted'
end

item['attempts'] = attempts
local ttl = redis.call('PTTL', KEYS[1])
redis.call('SET', KEYS[1], cjson.encode(item))
if ttl > 0 then
  redis.call('PEXPIRE', KEYS[1], ttl)
else
  redis.call('EXPIRE', KEYS[1], tonumber(ARGV[3]))
end
return 'invalid'
"""

_SLIDING_WINDOW_QUOTA_SCRIPT = r"""
-- auth-sliding-window-quota-v1
local redis_time = redis.call('TIME')
local now_ms = tonumber(redis_time[1]) * 1000 + math.floor(tonumber(redis_time[2]) / 1000)
local window_ms = tonumber(ARGV[1]) * 1000
local max_requests = tonumber(ARGV[2])
local cutoff_ms = now_ms - window_ms

redis.call('ZREMRANGEBYSCORE', KEYS[1], '-inf', cutoff_ms)
local count = redis.call('ZCARD', KEYS[1])
if count >= max_requests then
  local oldest = redis.call('ZRANGE', KEYS[1], 0, 0, 'WITHSCORES')
  local retry_after = 1
  if oldest[2] then
    retry_after = math.max(1, math.ceil((tonumber(oldest[2]) + window_ms - now_ms) / 1000))
  end
  redis.call('EXPIRE', KEYS[1], tonumber(ARGV[1]))
  return {0, retry_after}
end

local member = tostring(now_ms) .. ':' .. tostring(redis_time[2]) .. ':' .. tostring(ARGV[3])
redis.call('ZADD', KEYS[1], now_ms, member)
redis.call('EXPIRE', KEYS[1], tonumber(ARGV[1]))
return {1, 0}
"""

_RECORD_LOGIN_FAILURE_SCRIPT = r"""
-- auth-login-failure-v1
local count = redis.call('INCR', KEYS[1])
if count == 1 then
  redis.call('EXPIRE', KEYS[1], tonumber(ARGV[1]))
end
if count >= tonumber(ARGV[2]) then
  redis.call('SET', KEYS[2], '1', 'EX', tonumber(ARGV[3]))
end
return count
"""

_GETDEL_SCRIPT = r"""
-- auth-getdel-v1
local value = redis.call('GET', KEYS[1])
if value then
  redis.call('DEL', KEYS[1])
end
return value
"""


def session_epoch_key(user_id: str) -> str:
    return f"{SESSION_EPOCH_PREFIX}{user_id}"


async def get_session_epoch(redis_client: Redis, user_id: str) -> int:
    raw = await redis_client.get(session_epoch_key(user_id))
    try:
        return max(0, int(raw or 0))
    except (TypeError, ValueError):
        # A malformed server-side epoch must fail closed for existing tokens.
        return -1


async def token_session_is_current(
    redis_client: Redis,
    token_info: dict[str, Any],
) -> bool:
    user_id = str(token_info.get("user_id") or "").strip()
    if not user_id:
        return False
    try:
        token_epoch = max(0, int(token_info.get("session_epoch") or 0))
    except (TypeError, ValueError):
        return False
    current_epoch = await get_session_epoch(redis_client, user_id)
    return current_epoch >= 0 and token_epoch == current_epoch


async def revoke_user_sessions(redis_client: Redis, user_id: str) -> int:
    """Invalidate all current bearer sessions, including legacy unindexed ones."""
    return int(await redis_client.incr(session_epoch_key(str(user_id))))


def login_principal_digest(value: str) -> str:
    normalized = (value or "").strip().casefold()
    return hashlib.sha256(normalized.encode("utf-8")).hexdigest()


def login_rate_limit_keys(kind: str, value: str) -> tuple[str, str]:
    digest = login_principal_digest(value)
    prefix = f"auth:login_rate:{kind}:{digest}"
    return f"{prefix}:failures", f"{prefix}:blocked"


async def login_is_blocked(redis_client: Redis, kind: str, value: str) -> bool:
    _, block_key = login_rate_limit_keys(kind, value)
    return bool(await redis_client.get(block_key))


async def record_login_failure(
    redis_client: Redis,
    kind: str,
    value: str,
    *,
    window_seconds: int,
    max_attempts: int,
    block_seconds: int,
) -> int:
    failure_key, block_key = login_rate_limit_keys(kind, value)
    result = await redis_client.eval(
        _RECORD_LOGIN_FAILURE_SCRIPT,
        2,
        failure_key,
        block_key,
        window_seconds,
        max_attempts,
        block_seconds,
    )
    return int(result or 0)


async def clear_login_failures(redis_client: Redis, kind: str, value: str) -> None:
    failure_key, block_key = login_rate_limit_keys(kind, value)
    await redis_client.delete(failure_key, block_key)


async def consume_verification_code(
    redis_client: Redis,
    key: str,
    submitted_code: str,
    *,
    max_attempts: int,
    fallback_ttl_seconds: int,
) -> str:
    """Atomically verify a JSON code payload and increment failed attempts."""
    result = await redis_client.eval(
        _CONSUME_CODE_SCRIPT,
        1,
        key,
        submitted_code,
        max_attempts,
        fallback_ttl_seconds,
    )
    if isinstance(result, bytes):
        result = result.decode("utf-8", errors="replace")
    normalized = str(result or "expired")
    return (
        normalized
        if normalized in {"ok", "expired", "invalid", "exhausted"}
        else "expired"
    )


async def consume_captcha(
    redis_client: Redis,
    key: str,
    submitted_answer: str,
    *,
    max_attempts: int,
    fallback_ttl_seconds: int,
) -> str:
    """Atomically verify a JSON captcha payload and consume successful answers."""
    result = await redis_client.eval(
        _CONSUME_CAPTCHA_SCRIPT,
        1,
        key,
        submitted_answer,
        max_attempts,
        fallback_ttl_seconds,
    )
    if isinstance(result, bytes):
        result = result.decode("utf-8", errors="replace")
    normalized = str(result or "expired")
    return (
        normalized
        if normalized in {"ok", "expired", "invalid", "exhausted"}
        else "expired"
    )


def sms_ip_quota_key(client_ip: str) -> str:
    """Build a privacy-preserving quota key shared by all SMS send surfaces."""
    digest = hashlib.sha256((client_ip or "unknown").encode("utf-8")).hexdigest()
    return f"auth:sms_send_rate:ip:{digest}"


async def consume_sms_ip_quota(
    redis_client: Redis,
    client_ip: str,
    *,
    window_seconds: int,
    max_requests: int,
) -> tuple[bool, int]:
    """Consume one slot from an atomic Redis sliding-window SMS quota."""
    return await consume_sliding_window_quota(
        redis_client,
        sms_ip_quota_key(client_ip),
        window_seconds=window_seconds,
        max_requests=max_requests,
    )


async def consume_sliding_window_quota(
    redis_client: Redis,
    key: str,
    *,
    window_seconds: int,
    max_requests: int,
) -> tuple[bool, int]:
    """Consume one slot from a process-independent atomic Redis quota."""
    result = await redis_client.eval(
        _SLIDING_WINDOW_QUOTA_SCRIPT,
        1,
        key,
        max(1, int(window_seconds)),
        max(1, int(max_requests)),
        secrets.token_hex(8),
    )
    try:
        allowed, retry_after = result
        return bool(int(allowed)), max(0, int(retry_after))
    except (TypeError, ValueError):
        # A malformed Redis response must fail closed without exposing internals.
        return False, max(1, int(window_seconds))


async def getdel(redis_client: Redis, key: str) -> str | None:
    """Atomically read and consume a value on Redis versions before GETDEL."""
    value = await redis_client.eval(_GETDEL_SCRIPT, 1, key)
    if isinstance(value, bytes):
        return value.decode("utf-8", errors="replace")
    return str(value) if value is not None else None
