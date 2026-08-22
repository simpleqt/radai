"""流式工具证据与事件字段提取(拆自 stream_handler.py,纯移动)。"""

from __future__ import annotations

import json

from backend.application.agent_executors.stream_utils import stringify_message_content
from backend.config.loggers import get_logger
from backend.core.agents.context.ledger import (
    make_evidence_record,
    parse_multikb_text_evidence,
)
from typing import Any


logger = get_logger(__name__)


# 可见正文低于该阈值且执行过工具时，会话结束打 WARNING 便于排查「几乎无输出」
_LOW_VISIBLE_OUTPUT_WARN_CHARS = 300


_GROUNDWATER_PROJECT_EVIDENCE_TOOLS = {
    "groundwater_project_skill_read",
    "query_project_tables",
    "groundwater_prediction",
    "groundwater_quality_check",
}


_COORDINATED_EVIDENCE_TOOLS = {
    "multi_kb_search",
    "academic_search",
    "chat_file_search",
    "list_experiment_datasets",
    "inspect_excel_data",
    "query_excel_data",
    "summarize_excel_data",
    "inspect_project_tables",
    "query_project_tables",
    "summarize_project_table",
    "groundwater_prediction",
    "groundwater_quality_check",
    "groundwater_kml_summary",
}


_TOOL_OUTPUT_CONTENT_KEYS = (
    "text",
    "content",
    "output",
    "result",
    "results",
    "answer",
    "summary",
    "data",
    "rows",
    "records",
    "tables",
    "datasets",
    "schema",
    "statistics",
)


def _coordinated_tool_output_content(raw_output: Any) -> str:
    """Read result-bearing fields without treating tool inputs as evidence."""

    visited: set[int] = set()

    def extract(candidate: Any, *, allow_collection: bool = False) -> str:
        if candidate is None:
            return ""
        candidate_id = id(candidate)
        if candidate_id in visited:
            return ""
        visited.add(candidate_id)
        if isinstance(candidate, str):
            text = candidate.strip()
            if not text:
                return ""
            if text.startswith(("{", "[")):
                try:
                    decoded = json.loads(text)
                except (TypeError, json.JSONDecodeError):
                    return text
                decoded_content = extract(decoded, allow_collection=True)
                return decoded_content
            return text
        if isinstance(candidate, dict):
            for key in _TOOL_OUTPUT_CONTENT_KEYS:
                if key not in candidate:
                    continue
                value = candidate.get(key)
                content = extract(value, allow_collection=True)
                if content:
                    return content
            return ""
        if isinstance(candidate, (list, tuple, set)):
            if not allow_collection:
                return ""
            try:
                return json.dumps(list(candidate), ensure_ascii=False, default=str)
            except (TypeError, ValueError):
                return stringify_message_content(list(candidate)).strip()
        candidate_content = getattr(candidate, "content", None)
        content = extract(candidate_content, allow_collection=True)
        if content:
            return content
        return extract(getattr(candidate, "artifact", None), allow_collection=True)

    return extract(raw_output, allow_collection=True)


