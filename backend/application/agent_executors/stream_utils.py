"""Utilities for translating LangGraph events into runtime event payloads."""

from __future__ import annotations

import json
import time
import uuid
from typing import Any
from urllib.parse import urlparse


def summarize_tool_output(value: Any, max_length: int = 320) -> str:
    if value is None:
        return ""

    if isinstance(value, str):
        text = value.strip()
    else:
        try:
            text = json.dumps(value, ensure_ascii=False)
        except Exception:
            text = str(value)

    for prefix in ("Task Succeeded. Result:", "Task failed. Error:", "Task timed out. Error:"):
        if text.startswith(prefix):
            text = text[len(prefix):].strip()
            break

    metadata_marker = "Metadata:"
    idx = text.rfind(metadata_marker)
    if idx > 0:
        after = text[idx + len(metadata_marker):].strip()
        if after.startswith("{"):
            text = text[:idx].strip()

    text = " ".join(text.split())
    if len(text) > max_length:
        return f"{text[:max_length]}..."
    return text


def extract_task_message_text(raw_message: Any) -> str:
    if isinstance(raw_message, dict):
        content = raw_message.get("content")
        if isinstance(content, str) and content.strip():
            return content.strip()
        if isinstance(content, list):
            parts: list[str] = []
            for item in content:
                if isinstance(item, str) and item.strip():
                    parts.append(item.strip())
                elif isinstance(item, dict):
                    text = str(item.get("text") or "").strip()
                    if text:
                        parts.append(text)
            if parts:
                return "\n".join(parts)
    if isinstance(raw_message, str):
        return raw_message.strip()
    return summarize_tool_output(raw_message, max_length=800)


def _parse_web_search_payload(raw_output: Any) -> dict[str, Any]:
    """从工具原始输出（str / dict / ToolMessage 等）解析 web_search JSON。"""
    if isinstance(raw_output, dict):
        if isinstance(raw_output.get("results"), list):
            return raw_output
        for key in ("output", "content", "result", "text"):
            nested = raw_output.get(key)
            if nested is not None and nested is not raw_output:
                parsed = _parse_web_search_payload(nested)
                if parsed:
                    return parsed
        return raw_output

    if isinstance(raw_output, str):
        text = raw_output.strip()
        if text.startswith("{"):
            try:
                parsed = json.loads(text)
                if isinstance(parsed, dict):
                    return parsed
            except Exception:
                try:
                    parsed, _ = json.JSONDecoder().raw_decode(text)
                    if isinstance(parsed, dict):
                        return parsed
                except Exception:
                    return {}
        return {}

    text = stringify_message_content(raw_output).strip()
    if text.startswith("{"):
        return _parse_web_search_payload(text)
    return {}


def extract_web_search_metadata(raw_output: Any) -> dict[str, Any]:
    data = _parse_web_search_payload(raw_output)
    if not data:
        return {}

    query = str(data.get("query") or "").strip()
    results = data.get("results")
    urls: list[str] = []
    domains: list[str] = []
    web_results: list[dict[str, str]] = []

    if isinstance(results, list):
        for item in results:
            if not isinstance(item, dict):
                continue
            url = str(item.get("url") or item.get("href") or item.get("link") or "").strip()
            title = str(item.get("title") or "").strip()
            if not url:
                continue
            if url not in urls:
                urls.append(url)

            domain = urlparse(url).netloc.lower().strip()
            if domain.startswith("www."):
                domain = domain[4:]
            if domain and domain not in domains:
                domains.append(domain)
            # Keep a bounded evidence excerpt in the process event. The
            # coordinator previously retained only title/URL/domain, so the
            # branch handoff knew that pages had been retrieved but could not
            # map the branch's claims back to the page content.
            raw_excerpt = (
                item.get("content")
                or item.get("markdown")
                or item.get("description")
                or item.get("snippet")
                or ""
            )
            content_excerpt = " ".join(str(raw_excerpt).split())[:2000]
            web_result = {"title": title, "url": url, "domain": domain}
            if content_excerpt:
                web_result["content_excerpt"] = content_excerpt
            web_results.append(web_result)

    metadata: dict[str, Any] = {}
    engine = str(data.get("engine") or "").strip().lower()
    if engine:
        metadata["engine"] = engine
    if query:
        metadata["query"] = query
    if urls:
        metadata["urls"] = urls[:5]
    if domains:
        metadata["domains"] = domains[:5]
    if web_results:
        metadata["web_results"] = web_results[:5]
    total_results = data.get("total_results")
    if isinstance(total_results, int):
        metadata["total_results"] = total_results
    elif web_results:
        metadata["total_results"] = len(web_results)
    return metadata


