import asyncio
import json

import pytest
from starlette.requests import Request

from backend.api.routers import auth_sms
from backend.db.models import User


class FakeRedis:
    def __init__(self):
        self.store = {}
        self.quota_counts = {}

    async def get(self, key):
        return self.store.get(key)

    async def setex(self, key, ttl, value):
        del ttl
        self.store[key] = value
        return True

    async def set(self, key, value, *, ex=None, nx=False):
        del ex
        if nx and key in self.store:
            return False
        self.store[key] = value
        return True

    async def delete(self, *keys):
        deleted = 0
        for key in keys:
            if key in self.store:
                deleted += 1
                self.store.pop(key, None)
        return deleted

    async def eval(self, script, number_of_keys, *args):
        del number_of_keys
        if 'redis.call("get", KEYS[1]) == ARGV[1]' in script:
            # 属主比较删除（SMS 发送锁释放）
            key, owner = str(args[0]), str(args[1])
            if self.store.get(key) == owner:
                self.store.pop(key, None)
                return 1
            return 0
        if "auth-captcha-v1" in script:
            key, submitted = str(args[0]), str(args[1])
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
                return [0, 37]
            self.quota_counts[key] = count + 1
            return [1, 0]

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

        raise AssertionError("unexpected Lua script")


class _ScalarResult:
    def __init__(self, value):
        self._value = value

    def scalars(self):
        return self

    def first(self):
        return self._value


class FakeSession:
    def __init__(self, users: list[User] | None = None):
        self.users = list(users or [])
        self.added: list[User] = []
        self.committed = False

    async def execute(self, statement):
        try:
            params = dict(statement.compile().params)
        except Exception:
            params = {}
        values = {str(v) for v in params.values() if v is not None}

        for user in self.users:
            telephone = str(getattr(user, "telephone", "") or "")
            username = str(getattr(user, "username", "") or "")
            if telephone and telephone in values:
                return _ScalarResult(user)
            if username and username in values:
                return _ScalarResult(user)
        return _ScalarResult(None)

    def add(self, user: User):
        self.added.append(user)
        self.users.append(user)

    async def commit(self):
        self.committed = True


@pytest.fixture
def fake_redis():
    return FakeRedis()


@pytest.fixture
def empty_session():
    return FakeSession()


def _request(ip: str = "203.0.113.10") -> Request:
    return Request(
        {
            "type": "http",
            "method": "POST",
            "path": "/api/send-sms-code",
            "headers": [],
            "query_string": b"",
            "client": (ip, 12345),
            "server": ("testserver", 80),
            "scheme": "http",
        }
    )


def _response_json(response):
    return json.loads(response.body) if hasattr(response, "body") else response


async def _seed_captcha(
    redis_client: FakeRedis,
    captcha_id: str = "captcha-ok",
    answer: str = "12",
) -> str:
    await redis_client.setex(
        auth_sms._captcha_key(captcha_id),
        auth_sms.CAPTCHA_TTL_SECONDS,
        auth_sms.json.dumps({"answer": answer, "attempts": 0}),
    )
    return captcha_id


def _school_user(phone: str = "13800138000", username: str = "alice") -> User:
    return User(
        user_id="u-school-1",
        username=username,
        telephone=phone,
        password="hashed",
        api_key="sms_test-key",
        role="user",
        is_active=True,
    )


@pytest.mark.asyncio
async def test_send_sms_requires_captcha(monkeypatch, fake_redis, empty_session):
    async def unexpected_post(*args, **kwargs):
        raise AssertionError("短信网关不应在验证码缺失时被调用")

    class Client:
        async def __aenter__(self):
            return self

        async def __aexit__(self, *args):
            return False

        post = unexpected_post

    monkeypatch.setattr(auth_sms.httpx, "AsyncClient", lambda *args, **kwargs: Client())

    response = await auth_sms.send_sms_code(
        auth_sms.SendSmsRequest(telephone="13800138000", type="register"),
        _request(),
        redis_client=fake_redis,
        session=empty_session,
    )

    assert response["success"] is False
    assert "人机验证" in response["error"]


