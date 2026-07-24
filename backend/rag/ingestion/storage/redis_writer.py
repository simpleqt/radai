"""Redis metadata writer for self-ingestion."""

from __future__ import annotations

import asyncio
import json
from datetime import date, datetime
from typing import Sequence

from backend.config.loggers import get_logger
from backend.config.settings import settings
from backend.db.redis import get_redis
from backend.rag.ingestion import states
from backend.rag.ingestion.ids import sha256_text
from backend.rag.ingestion.storage.redis_bm25_index import queue_index_replacement
from backend.rag.ingestion.storage.redis_bm25_index import remove_doc as remove_bm25_doc
from backend.rag.ingestion.models import IngestChunk

logger = get_logger(__name__)


def _redis_batch_size() -> int:
    try:
        return max(int(getattr(settings, "self_redis_bm25_pipe_batch", 2000) or 2000), 200)
    except (TypeError, ValueError):
        return 2000


def _batched(values: Sequence[str], size: int):
    for offset in range(0, len(values), size):
        yield values[offset : offset + size]


def _to_redis_value(value) -> str:
    if value is None:
        return ""
    if isinstance(value, (datetime, date)):
        return value.isoformat()
    if isinstance(value, (list, tuple, set, dict)):
        return json.dumps(value, ensure_ascii=False, sort_keys=True)
    return str(value)


def _from_redis_value(value) -> str:
    if isinstance(value, bytes):
        return value.decode("utf-8", errors="ignore")
    return str(value or "")


async def _redis_set_members(client, key: str) -> set[str]:
    try:
        members = await client.smembers(key)
    except AttributeError:
        return set()
    return {_from_redis_value(member) for member in members}


async def _redis_list_values(client, key: str) -> list[str]:
    try:
        values = await client.lrange(key, 0, -1)
    except AttributeError:
        return []
    return [_from_redis_value(value) for value in values if _from_redis_value(value)]


def _entity_aliases(entity) -> set[str]:
    aliases = {getattr(entity, "name", ""), getattr(entity, "normalized_name", "")}
    attributes = getattr(entity, "attributes", None) or {}
    raw_aliases = attributes.get("aliases") or attributes.get("alias") or []
    if isinstance(raw_aliases, str):
        aliases.add(raw_aliases)
    elif isinstance(raw_aliases, (list, tuple, set)):
        aliases.update(str(item) for item in raw_aliases)
    return {alias.strip().lower() for alias in aliases if str(alias).strip()}


async def _queue_cleanup_doc_entity_refs(client, pipe, prefix: str, doc_id: str) -> None:
    """Queue removal of entity/alias references owned by a document."""
    doc_entities_key = f"{prefix}:doc:{doc_id}:entities"
    doc_aliases_key = f"{prefix}:doc:{doc_id}:entity_aliases"

    old_entity_ids: list[str] = []
    old_alias_keys: list[str] = []
    try:
        raw_entity_ids, raw_alias_keys = await asyncio.gather(
            client.lrange(doc_entities_key, 0, -1),
            client.smembers(doc_aliases_key),
        )
        old_entity_ids = [
            _from_redis_value(entity_id)
            for entity_id in raw_entity_ids
            if _from_redis_value(entity_id)
        ]
        old_alias_keys = sorted(
            {_from_redis_value(alias_key) for alias_key in raw_alias_keys}
        )
    except (AttributeError, TypeError):
        old_entity_ids = await _redis_list_values(client, doc_entities_key)
        old_alias_keys = sorted(
            await _redis_set_members(client, doc_aliases_key)
        )

    entity_doc_keys = [
        f"{prefix}:entity:{entity_id}:doc_ids" for entity_id in old_entity_ids
    ]
    alias_doc_keys = [f"{alias_key}:doc_ids" for alias_key in old_alias_keys]
    membership_keys = [*entity_doc_keys, *alias_doc_keys]
    memberships: list[set[str]] = []
    if membership_keys:
        try:
            read_pipe = client.pipeline(transaction=False)
            for membership_key in membership_keys:
                read_pipe.smembers(membership_key)
            membership_results = await read_pipe.execute()
            if len(membership_results) != len(membership_keys):
                raise ValueError("Redis pipeline returned incomplete membership results")
            memberships = [
                {_from_redis_value(value) for value in values}
                for values in membership_results
            ]
        except (AttributeError, TypeError, ValueError):
            memberships = [
                await _redis_set_members(client, membership_key)
                for membership_key in membership_keys
            ]

    entity_memberships = memberships[: len(entity_doc_keys)]
    alias_memberships = memberships[len(entity_doc_keys) :]
    for entity_id, entity_docs_key, entity_doc_ids in zip(
        old_entity_ids,
        entity_doc_keys,
        entity_memberships,
        strict=False,
    ):
        pipe.srem(entity_docs_key, doc_id)
        if entity_doc_ids <= {doc_id}:
            try:
                pipe.hdel(f"{prefix}:bm25:entity_corpus", entity_id)
            except AttributeError:
                pass
            pipe.hset(
                f"{prefix}:entity:{entity_id}:metadata",
                mapping={
                    "commit_status": states.DOC_SUPERSEDED,
                    "is_visible": "0",
                },
            )

    for alias_key, alias_docs_key, alias_doc_ids in zip(
        old_alias_keys,
        alias_doc_keys,
        alias_memberships,
        strict=False,
    ):
        pipe.srem(alias_docs_key, doc_id)
        if alias_doc_ids <= {doc_id}:
            pipe.hset(
                alias_key,
                mapping={
                    "commit_status": states.DOC_SUPERSEDED,
                    "is_visible": "0",
                },
            )
    pipe.delete(doc_aliases_key)


