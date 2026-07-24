"""Redis BM25 inverted index for self-ingestion v2 keyword recall.

The default ``inverted`` mode stores a real postings index in Redis:

    token -> {chunk_id: term_frequency}

plus per-chunk document lengths and corpus statistics.  Query-time scoring uses
BM25 over only the union of matching postings, avoiding full corpus scans.
Optional RedisSearch (FT.SEARCH) remains available when the module is installed.
"""

from __future__ import annotations

import re
import math
import os
from collections import Counter
from typing import Sequence

from backend.config.loggers import get_logger
from backend.config.settings import settings
from backend.db.redis import get_redis
from backend.rag.ingestion.models import IngestChunk

logger = get_logger(__name__)

_LATIN_RE = re.compile(r"[A-Za-z0-9_\-]{2,}")
_CJK_RE = re.compile(r"[\u4e00-\u9fff]{2,}")
_NUMBER_RE = re.compile(
    r"\d+(?:\.\d+)?(?:\s*(?:kev|mev|gev|ev|ns|us|μs|ms|s|mm|cm|m|m/d|mg/l|%|℃|°c))?",
    re.IGNORECASE,
)
_MAX_TOKEN_LEN = 48
_MAX_POSTINGS_PER_TOKEN = 8000
_MAX_UNIQUE_TOKENS_PER_CHUNK = 3000
_BM25_K1 = 1.5
_BM25_B = 0.75


def _query_token_limit() -> int:
    try:
        return max(4, int(os.getenv("SELF_REDIS_BM25_QUERY_TOKEN_LIMIT", "32") or "32"))
    except (TypeError, ValueError):
        return 32


def _redis_batch_size() -> int:
    try:
        return max(int(getattr(settings, "self_redis_bm25_pipe_batch", 2000) or 2000), 200)
    except (TypeError, ValueError):
        return 2000


def _batched(values: Sequence[str], size: int):
    for offset in range(0, len(values), size):
        yield values[offset : offset + size]


def _tokens(text: str) -> list[str]:
    """Tokenize mixed Chinese/English text for Redis BM25.

    Chinese text is indexed with 2-6 char ngrams so a query term such as
    "渗透系数" can hit a longer sentence without requiring an external segmenter.
    """
    value = text or ""
    tokens: list[str] = []
    for match in _LATIN_RE.findall(value):
        tokens.append(match.lower()[:_MAX_TOKEN_LEN])
    for match in _NUMBER_RE.findall(value):
        tokens.append(" ".join(match.lower().split())[:_MAX_TOKEN_LEN])
    for match in _CJK_RE.findall(value):
        span = match[:200]
        max_ngram = 6 if len(span) <= 80 else 4
        for size in range(2, min(max_ngram, len(span)) + 1):
            for offset in range(0, len(span) - size + 1):
                tokens.append(span[offset : offset + size])
        if len(match) <= _MAX_TOKEN_LEN:
            tokens.append(match)
    return [token for token in tokens if len(token) >= 2]


def _query_tokens(query_tokens: list[str]) -> list[str]:
    """Prepare a compact, position-balanced query set for postings lookup.

    Indexing keeps 2-6 character CJK ngrams, but selecting the longest query
    ngrams first makes long questions brittle: the lookup budget is consumed by
    overlapping fragments from the first clause.  Query with character bigrams
    instead.  They cover the whole question, tolerate wording variation, and do
    not multiply the weight of one phrase through nested ngrams.
    """
    value = " ".join(str(token or "") for token in query_tokens)
    candidates: list[str] = []

    def add(token: str) -> None:
        normalized = token.strip().lower()[:_MAX_TOKEN_LEN]
        if len(normalized) >= 2 and normalized not in candidates:
            candidates.append(normalized)

    for token in _LATIN_RE.findall(value):
        add(token)
    for token in _NUMBER_RE.findall(value):
        add(" ".join(token.split()))
    for span in _CJK_RE.findall(value):
        # Preserve a compact exact technical term while avoiding full-clause
        # tokens that almost never match source text verbatim.
        if len(span) <= 6:
            add(span)
        for offset in range(max(len(span) - 1, 0)):
            add(span[offset : offset + 2])

    if not candidates:
        return []
    limit = _query_token_limit()
    if len(candidates) <= limit:
        return candidates

    # Uniform sampling retains evidence from the beginning, middle and end.
    # Keep Latin/numeric terms (often parameters or identifiers) before filling
    # the remaining budget with position-balanced CJK bigrams.
    latin_or_numeric = [
        token
        for token in candidates
        if _LATIN_RE.fullmatch(token) or _NUMBER_RE.fullmatch(token)
    ][: min(4, limit)]
    cjk = [token for token in candidates if token not in latin_or_numeric]
    remaining = limit - len(latin_or_numeric)
    if remaining <= 0 or not cjk:
        return latin_or_numeric[:limit]
    if remaining >= len(cjk):
        return (latin_or_numeric + cjk)[:limit]
    indexes = {
        round(position * (len(cjk) - 1) / max(remaining - 1, 1))
        for position in range(remaining)
    }
    sampled = [cjk[index] for index in sorted(indexes)]
    return (latin_or_numeric + sampled)[:limit]


