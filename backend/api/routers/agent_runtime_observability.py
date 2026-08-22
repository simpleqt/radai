"""Agent 运行时观测路由:run 列表、trace、事件/单元/工具调用/上下文包/压实/交接与回放(由主 router include)。"""

from __future__ import annotations

from fastapi import APIRouter

from backend.config.loggers import get_logger

import asyncio

from backend.api.dependencies.auth import ApiKeyAuth, require_admin, verify_api_key
from backend.application.agent_trace import (
    AgentRunReplay,
    AgentTraceStoreProtocol,
    create_agent_trace_store,
)
from backend.application.agent_trace.models import AgentRunRecord
from fastapi import Depends, HTTPException, Query
from typing import Any

# 历史别名:AgentTraceStore 一直是工厂函数的模块级别名(测试可 monkeypatch)
AgentTraceStore = create_agent_trace_store

logger = get_logger(__name__)
router = APIRouter()


async def _read_trace_records_for_auth(
    run_id: str,
    *,
    auth: ApiKeyAuth,
    trace_root: str | None = None,
) -> tuple[AgentTraceStoreProtocol, list[AgentRunRecord]]:
    store = AgentTraceStore(trace_root)
    try:
        records = await asyncio.to_thread(store.read_records, run_id)
    except FileNotFoundError as exc:
        raise HTTPException(status_code=404, detail="agent run trace 不存在") from exc
    except ValueError as exc:
        raise HTTPException(status_code=422, detail=str(exc)) from exc
    run_payload = next(
        (record.payload for record in records if record.kind == "run"),
        {},
    )
    owner_id = str(run_payload.get("user_id") or "")
    if not owner_id and (auth.role or "").lower() != "admin":
        raise HTTPException(status_code=404, detail="agent run trace 不存在")
    if owner_id and owner_id != auth.user_id and (auth.role or "").lower() != "admin":
        raise HTTPException(status_code=404, detail="agent run trace 不存在")
    return store, records


def _payloads_for_kind(records: list[AgentRunRecord], kind: str) -> list[dict[str, Any]]:
    return [dict(record.payload) for record in records if record.kind == kind]


def _trace_counts(records: list[AgentRunRecord]) -> dict[str, int]:
    """Return stable counters used by both detail and admin inventory views."""
    return {
        "events": sum(1 for record in records if record.kind == "event"),
        "units": sum(1 for record in records if record.kind == "unit"),
        "tool_calls": sum(1 for record in records if record.kind == "tool_call"),
        "context_packs": sum(
            1 for record in records if record.kind == "context_pack"
        ),
        "compactions": sum(
            1
            for record in records
            if record.kind == "compaction"
            and str(record.payload.get("status") or "completed") == "completed"
        ),
        "compaction_attempts": sum(
            1 for record in records if record.kind == "compaction"
        ),
        "compaction_skipped": sum(
            1
            for record in records
            if record.kind == "compaction"
            and str(record.payload.get("status") or "completed") == "skipped"
        ),
        "handoffs": sum(1 for record in records if record.kind == "handoff"),
    }


def _trace_summary(run_id: str, records: list[AgentRunRecord]) -> dict[str, Any] | None:
    run_payloads = _payloads_for_kind(records, "run")
    if not run_payloads:
        return None
    summary = dict(run_payloads[-1])
    summary["run_id"] = run_id
    summary["record_count"] = len(records)
    summary["counts"] = _trace_counts(records)
    return summary


@router.get("/admin/runs")
async def list_admin_agent_runs(
    limit: int = Query(default=50, ge=1, le=200),
    offset: int = Query(default=0, ge=0),
    status: str | None = Query(default=None, max_length=32),
    agent_code: str | None = Query(default=None, max_length=64),
    user_id: str | None = Query(default=None, max_length=128),
    q: str | None = Query(default=None, max_length=256),
    auth: ApiKeyAuth = Depends(require_admin),
):
    """List run summaries for the Admin runtime console.

    Trace stores intentionally expose only a minimal ``list_run_ids`` contract.
    The inventory therefore reads one compact run record per id and never
    returns event/tool payloads.  Detailed records remain behind the existing
    per-run authorization checks.
    """
    del auth  # The dependency performs the authorization check.
    try:
        store = AgentTraceStore()
        run_ids = await asyncio.to_thread(store.list_run_ids)
    except Exception as exc:  # noqa: BLE001
        logger.exception("Failed to list agent trace runs")
        raise HTTPException(status_code=503, detail="运行记录暂时不可用") from exc

    normalized_status = str(status or "").strip().lower()
    normalized_agent = str(agent_code or "").strip().lower()
    normalized_user = str(user_id or "").strip().lower()
    normalized_query = str(q or "").strip().lower()
    ordered_run_ids = list(reversed(run_ids))

    # The common unfiltered view can page by IDs without opening every trace
    # file/blob.  This keeps the Admin page cheap even when the installation
    # has accumulated a large history; filters intentionally trade some I/O
    # for exact matching.
    if not any((normalized_status, normalized_agent, normalized_user, normalized_query)):
        items: list[dict[str, Any]] = []
        for run_id in ordered_run_ids[offset:offset + limit]:
            try:
                records = await asyncio.to_thread(store.read_records, run_id)
            except (FileNotFoundError, ValueError):
                continue
            summary = _trace_summary(run_id, records)
            if summary is not None:
                items.append(summary)
        return {
            "items": items,
            "total": len(run_ids),
            "limit": limit,
            "offset": offset,
        }

    filtered_items: list[dict[str, Any]] = []
    for run_id in ordered_run_ids:
        try:
            records = await asyncio.to_thread(store.read_records, run_id)
        except (FileNotFoundError, ValueError):
            # A concurrently rotating/deleting trace should not make the
            # entire Admin page fail.
            continue
        summary = _trace_summary(run_id, records)
        if summary is None:
            continue
        if normalized_status and str(summary.get("status") or "").lower() != normalized_status:
            continue
        if normalized_agent and str(summary.get("agent_code") or "").lower() != normalized_agent:
            continue
        if normalized_user and str(summary.get("user_id") or "").lower() != normalized_user:
            continue
        if normalized_query:
            searchable = " ".join(
                str(summary.get(field) or "")
                for field in ("run_id", "request_id", "thread_id", "user_id", "agent_code", "workflow", "model_name")
            ).lower()
            if normalized_query not in searchable:
                continue
        filtered_items.append(summary)

    total = len(filtered_items)
    return {
        "items": filtered_items[offset:offset + limit],
        "total": total,
        "limit": limit,
        "offset": offset,
    }


