"""External storage for large tool outputs kept outside prompt history."""

from __future__ import annotations

import asyncio
import contextlib
import fcntl
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


@contextlib.contextmanager
def _sidecar_merge_lock(metadata_target: Path):
    """Serialize the sidecar read-merge-write across threads and processes.

    Content-addressed objects are shared: two owners can ``store`` the same
    oid concurrently (Agent API, Guest API, and workers share the artifact
    root). Without a lock the owners/turn_ids union is a lost-update race and
    the fail-closed ``context_read`` scope check then denies a legitimate
    owner. The lock file never gets renamed, so flock stays valid while the
    sidecar itself is atomically replaced. Failure degrades to the previous
    unlocked behavior instead of failing the store.
    """
    lock_path = metadata_target.with_name(metadata_target.name + ".lock")
    handle = None
    try:
        handle = open(lock_path, "a+")
        fcntl.flock(handle.fileno(), fcntl.LOCK_EX)
    except OSError:
        logger.debug("context artifact sidecar lock unavailable", exc_info=True)
        if handle is not None:
            handle.close()
            handle = None
    try:
        yield
    finally:
        if handle is not None:
            try:
                fcntl.flock(handle.fileno(), fcntl.LOCK_UN)
            except OSError:
                pass
            handle.close()

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
        logger.warning("[ContextArtifact] async metadata write failed", exc_info=True)


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


def context_origin_scope_matches(
    origin: dict[str, Any] | None,
    *,
    user_id: str | None,
    thread_id: str | None,
) -> bool:
    """Return whether a restored context-object origin belongs to the scope.

    ``ContextObjectStore.read`` restores ownership from the artifact sidecar
    into ``origin`` (user_id/thread_id).  This is the same fail-closed check as
    :func:`context_artifact_scope_matches` but against the merged object, so
    the navigator can reuse the request-level read cache instead of performing
    an extra metadata round trip.  Missing owner fields are rejected.
    """

    if not isinstance(origin, dict):
        return False
    normalized_user = str(user_id or "").strip()
    normalized_thread = str(thread_id or "").strip()
    if not normalized_user or not normalized_thread:
        return False
    owner_user = str(
        origin.get("owner_user_id") or origin.get("user_id") or ""
    ).strip()
    owner_thread = str(
        origin.get("owner_thread_id") or origin.get("thread_id") or ""
    ).strip()
    if owner_user != normalized_user:
        return False
    if not owner_thread:
        return False
    return owner_thread == normalized_thread


def merge_sidecar_attributes(
    previous_attributes: dict[str, Any],
    attributes: dict[str, Any],
    *,
    turn_ids: list[str],
) -> dict[str, Any]:
    """Union-merge a sidecar refresh with its previous attributes.

    内容寻址对象跨用户/线程共享:owners 必须按 (user_id, thread_id) 并集,
    turn_ids 按序去重;直接 {**prev, **new} 会让后写者夺走先前属主,
    fail-closed 的 context_read 随即拒绝其合法读取。本地与 MinIO 两个
    后端共用同一合并语义。
    """

    prev_owners = [
        owner
        for owner in (previous_attributes.get("owners") or [])
        if isinstance(owner, dict)
    ]
    new_owners = [
        owner
        for owner in (attributes.get("owners") or [])
        if isinstance(owner, dict)
    ]
    merged = {**previous_attributes, **attributes}
    merged["turn_ids"] = list(
        dict.fromkeys(
            [
                *(previous_attributes.get("turn_ids") or []),
                *turn_ids,
            ]
        )
    )[-64:]
    merged_owners: list[dict[str, Any]] = []
    seen_owners: set[tuple[str, str]] = set()
    for owner in [*prev_owners, *new_owners]:
        key = (
            str(owner.get("user_id") or ""),
            str(owner.get("thread_id") or ""),
        )
        if key not in seen_owners:
            seen_owners.add(key)
            merged_owners.append(owner)
    if merged_owners:
        merged["owners"] = merged_owners[-64:]
    return merged


