"""Optional Milvus writer for self-ingestion chunks_v2.

按方案 §15.1 定义 chunks_v2 collection schema：包含可见性/状态/溯源/版本等
标量字段并建立标量索引，供 DirectRetriever v2 做方案 §18.2 要求的强过滤
（doc_status / commit_status / schema_version / is_visible / is_active）。
"""

from __future__ import annotations

import asyncio
import json
import re
from datetime import datetime
from typing import Any, Sequence

from backend.config.loggers import get_logger
from backend.config.settings import settings
from backend.rag.ingestion import states
from backend.rag.ingestion.models import IngestChunk

logger = get_logger(__name__)


def _connect_milvus(alias: str) -> None:
    from pymilvus import connections

    kwargs = {"alias": alias, "uri": settings.milvus_connection_uri}
    if settings.milvus_connection_db_name:
        kwargs["db_name"] = settings.milvus_connection_db_name
    if settings.milvus_user:
        kwargs["user"] = settings.milvus_user
    if settings.milvus_password:
        kwargs["password"] = settings.milvus_password
    connections.connect(**kwargs)


# chunks_v2 必备标量字段（用于检测旧 collection 是否兼容）
_REQUIRED_FIELDS = {
    "chunk_id", "vector", "kb_id", "kb_code", "doc_id", "job_id", "block_id",
    "chunk_index", "block_type", "content", "file_name", "file_path",
    "object_storage_uri", "page_num", "section_path", "content_hash",
    "source_hash", "schema_version", "parser_version", "commit_version",
    "doc_status", "is_active", "is_visible", "commit_status",
    "created_at_ts",
}

_REQUIRED_ENTITY_FIELDS = {
    "entity_id", "vector", "kb_id", "kb_code", "entity_name", "normalized_name",
    "entity_type", "doc_ids", "chunk_ids", "support_count", "status",
    "is_visible", "created_at_ts",
}

_REQUIRED_MEDIA_FIELDS = {
    "chunk_id", "vector", "kb_id", "kb_code", "doc_id", "job_id", "block_id",
    "chunk_index", "block_type", "media_type", "media_ref", "media_title",
    "content", "file_name", "file_path", "image_path", "page_num",
    "doc_status", "is_active", "is_visible", "commit_status", "created_at_ts",
}


def chunks_collection_name(kb_code: str) -> str:
    safe = re.sub(r"[^0-9A-Za-z_]", "_", kb_code or "default").strip("_") or "default"
    return f"{safe}_chunks_v2"


def entities_collection_name(kb_code: str) -> str:
    safe = re.sub(r"[^0-9A-Za-z_]", "_", kb_code or "default").strip("_") or "default"
    return f"{safe}_entities_v2"


def media_collection_name(kb_code: str) -> str:
    safe = re.sub(r"[^0-9A-Za-z_]", "_", kb_code or "default").strip("_") or "default"
    return f"{safe}_media_vectors_v2"


def _milvus_insert_batch_size() -> int:
    """Keep each gRPC insert under Milvus default 64MB message limit."""
    explicit = int(getattr(settings, "milvus_insert_batch_size", 0) or 0)
    if explicit > 0:
        return explicit
    return max(int(getattr(settings, "milvus_visibility_batch_size", 100) or 100), 1)


def _milvus_visibility_batch_size() -> int:
    """Batch scalar visibility updates under gRPC message limits."""
    return max(int(getattr(settings, "milvus_visibility_batch_size", 200) or 200), 1)


def _estimate_milvus_row_bytes(row: dict[str, Any]) -> int:
    """Rough payload estimate to avoid Milvus gRPC 64MB message limit."""
    total = 512
    for value in row.values():
        if isinstance(value, str):
            total += len(value.encode("utf-8", errors="ignore"))
        elif isinstance(value, (bytes, bytearray)):
            total += len(value)
        elif isinstance(value, (list, tuple)):
            if value and all(isinstance(item, (float, int)) for item in value):
                total += len(value) * 4
            else:
                total += len(str(value).encode("utf-8", errors="ignore"))
        else:
            total += 16
    return total


def _iter_milvus_payload_batches(
    rows: Sequence[dict[str, Any]],
    *,
    max_rows: int | None = None,
    max_bytes: int = 32 * 1024 * 1024,
) -> list[list[dict[str, Any]]]:
    """Split rows by both count and estimated payload size.

    Milvus defaults to a 64MB gRPC message cap. A 32MB target leaves room for
    protobuf overhead and avoids RESOURCE_EXHAUSTED on large books.
    """
    row_limit = max(int(max_rows or _milvus_insert_batch_size()), 1)
    byte_limit = max(int(max_bytes), 1024 * 1024)
    batches: list[list[dict[str, Any]]] = []
    current: list[dict[str, Any]] = []
    current_bytes = 0

    for row in rows:
        row_bytes = _estimate_milvus_row_bytes(row)
        if current and (len(current) >= row_limit or current_bytes + row_bytes > byte_limit):
            batches.append(current)
            current = []
            current_bytes = 0
        current.append(row)
        current_bytes += row_bytes

    if current:
        batches.append(current)
    return batches


