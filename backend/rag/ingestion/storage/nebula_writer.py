"""Optional NebulaGraph writer for self-ingestion graph_v2 data."""

from __future__ import annotations

import asyncio
import json
import time
from concurrent.futures import ThreadPoolExecutor, as_completed
from dataclasses import dataclass, field
from datetime import datetime
from typing import Any, Iterable, Sequence

from backend.config.loggers import get_logger
from backend.config.settings import settings

logger = get_logger(__name__)

# Treat more than this fraction of entity/relation writes as job failure.
_MAX_WRITE_FAILURE_RATE = 0.01
_MAX_FAILURE_LOG_SAMPLES = 3
_SCHEMA_READY_RETRIES = 30
_SCHEMA_READY_DELAY_SECONDS = 0.5
_DEFAULT_INSERT_BATCH_SIZE = 80
_DEFAULT_WRITE_WORKERS = 4


def _write_evidence_enabled() -> bool:
    return bool(getattr(settings, "self_ingestion_nebula_write_evidence", False))


def _insert_batch_size() -> int:
    raw = getattr(settings, "self_ingestion_nebula_insert_batch_size", _DEFAULT_INSERT_BATCH_SIZE)
    return max(int(raw or _DEFAULT_INSERT_BATCH_SIZE), 1)


def _write_workers() -> int:
    raw = getattr(settings, "self_ingestion_nebula_write_workers", _DEFAULT_WRITE_WORKERS)
    return max(int(raw or _DEFAULT_WRITE_WORKERS), 1)


def _pool_size() -> int:
    configured = int(getattr(settings, "nebula_max_connection_pool_size", 100) or 100)
    return max(configured, _write_workers() + 2)


@dataclass
class NebulaWriteStats:
    """Per-upsert counters used to detect silent partial writes."""

    entities_attempted: int = 0
    entities_succeeded: int = 0
    relations_attempted: int = 0
    relations_succeeded: int = 0
    evidence_attempted: int = 0
    evidence_succeeded: int = 0
    failure_samples: list[str] = field(default_factory=list)

    def record_failure(self, message: str) -> None:
        if len(self.failure_samples) < _MAX_FAILURE_LOG_SAMPLES:
            self.failure_samples.append(message)

    def entity_failure_rate(self) -> float:
        if not self.entities_attempted:
            return 0.0
        failed = self.entities_attempted - self.entities_succeeded
        return failed / self.entities_attempted

    def relation_failure_rate(self) -> float:
        if not self.relations_attempted:
            return 0.0
        failed = self.relations_attempted - self.relations_succeeded
        return failed / self.relations_attempted

    def is_success(self, *, entity_count: int, relation_count: int) -> bool:
        if entity_count and self.entities_succeeded == 0:
            return False
        if relation_count and self.relations_succeeded == 0:
            return False
        if self.entity_failure_rate() > _MAX_WRITE_FAILURE_RATE:
            return False
        if self.relation_failure_rate() > _MAX_WRITE_FAILURE_RATE:
            return False
        return True

    def merge(self, other: "NebulaWriteStats") -> None:
        self.entities_attempted += other.entities_attempted
        self.entities_succeeded += other.entities_succeeded
        self.relations_attempted += other.relations_attempted
        self.relations_succeeded += other.relations_succeeded
        self.evidence_attempted += other.evidence_attempted
        self.evidence_succeeded += other.evidence_succeeded
        for sample in other.failure_samples:
            self.record_failure(sample)


def _ensure_utf8(value: Any) -> str:
    """Normalize text to valid UTF-8 before embedding in nGQL."""
    if value is None:
        return ""
    if isinstance(value, bytes):
        return value.decode("utf-8", errors="replace")
    text = str(value)
    return text.encode("utf-8", errors="replace").decode("utf-8")


def _escape(value: Any) -> str:
    return (
        _ensure_utf8(value)
        .replace("\\", "\\\\")
        .replace('"', '\\"')
        .replace("\n", "\\n")
        .replace("\r", "\\r")
        .replace("\t", "\\t")
        .replace("\x00", "")
    )


def _safe_error_msg(result) -> str:
    try:
        return result.error_msg() or ""
    except Exception:
        return "unknown nebula error"