@pytest.mark.asyncio
async def test_send_sms_rejects_wrong_captcha(monkeypatch, fake_redis, empty_session):
    captcha_id = await _seed_captcha(fake_redis, answer="9")

    class Client:
        async def __aenter__(self):
            return self

        async def __aexit__(self, *args):
            return False

        async def post(self, *args, **kwargs):
            raise AssertionError("短信网关不应在验证码错误时被调用")

    monkeypatch.setattr(auth_sms.httpx, "AsyncClient", lambda *args, **kwargs: Client())

    response = await auth_sms.send_sms_code(
        auth_sms.SendSmsRequest(
            telephone="13800138000",
            type="register",
            captcha_id=captcha_id,
            captcha_answer="8",
        ),
        _request(),
        redis_client=fake_redis,
        session=empty_session,
    )

    assert response["success"] is False
    assert "人机验证" in response["error"]


@pytest.mark.asyncio
async def test_send_sms_respects_redis_send_lock(monkeypatch, fake_redis, empty_session):
    captcha_id = await _seed_captcha(fake_redis, answer="7")
    await fake_redis.setex(auth_sms._sms_send_lock_key("13800138000"), 60, "1")

    class Client:
        async def __aenter__(self):
            return self

        async def __aexit__(self, *args):
            return False

        async def post(self, *args, **kwargs):
            raise AssertionError("发送锁存在时不应调用短信网关")

    monkeypatch.setattr(auth_sms.httpx, "AsyncClient", lambda *args, **kwargs: Client())

    response = await auth_sms.send_sms_code(
        auth_sms.SendSmsRequest(
            telephone="13800138000",
            type="register",
            captcha_id=captcha_id,
            captcha_answer="7",
        ),
        _request(),
        redis_client=fake_redis,
        session=empty_session,
    )

    body = _response_json(response)
    assert response.status_code == 429
    assert response.headers["retry-after"] == str(auth_sms.SMS_RESEND_SECONDS)
    assert body["success"] is False
    assert "过于频繁" in body["error"]


@pytest.mark.asyncio
async def test_send_sms_accepts_valid_captcha(monkeypatch, fake_redis):
    captcha_id = await _seed_captcha(fake_redis, answer="7")
    calls = []
    monkeypatch.setenv("SMS_SEND_MESSAGE_API_URL", "https://sms.example.edu/send")
    monkeypatch.setenv("SMS_APP_ID", "school-app")
    monkeypatch.setenv("SMS_APP_SECRET", "school-secret")
    session = FakeSession([_school_user()])

    class Response:
        def json(self):
            return {"code": 200}

    class Client:
        async def __aenter__(self):
            return self

        async def __aexit__(self, *args):
            return False

        async def post(self, *args, **kwargs):
            calls.append({"args": args, "kwargs": kwargs})
            return Response()

    monkeypatch.setattr(auth_sms.httpx, "AsyncClient", lambda *args, **kwargs: Client())

    response = await auth_sms.send_sms_code(
        auth_sms.SendSmsRequest(
            telephone="13800138000",
            type="forgot",
            captcha_id=captcha_id,
            captcha_answer="7",
        ),
        _request(),
        redis_client=fake_redis,
        session=session,
    )

    assert response["success"] is True
    assert calls
    assert calls[0]["args"][0] == "https://sms.example.edu/send"
    assert calls[0]["kwargs"]["headers"] == {
        "appId": "school-app",
        "token": "school-secret",
    }
    assert auth_sms._captcha_key(captcha_id) not in fake_redis.store
    assert auth_sms._sms_code_key("forgot", "13800138000") in fake_redis.store
    assert auth_sms._sms_send_lock_key("13800138000") in fake_redis.store


@pytest.mark.asyncio
async def test_send_sms_register_rejects_existing_phone(
    monkeypatch, fake_redis
):
    captcha_id = await _seed_captcha(fake_redis, answer="7")
    session = FakeSession([_school_user()])

    class Client:
        async def __aenter__(self):
            return self

        async def __aexit__(self, *args):
            return False

        async def post(self, *args, **kwargs):
            raise AssertionError("已注册手机号不应调用短信网关")

    monkeypatch.setattr(auth_sms.httpx, "AsyncClient", lambda *args, **kwargs: Client())

    response = await auth_sms.send_sms_code(
        auth_sms.SendSmsRequest(
            telephone="13800138000",
            type="register",
            captcha_id=captcha_id,
            captcha_answer="7",
        ),
        _request(),
        redis_client=fake_redis,
        session=session,
    )

    assert response["success"] is False
    assert response["error"] == auth_sms.PHONE_ALREADY_REGISTERED_MESSAGE
    assert response.get("refresh_captcha") is True
    assert auth_sms._sms_send_lock_key("13800138000") not in fake_redis.store