def extract_report_metadata(raw_output: Any) -> dict[str, Any]:
    def parse_text_payload(text: str) -> dict[str, Any]:
        text = (text or "").strip()
        if not text:
            return {}
        if text.startswith("{"):
            try:
                parsed = json.loads(text)
                if isinstance(parsed, dict):
                    return parsed
            except Exception:
                try:
                    parsed, _ = json.JSONDecoder().raw_decode(text)
                    if isinstance(parsed, dict):
                        return parsed
                except Exception:
                    return {}

        marker = "Metadata:"
        idx = text.find(marker)
        if idx >= 0:
            payload_text = text[idx + len(marker):].strip()
            if payload_text.startswith("{"):
                try:
                    parsed = json.loads(payload_text)
                    if isinstance(parsed, dict):
                        return parsed
                except Exception:
                    try:
                        parsed, _ = json.JSONDecoder().raw_decode(payload_text)
                        if isinstance(parsed, dict):
                            return parsed
                    except Exception:
                        return {}
        return {}

    metadata: dict[str, Any] = {}
    queue: list[Any] = [raw_output]
    visited: set[int] = set()

    while queue:
        item = queue.pop(0)
        if item is None:
            continue
        item_id = id(item)
        if item_id in visited:
            continue
        visited.add(item_id)

        candidate: dict[str, Any] = {}
        if isinstance(item, dict):
            candidate = item
            queue.extend(item.values())
            if "artifact_path" not in metadata:
                artifacts_val = item.get("artifacts")
                if isinstance(artifacts_val, list):
                    for art in artifacts_val:
                        if isinstance(art, str) and art.strip():
                            metadata["artifact_path"] = art.strip()
                            break
        elif isinstance(item, (list, tuple, set)):
            queue.extend(list(item))
            continue
        elif isinstance(item, str):
            candidate = parse_text_payload(item)
        else:
            content = getattr(item, "content", None)
            if isinstance(content, str):
                candidate = parse_text_payload(content)
            elif isinstance(content, (dict, list, tuple, set)):
                queue.append(content)

            update = getattr(item, "update", None)
            if isinstance(update, (dict, list, tuple, set)):
                queue.append(update)

            messages = getattr(item, "messages", None)
            if isinstance(messages, (dict, list, tuple, set)):
                queue.append(messages)

        for key in ("title", "artifact_path", "minio_object"):
            val = candidate.get(key) if isinstance(candidate, dict) else None
            if isinstance(val, str) and val.strip() and key not in metadata:
                metadata[key] = val.strip()

        if all(key in metadata for key in ("artifact_path", "title", "minio_object")):
            break

    return metadata


def extract_task_result_metadata(raw_output: Any) -> dict[str, Any]:
    if isinstance(raw_output, dict):
        return {
            k: v
            for k, v in raw_output.items()
            if k in {"result_preview", "result_sections", "urls", "domains", "tool_calls", "search_queries", "message_count"}
        }

    if not isinstance(raw_output, str):
        return {}

    text = raw_output.strip()
    marker = "Metadata:"
    idx = text.rfind(marker)
    if idx < 0:
        return {}

    payload = text[idx + len(marker):].strip()
    if not payload.startswith("{"):
        return {}

    try:
        parsed = json.loads(payload)
    except Exception:
        return {}

    if not isinstance(parsed, dict):
        return {}
    return {
        k: parsed.get(k)
        for k in ("result_preview", "result_sections", "urls", "domains", "tool_calls", "search_queries", "message_count")
        if parsed.get(k) not in (None, "", [], {})
    }


