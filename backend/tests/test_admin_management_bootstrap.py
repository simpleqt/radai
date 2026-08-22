from urllib.parse import parse_qs, urlparse

import pytest
from fastapi import HTTPException
from starlette.requests import Request
from werkzeug.security import check_password_hash

from backend.api.routers import auth as auth_router
from backend.db import connection
from backend.db.init_db import create_default_user
from backend.db.models import ApiKey, User


class _SyncQuery:
    def __init__(self, value=None):
        self.value = value

    def filter(self, *_args):
        return self

    def first(self):
        return self.value


class _SyncSession:
    def __init__(self, existing_user=None):
        self.added = []
        self.committed = False
        self.closed = False
        self.existing_user = existing_user

    def query(self, _model):
        return _SyncQuery(self.existing_user)

    def add(self, value):
        self.added.append(value)

    def flush(self):
        return None

    def commit(self):
        self.committed = True

    def rollback(self):
        return None

    def close(self):
        self.closed = True


class _ScalarResult:
    def __init__(self, value):
        self.value = value

    def scalar_one_or_none(self):
        return self.value


class _AsyncSession:
    def __init__(self, user):
        self.user = user
        self.committed = False

    async def execute(self, _statement):
        return _ScalarResult(self.user)

    async def commit(self):
        self.committed = True


def _request() -> Request:
    return Request(
        {
            "type": "http",
            "method": "GET",
            "path": "/api/v1/auth/cas/callback",
            "query_string": b"",
            "headers": [],
            "scheme": "http",
            "server": ("testserver", 80),
            "client": ("127.0.0.1", 12345),
        }
    )


def test_create_default_user_creates_login_ready_admin(monkeypatch):
    session = _SyncSession()
    monkeypatch.setattr(connection, "get_session", lambda: session)
    monkeypatch.setenv("ADMIN_INITIAL_PASSWORD", "change-me-now")
    monkeypatch.setenv("ADMIN_INITIAL_API_KEY", "test-admin-key")

    create_default_user()

    user = next(item for item in session.added if isinstance(item, User))
    api_key = next(item for item in session.added if isinstance(item, ApiKey))
    assert user.username == "admin"
    assert user.role == "admin"
    assert user.is_active is True
    assert check_password_hash(user.password, "change-me-now")
    assert api_key.user_id == user.user_id
    assert api_key.api_key == "test-admin-key"
    assert session.committed is True
    assert session.closed is True


def test_create_default_user_repairs_legacy_admin(monkeypatch):
    user = User(
        user_id="legacy-admin",
        username="admin",
        password=None,
        email="admin@example.com",
        api_key="legacy-key",
        role="user",
        is_active=True,
    )
    session = _SyncSession(existing_user=user)
    monkeypatch.setattr(connection, "get_session", lambda: session)
    monkeypatch.setenv("ADMIN_INITIAL_PASSWORD", "repaired-password")

    create_default_user()

    assert user.role == "admin"
    assert check_password_hash(user.password, "repaired-password")
    assert session.committed is True
    assert session.closed is True


@pytest.mark.asyncio
async def test_cas_callback_rejects_existing_admin_account_collision(monkeypatch):
    user = User(
        user_id="admin-1",
        username="admin",
        password=None,
        email="admin@example.com",
        api_key="admin-key",
        role="admin",
        is_active=True,
    )
    session = _AsyncSession(user)

    monkeypatch.setattr(auth_router.settings, "cas_enabled", True)
    monkeypatch.setattr(auth_router, "_build_cas_service_url", lambda _request: "http://testserver/callback")

    async def verify(_ticket, _service_url):
        return "admin", "平台管理员"

    monkeypatch.setattr(auth_router, "_verify_cas_ticket", verify)

    with pytest.raises(HTTPException) as exc_info:
        await auth_router.cas_callback(
            _request(), ticket="ticket", session=session, redis_client=object()
        )

    assert user.role == "admin"
    assert exc_info.value.status_code == 409
    assert session.committed is False


