"""ECharts 交互式图表链路测试

覆盖三层：
1. excel_echarts_generator —— 各图型产出纯 JSON option、drilldown、大小上限；
2. chart_image_store —— option JSON 与 PNG 同 chart_id 存取（本地降级路径）；
3. chart_images 路由 —— format=option 的 200/403/404 与默认 PNG 不受影响。
"""

from __future__ import annotations

import asyncio
import json
from urllib.parse import parse_qs, urlparse

import numpy as np
import pandas as pd
import pytest
from fastapi import FastAPI
from fastapi.testclient import TestClient

import backend.services.chart_image_store as chart_store
from backend.application.tools.providers import excel_echarts_generator as gen
from backend.application.tools.providers.excel_echarts_generator import (
    build_echarts_payload,
)

# ── fixtures ──────────────────────────────────────────────────────────


@pytest.fixture
def sample_df() -> pd.DataFrame:
    rng = np.random.default_rng(42)
    return pd.DataFrame(
        {
            "材料": ["A", "B", "C"] * 20,
            "温度": list(range(300, 360)),
            "降解率": rng.uniform(10, 95, 60),
            "速率": rng.uniform(1e-4, 3e-2, 60),
            "批次": ["第一批", "第二批"] * 30,
            "日期": pd.date_range("2025-01-01", periods=60, freq="D").astype(str),
        }
    )


def _assert_pure_json(payload: dict) -> str:
    """allow_nan=False 的 dumps 能同时挡住 NaN/Inf 与不可序列化对象。"""
    return json.dumps(payload, ensure_ascii=False, allow_nan=False)


# ── 1. option 生成器 ──────────────────────────────────────────────────


@pytest.mark.parametrize(
    ("chart_type", "kwargs", "expected_series"),
    [
        ("scatter", {"x_col": "温度", "y_cols": ["降解率"]}, {"scatter", "line"}),
        ("bar", {"x_col": "材料", "y_cols": ["降解率"]}, {"bar"}),
        ("bar", {"x_col": "材料", "y_cols": ["降解率"], "group_by": "批次"}, {"bar"}),
        ("barh", {"x_col": "材料", "y_cols": ["降解率"]}, {"bar"}),
        ("line", {"x_col": "日期", "y_cols": ["降解率"]}, {"line"}),
        ("box", {"x_col": "材料", "y_cols": ["降解率"]}, {"boxplot", "scatter"}),
        ("histogram", {"y_cols": ["降解率"]}, {"bar"}),
        ("heatmap", {}, {"heatmap"}),
        ("bubble", {"x_col": "温度", "y_cols": ["降解率", "速率"]}, {"scatter"}),
        ("radar", {"x_col": "材料", "y_cols": ["温度", "降解率", "速率"]}, {"radar"}),
    ],
)
def test_chart_types_produce_expected_series(sample_df, chart_type, kwargs, expected_series):
    payload = build_echarts_payload(sample_df, chart_type, title="测试", **kwargs)
    _assert_pure_json(payload)
    assert payload["version"] == gen.PAYLOAD_VERSION
    assert payload["chart_type"] == chart_type
    series_types = {s["type"] for s in payload["option"]["series"]}
    assert series_types <= expected_series
    assert series_types & expected_series
    assert payload["meta"]["rows"] == len(sample_df)


def test_auto_detection_follows_reference_rules(sample_df):
    assert (
        build_echarts_payload(sample_df, "auto", x_col="温度", y_cols=["降解率"])["chart_type"]
        == "scatter"
    )
    assert (
        build_echarts_payload(sample_df, "auto", x_col="材料", y_cols=["降解率"])["chart_type"]
        == "bar"
    )


def test_violin_renders_as_boxplot(sample_df):
    payload = build_echarts_payload(sample_df, "violin", x_col="材料", y_cols=["降解率"])
    assert payload["chart_type"] == "violin"
    assert payload["meta"]["rendered_as"] == "boxplot"
    assert payload["option"]["series"][0]["type"] == "boxplot"
    # 五数概括：low ≤ Q1 ≤ median ≤ Q3 ≤ high
    for box in payload["option"]["series"][0]["data"]:
        assert box == sorted(box)


def test_bar_carries_std_and_drilldown(sample_df):
    payload = build_echarts_payload(sample_df, "bar", x_col="材料", y_cols=["降解率"])
    for item in payload["option"]["series"][0]["data"]:
        assert set(item) == {"value", "std"}
    drill = payload["drilldown"]
    assert drill["columns"] == ["材料", "降解率"]
    assert 0 < len(drill["rows"]) <= gen.MAX_DRILLDOWN_ROWS
    assert all(len(row) == 2 for row in drill["rows"])


