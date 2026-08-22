"""Periodic in-process scheduling for context/checkpoint garbage collection.

``doc-agent checkpoints-gc`` and ``doc-agent context-objects-gc`` are the
manual maintenance entries; without scheduling, the LangGraph SQLite
checkpoint database and the COS artifact sidecars grow without bound. This
scheduler runs both collectors inside the Agent API process on a fixed
interval.

It is disabled by default (``CONTEXT_MAINTENANCE_GC_INTERVAL_SECONDS=0``):
enabling automatic durable-state deletion stays a deployment decision, but it
is now one environment variable instead of external cron/systemd wiring.
Failures are logged and never propagate into request handling.
"""

from __future__ import annotations

import asyncio
import json
import os
from pathlib import Path

from backend.config.loggers import get_logger

logger = get_logger(__name__)

_MIN_INTERVAL_SECONDS = 300
_MAX_INITIAL_DELAY_SECONDS = 600


def _env_int(name: str, default: int, *, minimum: int = 0) -> int:
    raw = (os.getenv(name, "") or "").strip()
    try:
        value = int(raw) if raw else default
    except ValueError:
        value = default
    return max(minimum, value)


class ContextMaintenanceScheduler:
    """Run checkpoint retention GC and COS TTL GC on a fixed interval."""

    def __init__(self) -> None:
        self._task: asyncio.Task | None = None

    @property
    def interval_seconds(self) -> int:
        interval = _env_int("CONTEXT_MAINTENANCE_GC_INTERVAL_SECONDS", 0)
        if interval <= 0:
            return 0
        return max(_MIN_INTERVAL_SECONDS, interval)

    @property
    def enabled(self) -> bool:
        return self.interval_seconds > 0

    async def start(self) -> None:
        if not self.enabled:
            logger.info(
                "[上下文维护GC] 未启用（CONTEXT_MAINTENANCE_GC_INTERVAL_SECONDS=0）；"
                "可用 doc-agent checkpoints-gc / context-objects-gc 手动维护"
            )
            return
        if self._task is not None and not self._task.done():
            return
        self._task = asyncio.create_task(
            self._run_loop(),
            name="context-maintenance-gc",
        )
        logger.info(
            "[上下文维护GC] 已启动: interval=%ds, keep_per_thread=%d",
            self.interval_seconds,
            _env_int("CHECKPOINT_GC_KEEP_PER_THREAD", 50, minimum=1),
        )

    async def stop(self) -> None:
        task = self._task
        self._task = None
        if task is None or task.done():
            return
        task.cancel()
        try:
            await task
        except (asyncio.CancelledError, Exception):
            pass

    async def _run_loop(self) -> None:
        interval = self.interval_seconds
        await asyncio.sleep(min(interval, _MAX_INITIAL_DELAY_SECONDS))
        while True:
            try:
                await self.run_once()
            except asyncio.CancelledError:
                raise
            except Exception:
                # run_once 内部已按收集器隔离异常；这里兜住收集器之外的
                # 逃逸，避免维护循环静默死亡直到进程重启。
                logger.exception("[上下文维护GC] 单轮执行异常，继续下一轮")
            await asyncio.sleep(interval)

    async def run_once(self) -> dict:
        """Run all collectors once; each failure is contained and logged."""

        report: dict = {}
        try:
            report["checkpoints"] = await asyncio.to_thread(self._gc_checkpoints)
        except Exception:
            logger.warning("[上下文维护GC] checkpoint GC 失败", exc_info=True)
            report["checkpoints"] = {"error": "failed"}
        try:
            report["guest_checkpoints"] = await asyncio.to_thread(
                self._gc_guest_checkpoints
            )
        except Exception:
            logger.warning("[上下文维护GC] guest checkpoint GC 失败", exc_info=True)
            report["guest_checkpoints"] = {"error": "failed"}
        try:
            report["checkpoint_vacuum"] = await asyncio.to_thread(
                self._maybe_vacuum_checkpoint_db
            )
        except Exception:
            logger.warning("[上下文维护GC] checkpoint VACUUM 失败", exc_info=True)
            report["checkpoint_vacuum"] = {"error": "failed"}
        try:
            report["context_objects"] = await asyncio.to_thread(
                self._gc_context_objects
            )
        except Exception:
            logger.warning("[上下文维护GC] COS GC 失败", exc_info=True)
            report["context_objects"] = {"error": "failed"}
        try:
            report["agent_runtime"] = await self._gc_agent_runtime_runs()
        except Exception:
            logger.warning("[上下文维护GC] agent runtime 保留期 GC 失败", exc_info=True)
            report["agent_runtime"] = {"error": "failed"}
        try:
            report["kb_creating_sweep"] = await self._sweep_stuck_kb_creating()
        except Exception:
            logger.warning("[上下文维护GC] KB creating 清扫失败", exc_info=True)
            report["kb_creating_sweep"] = {"error": "failed"}
        try:
            report["guest_catalog"] = await asyncio.to_thread(
                self._gc_guest_catalog
            )
        except Exception:
            logger.warning("[上下文维护GC] 访客会话目录 GC 失败", exc_info=True)
            report["guest_catalog"] = {"error": "failed"}
        logger.info(
            "[上下文维护GC] 本轮完成: %s",
            json.dumps(report, ensure_ascii=False, default=str),
        )
        return report

    @staticmethod
    def _gc_checkpoints() -> dict:
        from backend.application.checkpointing.gc import (
            gc_sqlite_checkpoints,
            resolve_default_checkpoint_db,
        )

        db_path = Path(resolve_default_checkpoint_db())
        if not db_path.exists():
            return {"skipped": "db_missing", "db_path": str(db_path)}
        keep = _env_int("CHECKPOINT_GC_KEEP_PER_THREAD", 50, minimum=1)
        return gc_sqlite_checkpoints(
            db_path,
            keep_per_thread=keep,
            dry_run=False,
            vacuum=False,
        ).as_dict()

    @staticmethod
    def _gc_context_objects() -> dict:
        from backend.core.agents.context.object_store import ContextObjectStore

        store = ContextObjectStore()
        result = store.gc()
        # The removed-oid list can be long; keep the log payload bounded.
        removed = result.get("removed") or []
        return {
            "scanned": result.get("scanned"),
            "kept": result.get("kept"),
            "removed_count": len(removed),
            "ttl_seconds": result.get("ttl_seconds"),
        }

    @staticmethod
    def _gc_guest_checkpoints() -> dict:
        """访客进程的 guest_checkpoints.db 此前不在任何 GC 覆盖内。

        与主库同款 keep-per-thread 语义（老线程保留最近 N 条，不整线程删除）。
        """
        from backend.application.checkpointing.gc import (
            gc_sqlite_checkpoints,
            resolve_default_checkpoint_db,
        )

        guest_name = (os.getenv("GUEST_CHECKPOINT_DB") or "").strip() or (
            "guest_checkpoints.db"
        )
        db_path = Path(resolve_default_checkpoint_db(guest_name))
        if not db_path.exists():
            return {"skipped": "db_missing", "db_path": str(db_path)}
        keep = _env_int("CHECKPOINT_GC_KEEP_PER_THREAD", 50, minimum=1)
        return gc_sqlite_checkpoints(
            db_path,
            keep_per_thread=keep,
            dry_run=False,
            vacuum=False,
        ).as_dict()

    @staticmethod
    def _maybe_vacuum_checkpoint_db() -> dict:
        """opt-in VACUUM：行 GC 只把页放进 freelist，文件停在高水位。

        只有显式开启（CHECKPOINT_GC_VACUUM_ENABLED）且空闲页占比超过阈值
        才执行；VACUUM 会短暂阻塞写，须在维护窗口语义下使用。
        """
        enabled = (os.getenv("CHECKPOINT_GC_VACUUM_ENABLED") or "").strip().lower()
        if enabled not in {"1", "true", "yes", "on"}:
            return {"skipped": "disabled"}
        try:
            threshold = float(
                (os.getenv("CHECKPOINT_GC_VACUUM_MIN_FREE_RATIO") or "0.25").strip()
            )
        except ValueError:
            threshold = 0.25
        threshold = min(max(threshold, 0.05), 0.95)

        import sqlite3
        import time

        from backend.application.checkpointing.gc import (
            resolve_default_checkpoint_db,
        )

        db_path = Path(resolve_default_checkpoint_db())
        if not db_path.exists():
            return {"skipped": "db_missing"}
        conn = sqlite3.connect(db_path, timeout=120)
        try:
            freelist = int(conn.execute("PRAGMA freelist_count").fetchone()[0])
            page_count = int(conn.execute("PRAGMA page_count").fetchone()[0])
            if page_count <= 0:
                return {"skipped": "empty"}
            ratio = freelist / page_count
            if ratio < threshold:
                return {"skipped": "below_threshold", "free_ratio": round(ratio, 3)}
            started = time.monotonic()
            conn.execute("VACUUM")
            return {
                "vacuumed": True,
                "free_ratio_before": round(ratio, 3),
                "seconds": round(time.monotonic() - started, 1),
            }
        finally:
            conn.close()

    @staticmethod
    async def _gc_agent_runtime_runs() -> dict:
        """MySQL agent_runtime_runs/agents/events 终态保留期（默认关）。

        每个聊天轮次写 5-20 行且此前零清理；``AGENT_RUNTIME_RETENTION_DAYS=0``
        表示禁用，启用属部署决策（观测/恢复数据只对近期 run 有价值）。
        """
        retention_days = _env_int("AGENT_RUNTIME_RETENTION_DAYS", 0)
        if retention_days <= 0:
            return {"skipped": "disabled"}
        backend = (os.getenv("AGENT_RUNTIME_STORE_BACKEND") or "").strip().lower()
        if backend not in {"sql", "mysql", "sqlalchemy"}:
            return {"skipped": f"backend={backend or 'default'}"}
        import time as _time

        from backend.core.agents.sql_runtime_store import SQLAlchemyAgentRuntimeStore
        from backend.db.connection import get_engine

        store = SQLAlchemyAgentRuntimeStore(get_engine())
        cutoff = _time.time() - retention_days * 86400
        total = 0
        # 分批删除（每批 500 run，至多 40 批/轮），避免大事务锁表。
        for _ in range(40):
            removed = await store.purge_terminal_runs_before(cutoff, limit=500)
            total += removed
            if removed < 500:
                break
        return {"purged_runs": total, "retention_days": retention_days}


    @staticmethod
    def _gc_guest_catalog() -> dict:
        """匿名访客（guest_* scope）会话目录行保留期 GC。

        无设备头请求每请求铸造新 scope 且访客无删除入口，目录行只增
        不减。目录行的 thread_id 即 guest_checkpoints.db 的内部线程键
        （g_<scope>__t_<ext>，两者同源写入），不可再叠加重构。
        先删 checkpoint 后删目录行：中途崩溃时目录行仍在，下一轮可
        重收；反向则会留下不可重收的 checkpoint 孤儿。每轮限量一批，
        靠调度周期消化积压。
        """
        import sqlite3
        from datetime import datetime, timedelta

        from backend.application.checkpointing.gc import (
            resolve_default_checkpoint_db,
        )

        retention_days = _env_int("GUEST_CATALOG_RETENTION_DAYS", 30, minimum=1)
        cutoff = (datetime.now() - timedelta(days=retention_days)).isoformat()
        guest_name = (os.getenv("GUEST_CHECKPOINT_DB") or "").strip() or (
            "guest_checkpoints.db"
        )
        guest_db = str(resolve_default_checkpoint_db(guest_name))
        raw_catalog = (os.getenv("AGENT_CONVERSATION_SQLITE_PATH") or "").strip()
        if not raw_catalog:
            return {"skipped": "catalog_path_unconfigured"}
        catalog_db = raw_catalog
        if not Path(catalog_db).exists():
            return {"skipped": "catalog_missing"}

        # 1) 收集本批超期访客线程（rowid 界定本批目录行，防止超额误删）；
        # `_` 是 LIKE 通配符，转义后仅匹配字面 guest_ 前缀。
        conn = sqlite3.connect(catalog_db, timeout=30)
        try:
            rows = conn.execute(
                "SELECT rowid, user_id, thread_id FROM conversations "
                "WHERE user_id LIKE 'guest\\_%' ESCAPE '\\' "
                "AND updated_at < ? LIMIT 500",
                (cutoff,),
            ).fetchall()
        finally:
            conn.close()
        if not rows:
            return {"removed_threads": 0}
        row_ids = [row[0] for row in rows]

        # 2) 删除前复验超期：批次收集与删除之间线程可能被唤醒（两次
        # 连接之间的秒级窗口），复验后只处理仍超期的行。
        conn = sqlite3.connect(catalog_db, timeout=30)
        try:
            placeholders = ",".join("?" for _ in row_ids)
            still_stale = conn.execute(
                f"SELECT rowid, thread_id FROM conversations "
                f"WHERE rowid IN ({placeholders}) AND updated_at < ?",
                (*row_ids, cutoff),
            ).fetchall()
        finally:
            conn.close()
        if not still_stale:
            return {"removed_threads": 0}
        stale_ids = [row[0] for row in still_stale]
        internal_tids = [row[1] for row in still_stale if row[1]]

        # 3) 先删 guest checkpoint 库按线程删除（崩溃残留方向：目录行仍在）
        removed_ckpts = 0
        if internal_tids and Path(guest_db).exists():
            gconn = sqlite3.connect(guest_db, timeout=30)
            try:
                for tid in internal_tids:
                    cur = gconn.execute(
                        "DELETE FROM checkpoints WHERE thread_id = ?", (tid,)
                    )
                    removed_ckpts += int(cur.rowcount or 0)
                    gconn.execute("DELETE FROM writes WHERE thread_id = ?", (tid,))
                gconn.commit()
            finally:
                gconn.close()

        # 4) 再删目录行，范围严格限定为复验后仍超期的 rowid
        conn = sqlite3.connect(catalog_db, timeout=30)
        try:
            placeholders = ",".join("?" for _ in stale_ids)
            conn.execute(
                f"DELETE FROM conversations "
                f"WHERE rowid IN ({placeholders}) AND updated_at < ?",
                (*stale_ids, cutoff),
            )
            conn.commit()
        finally:
            conn.close()
        logger.info(
            "[上下文维护GC] 访客会话保留期: threads=%d checkpoints=%d retention=%dd",
            len(rows),
            removed_ckpts,
            retention_days,
        )
        return {
            "removed_threads": len(rows),
            "removed_checkpoints": removed_ckpts,
            "retention_days": retention_days,
        }

    @staticmethod
    async def _sweep_stuck_kb_creating() -> dict:
        """KB ``creating`` 状态进程崩溃后永久卡死（与 INACTIVE 墓碑同类）。

        创建流程在锁外做秒级 Nebula/存储 IO，期间崩溃会让 kb_code 被占位
        且不可用。超时（默认 10 分钟）后置 INACTIVE，允许按既有墓碑语义
        处理（人工清理后可复用编码）。
        """
        from datetime import datetime, timedelta

        from sqlalchemy import update as sa_update

        from backend.db import async_session_context
        from backend.db.models import KnowledgeBase

        try:
            timeout_minutes = _env_int("KB_CREATING_STUCK_MINUTES", 10, minimum=1)
        except Exception:
            timeout_minutes = 10
        cutoff = datetime.now() - timedelta(minutes=timeout_minutes)
        async with async_session_context() as session:
            result = await session.execute(
                sa_update(KnowledgeBase)
                .where(
                    KnowledgeBase.status == "creating",
                    KnowledgeBase.created_at < cutoff,
                )
                .values(status="inactive", updated_at=datetime.now())
            )
            await session.commit()
            swept = int(result.rowcount or 0)
        if swept:
            logger.warning(
                "[上下文维护GC] 清扫卡死的 creating 知识库 %d 个（超时 %d 分钟）",
                swept,
                timeout_minutes,
            )
        return {"swept": swept}


context_maintenance_scheduler = ContextMaintenanceScheduler()