def _execute_ngql(session, stmt: str):
    """Execute nGQL; tolerate malformed UTF-8 in server error payloads."""
    try:
        return session.execute(stmt)
    except UnicodeDecodeError as exc:
        raise RuntimeError(f"NebulaGraph response decode failed: {exc}") from exc


def _is_schema_missing(result) -> bool:
    msg = _safe_error_msg(result).lower()
    return "no schema found" in msg or "unknown tag" in msg or "unknown edge" in msg


class NebulaGraphV2Writer:
    """Write self-ingestion entities and relations without touching legacy tags."""

    def __init__(self, space: str | None):
        self.space = (space or "").strip()

    async def upsert_graph(self, extraction, *, visible: bool = True) -> bool:
        if not self.space:
            return False
        return await asyncio.to_thread(self._upsert_graph_sync, extraction, visible)

    async def ensure_schema(self) -> bool:
        """Ensure the self-ingestion v2 graph schema exists for this space."""
        if not self.space:
            return False
        return await asyncio.to_thread(self._ensure_schema_sync)

    async def hide_entities(self, entity_ids: list[str]) -> bool:
        """将给定实体顶点 is_visible 置 false（用于删除文档后 support_count=0 的 orphaned 实体）。"""
        if not self.space or not entity_ids:
            return False
        return await asyncio.to_thread(self._hide_entities_sync, entity_ids)

    async def hide_relations(self, relation_ids: list[str]) -> bool:
        """将给定 relation_id 对应的 relation_v2 边置为不可见。"""
        if not self.space or not relation_ids:
            return False
        return await asyncio.to_thread(self._hide_relations_sync, relation_ids)

    async def hide_doc_graph(self, *, doc_id: str, chunk_ids: list[str]) -> bool:
        """Hide graph_v2 facts sourced from a deleted/superseded document."""
        if not self.space or (not doc_id and not chunk_ids):
            return False
        return await asyncio.to_thread(self._hide_doc_graph_sync, doc_id, chunk_ids)

    def _connect(self):
        from nebula3.Config import Config
        from nebula3.gclient.net import ConnectionPool

        config = Config()
        config.max_connection_pool_size = _pool_size()
        pool = ConnectionPool()
        ok = pool.init([(settings.nebula_host, settings.nebula_graph_port)], config)
        if not ok:
            raise RuntimeError("连接 NebulaGraph 失败")
        return pool

    def _ensure_schema_sync(self) -> bool:
        pool = None
        session = None
        try:
            pool = self._connect()
            session = pool.get_session(settings.nebula_user, settings.nebula_password)
            space = _ensure_utf8(self.space)
            result = _execute_ngql(session, f"USE `{space}`")
            if not result.is_succeeded():
                raise RuntimeError(f"NebulaGraph Space 不可用: {_safe_error_msg(result)}")
            self._ensure_schema(session)
            logger.info("[self-ingestion] NebulaGraph v2 schema ready: space=%s", self.space)
            return True
        except Exception as exc:
            logger.warning("[self-ingestion] NebulaGraph v2 schema 初始化失败: space=%s error=%s", self.space, exc)
            return False
        finally:
            if session is not None:
                session.release()
            if pool is not None:
                pool.close()

    def _upsert_graph_sync(self, extraction, visible: bool = True) -> bool:
        if not extraction.entities and not extraction.relations:
            return False
        pool = None
        session = None
        try:
            pool = self._connect()
            session = pool.get_session(settings.nebula_user, settings.nebula_password)
            space = _ensure_utf8(self.space)
            result = _execute_ngql(session, f"USE `{space}`")
            if not result.is_succeeded():
                raise RuntimeError(f"NebulaGraph Space 不可用: {_safe_error_msg(result)}")
            self._ensure_schema(session)
            stats = NebulaWriteStats()
            entity_count = len(extraction.entities)
            relation_count = len(extraction.relations)
            entity_items = list(extraction.entities.values())
            relation_items = list(extraction.relations.values())
            workers = _write_workers()
            if workers > 1 and (len(entity_items) + len(relation_items)) >= _insert_batch_size():
                self._insert_entities_sharded(pool, space, entity_items, visible, stats, workers)
                self._insert_relations_sharded(pool, space, relation_items, visible, stats, workers)
            else:
                self._insert_entities(session, entity_items, visible, stats)
                self._insert_relations(session, relation_items, visible, stats)
            ok = stats.is_success(entity_count=entity_count, relation_count=relation_count)
            if ok:
                self._submit_stats_job(session)
                logger.info(
                    "[self-ingestion] NebulaGraph v2 写入完成: space=%s "
                    "entities=%s/%s relations=%s/%s evidence=%s/%s",
                    self.space,
                    stats.entities_succeeded,
                    stats.entities_attempted,
                    stats.relations_succeeded,
                    stats.relations_attempted,
                    stats.evidence_succeeded,
                    stats.evidence_attempted,
                )
            else:
                logger.warning(
                    "[self-ingestion] NebulaGraph v2 写入不完整: space=%s "
                    "entities=%s/%s relations=%s/%s evidence=%s/%s samples=%s",
                    self.space,
                    stats.entities_succeeded,
                    stats.entities_attempted,
                    stats.relations_succeeded,
                    stats.relations_attempted,
                    stats.evidence_succeeded,
                    stats.evidence_attempted,
                    stats.failure_samples,
                )
            return ok
        except Exception as exc:
            logger.warning("[self-ingestion] NebulaGraph v2 写入失败: space=%s error=%s", self.space, exc)
            return False
        finally:
            if session is not None:
                session.release()
            if pool is not None:
                pool.close()

    def _ensure_schema(self, session) -> None:
        statements = [
            """
            CREATE TAG IF NOT EXISTS entity_v2 (
                entity_id STRING,
                entity_name STRING,
                entity_type STRING,
                normalized_name STRING,
                description STRING,
                source_id STRING,
                chunk_ids STRING,
                is_visible BOOL,
                created_at TIMESTAMP
            )
            """,
            """
            CREATE EDGE IF NOT EXISTS relation_v2 (
                relation_id STRING,
                relation_type STRING,
                description STRING,
                source_id STRING,
                evidence_chunk_ids STRING,
                weight DOUBLE,
                status STRING,
                is_visible BOOL,
                created_at TIMESTAMP
            )
            """,
            "ALTER EDGE relation_v2 ADD (status STRING)",
            """
            CREATE TAG IF NOT EXISTS evidence_v2 (
                evidence_id STRING,
                evidence_type STRING,
                evidence_text STRING,
                chunk_id STRING,
                doc_id STRING,
                block_id STRING,
                source STRING,
                file_name STRING,
                page_num INT,
                confidence DOUBLE,
                status STRING,
                commit_version INT,
                is_visible BOOL,
                created_at TIMESTAMP
            )
            """,
            "CREATE TAG INDEX IF NOT EXISTS entity_v2_id_idx ON entity_v2(entity_id(128))",
            "CREATE TAG INDEX IF NOT EXISTS entity_v2_name_idx ON entity_v2(normalized_name(128))",
            "CREATE TAG INDEX IF NOT EXISTS entity_v2_source_idx ON entity_v2(source_id(128))",
            "CREATE EDGE INDEX IF NOT EXISTS relation_v2_id_idx ON relation_v2(relation_id(128))",
            "CREATE EDGE INDEX IF NOT EXISTS relation_v2_source_idx ON relation_v2(source_id(128))",
            "CREATE TAG INDEX IF NOT EXISTS evidence_v2_id_idx ON evidence_v2(evidence_id)",
            "CREATE TAG INDEX IF NOT EXISTS evidence_v2_doc_idx ON evidence_v2(doc_id(128))",
            "CREATE TAG INDEX IF NOT EXISTS evidence_v2_chunk_idx ON evidence_v2(chunk_id(128))",
        ]
        for stmt in statements:
            result = _execute_ngql(session, stmt)
            if not result.is_succeeded() and "Existed" not in _safe_error_msg(result):
                logger.debug("[self-ingestion] NebulaGraph v2 schema warning: %s", _safe_error_msg(result))
        self._wait_for_schema_ready(session)

    @staticmethod
    def _submit_stats_job(session) -> None:
        """Best-effort stats refresh so MATCH/LOOKUP can see newly written tags."""
        result = _execute_ngql(session, "SUBMIT JOB STATS")
        if not result.is_succeeded():
            logger.debug(
                "[self-ingestion] NebulaGraph SUBMIT JOB STATS warning: %s",
                _safe_error_msg(result),
            )

    def _wait_for_schema_ready(self, session) -> None:
        checks = [
            "DESCRIBE TAG entity_v2",
            "DESCRIBE EDGE relation_v2",
        ]
        if _write_evidence_enabled():
            checks.append("DESCRIBE TAG evidence_v2")
        for attempt in range(_SCHEMA_READY_RETRIES):
            ready = True
            for stmt in checks:
                result = _execute_ngql(session, stmt)
                if not result.is_succeeded():
                    ready = False
                    break
            if ready:
                return
            if attempt < _SCHEMA_READY_RETRIES - 1:
                time.sleep(_SCHEMA_READY_DELAY_SECONDS)
        logger.debug("[self-ingestion] NebulaGraph v2 schema may not be visible yet")

    def _execute_write_with_schema_retry(self, session, stmt: str):
        result = _execute_ngql(session, stmt)
        if result.is_succeeded() or not _is_schema_missing(result):
            return result
        for attempt in range(_SCHEMA_READY_RETRIES):
            self._ensure_schema(session)
            if attempt:
                time.sleep(_SCHEMA_READY_DELAY_SECONDS)
            result = _execute_ngql(session, stmt)
            if result.is_succeeded() or not _is_schema_missing(result):
                return result
        return result

    def _insert_entities_sharded(
        self,
        pool,
        space: str,
        entities: Sequence,
        visible: bool,
        stats: NebulaWriteStats,
        workers: int,
    ) -> None:
        shards = self._shard_items(list(entities), workers)
        with ThreadPoolExecutor(max_workers=len(shards)) as executor:
            futures = [
                executor.submit(self._insert_entities_worker, pool, space, shard, visible)
                for shard in shards
                if shard
            ]
            for future in as_completed(futures):
                stats.merge(future.result())

    def _insert_relations_sharded(
        self,
        pool,
        space: str,
        relations: Sequence,
        visible: bool,
        stats: NebulaWriteStats,
        workers: int,
    ) -> None:
        shards = self._shard_items(list(relations), workers)
        with ThreadPoolExecutor(max_workers=len(shards)) as executor:
            futures = [
                executor.submit(self._insert_relations_worker, pool, space, shard, visible)
                for shard in shards
                if shard
            ]
            for future in as_completed(futures):
                stats.merge(future.result())

    @staticmethod
    def _shard_items(items: list, workers: int) -> list[list]:
        if not items:
            return []
        worker_count = min(max(workers, 1), len(items))
        shard_size = (len(items) + worker_count - 1) // worker_count
        return [items[offset : offset + shard_size] for offset in range(0, len(items), shard_size)]

    def _insert_entities_worker(self, pool, space: str, entities: list, visible: bool) -> NebulaWriteStats:
        stats = NebulaWriteStats()
        session = pool.get_session(settings.nebula_user, settings.nebula_password)
        try:
            result = _execute_ngql(session, f"USE `{space}`")
            if not result.is_succeeded():
                raise RuntimeError(f"NebulaGraph Space 不可用: {_safe_error_msg(result)}")
            self._insert_entities(session, entities, visible, stats)
            return stats
        finally:
            session.release()

    def _insert_relations_worker(self, pool, space: str, relations: list, visible: bool) -> NebulaWriteStats:
        stats = NebulaWriteStats()
        session = pool.get_session(settings.nebula_user, settings.nebula_password)
        try:
            result = _execute_ngql(session, f"USE `{space}`")
            if not result.is_succeeded():
                raise RuntimeError(f"NebulaGraph Space 不可用: {_safe_error_msg(result)}")
            self._insert_relations(session, relations, visible, stats)
            return stats
        finally:
            session.release()

    @staticmethod
    def _entity_value_clause(entity, visible: bool) -> tuple[str, str]:
        source_id = ",".join(sorted(entity.doc_ids)[:20])
        chunk_ids = json.dumps(sorted(entity.chunk_ids)[:200], ensure_ascii=False)
        vid = _escape(entity.entity_id)
        values = (
            f'"{_escape(entity.entity_id)}", '
            f'"{_escape(entity.name)}", '
            f'"{_escape(entity.entity_type)}", '
            f'"{_escape(entity.normalized_name)}", '
            '"", '
            f'"{_escape(source_id)}", '
            f'"{_escape(chunk_ids)}", '
            f'{"true" if visible else "false"}, timestamp()'
        )
        return vid, values

    @staticmethod
    def _relation_value_clause(relation, visible: bool, rank: int) -> tuple[str, str]:
        evidence_chunk_ids = json.dumps(sorted(relation.chunk_ids)[:200], ensure_ascii=False)
        source_id = ",".join(sorted(getattr(relation, "doc_ids", set()) or [])[:20])
        if not source_id:
            source_id = ",".join(sorted(relation.chunk_ids)[:20])
        edge_ref = (
            f'"{_escape(relation.source_entity_id)}"->"{_escape(relation.target_entity_id)}"@{rank}'
        )
        values = (
            f'"{_escape(relation.relation_id)}", '
            f'"{_escape(relation.relation_type)}", '
            '"", '
            f'"{_escape(source_id)}", '
            f'"{_escape(evidence_chunk_ids)}", '
            f"{float(len(relation.chunk_ids) or 1)}, "
            f'"{_escape(getattr(relation, "status", "weak_evidence") or "weak_evidence")}", '
            f'{"true" if visible else "false"}, timestamp()'
        )
        return edge_ref, values

    def _execute_batched_insert(
        self,
        session,
        *,
        kind: str,
        prop_list: str,
        clauses: list[tuple[str, str]],
        stats: NebulaWriteStats,
        on_success,
        on_failure,
    ) -> None:
        if not clauses:
            return
        batch_size = _insert_batch_size()
        is_vertex = kind.startswith("VERTEX")
        for offset in range(0, len(clauses), batch_size):
            batch = clauses[offset : offset + batch_size]
            value_sql = ", ".join(
                f'"{ref}":({vals})' if is_vertex else f"{ref}:({vals})"
                for ref, vals in batch
            )
            stmt = f"INSERT {kind} {prop_list} VALUES {value_sql}"
            try:
                result = self._execute_write_with_schema_retry(session, stmt)
            except Exception as exc:
                if len(batch) == 1:
                    on_failure(batch[0], str(exc))
                    continue
                mid = len(batch) // 2
                self._execute_batched_insert(
                    session,
                    kind=kind,
                    prop_list=prop_list,
                    clauses=batch[:mid],
                    stats=stats,
                    on_success=on_success,
                    on_failure=on_failure,
                )
                self._execute_batched_insert(
                    session,
                    kind=kind,
                    prop_list=prop_list,
                    clauses=batch[mid:],
                    stats=stats,
                    on_success=on_success,
                    on_failure=on_failure,
                )
                continue
            if result.is_succeeded():
                on_success(len(batch))
                continue
            if len(batch) == 1:
                on_failure(batch[0], _safe_error_msg(result))
                continue
            mid = len(batch) // 2
            self._execute_batched_insert(
                session,
                kind=kind,
                prop_list=prop_list,
                clauses=batch[:mid],
                stats=stats,
                on_success=on_success,
                on_failure=on_failure,
            )
            self._execute_batched_insert(
                session,
                kind=kind,
                prop_list=prop_list,
                clauses=batch[mid:],
                stats=stats,
                on_success=on_success,
                on_failure=on_failure,
            )

    def _insert_entities(
        self,
        session,
        entities: Iterable,
        visible: bool,
        stats: NebulaWriteStats,
    ) -> None:
        clauses: list[tuple[str, str]] = []
        for entity in entities:
            stats.entities_attempted += 1
            try:
                clauses.append(self._entity_value_clause(entity, visible))
            except Exception as exc:
                msg = f"entity_v2 id={getattr(entity, 'entity_id', '?')}: {exc}"
                stats.record_failure(msg)
                logger.warning("[self-ingestion] NebulaGraph %s", msg)

        def on_success(count: int) -> None:
            stats.entities_succeeded += count

        def on_failure(item: tuple[str, str], error_msg: str) -> None:
            msg = f"entity_v2 id={item[0]}: {error_msg}"
            stats.record_failure(msg)
            logger.warning("[self-ingestion] NebulaGraph %s", msg)

        self._execute_batched_insert(
            session,
            kind="VERTEX entity_v2",
            prop_list="(entity_id, entity_name, entity_type, normalized_name, description, source_id, chunk_ids, is_visible, created_at)",
            clauses=clauses,
            stats=stats,
            on_success=on_success,
            on_failure=on_failure,
        )

    def _insert_relations(
        self,
        session,
        relations: Iterable,
        visible: bool,
        stats: NebulaWriteStats,
    ) -> None:
        now_rank = int(datetime.now().timestamp() * 1000)
        edge_clauses: list[tuple[str, str]] = []
        relation_records: list[tuple[Any, int]] = []
        for offset, relation in enumerate(relations):
            relation_id = getattr(relation, "relation_id", "?")
            stats.relations_attempted += 1
            try:
                edge_clauses.append(self._relation_value_clause(relation, visible, now_rank + offset))
                relation_records.append((relation, now_rank + offset))
            except Exception as exc:
                msg = f"relation_v2 id={relation_id}: {exc}"
                stats.record_failure(msg)
                logger.warning("[self-ingestion] NebulaGraph %s", msg)


        def on_success(count: int) -> None:
            stats.relations_succeeded += count

        def on_failure(item: tuple[str, str], error_msg: str) -> None:
            msg = f"relation_v2 edge={item[0]}: {error_msg}"
            stats.record_failure(msg)
            logger.warning("[self-ingestion] NebulaGraph %s", msg)

        self._execute_batched_insert(
            session,
            kind="EDGE relation_v2",
            prop_list="(relation_id, relation_type, description, source_id, evidence_chunk_ids, weight, status, is_visible, created_at)",
            clauses=edge_clauses,
            stats=stats,
            on_success=on_success,
            on_failure=on_failure,
        )

        if not _write_evidence_enabled():
            return

        for relation, _rank in relation_records:
            relation_id = getattr(relation, "relation_id", "?")
            doc_id = ""
            relation_doc_ids = sorted(getattr(relation, "doc_ids", set()) or [])
            if relation_doc_ids:
                doc_id = relation_doc_ids[0]
            evidence_clauses: list[tuple[str, str]] = []
            for evidence_offset, (chunk_id, evidence_text) in enumerate(relation.evidence.items(), start=1):
                stats.evidence_attempted += 1
                evidence_vid = f"ev_{_escape(relation.relation_id)}_{_escape(chunk_id)}_{evidence_offset}"
                values = (
                    f'"{evidence_vid}", '
                    '"relation_evidence", '
                    f'"{_escape(_ensure_utf8(evidence_text)[:1000])}", '
                    f'"{_escape(chunk_id)}", '
                    f'"{_escape(doc_id)}", '
                    '"", '
                    '"text", '
                    '"", '
                    "0, "
                    "1.0, "
                    '"active", '
                    "1, "
                    f'{"true" if visible else "false"}, timestamp()'
                )
                evidence_clauses.append((evidence_vid, values))

            def evidence_success(count: int) -> None:
                stats.evidence_succeeded += count

            def evidence_failure(item: tuple[str, str], error_msg: str) -> None:
                msg = f"evidence_v2 id={item[0]} relation={relation_id}: {error_msg}"
                stats.record_failure(msg)
                logger.warning("[self-ingestion] NebulaGraph %s", msg)

            self._execute_batched_insert(
                session,
                kind="VERTEX evidence_v2",
                prop_list="(evidence_id, evidence_type, evidence_text, chunk_id, doc_id, block_id, source, file_name, page_num, confidence, status, commit_version, is_visible, created_at)",
                clauses=evidence_clauses,
                stats=stats,
                on_success=evidence_success,
                on_failure=evidence_failure,
            )

    @staticmethod
    def _value_to_python(value: Any) -> Any:
        if value is None or isinstance(value, (str, int, float, bool)):
            return value
        for method in ("as_string", "as_int", "as_double", "as_bool"):
            reader = getattr(value, method, None)
            if callable(reader):
                try:
                    return reader()
                except Exception:
                    continue
        try:
            return value.cast()
        except Exception:
            return str(value)

    def _hide_entities_sync(self, entity_ids: list[str]) -> bool:
        """best-effort：将 orphaned 实体顶点 is_visible 置 false。

        关系边由 hide_relations 按 relation_id 另行隐藏；证据顶点目前只作为审计数据，
        Path-D 不直接读取 evidence_v2。
        """
        pool = None
        session = None
        try:
            pool = self._connect()
            session = pool.get_session(settings.nebula_user, settings.nebula_password)
            space = _ensure_utf8(self.space)
            result = _execute_ngql(session, f"USE `{space}`")
            if not result.is_succeeded():
                return False
            for entity_id in entity_ids:
                stmt = (
                    f'UPDATE VERTEX ON entity_v2 "{_escape(entity_id)}" '
                    "SET is_visible = false"
                )
                exec_result = _execute_ngql(session, stmt)
                if not exec_result.is_succeeded():
                    logger.debug(
                        "[self-ingestion] NebulaGraph hide entity warning: %s",
                        _safe_error_msg(exec_result),
                    )
            return True
        except Exception as exc:
            logger.warning("[self-ingestion] NebulaGraph hide_entities 失败: %s", exc)
            return False
        finally:
            if session is not None:
                session.release()
            if pool is not None:
                pool.close()

    def _hide_relations_sync(self, relation_ids: list[str]) -> bool:
        """best-effort：按 relation_id 查询 relation_v2 边并标记 is_visible=false。"""
        pool = None
        session = None
        try:
            pool = self._connect()
            session = pool.get_session(settings.nebula_user, settings.nebula_password)
            space = _ensure_utf8(self.space)
            result = _execute_ngql(session, f"USE `{space}`")
            if not result.is_succeeded():
                return False

            ok = True
            for relation_id in relation_ids:
                lookup_stmt = (
                    "LOOKUP ON relation_v2 "
                    f'WHERE relation_v2.relation_id == "{_escape(relation_id)}" '
                    "YIELD src(edge) AS src, dst(edge) AS dst, rank(edge) AS rank"
                )
                lookup_result = _execute_ngql(session, lookup_stmt)
                if not lookup_result.is_succeeded():
                    ok = False
                    logger.debug(
                        "[self-ingestion] NebulaGraph relation_v2 lookup warning: %s",
                        _safe_error_msg(lookup_result),
                    )
                    continue
                for idx in range(lookup_result.row_size()):
                    row = lookup_result.row_values(idx)
                    src = self._value_to_python(row[0]) if len(row) > 0 else ""
                    dst = self._value_to_python(row[1]) if len(row) > 1 else ""
                    rank = self._value_to_python(row[2]) if len(row) > 2 else 0
                    if not src or not dst:
                        ok = False
                        continue
                    stmt = (
                        f'UPDATE EDGE ON relation_v2 "{_escape(src)}"->"{_escape(dst)}"@{int(rank or 0)} '
                        'SET is_visible = false, status = "inactive"'
                    )
                    exec_result = _execute_ngql(session, stmt)
                    if not exec_result.is_succeeded():
                        ok = False
                        logger.debug(
                            "[self-ingestion] NebulaGraph hide relation warning: %s",
                            _safe_error_msg(exec_result),
                        )
            return ok
        except Exception as exc:
            logger.warning("[self-ingestion] NebulaGraph hide_relations 失败: %s", exc)
            return False
        finally:
            if session is not None:
                session.release()
            if pool is not None:
                pool.close()

    def _hide_doc_graph_sync(self, doc_id: str, chunk_ids: list[str]) -> bool:
        """best-effort：按 doc_id/chunk_id 隐藏 v2 图谱事实。"""
        pool = None
        session = None
        try:
            pool = self._connect()
            session = pool.get_session(settings.nebula_user, settings.nebula_password)
            space = _ensure_utf8(self.space)
            result = _execute_ngql(session, f"USE `{space}`")
            if not result.is_succeeded():
                return False
            self._ensure_schema(session)

            ok = True
            if doc_id:
                ok = self._hide_entities_by_source(session, doc_id) and ok
                ok = self._hide_relations_by_source(session, doc_id) and ok
                if _write_evidence_enabled():
                    ok = self._hide_evidence_by_doc(session, doc_id) and ok
            for chunk_id in sorted({item for item in chunk_ids if item}):
                ok = self._hide_entities_by_chunk(session, chunk_id) and ok
                ok = self._hide_relations_by_chunk(session, chunk_id) and ok
                if _write_evidence_enabled():
                    ok = self._hide_evidence_by_chunk(session, chunk_id) and ok
            return ok
        except Exception as exc:
            logger.warning("[self-ingestion] NebulaGraph hide_doc_graph 失败: %s", exc)
            return False
        finally:
            if session is not None:
                session.release()
            if pool is not None:
                pool.close()

    def _hide_entities_by_source(self, session, doc_id: str) -> bool:
        lookup = (
            "LOOKUP ON entity_v2 "
            f'WHERE entity_v2.source_id CONTAINS "{_escape(doc_id)}" '
            "YIELD id(vertex) AS vid"
        )
        return self._hide_vertices_from_lookup(session, lookup, "entity_v2")

    def _hide_entities_by_chunk(self, session, chunk_id: str) -> bool:
        lookup = (
            "LOOKUP ON entity_v2 "
            f'WHERE entity_v2.chunk_ids CONTAINS "{_escape(chunk_id)}" '
            "YIELD id(vertex) AS vid"
        )
        return self._hide_vertices_from_lookup(session, lookup, "entity_v2")

    def _hide_evidence_by_doc(self, session, doc_id: str) -> bool:
        lookup = (
            "LOOKUP ON evidence_v2 "
            f'WHERE evidence_v2.doc_id == "{_escape(doc_id)}" '
            "YIELD id(vertex) AS vid"
        )
        return self._hide_vertices_from_lookup(session, lookup, "evidence_v2", status="deleted")

    def _hide_evidence_by_chunk(self, session, chunk_id: str) -> bool:
        lookup = (
            "LOOKUP ON evidence_v2 "
            f'WHERE evidence_v2.chunk_id == "{_escape(chunk_id)}" '
            "YIELD id(vertex) AS vid"
        )
        return self._hide_vertices_from_lookup(session, lookup, "evidence_v2", status="deleted")

    def _hide_relations_by_source(self, session, doc_id: str) -> bool:
        lookup = (
            "LOOKUP ON relation_v2 "
            f'WHERE relation_v2.source_id CONTAINS "{_escape(doc_id)}" '
            "YIELD src(edge) AS src, dst(edge) AS dst, rank(edge) AS rank"
        )
        return self._hide_edges_from_lookup(session, lookup)

    def _hide_relations_by_chunk(self, session, chunk_id: str) -> bool:
        lookup = (
            "LOOKUP ON relation_v2 "
            f'WHERE relation_v2.evidence_chunk_ids CONTAINS "{_escape(chunk_id)}" '
            "YIELD src(edge) AS src, dst(edge) AS dst, rank(edge) AS rank"
        )
        return self._hide_edges_from_lookup(session, lookup)

    def _hide_vertices_from_lookup(self, session, lookup_stmt: str, tag: str, *, status: str | None = None) -> bool:
        result = _execute_ngql(session, lookup_stmt)
        if not result.is_succeeded():
            logger.debug("[self-ingestion] NebulaGraph vertex lookup warning: %s", _safe_error_msg(result))
            return False
        ok = True
        for idx in range(result.row_size()):
            row = result.row_values(idx)
            vid = self._value_to_python(row[0]) if row else ""
            if not vid:
                ok = False
                continue
            set_expr = "is_visible = false"
            if status is not None:
                set_expr += f', status = "{_escape(status)}"'
            stmt = f'UPDATE VERTEX ON {tag} "{_escape(vid)}" SET {set_expr}'
            update_result = _execute_ngql(session, stmt)
            if not update_result.is_succeeded():
                ok = False
                logger.debug("[self-ingestion] NebulaGraph hide vertex warning: %s", _safe_error_msg(update_result))
        return ok

    def _hide_edges_from_lookup(self, session, lookup_stmt: str) -> bool:
        result = _execute_ngql(session, lookup_stmt)
        if not result.is_succeeded():
            logger.debug("[self-ingestion] NebulaGraph edge lookup warning: %s", _safe_error_msg(result))
            return False
        ok = True
        for idx in range(result.row_size()):
            row = result.row_values(idx)
            src = self._value_to_python(row[0]) if len(row) > 0 else ""
            dst = self._value_to_python(row[1]) if len(row) > 1 else ""
            rank = self._value_to_python(row[2]) if len(row) > 2 else 0
            if not src or not dst:
                ok = False
                continue
            stmt = (
                f'UPDATE EDGE ON relation_v2 "{_escape(src)}"->"{_escape(dst)}"@{int(rank or 0)} '
                'SET is_visible = false, status = "inactive"'
            )
            update_result = _execute_ngql(session, stmt)
            if not update_result.is_succeeded():
                ok = False
                logger.debug("[self-ingestion] NebulaGraph hide edge warning: %s", _safe_error_msg(update_result))
        return ok
