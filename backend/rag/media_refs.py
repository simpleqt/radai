"""Shared normalization for figure, table, and formula references."""

from __future__ import annotations

import re


def normalize_media_kind(kind: str) -> str:
    value = (kind or "").strip().lower()
    if value in {"图", "fig", "figure"}:
        return "图"
    if value in {"表", "table"}:
        return "表"
    if value in {"公式", "formula", "eq", "equation"}:
        return "公式"
    return ""


def normalize_media_number(number: str) -> str:
    compact = re.sub(r"\s+", "", number or "")
    return re.sub(r"[.\-－—–~～￣_]+", "-", compact).strip("-")


__all__ = ["normalize_media_kind", "normalize_media_number"]
