"""Backend-aware hybrid retrieval fusion for legacy + self-ingestion."""

from __future__ import annotations

import asyncio
import inspect
from dataclasses import dataclass
from typing import Any, Awaitable, Callable, Iterable, Sequence

from backend.config.loggers import get_logger
from backend.domain.knowledge_base.backend_policy import LEGACY_BACKEND, SELF_BACKEND
from backend.rag.ingestion.ids import sha256_text

logger = get_logger(__name__)

RetrieverCallable = Callable[[str, int], Awaitable[Sequence[Any]] | Sequence[Any]]


@dataclass(slots=True)
class HybridRetriever:
    """Merge legacy and self-ingestion retrieval results without changing either backend.

    Callers inject two retriever functions. This class only normalizes, deduplicates,
    and marks provenance, which keeps the legacy service path untouched until an
    explicit routing integration is enabled.
    """

    legacy_retriever: RetrieverCallable | None = None
    self_retriever: RetrieverCallable | None = None
    legacy_weight: float = 0.9
    self_weight: float = 1.0

    async def retrieve(self, query: str, top_k: int = 12) -> list[dict[str, Any]]:
        top_k = max(int(top_k or 12), 1)
        legacy_results, self_results = await self._run_backends(query, top_k)
        legacy_items = self._normalize_backend_items(
            legacy_results,
            backend=LEGACY_BACKEND,
            source_index_default="legacy",
            weight=self.legacy_weight,
        )
        self_items = self._normalize_backend_items(
            self_results,
            backend=SELF_BACKEND,
            source_index_default="chunks_v2",
            weight=self.self_weight,
        )
        merged = self.merge_ranked_results(legacy_items, self_items)
        merged.sort(key=lambda item: (-float(item.get("_hybrid_score") or 0.0), item.get("file_name") or ""))
        return merged[:top_k]

    async def _run_backends(self, query: str, top_k: int) -> tuple[Sequence[Any], Sequence[Any]]:
        async def _run(retriever: RetrieverCallable | None) -> Sequence[Any]:
            if retriever is None:
                return []
            result = retriever(query, top_k)
            if inspect.isawaitable(result):
                result = await result
            return list(result or [])

        legacy_results, self_results = await asyncio.gather(
            _run(self.legacy_retriever),
            _run(self.self_retriever),
        )
        return legacy_results, self_results

    @classmethod
    def merge_ranked_results(
        cls,
        legacy_items: Iterable[dict[str, Any]],
        self_items: Iterable[dict[str, Any]],
    ) -> list[dict[str, Any]]:
        by_key: dict[tuple[str, str], dict[str, Any]] = {}

        for item in list(legacy_items) + list(self_items):
            keys = cls._dedupe_keys(item)
            existing = next((by_key[key] for key in keys if key in by_key), None)
            if existing is None:
                item = cls._with_hybrid_markers(item)
                for key in keys:
                    by_key[key] = item
                continue

            merged = cls._merge_duplicate(existing, item)
            existing_keys = cls._dedupe_keys(existing)
            for key in set(keys + existing_keys):
                by_key[key] = merged

        unique: list[dict[str, Any]] = []
        seen_ids: set[int] = set()
        for item in by_key.values():
            marker = id(item)
            if marker in seen_ids:
                continue
            seen_ids.add(marker)
            unique.append(item)
        return unique

    @classmethod
    def _normalize_backend_items(
        cls,
        items: Sequence[Any],
        *,
        backend: str,
        source_index_default: str,
        weight: float,
    ) -> list[dict[str, Any]]:
        raw_items = [cls._to_dict(item) for item in items]
        scores = [cls._raw_score(item) for item in raw_items]
        max_score = max(scores, default=0.0) or 1.0
        normalized: list[dict[str, Any]] = []
        for item, score in zip(raw_items, scores):
            item = dict(item)
            item.setdefault("source_backend", backend)
            item.setdefault("source_type", backend)
            item.setdefault("source_index", source_index_default)
            item.setdefault("_source_backend_score", float(score))
            item["_normalized_backend_score"] = max(0.0, min(float(score) / max_score, 1.0))
            item["_hybrid_score"] = item["_normalized_backend_score"] * float(weight)
            normalized.append(item)
        return normalized

    @staticmethod
    def _to_dict(item: Any) -> dict[str, Any]:
        if isinstance(item, dict):
            return dict(item)
        if hasattr(item, "to_dict"):
            return dict(item.to_dict())
        return dict(getattr(item, "__dict__", {}) or {})

    @staticmethod
    def _raw_score(item: dict[str, Any]) -> float:
        for key in ("_chunk_score", "score", "_coverage_score", "_vec_score", "_hybrid_score"):
            value = item.get(key)
            if value is None:
                continue
            try:
                return float(value)
            except (TypeError, ValueError):
                continue
        return 0.0

    @classmethod
    def _dedupe_keys(cls, item: dict[str, Any]) -> list[tuple[str, str]]:
        keys: list[tuple[str, str]] = []
        doc_id = str(item.get("doc_id") or item.get("document_id") or "").strip()
        chunk_id = str(item.get("chunk_id") or item.get("id") or item.get("_id") or "").strip()
        if doc_id and chunk_id:
            keys.append(("doc_chunk", f"{doc_id}:{chunk_id}"))
        content_hash = str(item.get("content_hash") or "").strip()
        if not content_hash:
            content = str(item.get("content") or "").strip()
            if content:
                content_hash = sha256_text(" ".join(content.split()))
                item["content_hash"] = content_hash
        if doc_id and content_hash:
            keys.append(("doc_content_hash", f"{doc_id}:{content_hash}"))
        if content_hash:
            keys.append(("content_hash", content_hash))
        if not keys:
            keys.append(("object", str(id(item))))
        return keys

    @staticmethod
    def _with_hybrid_markers(item: dict[str, Any]) -> dict[str, Any]:
        backend = item.get("source_backend") or "unknown"
        item["_hybrid_backends"] = [backend]
        item["_hybrid_duplicate_count"] = 1
        return item

    @classmethod
    def _merge_duplicate(cls, existing: dict[str, Any], incoming: dict[str, Any]) -> dict[str, Any]:
        existing = cls._with_hybrid_markers(existing) if "_hybrid_backends" not in existing else existing
        incoming = cls._with_hybrid_markers(dict(incoming))
        existing_backend = existing.get("source_backend")
        incoming_backend = incoming.get("source_backend")

        keep_incoming = False
        if incoming_backend == SELF_BACKEND and existing_backend != SELF_BACKEND:
            keep_incoming = True
        elif incoming_backend == existing_backend:
            keep_incoming = float(incoming.get("_hybrid_score") or 0.0) > float(existing.get("_hybrid_score") or 0.0)

        primary = incoming if keep_incoming else existing
        secondary = existing if keep_incoming else incoming
        backends = list(dict.fromkeys(list(existing.get("_hybrid_backends") or []) + list(incoming.get("_hybrid_backends") or [])))
        primary["_hybrid_backends"] = backends
        primary["_hybrid_duplicate_count"] = int(existing.get("_hybrid_duplicate_count") or 1) + 1
        primary["_hybrid_score"] = max(float(existing.get("_hybrid_score") or 0.0), float(incoming.get("_hybrid_score") or 0.0))
        primary["_alternate_source_backend"] = secondary.get("source_backend")
        primary["_alternate_source_index"] = secondary.get("source_index")
        primary["_alternate_chunk_id"] = secondary.get("chunk_id") or secondary.get("id")
        return primary