def make_process_event(
    kind: str,
    title: str,
    detail: str,
    *,
    tool: str | None = None,
    level: str = "info",
    duration_ms: int | None = None,
    summary: str | None = None,
    metadata: dict[str, Any] | None = None,
    event_key: str | None = None,
    phase: str | None = None,
    display: dict[str, Any] | None = None,
) -> dict[str, Any]:
    # `detail` is intentionally concise and can be identical for multiple
    # concurrent invocations (for example, every web search may report
    # "5 sources"). Include invocation identity/metadata so the frontend's
    # idempotency filter does not discard later, valid results.
    identity = str(event_key or "").strip()
    if not identity:
        metadata_identity = json.dumps(
            metadata or {},
            ensure_ascii=False,
            sort_keys=True,
            default=str,
        )
        identity = ":".join(
            [kind, title, detail, str(tool or ""), str(summary or ""), metadata_identity]
        )
    event_id = str(uuid.uuid5(uuid.NAMESPACE_DNS, identity))
    event: dict[str, Any] = {
        "id": event_id,
        "kind": kind,
        "title": title,
        "detail": detail,
        "level": level,
        "timestamp": int(time.time() * 1000),
    }
    if tool:
        event["tool"] = tool
    if duration_ms is not None:
        event["duration_ms"] = duration_ms
    if summary:
        event["summary"] = summary
    if metadata:
        event["metadata"] = metadata
    if phase:
        event["phase"] = phase
    if display:
        # 上限校验放在唯一出口，手工构造的展示载荷同样受约束。
        from backend.application.agent_executors.tool_presenters.types import (
            normalize_display_payload,
        )

        normalized = normalize_display_payload(display, tool_name=str(tool or ""))
        if normalized:
            event["display"] = normalized
    return event


def extract_task_runtime_event_payload(event: dict[str, Any]) -> dict[str, Any] | None:
    """Best-effort extraction for task tool runtime stream payloads."""
    queue: list[Any] = [event]
    visited: set[int] = set()

    while queue:
        candidate = queue.pop(0)
        if candidate is None:
            continue
        cid = id(candidate)
        if cid in visited:
            continue
        visited.add(cid)

        if isinstance(candidate, dict):
            ctype = str(candidate.get("type", "")).strip()
            if ctype.startswith("task_"):
                return candidate

            data_val = candidate.get("data")
            if isinstance(data_val, dict):
                data_type = str(data_val.get("type", "")).strip()
                if data_type.startswith("task_"):
                    return data_val
            elif isinstance(data_val, str):
                raw = data_val.strip()
                if raw.startswith("{"):
                    try:
                        parsed = json.loads(raw)
                        if isinstance(parsed, dict):
                            parsed_type = str(parsed.get("type", "")).strip()
                            if parsed_type.startswith("task_"):
                                return parsed
                            queue.append(parsed)
                    except Exception:
                        pass

            custom_val = candidate.get("custom")
            if isinstance(custom_val, dict):
                custom_type = str(custom_val.get("type", "")).strip()
                if custom_type.startswith("task_"):
                    return custom_val

            payload_val = candidate.get("payload")
            if isinstance(payload_val, dict):
                payload_type = str(payload_val.get("type", "")).strip()
                if payload_type.startswith("task_"):
                    return payload_val

            queue.extend(candidate.values())
        elif isinstance(candidate, (list, tuple, set)):
            queue.extend(list(candidate))
        else:
            if hasattr(candidate, "model_dump"):
                try:
                    dumped = candidate.model_dump()
                    if isinstance(dumped, dict):
                        queue.append(dumped)
                except Exception:
                    pass
            elif hasattr(candidate, "__dict__"):
                queue.append(vars(candidate))

    return None


