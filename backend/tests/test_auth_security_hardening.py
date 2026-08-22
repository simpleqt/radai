"""Security regressions for CAS, login throttling, sessions, and SMS codes."""

from __future__ import annotations

import asyncio
import json

import pytest
from fastapi import HTTPException
from starlette.requests import Request

from backend.api.routers import auth as auth_router
from backend.api.routers import auth_sms, guest_sms_auth
from backend.application.auth_security import (
    login_rate_limit_keys,
    revoke_user_sessions,
    token_session_is_current,
)
from backend.db.models import User


class FakeRedis:
    def __init__(self):
        self.store: dict[str, str] = {}
        self.quota_counts: dict[str, int] = {}

    async def get(self, key: str):
        return self.store.get(key)

    async def setex(self, key: str, _ttl: int, value: str):
        self.store[key] = value
        return True

    async def set(self, key: str, value: str, *, ex=None, nx=False):
        del ex
        if nx and key in self.store:
            return False
        self.store[key] = value
        return True

    async def delete(self, *keys: str):
        deleted = 0
        for key in keys:
            if key in self.store:
                deleted += 1
                del self.store[key]
        return deleted

    async def incr(self, key: str):
        value = int(self.store.get(key, "0")) + 1
        self.store[key] = str(value)
        return value

    async def eval(self, script: str, number_of_keys: int, *args):
        del number_of_keys
        if "auth-getdel-v1" in script:
            key = str(args[0])
            return self.store.pop(key, None)

        if "auth-login-failure-v1" in script:
            failure_key, block_key = str(args[0]), str(args[1])
            max_attempts = int(args[3])
            count = int(self.store.get(failure_key, "0")) + 1
            self.store[failure_key] = str(count)
            if count >= max_attempts:
                self.store[block_key] = "1"
            return count

        if "auth-verification-code-v1" in script:
            key = str(args[0])
            submitted = str(args[1])
            max_attempts = int(args[2])
            raw = self.store.get(key)
            if raw is None:
                return "expired"
            try:
                item = json.loads(raw)
            except (TypeError, json.JSONDecodeError):
                self.store.pop(key, None)
                return "expired"
            if str(item.get("code") or "") == submitted:
                self.store.pop(key, None)
                return "ok"
            attempts = int(item.get("attempts") or 0) + 1
            if attempts >= max_attempts:
                self.store.pop(key, None)
                return "exhausted"
            item["attempts"] = attempts
            self.store[key] = json.dumps(item)
            return "invalid"

        if "auth-captcha-v1" in script:
            key = str(args[0])
            submitted = str(args[1])
            max_attempts = int(args[2])
            raw = self.store.get(key)
            if raw is None:
                return "expired"
            try:
                item = json.loads(raw)
            except (TypeError, json.JSONDecodeError):
                self.store.pop(key, None)
                return "expired"
            if str(item.get("answer") or "") == submitted:
                self.store.pop(key, None)
                return "ok"
            attempts = int(item.get("attempts") or 0) + 1
            if attempts >= max_attempts:
                self.store.pop(key, None)
                return "exhausted"
            item["attempts"] = attempts
            self.store[key] = json.dumps(item)
            return "invalid"

        if "auth-sliding-window-quota-v1" in script:
            key = str(args[0])
            max_requests = int(args[2])
            count = self.quota_counts.get(key, 0)
            if count >= max_requests:
                return [0, 23]
            self.quota_counts[key] = count + 1
            return [1, 0]

        raise AssertionError("unexpected Lua script")


class _ScalarResult:
    def scalar_one_or_none(self):
        return None


class _LoginSession:
    async def execute(self, _statement):
        return _ScalarResult()


class _ExistingUserResult:
    def __init__(self, user):
        self.user = user

    def scalar_one_or_none(self):
        return self.user


class _ExistingUserSession:
    def __init__(self, user):
        self.user = user
        self.committed = False
        self.execute_count = 0

    async def execute(self, _statement):
        self.execute_count += 1
        return _ExistingUserResult(self.user)

    async def flush(self):
        return None

    async def commit(self):
        self.committed = True


def _request(*, host: str = "evil.example", ip: str = "203.0.113.10") -> Request:
    return Request(
        {
            "type": "http",
            "method": "POST",
            "path": "/api/v1/auth/login",
            "query_string": b"",
            "headers": [
                (b"host", host.encode()),
                (b"x-forwarded-host", b"attacker.example"),
                (b"x-forwarded-proto", b"http"),
            ],
            "scheme": "http",
            "server": (host, 80),
            "client": (ip, 12345),
        }
    )