def _coordinated_tool_evidence_records(
    tool_name: str | None,
    raw_output: Any,
    *,
    tool_run_id: str,
) -> list[dict[str, Any]]:
    """Extract bounded, internal-only evidence from one successful tool result."""

    normalized_tool = str(tool_name or "").strip()
    if normalized_tool not in _COORDINATED_EVIDENCE_TOOLS:
        return []

    queue: list[Any] = [raw_output]
    visited: set[int] = set()
    structured: list[dict[str, Any]] = []
    while queue and len(visited) < 64:
        candidate = queue.pop(0)
        if candidate is None:
            continue
        candidate_id = id(candidate)
        if candidate_id in visited:
            continue
        visited.add(candidate_id)
        if isinstance(candidate, dict):
            for key in ("evidence_records", "evidence"):
                records = candidate.get(key)
                if isinstance(records, list):
                    structured.extend(
                        dict(record) for record in records if isinstance(record, dict)
                    )
            queue.extend(candidate.values())
        elif isinstance(candidate, (list, tuple, set)):
            queue.extend(candidate)
        elif isinstance(candidate, str):
            text = candidate.strip()
            if text.startswith(("{", "[")):
                try:
                    queue.append(json.loads(text))
                except (TypeError, json.JSONDecodeError):
                    pass
        else:
            artifact = getattr(candidate, "artifact", None)
            if artifact is not None:
                queue.append(artifact)
            candidate_content = getattr(candidate, "content", None)
            if candidate_content is not None:
                queue.append(candidate_content)

    records: list[dict[str, Any]] = []
    for index, record in enumerate(structured[:12], start=1):
        record_content = str(record.get("content") or record.get("claim") or "").strip()
        if not record_content:
            continue
        records.append(
            make_evidence_record(
                source_tool=str(record.get("source_tool") or normalized_tool),
                content=record_content,
                category=(
                    "retrieval"
                    if str(record.get("category") or "") == "retrieval"
                    or normalized_tool
                    in {"multi_kb_search", "academic_search", "chat_file_search"}
                    else "tool_result"
                ),
                source_id=str(record.get("source_id") or normalized_tool),
                chunk_id=str(record.get("chunk_id") or tool_run_id or index),
                doc_title=str(
                    record.get("doc_title")
                    or record.get("source_id")
                    or normalized_tool
                ),
                score=(
                    record.get("score")
                    if isinstance(record.get("score"), (int, float))
                    else None
                ),
                kind=str(record.get("kind") or "") or None,
                raw_ref=str(record.get("raw_ref") or "") or None,
                tags=[str(tag) for tag in record.get("tags") or []][:10],
            )
        )
    if records:
        return records

    content = _coordinated_tool_output_content(raw_output)
    if not content:
        return []
    if normalized_tool == "multi_kb_search":
        parsed_records = parse_multikb_text_evidence(content)
        if parsed_records:
            return [dict(record) for record in parsed_records[:12]]

    if normalized_tool == "academic_search":
        sections = [section.strip() for section in content.split("\n### ") if section.strip()]
        paper_sections = sections[1:] if len(sections) > 1 else sections
        for index, section in enumerate(paper_sections[:8], start=1):
            lines = section.splitlines()
            title = lines[0].strip("# ") if lines else f"academic result {index}"
            records.append(
                make_evidence_record(
                    source_tool=normalized_tool,
                    content=section,
                    category="retrieval",
                    source_id=title,
                    chunk_id=f"{tool_run_id or 'result'}-{index}",
                    doc_title=title,
                    kind="retrieval",
                    tags=["evidence", "academic"],
                )
            )
        if records:
            return records

    return [
        make_evidence_record(
            source_tool=normalized_tool,
            content=content,
            category=("retrieval" if normalized_tool == "chat_file_search" else "tool_result"),
            source_id=normalized_tool,
            chunk_id=tool_run_id or "result",
            doc_title=f"{normalized_tool} result",
            tags=["evidence", "coordinated_tool_result"],
        )
    ]


def _extract_context_compaction(event: dict[str, Any]) -> dict[str, Any] | None:
    """Find middleware compaction metadata without traversing message bodies."""

    queue: list[Any] = [event.get("data"), event.get("output")]
    visited: set[int] = set()
    while queue and len(visited) < 32:
        candidate = queue.pop(0)
        if not isinstance(candidate, dict):
            continue
        candidate_id = id(candidate)
        if candidate_id in visited:
            continue
        visited.add(candidate_id)
        metadata = candidate.get("context_compaction")
        if isinstance(metadata, dict) and metadata.get("window_id"):
            return dict(metadata)
        for key, value in candidate.items():
            if key in {"messages", "context_records", "context_items", "context_pack"}:
                continue
            if isinstance(value, dict):
                queue.append(value)
    return None


def _display_compaction_summary(value: object, *, max_chars: int = 4000) -> str:
    text = str(value or "").strip()
    if text.startswith("Another language model started to solve this problem"):
        _, separator, remainder = text.partition("\n")
        if separator:
            text = remainder.strip()
    if len(text) <= max_chars:
        return text
    return text[:max_chars].rstrip() + "\n...[truncated]"


def _counts_toward_project_query_budget(raw_output: Any) -> bool:
    """Count only successful data queries, not rejected SQL or schema lookups."""
    candidate = raw_output
    if hasattr(candidate, "content"):
        candidate = getattr(candidate, "content")
    elif isinstance(candidate, dict):
        if "counts_toward_query_budget" in candidate:
            return candidate.get("counts_toward_query_budget") is not False
        candidate = candidate.get("output") or candidate.get("content") or candidate.get("result")
    try:
        payload = json.loads(str(candidate or ""))
    except (TypeError, json.JSONDecodeError):
        return True
    if not isinstance(payload, dict):
        return True
    return payload.get("counts_toward_query_budget") is not False


