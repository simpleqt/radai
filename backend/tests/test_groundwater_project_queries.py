"""地下水项目表查询、质量检查与大结果处理契约（拆自 test_groundwater_project_runtime.py）。"""

import json
import os
import pytest

from backend.application.agent_executors.stream_handler import (
    NEAgentStreamHandler,
    _counts_toward_project_query_budget,
)
from backend.application.tools.assembler import PolicyToolAssembler
from backend.core.agents.middlewares.context_pruning import ContextPruningMiddleware
from backend.core.agents.middlewares.groundwater_project_workflow import (
    GroundwaterProjectWorkflowMiddleware,
)
from backend.domain.tools.runtime_context import ToolRuntimeContext
from langchain_core.messages import AIMessage, HumanMessage, ToolMessage
from pathlib import Path
from types import SimpleNamespace

_BUILTIN_DATASET_ROOT = Path(
    os.getenv("GROUNDWATER_BUILTIN_ROOT", "")
    or Path(__file__).resolve().parents[2] / "六塘项目数据信息" / "standardized"
)

requires_builtin_groundwater_dataset = pytest.mark.skipif(
    not (_BUILTIN_DATASET_ROOT / "manifest.json").is_file(),
    reason=(
        "requires the external Liutang groundwater dataset; set "
        "GROUNDWATER_BUILTIN_ROOT to run dataset integration tests"
    ),
)


def _runtime_context(project_id: str = "liutang-groundwater") -> ToolRuntimeContext:
    return ToolRuntimeContext(
        request_id="req-groundwater",
        user_id="user-groundwater",
        agent_code="groundwater",
        domain="groundwater-project-analysis",
        thread_id="user-groundwater:thread-groundwater",
        request_metadata={"groundwater_project_id": project_id},
        domain_context={"project_id": project_id},
    )


def test_only_successful_project_data_queries_consume_writer_budget():
    assert _counts_toward_project_query_budget(
        '{"query_kind":"data_query","counts_toward_query_budget":true}'
    )
    assert not _counts_toward_project_query_budget(
        '{"query_kind":"validation_error","counts_toward_query_budget":false}'
    )
    assert not _counts_toward_project_query_budget(
        '{"query_kind":"schema_catalog","counts_toward_query_budget":false}'
    )
    assert not _counts_toward_project_query_budget(
        ToolMessage(
            content='{"query_kind":"validation_error","counts_toward_query_budget":false}',
            name="query_project_tables",
            tool_call_id="call-invalid-query",
        )
    )


def test_rejected_project_sql_is_not_elevated_as_project_evidence():
    state = {
        "messages": [
            AIMessage(
                content="",
                tool_calls=[
                    {
                        "id": "call-invalid-query",
                        "name": "query_project_tables",
                        "args": {"sql": "SELECT * FROM project_info"},
                    }
                ],
            ),
            ToolMessage(
                content=(
                    '{"error":"unknown_project_table",'
                    '"counts_toward_query_budget":false}'
                ),
                name="query_project_tables",
                tool_call_id="call-invalid-query",
            ),
        ]
    }

    updates = ContextPruningMiddleware()._process_state_elevation(state)

    assert "context_records" not in updates
    assert "not project evidence" in updates["messages"][0].content


@requires_builtin_groundwater_dataset
def test_project_table_query_maps_common_groundwater_legacy_table_names(monkeypatch):
    monkeypatch.setenv("GROUNDWATER_CATALOG_DISABLED", "true")
    tool = PolicyToolAssembler().materialize_tool_name(
        "query_project_tables",
        runtime_context=_runtime_context(),
    )[0]

    expected_counts = {
        "well_construction": 31,
        "wells": 31,
        "well_locations": 198,
        "monitoring_wells": 15,
        "monitoring_points": 15,
        "boreholes": 24,
        "stratigraphy": 119,
        "contamination_assessment": 748,
    }
    for legacy_name, expected_count in expected_counts.items():
        result = json.loads(
            tool.invoke(
                {"sql": f"SELECT COUNT(*) AS total FROM {legacy_name}"}
            )
        )
        assert result["rows"][0]["total"] == expected_count
        assert legacy_name in result["table_aliases_applied"]

    layers = json.loads(
        tool.invoke(
            {
                "sql": (
                    "SELECT point_code, layer_order FROM stratigraphy "
                    "WHERE point_code = 'JC01' ORDER BY layer_order LIMIT 1"
                )
            }
        )
    )
    assert layers["rows"][0]["point_code"] == "JC01"
    assert layers["rows"][0]["layer_no"] == 1