async def _cleanup_doc_entity_refs(client, prefix: str, doc_id: str) -> None:
    """Atomically remove entity/alias references for delete and repair paths."""
    pipe = client.pipeline(transaction=True)
    await _queue_cleanup_doc_entity_refs(client, pipe, prefix, doc_id)
    await pipe.execute()


async def write_chunk_corpus(
    kb_code: str,
    doc_id: str,
    chunks: Sequence[IngestChunk],
    *,
    extraction=None,
    uploaded_at: datetime | None = None,
    committed_at: datetime | None = None,
    visible_at: datetime | None = None,
) -> None:
    if not settings.redis_host or not chunks:
        return
    client = get_redis()
    prefix = f"{kb_code}:v2"
    try:
        doc_chunks_key = f"{prefix}:doc:{doc_id}:chunks"
        doc_entities_key = f"{prefix}:doc:{doc_id}:entities"
        doc_aliases_key = f"{prefix}:doc:{doc_id}:entity_aliases"
        entities = list(getattr(extraction, "entities", {}).values()) if extraction is not None else []
        visible_entities = [
            entity
            for entity in entities
            if getattr(entity, "status", states.ENTITY_ACTIVE) in states.RETRIEVAL_ENTITY_STATUSES
        ]
        pipe = client.pipeline(transaction=True)
        # Queue BM25, chunk, entity and visibility changes on the same
        # MULTI/EXEC so readers see either the old document or the new one.
        await queue_index_replacement(client, pipe, kb_code, doc_id, chunks)
        await _queue_cleanup_doc_entity_refs(client, pipe, prefix, doc_id)
        pipe.delete(doc_chunks_key, doc_entities_key)
        pipe.rpush(doc_chunks_key, *[chunk.chunk_id for chunk in chunks])
        if visible_entities:
            pipe.rpush(doc_entities_key, *[entity.entity_id for entity in visible_entities])
            pipe.hset(
                f"{prefix}:bm25:entity_corpus",
                mapping={
                    entity.entity_id: "\n".join(
                        part
                        for part in (
                            getattr(entity, "name", ""),
                            getattr(entity, "normalized_name", ""),
                            getattr(entity, "entity_type", ""),
                        )
                        if part
                    )[:4000]
                    for entity in visible_entities
                },
            )
            for entity in visible_entities:
                metadata = {
                    "entity_id": entity.entity_id,
                    "entity_name": getattr(entity, "name", ""),
                    "normalized_name": getattr(entity, "normalized_name", ""),
                    "entity_type": getattr(entity, "entity_type", ""),
                    "support_count": len(getattr(entity, "chunk_ids", []) or []),
                    "doc_ids": sorted(getattr(entity, "doc_ids", []) or []),
                    "chunk_ids": sorted(getattr(entity, "chunk_ids", []) or []),
                    "commit_status": states.COMMIT_COMMITTED,
                    "is_visible": "1",
                }
                pipe.hset(
                    f"{prefix}:entity:{entity.entity_id}:metadata",
                    mapping={key: _to_redis_value(value) for key, value in metadata.items()},
                )
                try:
                    pipe.sadd(f"{prefix}:entity:{entity.entity_id}:doc_ids", doc_id)
                except AttributeError:
                    pass
                alias_keys: list[str] = []
                for alias in _entity_aliases(entity):
                    alias_key = f"{prefix}:entity_alias:{sha256_text(alias)}"
                    alias_keys.append(alias_key)
                    pipe.hset(
                        alias_key,
                        mapping={
                            "alias": alias,
                            "entity_id": entity.entity_id,
                            "normalized_name": getattr(entity, "normalized_name", ""),
                            "entity_type": getattr(entity, "entity_type", ""),
                            "commit_status": states.COMMIT_COMMITTED,
                            "is_visible": "1",
                        },
                    )
                    try:
                        pipe.sadd(f"{alias_key}:doc_ids", doc_id)
                    except AttributeError:
                        pass
                if alias_keys:
                    try:
                        pipe.sadd(f"{prefix}:entity:{entity.entity_id}:aliases", *alias_keys)
                        pipe.sadd(doc_aliases_key, *alias_keys)
                    except AttributeError:
                        pass
        if uploaded_at:
            pipe.hset(f"{prefix}:docs:uploaded_at", mapping={doc_id: uploaded_at.isoformat()})
        if committed_at:
            pipe.hset(f"{prefix}:docs:committed_at", mapping={doc_id: committed_at.isoformat()})
        if visible_at:
            pipe.hset(f"{prefix}:docs:visible_at", mapping={doc_id: visible_at.isoformat()})
        pipe.hset(
            f"{prefix}:doc:{doc_id}:metadata",
            mapping={
                "doc_id": doc_id,
                "chunk_count": str(len(chunks)),
                "entity_count": str(len(visible_entities)),
                "commit_status": states.COMMIT_COMMITTED,
                "is_visible": "1",
                "uploaded_at": uploaded_at.isoformat() if uploaded_at else "",
                "committed_at": committed_at.isoformat() if committed_at else "",
                "visible_at": visible_at.isoformat() if visible_at else "",
            },
        )
        pipe.hset(
            f"{prefix}:doc:{doc_id}:status",
            mapping={"commit_status": states.COMMIT_COMMITTED, "is_visible": "1"},
        )
        await pipe.execute()
    except Exception as exc:
        logger.warning("[self-ingestion] Redis v2 corpus 写入失败: %s", exc)
        raise
    finally:
        await client.aclose()


