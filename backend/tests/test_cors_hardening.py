"""CORS 收紧契约:默认源不含死条目/明文生产源,env 可追加,方法/头收紧。"""

from __future__ import annotations

from pathlib import Path

from backend.config.runtime_security import cors_allow_origins


def _server_source(relpath: str) -> str:
    root = Path(__file__).resolve().parents[2]
    return (root / relpath).read_text(encoding="utf-8")


def test_cors_allow_origins_appends_env_and_dedups(monkeypatch):
    monkeypatch.setenv("CORS_ALLOW_ORIGINS", " https://extra.example.com/ , https://extra.example.com ")
    origins = cors_allow_origins(
        ["https://app.example.com"], env_var="CORS_ALLOW_ORIGINS"
    )
    assert origins == ["https://app.example.com", "https://extra.example.com"]


def test_cors_allow_origins_ignores_empty_env(monkeypatch):
    monkeypatch.setenv("CORS_ALLOW_ORIGINS", " ,  ")
    origins = cors_allow_origins(["https://app.example.com"], env_var="CORS_ALLOW_ORIGINS")
    assert origins == ["https://app.example.com"]


def test_main_api_cors_removed_dead_and_plaintext_origins():
    source = _server_source("backend/api/http_server.py")
    # 无文档、无 Vite 配置引用的历史调试源已移除。
    assert "56568" not in source
    # 生产域名只保留 https,去掉可被中间人劫持的明文 http 源。
    assert "http://radai2.cdut.edu.cn" not in source
    assert "http://csh-ai.cdut.edu.cn" not in source
    assert "http://fmca.cdut.edu.cn" not in source
    # 收紧为显式方法/头白名单,不再使用 "*"。
    assert 'allow_methods=["*"]' not in source
    assert 'allow_headers=["*"]' not in source
    # 已接线到 env 可追加的辅助函数。
    assert "cors_allow_origins" in source
    assert "CORS_ALLOW_ORIGINS" in source


def test_guest_api_cors_removed_plaintext_fpitekai():
    source = _server_source("backend/api/guest_http_server.py")
    assert "http://fpitekai.com" not in source
    assert "https://fpitekai.com" in source
    assert 'allow_methods=["*"]' not in source
    assert 'allow_headers=["*"]' not in source
    assert "cors_allow_origins" in source
    assert "GUEST_CORS_ALLOW_ORIGINS" in source
