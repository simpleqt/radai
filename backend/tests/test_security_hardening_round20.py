"""Round-20 security fixes: trusted client IP, MCP nesting, links, CAS XML.

Each test pins an invariant that an audit found violated, so a regression is a
test failure rather than a silent widening of an external attack surface.
"""

from __future__ import annotations

import pytest
from starlette.requests import Request

from backend.api.routers import auth_cas
from backend.application.artifacts.artifact_utils import (
    build_minio_artifact_download_url,
)
from backend.application.mcp.query_service import invoke_mcp_tool
from backend.config.runtime_security import public_base_url
from backend.core.utils.client_ip import (
    UNKNOWN_CLIENT_IP,
    forwarded_allow_ips,
    resolve_client_ip,
)


def _request(
    *,
    peer: str | None = "203.0.113.10",
    forwarded: str | None = None,
    host: str = "attacker.example",
) -> Request:
    headers = [(b"host", host.encode())]
    if forwarded is not None:
        headers.append((b"x-forwarded-for", forwarded.encode()))
    scope = {
        "type": "http",
        "method": "GET",
        "path": "/api/v1/probe",
        "query_string": b"",
        "headers": headers,
        "scheme": "http",
        "server": (host, 80),
    }
    if peer is not None:
        scope["client"] = (peer, 4321)
    return Request(scope)


# ── 可信客户端 IP ──────────────────────────────────────────────────────


def test_forwarded_header_from_untrusted_peer_is_ignored():
    """A direct caller must not be able to choose its own rate-limit identity."""

    request = _request(peer="203.0.113.10", forwarded="10.9.9.9, 198.51.100.7")
    assert resolve_client_ip(request) == "203.0.113.10"


def test_forwarded_header_from_trusted_proxy_resolves_real_client():
    request = _request(peer="127.0.0.1", forwarded="198.51.100.7")
    assert resolve_client_ip(request) == "198.51.100.7"


def test_client_supplied_prefix_cannot_impersonate_through_trusted_proxy():
    # The right-most untrusted address is the one our proxy actually saw.
    request = _request(peer="127.0.0.1", forwarded="1.2.3.4, 198.51.100.7")
    assert resolve_client_ip(request) == "198.51.100.7"


def test_trusted_proxy_chain_is_walked_past(monkeypatch):
    monkeypatch.setenv("HTTP_TRUSTED_PROXIES", "127.0.0.0/8,10.0.0.0/8")
    request = _request(peer="10.0.0.5", forwarded="198.51.100.7, 10.0.0.9")
    assert resolve_client_ip(request) == "198.51.100.7"


def test_trusted_proxy_without_forwarded_header_keeps_peer():
    assert resolve_client_ip(_request(peer="127.0.0.1")) == "127.0.0.1"


def test_missing_client_is_reported_as_unknown():
    assert resolve_client_ip(_request(peer=None)) == UNKNOWN_CLIENT_IP


def test_malformed_forwarded_entries_are_skipped():
    request = _request(peer="127.0.0.1", forwarded="not-an-ip, 198.51.100.7, junk")
    assert resolve_client_ip(request) == "198.51.100.7"


def test_forwarded_allow_ips_defaults_to_loopback_and_follows_config(monkeypatch):
    monkeypatch.delenv("HTTP_TRUSTED_PROXIES", raising=False)
    assert forwarded_allow_ips() == "127.0.0.0/8,::1/128"
    monkeypatch.setenv("HTTP_TRUSTED_PROXIES", "10.1.2.3")
    assert forwarded_allow_ips() == "10.1.2.3"


def test_api_processes_pin_forwarded_allow_ips():
    """Both HTTP entry points must pass the trust list to the server."""

    from pathlib import Path

    for module in ("backend/api/http_server.py", "backend/cli.py"):
        source = Path(module).read_text(encoding="utf-8")
        assert "forwarded_allow_ips()" in source, module


# ── MCP 嵌套工具授权 ──────────────────────────────────────────────────