@requires_builtin_groundwater_dataset
def test_project_table_catalog_query_returns_authorized_table_summary(monkeypatch):
    monkeypatch.setenv("GROUNDWATER_CATALOG_DISABLED", "true")
    tool = PolicyToolAssembler().materialize_tool_name(
        "query_project_tables",
        runtime_context=_runtime_context(),
    )[0]

    result = json.loads(
        tool.invoke({"sql": "SELECT table_name, row_count FROM duckdb_tables()"})
    )

    assert result["row_count"] == 13
    assert result["columns"] == ["table_name", "row_count", "column_count"]
    assert {
        (row["table_name"], row["row_count"])
        for row in result["rows"]
    } >= {
        ("points", 198),
        ("constructed_wells", 31),
        ("water_quality_measurements", 748),
    }
    assert "inspect_project_tables" in result["query_note"]
    assert result["counts_toward_query_budget"] is False


@requires_builtin_groundwater_dataset
def test_project_table_query_rejects_unknown_table_before_duckdb_execution(monkeypatch):
    monkeypatch.setenv("GROUNDWATER_CATALOG_DISABLED", "true")
    tool = PolicyToolAssembler().materialize_tool_name(
        "query_project_tables",
        runtime_context=_runtime_context(),
    )[0]

    result = json.loads(tool.invoke({"sql": "SELECT * FROM contamination_zones LIMIT 5"}))

    assert result["error"] == "unknown_project_table"
    assert result["unknown_tables"] == ["contamination_zones"]
    assert "points" in result["available_tables"]
    assert result["counts_toward_query_budget"] is False


@requires_builtin_groundwater_dataset
def test_project_table_query_returns_all_groundwater_layers(monkeypatch):
    monkeypatch.setenv("GROUNDWATER_CATALOG_DISABLED", "true")
    assembler = PolicyToolAssembler()
    tool = assembler.materialize_tool_name(
        "query_project_tables",
        runtime_context=_runtime_context(),
    )[0]

    result = json.loads(
        tool.invoke(
            {
                "sql": """
                    SELECT point_code, sample_layer, parameter_name, value_numeric, unit
                    FROM water_quality_measurements
                    WHERE point_code = 'GW18'
                    ORDER BY sample_layer, parameter_code
                """,
                "max_rows": 200,
            }
        )
    )

    measurements = result["rows"]
    assert len(measurements) == 66
    assert {row["point_code"] for row in measurements} == {"GW18"}
    assert {row["sample_layer"] for row in measurements} == {
        "upper",
        "middle",
        "lower",
    }
    assert any(row["parameter_name"] == "氯离子" for row in measurements)


@requires_builtin_groundwater_dataset
def test_project_table_query_supports_join_and_layer_completeness(monkeypatch):
    monkeypatch.setenv("GROUNDWATER_CATALOG_DISABLED", "true")
    tool = PolicyToolAssembler().materialize_tool_name(
        "query_project_tables",
        runtime_context=_runtime_context(),
    )[0]

    result = json.loads(
        tool.invoke(
            {
                "sql": """
                    SELECT m.sample_layer, COUNT(*) AS measurement_count,
                           COUNT(DISTINCT s.sample_id) AS sample_count
                    FROM water_quality_measurements m
                    JOIN water_samples s ON s.sample_id = m.sample_id
                    WHERE m.point_code = 'GW18'
                    GROUP BY m.sample_layer
                    ORDER BY m.sample_layer
                """
            }
        )
    )

    assert {row["sample_layer"]: row["measurement_count"] for row in result["rows"]} == {
        "lower": 22,
        "middle": 22,
        "upper": 22,
    }
    assert all(row["sample_count"] == 1 for row in result["rows"])


@requires_builtin_groundwater_dataset
def test_groundwater_quality_check_returns_point_iv_exceedances(monkeypatch):
    monkeypatch.setenv("GROUNDWATER_CATALOG_DISABLED", "true")
    tool = PolicyToolAssembler().materialize_tool_name(
        "groundwater_quality_check",
        runtime_context=_runtime_context(),
    )[0]

    result = json.loads(tool.invoke({"point_code": "GW18", "standard_class": "IV"}))

    assert result["point_code"] == "GW18"
    assert result["standard_class"] == "IV"
    assert result["measurement_count"] == 66
    assert result["layer_completeness"]["missing_layers"] == []
    exceedance_keys = {
        (row["sample_layer"], row["parameter_code"])
        for row in result["exceedances"]
    }
    assert ("upper", "arsenic") in exceedance_keys
    assert ("upper", "chloride") in exceedance_keys
    assert ("middle", "sulfate") in exceedance_keys
    assert ("lower", "sodium") in exceedance_keys
    upper_arsenic = next(
        row
        for row in result["exceedances"]
        if row["sample_layer"] == "upper" and row["parameter_code"] == "arsenic"
    )
    assert upper_arsenic["measured_value"] == 104.0
    assert upper_arsenic["standard_limit"] == 50.0
    assert upper_arsenic["exceed_amount"] == 54.0
    assert upper_arsenic["ratio_to_limit"] == 2.08


