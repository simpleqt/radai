"""Classify transient RAGAnything failures for caller-owned retry loops."""

from __future__ import annotations

import asyncio


RETRYABLE_NETWORK_EXCEPTIONS: tuple[type[Exception], ...] = (
    ConnectionError,
    ConnectionRefusedError,
    ConnectionResetError,
    TimeoutError,
    asyncio.TimeoutError,
)

RETRYABLE_HTTP_STATUS_CODES = frozenset({408, 429, 500, 502, 503, 504})


class RetryableError(Exception):
    """Explicitly mark an error as retryable."""


class NonRetryableError(Exception):
    """Explicitly mark an error as non-retryable."""


def is_retryable_http_error(error: Exception) -> bool:
    """Return whether an HTTP-style exception carries a transient status."""
    response = getattr(error, "response", None)
    status_code = getattr(response, "status_code", None)
    if status_code is None:
        status_code = getattr(error, "status_code", None)
    return status_code in RETRYABLE_HTTP_STATUS_CODES


def is_rate_limit_error(error: Exception) -> bool:
    """Recognize common rate-limit messages when no status is exposed."""
    error_text = str(error).lower()
    indicators = (
        "rate limit",
        "too many requests",
        "429",
        "quota exceeded",
        "requests per minute",
        "tokens per minute",
        "rpm",
        "tpm",
    )
    return any(indicator in error_text for indicator in indicators)


def is_timeout_error(error: Exception) -> bool:
    """Recognize timeout exceptions and common timeout messages."""
    if isinstance(error, (TimeoutError, asyncio.TimeoutError)):
        return True
    error_text = str(error).lower()
    return any(
        indicator in error_text
        for indicator in ("timeout", "timed out", "deadline exceeded")
    )


def should_retry(error: Exception) -> bool:
    """Return whether a caller-owned retry loop should retry ``error``."""
    if isinstance(error, NonRetryableError):
        return False
    if isinstance(error, (RetryableError, *RETRYABLE_NETWORK_EXCEPTIONS)):
        return True
    return (
        is_rate_limit_error(error)
        or is_timeout_error(error)
        or is_retryable_http_error(error)
    )


__all__ = [
    "NonRetryableError",
    "RETRYABLE_HTTP_STATUS_CODES",
    "RETRYABLE_NETWORK_EXCEPTIONS",
    "RetryableError",
    "is_rate_limit_error",
    "is_retryable_http_error",
    "is_timeout_error",
    "should_retry",
]
