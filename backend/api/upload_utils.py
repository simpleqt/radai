"""Bounded UploadFile readers shared by HTTP upload endpoints."""

from __future__ import annotations

import hashlib
import os
import tempfile
from pathlib import Path
from typing import BinaryIO

from fastapi import UploadFile


class UploadTooLargeError(ValueError):
    pass


async def read_upload_with_limit(
    upload: UploadFile,
    max_bytes: int,
    *,
    chunk_bytes: int = 1024 * 1024,
) -> bytes:
    """Read an upload incrementally and abort before retaining oversized data."""
    limit = max(1, int(max_bytes))
    step = max(64 * 1024, min(int(chunk_bytes), limit + 1))
    parts: list[bytes] = []
    total = 0
    while True:
        chunk = await upload.read(step)
        if not chunk:
            break
        total += len(chunk)
        if total > limit:
            raise UploadTooLargeError(f"upload exceeds {limit} bytes")
        parts.append(chunk)
    return b"".join(parts)


def spool_file_with_limit(
    source: BinaryIO,
    max_bytes: int,
    *,
    temp_dir: str | Path | None = None,
    target_path: str | Path | None = None,
    chunk_bytes: int = 1024 * 1024,
    prefix: str = "batch-upload-",
    suffix: str = ".tmp",
) -> tuple[str, int, str]:
    """Copy an upload to disk while enforcing size, hashing and atomic publish."""
    limit = max(1, int(max_bytes))
    step = max(64 * 1024, min(int(chunk_bytes), limit + 1))
    destination = Path(target_path).resolve() if target_path else None
    if destination is not None and temp_dir is not None:
        raise ValueError("temp_dir and target_path are mutually exclusive")
    directory = destination.parent if destination is not None else (
        Path(temp_dir).resolve() if temp_dir else None
    )
    if directory is not None:
        directory.mkdir(parents=True, exist_ok=True)

    temp_path: str | None = None
    try:
        with tempfile.NamedTemporaryFile(
            mode="wb",
            prefix=f".{destination.name}." if destination is not None else prefix,
            suffix=".part" if destination is not None else suffix,
            dir=str(directory) if directory else None,
            delete=False,
        ) as target:
            temp_path = target.name
            digest = hashlib.sha256()
            total = 0
            while True:
                chunk = source.read(step)
                if not chunk:
                    break
                total += len(chunk)
                if total > limit:
                    raise UploadTooLargeError(f"upload exceeds {limit} bytes")
                digest.update(chunk)
                target.write(chunk)
        if destination is not None:
            os.replace(temp_path, destination)
            temp_path = None
            return str(destination), total, digest.hexdigest()
        return temp_path, total, digest.hexdigest()
    except BaseException:
        if temp_path:
            try:
                os.unlink(temp_path)
            except FileNotFoundError:
                pass
        raise
