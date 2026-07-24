"""External storage for large tool outputs kept outside prompt history."""

from __future__ import annotations

import asyncio
import functools
import hashlib
import json
import logging
import os
import re
import time
from dataclasses import asdict, dataclass
from pathlib import Path
from tempfile import NamedTemporaryFile
from typing import Any, Protocol

logger = logging.getLogger(__name__)

DEFAULT_ARTIFACT_ROOT = Path(
    os.getenv("CONTEXT_ARTIFACT_ROOT", "/tmp/langchain_context_artifacts")
)
ARTIFACT_URI_PREFIX = "artifact://context/"
MINIO_ARTIFACT_MARKER = "minio"


def _metadata_db_enabled() -> bool:
    return os.getenv("CONTEXT_ARTIFACT_METADATA_DB", "").strip().lower() in {
        "1",
        "true",
        "yes",
        "on",
    }


def record_context_artifact_metadata(
    metadata: "ContextArtifactMetadata",
    *,
    preview: str | None = None,
) -> None:
    if not _metadata_db_enabled():
        return
    from backend.core.agents.context.artifact_metadata_repository import (
        record_context_artifact_metadata as record_metadata,
    )

    try:
        loop = asyncio.get_running_loop()
    except RuntimeError:
        record_metadata(metadata, preview=preview)
        return

    # Metadata is a best-effort search index.  ArtifactStore is also used by
    # async agent paths, so never make their event loop wait on pymysql.
    future = loop.run_in_executor(
        None,
        functools.partial(record_metadata, metadata, preview=preview),
    )
    future.add_done_callback(_log_metadata_write_failure)


def _log_metadata_write_failure(future: asyncio.Future[None]) -> None:
    try:
        future.result()
    except Exception:
        logger.debug("[ContextArtifact] async metadata write skipped", exc_info=True)


@dataclass(frozen=True)
class ContextArtifactMetadata:
    raw_ref: str
    thread_id: str
    source_tool: str
    size_chars: int
    sha256: str
    suffix: str
    created_at: float
    attributes: dict[str, Any]


def context_artifact_scope_matches(
    metadata: ContextArtifactMetadata | None,
    *,
    user_id: str | None,
    thread_id: str | None,
) -> bool:
    """Return whether an artifact belongs to the authenticated runtime scope.

    Artifact references are capabilities, not authorization credentials.  The
    metadata written by the runtime therefore carries the original user and
    thread identifiers, while the path remains sanitized for storage.  Older
    artifacts without an owner are intentionally rejected by this check so a
    scoped tool can fail closed during the migration.
    """

    if metadata is None:
        return False
    normalized_user = str(user_id or "").strip()
    normalized_thread = str(thread_id or "").strip()
    if not normalized_user or not normalized_thread:
        return False
    attributes = metadata.attributes if isinstance(metadata.attributes, dict) else {}
    owner_user = str(
        attributes.get("owner_user_id") or attributes.get("user_id") or ""
    ).strip()
    owner_thread = str(
        attributes.get("owner_thread_id") or attributes.get("thread_id") or ""
    ).strip()
    if owner_user != normalized_user:
        return False
    if owner_thread:
        return owner_thread == normalized_thread
    # Compatibility for metadata produced after ownership was introduced but
    # before the original thread value was persisted.
    return metadata.thread_id == ArtifactStore._safe_part(normalized_thread)


class ContextArtifactStore(Protocol):
    """Storage contract for large context payloads kept outside checkpoints."""

    def store(
        self,
        content: str,
        *,
        thread_id: str | None = None,
        source_tool: str = "unknown",
        suffix: str = ".txt",
        attributes: dict[str, Any] | None = None,
    ) -> str: ...

    def read(self, raw_ref: str | None, *, max_chars: int | None = None) -> str | None: ...

    def read_excerpt(self, raw_ref: str | None, *, max_chars: int) -> str | None: ...

    def metadata(self, raw_ref: str | None) -> ContextArtifactMetadata | None: ...