def test_cas_service_url_ignores_unknown_host_and_forwarding_headers(monkeypatch):
    configured = "https://login.example.edu/api/v1/auth/cas/callback"
    monkeypatch.setattr(auth_router.settings, "cas_service_url", configured)

    assert auth_router._build_cas_service_url(_request()) == configured


def test_cas_service_url_selects_allowlisted_host(monkeypatch):
    configured = (
        "https://radai2.cdut.edu.cn/api/v1/auth/cas/callback,"
        "https://csh-ai.cdut.edu.cn/api/v1/auth/cas/callback,"
        "https://fmca.cdut.edu.cn/api/v1/auth/cas/callback"
    )
    monkeypatch.setattr(auth_router.settings, "cas_service_url", configured)

    assert (
        auth_router._build_cas_service_url(_request(host="csh-ai.cdut.edu.cn"))
        == "https://csh-ai.cdut.edu.cn/api/v1/auth/cas/callback"
    )
    assert (
        auth_router._build_cas_service_url(_request(host="radai2.cdut.edu.cn"))
        == "https://radai2.cdut.edu.cn/api/v1/auth/cas/callback"
    )
    assert (
        auth_router._build_cas_service_url(_request(host="fmca.cdut.edu.cn"))
        == "https://fmca.cdut.edu.cn/api/v1/auth/cas/callback"
    )


def test_cas_service_url_rejects_unknown_host_to_default(monkeypatch):
    configured = (
        "https://radai2.cdut.edu.cn/api/v1/auth/cas/callback,"
        "https://csh-ai.cdut.edu.cn/api/v1/auth/cas/callback"
    )
    monkeypatch.setattr(auth_router.settings, "cas_service_url", configured)

    assert (
        auth_router._build_cas_service_url(_request(host="evil.example"))
        == "https://radai2.cdut.edu.cn/api/v1/auth/cas/callback"
    )


def test_cas_service_url_requires_fixed_absolute_configuration(monkeypatch):
    monkeypatch.setattr(auth_router.settings, "cas_service_url", None)

    with pytest.raises(HTTPException) as error:
        auth_router._build_cas_service_url(_request())

    assert error.value.status_code == 500


@pytest.mark.asyncio
async def test_cas_exchange_code_is_one_time():
    redis = FakeRedis()
    code = "one-time-code"
    key = f"{auth_router.CAS_EXCHANGE_KEY_PREFIX}{code}"
    await redis.setex(
        key,
        60,
        json.dumps(
            {
                "token": "long-lived-token",
                "user": {"user_id": "user-1", "username": "alice"},
            }
        ),
    )

    response = await auth_router.cas_exchange(
        auth_router.CasExchangeRequest(code=code),
        redis_client=redis,
    )
    assert response.token == "long-lived-token"
    assert key not in redis.store

    with pytest.raises(HTTPException) as reused:
        await auth_router.cas_exchange(
            auth_router.CasExchangeRequest(code=code),
            redis_client=redis,
        )
    assert reused.value.status_code == 401


@pytest.mark.asyncio
async def test_login_is_blocked_by_username_after_failures():
    redis = FakeRedis()
    request = _request()
    payload = auth_router.LoginRequest(username="missing-user", password="wrong")

    for _ in range(auth_router.LOGIN_MAX_FAILURES):
        response = await auth_router.login(
            payload,
            request,
            session=_LoginSession(),
            redis_client=redis,
        )
        assert response.success is False

    with pytest.raises(HTTPException) as blocked:
        await auth_router.login(
            payload,
            request,
            session=_LoginSession(),
            redis_client=redis,
        )

    assert blocked.value.status_code == 429
    _, username_block = login_rate_limit_keys("username", "missing-user")
    assert redis.store[username_block] == "1"
    assert "missing-user" not in " ".join(redis.store)


@pytest.mark.asyncio
async def test_login_is_blocked_by_ip_across_distinct_usernames():
    redis = FakeRedis()
    request = _request()

    for attempt in range(auth_router.LOGIN_IP_MAX_FAILURES):
        response = await auth_router.login(
            auth_router.LoginRequest(username=f"missing-{attempt}", password="wrong"),
            request,
            session=_LoginSession(),
            redis_client=redis,
        )
        assert response.success is False

    with pytest.raises(HTTPException) as blocked:
        await auth_router.login(
            auth_router.LoginRequest(username="another-user", password="wrong"),
            request,
            session=_LoginSession(),
            redis_client=redis,
        )

    assert blocked.value.status_code == 429
    _, ip_block = login_rate_limit_keys("ip", "203.0.113.10")
    assert redis.store[ip_block] == "1"