def _term_counts(text: str) -> Counter[str]:
    counts = Counter(_tokens(text))
    if len(counts) <= _MAX_UNIQUE_TOKENS_PER_CHUNK:
        return counts
    return Counter(dict(counts.most_common(_MAX_UNIQUE_TOKENS_PER_CHUNK)))


def _prefix(kb_code: str) -> str:
    return f"{kb_code}:v2:bm25"


def _token_key(kb_code: str, token: str) -> str:
    return f"{_prefix(kb_code)}:posting:{token}"


def _doc_tokens_key(kb_code: str, doc_id: str) -> str:
    return f"{_prefix(kb_code)}:doc:{doc_id}:tokens"


def _doc_chunks_key(kb_code: str, doc_id: str) -> str:
    return f"{_prefix(kb_code)}:doc:{doc_id}:chunk_tokens"


def _chunk_len_key(kb_code: str) -> str:
    return f"{_prefix(kb_code)}:chunk_len"


def _stats_key(kb_code: str) -> str:
    return f"{_prefix(kb_code)}:stats"


def _decode(value) -> str:
    return value.decode("utf-8", errors="ignore") if isinstance(value, bytes) else str(value)


def _parse_int(value, default: int = 0) -> int:
    try:
        return int(float(_decode(value)))
    except (TypeError, ValueError):
        return default


async def _get_stats(client, kb_code: str) -> tuple[int, int, float]:
    stats = await client.hmget(_stats_key(kb_code), ["doc_count", "total_doc_len"])
    doc_count = max(_parse_int(stats[0] if stats else None), 0)
    total_doc_len = max(_parse_int(stats[1] if stats and len(stats) > 1 else None), 0)
    avgdl = total_doc_len / doc_count if doc_count else 1.0
    return doc_count, total_doc_len, avgdl


async def _set_stats(client, kb_code: str, doc_count: int, total_doc_len: int) -> None:
    await client.hset(
        _stats_key(kb_code),
        mapping={
            "doc_count": str(max(doc_count, 0)),
            "total_doc_len": str(max(total_doc_len, 0)),
        },
    )


async def _remove_existing_doc_index(client, kb_code: str, doc_id: str) -> None:
    doc_token_set = _doc_tokens_key(kb_code, doc_id)
    doc_chunk_map = _doc_chunks_key(kb_code, doc_id)
    chunk_len_key = _chunk_len_key(kb_code)
    old_tokens = await client.smembers(doc_token_set)
    old_chunk_payloads = await client.hgetall(doc_chunk_map)
    old_chunk_ids = [
        _decode(raw_chunk_id)
        for raw_chunk_id in (old_chunk_payloads or {}).keys()
    ]

    if old_tokens and old_chunk_ids:
        pipe = client.pipeline(transaction=False)
        batch_ops = 0
        max_pipe_ops = _redis_batch_size()
        chunk_id_batches = list(_batched(old_chunk_ids, max_pipe_ops))
        for raw_token in old_tokens:
            token = _decode(raw_token)
            token_key = _token_key(kb_code, token)
            for chunk_id_batch in chunk_id_batches:
                pipe.hdel(token_key, *chunk_id_batch)
                batch_ops += 1
                if batch_ops >= max_pipe_ops:
                    await pipe.execute()
                    pipe = client.pipeline(transaction=False)
                    batch_ops = 0
        if batch_ops:
            await pipe.execute()

    removed_len = 0
    for raw_payload in (old_chunk_payloads or {}).values():
        try:
            length_text, _ = _decode(raw_payload).split(":", 1)
        except ValueError:
            length_text = _decode(raw_payload)
        removed_len += _parse_int(length_text)

    if old_chunk_ids:
        pipe = client.pipeline(transaction=False)
        batch_ops = 0
        max_pipe_ops = _redis_batch_size()
        for chunk_id_batch in _batched(old_chunk_ids, max_pipe_ops):
            pipe.hdel(chunk_len_key, *chunk_id_batch)
            batch_ops += 1
            if batch_ops >= max_pipe_ops:
                await pipe.execute()
                pipe = client.pipeline(transaction=False)
                batch_ops = 0
        if batch_ops:
            await pipe.execute()

    doc_count, total_doc_len, _ = await _get_stats(client, kb_code)
    await _set_stats(
        client,
        kb_code,
        doc_count - len(old_chunk_ids),
        total_doc_len - removed_len,
    )
    await client.delete(doc_token_set, doc_chunk_map)