@pytest.mark.asyncio
async def test_send_sms_forgot_rejects_unregistered_phone(
    monkeypatch, fake_redis, empty_session
):
    captcha_id = await _seed_captcha(fake_redis, answer="7")

    class Client:
        async def __aenter__(self):
            return self

        async def __aexit__(self, *args):
            return False

        async def post(self, *args, **kwargs):
            raise AssertionError("未注册手机号不应调用短信网关")

    monkeypatch.setattr(auth_sms.httpx, "AsyncClient", lambda *args, **kwargs: Client())

    response = await auth_sms.send_sms_code(
        auth_sms.SendSmsRequest(
            telephone="13800138000",
            type="forgot",
            captcha_id=captcha_id,
            captcha_answer="7",
        ),
        _request(),
        redis_client=fake_redis,
        session=empty_session,
    )

    assert response["success"] is False
    assert response["error"] == auth_sms.PHONE_NOT_REGISTERED_MESSAGE
    assert response.get("refresh_captcha") is True


@pytest.mark.asyncio
async def test_send_sms_reports_missing_school_gateway_config(
    monkeypatch, fake_redis, empty_session
):
    captcha_id = await _seed_captcha(fake_redis, answer="7")
    for key in auth_sms.SCHOOL_SMS_CONFIG_KEYS:
        monkeypatch.delenv(key, raising=False)

    class Client:
        async def __aenter__(self):
            return self

        async def __aexit__(self, *args):
            return False

        async def post(self, *args, **kwargs):
            raise AssertionError("学校短信网关未配置时不应发起请求")

    monkeypatch.setattr(auth_sms.httpx, "AsyncClient", lambda *args, **kwargs: Client())

    response = await auth_sms.send_sms_code(
        auth_sms.SendSmsRequest(
            telephone="13800138000",
            type="register",
            captcha_id=captcha_id,
            captcha_answer="7",
        ),
        _request(),
        redis_client=fake_redis,
        session=empty_session,
    )

    assert response["success"] is False
    assert response["error"] == auth_sms.SCHOOL_SMS_FAILURE_MESSAGE
    assert response.get("refresh_captcha") is True
    assert auth_sms._sms_send_lock_key("13800138000") not in fake_redis.store


@pytest.mark.asyncio
async def test_captcha_can_only_be_consumed_once_under_concurrency(fake_redis):
    captcha_id = await _seed_captcha(fake_redis, answer="11")

    results = await asyncio.gather(
        auth_sms._verify_sms_captcha(fake_redis, captcha_id, "11"),
        auth_sms._verify_sms_captcha(fake_redis, captcha_id, "11"),
    )

    assert sum(1 for valid, _ in results if valid) == 1
    assert auth_sms._captcha_key(captcha_id) not in fake_redis.store


@pytest.mark.asyncio
async def test_ip_quota_limits_sends_across_different_phone_numbers(
    monkeypatch, fake_redis, empty_session
):
    monkeypatch.setattr(auth_sms, "SMS_IP_RATE_MAX_REQUESTS", 2)
    monkeypatch.setenv("SMS_SEND_MESSAGE_API_URL", "https://sms.example.edu/send")
    monkeypatch.setenv("SMS_APP_ID", "school-app")
    monkeypatch.setenv("SMS_APP_SECRET", "school-secret")
    gateway_calls = 0

    class Response:
        status_code = 200

        def json(self):
            return {"code": 200}

    class Client:
        async def __aenter__(self):
            return self

        async def __aexit__(self, *args):
            return False

        async def post(self, *args, **kwargs):
            nonlocal gateway_calls
            gateway_calls += 1
            return Response()

    monkeypatch.setattr(auth_sms.httpx, "AsyncClient", lambda *args, **kwargs: Client())
    request = _request("198.51.100.8")

    for index, phone in enumerate(
        ("13800138000", "13800138001", "13800138002"), start=1
    ):
        captcha_id = await _seed_captcha(
            fake_redis,
            captcha_id=f"quota-captcha-{index}",
            answer="7",
        )
        response = await auth_sms.send_sms_code(
            auth_sms.SendSmsRequest(
                telephone=phone,
                type="register",
                captcha_id=captcha_id,
                captcha_answer="7",
            ),
            request,
            redis_client=fake_redis,
            session=empty_session,
        )
        if index < 3:
            assert response["success"] is True
        else:
            body = _response_json(response)
            assert response.status_code == 429
            assert response.headers["retry-after"] == "37"
            assert body == {
                "success": False,
                "error": auth_sms.SMS_RATE_LIMIT_MESSAGE,
                "retry_after": 37,
            }

    assert gateway_calls == 2