def _escape_milvus_string(value: Any) -> str:
    return str(value or "").replace("\\", "\\\\").replace('"', '\\"')


def _varchar(value: Any, max_length: int) -> str:
    """Milvus VARCHAR max_length is byte-oriented; keep UTF-8 valid after truncation."""
    text = str(value or "")
    max_length = max(int(max_length or 0), 0)
    encoded = text.encode("utf-8")
    if len(encoded) <= max_length:
        return text
    return encoded[:max_length].decode("utf-8", errors="ignore")


def _delete_by_entity_ids(collection, entity_ids: Sequence[str]) -> None:
    if not entity_ids:
        return
    batch_size = _milvus_insert_batch_size()
    for offset in range(0, len(entity_ids), batch_size):
        batch = entity_ids[offset : offset + batch_size]
        quoted = ", ".join(f'"{_escape_milvus_string(item)}"' for item in batch)
        collection.delete(f"entity_id in [{quoted}]")


class MilvusChunkWriter:
    def __init__(self, kb_code: str):
        self.collection_name = chunks_collection_name(kb_code)

    async def upsert_chunks(
        self,
        chunks: Sequence[IngestChunk],
        vectors: Sequence[Sequence[float]],
        *,
        kb_id: str,
        kb_code: str,
        visible: bool = False,
    ) -> int:
        if not chunks or not vectors:
            return 0
        if len(chunks) != len(vectors):
            logger.warning(
                "[self-ingestion] Milvus chunks_v2 写入跳过：chunk/vector 数量不一致 chunks=%d vectors=%d",
                len(chunks), len(vectors),
            )
            return 0
        if not settings.milvus_connection_uri:
            logger.info("[self-ingestion] Milvus 未配置，跳过 chunks_v2 写入")
            return 0
        return await asyncio.to_thread(
            self._upsert_sync, chunks, vectors, kb_id, kb_code, visible
        )

    async def mark_doc_visible(
        self,
        doc_id: str,
        *,
        chunk_ids: Sequence[str] | None = None,
    ) -> bool:
        if not settings.milvus_connection_uri:
            return False
        return await asyncio.to_thread(self._set_doc_visibility_sync, doc_id, True, chunk_ids)

    async def hide_doc(self, doc_id: str) -> bool:
        if not settings.milvus_connection_uri:
            return False
        return await asyncio.to_thread(self._set_doc_visibility_sync, doc_id, False)

    async def delete_doc(self, doc_id: str) -> bool:
        if not settings.milvus_connection_uri:
            return False
        return await asyncio.to_thread(self._delete_doc_sync, doc_id)

    async def delete_chunks_by_version(self, doc_id: str, commit_version: int) -> bool:
        """精确删除某 doc 的旧 commit_version 向量，避免误删新版本。"""
        if not settings.milvus_connection_uri:
            return False
        return await asyncio.to_thread(self._delete_chunks_by_version_sync, doc_id, commit_version)

    # --------------------------- sync helpers ---------------------------

    def _connect(self, alias: str) -> None:
        _connect_milvus(alias)

    def _build_schema(self, dim: int):
        from pymilvus import CollectionSchema, DataType, FieldSchema

        fields = [
            FieldSchema(name="chunk_id", dtype=DataType.VARCHAR, max_length=128, is_primary=True),
            FieldSchema(name="vector", dtype=DataType.FLOAT_VECTOR, dim=dim),
            FieldSchema(name="kb_id", dtype=DataType.VARCHAR, max_length=64),
            FieldSchema(name="kb_code", dtype=DataType.VARCHAR, max_length=64),
            FieldSchema(name="doc_id", dtype=DataType.VARCHAR, max_length=128),
            FieldSchema(name="job_id", dtype=DataType.VARCHAR, max_length=64),
            FieldSchema(name="block_id", dtype=DataType.VARCHAR, max_length=128),
            FieldSchema(name="chunk_index", dtype=DataType.INT64),
            FieldSchema(name="block_type", dtype=DataType.VARCHAR, max_length=32),
            FieldSchema(name="content", dtype=DataType.VARCHAR, max_length=8192),
            FieldSchema(name="file_name", dtype=DataType.VARCHAR, max_length=255),
            FieldSchema(name="file_path", dtype=DataType.VARCHAR, max_length=512),
            FieldSchema(name="object_storage_uri", dtype=DataType.VARCHAR, max_length=700),
            FieldSchema(name="page_num", dtype=DataType.INT64),
            FieldSchema(name="section_path", dtype=DataType.VARCHAR, max_length=512),
            FieldSchema(name="content_hash", dtype=DataType.VARCHAR, max_length=64),
            FieldSchema(name="source_hash", dtype=DataType.VARCHAR, max_length=64),
            FieldSchema(name="schema_version", dtype=DataType.VARCHAR, max_length=64),
            FieldSchema(name="parser_version", dtype=DataType.VARCHAR, max_length=64),
            FieldSchema(name="commit_version", dtype=DataType.INT64),
            FieldSchema(name="doc_status", dtype=DataType.VARCHAR, max_length=32),
            FieldSchema(name="is_active", dtype=DataType.BOOL),
            FieldSchema(name="is_visible", dtype=DataType.BOOL),
            FieldSchema(name="commit_status", dtype=DataType.VARCHAR, max_length=32),
            FieldSchema(name="created_at_ts", dtype=DataType.INT64),
        ]
        return CollectionSchema(fields, description="self-ingestion chunks v2")

    def _ensure_collection(self, alias: str, dim: int):
        """返回 (collection, compatible)。compatible=False 表示旧 collection 字段缺失，调用方应降级。"""
        from pymilvus import Collection, utility

        if utility.has_collection(self.collection_name, using=alias):
            collection = Collection(self.collection_name, using=alias)
            try:
                collection.load()
            except Exception:
                pass
            existing_fields = {f.name for f in collection.schema.fields}
            missing = _REQUIRED_FIELDS - existing_fields
            if missing:
                logger.error(
                    "[self-ingestion] Milvus collection %s 缺少字段 %s，"
                    "本次写入将跳过 Milvus（MySQL 仍可检索）。请重建该 collection。",
                    self.collection_name, sorted(missing),
                )
                return collection, False
            return collection, True

        schema = self._build_schema(dim)
        collection = Collection(self.collection_name, schema=schema, using=alias)
        collection.create_index(
            field_name="vector",
            index_params={"metric_type": "COSINE", "index_type": "FLAT", "params": {}},
        )
        # 标量索引：方案 §15.1 要求
        for scalar_field in (
            "kb_id", "doc_id", "block_id", "block_type", "doc_status",
            "is_active", "is_visible", "commit_status", "schema_version",
            "commit_version", "created_at_ts",
        ):
            try:
                collection.create_index(field_name=scalar_field)
            except Exception as exc:
                logger.debug(
                    "[self-ingestion] Milvus scalar index %s create skipped: %s",
                    scalar_field, exc,
                )
        collection.load()
        logger.info("[self-ingestion] Milvus collection created: %s", self.collection_name)
        return collection, True

    @staticmethod
    def _row_from_chunk(
        chunk: IngestChunk,
        vector: Sequence[float],
        kb_id: str,
        kb_code: str,
        visible: bool,
    ) -> dict[str, Any]:
        return {
            "chunk_id": _varchar(chunk.chunk_id, 128),
            "vector": list(vector),
            "kb_id": _varchar(kb_id, 64),
            "kb_code": _varchar(kb_code, 64),
            "doc_id": _varchar(chunk.doc_id, 128),
            "job_id": _varchar(chunk.job_id, 64),
            "block_id": _varchar(chunk.block_id, 128),
            "chunk_index": int(chunk.chunk_index),
            "block_type": _varchar(chunk.block_type or "text", 32),
            "content": _varchar(chunk.content, 8192),
            "file_name": _varchar(chunk.file_name, 255),
            "file_path": _varchar(chunk.file_path, 512),
            "object_storage_uri": "",
            "page_num": int(chunk.page_num) if chunk.page_num is not None else -1,
            "section_path": _varchar("/".join(chunk.section_path or []), 512),
            "content_hash": _varchar(chunk.content_hash, 64),
            "source_hash": _varchar(chunk.source_hash, 64),
            "schema_version": _varchar(chunk.schema_version, 64),
            "parser_version": _varchar(chunk.parser_version, 64),
            "commit_version": int(chunk.commit_version),
            "doc_status": _varchar(states.DOC_STAGED if not visible else states.DOC_VISIBLE, 32),
            "is_active": True,
            "is_visible": bool(visible),
            "commit_status": _varchar(states.COMMIT_STAGED if not visible else states.COMMIT_COMMITTED, 32),
            "created_at_ts": int(datetime.now().timestamp()),
        }

    def _upsert_sync(
        self,
        chunks: Sequence[IngestChunk],
        vectors: Sequence[Sequence[float]],
        kb_id: str,
        kb_code: str,
        visible: bool,
    ) -> int:
        alias = f"self_ingestion_{id(self)}"
        try:
            from pymilvus import connections

            dim = len(vectors[0])
            self._connect(alias)
            collection, compatible = self._ensure_collection(alias, dim)
            if not compatible:
                return 0
            rows = [
                self._row_from_chunk(chunk, vector, kb_id, kb_code, visible)
                for chunk, vector in zip(chunks, vectors)
            ]
            chunk_ids = [row["chunk_id"] for row in rows]
            batch_size = _milvus_insert_batch_size()
            if hasattr(collection, "upsert"):
                for batch in _iter_milvus_payload_batches(rows, max_rows=batch_size):
                    collection.upsert(batch)
            else:
                self._delete_by_chunk_ids(collection, chunk_ids)
                for batch in _iter_milvus_payload_batches(rows, max_rows=batch_size):
                    collection.insert(batch)
            collection.flush()
            return len(rows)
        except Exception as exc:
            logger.warning("[self-ingestion] Milvus chunks_v2 写入失败，保留 MySQL 可检索数据: %s", exc)
            return 0
        finally:
            try:
                connections.disconnect(alias)
            except Exception:
                pass

    @staticmethod
    def _delete_by_chunk_ids(collection, chunk_ids: Sequence[str]) -> None:
        """按主键 chunk_id 删除，避免 doc_id 标量过滤在部分 Milvus 版本 delete 不可用。"""
        if not chunk_ids:
            return
        batch_size = _milvus_visibility_batch_size()
        for offset in range(0, len(chunk_ids), batch_size):
            batch = chunk_ids[offset : offset + batch_size]
            quoted = ", ".join(f'"{_escape_milvus_string(item)}"' for item in batch)
            collection.delete(f"chunk_id in [{quoted}]")

    @classmethod
    def _delete_matching_chunks(cls, collection, expr: str) -> int:
        """Page matching primary keys so one document cannot exhaust memory."""

        batch_size = _milvus_visibility_batch_size()
        query_iterator_factory = getattr(collection, "query_iterator", None)
        if not callable(query_iterator_factory):
            rows = collection.query(expr=expr, output_fields=["chunk_id"])
            chunk_ids = [row["chunk_id"] for row in rows if row.get("chunk_id")]
            cls._delete_by_chunk_ids(collection, chunk_ids)
            return len(chunk_ids)

        iterator = query_iterator_factory(
            expr=expr,
            output_fields=["chunk_id"],
            batch_size=batch_size,
        )
        deleted = 0
        try:
            while True:
                rows = iterator.next()
                if not rows:
                    break
                chunk_ids = [
                    row["chunk_id"] for row in rows if row.get("chunk_id")
                ]
                cls._delete_by_chunk_ids(collection, chunk_ids)
                deleted += len(chunk_ids)
        finally:
            close = getattr(iterator, "close", None)
            if callable(close):
                close()
        return deleted

    def _set_doc_visibility_sync(
        self,
        doc_id: str,
        visible: bool,
        chunk_ids: Sequence[str] | None = None,
    ) -> bool:
        """Toggle visibility via upsert (preferred) or PK-scoped delete+insert."""
        alias = f"self_ingestion_visibility_{doc_id[:8]}"
        batch_size = _milvus_visibility_batch_size()
        try:
            from pymilvus import Collection, connections, utility

            self._connect(alias)
            if not utility.has_collection(self.collection_name, using=alias):
                return False
            collection = Collection(self.collection_name, using=alias)
            collection.load()

            existing_fields = {f.name for f in collection.schema.fields}
            missing = _REQUIRED_FIELDS - existing_fields
            if missing:
                logger.warning(
                    "[self-ingestion] Milvus collection %s 缺少字段 %s，跳过可见性更新: doc=%s",
                    self.collection_name, sorted(missing), doc_id,
                )
                return False

            output_fields = sorted(_REQUIRED_FIELDS)
            chunk_id_list = [str(item) for item in (chunk_ids or []) if item]
            if chunk_ids is None:
                id_rows = collection.query(
                    expr=f'doc_id == "{_escape_milvus_string(doc_id)}"',
                    output_fields=["chunk_id"],
                )
                chunk_id_list = [str(row["chunk_id"]) for row in id_rows if row.get("chunk_id")]

            if not chunk_id_list:
                return False

            updated = 0
            missing_vector = False
            for offset in range(0, len(chunk_id_list), batch_size):
                batch_ids = chunk_id_list[offset : offset + batch_size]
                quoted = ", ".join(f'"{_escape_milvus_string(item)}"' for item in batch_ids)
                rows = collection.query(
                    expr=f"chunk_id in [{quoted}]",
                    output_fields=output_fields,
                )
                valid_rows = [r for r in rows if r.get("vector")]
                if not valid_rows:
                    missing_vector = True
                    continue

                for row in valid_rows:
                    row["is_visible"] = bool(visible)
                    row["doc_status"] = states.DOC_VISIBLE if visible else states.DOC_STAGED
                    row["commit_status"] = states.COMMIT_COMMITTED if visible else states.COMMIT_STAGED

                if hasattr(collection, "upsert"):
                    for payload in _iter_milvus_payload_batches(valid_rows, max_rows=batch_size):
                        collection.upsert(payload)
                else:
                    self._delete_by_chunk_ids(collection, [row["chunk_id"] for row in valid_rows])
                    for payload in _iter_milvus_payload_batches(valid_rows, max_rows=batch_size):
                        collection.insert(payload)
                updated += len(valid_rows)

            if missing_vector and not updated:
                logger.warning(
                    "[self-ingestion] Milvus query 未返回 vector 字段，跳过可见性更新: doc=%s",
                    doc_id,
                )
                return False
            collection.flush()
            logger.info(
                "[self-ingestion] Milvus 可见性更新完成: doc=%s rows=%d visible=%s",
                doc_id, updated, visible,
            )
            return updated > 0
        except Exception as exc:
            logger.warning("[self-ingestion] Milvus 可见性更新失败: doc=%s error=%s", doc_id, exc)
            return False
        finally:
            try:
                connections.disconnect(alias)
            except Exception:
                pass

    def _delete_doc_sync(self, doc_id: str) -> bool:
        alias = f"self_ingestion_delete_{doc_id[:8]}"
        try:
            from pymilvus import Collection, connections, utility

            self._connect(alias)
            if not utility.has_collection(self.collection_name, using=alias):
                return True
            collection = Collection(self.collection_name, using=alias)
            collection.load()
            self._delete_matching_chunks(
                collection,
                f'doc_id == "{_escape_milvus_string(doc_id)}"',
            )
            collection.flush()
            return True
        except Exception as exc:
            logger.warning("[self-ingestion] Milvus doc 删除失败: doc=%s error=%s", doc_id, exc)
            return False
        finally:
            try:
                connections.disconnect(alias)
            except Exception:
                pass

    def _delete_chunks_by_version_sync(self, doc_id: str, commit_version: int) -> bool:
        alias = f"self_ingestion_delver_{doc_id[:8]}_{commit_version}"
        try:
            from pymilvus import Collection, connections, utility

            self._connect(alias)
            if not utility.has_collection(self.collection_name, using=alias):
                return False
            collection = Collection(self.collection_name, using=alias)
            collection.load()
            self._delete_matching_chunks(
                collection,
                expr=(
                    f'doc_id == "{_escape_milvus_string(doc_id)}" '
                    f"&& commit_version == {int(commit_version)}"
                ),
            )
            collection.flush()
            return True
        except Exception as exc:
            logger.warning(
                "[self-ingestion] Milvus 按版本删除失败: doc=%s ver=%s error=%s",
                doc_id, commit_version, exc,
            )
            return False
        finally:
            try:
                connections.disconnect(alias)
            except Exception:
                pass