def test_histogram_counts_cover_all_values(sample_df):
    payload = build_echarts_payload(sample_df, "histogram", y_cols=["降解率"])
    counts = payload["option"]["series"][0]["data"]
    assert sum(counts) == sample_df["降解率"].notna().sum()
    assert "均值" in payload["option"]["title"]["subtext"]


def test_scatter_includes_trend_and_correlation():
    df = pd.DataFrame({"x": range(30), "y": [2 * i + 1 for i in range(30)]})
    payload = build_echarts_payload(df, "scatter", x_col="x", y_cols=["y"])
    trend = [s for s in payload["option"]["series"] if s["type"] == "line"]
    assert len(trend) == 1
    assert "Pearson r = 1.000" in payload["option"]["title"]["subtext"]


def test_line_time_axis_uses_iso_timestamps(sample_df):
    payload = build_echarts_payload(sample_df, "line", x_col="日期", y_cols=["降解率"])
    assert payload["option"]["xAxis"]["type"] == "time"
    first_point = payload["option"]["series"][0]["data"][0]
    assert first_point[0].startswith("2025-01-01")


def test_nan_values_are_serialized_as_null():
    df = pd.DataFrame(
        {"类别": ["a", "b", "c"], "值": [1.0, float("nan"), 3.0]}
    )
    payload = build_echarts_payload(df, "bar", x_col="类别", y_cols=["值"])
    serialized = _assert_pure_json(payload)
    assert "NaN" not in serialized


def test_bubble_symbol_sizes_are_numeric(sample_df):
    payload = build_echarts_payload(
        sample_df, "bubble", x_col="温度", y_cols=["降解率", "速率"]
    )
    for item in payload["option"]["series"][0]["data"]:
        assert isinstance(item["symbolSize"], (int, float))
        assert 10 <= item["symbolSize"] <= 52


def test_radar_requires_three_numeric_dimensions(sample_df):
    with pytest.raises(ValueError):
        build_echarts_payload(sample_df, "radar", x_col="材料", y_cols=["降解率"])


def test_payload_cap_drops_drilldown_then_raises(sample_df, monkeypatch):
    base = build_echarts_payload(sample_df, "bar", x_col="材料", y_cols=["降解率"])
    assert "drilldown" in base
    base_len = len(json.dumps(base, ensure_ascii=False))

    # 上限压到「无 drilldown 恰好放得下」：drilldown 被丢弃
    monkeypatch.setattr(gen, "MAX_PAYLOAD_CHARS", base_len - 1)
    slim = build_echarts_payload(sample_df, "bar", x_col="材料", y_cols=["降解率"])
    assert "drilldown" not in slim

    # 上限压到连 option 都放不下：直接报错，工具端回落纯 PNG
    monkeypatch.setattr(gen, "MAX_PAYLOAD_CHARS", 10)
    with pytest.raises(ValueError):
        build_echarts_payload(sample_df, "bar", x_col="材料", y_cols=["降解率"])


def test_empty_dataframe_rejected():
    with pytest.raises(ValueError):
        build_echarts_payload(pd.DataFrame(), "bar")


# ── 2. 存储层（本地降级路径）─────────────────────────────────────────


@pytest.fixture
def local_store(monkeypatch, tmp_path):
    """禁用 MinIO，把本地目录指到 tmp_path。"""

    async def _no_minio(*args, **kwargs):
        return False

    async def _no_fetch(*args, **kwargs):
        return None

    monkeypatch.setattr(chart_store, "_save_to_minio", _no_minio)
    monkeypatch.setattr(chart_store, "_fetch_from_minio", _no_fetch)
    monkeypatch.setattr(chart_store, "LOCAL_CHARTS_DIR", tmp_path)
    monkeypatch.setattr(
        chart_store.settings, "internal_api_secret", "test-secret", raising=False
    )
    return tmp_path


async def test_store_chart_roundtrips_png_and_option(local_store):
    option = {"version": 1, "chart_type": "bar", "option": {"series": []}}
    url = await chart_store.store_chart(
        b"png-bytes", user_id="user-1", chart_id="chart-1", option_json=option
    )
    assert url.startswith("/api/v1/charts/user-1/chart-1?")
    assert (local_store / "user-1" / "chart-1.png").exists()
    assert (local_store / "user-1" / "chart-1.json").exists()

    assert await chart_store.fetch_chart("user-1", "chart-1") == b"png-bytes"
    raw = await chart_store.fetch_chart_option("user-1", "chart-1")
    assert json.loads(raw) == option


