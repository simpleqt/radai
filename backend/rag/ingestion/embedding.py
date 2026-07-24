"""Embedding helpers for self-ingestion and v2 retrieval."""

from __future__ import annotations

import asyncio
import hashlib
import logging
import os
import random
import re
from typing import Sequence

import httpx
import numpy as np

from backend.config.settings import settings
from backend.rag.ingestion.concurrency import (
    embedding_concurrency,
    media_embedding_concurrency,
    semaphore,
)

logger = logging.getLogger(__name__)

_DEFAULT_BATCH_SIZE = 32
_DEFAULT_MAX_RETRIES = 3
_DEFAULT_TIMEOUT_S = 120.0
_BATCH_INTERVAL_S = 0.1
_EMBED_PLACEHOLDER = " "


def _positive_int_env(name: str, default: int) -> int:
    try:
        value = int(os.getenv(name, "") or default)
    except (TypeError, ValueError):
        value = default
    return max(value, 1)


def _positive_float_env(name: str, default: float) -> float:
    try:
        value = float(os.getenv(name, "") or default)
    except (TypeError, ValueError):
        value = default
    return max(value, 1.0)


def _embedding_timeout() -> float:
    return _positive_float_env("SELF_INGESTION_EMBEDDING_TIMEOUT", _DEFAULT_TIMEOUT_S)


def _embedding_max_retries() -> int:
    return _positive_int_env("SELF_INGESTION_EMBEDDING_MAX_RETRIES", _DEFAULT_MAX_RETRIES)


def _embedding_min_concurrency() -> int:
    return _positive_int_env("SELF_INGESTION_EMBEDDING_MIN_CONCURRENCY", 1)


def _is_retryable_embedding_error(exc: BaseException) -> bool:
    if isinstance(
        exc,
        (
            httpx.TimeoutException,
            httpx.ConnectError,
            httpx.NetworkError,
            httpx.RemoteProtocolError,
        ),
    ):
        return True
    if isinstance(exc, httpx.HTTPStatusError):
        status = exc.response.status_code
        return status == 429 or 500 <= status < 600
    return False


def require_embedding_success() -> bool:
    raw = os.getenv("SELF_INGESTION_REQUIRE_EMBEDDING", "")
    return raw.strip().lower() in {"1", "true", "yes", "on"}


def _normalize_embed_input(text: str) -> str:
    """Avoid empty inputs that some embedding APIs may skip or collapse."""
    normalized = (text or "").strip()
    return normalized if normalized else _EMBED_PLACEHOLDER


def _normalize_media_embed_input(text: str) -> str:
    """Prefer structured image descriptions and bound OCR-heavy model input."""
    normalized = _normalize_embed_input(text)
    descriptions = re.findall(
        r"图片描述:\s*(.*?)(?=\n(?:上下文|图片描述|章节|公式|变量说明|媒体编号):|\Z)",
        normalized,
        flags=re.DOTALL,
    )
    if descriptions:
        normalized = "\n".join(
            description.strip() for description in descriptions if description.strip()
        ) or normalized
    max_chars = _positive_int_env("MEDIA_EMBEDDING_MAX_CHARS", 4096)
    return normalized[:max_chars]


def _configured_dim(default: int = 384) -> int:
    raw = settings.embedding_config.get("dim") or os.getenv("EMBEDDING_DIM")
    try:
        return int(raw) if raw else default
    except (TypeError, ValueError):
        return default


def mock_embedding(texts: Sequence[str], dim: int | None = None) -> list[list[float]]:
    size = int(dim or _configured_dim())
    vectors: list[list[float]] = []
    for text in texts:
        vec = np.zeros(size, dtype=np.float32)
        tokens = re.findall(r"\w+|[\u4e00-\u9fff]", (text or "").lower())
        if not tokens:
            vectors.append(vec.tolist())
            continue
        for token in tokens:
            digest = hashlib.sha256(token.encode("utf-8")).digest()
            idx = int.from_bytes(digest[:4], "big") % size
            sign = 1.0 if digest[4] % 2 == 0 else -1.0
            vec[idx] += sign
        norm = float(np.linalg.norm(vec))
        if norm > 0:
            vec /= norm
        vectors.append(vec.tolist())
    return vectors