@pytest.mark.asyncio
async def test_school_gateway_error_details_are_not_returned(
    monkeypatch, fake_redis, empty_session
):
    captcha_id = await _seed_captcha(fake_redis, answer="7")
    monkeypatch.setenv("SMS_SEND_MESSAGE_API_URL", "https://sms.example.edu/send")
    monkeypatch.setenv("SMS_APP_ID", "school-app")
    monkeypatch.setenv("SMS_APP_SECRET", "school-secret")
    private_detail = "upstream database password leaked"

    class Response:
        status_code = 500

        def json(self):
            return {"code": 500, "message": private_detail}

    class Client:
        async def __aenter__(self):
            return self

        async def __aexit__(self, *args):
            return False

        async def post(self, *args, **kwargs):
            return Response()

    monkeypatch.setattr(auth_sms.httpx, "AsyncClient", lambda *args, **kwargs: Client())

    response = await auth_sms.send_sms_code(
        auth_sms.SendSmsRequest(
            telephone="13800138000",
            type="register",
            captcha_id=captcha_id,
            captcha_answer="7",
        ),
        _request(),
        redis_client=fake_redis,
        session=empty_session,
    )

    assert response == {
        "success": False,
        "error": auth_sms.SCHOOL_SMS_FAILURE_MESSAGE,
        "refresh_captcha": True,
    }
    assert private_detail not in response["error"]


@pytest.mark.asyncio
async def test_register_binds_telephone(fake_redis, empty_session, monkeypatch):
    phone = "13800138000"
    await fake_redis.setex(
        auth_sms._sms_code_key("register", phone),
        300,
        json.dumps({"code": "123456", "attempts": 0}),
    )

    async def fake_issue(_redis, _user, _username):
        return "token-school-1"

    monkeypatch.setattr(auth_sms, "_issue_auth_token", fake_issue)

    response = await auth_sms.register(
        auth_sms.RegisterRequest(
            telephone=phone,
            code="123456",
            username="alice",
            password="secret1",
            confirmPassword="secret1",
        ),
        session=empty_session,
        redis_client=fake_redis,
    )

    assert response["success"] is True
    assert empty_session.committed is True
    assert len(empty_session.added) == 1
    assert empty_session.added[0].telephone == phone
    assert empty_session.added[0].username == "alice"
    assert str(empty_session.added[0].api_key).startswith("sms_")


@pytest.mark.asyncio
async def test_register_rejects_existing_telephone(fake_redis, monkeypatch):
    phone = "13800138000"
    await fake_redis.setex(
        auth_sms._sms_code_key("register", phone),
        300,
        json.dumps({"code": "123456", "attempts": 0}),
    )
    session = FakeSession([_school_user(phone=phone)])

    async def unexpected_issue(*args, **kwargs):
        raise AssertionError("已注册手机号不应发 token")

    monkeypatch.setattr(auth_sms, "_issue_auth_token", unexpected_issue)

    response = await auth_sms.register(
        auth_sms.RegisterRequest(
            telephone=phone,
            code="123456",
            username="bob",
            password="secret1",
            confirmPassword="secret1",
        ),
        session=session,
        redis_client=fake_redis,
    )

    assert response["success"] is False
    assert response["error"] == auth_sms.PHONE_ALREADY_REGISTERED_MESSAGE
    assert session.committed is False


@pytest.mark.asyncio
async def test_forgot_password_finds_user_by_telephone(fake_redis, monkeypatch):
    phone = "13800138000"
    user = _school_user(phone=phone, username="alice")
    session = FakeSession([user])
    await fake_redis.setex(
        auth_sms._sms_code_key("forgot", phone),
        300,
        json.dumps({"code": "654321", "attempts": 0}),
    )

    async def fake_revoke(_redis, _user_id):
        return None

    monkeypatch.setattr(auth_sms, "revoke_user_sessions", fake_revoke)

    response = await auth_sms.forgot_password(
        auth_sms.ForgotPasswordRequest(
            telephone=phone,
            code="654321",
            newPassword="newpass1",
            confirmPassword="newpass1",
        ),
        session=session,
        redis_client=fake_redis,
    )

    assert response["success"] is True
    assert session.committed is True
    assert user.password != "hashed"


