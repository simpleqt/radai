"""Agent state definitions for LangGraph-based agent graphs.

Project-local thread state schema (pure data types, no I/O).
"""

from __future__ import annotations

import operator
from typing import Annotated

from langchain.agents import AgentState
from typing_extensions import NotRequired, TypedDict

from backend.core.agents.context.types import (
    ContextBudgetState,
    ContextItem,
    ContextPack,
    EvidenceRecord,
)


class ThreadDataState(TypedDict):
    thread_id: NotRequired[str | None]
    internal_thread_id: NotRequired[str | None]
    workspace_path: NotRequired[str | None]
    uploads_path: NotRequired[str | None]
    outputs_path: NotRequired[str | None]


class RuntimeTurnState(TypedDict):
    """Durable semantic model-turn count for one graph thread."""

    runtime_model_turns: NotRequired[Annotated[int, operator.add]]


class ViewedImageData(TypedDict):
    base64: str
    mime_type: str


MAX_VIEWED_IMAGES = 20
MAX_VIEWED_IMAGE_BASE64_CHARS = 2_000_000


def _bounded_viewed_images(
    images: dict[str, ViewedImageData] | None,
) -> dict[str, ViewedImageData]:
    bounded: dict[str, ViewedImageData] = {}
    for image_id, image in (images or {}).items():
        if not isinstance(image, dict):
            continue
        encoded = image.get("base64")
        if not isinstance(encoded, str) or len(encoded) > MAX_VIEWED_IMAGE_BASE64_CHARS:
            continue
        key = str(image_id)
        bounded.pop(key, None)
        bounded[key] = image
    return dict(list(bounded.items())[-MAX_VIEWED_IMAGES:])


def merge_artifacts(existing: list[str] | None, new: list[str] | None) -> list[str]:
    if existing is None:
        return new or []
    if new is None:
        return existing
    return list(dict.fromkeys(existing + new))


def merge_viewed_images(
    existing: dict[str, ViewedImageData] | None,
    new: dict[str, ViewedImageData] | None,
) -> dict[str, ViewedImageData]:
    if existing is None:
        return _bounded_viewed_images(new)
    if new is None:
        return _bounded_viewed_images(existing)
    if len(new) == 0:
        return {}
    merged = _bounded_viewed_images(existing)
    for image_id, image in _bounded_viewed_images(new).items():
        merged.pop(image_id, None)
        merged[image_id] = image
    return dict(list(merged.items())[-MAX_VIEWED_IMAGES:])


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


class ThreadState(AgentState, RuntimeTurnState):
    thread_data: NotRequired[ThreadDataState | None]
    title: NotRequired[str | None]
    artifacts: Annotated[list[str], merge_artifacts]
    todos: NotRequired[list | None]
    uploaded_files: NotRequired[list[dict] | None]
    viewed_images: Annotated[dict[str, ViewedImageData], merge_viewed_images]
    research_workflow: NotRequired[dict | None]
    clarification: NotRequired[dict | None]
    loaded_skills: Annotated[dict[str, str], merge_loaded_skills]
    retrieved_evidence: Annotated[list[str], merge_retrieved_evidence]
    project_evidence: Annotated[list[str], merge_retrieved_evidence]
    project_bootstrap: NotRequired[dict | None]
    project_skill: Annotated[str | None, merge_project_skill]
    context_items: Annotated[list[ContextItem], merge_context_items]
    context_records: Annotated[list[EvidenceRecord], merge_context_records]
    context_pack: NotRequired[ContextPack | None]
    context_budget: NotRequired[ContextBudgetState | None]
    context_pack_trace_index: NotRequired[int]
    context_window_id: NotRequired[str | None]
    context_compaction: NotRequired[dict | None]
    context_checkpoint_ref: NotRequired[dict | None]
    context_compaction_history: Annotated[list[dict], merge_compaction_history]
    trace_context: NotRequired[dict | None]
