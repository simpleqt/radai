"""Context Object Store (COS) built on the existing artifact byte layer."""

from __future__ import annotations

import json
import logging
import os
import re
import time
from concurrent.futures import ThreadPoolExecutor
from typing import Any

from backend.core.agents.context.artifact_store import (
    ContextArtifactMetadata,
    ContextArtifactStore,
    create_context_artifact_store,
)
from backend.core.agents.context.objects import (
    ContextObject,
    Segment,
    content_digest,
    find_segment,
)

logger = logging.getLogger(__name__)

OBJECT_SCHEMA_PREFIX = "context_object"
OBJECT_ATTRIBUTE_KEY = "context_object_oid"


def _object_raw_ref(oid: str) -> str:
    """Deterministic local artifact ref; backend-aware refs use store.object_ref."""

    digest = str(oid or "")[:20]
    return f"artifact://context/cos/{OBJECT_SCHEMA_PREFIX}-{digest}.json"


class ContextObjectStore:
    """Persist, read, scan and garbage-collect immutable context objects."""

    def __init__(
        self,
        artifact_store: ContextArtifactStore | None = None,
        *,
        object_ttl_seconds: int | None = None,
    ) -> None:
        self._store = artifact_store or create_context_artifact_store()
        self._object_ttl_seconds = max(
            0,
            int(
                object_ttl_seconds
                if object_ttl_seconds is not None
                else os.getenv("CONTEXT_OBJECT_TTL_SECONDS", str(14 * 86400))
            ),
        )
        # Request-scoped read cache.  The store instance is created per
        # middleware/executor (request or nested unit), so the cache lifetime
        # is naturally bounded.  Objects are content-addressed and immutable;
        # ``put`` refreshes the merged entry so a write is never followed by a
        # stale read.
        self._read_cache: dict[str, ContextObject] = {}
        self._read_count = 0
        self._cache_hit_count = 0

    @property
    def artifact_store(self) -> ContextArtifactStore:
        return self._store

    def put(self, obj: ContextObject) -> str:
        """Persist an object and return its content-addressed oid."""

        if not isinstance(obj, dict):
            raise TypeError("ContextObject must be a mapping")
        normalized = dict(obj)
        oid = str(normalized.get("oid") or content_digest(normalized)).strip()
        if not re.fullmatch(r"[0-9a-f]{64}", oid):
            oid = content_digest(normalized)
        # The oid is content-addressed over the body; keep it out of the
        # stored payload so sha256(body) == oid and the artifact digest
        # matches the deterministic raw_ref. Origin metadata lives in the
        # artifact sidecar so it can be restored without changing identity.
        normalized.pop("oid", None)
        origin = dict(normalized.pop("origin", {}) or {})
        payload = json.dumps(
            {
                key: normalized.get(key)
                for key in ("schema", "index", "segments", "derived_from")
                if normalized.get(key) is not None
            },
            ensure_ascii=False,
            sort_keys=True,
            separators=(",", ":"),
            default=str,
        )
        attributes: dict[str, Any] = {
            OBJECT_ATTRIBUTE_KEY: oid,
            "schema": str(normalized.get("schema") or "text/v1"),
            "segment_count": len(normalized.get("segments") or []),
            "origin": dict(origin),
            "turn_id": str(origin.get("turn_id") or ""),
            "last_seen_at": time.time(),
        }
        owner_user = str(origin.get("user_id") or "").strip()
        if owner_user:
            attributes["owner_user_id"] = owner_user
        owner_thread = str(origin.get("thread_id") or "").strip()
        if owner_thread:
            attributes["owner_thread_id"] = owner_thread
        # 多属主：内容寻址去重会让相同工具输出跨用户/线程共用同一 sidecar。owners
        # 追加当前 (user, thread)，artifact 层做并集，context_read 校验请求者是否
        # 在集合内，避免后写者夺属主导致先前所有者取不回证据。
        if owner_user or owner_thread:
            attributes["owners"] = [
                {"user_id": owner_user, "thread_id": owner_thread}
            ]
        self._store.store(
            payload,
            thread_id="cos",
            source_tool=OBJECT_SCHEMA_PREFIX,
            suffix=".json",
            attributes=attributes,
        )
        # Backfill the request-level cache with the same merged shape that
        # ``read`` returns (payload + oid + sidecar origin), so a put is never
        # followed by a stale read.
        cached: dict[str, Any] = {
            key: normalized.get(key)
            for key in ("schema", "index", "segments", "derived_from")
            if normalized.get(key) is not None
        }
        cached["oid"] = oid
        cached["origin"] = dict(attributes.get("origin") or {})
        cached["owners"] = [
            dict(owner)
            for owner in (attributes.get("owners") or [])
            if isinstance(owner, dict)
        ]
        self._read_cache[oid] = ContextObject(cached)
        logger.debug(
            "[COS] put object: oid=%s schema=%s segments=%d",
            oid,
            normalized.get("schema"),
            len(normalized.get("segments") or []),
        )
        return oid

    def read(self, oid: str) -> ContextObject | None:
        oid = str(oid or "")
        if oid in self._read_cache:
            self._cache_hit_count += 1
            return self._read_cache[oid]
        self._read_count += 1
        restored_obj = self._read_uncached(oid)
        if restored_obj is None:
            return None
        self._read_cache[oid] = restored_obj
        return restored_obj

    def read_many(self, oids: list[str]) -> dict[str, ContextObject]:
        """Read several objects, fetching cache misses concurrently.

        The first JIT projection of a request resolves every state ref in one
        pass; each artifact-layer miss costs one content read plus one
        metadata read (two network round trips on the MinIO backend), so
        misses run on a bounded local thread pool. ``_read_uncached`` touches
        no shared state; cache/counter updates happen on the calling thread.
        """

        wanted = [oid for oid in dict.fromkeys(str(o or "") for o in oids) if oid]
        results: dict[str, ContextObject] = {}
        misses: list[str] = []
        for oid in wanted:
            cached = self._read_cache.get(oid)
            if cached is not None:
                self._cache_hit_count += 1
                results[oid] = cached
            else:
                misses.append(oid)
        if not misses:
            return results
        if len(misses) == 1:
            fetched: list[tuple[str, ContextObject | None]] = [
                (misses[0], self._read_uncached(misses[0]))
            ]
        else:
            with ThreadPoolExecutor(
                max_workers=min(8, len(misses)),
                thread_name_prefix="cos-read",
            ) as pool:
                fetched = list(
                    pool.map(lambda oid: (oid, self._read_uncached(oid)), misses)
                )
        for oid, obj in fetched:
            self._read_count += 1
            if obj is None:
                continue
            self._read_cache[oid] = obj
            results[oid] = obj
        return results

    def _read_uncached(self, oid: str) -> ContextObject | None:
        """Fetch and restore one object without touching cache or counters."""

        raw_ref = self._object_ref(oid)
        content = self._store.read(raw_ref)
        if not content:
            return None
        try:
            payload = json.loads(content)
        except (TypeError, json.JSONDecodeError):
            return None
        if not isinstance(payload, dict):
            return None
        metadata = self._store.metadata(raw_ref)
        attributes = (
            metadata.attributes if metadata is not None and isinstance(metadata.attributes, dict) else {}
        )
        self._maybe_refresh_last_seen(raw_ref, attributes)
        restored = dict(payload)
        restored["oid"] = oid
        restored["origin"] = dict(attributes.get("origin") or {})
        restored["owners"] = [
            dict(owner)
            for owner in (attributes.get("owners") or [])
            if isinstance(owner, dict)
        ]
        return ContextObject(restored)

    def stats(self) -> dict[str, int]:
        """Return request-level cache/read accounting for observability."""

        return {
            "objects_read": self._read_count,
            "object_cache_hits": self._cache_hit_count,
        }

    def manifest(self, oid: str) -> ContextObject | None:
        return self.read(oid)

    def read_segments(self, oid: str, sids: list[str] | None = None) -> list[Segment]:
        obj = self.read(oid)
        if obj is None:
            return []
        if not sids:
            return list(obj.get("segments") or [])
        wanted = {str(sid) for sid in sids if str(sid).strip()}
        return [
            segment
            for segment in obj.get("segments") or []
            if str(segment.get("sid") or "") in wanted
        ]

    def raw_ref(self, oid: str) -> str:
        return self._object_ref(str(oid or ""))

    def get_segment(self, oid: str, sid: str) -> Segment | None:
        obj = self.read(oid)
        return find_segment(obj, sid) if obj is not None else None

    def scan(self, pattern: str = "") -> list[dict[str, Any]]:
        """Return lightweight object manifests matching an optional oid prefix."""

        records: list[dict[str, Any]] = []
        for metadata in self._scan_metadata():
            attributes = (
                metadata.attributes if isinstance(metadata.attributes, dict) else {}
            )
            oid = str(attributes.get(OBJECT_ATTRIBUTE_KEY) or "").strip()
            if not oid or str(metadata.source_tool or "") != OBJECT_SCHEMA_PREFIX:
                continue
            if pattern and not oid.startswith(str(pattern).strip()):
                continue
            records.append(
                {
                    "oid": oid,
                    "raw_ref": metadata.raw_ref,
                    "schema": str(attributes.get("schema") or ""),
                    "segment_count": int(attributes.get("segment_count") or 0),
                    "created_at": metadata.created_at,
                    "last_seen_at": float(
                        attributes.get("last_seen_at") or metadata.created_at
                    ),
                    "turn_ids": list(attributes.get("turn_ids") or []),
                    "thread_id": metadata.thread_id,
                    "owner_user_id": str(attributes.get("owner_user_id") or ""),
                    "owner_thread_id": str(attributes.get("owner_thread_id") or ""),
                }
            )
        return records

    def delete(self, oid: str) -> bool:
        oid = str(oid or "")
        raw_ref = self._object_ref(oid)
        removed = bool(self._delete_ref(raw_ref))
        if removed:
            self._read_cache.pop(oid, None)
        return removed

    def gc(
        self,
        *,
        ttl_seconds: int | None = None,
        now: float | None = None,
    ) -> dict[str, Any]:
        """Delete expired context objects and return a small accounting report."""

        ttl = max(
            0,
            int(ttl_seconds if ttl_seconds is not None else self._object_ttl_seconds),
        )
        current = float(now if now is not None else time.time())
        removed: list[str] = []
        kept = 0
        for entry in self.scan():
            last_seen = float(entry.get("last_seen_at") or entry.get("created_at") or current)
            age = current - last_seen
            if ttl and age > ttl:
                if self.delete(str(entry.get("oid") or "")):
                    removed.append(str(entry.get("oid")))
                    continue
            kept += 1
        return {
            "scanned": kept + len(removed),
            "kept": kept,
            "removed": removed,
            "ttl_seconds": ttl,
            "generated_at": current,
        }

    def _scan_metadata(self) -> list[ContextArtifactMetadata]:
        scanner = getattr(self._store, "scan_metadata", None)
        if callable(scanner):
            return scanner()
        return []

    def _maybe_refresh_last_seen(
        self,
        raw_ref: str,
        attributes: dict[str, Any],
    ) -> None:
        """Throttled read-side sidecar refresh so GC ages by real activity.

        ``last_seen_at`` was only written on ``put``: an object a long-lived
        thread kept projecting/reading (but never re-captured) still expired
        at the TTL, leaving dangling state refs and ledger sids. Refreshing at
        most once per interval bounds the extra sidecar writes; the file lock
        serializes concurrent refreshes (safe from ``read_many`` worker
        threads).
        """

        if self._object_ttl_seconds <= 0:
            return
        toucher = getattr(self._store, "refresh_last_seen", None)
        if not callable(toucher):
            return
        interval = max(3600, self._object_ttl_seconds // 14)
        last_seen = float(attributes.get("last_seen_at") or 0.0)
        if last_seen and time.time() - last_seen < interval:
            return
        try:
            toucher(raw_ref)
        except Exception:
            logger.debug(
                "[COS] read-side last_seen refresh failed: ref=%s",
                raw_ref,
                exc_info=True,
            )

    def _object_ref(self, oid: str) -> str:
        resolver = getattr(self._store, "object_ref", None)
        if callable(resolver):
            return resolver(oid, prefix=OBJECT_SCHEMA_PREFIX)
        return _object_raw_ref(oid)

    def _delete_ref(self, raw_ref: str) -> bool:
        deleter = getattr(self._store, "delete", None)
        if callable(deleter):
            return bool(deleter(raw_ref))
        return False


__all__ = ["ContextObjectStore"]