@pytest.mark.asyncio
async def test_invoke_mcp_tool_rejects_tool_outside_key_allowlist():
    with pytest.raises(PermissionError):
        await invoke_mcp_tool(
            "execute_python",
            {},
            user_id="mcp-user",
            allowed_tools=("query_knowledge_base",),
        )


@pytest.mark.asyncio
async def test_invoke_mcp_tool_allows_tool_inside_allowlist(monkeypatch):
    """An allowlisted name must still pass the catalog/runtime boundary."""

    # The allowlist check runs before any catalog work, so reaching the catalog
    # (and failing there for an unknown tool) proves the allowlist admitted it.
    with pytest.raises((PermissionError, ValueError, RuntimeError)) as error:
        await invoke_mcp_tool(
            "definitely_not_registered",
            {},
            user_id="mcp-user",
            allowed_tools=("definitely_not_registered",),
        )
    assert "无权调用 MCP 工具" not in str(error.value)


@pytest.mark.asyncio
async def test_mcp_proxy_call_tool_denies_unlisted_nested_tool(monkeypatch):
    import mcp.types as mt

    from backend.application.auth_service import ResolvedAuth
    from backend.application.mcp import fastmcp_app

    middleware = fastmcp_app.McpToolAllowlistMiddleware()
    auth = ResolvedAuth(
        api_key="key",
        user_id="mcp-user",
        source="api_key",
        mcp_allowed_tools=("call_tool",),
    )
    monkeypatch.setattr(fastmcp_app, "_request_auth", lambda _context: auth)

    class _Context:
        def __init__(self, message):
            self.message = message
            self.fastmcp_context = None

    async def _call_next(_context):  # pragma: no cover - must not be reached
        raise AssertionError("nested tool call must be rejected before dispatch")

    context = _Context(
        mt.CallToolRequestParams(
            name="call_tool",
            arguments={"tool_name": "execute_python", "arguments": {}},
        )
    )
    with pytest.raises(Exception) as error:
        await middleware.on_call_tool(context, _call_next)
    assert "execute_python" in str(error.value)


@pytest.mark.asyncio
async def test_mcp_proxy_call_tool_allows_listed_nested_tool(monkeypatch):
    import mcp.types as mt

    from backend.application.auth_service import ResolvedAuth
    from backend.application.mcp import fastmcp_app

    middleware = fastmcp_app.McpToolAllowlistMiddleware()
    auth = ResolvedAuth(
        api_key="key",
        user_id="mcp-user",
        source="api_key",
        mcp_allowed_tools=("call_tool", "multi_kb_search"),
    )
    monkeypatch.setattr(fastmcp_app, "_request_auth", lambda _context: auth)

    class _Context:
        def __init__(self, message):
            self.message = message
            self.fastmcp_context = None

    dispatched: list[str] = []

    async def _call_next(context):
        dispatched.append(context.message.name)
        return "ok"

    context = _Context(
        mt.CallToolRequestParams(
            name="call_tool",
            arguments={"tool_name": "multi_kb_search", "arguments": {}},
        )
    )
    assert await middleware.on_call_tool(context, _call_next) == "ok"
    assert dispatched == ["call_tool"]


def test_http_mcp_route_forwards_key_allowlist():
    from pathlib import Path

    source = Path("backend/api/routers/mcp.py").read_text(encoding="utf-8")
    assert "allowed_tools=auth.mcp_allowed_tools" in source


# ── 外发链接与下载响应 ────────────────────────────────────────────────


def test_public_base_url_prefers_configuration_over_host_header(monkeypatch):
    monkeypatch.setenv("PUBLIC_BASE_URL", "https://radai2.cdut.edu.cn/")
    assert public_base_url(_request()) == "https://radai2.cdut.edu.cn"


def test_public_base_url_falls_back_to_request_origin_when_unset(monkeypatch):
    monkeypatch.delenv("PUBLIC_BASE_URL", raising=False)
    assert public_base_url(_request(host="dev.local")) == "http://dev.local"


