"""Agent state definitions for LangGraph-based agent graphs.

Project-local thread state schema (pure data types, no I/O).
"""

from __future__ import annotations

import operator
import os
from typing import Annotated

from langchain.agents import AgentState
from typing_extensions import NotRequired, TypedDict

from backend.core.agents.context.types import (
    ContextBudgetState,
    ContextItem,
    ContextPack,
    EvidenceRecord,
)
from backend.core.agents.context.objects import ContextObject, ProjectionReceipt


class ThreadDataState(TypedDict):
    thread_id: NotRequired[str | None]
    internal_thread_id: NotRequired[str | None]
    workspace_path: NotRequired[str | None]
    uploads_path: NotRequired[str | None]
    outputs_path: NotRequired[str | None]


class RuntimeTurnState(TypedDict):
    """Durable semantic model-turn count for one graph thread."""

    runtime_model_turns: NotRequired[Annotated[int, operator.add]]


def merge_artifacts(existing: list[str] | None, new: list[str] | None) -> list[str]:
    if existing is None:
        return new or []
    if new is None:
        return existing
    return list(dict.fromkeys(existing + new))


def merge_loaded_skills(
    existing: dict[str, str] | None,
    new: dict[str, str] | None,
) -> dict[str, str]:
    if existing is None:
        return {
            str(name): _bound_state_text(content, max_chars=12000)
            for name, content in (new or {}).items()
        }
    if new is None:
        return {
            str(name): _bound_state_text(content, max_chars=12000)
            for name, content in existing.items()
        }
    merged = {**existing, **new}
    # Skill files are durable instructions, not an unbounded checkpoint cache.
    # Large files are persisted by SkillCaptureHandler and represented in state
    # by a bounded summary; keep a defensive cap for legacy callers that still
    # write the raw content directly.
    return {
        str(name): _bound_state_text(content, max_chars=12000)
        for name, content in merged.items()
    }


def merge_retrieved_evidence(
    existing: list[str] | None,
    new: list[str] | None,
) -> list[str]:
    values = [
        _bound_state_text(item, max_chars=12000)
        for item in [*(existing or []), *(new or [])]
    ]
    # Legacy evidence fields remain accepted during migration, but they are
    # not allowed to become an unbounded checkpoint transcript.
    return list(dict.fromkeys(values))[-80:]


def merge_project_skill(existing: str | None, new: str | None) -> str | None:
    """Keep the latest project skill while bounding durable state size."""

    if new is None:
        new = existing
    if new is None:
        return None
    return _bound_state_text(new, max_chars=12000)


def merge_context_items(
    existing: list[ContextItem] | None,
    new: list[ContextItem] | None,
) -> list[ContextItem]:
    """Append context records, deduplicate them, and bound checkpoint growth."""

    merged = [_bound_context_item(item) for item in [*(existing or []), *(new or [])]]
    by_id: dict[str, ContextItem] = {}
    for item in merged:
        key = str(item.get("id") or "")
        # Assignment does not refresh an existing dict key's insertion order.
        by_id.pop(key, None)
        by_id[key] = item
    return list(by_id.values())[-80:]


def merge_context_records(
    existing: list[EvidenceRecord] | None,
    new: list[EvidenceRecord] | None,
) -> list[EvidenceRecord]:
    """Append structured evidence records, deduplicate them, and bound growth."""

    merged = [_bound_context_record(record) for record in [*(existing or []), *(new or [])]]
    by_key: dict[str, EvidenceRecord] = {}
    for record in merged:
        key = str(
            record.get("id")
            or ":".join(
                [
                    str(record.get("source_tool") or ""),
                    str(record.get("source_id") or ""),
                    str(record.get("chunk_id") or ""),
                    str(record.get("claim") or "")[:120],
                ]
            )
        )
        by_key.pop(key, None)
        by_key[key] = record
    return list(by_key.values())[-160:]


