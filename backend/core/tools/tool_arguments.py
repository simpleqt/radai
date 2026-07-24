"""Small, conservative repairs for malformed model tool arguments."""

from __future__ import annotations

import json
import re
from typing import Any

# Some OpenAI-compatible tool-call parsers occasionally leave their XML-like
# parameter envelope in the decoded argument.  Only remove it at the outer
# boundary; XML or HTML occurring inside a legitimate value is left intact.
_LEADING_PARAMETER_TAG = re.compile(r"^\s*<parameter(?:\s+[^>]*)?>\s*", re.IGNORECASE)
_TRAILING_PARAMETER_TAG = re.compile(r"\s*</parameter\s*>?\s*$", re.IGNORECASE)
_EMBEDDED_PARAMETER_TAG = re.compile(
    r"</?parameter(?:\s+[^>]*)?>?",
    re.IGNORECASE,
)


def clean_malformed_tool_text(value: Any) -> Any:
    """Remove an accidental outer ``<parameter>`` envelope from a string."""
    if not isinstance(value, str):
        return value

    cleaned = _LEADING_PARAMETER_TAG.sub("", value)
    cleaned = _TRAILING_PARAMETER_TAG.sub("", cleaned)
    return cleaned.strip() if cleaned != value else value


def clean_malformed_tool_args(value: Any) -> Any:
    """Recursively repair decoded tool-call arguments without changing types."""
    if isinstance(value, str):
        return clean_malformed_tool_text(value)
    if isinstance(value, dict):
        return {key: clean_malformed_tool_args(item) for key, item in value.items()}
    if isinstance(value, list):
        return [clean_malformed_tool_args(item) for item in value]
    if isinstance(value, tuple):
        return tuple(clean_malformed_tool_args(item) for item in value)
    return value


def parse_malformed_tool_args(value: Any) -> dict[str, Any] | None:
    """Recover a provider tool-call argument object when repair is lossless.

    OpenAI-compatible reasoning providers sometimes insert their internal
    ``<parameter>`` delimiter into the JSON argument stream.  LangChain then
    records the call in ``invalid_tool_calls`` even though removing that
    delimiter produces valid JSON.  Only this known delimiter is removed; we
    deliberately do not guess missing keys, quotes, commas, or braces.
    """
    if isinstance(value, dict):
        return clean_malformed_tool_args(value)
    if not isinstance(value, str):
        return None

    candidates = [value.strip()]
    without_parameter_tags = _EMBEDDED_PARAMETER_TAG.sub("", candidates[0]).strip()
    if without_parameter_tags != candidates[0]:
        candidates.append(without_parameter_tags)

    for candidate in candidates:
        try:
            decoded = json.loads(candidate)
        except (TypeError, json.JSONDecodeError):
            continue
        if isinstance(decoded, dict):
            return clean_malformed_tool_args(decoded)
    return None
