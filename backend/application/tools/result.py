"""Stable service-tool result and error envelopes."""

from __future__ import annotations

import json
from typing import Any

from backend.application.tools.execution import classify_tool_error


def normalize_tool_result(
    value: Any,
    *,
    tool_name: str,
    max_output_chars: int | None = None,
) -> dict[str, Any]:
    """Return one model- and API-friendly envelope while preserving legacy keys."""

    parsed = _parse_value(value)
    if isinstance(parsed, dict):
        text = str(
            parsed.get("summary") or parsed.get("text") or parsed.get("content") or ""
        )
        evidence = parsed.get("evidence")
        if not isinstance(evidence, list):
            evidence = parsed.get("evidence_records")
        if not isinstance(evidence, list):
            evidence = []
        artifacts = parsed.get("artifacts")
        if not isinstance(artifacts, list):
            artifact = parsed.get("artifact")
            artifacts = [artifact] if artifact is not None else []
        metrics = parsed.get("metrics")
        if not isinstance(metrics, dict):
            metrics = parsed.get("stats")
        if not isinstance(metrics, dict):
            metrics = {}
        warnings = parsed.get("warnings")
        if not isinstance(warnings, list):
            warnings = []
        registered_images = parsed.get("registered_images")
        if not isinstance(registered_images, list):
            registered_images = []
        data = parsed.get("data")
        if data is None:
            reserved = {
                "status",
                "summary",
                "text",
                "content",
                "data",
                "evidence",
                "evidence_records",
                "artifacts",
                "artifact",
                "warnings",
                "metrics",
                "stats",
                "registered_images",
                "error",
                "truncated",
            }
            data = {key: item for key, item in parsed.items() if key not in reserved}
    else:
        text = str(parsed or "")
        evidence, artifacts, metrics, warnings, registered_images, data = (
            [],
            [],
            {},
            [],
            [],
            {},
        )

    text, truncated = _truncate(text, max_output_chars)
    status = "success"
    if isinstance(parsed, dict):
        status = str(
            parsed.get("status")
            or ("error" if parsed.get("error") else "success")
        )
    envelope = {
        "status": status,
        "tool_name": tool_name,
        "summary": text,
        "data": data,
        "evidence": evidence,
        "artifacts": artifacts,
        "warnings": warnings,
        "metrics": metrics,
        "registered_images": registered_images,
        "truncated": bool(
            truncated or (isinstance(parsed, dict) and parsed.get("truncated"))
        ),
        "error": parsed.get("error") if isinstance(parsed, dict) else None,
        # Compatibility aliases used by coordinated evidence workflows.
        "text": text,
        "evidence_records": evidence,
        "stats": metrics,
    }
    return _fit_envelope(envelope, max_output_chars)


def normalize_tool_error(error: BaseException, *, tool_name: str) -> dict[str, Any]:
    code = classify_tool_error(error)
    details = getattr(error, "details", {})
    if not isinstance(details, dict):
        details = {}
    return {
        "status": "error",
        "tool_name": tool_name,
        "summary": "",
        "data": details,
        "evidence": [],
        "artifacts": [],
        "warnings": [],
        "metrics": {},
        "registered_images": [],
        "truncated": False,
        "error": {
            "code": code,
            "message": str(error) or type(error).__name__,
            "retryable": code
            in {"timeout", "connection", "rate_limited", "unavailable"},
        },
        "text": "",
        "evidence_records": [],
        "stats": {},
    }


def _parse_value(value: Any) -> Any:
    if not isinstance(value, str):
        return value
    try:
        return json.loads(value)
    except (TypeError, ValueError, json.JSONDecodeError):
        return value


def _truncate(text: str, limit: int | None) -> tuple[str, bool]:
    if limit is None or limit <= 0 or len(text) <= limit:
        return text, False
    marker = "...[truncated]"
    return text[: max(0, limit - len(marker))] + marker, True


