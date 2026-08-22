"""Small helpers for MinIO SDK endpoint and object path handling."""

from __future__ import annotations

from pathlib import Path
from urllib.parse import urlparse

LOCAL_CACHE_FILE_NAME_MAX_BYTES = 240


def build_bounded_download_cache_path(
    cache_dir: Path,
    file_id: str,
    file_name: str,
    *,
    max_component_bytes: int = LOCAL_CACHE_FILE_NAME_MAX_BYTES,
) -> Path:
    """Build a unique cache path below the filesystem component limit."""
    original_name = Path(file_name or "").name or "pending_file"
    suffix = Path(original_name).suffix
    stem = Path(original_name).stem or "pending_file"
    safe_stem = "".join(
        ch if ch.isalnum() or ch in ("-", "_") else "_" for ch in stem
    ).strip("_")
    safe_stem = safe_stem or "pending_file"
    cache_stem = f"{file_id}_{safe_stem}"
    stem_budget = max(1, int(max_component_bytes) - len(suffix.encode("utf-8")))
    encoded_stem = cache_stem.encode("utf-8")
    if len(encoded_stem) > stem_budget:
        cache_stem = encoded_stem[:stem_budget].decode("utf-8", errors="ignore")
    return cache_dir / f"{cache_stem}{suffix}"


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