async def _redisearch_available(client) -> bool:
    mode = str(getattr(settings, "self_redis_bm25_mode", "inverted") or "inverted").lower()
    if mode != "redisearch":
        return False
    try:
        modules = await client.execute_command("MODULE", "LIST")
        for item in modules or []:
            if isinstance(item, (list, tuple)) and len(item) >= 2:
                name = item[1]
                if isinstance(name, bytes):
                    name = name.decode("utf-8", errors="ignore")
                if str(name).lower() == "search":
                    return True
    except Exception:
        return False
    return False


async def _ensure_redisearch_index(client, kb_code: str) -> str:
    index_name = f"idx_{kb_code}_v2_chunks".replace("-", "_")
    prefix = f"{_prefix(kb_code)}:rs:chunk:"
    try:
        await client.execute_command(
            "FT.CREATE",
            index_name,
            "ON", "HASH",
            "PREFIX", "1", prefix,
            "SCHEMA",
            "content", "TEXT",
            "chunk_id", "TAG",
            "doc_id", "TAG",
        )
    except Exception as exc:
        if "Index already exists" not in str(exc):
            logger.debug("[redis-bm25] FT.CREATE skipped: %s", exc)
    return index_name


async def queue_index_replacement(
    client,
    pipe,
    kb_code: str,
    doc_id: str,
    chunks: Sequence[IngestChunk],
) -> tuple[bool, str]:
    """Queue one document's complete BM25 replacement on an existing transaction."""
    corpus_key = f"{kb_code}:v2:bm25:chunk_corpus"
    doc_token_set = _doc_tokens_key(kb_code, doc_id)
    doc_chunk_map = _doc_chunks_key(kb_code, doc_id)
    use_rs = await _redisearch_available(client)
    rs_index = await _ensure_redisearch_index(client, kb_code) if use_rs else ""
    rs_prefix = f"{_prefix(kb_code)}:rs:chunk:"

    old_tokens = await client.smembers(doc_token_set)
    old_chunk_payloads = await client.hgetall(doc_chunk_map)
    old_chunk_ids = [
        _decode(raw_chunk_id)
        for raw_chunk_id in (old_chunk_payloads or {}).keys()
    ]
    removed_len = 0
    for raw_payload in (old_chunk_payloads or {}).values():
        try:
            length_text, _ = _decode(raw_payload).split(":", 1)
        except ValueError:
            length_text = _decode(raw_payload)
        removed_len += _parse_int(length_text)

    corpus_mapping: dict[str, str] = {}
    chunk_len_mapping: dict[str, str] = {}
    doc_chunk_mapping: dict[str, str] = {}
    postings_by_token: dict[str, dict[str, str]] = {}
    added_doc_len = 0
    indexed_chunk_count = 0

    for chunk in chunks:
        term_counts = _term_counts(chunk.content)
        if not term_counts:
            continue
        doc_len = int(sum(term_counts.values()))
        added_doc_len += doc_len
        indexed_chunk_count += 1
        chunk_len_mapping[chunk.chunk_id] = str(doc_len)
        doc_chunk_mapping[chunk.chunk_id] = (
            f"{doc_len}:"
            + ",".join(f"{token}={freq}" for token, freq in sorted(term_counts.items()))
        )
        for token, freq in term_counts.items():
            postings_by_token.setdefault(token, {})[chunk.chunk_id] = str(freq)
        corpus_mapping[chunk.chunk_id] = (chunk.content or "")[:4000]

    doc_count, total_doc_len, _ = await _get_stats(client, kb_code)
    old_chunk_batches = list(_batched(old_chunk_ids, _redis_batch_size()))
    if old_tokens and old_chunk_ids:
        for raw_token in old_tokens:
            token_key = _token_key(kb_code, _decode(raw_token))
            for chunk_id_batch in old_chunk_batches:
                pipe.hdel(token_key, *chunk_id_batch)
    for chunk_id_batch in old_chunk_batches:
        pipe.hdel(_chunk_len_key(kb_code), *chunk_id_batch)
        pipe.hdel(corpus_key, *chunk_id_batch)
        if use_rs:
            pipe.delete(*[f"{rs_prefix}{chunk_id}" for chunk_id in chunk_id_batch])
    pipe.delete(doc_token_set, doc_chunk_map)

    for token, posting_map in postings_by_token.items():
        pipe.hset(_token_key(kb_code, token), mapping=posting_map)
        pipe.sadd(doc_token_set, token)
    if corpus_mapping:
        pipe.hset(corpus_key, mapping=corpus_mapping)
    if chunk_len_mapping:
        pipe.hset(_chunk_len_key(kb_code), mapping=chunk_len_mapping)
    if doc_chunk_mapping:
        pipe.hset(doc_chunk_map, mapping=doc_chunk_mapping)
    if use_rs:
        for chunk in chunks:
            pipe.hset(
                f"{rs_prefix}{chunk.chunk_id}",
                mapping={
                    "chunk_id": chunk.chunk_id,
                    "doc_id": doc_id,
                    "content": (chunk.content or "")[:4000],
                },
            )
    pipe.hset(
        _stats_key(kb_code),
        mapping={
            "doc_count": str(
                max(doc_count - len(old_chunk_ids) + indexed_chunk_count, 0)
            ),
            "total_doc_len": str(
                max(total_doc_len - removed_len + added_doc_len, 0)
            ),
        },
    )
    return use_rs, rs_index