@pytest.mark.asyncio
async def test_send_sms_login_rejects_unregistered_phone(
    monkeypatch, fake_redis, empty_session
):
    captcha_id = await _seed_captcha(fake_redis, answer="7")

    class Client:
        async def __aenter__(self):
            return self

        async def __aexit__(self, *args):
            return False

        async def post(self, *args, **kwargs):
            raise AssertionError("未注册手机号登录不应调用短信网关")

    monkeypatch.setattr(auth_sms.httpx, "AsyncClient", lambda *args, **kwargs: Client())

    response = await auth_sms.send_sms_code(
        auth_sms.SendSmsRequest(
            telephone="13800138000",
            type="login",
            captcha_id=captcha_id,
            captcha_answer="7",
        ),
        _request(),
        redis_client=fake_redis,
        session=empty_session,
    )

    assert response["success"] is False
    assert response["error"] == auth_sms.PHONE_NOT_REGISTERED_MESSAGE


@pytest.mark.asyncio
async def test_send_sms_login_accepts_registered_phone(monkeypatch, fake_redis):
    captcha_id = await _seed_captcha(fake_redis, answer="7")
    session = FakeSession([_school_user()])
    monkeypatch.setenv("SMS_SEND_MESSAGE_API_URL", "https://sms.example.edu/send")
    monkeypatch.setenv("SMS_APP_ID", "school-app")
    monkeypatch.setenv("SMS_APP_SECRET", "school-secret")
    calls = []

    class Response:
        def json(self):
            return {"code": 200}

    class Client:
        async def __aenter__(self):
            return self

        async def __aexit__(self, *args):
            return False

        async def post(self, *args, **kwargs):
            calls.append(1)
            return Response()

    monkeypatch.setattr(auth_sms.httpx, "AsyncClient", lambda *args, **kwargs: Client())

    response = await auth_sms.send_sms_code(
        auth_sms.SendSmsRequest(
            telephone="13800138000",
            type="login",
            captcha_id=captcha_id,
            captcha_answer="7",
        ),
        _request(),
        redis_client=fake_redis,
        session=session,
    )

    assert response["success"] is True
    assert calls
    assert auth_sms._sms_code_key("login", "13800138000") in fake_redis.store


@pytest.mark.asyncio
async def test_sms_login_issues_token(fake_redis, monkeypatch):
    phone = "13800138000"
    user = _school_user(phone=phone, username="alice")
    session = FakeSession([user])
    await fake_redis.setex(
        auth_sms._sms_code_key("login", phone),
        300,
        json.dumps({"code": "112233", "attempts": 0}),
    )

    async def fake_issue(_redis, _user, _display_name):
        return "token-sms-login"

    monkeypatch.setattr(auth_sms, "_issue_auth_token", fake_issue)

    response = await auth_sms.sms_login(
        auth_sms.SmsLoginRequest(telephone=phone, code="112233"),
        session=session,
        redis_client=fake_redis,
    )

    assert response["success"] is True
    assert response["token"] == "token-sms-login"
    assert response["user"]["username"] == "alice"
    assert session.committed is True


@pytest.mark.asyncio
async def test_sms_login_allows_guest_sms_user(fake_redis, monkeypatch):
    phone = "18881048355"
    user = User(
        user_id="u-guest-1",
        username=f"guest_{phone}",
        telephone=None,
        password=None,
        api_key="guest_sms_test-key",
        role="user",
        is_active=True,
    )
    session = FakeSession([user])
    await fake_redis.setex(
        auth_sms._sms_code_key("login", phone),
        300,
        json.dumps({"code": "445566", "attempts": 0}),
    )

    async def fake_issue(_redis, _user, _display_name):
        return "token-guest-unified"

    monkeypatch.setattr(auth_sms, "_issue_auth_token", fake_issue)

    response = await auth_sms.sms_login(
        auth_sms.SmsLoginRequest(telephone=phone, code="445566"),
        session=session,
        redis_client=fake_redis,
    )

    assert response["success"] is True
    assert response["token"] == "token-guest-unified"
    assert user.telephone == phone
    assert session.committed is True