def _bound_state_text(value: object, *, max_chars: int) -> str:
    text = str(value or "")
    if len(text) <= max_chars:
        return text
    head = max(1, max_chars // 2)
    tail = max(1, max_chars - head - 32)
    return f"{text[:head]}\n...[checkpoint truncated]...\n{text[-tail:]}"


def _bound_context_item(item: ContextItem) -> ContextItem:
    bounded = dict(item)
    for key, limit in (
        ("summary", 2400),
        ("prompt_content", 6000),
    ):
        if key in bounded:
            bounded[key] = _bound_state_text(bounded.get(key), max_chars=limit)
    bounded["keywords"] = [str(value)[:160] for value in list(bounded.get("keywords") or [])[:32]]
    bounded["tags"] = [str(value)[:160] for value in list(bounded.get("tags") or [])[:32]]
    return bounded  # type: ignore[return-value]


def _bound_context_record(record: EvidenceRecord) -> EvidenceRecord:
    bounded = dict(record)
    bounded["claim"] = _bound_state_text(bounded.get("claim"), max_chars=2400)
    bounded["content"] = _bound_state_text(bounded.get("content"), max_chars=6000)
    bounded["doc_title"] = _bound_state_text(bounded.get("doc_title"), max_chars=500)
    bounded["tags"] = [str(value)[:160] for value in list(bounded.get("tags") or [])[:32]]
    return bounded  # type: ignore[return-value]


def merge_compaction_history(
    existing: list[dict] | None,
    new: list[dict] | None,
) -> list[dict]:
    merged = [*(existing or []), *(new or [])]
    by_window: dict[str, dict] = {}
    anonymous: list[dict] = []
    for entry in merged:
        window_id = str(entry.get("window_id") or "").strip()
        if window_id:
            by_window[window_id] = entry
        else:
            anonymous.append(entry)
    return [*anonymous, *by_window.values()][-20:]


def context_object_ref_cap() -> int:
    """Read the bounded state-reference cap (default keeps current behavior)."""

    try:
        return max(10, min(int(os.getenv("CONTEXT_OBJECT_REF_CAP", "200")), 1000))
    except (TypeError, ValueError):
        return 200


def converge_context_object_refs(
    refs: list[ContextObject] | None,
    *,
    cap: int | None = None,
) -> list[ContextObject]:
    """Deduplicate and converge a context-object ref list under the cap.

    Used by both the state reducer (on write) and the JIT reader (on load), so
    oversized legacy checkpoints converge with the same ``last_seen_at``
    eviction rule instead of a raw tail slice.
    """

    limit = int(cap) if cap is not None else context_object_ref_cap()
    by_oid: dict[str, ContextObject] = {}
    for obj in refs or []:
        if not isinstance(obj, dict):
            continue
        oid = str(obj.get("oid") or "")
        if not oid:
            continue
        by_oid.pop(oid, None)
        by_oid[oid] = obj
    ordered = sorted(
        by_oid.values(),
        key=lambda obj: (
            float((obj.get("origin") or {}).get("last_seen_at") or 0.0),
            str((obj.get("origin") or {}).get("turn_id") or ""),
            str(obj.get("oid") or ""),
        ),
    )
    return ordered[-max(1, limit):]


def merge_context_objects(
    existing: list[ContextObject] | None,
    new: list[ContextObject] | None,
) -> list[ContextObject]:
    """Keep small object refs by content-addressed oid, evicting oldest.

    The cap is configurable through ``CONTEXT_OBJECT_REF_CAP`` (default 200).
    Eviction order prefers ``origin.last_seen_at`` (capture time written by
    segmenters) and falls back to write order for refs without it, so a
    deterministic LRU ordering replaces pure insertion-order truncation.
    """

    return converge_context_object_refs([*(existing or []), *(new or [])])


def merge_projection_receipts(
    existing: list[ProjectionReceipt] | None,
    new: list[ProjectionReceipt] | None,
) -> list[ProjectionReceipt]:
    merged = [*(existing or []), *(new or [])]
    by_key: dict[str, ProjectionReceipt] = {}
    for receipt in merged:
        key = ":".join(
            [
                str(receipt.get("oid") or ""),
                str(receipt.get("handle") or ""),
                str(receipt.get("turn_id") or ""),
            ]
        )
        # Assignment does not refresh an existing dict key's insertion order;
        # without the pop a frequently refreshed receipt kept its old slot and
        # could be evicted by the tail cap despite being the most recent.
        by_key.pop(key, None)
        by_key[key] = receipt
    return list(by_key.values())[-200:]


class ThreadState(AgentState, RuntimeTurnState):
    thread_data: NotRequired[ThreadDataState | None]
    title: NotRequired[str | None]
    artifacts: Annotated[list[str], merge_artifacts]
    todos: NotRequired[list | None]
    uploaded_files: NotRequired[list[dict] | None]
    research_workflow: NotRequired[dict | None]
    clarification: NotRequired[dict | None]
    loaded_skills: Annotated[dict[str, str], merge_loaded_skills]
    retrieved_evidence: Annotated[list[str], merge_retrieved_evidence]
    project_evidence: Annotated[list[str], merge_retrieved_evidence]
    project_bootstrap: NotRequired[dict | None]
    project_skill: Annotated[str | None, merge_project_skill]
    context_items: Annotated[list[ContextItem], merge_context_items]
    context_records: Annotated[list[EvidenceRecord], merge_context_records]
    context_objects: Annotated[list[ContextObject], merge_context_objects]
    projection_receipts: Annotated[
        list[ProjectionReceipt],
        merge_projection_receipts,
    ]
    context_accounting: NotRequired[dict | None]
    context_pack: NotRequired[ContextPack | None]
    context_budget: NotRequired[ContextBudgetState | None]
    context_pack_trace_index: NotRequired[int]
    context_window_id: NotRequired[str | None]
    context_compaction: NotRequired[dict | None]
    context_checkpoint_ref: NotRequired[dict | None]
    context_compaction_history: Annotated[list[dict], merge_compaction_history]
    trace_context: NotRequired[dict | None]