async def index_chunks(kb_code: str, doc_id: str, chunks: Sequence[IngestChunk]) -> None:
    """Atomically replace one document's inverted-index representation."""
    if not settings.redis_host or not chunks:
        return
    client = get_redis()
    try:
        pipe = client.pipeline(transaction=True)
        use_rs, rs_index = await queue_index_replacement(
            client,
            pipe,
            kb_code,
            doc_id,
            chunks,
        )
        await pipe.execute()
        if use_rs and rs_index:
            logger.debug("[redis-bm25] indexed %d chunks via RedisSearch index %s", len(chunks), rs_index)
    except Exception as exc:
        logger.warning("[redis-bm25] index_chunks failed: %s", exc)
        raise
    finally:
        await client.aclose()


async def remove_doc(kb_code: str, doc_id: str, chunk_ids: Sequence[str]) -> None:
    if not settings.redis_host:
        return
    client = get_redis()
    corpus_key = f"{kb_code}:v2:bm25:chunk_corpus"
    rs_prefix = f"{_prefix(kb_code)}:rs:chunk:"
    try:
        await _remove_existing_doc_index(client, kb_code, doc_id)
        if chunk_ids:
            pipe = client.pipeline(transaction=False)
            batch_ops = 0
            max_pipe_ops = _redis_batch_size()
            for chunk_id_batch in _batched(list(chunk_ids), max_pipe_ops):
                pipe.hdel(corpus_key, *chunk_id_batch)
                pipe.delete(*[f"{rs_prefix}{cid}" for cid in chunk_id_batch])
                batch_ops += 2
                if batch_ops >= max_pipe_ops:
                    await pipe.execute()
                    pipe = client.pipeline(transaction=False)
                    batch_ops = 0
            if batch_ops:
                await pipe.execute()
    except Exception as exc:
        logger.warning("[redis-bm25] remove_doc failed: %s", exc)
    finally:
        await client.aclose()