class ArtifactStore:
    """Persist and retrieve raw context payloads through opaque references."""

    def __init__(self, root: str | Path | None = None):
        self._root = Path(root or DEFAULT_ARTIFACT_ROOT)
        self._root.mkdir(parents=True, exist_ok=True)

    def store(
        self,
        content: str,
        *,
        thread_id: str | None = None,
        source_tool: str = "unknown",
        suffix: str = ".txt",
        attributes: dict[str, Any] | None = None,
    ) -> str:
        thread_part = self._safe_part(thread_id or "shared")
        safe_tool = self._safe_part(source_tool)
        safe_suffix = self._safe_suffix(suffix)
        encoded = content.encode("utf-8")
        full_digest = hashlib.sha256(encoded).hexdigest()
        digest = full_digest[:20]
        target_dir = self._root / thread_part
        target_dir.mkdir(parents=True, exist_ok=True)
        target = target_dir / f"{safe_tool}-{digest}{safe_suffix}"
        if not target.exists():
            self._write_text_atomic(target, content)
        relative = target.relative_to(self._root).as_posix()
        raw_ref = f"{ARTIFACT_URI_PREFIX}{relative}"
        metadata = ContextArtifactMetadata(
            raw_ref=raw_ref,
            thread_id=thread_part,
            source_tool=safe_tool,
            size_chars=len(content),
            sha256=full_digest,
            suffix=safe_suffix,
            created_at=time.time(),
            attributes=dict(attributes or {}),
        )
        metadata_target = self._metadata_path(target)
        if not metadata_target.exists():
            self._write_text_atomic(
                metadata_target,
                json.dumps(asdict(metadata), ensure_ascii=False, sort_keys=True),
            )
        record_context_artifact_metadata(metadata, preview=content)
        return raw_ref

    def read(self, raw_ref: str | None, *, max_chars: int | None = None) -> str | None:
        target = self._resolve_ref(raw_ref)
        if target is None:
            return None
        if max_chars is None:
            return target.read_text(encoding="utf-8")
        limit = max(0, int(max_chars))
        with target.open("r", encoding="utf-8") as handle:
            return handle.read(limit)

    def read_excerpt(self, raw_ref: str | None, *, max_chars: int) -> str | None:
        """Read a bounded head/tail excerpt without loading the complete file."""

        target = self._resolve_ref(raw_ref)
        if target is None:
            return None
        limit = max(0, int(max_chars))
        if limit == 0:
            return ""
        marker = "\n... [middle omitted; raw result available by reference] ...\n"
        if limit <= len(marker) + 2:
            return self.read(raw_ref, max_chars=limit)

        metadata = self.metadata(raw_ref)
        if metadata is not None and metadata.size_chars <= limit:
            return self.read(raw_ref, max_chars=limit)

        available = limit - len(marker)
        head_chars = max(1, int(available * 0.72))
        tail_chars = max(1, available - head_chars)
        # UTF-8 uses at most four bytes per code point. Reading four times the
        # character allowance keeps memory bounded while covering CJK content.
        with target.open("rb") as handle:
            head = handle.read(head_chars * 4).decode("utf-8", errors="ignore")
            handle.seek(0, os.SEEK_END)
            size_bytes = handle.tell()
            handle.seek(max(0, size_bytes - tail_chars * 4), os.SEEK_SET)
            tail = handle.read(tail_chars * 4).decode("utf-8", errors="ignore")
        return f"{head[:head_chars].rstrip()}{marker}{tail[-tail_chars:].lstrip()}"

    def metadata(self, raw_ref: str | None) -> ContextArtifactMetadata | None:
        target = self._resolve_ref(raw_ref)
        if target is None:
            return None
        metadata_path = self._metadata_path(target)
        if not metadata_path.is_file():
            return None
        try:
            payload = json.loads(metadata_path.read_text(encoding="utf-8"))
        except (OSError, json.JSONDecodeError):
            return None
        if not isinstance(payload, dict):
            return None
        return ContextArtifactMetadata(
            raw_ref=str(payload.get("raw_ref") or raw_ref or ""),
            thread_id=str(payload.get("thread_id") or ""),
            source_tool=str(payload.get("source_tool") or ""),
            size_chars=int(payload.get("size_chars") or 0),
            sha256=str(payload.get("sha256") or ""),
            suffix=str(payload.get("suffix") or ""),
            created_at=float(payload.get("created_at") or 0.0),
            attributes=payload.get("attributes")
            if isinstance(payload.get("attributes"), dict)
            else {},
        )

    def _resolve_ref(self, raw_ref: str | None) -> Path | None:
        if not raw_ref or not raw_ref.startswith(ARTIFACT_URI_PREFIX):
            return None
        relative = raw_ref[len(ARTIFACT_URI_PREFIX) :]
        target = (self._root / relative).resolve()
        try:
            target.relative_to(self._root.resolve())
        except ValueError:
            return None
        if not target.is_file():
            return None
        return target

    @staticmethod
    def _metadata_path(target: Path) -> Path:
        return target.with_name(f"{target.name}.meta.json")

    @staticmethod
    def _safe_part(value: str) -> str:
        cleaned = re.sub(r"[^a-zA-Z0-9_.-]+", "_", value).strip("._")
        return cleaned[:100] or "unknown"

    @staticmethod
    def _safe_suffix(value: str) -> str:
        suffix = str(value or ".txt").strip().replace("\\", "/").rsplit("/", 1)[-1]
        if not suffix.startswith("."):
            suffix = f".{suffix}"
        suffix = re.sub(r"[^a-zA-Z0-9.]+", "_", suffix)
        suffix = suffix.replace("..", ".")
        return suffix[:24] or ".txt"

    @staticmethod
    def _write_text_atomic(target: Path, content: str) -> None:
        with NamedTemporaryFile(
            "w",
            encoding="utf-8",
            dir=str(target.parent),
            delete=False,
        ) as handle:
            handle.write(content)
            temp_name = handle.name
        Path(temp_name).replace(target)