class MilvusMediaWriter:
    """Optional Milvus writer for dedicated media/VL vectors."""

    def __init__(self, kb_code: str):
        self.collection_name = media_collection_name(kb_code)

    async def upsert_media(
        self,
        chunks: Sequence[IngestChunk],
        vectors: Sequence[Sequence[float]],
        *,
        kb_id: str,
        kb_code: str,
        visible: bool = False,
    ) -> int:
        if not chunks or not vectors:
            return 0
        if len(chunks) != len(vectors):
            logger.warning(
                "[self-ingestion] Milvus media_vectors_v2 写入跳过：chunk/vector 数量不一致 chunks=%d vectors=%d",
                len(chunks), len(vectors),
            )
            return 0
        if not settings.milvus_connection_uri:
            logger.info("[self-ingestion] Milvus 未配置，跳过 media_vectors_v2 写入")
            return 0
        return await asyncio.to_thread(
            self._upsert_sync, chunks, vectors, kb_id, kb_code, visible
        )

    async def mark_doc_visible(
        self,
        doc_id: str,
        *,
        chunk_ids: Sequence[str] | None = None,
    ) -> bool:
        if not settings.milvus_connection_uri:
            return False
        return await asyncio.to_thread(self._set_doc_visibility_sync, doc_id, True, chunk_ids)

    async def hide_doc(self, doc_id: str) -> bool:
        if not settings.milvus_connection_uri:
            return False
        return await asyncio.to_thread(self._set_doc_visibility_sync, doc_id, False)

    async def delete_doc(self, doc_id: str) -> bool:
        if not settings.milvus_connection_uri:
            return False
        return await asyncio.to_thread(self._delete_doc_sync, doc_id)

    def _connect(self, alias: str) -> None:
        _connect_milvus(alias)

    def _build_schema(self, dim: int):
        from pymilvus import CollectionSchema, DataType, FieldSchema

        fields = [
            FieldSchema(name="chunk_id", dtype=DataType.VARCHAR, max_length=128, is_primary=True),
            FieldSchema(name="vector", dtype=DataType.FLOAT_VECTOR, dim=dim),
            FieldSchema(name="kb_id", dtype=DataType.VARCHAR, max_length=64),
            FieldSchema(name="kb_code", dtype=DataType.VARCHAR, max_length=64),
            FieldSchema(name="doc_id", dtype=DataType.VARCHAR, max_length=128),
            FieldSchema(name="job_id", dtype=DataType.VARCHAR, max_length=64),
            FieldSchema(name="block_id", dtype=DataType.VARCHAR, max_length=128),
            FieldSchema(name="chunk_index", dtype=DataType.INT64),
            FieldSchema(name="block_type", dtype=DataType.VARCHAR, max_length=32),
            FieldSchema(name="media_type", dtype=DataType.VARCHAR, max_length=32),
            FieldSchema(name="media_ref", dtype=DataType.VARCHAR, max_length=64),
            FieldSchema(name="media_title", dtype=DataType.VARCHAR, max_length=512),
            FieldSchema(name="content", dtype=DataType.VARCHAR, max_length=8192),
            FieldSchema(name="file_name", dtype=DataType.VARCHAR, max_length=255),
            FieldSchema(name="file_path", dtype=DataType.VARCHAR, max_length=512),
            FieldSchema(name="image_path", dtype=DataType.VARCHAR, max_length=700),
            FieldSchema(name="page_num", dtype=DataType.INT64),
            FieldSchema(name="doc_status", dtype=DataType.VARCHAR, max_length=32),
            FieldSchema(name="is_active", dtype=DataType.BOOL),
            FieldSchema(name="is_visible", dtype=DataType.BOOL),
            FieldSchema(name="commit_status", dtype=DataType.VARCHAR, max_length=32),
            FieldSchema(name="created_at_ts", dtype=DataType.INT64),
        ]
        return CollectionSchema(fields, description="self-ingestion media vectors v2")

    def _ensure_collection(self, alias: str, dim: int):
        from pymilvus import Collection, utility

        if utility.has_collection(self.collection_name, using=alias):
            collection = Collection(self.collection_name, using=alias)
            try:
                collection.load()
            except Exception:
                pass
            existing_fields = {f.name for f in collection.schema.fields}
            missing = _REQUIRED_MEDIA_FIELDS - existing_fields
            if missing:
                logger.error(
                    "[self-ingestion] Milvus collection %s 缺少字段 %s，本次跳过 media_vectors_v2 写入。",
                    self.collection_name, sorted(missing),
                )
                return collection, False
            return collection, True

        collection = Collection(self.collection_name, schema=self._build_schema(dim), using=alias)
        collection.create_index(
            field_name="vector",
            index_params={"metric_type": "COSINE", "index_type": "FLAT", "params": {}},
        )
        for scalar_field in (
            "kb_id", "doc_id", "block_type", "media_type", "media_ref",
            "doc_status", "is_active", "is_visible", "commit_status", "created_at_ts",
        ):
            try:
                collection.create_index(field_name=scalar_field)
            except Exception as exc:
                logger.debug(
                    "[self-ingestion] Milvus media scalar index %s create skipped: %s",
                    scalar_field, exc,
                )
        collection.load()
        logger.info("[self-ingestion] Milvus collection created: %s", self.collection_name)
        return collection, True

    @staticmethod
    def _row_from_chunk(
        chunk: IngestChunk,
        vector: Sequence[float],
        kb_id: str,
        kb_code: str,
        visible: bool,
    ) -> dict[str, Any]:
        extra = dict(chunk.extra or {})
        media_type = extra.get("media_type") or chunk.block_type or "image"
        image_path = (
            extra.get("image_path")
            or extra.get("asset_uri")
            or extra.get("object_storage_uri")
            or extra.get("source_image_url")
            or ""
        )
        return {
            "chunk_id": _varchar(chunk.chunk_id, 128),
            "vector": list(vector),
            "kb_id": _varchar(kb_id, 64),
            "kb_code": _varchar(kb_code, 64),
            "doc_id": _varchar(chunk.doc_id, 128),
            "job_id": _varchar(chunk.job_id, 64),
            "block_id": _varchar(chunk.block_id, 128),
            "chunk_index": int(chunk.chunk_index),
            "block_type": _varchar(chunk.block_type or "image", 32),
            "media_type": _varchar(media_type, 32),
            "media_ref": _varchar(extra.get("media_ref") or "", 64),
            "media_title": _varchar(extra.get("media_title") or extra.get("caption") or "", 512),
            "content": _varchar(chunk.content, 8192),
            "file_name": _varchar(chunk.file_name, 255),
            "file_path": _varchar(chunk.file_path, 512),
            "image_path": _varchar(image_path, 700),
            "page_num": int(chunk.page_num) if chunk.page_num is not None else -1,
            "doc_status": _varchar(states.DOC_STAGED if not visible else states.DOC_VISIBLE, 32),
            "is_active": True,
            "is_visible": bool(visible),
            "commit_status": _varchar(states.COMMIT_STAGED if not visible else states.COMMIT_COMMITTED, 32),
            "created_at_ts": int(datetime.now().timestamp()),
        }

    def _upsert_sync(
        self,
        chunks: Sequence[IngestChunk],
        vectors: Sequence[Sequence[float]],
        kb_id: str,
        kb_code: str,
        visible: bool,
    ) -> int:
        alias = f"self_media_{id(self)}"
        try:
            from pymilvus import connections

            dim = len(vectors[0])
            self._connect(alias)
            collection, compatible = self._ensure_collection(alias, dim)
            if not compatible:
                return 0
            rows = [
                self._row_from_chunk(chunk, vector, kb_id, kb_code, visible)
                for chunk, vector in zip(chunks, vectors)
            ]
            chunk_ids = [row["chunk_id"] for row in rows]
            batch_size = _milvus_insert_batch_size()
            if hasattr(collection, "upsert"):
                for batch in _iter_milvus_payload_batches(rows, max_rows=batch_size):
                    collection.upsert(batch)
            else:
                MilvusChunkWriter._delete_by_chunk_ids(collection, chunk_ids)
                for batch in _iter_milvus_payload_batches(rows, max_rows=batch_size):
                    collection.insert(batch)
            collection.flush()
            return len(rows)
        except Exception as exc:
            logger.warning("[self-ingestion] Milvus media_vectors_v2 写入失败: %s", exc)
            return 0
        finally:
            try:
                connections.disconnect(alias)
            except Exception:
                pass

    def _set_doc_visibility_sync(
        self,
        doc_id: str,
        visible: bool,
        chunk_ids: Sequence[str] | None = None,
    ) -> bool:
        alias = f"self_media_visibility_{doc_id[:8]}"
        batch_size = _milvus_visibility_batch_size()
        try:
            from pymilvus import Collection, connections, utility

            self._connect(alias)
            if not utility.has_collection(self.collection_name, using=alias):
                return False
            collection = Collection(self.collection_name, using=alias)
            collection.load()
            output_fields = sorted(_REQUIRED_MEDIA_FIELDS)
            chunk_id_list = [str(item) for item in (chunk_ids or []) if item]
            if chunk_ids is None:
                id_rows = collection.query(
                    expr=f'doc_id == "{_escape_milvus_string(doc_id)}"',
                    output_fields=["chunk_id"],
                )
                chunk_id_list = [str(row["chunk_id"]) for row in id_rows if row.get("chunk_id")]

            if not chunk_id_list:
                return False

            updated = 0
            for offset in range(0, len(chunk_id_list), batch_size):
                batch_ids = chunk_id_list[offset : offset + batch_size]
                quoted = ", ".join(f'"{_escape_milvus_string(item)}"' for item in batch_ids)
                rows = collection.query(
                    expr=f"chunk_id in [{quoted}]",
                    output_fields=output_fields,
                )
                valid_rows = [row for row in rows if row.get("vector")]
                if not valid_rows:
                    continue
                for row in valid_rows:
                    row["is_visible"] = bool(visible)
                    row["doc_status"] = states.DOC_VISIBLE if visible else states.DOC_STAGED
                    row["commit_status"] = states.COMMIT_COMMITTED if visible else states.COMMIT_STAGED
                if hasattr(collection, "upsert"):
                    for payload in _iter_milvus_payload_batches(valid_rows, max_rows=batch_size):
                        collection.upsert(payload)
                else:
                    MilvusChunkWriter._delete_by_chunk_ids(
                        collection, [row["chunk_id"] for row in valid_rows]
                    )
                    for payload in _iter_milvus_payload_batches(valid_rows, max_rows=batch_size):
                        collection.insert(payload)
                updated += len(valid_rows)
            collection.flush()
            logger.info(
                "[self-ingestion] Milvus media 可见性更新完成: doc=%s rows=%d visible=%s",
                doc_id, updated, visible,
            )
            return updated > 0
        except Exception as exc:
            logger.warning("[self-ingestion] Milvus media 可见性更新失败: doc=%s error=%s", doc_id, exc)
            return False
        finally:
            try:
                connections.disconnect(alias)
            except Exception:
                pass

    def _delete_doc_sync(self, doc_id: str) -> bool:
        alias = f"self_media_delete_{doc_id[:8]}"
        try:
            from pymilvus import Collection, connections, utility

            self._connect(alias)
            if not utility.has_collection(self.collection_name, using=alias):
                return True
            collection = Collection(self.collection_name, using=alias)
            collection.load()
            rows = collection.query(expr=f'doc_id == "{doc_id}"', output_fields=["chunk_id"])
            chunk_ids = [row["chunk_id"] for row in rows if row.get("chunk_id")]
            if not chunk_ids:
                return True
            MilvusChunkWriter._delete_by_chunk_ids(collection, chunk_ids)
            collection.flush()
            return True
        except Exception as exc:
            logger.warning("[self-ingestion] Milvus media doc 删除失败: doc=%s error=%s", doc_id, exc)
            return False
        finally:
            try:
                connections.disconnect(alias)
            except Exception:
                pass