@pytest.mark.asyncio
async def test_session_epoch_revokes_legacy_and_current_tokens():
    redis = FakeRedis()
    legacy = {"user_id": "user-1"}
    current = {"user_id": "user-1", "session_epoch": 0}

    assert await token_session_is_current(redis, legacy) is True
    assert await token_session_is_current(redis, current) is True

    assert await revoke_user_sessions(redis, "user-1") == 1
    assert await token_session_is_current(redis, legacy) is False
    assert await token_session_is_current(redis, current) is False
    assert (
        await token_session_is_current(redis, {"user_id": "user-1", "session_epoch": 1})
        is True
    )


@pytest.mark.asyncio
async def test_issued_token_carries_current_session_epoch():
    redis = FakeRedis()
    await revoke_user_sessions(redis, "user-1")
    user = User(
        user_id="user-1",
        username="alice",
        password="hash",
        api_key="key",
        role="user",
        is_active=True,
    )

    token = await auth_router._issue_auth_token(redis, user)
    token_info = json.loads(redis.store[f"auth:token:{token}"])

    assert token_info["session_epoch"] == 1
    assert await token_session_is_current(redis, token_info) is True


@pytest.mark.asyncio
async def test_password_login_skips_last_login_write_in_read_only_mode(monkeypatch):
    monkeypatch.setenv("MYSQL_SESSION_READ_ONLY", "true")
    monkeypatch.setattr(auth_router, "verify_password", lambda *_args: True)
    redis = FakeRedis()
    user = User(
        user_id="user-1",
        username="alice",
        password="hash",
        api_key="key",
        role="user",
        is_active=True,
    )
    session = _ExistingUserSession(user)

    response = await auth_router.login(
        auth_router.LoginRequest(username="alice", password="correct"),
        _request(),
        session=session,
        redis_client=redis,
    )

    assert response.success is True
    assert response.token
    assert session.execute_count == 1
    assert session.committed is False
    assert user.last_login is None


@pytest.mark.asyncio
async def test_password_login_updates_last_login_in_writable_mode(monkeypatch):
    monkeypatch.delenv("MYSQL_SESSION_READ_ONLY", raising=False)
    monkeypatch.setattr(auth_router, "verify_password", lambda *_args: True)
    redis = FakeRedis()
    user = User(
        user_id="user-1",
        username="alice",
        password="hash",
        api_key="key",
        role="user",
        is_active=True,
    )
    session = _ExistingUserSession(user)

    response = await auth_router.login(
        auth_router.LoginRequest(username="alice", password="correct"),
        _request(),
        session=session,
        redis_client=redis,
    )

    assert response.success is True
    assert response.token
    assert session.execute_count == 2
    assert session.committed is True
    assert user.last_login is not None


@pytest.mark.asyncio
async def test_school_sms_code_has_atomic_attempt_limit():
    redis = FakeRedis()
    key = auth_sms._sms_code_key("forgot", "13800138000")
    await redis.setex(key, 300, json.dumps({"code": "123456", "attempts": 0}))

    for _ in range(auth_sms.SMS_CODE_MAX_ATTEMPTS - 1):
        ok, message = await auth_sms._consume_sms_code(
            redis, "forgot", "13800138000", "000000"
        )
        assert ok is False
        assert message == "验证码错误"

    ok, message = await auth_sms._consume_sms_code(
        redis, "forgot", "13800138000", "000000"
    )
    assert ok is False
    assert "错误次数过多" in message
    assert key not in redis.store


@pytest.mark.asyncio
async def test_guest_sms_code_uses_same_atomic_attempt_limit():
    redis = FakeRedis()
    key = "guest_sms:code:13800138000"
    await redis.setex(key, 300, json.dumps({"code": "654321", "attempts": 0}))

    ok, message = await guest_sms_auth._consume_guest_sms_code(
        redis, "13800138000", "654321"
    )

    assert ok is True
    assert message == ""
    assert key not in redis.store


@pytest.mark.asyncio
async def test_guest_captcha_can_only_be_consumed_once_under_concurrency():
    redis = FakeRedis()
    captcha_id = "guest-captcha"
    key = f"guest_sms:captcha:{captcha_id}"
    await redis.setex(key, 300, json.dumps({"answer": "9", "attempts": 0}))

    results = await asyncio.gather(
        guest_sms_auth._verify_captcha(redis, captcha_id, "9"),
        guest_sms_auth._verify_captcha(redis, captcha_id, "9"),
    )

    assert sum(1 for valid, _ in results if valid) == 1
    assert key not in redis.store


