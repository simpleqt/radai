"""Shared HTTP helpers for SMS authentication routes."""

from __future__ import annotations

from fastapi import Request
from fastapi.responses import JSONResponse

from backend.core.utils.client_ip import resolve_client_ip

SMS_RATE_LIMIT_MESSAGE = "验证码发送过于频繁，请稍后再试"


def client_ip(request: Request) -> str:
    return resolve_client_ip(request)


def sms_rate_limited_response(retry_after: int) -> JSONResponse:
    retry_after = max(1, int(retry_after))
    return JSONResponse(
        status_code=429,
        headers={"Retry-After": str(retry_after)},
        content={
            "success": False,
            "error": SMS_RATE_LIMIT_MESSAGE,
            "retry_after": retry_after,
        },
    )


__all__ = ["SMS_RATE_LIMIT_MESSAGE", "client_ip", "sms_rate_limited_response"]
