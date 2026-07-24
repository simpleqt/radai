"""Streaming helpers for object-storage HTTP responses."""

from __future__ import annotations

from collections.abc import Iterator
from typing import Any


def iter_minio_response(
    response: Any, *, chunk_size: int = 64 * 1024
) -> Iterator[bytes]:
    """Yield a MinIO response body and always release its connection."""
    try:
        yield from response.stream(chunk_size)
    finally:
        try:
            response.close()
        finally:
            response.release_conn()
