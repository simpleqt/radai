"""Small helpers for MinIO SDK endpoint and object path handling."""

from __future__ import annotations

from urllib.parse import urlparse


def normalize_minio_endpoint(endpoint: str) -> str:
    """Return the host[:port] form required by the MinIO SDK."""
    raw = str(endpoint or "").strip()
    if not raw:
        return raw
    parsed = urlparse(raw if "://" in raw else f"//{raw}")
    if parsed.netloc:
        return parsed.netloc
    return raw.split("/", 1)[0]


def parse_minio_source(source: str) -> tuple[str | None, str]:
    """Parse either minio://bucket/object or a legacy object name."""
    raw = str(source or "").strip()
    if raw.startswith("minio://"):
        parsed = urlparse(raw)
        bucket = parsed.netloc or None
        object_name = parsed.path.lstrip("/")
        return bucket, object_name
    return None, raw.lstrip("/")