async def search_chunk_scores(
    kb_code: str,
    query_tokens: list[str],
    *,
    top_k: int = 12,
) -> dict[str, float]:
    """Return BM25 scores for candidate chunk_ids."""
    if not query_tokens or not settings.redis_host:
        return {}
    tokens = _query_tokens(query_tokens)
    if not tokens:
        return {}

    client = get_redis()
    try:
        mode = str(getattr(settings, "self_redis_bm25_mode", "inverted") or "inverted").lower()
        if mode == "scan":
            return {}

        if mode == "redisearch" and await _redisearch_available(client):
            index_name = f"idx_{kb_code}_v2_chunks".replace("-", "_")
            query = " | ".join(tokens)
            try:
                result = await client.execute_command(
                    "FT.SEARCH", index_name, query, "LIMIT", "0", str(max(top_k * 10, 30)),
                )
                chunk_ids: list[str] = []
                if isinstance(result, (list, tuple)) and len(result) >= 1:
                    for i in range(1, len(result), 2):
                        fields = result[i + 1] if i + 1 < len(result) else []
                        cid = None
                        if isinstance(fields, (list, tuple)):
                            for j in range(0, len(fields) - 1, 2):
                                key = fields[j]
                                val = fields[j + 1]
                                if (key.decode() if isinstance(key, bytes) else key) == "chunk_id":
                                    cid = val.decode() if isinstance(val, bytes) else str(val)
                                    break
                        if cid:
                            chunk_ids.append(cid)
                return {
                    chunk_id: 1.0 / (rank + 1)
                    for rank, chunk_id in enumerate(chunk_ids)
                }
            except Exception as exc:
                logger.debug("[redis-bm25] RedisSearch query failed, fallback inverted: %s", exc)

        # Inverted BM25: union postings, score from token tf, df and chunk length.
        doc_count, _, avgdl = await _get_stats(client, kb_code)
        if doc_count <= 0:
            return {}
        postings_by_token: dict[str, dict[str, int]] = {}
        candidate_ids: set[str] = set()
        token_keys = {token: _token_key(kb_code, token) for token in tokens}
        cursors: dict[str, int | bytes] = {token: 0 for token in tokens}
        scanned_by_token = {token: 0 for token in tokens}
        postings_by_token = {token: {} for token in tokens}
        active_tokens = list(tokens)
        while active_tokens:
            try:
                pipe = client.pipeline(transaction=False)
                for token in active_tokens:
                    pipe.hscan(token_keys[token], cursor=cursors[token], count=500)
                scan_results = await pipe.execute()
                if len(scan_results) != len(active_tokens):
                    raise ValueError("Redis pipeline returned incomplete HSCAN results")
            except (AttributeError, TypeError, ValueError):
                # Some lightweight/legacy Redis clients do not expose HSCAN
                # results through pipelines. Keep those clients functional
                # while using the batched path for normal redis-py clients.
                scan_results = [
                    await client.hscan(
                        token_keys[token],
                        cursor=cursors[token],
                        count=500,
                    )
                    for token in active_tokens
                ]
            next_active: list[str] = []
            for token, (cursor, batch) in zip(
                active_tokens,
                scan_results,
                strict=False,
            ):
                postings = postings_by_token[token]
                for raw_chunk_id, raw_tf in (batch or {}).items():
                    cid = _decode(raw_chunk_id)
                    tf = _parse_int(raw_tf)
                    if tf > 0:
                        postings[cid] = tf
                        candidate_ids.add(cid)
                scanned_by_token[token] += len(batch or {})
                cursors[token] = cursor
                if (
                    cursor not in (0, b"0")
                    and scanned_by_token[token] < _MAX_POSTINGS_PER_TOKEN
                ):
                    next_active.append(token)
            active_tokens = next_active
        postings_by_token = {
            token: postings
            for token, postings in postings_by_token.items()
            if postings
        }
        if not candidate_ids:
            return {}

        chunk_ids = list(candidate_ids)
        raw_lengths = await client.hmget(_chunk_len_key(kb_code), chunk_ids)
        lengths = {
            chunk_id: max(_parse_int(raw_len, 1), 1)
            for chunk_id, raw_len in zip(chunk_ids, raw_lengths, strict=False)
        }

        scores: dict[str, float] = {}
        for postings in postings_by_token.values():
            df = max(len(postings), 1)
            idf = math.log((doc_count - df + 0.5) / (df + 0.5) + 1.0)
            for chunk_id, tf in postings.items():
                doc_len = lengths.get(chunk_id, 1)
                denom = tf + _BM25_K1 * (1.0 - _BM25_B + _BM25_B * doc_len / max(avgdl, 1.0))
                scores[chunk_id] = scores.get(chunk_id, 0.0) + idf * (tf * (_BM25_K1 + 1.0)) / denom
        ranked = sorted(scores.items(), key=lambda x: x[1], reverse=True)
        return dict(ranked[: max(top_k * 8, top_k)])
    except Exception as exc:
        logger.debug("[redis-bm25] search_chunk_ids failed: %s", exc)
        return {}
    finally:
        await client.aclose()


async def search_chunk_ids(
    kb_code: str,
    query_tokens: list[str],
    *,
    top_k: int = 12,
) -> list[str]:
    """Return candidate chunk_ids for callers that only need IDs."""
    scores = await search_chunk_scores(kb_code, query_tokens, top_k=top_k)
    return list(scores)