def test_signed_link_builders_use_public_base_url():
    from pathlib import Path

    for module in (
        "backend/api/routers/chat_files.py",
        "backend/api/routers/my_files.py",
    ):
        source = Path(module).read_text(encoding="utf-8")
        assert "public_base_url(request)" in source, module
        assert "str(request.base_url)" not in source, module


def test_artifact_download_url_carries_no_caller_content_type():
    url = build_minio_artifact_download_url(
        minio_object="users/u1/threads/t1/html-ppt/a.html",
        filename="a.html",
        mime_type="text/html",
    )
    assert "mime_type" not in url


def test_artifact_media_type_comes_from_stored_object():
    from backend.api.routers.files import _artifact_media_type

    assert _artifact_media_type("users/u1/threads/t1/report.pdf") == "application/pdf"
    assert _artifact_media_type("users/u1/threads/t1/deck.html") == (
        "application/octet-stream"
    )
    assert _artifact_media_type("users/u1/threads/t1/plot.png") == "image/png"


def test_artifact_download_endpoint_rejects_caller_supplied_mime_type():
    import inspect

    from backend.api.routers.files import download_minio_artifact

    assert "mime_type" not in inspect.signature(download_minio_artifact).parameters


# ── CAS 票据响应解析 ──────────────────────────────────────────────────


def test_cas_response_with_doctype_is_rejected():
    entity_bomb = (
        '<?xml version="1.0"?><!DOCTYPE lolz [<!ENTITY lol "lol">'
        '<!ENTITY lol2 "&lol;&lol;&lol;">]>'
        "<serviceResponse><user>&lol2;</user></serviceResponse>"
    )
    success, username, reason, _attributes = auth_cas._parse_cas_validate_response(
        entity_bomb
    )
    assert success is False
    assert username is None
    assert "文档类型声明" in str(reason)


def test_cas_response_with_external_entity_is_rejected():
    xxe = (
        '<?xml version="1.0"?><!DOCTYPE r [<!ENTITY x SYSTEM "file:///etc/passwd">]>'
        "<serviceResponse><user>&x;</user></serviceResponse>"
    )
    success, _username, reason, _attributes = auth_cas._parse_cas_validate_response(xxe)
    assert success is False
    assert "文档类型声明" in str(reason)


def test_cas_response_larger_than_limit_is_rejected():
    oversized = "<serviceResponse>" + ("x" * auth_cas.CAS_VALIDATE_RESPONSE_MAX_CHARS)
    success, _username, reason, _attributes = auth_cas._parse_cas_validate_response(
        oversized
    )
    assert success is False
    assert "过大" in str(reason)


def test_cas_success_response_still_parses_with_prolog_and_comment():
    xml = (
        '<?xml version="1.0" encoding="UTF-8"?>\n<!-- issued by cas -->\n'
        "<cas:serviceResponse xmlns:cas='http://www.yale.edu/tp/cas'>"
        "<cas:authenticationSuccess><cas:user>alice</cas:user>"
        "<cas:attributes><cas:name>Alice</cas:name></cas:attributes>"
        "</cas:authenticationSuccess></cas:serviceResponse>"
    )
    success, username, reason, attributes = auth_cas._parse_cas_validate_response(xml)
    assert (success, username, reason) == (True, "alice", None)
    assert attributes["name"] == "Alice"


def test_cas_doctype_inside_element_text_is_not_rejected():
    xml = (
        "<serviceResponse><authenticationSuccess><user>carol</user>"
        "<attributes><note>see &lt;!DOCTYPE&gt; docs</note></attributes>"
        "</authenticationSuccess></serviceResponse>"
    )
    success, username, _reason, attributes = auth_cas._parse_cas_validate_response(xml)
    assert (success, username) == (True, "carol")
    assert attributes["note"] == "see <!DOCTYPE> docs"
