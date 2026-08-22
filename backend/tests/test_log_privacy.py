from __future__ import annotations

import pytest

from backend.config.log_privacy import text_fingerprint


def test_text_fingerprint_is_stable_without_exposing_source_text():
    sensitive = "患者姓名张三，身份证号 123456789"

    first = text_fingerprint(sensitive)
    second = text_fingerprint(sensitive)

    assert first == second
    assert first.startswith(f"chars={len(sensitive)},sha256=")
    assert sensitive not in first
    assert "张三" not in first
    assert "123456789" not in first


def test_text_fingerprint_distinguishes_content_with_the_same_length():
    assert text_fingerprint("secret-a") != text_fingerprint("secret-b")


def test_sensitive_data_filter_redacts_compound_keys_and_credentials() -> None:
    """过滤器是全库唯一兜底防线：复合 token 键名/Bearer/连接串必须命中。"""
    import logging

    from backend.config.filters import SensitiveDataFilter

    filt = SensitiveDataFilter()

    def _redact(message: str) -> str:
        record = logging.LogRecord(
            "test", logging.INFO, "path", 1, message, None, None
        )
        assert filt.filter(record) is True
        return record.getMessage()

    assert _redact("token=abc123") == "token=***"
    assert _redact("access_token=xyz") == "access_token=***"
    assert _redact("refresh_token=xyz") == "refresh_token=***"
    assert _redact("session_id=42") == "session_id=***"
    assert "eyJ" not in _redact("authorization: Bearer eyJhbGci.abc.def")
    assert _redact("mysql://root:pw@10.0.0.1/db") == "mysql://root:***@10.0.0.1/db"
    assert _redact("redis://:pw@host:6379") == "redis://:***@host:6379"
    # 记账字段不受影响
    assert _redact("total_tokens=100 ok") == "total_tokens=100 ok"
    assert _redact("tokens=12 ok") == "tokens=12 ok"


def test_remote_fetch_presigned_detection(monkeypatch: pytest.MonkeyPatch) -> None:
    from backend.config import settings
    from backend.services.remote_fetch import looks_server_presigned

    monkeypatch.setattr(settings, "minio_endpoint", "minio.example:9000", raising=False)
    # 主机 + 签名参数同时匹配才算服务端自签
    assert looks_server_presigned(
        "http://minio.example:9000/bucket/k?X-Amz-Signature=abc&X-Amz-Expires=600"
    )
    # H1 回归守护：伪造签名参数的任意主机 URL 不得通过豁免
    assert not looks_server_presigned(
        "http://169.254.169.254/latest/?X-Amz-Signature=1"
    )
    assert not looks_server_presigned("https://evil.example/img.jpg?X-Amz-Signature=1")
    assert not looks_server_presigned("https://evil.example/img.jpg")
    assert not looks_server_presigned("https://evil.example/img.jpg?sig=hmac")


@pytest.mark.asyncio
async def test_remote_fetch_rejects_private_targets() -> None:
    from backend.services.remote_fetch import resolve_public_remote_target

    # 内网/环回/链路本地一律拒绝（SSRF 防护核心语义）
    assert await resolve_public_remote_target("http://127.0.0.1/x.png") is None
    assert await resolve_public_remote_target("http://10.0.0.1/x.png") is None
    assert await resolve_public_remote_target("http://169.254.169.254/x") is None
    assert await resolve_public_remote_target("http://[::1]/x") is None
    # 非 http(s) scheme、userinfo、空串拒绝
    assert await resolve_public_remote_target("file:///etc/passwd") is None
    assert await resolve_public_remote_target("http://u:p@example.com/x") is None
    assert await resolve_public_remote_target("") is None