def _fit_envelope(
    envelope: dict[str, Any],
    limit: int | None,
) -> dict[str, Any]:
    """Bound the serialized result, compacting lower-priority payloads first."""

    if limit is None or limit <= 0 or _serialized_size(envelope) <= limit:
        return envelope

    envelope["truncated"] = True
    warnings = [str(item) for item in envelope.get("warnings", [])]
    if "tool_output_truncated" not in warnings:
        warnings.append("tool_output_truncated")
    envelope["warnings"] = warnings
    structured_omissions: list[dict[str, Any]] = []

    # Structured data is often the largest duplicate of the human-readable
    # summary. Evidence is kept longer because downstream writers depend on it.
    if envelope.get("data"):
        envelope["data"] = {"_truncated": True}
        structured_omissions.append(
            {"reason": "tool_output_truncated", "dropped": "data"}
        )
    if _serialized_size(envelope) > limit and envelope.get("artifacts"):
        envelope["artifacts"] = []
        structured_omissions.append(
            {"reason": "tool_output_truncated", "dropped": "artifacts"}
        )
    if _serialized_size(envelope) > limit and envelope.get("metrics"):
        envelope["metrics"] = {}
        envelope["stats"] = envelope["metrics"]
        structured_omissions.append(
            {"reason": "tool_output_truncated", "dropped": "metrics"}
        )

    original_text = str(envelope.get("text") or envelope.get("summary") or "")
    if _serialized_size(envelope) > limit:
        _fit_text_aliases(envelope, limit, original=original_text)

    # If structured metadata alone is oversized, shed records from the tail.
    # Evidence and registered images survive whenever a smaller text body is
    # enough to satisfy the limit.
    evidence = list(envelope.get("evidence") or [])
    if evidence and _serialized_size(envelope) > limit:
        _fit_sequence_aliases(
            envelope,
            limit,
            values=evidence,
            keys=("evidence", "evidence_records"),
        )

    registered_images = list(envelope.get("registered_images") or [])
    if registered_images and _serialized_size(envelope) > limit:
        _fit_sequence_aliases(
            envelope,
            limit,
            values=registered_images,
            keys=("registered_images",),
        )

    if _serialized_size(envelope) <= limit and not envelope.get("text") and original_text:
        _fit_text_aliases(envelope, limit, original=original_text)
    if _serialized_size(envelope) > limit and envelope.get("error"):
        error = envelope.get("error")
        if isinstance(error, dict):
            compact_error = {
                "code": str(error.get("code") or "internal_error")[:80],
                "message": _truncate(str(error.get("message") or ""), max(1, limit // 4))[0],
                "retryable": bool(error.get("retryable", False)),
            }
            envelope["error"] = compact_error
        else:
            envelope["error"] = _truncate(str(error), max(1, limit // 4))[0]
    if _serialized_size(envelope) > limit:
        envelope["warnings"] = ["tool_output_truncated"]
    else:
        candidate_warnings = [*warnings, *structured_omissions]
        envelope["warnings"] = candidate_warnings
        if _serialized_size(envelope) > limit:
            envelope["warnings"] = warnings
    return envelope


def _fit_text_aliases(
    envelope: dict[str, Any],
    limit: int,
    *,
    original: str | None = None,
) -> None:
    """Fit duplicated summary/text aliases without subtracting overflow twice."""

    original = str(
        original
        if original is not None
        else envelope.get("text") or envelope.get("summary") or ""
    )
    low, high = 0, len(original)
    best = ""
    marker = "...[truncated]"
    while low <= high:
        midpoint = (low + high) // 2
        if midpoint >= len(original):
            candidate = original
        elif midpoint <= len(marker):
            candidate = marker[:midpoint]
        else:
            candidate = original[: midpoint - len(marker)] + marker
        envelope["summary"] = candidate
        envelope["text"] = candidate
        if _serialized_size(envelope) <= limit:
            best = candidate
            low = midpoint + 1
        else:
            high = midpoint - 1
    envelope["summary"] = best
    envelope["text"] = best


def _fit_sequence_aliases(
    envelope: dict[str, Any],
    limit: int,
    *,
    values: list[Any],
    keys: tuple[str, ...],
) -> None:
    """Keep the longest fitting prefix without serializing once per record."""

    low, high = 0, len(values)
    best = 0
    while low <= high:
        midpoint = (low + high) // 2
        candidate = values[:midpoint]
        for key in keys:
            envelope[key] = candidate
        if _serialized_size(envelope) <= limit:
            best = midpoint
            low = midpoint + 1
        else:
            high = midpoint - 1
    fitted = values[:best]
    for key in keys:
        envelope[key] = fitted


def _serialized_size(value: Any) -> int:
    return len(json.dumps(value, ensure_ascii=False, default=str, separators=(",", ":")))


__all__ = ["normalize_tool_error", "normalize_tool_result"]
