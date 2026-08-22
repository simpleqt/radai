import json

from backend.core.agents.context.structured_result import compact_structured_result


def _geology_rows() -> list[dict]:
    rows: list[dict] = []
    for point_code, bottoms in (
        ("JC01", [2.0, 6.0, 15.0]),
        ("JC02", [3.0, 10.0, 30.0]),
        ("JC03", [1.5, 8.0, 40.0]),
    ):
        top = 0.0
        for layer_no, bottom in enumerate(bottoms, start=1):
            rows.append(
                {
                    "point_code": point_code,
                    "layer_no": layer_no,
                    "top_depth_m": top,
                    "bottom_depth_m": bottom,
                    "thickness_m": bottom - top,
                    "stratum_name": ("素填土", "粉质黏土", "灰岩")[layer_no - 1],
                    "lithology": None if point_code == "JC03" and layer_no == 1 else "岩土",
                    "cave_flag": "yes" if point_code == "JC02" and layer_no == 3 else "no",
                }
            )
            top = bottom
    return rows


def test_compaction_profiles_full_result_not_only_preview_rows():
    rows = _geology_rows()
    rendered = compact_structured_result(
        {
            "project_id": "gw-test",
            "data_version": 4,
            "row_count": len(rows),
            "columns": list(rows[0]),
            "rows": rows,
            "query_kind": "data_query",
        },
        max_chars=10_000,
        head_rows=1,
        tail_rows=1,
    )

    payload = json.loads(rendered)
    statistics = payload["table_statistics"]
    profiles = {
        item["column"]: item for item in statistics["column_profiles"]
    }

    assert statistics["profiled_row_count"] == 9
    assert profiles["bottom_depth_m"]["numeric_range"] == {
        "min": 1.5,
        "max": 40,
    }
    assert profiles["lithology"]["null_count"] == 1
    assert statistics["categorical_values"]["stratum_name"]["values"] == [
        {"value": "灰岩", "count": 3},
        {"value": "粉质黏土", "count": 3},
        {"value": "素填土", "count": 3},
    ]

    coverage = statistics["interval_coverage"][0]
    assert coverage["top_column"] == "top_depth_m"
    assert coverage["bottom_column"] == "bottom_depth_m"
    assert coverage["valid_interval_count"] == 9
    assert coverage["overall_range"] == {
        "min_top": 0,
        "max_bottom": 40,
        "span": 40,
    }
    assert coverage["group_column"] == "point_code"
    assert coverage["group_count"] == 3
    assert coverage["group_max_bottom_range"] == {"min": 15, "max": 40}


def test_compaction_detects_multiple_generic_top_bottom_pairs():
    rows = [
        {
            "point_code": "D1",
            "screen_top_m": 5.0,
            "screen_bottom_m": 12.0,
            "filter_top_m": 4.0,
            "filter_bottom_m": 13.0,
        },
        {
            "point_code": "D2",
            "screen_top_m": 8.0,
            "screen_bottom_m": 20.0,
            "filter_top_m": None,
            "filter_bottom_m": None,
        },
    ]
    rendered = compact_structured_result(
        {"columns": list(rows[0]), "rows": rows, "row_count": 2},
        max_chars=10_000,
    )

    coverage = json.loads(rendered)["table_statistics"]["interval_coverage"]
    by_top = {item["top_column"]: item for item in coverage}
    assert by_top["screen_top_m"]["overall_range"]["max_bottom"] == 20
    assert by_top["filter_top_m"]["valid_interval_count"] == 1
    assert by_top["filter_top_m"]["null_pair_count"] == 1


def test_compaction_keeps_statistics_within_default_budget():
    rows = _geology_rows() * 14
    rendered = compact_structured_result(
        {
            "row_count": len(rows),
            "columns": list(rows[0]),
            "rows": rows,
        },
        max_chars=5_000,
    )

    assert len(rendered) <= 5_000
    payload = json.loads(rendered)
    assert payload["table_statistics"]["profiled_row_count"] == len(rows)
    assert payload["table_statistics"]["interval_coverage"][0][
        "overall_range"
    ]["max_bottom"] == 40


def test_extremely_wide_result_remains_valid_json_under_tiny_budget():
    rows = [
        {f"measurement_{column}": column + row for column in range(80)}
        for row in range(20)
    ]
    rendered = compact_structured_result(
        {"row_count": len(rows), "columns": list(rows[0]), "rows": rows},
        max_chars=600,
    )

    assert len(rendered) <= 600
    payload = json.loads(rendered)
    assert payload["row_count"] == 20
    assert payload["table_statistics"]["profiled_row_count"] == 20