async def _embed_batch(
    texts: Sequence[str],
    url: str,
    headers: dict[str, str],
    model: str,
    max_retries: int | None = None,
    client: httpx.AsyncClient | None = None,
) -> list[list[float]]:
    """Embed a single batch with retry and exponential backoff."""
    last_error: Exception | None = None
    retries = max(max_retries or _embedding_max_retries(), 1)
    owns_client = client is None
    if client is None:
        client = httpx.AsyncClient(trust_env=False, timeout=_embedding_timeout())
    try:
        for attempt in range(retries):
            try:
                response = await client.post(
                    url,
                    headers=headers,
                    json={"model": model, "input": list(texts), "encoding_format": "float"},
                )
                response.raise_for_status()
                payload = response.json()
                items = sorted(
                    payload.get("data") or [],
                    key=lambda item: int(item.get("index", 0)),
                )
                vectors = [item.get("embedding") or [] for item in items]
                if len(vectors) != len(texts) or any(not v for v in vectors):
                    raise RuntimeError("嵌入服务返回数据不完整")
                return vectors
            except Exception as exc:
                last_error = exc
                if attempt < retries - 1:
                    backoff = min(2 ** attempt, 8)
                    wait = random.uniform(backoff * 0.5, backoff)
                    logger.warning(
                        "[embedding] batch retry %d/%d after %.1fs: %s",
                        attempt + 1, retries, wait, exc,
                    )
                    await asyncio.sleep(wait)
        raise last_error  # type: ignore[misc]
    finally:
        if owns_client:
            await client.aclose()