async def delete_doc_from_corpus(
    kb_code: str,
    doc_id: str,
    chunk_ids: Sequence[str],
) -> None:
    """Remove a document's chunks from BM25 corpus and mark status as deleted."""
    if not settings.redis_host:
        return
    client = get_redis()
    prefix = f"{kb_code}:v2"
    try:
        if chunk_ids:
            for chunk_id_batch in _batched(list(chunk_ids), _redis_batch_size()):
                await client.hdel(f"{prefix}:bm25:chunk_corpus", *chunk_id_batch)
        await remove_bm25_doc(kb_code, doc_id, chunk_ids)
        await _cleanup_doc_entity_refs(client, prefix, doc_id)
        await client.delete(f"{prefix}:doc:{doc_id}:chunks")
        await client.delete(f"{prefix}:doc:{doc_id}:entities")
        await client.hset(
            f"{prefix}:doc:{doc_id}:status",
            mapping={"commit_status": states.DOC_DELETED, "is_visible": "0"},
        )
    except Exception as exc:
        logger.warning("[self-ingestion] Redis v2 doc 删除清理失败: %s", exc)
    finally:
        await client.aclose()


async def mark_doc_status(
    kb_code: str,
    doc_id: str,
    *,
    commit_status: str,
    is_visible: bool,
) -> bool:
    if not settings.redis_host:
        return True
    client = get_redis()
    prefix = f"{kb_code}:v2"
    try:
        await client.hset(
            f"{prefix}:doc:{doc_id}:status",
            mapping={
                "commit_status": commit_status,
                "is_visible": "1" if is_visible else "0",
            },
        )
        return True
    except Exception as exc:
        logger.warning("[self-ingestion] Redis v2 doc 状态写入失败: %s", exc)
        return False
    finally:
        await client.aclose()