def _extract_ai_message_from_output(output: Any) -> Any | None:
    """Best-effort extract AIMessage from on_chat_model_end payload."""
    if output is None:
        return None
    if hasattr(output, "tool_calls") or getattr(output, "type", None) == "ai":
        return output
    if isinstance(output, dict):
        for key in ("message", "output", "result"):
            if key in output:
                msg = _extract_ai_message_from_output(output.get(key))
                if msg is not None:
                    return msg
        generations = output.get("generations")
        if generations:
            return _extract_ai_message_from_output(generations)
        return None
    if isinstance(output, list):
        for item in output:
            msg = _extract_ai_message_from_output(item)
            if msg is not None:
                return msg
        return None
    if hasattr(output, "message"):
        return _extract_ai_message_from_output(getattr(output, "message"))
    if hasattr(output, "generations"):
        return _extract_ai_message_from_output(getattr(output, "generations"))
    return None


def _extract_finish_reason(output: Any) -> str | None:
    if output is None:
        return None
    if isinstance(output, dict):
        for key in ("finish_reason", "stop_reason"):
            value = output.get(key)
            if isinstance(value, str) and value.strip():
                return value.strip()
        for key in ("message", "output", "result", "generations"):
            if key in output:
                reason = _extract_finish_reason(output.get(key))
                if reason:
                    return reason
        return None
    if isinstance(output, list):
        for item in output:
            reason = _extract_finish_reason(item)
            if reason:
                return reason
        return None
    generation_info = getattr(output, "generation_info", None)
    if isinstance(generation_info, dict):
        for key in ("finish_reason", "stop_reason"):
            value = generation_info.get(key)
            if isinstance(value, str) and value.strip():
                return value.strip()
    response_metadata = getattr(output, "response_metadata", None)
    if isinstance(response_metadata, dict):
        value = response_metadata.get("finish_reason") or response_metadata.get("stop_reason")
        if isinstance(value, str) and value.strip():
            return value.strip()
    if hasattr(output, "message"):
        return _extract_finish_reason(getattr(output, "message"))
    if hasattr(output, "generations"):
        return _extract_finish_reason(getattr(output, "generations"))
    return None


def _extract_tool_call_names(output: Any) -> list[str]:
    msg = _extract_ai_message_from_output(output)
    if msg is None:
        return []
    tool_calls = getattr(msg, "tool_calls", None) or []
    if not tool_calls:
        additional_kwargs = getattr(msg, "additional_kwargs", None)
        if isinstance(additional_kwargs, dict):
            tool_calls = additional_kwargs.get("tool_calls") or additional_kwargs.get("invalid_tool_calls") or []
    names: list[str] = []
    for tc in tool_calls:
        if isinstance(tc, dict):
            function = tc.get("function")
            if isinstance(function, dict):
                name = str(function.get("name") or tc.get("name") or "").strip()
            else:
                name = str(tc.get("name") or "").strip()
        else:
            name = str(getattr(tc, "name", None) or "").strip()
        if name:
            names.append(name)
    return names


def _is_messages_mode_stream_event(event: dict[str, Any]) -> bool:
    """Whether an ``on_chain_stream`` event carries a ``messages`` token payload.

    In ``stream_mode=["messages", ...]`` every model token arrives as a root
    ``on_chain_stream`` whose ``data.chunk`` is a ``("messages", (msg, meta))``
    (or bare ``(msg, meta)``) tuple. These are per-token and must never trigger
    a checkpoint ``aget_state`` read (artifacts only change on node completion),
    otherwise long conversations turn the shared single-connection sqlite
    checkpointer into a deterministic per-token hot spot.
    """

    if event.get("event") != "on_chain_stream":
        return False
    data = event.get("data") or {}
    if not isinstance(data, dict):
        return False
    candidate = data.get("chunk")
    if (
        isinstance(candidate, tuple)
        and len(candidate) == 2
        and candidate[0] == "messages"
    ):
        candidate = candidate[1]
    if not isinstance(candidate, tuple) or len(candidate) != 2:
        return False
    _message, metadata = candidate
    return isinstance(metadata, dict)


def _extract_graph_message_stream(event: dict[str, Any]) -> Any | None:
    """Extract LangGraph ``messages`` mode payloads from chain stream events."""

    if event.get("event") != "on_chain_stream":
        return None
    data = event.get("data") or {}
    if not isinstance(data, dict):
        return None
    candidate = data.get("chunk")
    if (
        isinstance(candidate, tuple)
        and len(candidate) == 2
        and candidate[0] == "messages"
    ):
        candidate = candidate[1]
    if not isinstance(candidate, tuple) or len(candidate) != 2:
        return None
    message, metadata = candidate
    if not isinstance(metadata, dict):
        return None
    if str(metadata.get("langgraph_node") or "") != "model":
        return None
    return message if hasattr(message, "content") else None
