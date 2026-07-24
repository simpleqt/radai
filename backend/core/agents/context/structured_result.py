"""Deterministic compaction and identity for structured tool results."""

from __future__ import annotations

import hashlib
import json
import math
from collections import Counter
from numbers import Real
from typing import Any

_MAX_PROFILE_COLUMNS = 32
_MAX_CATEGORY_COLUMNS = 12
_MAX_CATEGORY_VALUES = 6
_MAX_GROUP_COVERAGE_ITEMS = 6


def _payload(value: Any) -> dict[str, Any] | None:
    if isinstance(value, dict):
        return value
    if not isinstance(value, str):
        return None
    try:
        decoded = json.loads(value)
    except (TypeError, json.JSONDecodeError):
        return None
    return decoded if isinstance(decoded, dict) else None


def structured_result_fingerprint(value: Any) -> str | None:
    """Return an order-insensitive identity for a successful tabular result."""
    payload = _payload(value)
    rows = payload.get("rows") if payload is not None else None
    if not isinstance(rows, list) or payload.get("error"):
        return None
    canonical = {
        "project_id": payload.get("project_id"),
        "data_version": payload.get("data_version"),
        "query_kind": payload.get("query_kind"),
        "row_count": payload.get("row_count", len(rows)),
        "columns": sorted(str(item) for item in (payload.get("columns") or [])),
        "rows": rows,
    }
    encoded = json.dumps(
        canonical,
        ensure_ascii=False,
        sort_keys=True,
        separators=(",", ":"),
        default=str,
    )
    return hashlib.sha256(encoded.encode("utf-8")).hexdigest()


def _is_null(value: Any) -> bool:
    if value is None:
        return True
    if isinstance(value, str):
        return not value.strip()
    if isinstance(value, Real) and not isinstance(value, bool):
        try:
            return not math.isfinite(float(value))
        except (TypeError, ValueError, OverflowError):
            return False
    return False


def _numeric_value(value: Any) -> float | None:
    if isinstance(value, bool) or not isinstance(value, Real):
        return None
    try:
        numeric = float(value)
    except (TypeError, ValueError, OverflowError):
        return None
    return numeric if math.isfinite(numeric) else None


def _compact_number(value: float) -> int | float:
    if value.is_integer():
        return int(value)
    return round(value, 8)


def _scalar_key(value: Any) -> tuple[str, str] | None:
    if isinstance(value, (dict, list, tuple, set)):
        return None
    if isinstance(value, str):
        rendered = value.strip()
    else:
        rendered = str(value)
    if not rendered:
        return None
    return type(value).__name__, rendered[:160]


def _ordered_columns(payload: dict[str, Any], rows: list[Any]) -> list[str]:
    columns: list[str] = []
    seen: set[str] = set()
    for value in payload.get("columns") or []:
        column = str(value)
        if column and column not in seen:
            seen.add(column)
            columns.append(column)
    for row in rows:
        if not isinstance(row, dict):
            continue
        for value in row:
            column = str(value)
            if column and column not in seen:
                seen.add(column)
                columns.append(column)
    return columns[:_MAX_PROFILE_COLUMNS]


def _looks_like_identifier(column: str) -> bool:
    normalized = column.casefold()
    return normalized in {"id", "code"} or normalized.endswith(("_id", "_code"))


