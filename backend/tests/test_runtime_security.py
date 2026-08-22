from __future__ import annotations

import httpx
import pytest
from fastapi import FastAPI
from fastapi.responses import HTMLResponse

from backend.config.runtime_security import (
    fastapi_documentation_kwargs,
    install_fastapi_documentation_guard,
)


def test_fastapi_documentation_is_enabled_by_default(monkeypatch):
    monkeypatch.delenv("API_DOCS_ENABLED", raising=False)

    assert fastapi_documentation_kwargs() == {}


def test_fastapi_documentation_can_be_disabled_for_production(monkeypatch):
    monkeypatch.setenv("API_DOCS_ENABLED", "false")

    assert fastapi_documentation_kwargs() == {
        "docs_url": None,
        "redoc_url": None,
        "openapi_url": None,
    }


@pytest.mark.asyncio
async def test_disabled_documentation_paths_do_not_fall_through_to_spa(
    monkeypatch,
):
    monkeypatch.setenv("API_DOCS_ENABLED", "false")
    app = FastAPI(**fastapi_documentation_kwargs())
    install_fastapi_documentation_guard(app)

    @app.get("/{path:path}")
    async def spa_fallback(path: str) -> HTMLResponse:
        return HTMLResponse(f"spa:{path}")

    transport = httpx.ASGITransport(app=app)
    async with httpx.AsyncClient(
        transport=transport,
        base_url="http://testserver",
    ) as client:
        for path in (
            "/docs",
            "/docs/",
            "/docs/oauth2-redirect",
            "/redoc",
            "/redoc/",
            "/openapi.json",
            "/openapi.json/",
        ):
            response = await client.get(path)
            assert response.status_code == 404

        response = await client.get("/application")

    assert response.status_code == 200
    assert response.text == "spa:application"
