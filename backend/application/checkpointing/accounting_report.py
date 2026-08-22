"""Read-only aggregation of per-thread context accounting from checkpoints.

`ContextPruningMiddleware` persists `context_budget` and (in COS shadow/on
modes) `context_accounting` into graph state through the model-node command
channel. This module aggregates the latest checkpoint of every thread in a
SQLite checkpoint database into one report, giving the COS convergence
decision real utilization data instead of guesses.

The report is strictly read-only and safe to run against a live database.
"""

from __future__ import annotations

import sqlite3
import statistics
from dataclasses import asdict, dataclass, field
from pathlib import Path
from typing import Any


@dataclass
class ContextAccountingReport:
    db_path: str
    threads_total: int
    threads_with_budget: int
    threads_with_accounting: int
    threads_failed: int = 0
    failure_sample: str = ""
    budget: dict[str, Any] = field(default_factory=dict)
    accounting: dict[str, Any] = field(default_factory=dict)

    def as_dict(self) -> dict[str, Any]:
        return asdict(self)


def _distribution(values: list[float]) -> dict[str, float]:
    if not values:
        return {}
    return {
        "count": len(values),
        "min": min(values),
        "median": statistics.median(values),
        "max": max(values),
        "mean": round(statistics.fmean(values), 2),
    }


def _latest_checkpoint_rows(conn: sqlite3.Connection) -> list[tuple[str, str, str]]:
    """Return (thread_id, checkpoint_ns, checkpoint_id) of each thread's tip."""

    return conn.execute(
        """
        SELECT thread_id, checkpoint_ns, MAX(checkpoint_id)
        FROM checkpoints
        GROUP BY thread_id, checkpoint_ns
        """
    ).fetchall()


def collect_context_accounting(
    db_path: str | Path,
    *,
    limit: int | None = None,
) -> ContextAccountingReport:
    """Aggregate the latest `context_budget`/`context_accounting` per thread."""

    path = Path(db_path)
    if not path.exists():
        raise FileNotFoundError(f"checkpoint database not found: {path}")

    # The sync saver shares the checkpoint (de)serialization contract with the
    # async one used by the API; importing it keeps blob parsing out of here.
    from langgraph.checkpoint.sqlite import SqliteSaver

    conn = sqlite3.connect(str(path))
    try:
        conn.execute("PRAGMA busy_timeout=5000")
        conn.execute("PRAGMA query_only=ON")
        tables = {
            row[0]
            for row in conn.execute(
                "SELECT name FROM sqlite_master WHERE type='table'"
            )
        }
        if "checkpoints" not in tables:
            raise ValueError(
                f"{path} has no 'checkpoints' table; not a LangGraph checkpoint DB"
            )
        saver = SqliteSaver(conn)
        saver.is_setup = True  # read-only: never run setup DDL

        tips = _latest_checkpoint_rows(conn)
        threads_total = len(tips)
        if limit is not None:
            tips = tips[: max(0, int(limit))]

        tokens_used: list[float] = []
        dynamic_budget_used: list[float] = []
        dynamic_utilization: list[float] = []
        items_injected: list[float] = []
        threads_with_budget = 0

        pool_remaining: list[float] = []
        jit_body_tokens: list[float] = []
        navigator_tokens: list[float] = []
        object_cache_hits: list[float] = []
        objects_read: list[float] = []
        selected_segments: list[float] = []
        dropped_by_topk: list[float] = []
        state_object_refs: list[float] = []
        threads_with_accounting = 0

        threads_failed = 0
        failure_sample = ""
        for thread_id, checkpoint_ns, checkpoint_id in tips:
            config = {
                "configurable": {
                    "thread_id": thread_id,
                    "checkpoint_ns": checkpoint_ns,
                    "checkpoint_id": checkpoint_id,
                }
            }
            try:
                checkpoint_tuple = saver.get_tuple(config)
            except Exception as exc:
                # Long-lived databases contain legacy/damaged tip blobs (a
                # truncated msgpack body raises "unexpected end of file").
                # One bad thread must not abort the whole convergence report.
                threads_failed += 1
                if not failure_sample:
                    failure_sample = f"{type(exc).__name__}: {exc}"[:200]
                continue
            if checkpoint_tuple is None:
                continue
            checkpoint = checkpoint_tuple.checkpoint
            if not isinstance(checkpoint, dict):
                # Decodable-but-wrong-type blobs (e.g. a single msgpack int)
                # are the second damage shape seen in old databases.
                threads_failed += 1
                if not failure_sample:
                    failure_sample = (
                        f"non-dict checkpoint: {type(checkpoint).__name__}"
                    )
                continue
            channel_values = checkpoint.get("channel_values") or {}

            budget = channel_values.get("context_budget")
            if isinstance(budget, dict) and budget:
                threads_with_budget += 1
                tokens_used.append(float(budget.get("tokens_used") or 0))
                items_injected.append(float(budget.get("items_injected") or 0))
                dyn_budget = float(budget.get("dynamic_token_budget") or 0)
                dyn_used = float(budget.get("dynamic_tokens_used") or 0)
                dynamic_budget_used.append(dyn_used)
                if dyn_budget > 0:
                    dynamic_utilization.append(round(dyn_used / dyn_budget, 4))

            accounting = channel_values.get("context_accounting")
            if isinstance(accounting, dict) and accounting:
                threads_with_accounting += 1
                pool_remaining.append(float(accounting.get("remaining_tokens") or 0))
                jit_body_tokens.append(float(accounting.get("jit_body_tokens") or 0))
                navigator_tokens.append(
                    float(accounting.get("navigator_tokens") or 0)
                )
                registry = accounting.get("registry") or {}
                if isinstance(registry, dict):
                    object_cache_hits.append(
                        float(registry.get("object_cache_hits") or 0)
                    )
                    objects_read.append(float(registry.get("objects_read") or 0))
                    selected_segments.append(
                        float(registry.get("selected_segments") or 0)
                    )
                    dropped_by_topk.append(
                        float(registry.get("dropped_by_topk") or 0)
                    )
                    state_object_refs.append(
                        float(registry.get("state_object_refs") or 0)
                    )

        return ContextAccountingReport(
            db_path=str(path),
            threads_total=threads_total,
            threads_with_budget=threads_with_budget,
            threads_with_accounting=threads_with_accounting,
            threads_failed=threads_failed,
            failure_sample=failure_sample,
            budget={
                "tokens_used": _distribution(tokens_used),
                "dynamic_tokens_used": _distribution(dynamic_budget_used),
                "dynamic_utilization": _distribution(dynamic_utilization),
                "items_injected": _distribution(items_injected),
            },
            accounting={
                "pool_remaining_tokens": _distribution(pool_remaining),
                "jit_body_tokens": _distribution(jit_body_tokens),
                "navigator_tokens": _distribution(navigator_tokens),
                "object_cache_hits": _distribution(object_cache_hits),
                "objects_read": _distribution(objects_read),
                "selected_segments": _distribution(selected_segments),
                "dropped_by_topk": _distribution(dropped_by_topk),
                "state_object_refs": _distribution(state_object_refs),
            },
        )
    finally:
        conn.close()