@router.get("/runs/{run_id}")
async def get_agent_run_trace(
    run_id: str,
    auth: ApiKeyAuth = Depends(verify_api_key),
):
    _, records = await _read_trace_records_for_auth(
        run_id,
        auth=auth,
    )
    run_payloads = _payloads_for_kind(records, "run")
    return {
        "run_id": run_id,
        "run": run_payloads[-1] if run_payloads else None,
        "record_count": len(records),
        "counts": _trace_counts(records),
    }


@router.post("/runs/{run_id}/cancel")
async def cancel_live_agent_run(
    run_id: str,
    auth: ApiKeyAuth = Depends(verify_api_key),
):
    """Cooperatively cancel a live request tree in this API worker."""

    from backend.core.agents.runtime_manager import get_live_agent_runtime_manager

    manager = get_live_agent_runtime_manager(run_id)
    if manager is None:
        raise HTTPException(
            status_code=409,
            detail="agent run is not active in this API worker",
        )
    metadata = dict(getattr(manager, "run_metadata", {}) or {})
    owner_id = str(metadata.get("user_id") or "")
    if (
        (auth.role or "").lower() != "admin"
        and (not owner_id or owner_id != auth.user_id)
    ):
        raise HTTPException(status_code=404, detail="agent run 不存在")
    try:
        await manager.interrupt(
            manager.root_agent_id,
            reason="cancelled by user",
        )
    except (asyncio.CancelledError, GeneratorExit):
        raise
    except Exception as exc:  # noqa: BLE001
        logger.exception("Failed to cancel live agent run %s", run_id)
        raise HTTPException(status_code=503, detail="agent run 暂时无法取消") from exc
    return {
        "run_id": run_id,
        "status": "cancelling",
        "scope": "current_worker",
    }


@router.get("/runs/{run_id}/events")
async def list_agent_run_events(
    run_id: str,
    auth: ApiKeyAuth = Depends(verify_api_key),
):
    _, records = await _read_trace_records_for_auth(
        run_id,
        auth=auth,
    )
    return {"run_id": run_id, "events": _payloads_for_kind(records, "event")}


@router.get("/runs/{run_id}/units")
async def list_agent_run_units(
    run_id: str,
    auth: ApiKeyAuth = Depends(verify_api_key),
):
    _, records = await _read_trace_records_for_auth(
        run_id,
        auth=auth,
    )
    return {"run_id": run_id, "units": _payloads_for_kind(records, "unit")}


@router.get("/runs/{run_id}/tool-calls")
async def list_agent_run_tool_calls(
    run_id: str,
    auth: ApiKeyAuth = Depends(verify_api_key),
):
    _, records = await _read_trace_records_for_auth(
        run_id,
        auth=auth,
    )
    return {"run_id": run_id, "tool_calls": _payloads_for_kind(records, "tool_call")}


@router.get("/runs/{run_id}/context-packs")
async def list_agent_run_context_packs(
    run_id: str,
    auth: ApiKeyAuth = Depends(verify_api_key),
):
    _, records = await _read_trace_records_for_auth(
        run_id,
        auth=auth,
    )
    return {
        "run_id": run_id,
        "context_packs": _payloads_for_kind(records, "context_pack"),
    }


@router.get("/runs/{run_id}/compactions")
async def list_agent_run_compactions(
    run_id: str,
    auth: ApiKeyAuth = Depends(verify_api_key),
):
    _, records = await _read_trace_records_for_auth(
        run_id,
        auth=auth,
    )
    return {
        "run_id": run_id,
        "compactions": _payloads_for_kind(records, "compaction"),
    }


@router.get("/runs/{run_id}/handoffs")
async def list_agent_run_handoffs(
    run_id: str,
    auth: ApiKeyAuth = Depends(verify_api_key),
):
    _, records = await _read_trace_records_for_auth(
        run_id,
        auth=auth,
    )
    return {"run_id": run_id, "handoffs": _payloads_for_kind(records, "handoff")}


@router.post("/runs/{run_id}/replay")
async def replay_agent_run_trace(
    run_id: str,
    mode: str = Query(default="dry-run", pattern="^(dry-run|tool-cache|full)$"),
    from_unit: str | None = None,
    auth: ApiKeyAuth = Depends(verify_api_key),
):
    store, _ = await _read_trace_records_for_auth(
        run_id,
        auth=auth,
    )
    try:
        summary = await asyncio.to_thread(
            AgentRunReplay(store).replay,
            run_id,
            mode=mode,
            from_unit=from_unit,
        )
    except ValueError as exc:
        raise HTTPException(status_code=422, detail=str(exc)) from exc
    return summary.to_json_dict()