def _column_profiles(
    rows: list[Any],
    columns: list[str],
) -> tuple[list[dict[str, Any]], dict[str, dict[str, Any]]]:
    profiles: list[dict[str, Any]] = []
    categories: dict[str, dict[str, Any]] = {}
    dict_rows = [row for row in rows if isinstance(row, dict)]

    for column in columns:
        values = [row.get(column) for row in dict_rows]
        non_null = [value for value in values if not _is_null(value)]
        scalar_keys = [key for value in non_null if (key := _scalar_key(value))]
        distinct_count = len(set(scalar_keys))
        profile: dict[str, Any] = {
            "column": column,
            "non_null_count": len(non_null),
            "null_count": len(values) - len(non_null),
            "distinct_count": distinct_count,
        }

        numeric_values = [
            numeric
            for value in non_null
            if (numeric := _numeric_value(value)) is not None
        ]
        if numeric_values:
            profile["numeric_count"] = len(numeric_values)
            if len(numeric_values) < len(non_null):
                profile["non_numeric_count"] = len(non_null) - len(numeric_values)
            profile["numeric_range"] = {
                "min": _compact_number(min(numeric_values)),
                "max": _compact_number(max(numeric_values)),
            }
        profiles.append(profile)

        category_keys = [
            key
            for value in non_null
            if _numeric_value(value) is None
            if (key := _scalar_key(value)) is not None
        ]
        if (
            len(categories) >= _MAX_CATEGORY_COLUMNS
            or not category_keys
            or _looks_like_identifier(column)
        ):
            continue
        counts = Counter(key[1] for key in category_keys)
        top_values = [
            {"value": value, "count": count}
            for value, count in sorted(
                counts.items(),
                key=lambda item: (-item[1], item[0]),
            )[:_MAX_CATEGORY_VALUES]
        ]
        categories[column] = {
            "values": top_values,
            "truncated": len(counts) > len(top_values),
        }
    return profiles, categories


def _interval_column_pairs(columns: list[str]) -> list[tuple[str, str]]:
    available = {column.casefold(): column for column in columns}
    pairs: list[tuple[str, str]] = []
    for column in columns:
        tokens = column.casefold().split("_")
        if "top" not in tokens:
            continue
        index = tokens.index("top")
        bottom_tokens = [*tokens]
        bottom_tokens[index] = "bottom"
        bottom = "_".join(bottom_tokens)
        if bottom in available:
            pairs.append((column, available[bottom]))
    return pairs


def _group_column(rows: list[dict[str, Any]], columns: list[str]) -> str | None:
    row_count = len(rows)
    if row_count < 2:
        return None
    candidates: list[tuple[int, int, str]] = []
    for order, column in enumerate(columns):
        if not _looks_like_identifier(column):
            continue
        values = {
            key
            for row in rows
            if not _is_null(row.get(column))
            if (key := _scalar_key(row.get(column))) is not None
        }
        if 1 < len(values) < row_count:
            candidates.append((len(values), order, column))
    if not candidates:
        return None
    return min(candidates, key=lambda item: (item[0], item[1]))[2]


def _interval_coverage(
    rows: list[Any],
    columns: list[str],
) -> list[dict[str, Any]]:
    dict_rows = [row for row in rows if isinstance(row, dict)]
    group_column = _group_column(dict_rows, columns)
    coverage_items: list[dict[str, Any]] = []
    for top_column, bottom_column in _interval_column_pairs(columns):
        intervals: list[tuple[float, float, Any]] = []
        null_pair_count = 0
        invalid_order_count = 0
        for row in dict_rows:
            top = _numeric_value(row.get(top_column))
            bottom = _numeric_value(row.get(bottom_column))
            if top is None or bottom is None:
                null_pair_count += 1
                continue
            if bottom < top:
                invalid_order_count += 1
                continue
            intervals.append((top, bottom, row.get(group_column) if group_column else None))
        if not intervals:
            continue

        min_top = min(item[0] for item in intervals)
        max_bottom = max(item[1] for item in intervals)
        item: dict[str, Any] = {
            "top_column": top_column,
            "bottom_column": bottom_column,
            "valid_interval_count": len(intervals),
            "null_pair_count": null_pair_count,
            "invalid_order_count": invalid_order_count,
            "overall_range": {
                "min_top": _compact_number(min_top),
                "max_bottom": _compact_number(max_bottom),
                "span": _compact_number(max_bottom - min_top),
            },
            "interval_length_sum": _compact_number(
                sum(bottom - top for top, bottom, _ in intervals)
            ),
        }
        if group_column:
            grouped: dict[str, list[tuple[float, float]]] = {}
            for top, bottom, group in intervals:
                if _is_null(group):
                    continue
                grouped.setdefault(str(group), []).append((top, bottom))
            if grouped:
                group_ranges = [
                    {
                        "group": group,
                        "interval_count": len(group_intervals),
                        "min_top": _compact_number(
                            min(interval[0] for interval in group_intervals)
                        ),
                        "max_bottom": _compact_number(
                            max(interval[1] for interval in group_intervals)
                        ),
                    }
                    for group, group_intervals in sorted(grouped.items())
                ]
                max_bottoms = [entry["max_bottom"] for entry in group_ranges]
                item.update(
                    {
                        "group_column": group_column,
                        "group_count": len(group_ranges),
                        "group_max_bottom_range": {
                            "min": min(max_bottoms),
                            "max": max(max_bottoms),
                        },
                        "group_coverage_preview": group_ranges[
                            :_MAX_GROUP_COVERAGE_ITEMS
                        ],
                        "omitted_group_count": max(
                            0, len(group_ranges) - _MAX_GROUP_COVERAGE_ITEMS
                        ),
                    }
                )
        coverage_items.append(item)
    return coverage_items