async def _embed_batches_adaptive(
    batches: list[tuple[int, Sequence[str]]],
    *,
    total_texts: int,
    batch_size: int,
    url: str,
    headers: dict[str, str],
    model: str,
    resource_name: str = "self_ingestion_embedding",
    concurrency_limit: int | None = None,
    client: httpx.AsyncClient | None = None,
) -> list[list[float]]:
    """Run embedding batches with adaptive concurrency downgrade on transient errors."""
    resource_limit = max(int(concurrency_limit or embedding_concurrency()), 1)
    configured_limit = min(len(batches), resource_limit)
    current_limit = configured_limit
    min_limit = min(_embedding_min_concurrency(), configured_limit)
    max_attempts = _embedding_max_retries()
    pending: dict[int, Sequence[str]] = dict(batches)
    attempts: dict[int, int] = {index: 0 for index, _ in batches}
    completed: dict[int, list[list[float]]] = {}
    success_windows = 0

    async def run_batch(index: int, batch: Sequence[str]) -> tuple[int, list[list[float]]]:
        vectors = await _embed_batch(
            batch,
            url,
            headers,
            model,
            max_retries=1,
            client=client,
        )
        if index + batch_size < total_texts:
            await asyncio.sleep(_BATCH_INTERVAL_S)
        return index, vectors

    while pending:
        window = list(sorted(pending.items()))[:current_limit]
        for index, _ in window:
            pending.pop(index, None)

        batch_semaphore = await semaphore(
            resource_name,
            resource_limit,
        )

        async def guarded(index: int, batch: Sequence[str]):
            async with batch_semaphore:
                return await run_batch(index, batch)

        results = await asyncio.gather(
            *(guarded(index, batch) for index, batch in window),
            return_exceptions=True,
        )

        transient_failure = False
        for (index, batch), result in zip(window, results):
            if not isinstance(result, Exception):
                completed[result[0]] = result[1]
                continue

            attempts[index] += 1
            if attempts[index] >= max_attempts or not _is_retryable_embedding_error(result):
                raise result
            pending[index] = batch
            transient_failure = True

        if transient_failure:
            success_windows = 0
            old_limit = current_limit
            current_limit = max(min_limit, max(1, current_limit // 2))
            logger.warning(
                "[embedding] transient failure; downgrade concurrency %d -> %d, pending_batches=%d",
                old_limit,
                current_limit,
                len(pending),
            )
            await asyncio.sleep(min(2 ** min(max(attempts.values(), default=1), 3), 8))
        else:
            success_windows += 1
            if success_windows >= 2 and current_limit < configured_limit:
                current_limit += 1
                success_windows = 0
                logger.info(
                    "[embedding] recovered; increase concurrency to %d/%d",
                    current_limit,
                    configured_limit,
                )

    vectors: list[list[float]] = []
    for _, batch_vectors in sorted(completed.items(), key=lambda item: item[0]):
        vectors.extend(batch_vectors)
    return vectors


async def _embed_sequential(
    texts: Sequence[str],
    url: str,
    headers: dict[str, str],
    model: str,
    client: httpx.AsyncClient | None = None,
) -> list[list[float]]:
    """Fallback: embed one text at a time to guarantee output alignment."""
    aligned: list[list[float]] = []
    for text in texts:
        try:
            batch = await _embed_batch([text], url, headers, model, client=client)
            aligned.append(batch[0])
        except Exception as exc:
            logger.warning("[embedding] 单条嵌入失败，使用 mock 向量: %s", exc)
            aligned.append(mock_embedding([text])[0])
    return aligned


async def embed_texts(
    texts: Sequence[str],
    *,
    batch_size: int | None = None,
) -> list[list[float]]:
    """Embed texts with automatic batching and per-batch retry."""
    if not texts:
        return []

    normalized = [_normalize_embed_input(text) for text in texts]

    embedding_config = settings.embedding_config
    api_base = (embedding_config.get("api_base") or os.getenv("EMBEDDING_API_BASE") or "").rstrip("/")
    api_key = embedding_config.get("api_key") or os.getenv("EMBEDDING_API_KEY")
    model = embedding_config.get("model") or os.getenv("EMBEDDING_MODEL")

    if not api_base or not model:
        return mock_embedding(normalized)

    url = f"{api_base}/embeddings" if not api_base.endswith("/embeddings") else api_base
    headers: dict[str, str] = {"Content-Type": "application/json"}
    if api_key:
        headers["Authorization"] = f"Bearer {api_key}"

    bs = int(
        batch_size
        or os.getenv("SELF_INGESTION_EMBEDDING_BATCH_SIZE")
        or settings.rag_ingestion_config.get("chunking", {}).get("embedding_batch_size")
        or embedding_config.get("batch_size")
        or _DEFAULT_BATCH_SIZE
    )
    bs = max(bs, 1)

    resource_limit = embedding_concurrency()
    async with httpx.AsyncClient(
        trust_env=False,
        timeout=_embedding_timeout(),
    ) as client:
        if len(normalized) <= bs:
            batch_semaphore = await semaphore("self_ingestion_embedding", resource_limit)
            async with batch_semaphore:
                vectors = await _embed_batch(
                    normalized,
                    url,
                    headers,
                    model,
                    client=client,
                )
        else:
            batches = [
                (index, normalized[index : index + bs])
                for index in range(0, len(normalized), bs)
            ]
            vectors = await _embed_batches_adaptive(
                batches,
                total_texts=len(normalized),
                batch_size=bs,
                url=url,
                headers=headers,
                model=model,
                resource_name="self_ingestion_embedding",
                concurrency_limit=resource_limit,
                client=client,
            )

        if len(vectors) != len(normalized):
            logger.warning(
                "[embedding] 批量嵌入数量不齐 texts=%d vectors=%d，逐条重试",
                len(normalized), len(vectors),
            )
            vectors = await _embed_sequential(
                normalized,
                url,
                headers,
                model,
                client=client,
            )

    return vectors


def media_embedding_enabled() -> bool:
    raw = os.getenv("MEDIA_EMBEDDING_ENABLED", "")
    if raw:
        return raw.strip().lower() in {"1", "true", "yes", "on"}
    configured_api_base = (
        os.getenv("MEDIA_EMBEDDING_API_BASE")
        or settings.vl_embedding_config.get("api_base")
        or ""
    )
    return bool(str(configured_api_base).strip())


async def embed_media_texts(
    texts: Sequence[str],
    *,
    batch_size: int | None = None,
) -> list[list[float]]:
    """Embed media chunks with the dedicated media/VL embedding endpoint."""
    if not texts:
        return []
    if not media_embedding_enabled():
        return []

    normalized = [_normalize_media_embed_input(text) for text in texts]
    media_config = settings.vl_embedding_config
    api_base = str(
        os.getenv("MEDIA_EMBEDDING_API_BASE")
        or media_config.get("api_base")
        or ""
    ).rstrip("/")
    api_key = os.getenv("MEDIA_EMBEDDING_API_KEY")
    model = (
        os.getenv("MEDIA_EMBEDDING_MODEL")
        or media_config.get("model")
        or "Qwen3-VL-Embedding-8B"
    )
    if not api_base or not model:
        return []

    url = f"{api_base}/embeddings" if not api_base.endswith("/embeddings") else api_base
    headers: dict[str, str] = {"Content-Type": "application/json"}
    if api_key:
        headers["Authorization"] = f"Bearer {api_key}"

    bs = int(batch_size or os.getenv("MEDIA_EMBEDDING_BATCH_SIZE") or 8)
    bs = max(bs, 1)
    resource_limit = media_embedding_concurrency()
    async with httpx.AsyncClient(
        trust_env=False,
        timeout=_embedding_timeout(),
    ) as client:
        if len(normalized) <= bs:
            batch_semaphore = await semaphore(
                "self_ingestion_media_embedding",
                resource_limit,
            )
            async with batch_semaphore:
                vectors = await _embed_batch(
                    normalized,
                    url,
                    headers,
                    model,
                    client=client,
                )
        else:
            batches = [
                (index, normalized[index : index + bs])
                for index in range(0, len(normalized), bs)
            ]
            vectors = await _embed_batches_adaptive(
                batches,
                total_texts=len(normalized),
                batch_size=bs,
                url=url,
                headers=headers,
                model=model,
                resource_name="self_ingestion_media_embedding",
                concurrency_limit=resource_limit,
                client=client,
            )
        if len(vectors) != len(normalized):
            logger.warning(
                "[media-embedding] 批量嵌入数量不齐 texts=%d vectors=%d，逐条重试",
                len(normalized), len(vectors),
            )
            vectors = await _embed_sequential(
                normalized,
                url,
                headers,
                model,
                client=client,
            )
    return vectors
