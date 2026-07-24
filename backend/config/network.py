"""Network defaults shared by backend services."""

from __future__ import annotations

import os
from typing import Final

PROXY_ENV_VARS: Final[tuple[str, ...]] = (
    "HTTP_PROXY",
    "HTTPS_PROXY",
    "ALL_PROXY",
    "NO_PROXY",
    "http_proxy",
    "https_proxy",
    "all_proxy",
    "no_proxy",
)

_REMOVED_PROXY_ENV: dict[str, str] = {}


def disable_proxy_environment() -> dict[str, str]:
    """Remove proxy env vars so internal service calls never use local proxies."""
    for key in PROXY_ENV_VARS:
        value = os.environ.pop(key, None)
        if value:
            _REMOVED_PROXY_ENV.setdefault(key, value)
    return dict(_REMOVED_PROXY_ENV)