@pytest.mark.asyncio
async def test_cas_callback_trusts_cas_provisioned_admin(monkeypatch):
    # A CAS-provisioned account promoted to admin keeps its cas_ provider
    # marker, so the CAS callback must not treat it as an unrelated
    # privileged-account collision.
    user = User(
        user_id="cas-admin-1",
        username="cas-admin",
        password=None,
        email=None,
        api_key="cas_promoted-key",
        role="admin",
        is_active=True,
    )
    session = _AsyncSession(user)

    monkeypatch.setattr(auth_router.settings, "cas_enabled", True)
    monkeypatch.setattr(auth_router.settings, "cas_frontend_success_url", "/login")
    monkeypatch.setattr(
        auth_router,
        "_build_cas_service_url",
        lambda _request: "http://testserver/callback",
    )

    async def verify(_ticket, _service_url):
        return "cas-admin", "CAS 管理员"

    async def issue(_redis, issued_user, _display_name):
        assert issued_user is user
        return "cas-admin-token"

    async def issue_exchange(_redis, token, issued_user):
        assert token == "cas-admin-token"
        assert issued_user is user
        return "exchange-code"

    monkeypatch.setattr(auth_router, "_verify_cas_ticket", verify)
    monkeypatch.setattr(auth_router, "_issue_auth_token", issue)
    monkeypatch.setattr(auth_router, "_issue_cas_exchange", issue_exchange)

    response = await auth_router.cas_callback(
        _request(), ticket="ticket", session=session, redis_client=object()
    )

    query = parse_qs(urlparse(response.headers["location"]).query)
    assert query["code"] == ["exchange-code"]
    assert session.committed is True


@pytest.mark.asyncio
async def test_cas_callback_reuses_only_existing_cas_user(monkeypatch):
    user = User(
        user_id="cas-user-1",
        username="cas-user",
        password=None,
        email=None,
        api_key="cas_existing-marker",
        role="user",
        is_active=True,
    )
    session = _AsyncSession(user)

    monkeypatch.setattr(auth_router.settings, "cas_enabled", True)
    monkeypatch.setattr(auth_router.settings, "cas_frontend_success_url", "/login")
    monkeypatch.setattr(
        auth_router,
        "_build_cas_service_url",
        lambda _request: "http://testserver/callback",
    )

    async def verify(_ticket, _service_url):
        return "cas-user", "CAS 用户"

    async def issue(_redis, issued_user, _display_name):
        assert issued_user is user
        return "cas-token"

    async def issue_exchange(_redis, token, issued_user):
        assert token == "cas-token"
        assert issued_user is user
        return "exchange-code"

    monkeypatch.setattr(auth_router, "_verify_cas_ticket", verify)
    monkeypatch.setattr(auth_router, "_issue_auth_token", issue)
    monkeypatch.setattr(auth_router, "_issue_cas_exchange", issue_exchange)

    response = await auth_router.cas_callback(
        _request(), ticket="ticket", session=session, redis_client=object()
    )

    query = parse_qs(urlparse(response.headers["location"]).query)
    assert query["code"] == ["exchange-code"]
    assert session.committed is True


@pytest.mark.asyncio
async def test_cas_callback_rejects_disabled_existing_user(monkeypatch):
    user = User(
        user_id="disabled-1",
        username="disabled",
        password=None,
        email=None,
        api_key="disabled-key",
        role="user",
        is_active=False,
    )
    session = _AsyncSession(user)

    monkeypatch.setattr(auth_router.settings, "cas_enabled", True)
    monkeypatch.setattr(auth_router, "_build_cas_service_url", lambda _request: "http://testserver/callback")

    async def verify(_ticket, _service_url):
        return "disabled", None

    monkeypatch.setattr(auth_router, "_verify_cas_ticket", verify)

    with pytest.raises(HTTPException) as exc_info:
        await auth_router.cas_callback(
            _request(), ticket="ticket", session=session, redis_client=object()
        )

    assert exc_info.value.status_code == 403
    assert session.committed is False
