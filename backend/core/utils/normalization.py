"""Dependency-light string normalization helpers."""

from __future__ import annotations

from collections.abc import Iterable
from typing import Any


_DOCUMENT_SUFFIXES = {".pdf", ".doc", ".docx", ".txt", ".md"}


def normalize_unique_strings(values: Iterable[object] | None) -> list[str]:
    """Strip, discard empty values, and preserve first-seen order."""
    result: list[str] = []
    seen: set[str] = set()
    for value in values or ():
        normalized = str(value or "").strip()
        if normalized and normalized not in seen:
            seen.add(normalized)
            result.append(normalized)
    return result


def normalize_string_list(value: Any) -> list[str]:
    """Normalize a scalar string or list of values into a string list."""
    if isinstance(value, str):
        return [value.strip()] if value.strip() else []
    if not isinstance(value, list):
        return []
    return [str(item).strip() for item in value if str(item or "").strip()]


def normalize_string_set(value: Any) -> set[str]:
    """Normalize a scalar string or list of values into a string set."""
    return set(normalize_string_list(value))


def normalize_document_file_name(file_name: str) -> str:
    """Normalize a document name for case-insensitive storage matching."""
    normalized = file_name.strip()
    lowered = normalized.lower()
    for suffix in _DOCUMENT_SUFFIXES:
        if lowered.endswith(suffix):
            normalized = normalized[: -len(suffix)]
            break
    return normalized.lower().strip()


__all__ = [
    "normalize_string_list",
    "normalize_string_set",
    "normalize_unique_strings",
    "normalize_document_file_name",
]