@requires_builtin_groundwater_dataset
def test_groundwater_quality_check_context_capture_keeps_exceedance_summary(monkeypatch):
    monkeypatch.setenv("GROUNDWATER_CATALOG_DISABLED", "true")
    # 断言 legacy 状态提升摘要;CONTEXT_OBJECT_MODE env 默认已是 on,显式钉 off。
    monkeypatch.setenv("CONTEXT_OBJECT_MODE", "off")
    tool = PolicyToolAssembler().materialize_tool_name(
        "groundwater_quality_check",
        runtime_context=_runtime_context(),
    )[0]
    payload = tool.invoke({"point_code": "GW18", "standard_class": "IV"})
    state = {
        "messages": [
            AIMessage(
                content="",
                tool_calls=[
                    {
                        "id": "call-quality",
                        "name": "groundwater_quality_check",
                        "args": {"point_code": "GW18", "standard_class": "IV"},
                    }
                ],
            ),
            ToolMessage(
                content=payload,
                name="groundwater_quality_check",
                tool_call_id="call-quality",
            ),
        ]
    }

    updates = ContextPruningMiddleware(char_threshold=100)._process_state_elevation(state)

    assert updates is not None
    record = updates["context_records"][0]
    assert record["source_tool"] == "groundwater_quality_check"
    assert "GROUNDWATER_QUALITY_CHECK_RESULT" in record["claim"]
    assert "point_code=GW18" in record["claim"]
    assert "砷" in record["claim"]
    assert "ratio_to_limit" in record["claim"]
    assert updates["messages"][0].content.startswith(
        "GROUNDWATER_QUALITY_CHECK_RESULT"
    )
    assert "Context Isolation" not in updates["messages"][0].content


@pytest.mark.asyncio
async def test_large_project_result_is_summarized_by_tool_free_child(monkeypatch):
    middleware = ContextPruningMiddleware()
    assert middleware.char_threshold == 3000

    calls = []

    async def fake_summarize(*, source_tool, content, user_query):
        calls.append((source_tool, len(content), user_query))
        return "查询结果摘要：GW18 共返回 66 条检测记录，关键数值和单位已保留。"

    monkeypatch.setattr(
        middleware._large_result_summarizer,
        "summarize",
        fake_summarize,
    )
    state = {
        "messages": [
            HumanMessage(content="总结 GW18 的检测结果"),
            AIMessage(
                content="",
                tool_calls=[
                    {
                        "id": "call-large-query",
                        "name": "query_project_tables",
                        "args": {"sql": "SELECT * FROM water_quality_measurements"},
                    }
                ],
            ),
            ToolMessage(
                content="x" * 3500,
                name="query_project_tables",
                tool_call_id="call-large-query",
            ),
        ]
    }

    updates = await middleware.abefore_model(state, None)

    assert calls == [
        ("query_project_tables", 3500, "总结 GW18 的检测结果")
    ]
    replacement = updates["messages"][0].content
    assert replacement.startswith("[Result Summarizer Handoff:")
    assert "did not perform another query" in replacement
    assert "GW18 共返回 66 条检测记录" in replacement
    record = updates["context_records"][0]
    assert record["claim"] == replacement
    assert record["category"] == "handoff"
    assert record["kind"] == "subagent_result"
    assert "subagent_summary" in record["tags"]


@pytest.mark.asyncio
async def test_large_result_child_has_distinct_process_event():
    handler = NEAgentStreamHandler()
    event = {
        "event": "on_chat_model_start",
        "tags": ["large-result-subagent"],
        "metadata": {
            "purpose": "large_result_summarization",
            "subagent_type": "result-summarizer",
        },
    }

    items = [item async for item in handler.handle(event)]

    assert items[0]["content"] == "正在压缩较长的工具结果，请稍候；关键来源信息会继续保留..."
    assert items[1]["event"]["tool"] == "large_result_summarization"
    assert items[1]["event"]["title"] == "正在压缩工具结果"
    assert "来源标识" in items[1]["event"]["detail"]