class MinioContextArtifactStore:
    """Persist context payloads in the configured user MinIO bucket.

    Raw refs keep the existing ``artifact://context/`` scheme and encode the
    backend in the path: ``artifact://context/minio/<bucket>/<object>``.
    """

    def __init__(
        self,
        *,
        bucket: str | None = None,
        prefix: str | None = None,
    ) -> None:
        from backend.storage import get_user_minio_bucket_name

        self._bucket = bucket or os.getenv("CONTEXT_ARTIFACT_MINIO_BUCKET") or get_user_minio_bucket_name()
        self._prefix = (prefix or os.getenv("CONTEXT_ARTIFACT_MINIO_PREFIX") or "context-artifacts").strip("/")

    def store(
        self,
        content: str,
        *,
        thread_id: str | None = None,
        source_tool: str = "unknown",
        suffix: str = ".txt",
        attributes: dict[str, Any] | None = None,
    ) -> str:
        from backend.storage import upload_bytes_to_minio_sync

        thread_part = ArtifactStore._safe_part(thread_id or "shared")
        safe_tool = ArtifactStore._safe_part(source_tool)
        safe_suffix = ArtifactStore._safe_suffix(suffix)
        encoded = content.encode("utf-8")
        full_digest = hashlib.sha256(encoded).hexdigest()
        digest = full_digest[:20]
        object_name = f"{self._prefix}/{thread_part}/{safe_tool}-{digest}{safe_suffix}"
        uploaded = upload_bytes_to_minio_sync(
            encoded,
            object_name,
            content_type="text/plain; charset=utf-8",
            bucket_name=self._bucket,
        )
        if not uploaded:
            raise RuntimeError("failed to upload context artifact to MinIO")
        raw_ref = self._raw_ref(object_name)
        metadata = ContextArtifactMetadata(
            raw_ref=raw_ref,
            thread_id=thread_part,
            source_tool=safe_tool,
            size_chars=len(content),
            sha256=full_digest,
            suffix=safe_suffix,
            created_at=time.time(),
            attributes={
                **dict(attributes or {}),
                "storage": "minio",
                "bucket": self._bucket,
                "object_name": object_name,
            },
        )
        upload_bytes_to_minio_sync(
            json.dumps(asdict(metadata), ensure_ascii=False, sort_keys=True).encode("utf-8"),
            f"{object_name}.meta.json",
            content_type="application/json; charset=utf-8",
            bucket_name=self._bucket,
        )
        record_context_artifact_metadata(metadata, preview=content)
        return raw_ref

    def read(self, raw_ref: str | None, *, max_chars: int | None = None) -> str | None:
        object_name = self._object_from_ref(raw_ref)
        if not object_name:
            return None
        try:
            payload = self._read_object(object_name)
        except Exception:
            logger.warning(
                "failed to read MinIO context artifact: ref=%s",
                raw_ref,
                exc_info=True,
            )
            return None
        text = payload.decode("utf-8", errors="replace")
        if max_chars is None:
            return text
        return text[: max(0, int(max_chars))]

    def read_excerpt(self, raw_ref: str | None, *, max_chars: int) -> str | None:
        object_name = self._object_from_ref(raw_ref)
        if not object_name:
            return None
        try:
            payload = self._read_object(object_name)
        except Exception:
            logger.warning(
                "failed to read MinIO context artifact excerpt: ref=%s",
                raw_ref,
                exc_info=True,
            )
            return None
        return _head_tail_excerpt(
            payload.decode("utf-8", errors="replace"),
            max_chars=max_chars,
        )

    def metadata(self, raw_ref: str | None) -> ContextArtifactMetadata | None:
        object_name = self._object_from_ref(raw_ref)
        if not object_name:
            return None
        try:
            payload = json.loads(self._read_object(f"{object_name}.meta.json").decode("utf-8"))
        except Exception:
            logger.warning(
                "failed to read MinIO context artifact metadata: ref=%s",
                raw_ref,
                exc_info=True,
            )
            return None
        if not isinstance(payload, dict):
            return None
        return ContextArtifactMetadata(
            raw_ref=str(payload.get("raw_ref") or raw_ref or ""),
            thread_id=str(payload.get("thread_id") or ""),
            source_tool=str(payload.get("source_tool") or ""),
            size_chars=int(payload.get("size_chars") or 0),
            sha256=str(payload.get("sha256") or ""),
            suffix=str(payload.get("suffix") or ""),
            created_at=float(payload.get("created_at") or 0.0),
            attributes=payload.get("attributes")
            if isinstance(payload.get("attributes"), dict)
            else {},
        )

    def _read_object(self, object_name: str) -> bytes:
        from backend.storage import _get_minio_client

        response = _get_minio_client().get_object(self._bucket, object_name)
        try:
            return response.read()
        finally:
            close = getattr(response, "close", None)
            release_conn = getattr(response, "release_conn", None)
            if callable(close):
                close()
            if callable(release_conn):
                release_conn()

    def _raw_ref(self, object_name: str) -> str:
        return f"{ARTIFACT_URI_PREFIX}{MINIO_ARTIFACT_MARKER}/{ArtifactStore._safe_part(self._bucket)}/{object_name.lstrip('/')}"

    def _object_from_ref(self, raw_ref: str | None) -> str | None:
        if not raw_ref or not raw_ref.startswith(ARTIFACT_URI_PREFIX):
            return None
        relative = raw_ref[len(ARTIFACT_URI_PREFIX) :].lstrip("/")
        marker_prefix = f"{MINIO_ARTIFACT_MARKER}/{self._bucket}/"
        safe_marker_prefix = f"{MINIO_ARTIFACT_MARKER}/{ArtifactStore._safe_part(self._bucket)}/"
        if relative.startswith(marker_prefix):
            object_name = relative[len(marker_prefix) :]
        elif relative.startswith(safe_marker_prefix):
            object_name = relative[len(safe_marker_prefix) :]
        else:
            return None
        object_name = object_name.lstrip("/")
        if ".." in object_name.split("/"):
            return None
        if self._prefix and not object_name.startswith(f"{self._prefix}/"):
            return None
        return object_name or None


