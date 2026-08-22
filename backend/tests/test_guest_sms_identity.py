"""访客短信登录/注册按手机号收敛为单一身份的回归测试。

修复前两条流程用不同键定位账户且登录建号不写 telephone,导致:
- 先短信登录(telephone 留空)后想注册,被自己的 guest_{phone} 用户名误报占用;
- 先用自定义用户名注册,短信登录只按 guest_{phone} 查,找不到而静默另建一个
  无口令账户,用户登进了与自己数据无关的身份。

同时保留安全边界:短信码绝不能命中非短信来源(CAS/管理员/网页口令)账户。
"""

from __future__ import annotations

from types import SimpleNamespace

import pytest

from backend.api.routers import guest_sms_auth
from backend.api.routers.guest_sms_auth import (
    GuestSmsLoginRequest,
    GuestSmsRegisterRequest,
    guest_sms_login,
    guest_sms_register,
)


class _Result:
    def __init__(self, row):
        self._row = row

    def scalar_one_or_none(self):
        return self._row


class _SeqSession:
    """按调用顺序返回预置 SELECT 结果;UPDATE 结果被业务忽略。"""

    def __init__(self, rows: list):
        self._rows = list(rows)
        self.calls = 0
        self.added: list = []
        self.committed = False

    async def execute(self, _stmt):
        row = self._rows[self.calls] if self.calls < len(self._rows) else None
        self.calls += 1
        return _Result(row)

    def add(self, obj) -> None:
        self.added.append(obj)

    async def flush(self) -> None:
        return None

    async def commit(self) -> None:
        self.committed = True


def _patch_common(monkeypatch) -> None:
    async def _ok_code(*_a, **_k):
        return True, ""

    async def _token(*_a, **_k):
        return "tok-test"

    async def _revoke(*_a, **_k):
        return 1

    monkeypatch.setattr(guest_sms_auth, "_consume_guest_sms_code", _ok_code)
    monkeypatch.setattr(guest_sms_auth, "_issue_auth_token", _token)
    monkeypatch.setattr(guest_sms_auth, "revoke_user_sessions", _revoke)
    monkeypatch.setattr(guest_sms_auth, "_hash_password", lambda _p: "hashed")


def _guest_row(phone: str, **overrides):
    row = SimpleNamespace(
        user_id="u-existing",
        username=guest_sms_auth._guest_username(phone),
        telephone=None,
        password=None,
        api_key="guest_sms_legacy",
        role="user",
        is_active=True,
        display_name=None,
    )
    for key, value in overrides.items():
        setattr(row, key, value)
    return row


def test_is_trusted_guest_sms_user_gates_on_api_key_prefix() -> None:
    phone = "13800000000"
    assert guest_sms_auth._is_trusted_guest_sms_user(_guest_row(phone)) is True
    # 自定义用户名但仍是短信来源账户 → 可信(身份绑定手机号,不绑定用户名)。
    assert (
        guest_sms_auth._is_trusted_guest_sms_user(
            _guest_row(phone, username="alice", password="hashed")
        )
        is True
    )
    # 非短信来源(网页口令/管理员)→ 不可信。
    assert (
        guest_sms_auth._is_trusted_guest_sms_user(
            _guest_row(phone, api_key="pwd_xxx", password="hashed", role="admin")
        )
        is False
    )


@pytest.mark.asyncio
async def test_find_guest_sms_user_prefers_bound_telephone() -> None:
    phone = "13800000001"
    bound = _guest_row(phone, username="alice", telephone=phone)
    session = _SeqSession([bound])
    found = await guest_sms_auth._find_guest_sms_user_by_phone(session, phone)
    assert found is bound
    assert session.calls == 1  # 命中 telephone 后不再查用户名


@pytest.mark.asyncio
async def test_login_creates_user_with_bound_telephone(monkeypatch) -> None:
    _patch_common(monkeypatch)
    phone = "13800000002"
    session = _SeqSession([None, None])  # telephone 无、guest_username 无
    resp = await guest_sms_login(
        GuestSmsLoginRequest(telephone=phone, code="123456"),
        session=session,
        redis_client=object(),
    )
    assert resp["success"] is True
    assert session.committed is True
    assert len(session.added) == 1
    created = session.added[0]
    # 关键:建号即写 telephone,后续注册/登录才能收敛到同一身份。
    assert created.telephone == phone
    assert created.username == guest_sms_auth._guest_username(phone)


@pytest.mark.asyncio
async def test_login_reaches_custom_username_account_without_forking(monkeypatch) -> None:
    _patch_common(monkeypatch)
    phone = "13800000003"
    account = _guest_row(
        phone, username="alice", telephone=phone, password="hashed",
        display_name="Alice",
    )
    session = _SeqSession([account])  # telephone 命中真实账户
    resp = await guest_sms_login(
        GuestSmsLoginRequest(telephone=phone, code="123456"),
        session=session,
        redis_client=object(),
    )
    assert resp["success"] is True
    assert resp["user"]["username"] == "alice"  # 到达真身份,而非 guest_{phone}
    assert session.added == []  # 不再另建第二个账户


@pytest.mark.asyncio
async def test_login_rejects_foreign_account_via_username_fallback(monkeypatch) -> None:
    _patch_common(monkeypatch)
    phone = "13800000004"
    foreign = _guest_row(
        phone, api_key="pwd_admin", password="hashed", role="admin"
    )
    session = _SeqSession([None, foreign])  # telephone 无 → username 命中外来账户
    resp = await guest_sms_login(
        GuestSmsLoginRequest(telephone=phone, code="123456"),
        session=session,
        redis_client=object(),
    )
    assert resp["success"] is False
    assert "无法使用短信登录" in resp["error"]
    assert session.added == []
    assert session.committed is False


@pytest.mark.asyncio
async def test_register_upgrades_legacy_quick_login_row(monkeypatch) -> None:
    _patch_common(monkeypatch)
    phone = "13800000005"
    legacy = _guest_row(phone)  # username=guest_{phone}, telephone=None, 无口令
    # _find: telephone 无 → guest_username 命中 legacy;随后 username_owner 复用 legacy。
    session = _SeqSession([None, legacy, legacy])
    resp = await guest_sms_register(
        GuestSmsRegisterRequest(
            telephone=phone,
            username=None,
            code="123456",
            password="secret1",
            confirm_password="secret1",
            display_name=None,
        ),
        session=session,
        redis_client=object(),
    )
    # 修复前此处会误报「该用户名已被使用」。
    assert resp["success"] is True
    assert legacy.password == "hashed"
    assert legacy.telephone == phone  # 回填手机号,身份收敛
