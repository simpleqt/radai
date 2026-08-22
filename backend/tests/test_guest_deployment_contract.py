from __future__ import annotations

import importlib.util
import json
from pathlib import Path

import pytest

PROJECT_ROOT = Path(__file__).resolve().parents[2]
SMOKE_PATH = PROJECT_ROOT / "deploy/local-test/smoke_test.py"
SMOKE_SPEC = importlib.util.spec_from_file_location("local_test_smoke", SMOKE_PATH)
assert SMOKE_SPEC is not None and SMOKE_SPEC.loader is not None
local_test_smoke = importlib.util.module_from_spec(SMOKE_SPEC)
SMOKE_SPEC.loader.exec_module(local_test_smoke)


def test_guest_declares_shared_runtime_dependencies_in_its_own_lockfile() -> None:
    guest_package = json.loads(
        (PROJECT_ROOT / "guest" / "package.json").read_text(encoding="utf-8")
    )
    guest_lock = json.loads(
        (PROJECT_ROOT / "guest" / "package-lock.json").read_text(encoding="utf-8")
    )
    frontend_package = json.loads(
        (PROJECT_ROOT / "frontend" / "package.json").read_text(encoding="utf-8")
    )

    expected_mermaid = frontend_package["dependencies"]["mermaid"]
    assert guest_package["dependencies"]["mermaid"] == expected_mermaid
    assert guest_lock["packages"][""]["dependencies"]["mermaid"] == expected_mermaid
    assert guest_lock["packages"]["node_modules/mermaid"]["version"]


def test_guest_container_installs_platform_optional_dependencies() -> None:
    dockerfile = (PROJECT_ROOT / "guest" / "Dockerfile").read_text(encoding="utf-8")

    assert "npm ci --include=optional" in dockerfile


def test_guest_smoke_requires_and_parses_follow_up_suggestions() -> None:
    payload = b"\n".join(
        (
            b'data: {"type":"chunk","content":"ok"}',
            b'data: {"type":"follow_up_suggestions","suggestions":["a","b","c"]}',
            b"data: [DONE]",
            b"",
        )
    )

    result = local_test_smoke.parse_sse(
        payload,
        guest=True,
        require_follow_up=True,
    )

    assert result["follow_up_suggestion_count"] == 3
    assert "follow_up_suggestions" in result["event_types"]


def test_smoke_model_catalog_accepts_only_chat_scoped_models() -> None:
    result = local_test_smoke.validate_model_catalog(
        {
            "default_model": "chat-default",
            "models": [
                {"id": "chat-default", "name": "Chat Default"},
                {"id": "chat-secondary", "name": "Chat Secondary"},
            ],
        },
        selected_model="chat-default",
        active_chat_models={"chat-default", "chat-secondary"},
        active_non_chat_models={"image-analysis", "embedding"},
    )

    assert result["selected_model_present"] is True
    assert result["active_non_chat_models_excluded"] == 2


def test_smoke_model_catalog_rejects_non_chat_model_leak() -> None:
    with pytest.raises(local_test_smoke.SmokeError, match="outside the chat scope"):
        local_test_smoke.validate_model_catalog(
            {
                "default_model": "chat-default",
                "models": [
                    {"id": "chat-default"},
                    {"id": "image-analysis"},
                ],
            },
            selected_model="chat-default",
            active_chat_models={"chat-default"},
            active_non_chat_models={"image-analysis"},
        )


def test_guest_query_agent_code_whitelist(monkeypatch):
    """访客 agent_code 必须收敛到白名单（默认覆盖三公开站点，可用 env 收紧）。"""
    import pytest
    from fastapi import HTTPException

    from backend.api.routers import guest_agent

    monkeypatch.delenv("GUEST_ALLOWED_AGENT_CODES", raising=False)
    # 内部审计 profile 不得对访客开放（ownerless 知识库攻击链）
    with pytest.raises(HTTPException) as exc_info:
        guest_agent._assert_guest_agent_allowed("audit")
    assert exc_info.value.status_code == 403
    # 三个公开站点的匿名入口（各自 DEFAULT_AGENT_CODE）默认放行
    for site_code in ("ne", "csh", "fmca"):
        guest_agent._assert_guest_agent_allowed(site_code)
    # 白名单比较大小写不敏感（env 填大写不静默 403）
    guest_agent._assert_guest_agent_allowed("NE")

    monkeypatch.setenv("GUEST_ALLOWED_AGENT_CODES", "ne, fmca")
    guest_agent._assert_guest_agent_allowed("fmca")
    with pytest.raises(HTTPException):
        guest_agent._assert_guest_agent_allowed("audit")


def test_guest_device_scope_secret_pinned_to_minio(monkeypatch):
    """设备作用域密钥必须钉死在 minio_secret_key（与 URL 签名链解耦）。

    internal_api_secret 与 minio_secret_key 不同时，作用域哈希不能漂移，
    否则匿名访客的全部历史目录行不可达。
    """
    from backend.api.routers import guest_agent
    from backend.config.settings import settings

    monkeypatch.delenv("GUEST_DEVICE_SCOPE_SECRET", raising=False)
    monkeypatch.setattr(
        guest_agent.app_settings, "minio_secret_key", "scope-secret-A", raising=False
    )
    monkeypatch.setattr(
        settings, "internal_api_secret", "different-internal-secret", raising=False
    )
    scope = guest_agent._device_scope_id("gdev_123456789012_abcdefghijklmnop")
    # 与 2f5f216 之前的原始派生（hmac(minio_secret_key, device)）逐字节一致
    import hashlib
    import hmac as hmac_mod

    expected = hmac_mod.new(
        b"scope-secret-A", b"gdev_123456789012_abcdefghijklmnop", hashlib.sha256
    ).hexdigest()[:32]
    assert scope == expected

    # 显式 GUEST_DEVICE_SCOPE_SECRET 优先（轮换逃生口）
    monkeypatch.setenv("GUEST_DEVICE_SCOPE_SECRET", "rotated-scope-secret")
    assert guest_agent._device_scope_id("gdev_123456789012_abcdefghijklmnop") != scope


def test_guest_auth_role_derivation():
    """匿名访客必须派生 auth_role=guest（写工具守卫前提）。"""
    from backend.api.routers.guest_agent import guest_auth_role

    assert guest_auth_role(False) == "guest"
    assert guest_auth_role(True) == "user"


def test_guest_principal_role_fallback():
    """agent_service 兜底：guest_* 主体即便漏传 auth_role 也归一为 guest。"""
    from backend.application.agent_service import AgentService

    derived = AgentService._derive_auth_role_for_request(
        user_id="guest_abc", auth_role=None
    )
    assert derived == "guest"
    assert (
        AgentService._derive_auth_role_for_request(
            user_id="u123", auth_role=None
        )
        == "user"
    )