def build_task_runtime_metadata(
    payload: dict[str, Any],
    *,
    task_id: str | None,
    description: str | None = None,
) -> dict[str, Any]:
    metadata = payload.get("metadata")
    normalized: dict[str, Any] = dict(metadata) if isinstance(metadata, dict) else {}

    if task_id:
        normalized["task_id"] = task_id
    if description:
        normalized.setdefault("description", description)

    subagent_type = str(payload.get("subagent_type", "")).strip()
    if subagent_type:
        normalized.setdefault("subagent_type", subagent_type)

    trace_id = str(payload.get("trace_id", "")).strip()
    if trace_id:
        normalized.setdefault("trace_id", trace_id)

    for key in (
        "query",
        "urls",
        "domains",
        "total_results",
        "message_text",
        "message_tool_calls",
        "result_preview",
        "result_sections",
        "message_count",
    ):
        if key in payload and payload.get(key) not in (None, "", [], {}):
            normalized.setdefault(key, payload.get(key))

    return normalized


def iter_recovered_text_chunks(text: str, max_chars: int = 600):
    """Split non-streamed final model output into UI-friendly chunks."""
    remaining = str(text or "")
    while remaining:
        if len(remaining) <= max_chars:
            yield remaining
            break

        split_at = remaining.rfind("\n", 0, max_chars)
        if split_at < max_chars // 2:
            split_at = remaining.rfind("。", 0, max_chars)
            if split_at >= max_chars // 2:
                split_at += 1
        if split_at < max_chars // 2:
            split_at = max_chars

        yield remaining[:split_at]
        remaining = remaining[split_at:]


def stringify_message_content(content: Any) -> str:
    if isinstance(content, str):
        return content
    if isinstance(content, list):
        parts: list[str] = []
        for item in content:
            parts.append(stringify_message_content(item))
        return "".join(part for part in parts if part)
    if isinstance(content, dict):
        text_value = content.get("text")
        if isinstance(text_value, str):
            return text_value
        nested_content = content.get("content")
        if nested_content is not None:
            return stringify_message_content(nested_content)
        output_text = content.get("output_text")
        if isinstance(output_text, str):
            return output_text
        return ""
    if hasattr(content, "content"):
        return stringify_message_content(getattr(content, "content"))
    if hasattr(content, "text"):
        text_value = getattr(content, "text")
        if isinstance(text_value, str):
            return text_value
    return ""


def extract_reasoning_content(message: Any) -> str:
    # 1. Direct attribute extraction
    direct = getattr(message, "reasoning_content", None)
    if direct:
        return stringify_message_content(direct)

    # 2. Fallback to additional_kwargs
    additional_kwargs = getattr(message, "additional_kwargs", None)
    if isinstance(additional_kwargs, dict):
        reasoning = additional_kwargs.get("reasoning_content") or additional_kwargs.get("reasoning")
        if reasoning:
            return stringify_message_content(reasoning)
    return ""


def extract_chat_model_output_text(output: Any) -> str:
    if output is None:
        return ""
    direct = stringify_message_content(output)
    if direct:
        return direct
    if isinstance(output, dict):
        for key in ("message", "output", "result"):
            if key in output:
                text = extract_chat_model_output_text(output.get(key))
                if text:
                    return text
        generations = output.get("generations")
        if generations:
            return extract_chat_model_output_text(generations)
        return ""
    if isinstance(output, list):
        parts: list[str] = []
        for item in output:
            text = extract_chat_model_output_text(item)
            if text:
                parts.append(text)
        return "".join(parts)
    if hasattr(output, "message"):
        text = extract_chat_model_output_text(getattr(output, "message"))
        if text:
            return text
    if hasattr(output, "generations"):
        return extract_chat_model_output_text(getattr(output, "generations"))
    return ""