@pytest.mark.asyncio
async def test_guest_sms_ip_quota_spans_different_phone_numbers(monkeypatch):
    redis = FakeRedis()
    monkeypatch.setattr(guest_sms_auth, "SMS_IP_RATE_MAX_REQUESTS", 1)
    gateway_calls = 0

    async def fake_send(_telephone: str, _code: str):
        nonlocal gateway_calls
        gateway_calls += 1
        return True, "验证码已发送"

    monkeypatch.setattr(guest_sms_auth, "_send_tencent_sms", fake_send)
    request = _request(ip="198.51.100.9")

    first_key = "guest_sms:captcha:first"
    await redis.setex(first_key, 300, json.dumps({"answer": "1", "attempts": 0}))
    first = await guest_sms_auth.send_guest_sms_code(
        guest_sms_auth.GuestSmsSendCodeRequest(
            telephone="13800138000", captcha_id="first", captcha_answer="1"
        ),
        request,
        redis_client=redis,
    )
    assert first["success"] is True

    second_key = "guest_sms:captcha:second"
    await redis.setex(second_key, 300, json.dumps({"answer": "2", "attempts": 0}))
    second = await guest_sms_auth.send_guest_sms_code(
        guest_sms_auth.GuestSmsSendCodeRequest(
            telephone="13800138001", captcha_id="second", captcha_answer="2"
        ),
        request,
        redis_client=redis,
    )

    body = json.loads(second.body)
    assert second.status_code == 429
    assert second.headers["retry-after"] == "23"
    assert body["error"] == guest_sms_auth.SMS_RATE_LIMIT_MESSAGE
    assert gateway_calls == 1


@pytest.mark.asyncio
async def test_guest_sms_login_rejects_existing_non_sms_admin_collision():
    phone = "13800138000"
    redis = FakeRedis()
    await redis.setex(
        f"guest_sms:code:{phone}",
        300,
        json.dumps({"code": "654321", "attempts": 0}),
    )
    user = User(
        user_id="admin-collision",
        username=f"guest_{phone}",
        password="local-password-hash",
        api_key="local-admin-key",
        role="admin",
        is_active=True,
    )
    session = _ExistingUserSession(user)

    response = await guest_sms_auth.guest_sms_login(
        guest_sms_auth.GuestSmsLoginRequest(telephone=phone, code="654321"),
        session=session,
        redis_client=redis,
    )

    assert response["success"] is False
    assert session.committed is False
    assert not any(key.startswith("auth:token:") for key in redis.store)


@pytest.mark.asyncio
async def test_guest_sms_register_rejects_existing_non_sms_admin_collision():
    phone = "13800138000"
    redis = FakeRedis()
    await redis.setex(
        f"guest_sms:code:{phone}",
        300,
        json.dumps({"code": "654321", "attempts": 0}),
    )
    user = User(
        user_id="admin-collision",
        username=f"guest_{phone}",
        password="local-password-hash",
        api_key="local-admin-key",
        role="admin",
        is_active=True,
    )
    session = _ExistingUserSession(user)

    response = await guest_sms_auth.guest_sms_register(
        guest_sms_auth.GuestSmsRegisterRequest(
            telephone=phone,
            code="654321",
            password="new-password",
            confirm_password="new-password",
        ),
        session=session,
        redis_client=redis,
    )

    assert response["success"] is False
    assert user.password == "local-password-hash"
    assert session.committed is False
    assert not any(key.startswith("auth:token:") for key in redis.store)


@pytest.mark.asyncio
async def test_guest_sms_login_reuses_existing_sms_user():
    phone = "13800138000"
    redis = FakeRedis()
    await redis.setex(
        f"guest_sms:code:{phone}",
        300,
        json.dumps({"code": "654321", "attempts": 0}),
    )
    user = User(
        user_id="guest-sms-user",
        username=f"guest_{phone}",
        password="sms-password-hash",
        api_key="guest_sms_existing-marker",
        role="user",
        is_active=True,
    )
    session = _ExistingUserSession(user)

    response = await guest_sms_auth.guest_sms_login(
        guest_sms_auth.GuestSmsLoginRequest(telephone=phone, code="654321"),
        session=session,
        redis_client=redis,
    )

    assert response["success"] is True
    assert response["user"]["user_id"] == "guest-sms-user"
    assert session.committed is True
    assert any(key.startswith("auth:token:") for key in redis.store)