def _table_statistics(payload: dict[str, Any], rows: list[Any]) -> dict[str, Any]:
    columns = _ordered_columns(payload, rows)
    profiles, categories = _column_profiles(rows, columns)
    statistics: dict[str, Any] = {
        "profiled_row_count": len(rows),
        "profiled_column_count": len(columns),
        "column_profiles": profiles,
    }
    if categories:
        statistics["categorical_values"] = categories
    interval_coverage = _interval_coverage(rows, columns)
    if interval_coverage:
        statistics["interval_coverage"] = interval_coverage
    return statistics


def _render_compact_payload(compact: dict[str, Any], *, max_chars: int) -> str:
    """Fit valid JSON by dropping samples before deterministic statistics."""

    def render() -> str:
        return json.dumps(compact, ensure_ascii=False, default=str)

    budget = max(600, int(max_chars))
    rendered = render()
    if len(rendered) <= budget:
        return rendered

    preview = compact.get("rows_preview")
    if isinstance(preview, list) and len(preview) > 3:
        compact["rows_preview"] = [*preview[:2], preview[-1]]
        compact["preview_note"] = "行样本因上下文预算缩减；统计仍基于全部返回行。"
        rendered = render()
        if len(rendered) <= budget:
            return rendered

    statistics = compact.get("table_statistics")
    if isinstance(statistics, dict):
        for coverage in statistics.get("interval_coverage") or []:
            if not isinstance(coverage, dict):
                continue
            group_preview = coverage.get("group_coverage_preview")
            if isinstance(group_preview, list) and len(group_preview) > 3:
                coverage["group_coverage_preview"] = group_preview[:3]
                coverage["omitted_group_count"] = max(
                    int(coverage.get("group_count") or 0) - 3,
                    int(coverage.get("omitted_group_count") or 0),
                )
        for category in (statistics.get("categorical_values") or {}).values():
            if not isinstance(category, dict):
                continue
            values = category.get("values")
            if isinstance(values, list) and len(values) > 3:
                category["values"] = values[:3]
                category["truncated"] = True
        rendered = render()
        if len(rendered) <= budget:
            return rendered

    if isinstance(compact.get("rows_preview"), list):
        compact["rows_preview"] = compact["rows_preview"][:1]
        rendered = render()
        if len(rendered) <= budget:
            return rendered

    compact["rows_preview"] = []
    compact["preview_note"] = "行样本已省略；统计仍基于全部返回行。"
    rendered = render()
    if len(rendered) <= budget:
        return rendered

    if isinstance(statistics, dict):
        for coverage in statistics.get("interval_coverage") or []:
            if isinstance(coverage, dict):
                coverage.pop("group_coverage_preview", None)
        categories = statistics.get("categorical_values")
        if isinstance(categories, dict):
            for category in categories.values():
                if isinstance(category, dict) and isinstance(
                    category.get("values"), list
                ):
                    category["values"] = category["values"][:1]
                    category["truncated"] = True
        rendered = render()
        if len(rendered) <= budget:
            return rendered

        profiles = statistics.get("column_profiles")
        if isinstance(profiles, list):
            original_profile_count = len(profiles)
            prioritized = sorted(
                profiles,
                key=lambda profile: (
                    not bool(profile.get("null_count")),
                    "numeric_range" not in profile,
                ),
            )
            for keep_count in (24, 16, 8, 4):
                if len(prioritized) <= keep_count:
                    continue
                statistics["column_profiles"] = prioritized[:keep_count]
                statistics["omitted_column_profile_count"] = (
                    original_profile_count - keep_count
                )
                rendered = render()
                if len(rendered) <= budget:
                    return rendered

        statistics.pop("categorical_values", None)
        interval_coverage = statistics.get("interval_coverage")
        if isinstance(interval_coverage, list) and len(interval_coverage) > 2:
            statistics["interval_coverage"] = interval_coverage[:2]
            statistics["omitted_interval_coverage_count"] = (
                len(interval_coverage) - 2
            )
        rendered = render()
        if len(rendered) <= budget:
            return rendered

    minimal = {
        key: compact.get(key)
        for key in ("project_id", "data_version", "query_kind", "row_count")
        if compact.get(key) is not None
    }
    if isinstance(statistics, dict):
        minimal["table_statistics"] = {
            key: statistics.get(key)
            for key in ("profiled_row_count", "profiled_column_count")
            if statistics.get(key) is not None
        }
    minimal["instruction"] = (
        "统计明细因上下文预算省略；完整结果保存在动态上下文工件中。"
    )
    return json.dumps(minimal, ensure_ascii=False, default=str)