def test_groundwater_workflow_blocks_redundant_quality_sql_after_quality_check():
    state = {
        "context_records": [
            {
                "source_tool": "groundwater_quality_check",
                "claim": "GROUNDWATER_QUALITY_CHECK_RESULT\npoint_code=GW18; standard_class=IV",
            }
        ],
    }
    request = SimpleNamespace(
        tool_call={
            "id": "call-sql",
            "name": "query_project_tables",
            "args": {
                "sql": (
                    "SELECT * FROM water_quality_measurements "
                    "WHERE point_code = 'GW18'"
                )
            },
        },
        state=state,
    )

    blocked = GroundwaterProjectWorkflowMiddleware._blocked_message(request)

    assert blocked is not None
    assert blocked.status == "error"
    payload = json.loads(blocked.content)
    assert payload["error"] == "redundant_groundwater_quality_query"
    assert payload["point_code"] == "GW18"
    assert payload["counts_toward_query_budget"] is False


@requires_builtin_groundwater_dataset
def test_builtin_liutang_kml_summary_tool(monkeypatch):
    monkeypatch.setenv("GROUNDWATER_CATALOG_DISABLED", "true")
    tool = PolicyToolAssembler().materialize_tool_name(
        "groundwater_kml_summary",
        runtime_context=_runtime_context(),
    )[0]

    result = json.loads(tool.invoke({"category": "注入井"}))

    assert result["available"] is True
    assert result["summary"]["total_features"] == 141
    assert result["summary"]["geometry_counts"] == {"Point": 128, "LineString": 13}
    assert result["summary"]["matched_features"] == 60
    assert result["summary"]["duplicate_names"] == {"D30": 2, "D37": 2, "G06": 2}


@requires_builtin_groundwater_dataset
def test_project_table_query_rejects_external_file_access(monkeypatch):
    monkeypatch.setenv("GROUNDWATER_CATALOG_DISABLED", "true")
    tool = PolicyToolAssembler().materialize_tool_name(
        "query_project_tables",
        runtime_context=_runtime_context(),
    )[0]

    result = json.loads(tool.invoke({"sql": "SELECT * FROM read_csv('/tmp/x.csv')"}))

    assert "不允许" in result["error"]


@requires_builtin_groundwater_dataset
def test_project_table_query_accepts_common_model_sql_wrappers(monkeypatch):
    monkeypatch.setenv("GROUNDWATER_CATALOG_DISABLED", "true")
    tool = PolicyToolAssembler().materialize_tool_name(
        "query_project_tables",
        runtime_context=_runtime_context(),
    )[0]

    wrapped_queries = [
        "```sql\nSELECT COUNT(*) AS n FROM water_samples\n```",
        "SQL: SELECT COUNT(*) AS n FROM water_samples",
        "-- check sample count\nSELECT COUNT(*) AS n FROM water_samples",
    ]
    for sql in wrapped_queries:
        result = json.loads(tool.invoke({"sql": sql}))
        assert result["rows"] == [{"n": 34}]


@requires_builtin_groundwater_dataset
def test_project_table_query_still_rejects_explanatory_prose(monkeypatch):
    monkeypatch.setenv("GROUNDWATER_CATALOG_DISABLED", "true")
    tool = PolicyToolAssembler().materialize_tool_name(
        "query_project_tables",
        runtime_context=_runtime_context(),
    )[0]

    result = json.loads(
        tool.invoke({"sql": "请查询下面的数据：SELECT * FROM water_samples"})
    )

    assert "仅允许 SELECT 或 WITH" in result["error"]


@requires_builtin_groundwater_dataset
def test_groundwater_prediction_uses_only_matching_coordinate_profile(monkeypatch):
    monkeypatch.setenv("GROUNDWATER_CATALOG_DISABLED", "true")
    assembler = PolicyToolAssembler()
    tool = assembler.materialize_tool_name(
        "groundwater_prediction",
        runtime_context=_runtime_context(),
    )[0]

    result = json.loads(
        tool.invoke(
            {
                "target_x": 36597892.287,
                "target_y": 2710752.5148,
                "coordinate_profile": "liutang_3659_271",
                "radius_m": 500,
                "neighbor_count": 5,
            }
        )
    )

    assert result["project_id"] == "liutang-groundwater"
    assert result["evidence_points"]
    assert all(
        row["coordinate_profile"] == "liutang_3659_271"
        for row in result["evidence_points"]
    )
    assert result["prediction"]["suggested_depth_range_m"] is not None
