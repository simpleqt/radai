import pytest
from fastapi import HTTPException
from starlette.requests import Request
from werkzeug.security import generate_password_hash

from backend.api.routers import auth as auth_router
from backend.application.auth_security import session_epoch_key
from backend.db.models import User


class _ScalarResult:
    def __init__(self, value=None):
        self.value = value

    def scalar_one_or_none(self):
        return self.value


class _AsyncSession:
    def __init__(self, existing_email_user=None):
        self.existing_email_user = existing_email_user
        self.committed = False
        self.refreshed = False
        self.executed = []

    async def execute(self, statement):
        self.executed.append(statement)
        return _ScalarResult(self.existing_email_user)

    async def commit(self):
        self.committed = True

    async def refresh(self, _user):
        self.refreshed = True


class _Redis:
    def __init__(self):
        self.deleted = []
        self.values = {}

    async def get(self, key):
        return self.values.get(key)

    async def delete(self, *keys):
        self.deleted.extend(keys)

    async def incr(self, key):
        self.values[key] = int(self.values.get(key, 0)) + 1
        return self.values[key]


def _request(token: str = "settings-token") -> Request:
    return Request(
        {
            "type": "http",
            "method": "POST",
            "path": "/api/v1/auth/me/change-password",
            "query_string": b"",
            "headers": [(b"authorization", f"Bearer {token}".encode())],
            "scheme": "http",
            "server": ("testserver", 80),
            "client": ("127.0.0.1", 12345),
        }
    )


def _user(**kwargs) -> User:
    data = {
        "user_id": "user-1",
        "username": "settings-user",
        "display_name": None,
        "password": generate_password_hash("old-password"),
        "email": "old@example.com",
        "api_key": "api-key",
        "role": "user",
        "is_active": True,
    }
    data.update(kwargs)
    return User(**data)


@pytest.mark.asyncio
async def test_update_me_changes_display_name_and_email():
    user = _user()
    session = _AsyncSession()

    response = await auth_router.update_me(
        auth_router.UserProfileUpdateRequest(
            display_name="  新名称  ",
            email="next@example.com",
        ),
        user=user,
        session=session,
    )

    assert user.display_name == "新名称"
    assert user.email == "next@example.com"
    assert response.user["display_name"] == "新名称"
    assert response.user["email"] == "next@example.com"
    assert session.committed is True
    assert session.refreshed is True


@pytest.mark.asyncio
async def test_update_me_rejects_duplicate_email():
    user = _user()
    session = _AsyncSession(existing_email_user=_user(user_id="other", email="next@example.com"))

    with pytest.raises(HTTPException) as exc_info:
        await auth_router.update_me(
            auth_router.UserProfileUpdateRequest(email="next@example.com"),
            user=user,
            session=session,
        )

    assert exc_info.value.status_code == 409
    assert session.committed is False


@pytest.mark.asyncio
async def test_change_password_deletes_current_token():
    user = _user()
    session = _AsyncSession()
    redis = _Redis()

    response = await auth_router.change_my_password(
        auth_router.ChangePasswordRequest(
            current_password="old-password",
            new_password="new-password",
        ),
        _request("settings-token"),
        user=user,
        session=session,
        redis_client=redis,
    )

    assert response.success is True
    assert session.committed is True
    # 成功改密顺带清空 pwchange 失败锁定键，再撤销当前 token。
    assert redis.deleted[-1] == "auth:token:settings-token"
    assert any(key.startswith("auth:login_rate:pwchange:") for key in redis.deleted)
    assert redis.values[session_epoch_key("user-1")] == 1
    assert session.executed


@pytest.mark.asyncio
async def test_change_password_rejects_cas_user():
    user = _user(password=None)

    with pytest.raises(HTTPException) as exc_info:
        await auth_router.change_my_password(
            auth_router.ChangePasswordRequest(
                current_password="old-password",
                new_password="new-password",
            ),
            _request(),
            user=user,
            session=_AsyncSession(),
            redis_client=_Redis(),
        )

    assert exc_info.value.status_code == 400
