"""Side-effect-free helpers shared by the optional OpenAI-compatible proxies."""

from __future__ import annotations

import json
import logging
from collections.abc import Mapping

_HOP_BY_HOP_HEADERS = {
    "connection",
    "content-length",
    "host",
    "keep-alive",
    "proxy-authenticate",
    "proxy-authorization",
    "te",
    "trailer",
    "transfer-encoding",
    "upgrade",
}


def build_forward_headers(
    headers: Mapping[str, str],
    *,
    api_key: str,
    accept: str = "application/json",
) -> dict[str, str]:
    """Build sanitized upstream headers and replace caller credentials."""

    forwarded = {
        key: value
        for key, value in headers.items()
        if key.lower() not in _HOP_BY_HOP_HEADERS | {"authorization", "accept"}
    }
    forwarded["Authorization"] = f"Bearer {api_key}"
    forwarded["Accept"] = accept
    forwarded.setdefault("Content-Type", "application/json")
    return forwarded


def transform_model_request(
    body: bytes | None,
    *,
    target_model: str,
    logger: logging.Logger,
) -> bytes | None:
    """Replace a JSON request model without logging request contents."""

    if body is None or not target_model:
        return body
    try:
        payload = json.loads(body)
    except (json.JSONDecodeError, UnicodeDecodeError):
        return body
    if not isinstance(payload, dict) or "model" not in payload:
        return body
    payload["model"] = target_model
    logger.debug("OpenAI-compatible proxy model mapping applied")
    return json.dumps(payload, ensure_ascii=False, separators=(",", ":")).encode()