class FallbackContextArtifactStore:
    """Use primary storage first and transparently fall back to local storage."""

    def __init__(
        self,
        primary: ContextArtifactStore,
        fallback: ContextArtifactStore | None = None,
    ) -> None:
        self._primary = primary
        self._fallback = fallback or ArtifactStore()

    def store(self, content: str, **kwargs: Any) -> str:
        try:
            return self._primary.store(content, **kwargs)
        except Exception:
            logger.exception(
                "primary context artifact storage failed; using node-local fallback"
            )
            return self._fallback.store(content, **kwargs)

    def read(self, raw_ref: str | None, *, max_chars: int | None = None) -> str | None:
        return self._primary.read(raw_ref, max_chars=max_chars) or self._fallback.read(
            raw_ref,
            max_chars=max_chars,
        )

    def read_excerpt(self, raw_ref: str | None, *, max_chars: int) -> str | None:
        primary_reader = getattr(self._primary, "read_excerpt", None)
        if callable(primary_reader):
            content = primary_reader(raw_ref, max_chars=max_chars)
            if content:
                return content
        fallback_reader = getattr(self._fallback, "read_excerpt", None)
        if callable(fallback_reader):
            return fallback_reader(raw_ref, max_chars=max_chars)
        return self._fallback.read(raw_ref, max_chars=max_chars)

    def metadata(self, raw_ref: str | None) -> ContextArtifactMetadata | None:
        return self._primary.metadata(raw_ref) or self._fallback.metadata(raw_ref)


def create_context_artifact_store() -> ContextArtifactStore:
    backend = os.getenv("CONTEXT_ARTIFACT_STORE", "local").strip().lower()
    if backend in {"minio", "s3"}:
        return FallbackContextArtifactStore(MinioContextArtifactStore())
    return ArtifactStore()


def _head_tail_excerpt(content: str, *, max_chars: int) -> str:
    limit = max(0, int(max_chars))
    if len(content) <= limit:
        return content
    if limit < 200:
        return content[:limit]
    marker = "\n... [middle omitted; raw result available by reference] ...\n"
    available = limit - len(marker)
    head_chars = max(1, int(available * 0.72))
    tail_chars = max(1, available - head_chars)
    return f"{content[:head_chars].rstrip()}{marker}{content[-tail_chars:].lstrip()}"
