"""Tests for chart builder (T3-04).

3 张图表：PNG 字节非空 + 同源 JSON 数据与输入一致。
"""

from __future__ import annotations

import json
from decimal import Decimal

from backend.application.course_eval.report.chart_builder import (
    build_distribution_chart,
    build_goal_trend_chart,
    build_section_rate_chart,
)


def test_distribution_chart():
    result = build_distribution_chart(
        band_labels=["50以下", "51-59", "60-69", "70-79", "80-89", "90以上"],
        counts=[28, 12, 17, 6, 5, 1],
        pcts=[
            Decimal("40.6"),
            Decimal("17.4"),
            Decimal("24.6"),
            Decimal("8.7"),
            Decimal("7.2"),
            Decimal("1.4"),
        ],
    )
    assert len(result.png_bytes) > 1000  # PNG 非空
    data = json.loads(result.data_json)
    assert data["type"] == "bar"
    assert data["values"] == [28, 12, 17, 6, 5, 1]
    assert data["labels"][0] == "50以下"


def test_section_rate_chart():
    result = build_section_rate_chart(
        labels=["填空题", "证明题", "作图题", "计算题"],
        rates=[Decimal("46.0"), Decimal("37.0"), Decimal("73.5"), Decimal("50.6")],
    )
    assert len(result.png_bytes) > 1000
    data = json.loads(result.data_json)
    assert data["type"] == "barh"
    assert data["values"] == ["46.0", "37.0", "73.5", "50.6"]


def test_goal_trend_chart():
    result = build_goal_trend_chart(
        goals=["目标1", "目标2"],
        current=[Decimal("0.618"), Decimal("0.654")],
        previous=[Decimal("0.602"), Decimal("0.640")],
    )
    assert len(result.png_bytes) > 1000
    data = json.loads(result.data_json)
    assert data["type"] == "grouped_bar"
    assert data["current"] == ["0.618", "0.654"]
    assert data["previous"] == ["0.602", "0.640"]


def test_goal_trend_chart_without_previous():
    result = build_goal_trend_chart(goals=["目标1"], current=[Decimal("0.618")])
    data = json.loads(result.data_json)
    assert data["previous"] is None