class MilvusEntityWriter:
    """Optional Milvus writer for `{kb_code}_entities_v2` Path-A recall."""

    def __init__(self, kb_code: str):
        self.collection_name = entities_collection_name(kb_code)

    async def upsert_entities(
        self,
        entities: Sequence[Any],
        vectors: Sequence[Sequence[float]],
        *,
        kb_id: str,
        kb_code: str,
        visible: bool = True,
    ) -> int:
        if not entities or not vectors:
            return 0
        if len(entities) != len(vectors):
            logger.warning(
                "[self-ingestion] Milvus entities_v2 写入跳过：entity/vector 数量不一致 entities=%d vectors=%d",
                len(entities), len(vectors),
            )
            return 0
        if not settings.milvus_connection_uri:
            logger.info("[self-ingestion] Milvus 未配置，跳过 entities_v2 写入")
            return 0
        return await asyncio.to_thread(
            self._upsert_sync, entities, vectors, kb_id, kb_code, visible
        )

    def _connect(self, alias: str) -> None:
        _connect_milvus(alias)

    def _build_schema(self, dim: int):
        from pymilvus import CollectionSchema, DataType, FieldSchema

        fields = [
            FieldSchema(name="entity_id", dtype=DataType.VARCHAR, max_length=128, is_primary=True),
            FieldSchema(name="vector", dtype=DataType.FLOAT_VECTOR, dim=dim),
            FieldSchema(name="kb_id", dtype=DataType.VARCHAR, max_length=64),
            FieldSchema(name="kb_code", dtype=DataType.VARCHAR, max_length=64),
            FieldSchema(name="entity_name", dtype=DataType.VARCHAR, max_length=255),
            FieldSchema(name="normalized_name", dtype=DataType.VARCHAR, max_length=255),
            FieldSchema(name="entity_type", dtype=DataType.VARCHAR, max_length=64),
            FieldSchema(name="doc_ids", dtype=DataType.VARCHAR, max_length=4096),
            FieldSchema(name="chunk_ids", dtype=DataType.VARCHAR, max_length=8192),
            FieldSchema(name="support_count", dtype=DataType.INT64),
            FieldSchema(name="status", dtype=DataType.VARCHAR, max_length=32),
            FieldSchema(name="is_visible", dtype=DataType.BOOL),
            FieldSchema(name="created_at_ts", dtype=DataType.INT64),
        ]
        return CollectionSchema(fields, description="self-ingestion entities v2")

    def _ensure_collection(self, alias: str, dim: int):
        from pymilvus import Collection, utility

        if utility.has_collection(self.collection_name, using=alias):
            collection = Collection(self.collection_name, using=alias)
            try:
                collection.load()
            except Exception:
                pass
            existing_fields = {f.name for f in collection.schema.fields}
            missing = _REQUIRED_ENTITY_FIELDS - existing_fields
            if missing:
                logger.error(
                    "[self-ingestion] Milvus collection %s 缺少字段 %s，本次跳过 entities_v2 写入。",
                    self.collection_name, sorted(missing),
                )
                return collection, False
            return collection, True

        collection = Collection(self.collection_name, schema=self._build_schema(dim), using=alias)
        collection.create_index(
            field_name="vector",
            index_params={"metric_type": "COSINE", "index_type": "FLAT", "params": {}},
        )
        for scalar_field in (
            "kb_id", "entity_type", "status", "is_visible", "support_count", "created_at_ts",
        ):
            try:
                collection.create_index(field_name=scalar_field)
            except Exception as exc:
                logger.debug(
                    "[self-ingestion] Milvus entity scalar index %s create skipped: %s",
                    scalar_field, exc,
                )
        collection.load()
        logger.info("[self-ingestion] Milvus collection created: %s", self.collection_name)
        return collection, True

    @staticmethod
    def _row_from_entity(
        entity: Any,
        vector: Sequence[float],
        kb_id: str,
        kb_code: str,
        visible: bool,
    ) -> dict[str, Any]:
        return {
            "entity_id": _varchar(entity.entity_id, 128),
            "vector": list(vector),
            "kb_id": _varchar(kb_id, 64),
            "kb_code": _varchar(kb_code, 64),
            "entity_name": _varchar(entity.name, 255),
            "normalized_name": _varchar(entity.normalized_name, 255),
            "entity_type": _varchar(entity.entity_type, 64),
            "doc_ids": _varchar(json.dumps(sorted(entity.doc_ids), ensure_ascii=False), 4096),
            "chunk_ids": _varchar(json.dumps(sorted(entity.chunk_ids), ensure_ascii=False), 8192),
            "support_count": int(len(entity.chunk_ids)),
            "status": _varchar(
                getattr(entity, "status", states.ENTITY_ACTIVE) or states.ENTITY_ACTIVE,
                32,
            ),
            "is_visible": bool(visible),
            "created_at_ts": int(datetime.now().timestamp()),
        }

    def _upsert_sync(
        self,
        entities: Sequence[Any],
        vectors: Sequence[Sequence[float]],
        kb_id: str,
        kb_code: str,
        visible: bool,
    ) -> int:
        alias = f"self_entities_{id(self)}"
        try:
            from pymilvus import connections

            dim = len(vectors[0])
            self._connect(alias)
            collection, compatible = self._ensure_collection(alias, dim)
            if not compatible:
                return 0
            entity_ids = [entity.entity_id for entity in entities]
            if entity_ids:
                try:
                    _delete_by_entity_ids(collection, entity_ids)
                except Exception as exc:
                    logger.debug("[self-ingestion] Milvus old entities delete skipped: %s", exc)
            rows = [
                self._row_from_entity(entity, vector, kb_id, kb_code, visible)
                for entity, vector in zip(entities, vectors)
            ]
            batch_size = _milvus_insert_batch_size()
            for offset in range(0, len(rows), batch_size):
                collection.insert(rows[offset : offset + batch_size])
            collection.flush()
            return len(rows)
        except Exception as exc:
            logger.warning("[self-ingestion] Milvus entities_v2 写入失败: %s", exc)
            return 0
        finally:
            try:
                connections.disconnect(alias)
            except Exception:
                pass
