"""Small environment-driven security switches shared by HTTP services."""

from __future__ import annotations

import os
from typing import Any

from fastapi import Request
from starlette.responses import Response


_FASTAPI_DOCUMENTATION_PREFIXES = ("/docs", "/redoc", "/openapi.json")


def env_enabled(name: str, *, default: bool = False) -> bool:
    fallback = "true" if default else "false"
    return os.getenv(name, fallback).strip().lower() in {
        "1",
        "true",
        "yes",
        "on",
    }


def cors_allow_origins(defaults: list[str], *, env_var: str) -> list[str]:
    """Build the CORS allowlist from a fixed default plus an env override.

    The default list is the reviewed production/development set.  ``env_var``
    names a comma-separated environment variable whose entries are *appended*
    (de-duplicated) so an operator can temporarily admit an extra origin
    without shipping a code change; the safe defaults always remain.
    """

    origins = list(defaults)
    for raw in (os.getenv(env_var, "") or "").split(","):
        origin = raw.strip().rstrip("/")
        if origin and origin not in origins:
            origins.append(origin)
    return origins


def public_base_url(request: Any | None = None) -> str:
    """Return the base URL used for links this service hands back to clients.

    ``request.base_url`` reflects the caller-supplied ``Host`` header, so using
    it for signed URLs lets an attacker have the backend mint links pointing at
    a domain they control.  ``PUBLIC_BASE_URL`` is therefore the authority, and
    the request origin is only a development fallback.
    """

    configured = (os.getenv("PUBLIC_BASE_URL", "") or "").strip().rstrip("/")
    if configured:
        return configured
    if request is None:
        return ""
    return str(getattr(request, "base_url", "") or "").rstrip("/")


def fastapi_documentation_kwargs() -> dict[str, Any]:
    """Return FastAPI URL settings for the current runtime.

    Developer and isolated test environments retain the interactive API
    documentation by default.  Production disables it explicitly with
    ``API_DOCS_ENABLED=false``.
    """

    if env_enabled("API_DOCS_ENABLED", default=True):
        return {}
    return {"docs_url": None, "redoc_url": None, "openapi_url": None}


def install_fastapi_documentation_guard(app: Any) -> None:
    """Keep disabled documentation paths from falling through to an SPA.

    Passing ``None`` for FastAPI's documentation URLs removes the generated
    routes, but a later catch-all mount can still answer those paths.  The
    production contract requires a hard 404 instead.
    """

    if env_enabled("API_DOCS_ENABLED", default=True):
        return

    @app.middleware("http")
    async def reject_disabled_documentation(
        request: Request,
        call_next: Any,
    ) -> Any:
        path = request.url.path
        if any(
            path == prefix or path.startswith(f"{prefix}/")
            for prefix in _FASTAPI_DOCUMENTATION_PREFIXES
        ):
            return Response(status_code=404)
        return await call_next(request)