def context_owner_allowed(
    origin: dict[str, Any] | None,
    owners: list[dict[str, Any]] | None,
    *,
    user_id: str | None,
    thread_id: str | None,
) -> bool:
    """Return whether the requester owns a (possibly multi-owner) context object.

    Content-addressed COS deduplicates identical tool output across users and
    threads, so one stored object may have several legitimate owners. Each entry
    in ``owners`` — plus the restored ``origin`` for backward compatibility with
    objects written before multi-owner tracking — is checked with the same
    fail-closed scope match; any single match authorizes the read.
    """

    candidates: list[dict[str, Any]] = []
    if isinstance(owners, list):
        candidates.extend(owner for owner in owners if isinstance(owner, dict))
    if isinstance(origin, dict):
        candidates.append(origin)
    for candidate in candidates:
        if context_origin_scope_matches(
            candidate, user_id=user_id, thread_id=thread_id
        ):
            return True
    return False


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

    def object_ref(self, oid: str, prefix: str = "context_object") -> str: ...

    def scan_metadata(self) -> list[ContextArtifactMetadata]: ...

    def delete(self, raw_ref: str | None) -> bool: ...


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
        attributes = dict(attributes or {})
        now = time.time()
        turn_id = str(attributes.get("turn_id") or "").strip()
        turn_ids = [
            str(value)
            for value in attributes.get("turn_ids") or []
            if str(value).strip()
        ]
        if turn_id and turn_id not in turn_ids:
            turn_ids.append(turn_id)
        attributes["turn_ids"] = turn_ids[-64:]
        attributes["last_seen_at"] = now
        created_at = now
        metadata_target = self._metadata_path(target)
        # 读-合并-写全程持有跨进程侧车锁:并发 put 同一内容寻址对象时,
        # 无锁合并是丢失更新竞态(owners/turn_ids 被互相覆盖)。
        with _sidecar_merge_lock(metadata_target):
            if metadata_target.exists():
                try:
                    previous = json.loads(
                        metadata_target.read_text(encoding="utf-8")
                    )
                    if isinstance(previous, dict):
                        created_at = float(previous.get("created_at") or created_at)
                        previous_attributes = previous.get("attributes")
                        if isinstance(previous_attributes, dict):
                            # owners 并集 + turn_ids 去重(共享合并语义)。
                            attributes = merge_sidecar_attributes(
                                previous_attributes,
                                attributes,
                                turn_ids=turn_ids,
                            )
                except (OSError, json.JSONDecodeError):
                    logger.warning(
                        "failed to refresh context artifact metadata: raw_ref=%s",
                        raw_ref,
                        exc_info=True,
                    )
            metadata = ContextArtifactMetadata(
                raw_ref=raw_ref,
                thread_id=thread_part,
                source_tool=safe_tool,
                size_chars=len(content),
                sha256=full_digest,
                suffix=safe_suffix,
                created_at=created_at,
                attributes=attributes,
            )
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

    def refresh_last_seen(
        self,
        raw_ref: str | None,
        *,
        now: float | None = None,
    ) -> bool:
        """Bump the sidecar ``last_seen_at`` without rewriting the artifact.

        GC ages objects by ``last_seen_at``; only writes used to refresh it,
        so evidence a long-lived thread kept *reading* still expired at the
        TTL. Read paths call this (throttled) to keep actively referenced
        objects alive.
        """

        target = self._resolve_ref(raw_ref)
        if target is None:
            return False
        metadata_target = self._metadata_path(target)
        if not metadata_target.is_file():
            return False
        stamp = float(now if now is not None else time.time())
        with _sidecar_merge_lock(metadata_target):
            try:
                payload = json.loads(metadata_target.read_text(encoding="utf-8"))
            except (OSError, json.JSONDecodeError):
                return False
            if not isinstance(payload, dict):
                return False
            attributes = payload.get("attributes")
            if not isinstance(attributes, dict):
                attributes = {}
            attributes["last_seen_at"] = stamp
            payload["attributes"] = attributes
            try:
                self._write_text_atomic(
                    metadata_target,
                    json.dumps(payload, ensure_ascii=False, sort_keys=True),
                )
            except OSError:
                return False
        return True

    def object_ref(self, oid: str, prefix: str = "context_object") -> str:
        digest = str(oid or "")[:20]
        safe_prefix = self._safe_part(prefix)
        return f"{ARTIFACT_URI_PREFIX}cos/{safe_prefix}-{digest}.json"

    def scan_metadata(self) -> list[ContextArtifactMetadata]:
        """Return every metadata record persisted in the local root."""

        records: list[ContextArtifactMetadata] = []
        if not self._root.is_dir():
            return records
        for metadata_path in sorted(self._root.rglob("*.meta.json")):
            try:
                payload = json.loads(metadata_path.read_text(encoding="utf-8"))
            except (OSError, json.JSONDecodeError):
                continue
            if not isinstance(payload, dict):
                continue
            records.append(
                ContextArtifactMetadata(
                    raw_ref=str(payload.get("raw_ref") or ""),
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
            )
        return records

    def delete(self, raw_ref: str | None) -> bool:
        target = self._resolve_ref(raw_ref)
        if target is None:
            return False
        try:
            target.unlink()
        except FileNotFoundError:
            pass
        except OSError:
            logger.warning(
                "failed to delete context artifact: raw_ref=%s",
                raw_ref,
                exc_info=True,
            )
            return False
        metadata_path = self._metadata_path(target)
        lock_path = metadata_path.with_name(metadata_path.name + ".lock")
        try:
            lock_path.unlink()
        except OSError:
            pass
        try:
            if metadata_path.exists():
                metadata_path.unlink()
        except OSError:
            logger.warning(
                "failed to delete context artifact metadata: raw_ref=%s",
                raw_ref,
                exc_info=True,
            )
        return True

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
        attributes = dict(attributes or {})
        now = time.time()
        turn_id = str(attributes.get("turn_id") or "").strip()
        turn_ids = [
            str(value)
            for value in attributes.get("turn_ids") or []
            if str(value).strip()
        ]
        if turn_id and turn_id not in turn_ids:
            turn_ids.append(turn_id)
        attributes["turn_ids"] = turn_ids[-64:]
        attributes["last_seen_at"] = now
        attributes["storage"] = "minio"
        attributes["bucket"] = self._bucket
        attributes["object_name"] = object_name
        created_at = now
        try:
            previous = json.loads(
                self._read_object(f"{object_name}.meta.json").decode(
                    "utf-8",
                    errors="replace",
                )
            )
            if isinstance(previous, dict):
                created_at = float(previous.get("created_at") or created_at)
                previous_attributes = previous.get("attributes")
                if isinstance(previous_attributes, dict):
                    # 与本地后端一致的 owners 并集:{**prev, **new} 覆盖式合并
                    # 会让后写者夺走先前属主,fail-closed 的 context_read 随即
                    # 拒绝其合法读取(本地已修,此处移植)。对象存储侧无 flock
                    # 等价物,并发写仍可能丢一次合并(窗口极窄),owners 语义
                    # 保证单次合并不再覆盖。
                    attributes = merge_sidecar_attributes(
                        previous_attributes,
                        attributes,
                        turn_ids=turn_ids,
                    )
        except Exception:
            logger.debug(
                "MinIO context artifact metadata refresh skipped: ref=%s",
                raw_ref,
                exc_info=True,
            )
        metadata = ContextArtifactMetadata(
            raw_ref=raw_ref,
            thread_id=thread_part,
            source_tool=safe_tool,
            size_chars=len(content),
            sha256=full_digest,
            suffix=safe_suffix,
            created_at=created_at,
            attributes=attributes,
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

    def refresh_last_seen(
        self,
        raw_ref: str | None,
        *,
        now: float | None = None,
    ) -> bool:
        """Best-effort read-side ``last_seen_at`` refresh (no flock on MinIO)."""

        object_name = self._object_from_ref(raw_ref)
        if not object_name:
            return False
        meta_name = f"{object_name}.meta.json"
        try:
            payload = json.loads(
                self._read_object(meta_name).decode("utf-8", errors="replace")
            )
        except Exception:
            return False
        if not isinstance(payload, dict):
            return False
        attributes = payload.get("attributes")
        if not isinstance(attributes, dict):
            attributes = {}
        attributes["last_seen_at"] = float(
            now if now is not None else time.time()
        )
        payload["attributes"] = attributes
        try:
            from backend.storage import upload_bytes_to_minio_sync

            return bool(
                upload_bytes_to_minio_sync(
                    json.dumps(
                        payload, ensure_ascii=False, sort_keys=True
                    ).encode("utf-8"),
                    meta_name,
                    content_type="application/json; charset=utf-8",
                    bucket_name=self._bucket,
                )
            )
        except Exception:
            logger.debug(
                "MinIO context artifact last_seen refresh failed: ref=%s",
                raw_ref,
                exc_info=True,
            )
            return False

    def object_ref(self, oid: str, prefix: str = "context_object") -> str:
        digest = str(oid or "")[:20]
        safe_prefix = ArtifactStore._safe_part(prefix)
        object_name = f"{self._prefix}/cos/{safe_prefix}-{digest}.json"
        return self._raw_ref(object_name)

    def scan_metadata(self) -> list[ContextArtifactMetadata]:
        """List MinIO metadata sidecars under the artifact prefix."""

        try:
            from backend.storage import _get_minio_client

            client = _get_minio_client()
        except Exception:
            logger.warning("MinIO context artifact scan unavailable", exc_info=True)
            return []
        records: list[ContextArtifactMetadata] = []
        prefix = f"{self._prefix}/"
        try:
            for item in client.list_objects(self._bucket, prefix=prefix, recursive=True):
                object_name = str(getattr(item, "object_name", "") or "")
                if not object_name.endswith(".meta.json"):
                    continue
                payload_bytes = self._read_object(object_name)
                payload = json.loads(payload_bytes.decode("utf-8", errors="replace"))
                if not isinstance(payload, dict):
                    continue
                records.append(
                    ContextArtifactMetadata(
                        raw_ref=str(payload.get("raw_ref") or ""),
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
                )
        except Exception:
            logger.warning(
                "failed to scan MinIO context artifacts",
                exc_info=True,
            )
        return records

    def delete(self, raw_ref: str | None) -> bool:
        object_name = self._object_from_ref(raw_ref)
        if not object_name:
            return False
        try:
            from backend.storage import _get_minio_client

            client = _get_minio_client()
            client.remove_object(self._bucket, object_name)
            client.remove_object(self._bucket, f"{object_name}.meta.json")
            return True
        except Exception:
            logger.warning(
                "failed to delete MinIO context artifact: ref=%s",
                raw_ref,
                exc_info=True,
            )
            return False

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

    @staticmethod
    def _local_cos_ref(raw_ref: str | None) -> str | None:
        """Translate a backend-form COS ref into the local fallback's form.

        ``object_ref`` always returns the primary (e.g. MinIO) reference, but
        a write that fell back landed under the local root at
        ``cos/<name>.json``. The local resolver maps the MinIO-form reference
        to a non-existent ``minio/<bucket>/...`` path, so without this
        translation every fallback-written object is unreadable (and its
        local file survives GC forever).
        """

        if not raw_ref or not raw_ref.startswith(ARTIFACT_URI_PREFIX):
            return None
        relative = raw_ref[len(ARTIFACT_URI_PREFIX) :].lstrip("/")
        parts = [part for part in relative.split("/") if part]
        try:
            index = len(parts) - 1 - parts[::-1].index("cos")
        except ValueError:
            return None
        candidate = ARTIFACT_URI_PREFIX + "/".join(["cos", *parts[index + 1 :]])
        return candidate if candidate != raw_ref else None

    def read(self, raw_ref: str | None, *, max_chars: int | None = None) -> str | None:
        content = self._primary.read(raw_ref, max_chars=max_chars) or self._fallback.read(
            raw_ref,
            max_chars=max_chars,
        )
        if content:
            return content
        local_ref = self._local_cos_ref(raw_ref)
        if local_ref is None:
            return None
        return self._fallback.read(local_ref, max_chars=max_chars)

    def read_excerpt(self, raw_ref: str | None, *, max_chars: int) -> str | None:
        primary_reader = getattr(self._primary, "read_excerpt", None)
        if callable(primary_reader):
            content = primary_reader(raw_ref, max_chars=max_chars)
            if content:
                return content
        fallback_reader = getattr(self._fallback, "read_excerpt", None)
        candidates = [raw_ref]
        local_ref = self._local_cos_ref(raw_ref)
        if local_ref is not None:
            candidates.append(local_ref)
        for candidate in candidates:
            if callable(fallback_reader):
                content = fallback_reader(candidate, max_chars=max_chars)
            else:
                content = self._fallback.read(candidate, max_chars=max_chars)
            if content:
                return content
        return None

    def metadata(self, raw_ref: str | None) -> ContextArtifactMetadata | None:
        found = self._primary.metadata(raw_ref) or self._fallback.metadata(raw_ref)
        if found is not None:
            return found
        local_ref = self._local_cos_ref(raw_ref)
        if local_ref is None:
            return None
        return self._fallback.metadata(local_ref)

    def object_ref(self, oid: str, prefix: str = "context_object") -> str:
        primary_ref = getattr(self._primary, "object_ref", None)
        fallback_ref = getattr(self._fallback, "object_ref", None)
        if callable(primary_ref):
            return primary_ref(oid, prefix=prefix)
        if callable(fallback_ref):
            return fallback_ref(oid, prefix=prefix)
        digest = str(oid or "")[:20]
        return f"{ARTIFACT_URI_PREFIX}cos/context_object-{digest}.json"

    def refresh_last_seen(
        self,
        raw_ref: str | None,
        *,
        now: float | None = None,
    ) -> bool:
        refreshed = False
        candidates: list[tuple[Any, str | None]] = [
            (self._primary, raw_ref),
            (self._fallback, raw_ref),
        ]
        local_ref = self._local_cos_ref(raw_ref)
        if local_ref is not None:
            candidates.append((self._fallback, local_ref))
        for store, candidate in candidates:
            toucher = getattr(store, "refresh_last_seen", None)
            if callable(toucher) and candidate and toucher(candidate, now=now):
                refreshed = True
        return refreshed

    def scan_metadata(self) -> list[ContextArtifactMetadata]:
        records: list[ContextArtifactMetadata] = []
        for store in (self._primary, self._fallback):
            scanner = getattr(store, "scan_metadata", None)
            if callable(scanner):
                records.extend(scanner())
        seen: set[tuple[str, str]] = set()
        unique: list[ContextArtifactMetadata] = []
        for metadata in records:
            key = (str(metadata.sha256 or ""), str(metadata.raw_ref or ""))
            if key in seen:
                continue
            seen.add(key)
            unique.append(metadata)
        return unique

    def delete(self, raw_ref: str | None) -> bool:
        removed = False
        for store in (self._primary, self._fallback):
            deleter = getattr(store, "delete", None)
            if callable(deleter) and deleter(raw_ref):
                removed = True
        # Also remove the fallback-written local copy addressed by its own
        # ref form; without it GC "successfully" deleted the (idempotent)
        # MinIO name every cycle while the local file leaked forever.
        local_ref = self._local_cos_ref(raw_ref)
        if local_ref is not None:
            deleter = getattr(self._fallback, "delete", None)
            if callable(deleter) and deleter(local_ref):
                removed = True
        return removed


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