def compact_structured_result(
    value: Any,
    *,
    max_chars: int = 5000,
    head_rows: int = 8,
    tail_rows: int = 2,
) -> str | None:
    """Compact JSON rows without another model call.

    The complete result remains in the context artifact. This representation
    preserves schema, cardinality, bounded column profiles, categorical
    frequencies, interval coverage and a small row sample so callers can
    decide whether a targeted aggregate query is still needed.
    """
    payload = _payload(value)
    rows = payload.get("rows") if payload is not None else None
    if not isinstance(rows, list) or payload.get("error"):
        return None

    head_count = max(1, int(head_rows))
    tail_count = max(0, int(tail_rows))
    if len(rows) <= head_count + tail_count:
        preview = rows
    else:
        preview = (
            [*rows[:head_count], *rows[-tail_count:]]
            if tail_count
            else rows[:head_count]
        )

    compact = {
        key: payload.get(key)
        for key in (
            "project_id",
            "data_version",
            "engine",
            "query_kind",
            "row_count",
            "columns",
            "query_note",
            "table_aliases_applied",
        )
        if payload.get(key) is not None
    }
    payload_columns = payload.get("columns")
    if isinstance(payload_columns, list) and len(payload_columns) > _MAX_PROFILE_COLUMNS:
        compact["columns"] = payload_columns[:_MAX_PROFILE_COLUMNS]
        compact["omitted_column_count"] = len(payload_columns) - _MAX_PROFILE_COLUMNS
    compact["table_statistics"] = _table_statistics(payload, rows)
    compact["rows_preview"] = preview
    omitted = max(0, len(rows) - len(preview))
    if omitted:
        compact["omitted_row_count"] = omitted
        compact["instruction"] = (
            "完整结果已保存在动态上下文工件中；不要重新查询同一完整列表。"
            "如仍缺少结论，请执行更有针对性的聚合或筛选查询。"
        )
    return _render_compact_payload(compact, max_chars=max_chars)


__all__ = ["compact_structured_result", "structured_result_fingerprint"]