async def test_store_chart_without_option_writes_no_json(local_store):
    await chart_store.store_chart(b"png", user_id="user-2", chart_id="chart-2")
    assert (local_store / "user-2" / "chart-2.png").exists()
    assert not (local_store / "user-2" / "chart-2.json").exists()
    assert await chart_store.fetch_chart_option("user-2", "chart-2") is None


async def test_store_chart_survives_unserializable_option(local_store):
    url = await chart_store.store_chart(
        b"png",
        user_id="user-3",
        chart_id="chart-3",
        option_json={"bad": float("nan")},
    )
    assert url.startswith("/api/v1/charts/user-3/chart-3?")
    assert (local_store / "user-3" / "chart-3.png").exists()
    assert not (local_store / "user-3" / "chart-3.json").exists()


# ── 3. 路由 ───────────────────────────────────────────────────────────


@pytest.fixture
def chart_client(local_store):
    from backend.api.routers.chart_images import router

    app = FastAPI()
    app.include_router(router, prefix="/api/v1")
    return TestClient(app)


def _signed_query(user_id: str, chart_id: str) -> dict[str, str]:
    url = chart_store.build_chart_url(user_id, chart_id)
    query = parse_qs(urlparse(url).query)
    return {"exp": query["exp"][0], "sig": query["sig"][0]}


async def test_router_serves_option_json(local_store, chart_client):
    option = {"version": 1, "chart_type": "line", "option": {"series": []}}
    await chart_store.store_chart(
        b"png", user_id="u1", chart_id="c1", option_json=option
    )
    params = {**_signed_query("u1", "c1"), "format": "option"}
    resp = chart_client.get("/api/v1/charts/u1/c1", params=params)
    assert resp.status_code == 200
    assert resp.headers["content-type"].startswith("application/json")
    assert resp.json() == option

    # 同一签名默认仍返回 PNG（导出链路不受影响）
    resp_png = chart_client.get("/api/v1/charts/u1/c1", params=_signed_query("u1", "c1"))
    assert resp_png.status_code == 200
    assert resp_png.headers["content-type"] == "image/png"
    assert resp_png.content == b"png"


async def test_router_option_404_when_only_png(local_store, chart_client):
    await chart_store.store_chart(b"png", user_id="u2", chart_id="c2")
    params = {**_signed_query("u2", "c2"), "format": "option"}
    resp = chart_client.get("/api/v1/charts/u2/c2", params=params)
    assert resp.status_code == 404


async def test_router_option_rejects_bad_signature(local_store, chart_client):
    await chart_store.store_chart(
        b"png", user_id="u3", chart_id="c3", option_json={"version": 1}
    )
    params = {**_signed_query("u3", "c3"), "format": "option", "sig": "0" * 64}
    params["sig"] = "0" * 64
    resp = chart_client.get("/api/v1/charts/u3/c3", params=params)
    assert resp.status_code == 403


# ── 4. DOCX 导出的图表本地化 ─────────────────────────────────────────


async def test_docx_export_materializes_chart_images(local_store, tmp_path):
    from backend.application.tools.providers.deep_research_report_tool import (
        _materialize_chart_images,
    )

    url = await chart_store.store_chart(
        b"png-bytes", user_id="u9", chart_id="c9", option_json={"version": 1}
    )
    markdown = f"# 报告\n\n分析如下：\n\n![降解率对比]({url})\n"
    work_dir = tmp_path / "pandoc"
    work_dir.mkdir()

    rewritten = await asyncio.to_thread(_materialize_chart_images, markdown, work_dir)

    local_png = work_dir / "chart-u9-c9.png"
    assert local_png.exists()
    assert local_png.read_bytes() == b"png-bytes"
    assert url not in rewritten
    assert f"![降解率对比]({local_png.as_posix()})" in rewritten


async def test_docx_export_keeps_reference_on_bad_signature(local_store, tmp_path):
    from backend.application.tools.providers.deep_research_report_tool import (
        _materialize_chart_images,
    )

    await chart_store.store_chart(b"png", user_id="u8", chart_id="c8")
    bad_url = "/api/v1/charts/u8/c8?exp=1&sig=" + "0" * 64
    markdown = f"![图]({bad_url})"

    rewritten = await asyncio.to_thread(_materialize_chart_images, markdown, tmp_path)

    assert rewritten == markdown
    assert not list(tmp_path.glob("chart-*.png"))
