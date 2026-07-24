"""Request-scoped groundwater project analysis tools."""

from __future__ import annotations

import json
import math
from collections import Counter
from typing import Any

from langchain_core.tools import tool

from backend.application.groundwater import GroundwaterProjectService
from backend.application.tools.providers.project_table_tools import (
    _execute,
    _records,
    _table_paths,
)
from backend.application.tools.providers.excel_engine import get_named_tables_schema
from backend.config.loggers import get_logger
from backend.domain.tools.runtime_context import ToolRuntimeContext
from backend.core.tools.tool_arguments import clean_malformed_tool_text


logger = get_logger(__name__)


def _json(value: Any) -> str:
    return json.dumps(value, ensure_ascii=False, indent=2)


def _project_context(runtime_context: ToolRuntimeContext | None):
    if runtime_context is None:
        raise ValueError("缺少工具运行时上下文")
    project_id = str(runtime_context.domain_context.get("project_id") or "").strip()
    if not project_id:
        raise ValueError("地下水项目上下文未经过 AgentService 解析")
    service = GroundwaterProjectService()
    return service, service.resolve(runtime_context.user_id, project_id)


_PROJECT_TABLE_ROUTES = {
    "监测点、监测井、坐标或状态": "points（监测点须筛选 point_type = 'monitoring_point'）",
    "治理井、抽注井或井深": "constructed_wells",
    "计划井或已建/计划比对": "planned_wells、planned_constructed_comparison",
    "地勘孔、钻孔或岩土参数": "geotechnical_boreholes",
    "地层、岩性、层厚": "geology_layers",
    "筛管、滤料、封闭段": "well_components",
    "水样、污染物浓度、超标": "water_samples、water_quality_measurements",
    "水位": "water_level_measurements",
    "KML 地物或空间分组": "先 groundwater_kml_summary，再按需 kml_features、kml_vertices",
}

_GROUNDWATER_IV_LIMITS: dict[str, dict[str, Any]] = {
    # GB/T 14848-2017 地下水质量标准，IV 类常用指标限值。
    # Keep units aligned with project rows where possible; conversion is still
    # handled below for mixed mg/L and ug/L measurements.
    "aluminum": {"limit": 500.0, "unit": "ug/L", "standard_name": "铝"},
    "ammonia_nitrogen": {"limit": 1.5, "unit": "mg/L", "standard_name": "氨氮"},
    "arsenic": {"limit": 50.0, "unit": "ug/L", "standard_name": "砷"},
    "barium": {"limit": 4000.0, "unit": "ug/L", "standard_name": "钡"},
    "cadmium": {"limit": 10.0, "unit": "ug/L", "standard_name": "镉"},
    "chloride": {"limit": 350.0, "unit": "mg/L", "standard_name": "氯化物"},
    "fluoride": {"limit": 2.0, "unit": "mg/L", "standard_name": "氟化物"},
    "iodide": {"limit": 0.5, "unit": "mg/L", "standard_name": "碘化物"},
    "iron": {"limit": 2.0, "unit": "mg/L", "standard_name": "铁"},
    "lead": {"limit": 100.0, "unit": "ug/L", "standard_name": "铅"},
    "manganese": {"limit": 1.5, "unit": "mg/L", "standard_name": "锰"},
    "mercury": {"limit": 2.0, "unit": "ug/L", "standard_name": "汞"},
    "nickel": {"limit": 100.0, "unit": "ug/L", "standard_name": "镍"},
    "nitrate_nitrogen": {"limit": 30.0, "unit": "mg/L", "standard_name": "硝酸盐氮"},
    "nitrite_nitrogen": {"limit": 4.8, "unit": "mg/L", "standard_name": "亚硝酸盐氮"},
    "sodium": {"limit": 400.0, "unit": "mg/L", "standard_name": "钠"},
    "sulfate": {"limit": 350.0, "unit": "mg/L", "standard_name": "硫酸盐"},
    "sulfide": {"limit": 0.1, "unit": "mg/L", "standard_name": "硫化物"},
    "volatile_phenols": {"limit": 0.01, "unit": "mg/L", "standard_name": "挥发性酚类"},
}


def _sql_literal(value: str) -> str:
    return "'" + str(value).replace("'", "''") + "'"


def _normalize_standard_class(value: str) -> str:
    normalized = str(value or "IV").strip().upper()
    normalized = normalized.replace("类", "").replace("CLASS", "").strip()
    normalized = normalized.replace("Ⅳ", "IV").replace("四", "IV").replace("4", "IV")
    return normalized or "IV"


def _convert_concentration(
    value: float,
    from_unit: str,
    to_unit: str,
) -> float | None:
    source = str(from_unit or "").strip().lower().replace("μ", "u")
    target = str(to_unit or "").strip().lower().replace("μ", "u")
    if source == target:
        return value
    if source == "mg/l" and target == "ug/l":
        return value * 1000.0
    if source == "ug/l" and target == "mg/l":
        return value / 1000.0
    return None


def _round_metric(value: float | None) -> float | None:
    if value is None:
        return None
    if abs(value) >= 100:
        return round(value, 2)
    if abs(value) >= 1:
        return round(value, 4)
    return round(value, 6)


def _groundwater_iv_quality_payload(
    context,
    point_code: str,
    *,
    include_all_comparisons: bool = False,
) -> dict[str, Any]:
    point = point_code.strip().upper()
    if not point:
        raise ValueError("point_code 不能为空")

    literal = _sql_literal(point)
    measurements = _records(
        _execute(
            context,
            (
                "SELECT m.point_code, m.sample_id, m.sample_layer, "
                "m.parameter_code, m.parameter_name, m.value_numeric, m.unit, "
                "s.sample_id AS sample_record_id "
                "FROM water_quality_measurements m "
                "LEFT JOIN water_samples s ON s.sample_id = m.sample_id "
                f"WHERE upper(m.point_code) = {literal} "
                "ORDER BY m.sample_layer, m.parameter_code"
            ),
            1000,
        )
    )
    layer_counts = _records(
        _execute(
            context,
            (
                "SELECT m.sample_layer, COUNT(*) AS measurement_count, "
                "COUNT(DISTINCT s.sample_id) AS sample_count "
                "FROM water_quality_measurements m "
                "LEFT JOIN water_samples s ON s.sample_id = m.sample_id "
                f"WHERE upper(m.point_code) = {literal} "
                "GROUP BY m.sample_layer ORDER BY m.sample_layer"
            ),
            100,
        )
    )

    expected_layers = ["upper", "middle", "lower"]
    seen_layers = {str(row.get("sample_layer") or "") for row in layer_counts}
    comparisons: list[dict[str, Any]] = []
    exceedances: list[dict[str, Any]] = []
    no_threshold: list[dict[str, Any]] = []
    missing_values: list[dict[str, Any]] = []

    for row in measurements:
        parameter_code = str(row.get("parameter_code") or "").strip()
        parameter_name = str(row.get("parameter_name") or parameter_code).strip()
        layer = str(row.get("sample_layer") or "").strip()
        value_raw = row.get("value_numeric")
        limit = _GROUNDWATER_IV_LIMITS.get(parameter_code)
        base = {
            "point_code": row.get("point_code") or point,
            "sample_layer": layer,
            "sample_id": row.get("sample_id"),
            "parameter_code": parameter_code,
            "parameter_name": parameter_name,
            "measured_value": value_raw,
            "measured_unit": row.get("unit"),
        }
        if value_raw in (None, ""):
            missing_values.append(base)
            continue
        if limit is None:
            no_threshold.append({**base, "reason": "standard_iv_limit_not_configured"})
            continue
        converted = _convert_concentration(
            float(value_raw),
            str(row.get("unit") or ""),
            str(limit["unit"]),
        )
        if converted is None:
            no_threshold.append(
                {
                    **base,
                    "reason": "unit_conversion_not_supported",
                    "standard_unit": limit["unit"],
                }
            )
            continue
        standard_limit = float(limit["limit"])
        ratio = converted / standard_limit if standard_limit else None
        exceed_amount = converted - standard_limit
        comparison = {
            **base,
            "value_for_comparison": _round_metric(converted),
            "comparison_unit": limit["unit"],
            "standard_limit": standard_limit,
            "standard_unit": limit["unit"],
            "standard_parameter_name": limit["standard_name"],
            "is_exceedance": converted > standard_limit,
            "ratio_to_limit": _round_metric(ratio),
            "exceed_amount": _round_metric(exceed_amount if converted > standard_limit else 0.0),
            "exceed_amount_unit": limit["unit"],
        }
        comparisons.append(comparison)
        if comparison["is_exceedance"]:
            exceedances.append(comparison)

    exceedances.sort(
        key=lambda item: (
            str(item.get("sample_layer") or ""),
            str(item.get("parameter_code") or ""),
        )
    )
    compact_missing_values = [
        {
            "sample_layer": row.get("sample_layer"),
            "parameter_code": row.get("parameter_code"),
            "parameter_name": row.get("parameter_name"),
        }
        for row in missing_values[:20]
    ]
    unchecked_by_parameter: dict[str, dict[str, Any]] = {}
    for row in no_threshold:
        key = str(row.get("parameter_code") or row.get("parameter_name") or "")
        if not key:
            continue
        item = unchecked_by_parameter.setdefault(
            key,
            {
                "parameter_code": row.get("parameter_code"),
                "parameter_name": row.get("parameter_name"),
                "reason": row.get("reason"),
                "layers": [],
            },
        )
        layer = row.get("sample_layer")
        if layer not in item["layers"]:
            item["layers"].append(layer)

    payload = {
        "project_id": context.project_id,
        "data_version": context.data_version,
        "point_code": point,
        "standard_class": "IV",
        "standard_reference": "GB/T 14848-2017《地下水质量标准》IV 类限值（内置常用指标映射；正式报告仍可用标准知识库核验来源文本）",
        "layer_completeness": {
            "expected_layers": expected_layers,
            "present_layers": sorted(seen_layers),
            "missing_layers": [layer for layer in expected_layers if layer not in seen_layers],
            "counts": layer_counts,
        },
        "measurement_count": len(measurements),
        "checked_parameter_count": len(comparisons),
        "exceedance_count": len(exceedances),
        "exceedances": exceedances,
        "non_exceedance_count": len([row for row in comparisons if not row.get("is_exceedance")]),
        "missing_value_count": len(missing_values),
        "missing_values_preview": compact_missing_values,
        "parameters_without_configured_iv_limit": list(unchecked_by_parameter.values()),
        "query_strategy": (
            "single deterministic project-data check: joined water_quality_measurements "
            "with water_samples and compared configured IV limits"
        ),
    }
    if include_all_comparisons:
        payload["comparisons"] = comparisons
    return payload


def _bootstrap_payload(context) -> dict[str, Any]:
    """Return the compact, durable project contract used before SQL generation."""
    schema = get_named_tables_schema(
        _table_paths(context),
        authorized_roots=[context.project_root],
    )
    tables = {
        name: {
            "row_count": int(columns[0].get("total") or 0) if columns else 0,
            "columns": [str(column.get("name")) for column in columns],
        }
        for name, columns in sorted(schema.items())
    }
    quality = context.manifest.get("quality_status") or {}
    if not isinstance(quality, dict):
        quality = {"status": quality}
    return {
        "project_id": context.project_id,
        "project_name": context.project_name,
        "data_version": context.data_version,
        "engine": "duckdb",
        "tables": tables,
        "table_routes": _PROJECT_TABLE_ROUTES,
        "quality_status": quality,
        "coordinate_reference": context.manifest.get("coordinate_reference"),
        "on_demand_resources": {
            "background": "仅在工程背景、水文地质条件或项目依据需要时读取",
            "data_quality": "仅在缺测、坐标、单位或质量限制需要展开时读取",
        },
        "workflow": [
            "已加载通用 Skill 后，读取项目 Skill（resource='skill'）。",
            "调用本工具一次，按 table_routes 选择真实表。",
            "只执行与问题直接相关的 SELECT/WITH；表名或字段不确定时只补一次 inspect_project_tables。",
            "有足够项目证据后立即生成结论，不再试探通用表名。",
        ],
    }


def get_groundwater_project_tools(
    *,
    runtime_context: ToolRuntimeContext | None = None,
):
    @tool("groundwater_project_skill_read")
    def groundwater_project_skill_read(
        resource: str = "manifest",
        limit: int = 100,
    ) -> str:
        """读取当前地下水项目的 Skill、清单、背景或标准化数据集。"""
        try:
            service, context = _project_context(runtime_context)
            resource = clean_malformed_tool_text(resource)
            if resource in (context.manifest.get("datasets") or {}):
                raise ValueError(
                    "结构化表格资源必须使用 inspect_project_tables/query_project_tables"
                )
            return _json(service.read_resource(context, resource, limit=limit))
        except Exception as exc:
            logger.exception("[GroundwaterProjectTool] 读取项目资源失败")
            return _json({"error": str(exc)})

    @tool("groundwater_project_bootstrap")
    def groundwater_project_bootstrap() -> str:
        """加载项目 SQL 合同：真实表、字段、质量摘要和高频选表路径。"""
        try:
            _, context = _project_context(runtime_context)
            return _json(_bootstrap_payload(context))
        except Exception as exc:
            logger.exception("[GroundwaterProjectTool] 加载项目合同失败")
            return _json({"error": str(exc)})

    @tool("groundwater_prediction")
    def groundwater_prediction(
        target_x: float,
        target_y: float,
        coordinate_profile: str,
        radius_m: float = 1000.0,
        neighbor_count: int = 5,
    ) -> str:
        """基于同一坐标组的邻近已建井和地勘孔做可解释的新井初步预测。"""
        try:
            _, context = _project_context(runtime_context)
            numeric_values = (float(target_x), float(target_y), float(radius_m))
            if not all(math.isfinite(value) for value in numeric_values):
                raise ValueError("目标坐标和搜索半径必须是有限数值")
            if radius_m <= 0:
                raise ValueError("搜索半径必须大于 0")
            bounded_neighbors = max(1, min(int(neighbor_count), 20))
            profile_literal = _sql_literal(coordinate_profile)
            distance_sql = (
                f"sqrt(power(x_coord - {float(target_x)}, 2) + "
                f"power(y_coord - {float(target_y)}, 2))"
            )
            selected = _records(
                _execute(
                    context,
                    (
                        "SELECT point_code, x_coord, y_coord, coordinate_profile, "
                        f"round({distance_sql}, 2) AS distance_m "
                        "FROM points "
                        f"WHERE coordinate_profile = {profile_literal} "
                        "AND x_coord IS NOT NULL AND y_coord IS NOT NULL "
                        f"AND {distance_sql} <= {float(radius_m)} "
                        f"ORDER BY {distance_sql} ASC LIMIT {bounded_neighbors}"
                    ),
                    bounded_neighbors,
                )
            )

            point_codes = [
                str(row.get("point_code"))
                for row in selected
                if row.get("point_code") not in (None, "")
            ]
            if point_codes:
                code_list = ", ".join(_sql_literal(code) for code in point_codes)
                wells = _records(
                    _execute(
                        context,
                        f"SELECT * FROM constructed_wells WHERE point_code IN ({code_list})",
                        bounded_neighbors,
                    )
                )
                geotechnical = _records(
                    _execute(
                        context,
                        (
                            "SELECT * FROM geotechnical_boreholes "
                            f"WHERE point_code IN ({code_list})"
                        ),
                        bounded_neighbors,
                    )
                )
            else:
                wells = []
                geotechnical = []
            details = {
                str(row.get("point_code")): row
                for row in wells + geotechnical
            }
            evidence = [
                {**row, "detail": details.get(str(row.get("point_code")), {})}
                for row in selected
            ]
            depths = [
                float(item["detail"]["actual_depth_m"])
                for item in evidence
                if item["detail"].get("actual_depth_m") not in (None, "")
            ]
            water_depths = [
                float(item["detail"]["initial_water_depth_m"])
                for item in evidence
                if item["detail"].get("initial_water_depth_m") not in (None, "")
            ]
            prediction = {
                "suggested_depth_range_m": (
                    [round(min(depths), 2), round(max(depths), 2)] if depths else None
                ),
                "observed_initial_water_depth_range_m": (
                    [round(min(water_depths), 2), round(max(water_depths), 2)]
                    if water_depths
                    else None
                ),
            }
            confidence = "medium" if len(evidence) >= 3 else "low"
            return _json(
                {
                    "project_id": context.project_id,
                    "data_version": context.data_version,
                    "target": {
                        "x": target_x,
                        "y": target_y,
                        "coordinate_profile": coordinate_profile,
                        "radius_m": radius_m,
                    },
                    "prediction": prediction,
                    "evidence_points": evidence,
                    "confidence": confidence,
                    "uncertainties": [
                        "结果仅使用同一 coordinate_profile 内的欧氏距离。",
                        "坐标轴语义和投影分带未确认前，不得用于正式工程放样。",
                        "该结果是邻近井范围统计，不是水文地质数值模型结论。",
                    ],
                }
            )
        except Exception as exc:
            logger.exception("[GroundwaterProjectTool] 地下水预测失败")
            return _json({"error": str(exc)})

    @tool("groundwater_quality_check")
    def groundwater_quality_check(
        point_code: str = "",
        standard_class: str = "IV",
        include_all_comparisons: bool = False,
    ) -> str:
        """检查数据质量；传入 point_code 时按地下水 IV 类限值快速判断点位超标。"""
        try:
            _, context = _project_context(runtime_context)
            point_code = clean_malformed_tool_text(point_code).strip()
            normalized_class = _normalize_standard_class(standard_class)
            if point_code:
                if normalized_class != "IV":
                    return _json(
                        {
                            "error": "unsupported_standard_class",
                            "requested_standard_class": standard_class,
                            "supported_standard_classes": ["IV"],
                            "instruction": "当前快速判断仅内置 GB/T 14848-2017 IV 类常用限值；其它类别请使用标准知识库和 query_project_tables 手工核验。",
                        }
                    )
                payload = _groundwater_iv_quality_payload(
                    context,
                    point_code,
                    include_all_comparisons=include_all_comparisons,
                )
                return _json(payload)
            issues = _records(_execute(context, "SELECT * FROM quality_issues", 1000))
            return _json(
                {
                    "project_id": context.project_id,
                    "data_version": context.data_version,
                    "quality_status": context.manifest.get("quality_status"),
                    "coordinate_reference": context.manifest.get("coordinate_reference"),
                    "issues": issues,
                    "standard_kb_required": True,
                    "point_quality_check_usage": {
                        "tool": "groundwater_quality_check",
                        "example_args": {"point_code": "GW18", "standard_class": "IV"},
                    },
                }
            )
        except Exception as exc:
            logger.exception("[GroundwaterProjectTool] 地下水质量检查失败")
            return _json({"error": str(exc)})

    @tool("groundwater_kml_summary")
    def groundwater_kml_summary(
        feature_name: str = "",
        category: str = "",
        max_features: int = 200,
    ) -> str:
        """汇总当前项目 KML 地物；可按地物名称或分组筛选点、线和坐标范围。"""
        try:
            _, context = _project_context(runtime_context)
            datasets = context.manifest.get("datasets") or {}
            if "kml_features" not in datasets:
                return _json(
                    {
                        "project_id": context.project_id,
                        "data_version": context.data_version,
                        "available": False,
                        "message": "当前项目包未包含 kml_features 数据集",
                    }
                )
            features = _records(_execute(context, "SELECT * FROM kml_features", 1000))
            normalized_name = feature_name.strip().casefold()
            normalized_category = category.strip().casefold()
            selected = [
                item
                for item in features
                if (
                    not normalized_name
                    or normalized_name in str(item.get("feature_name") or "").casefold()
                )
                and (
                    not normalized_category
                    or normalized_category in str(item.get("category") or "").casefold()
                )
            ]
            named_counts = Counter(
                str(item.get("feature_name") or "").strip()
                for item in features
                if str(item.get("feature_name") or "").strip()
            )
            duplicate_names = {
                name: count for name, count in named_counts.items() if count > 1
            }
            return _json(
                {
                    "project_id": context.project_id,
                    "data_version": context.data_version,
                    "available": True,
                    "filters": {
                        "feature_name": feature_name or None,
                        "category": category or None,
                    },
                    "summary": {
                        "total_features": len(features),
                        "matched_features": len(selected),
                        "named_features": sum(
                            bool(str(item.get("feature_name") or "").strip())
                            for item in features
                        ),
                        "unnamed_features": sum(
                            not bool(str(item.get("feature_name") or "").strip())
                            for item in features
                        ),
                        "geometry_counts": dict(
                            Counter(str(item.get("geometry_type") or "unknown") for item in features)
                        ),
                        "category_counts": dict(
                            Counter(str(item.get("category") or "未分组") for item in features)
                        ),
                        "duplicate_names": duplicate_names,
                        "bounds": (
                            context.manifest.get("spatial_data") or {}
                        ).get("bounds"),
                    },
                    "features": selected[: max(1, min(max_features, 500))],
                    "truncated": len(selected) > max(1, min(max_features, 500)),
                    "coordinate_warning": (
                        "KML 经纬度与原项目平面坐标属于不同坐标表达；"
                        "转换参数未确认前不得直接混算距离或用于工程放样。"
                    ),
                }
            )
        except Exception as exc:
            logger.exception("[GroundwaterProjectTool] KML 汇总失败")
            return _json({"error": str(exc)})

    return [
        groundwater_project_skill_read,
        groundwater_project_bootstrap,
        groundwater_prediction,
        groundwater_quality_check,
        groundwater_kml_summary,
    ]


def _get_tool(name: str, runtime_context: ToolRuntimeContext | None):
    return next(
        item
        for item in get_groundwater_project_tools(runtime_context=runtime_context)
        if item.name == name
    )


def get_groundwater_project_skill_read_tool(
    *, runtime_context: ToolRuntimeContext | None = None
):
    return _get_tool("groundwater_project_skill_read", runtime_context)


def get_groundwater_project_bootstrap_tool(
    *, runtime_context: ToolRuntimeContext | None = None
):
    return _get_tool("groundwater_project_bootstrap", runtime_context)


def get_groundwater_prediction_tool(
    *, runtime_context: ToolRuntimeContext | None = None
):
    return _get_tool("groundwater_prediction", runtime_context)


def get_groundwater_quality_check_tool(
    *, runtime_context: ToolRuntimeContext | None = None
):
    return _get_tool("groundwater_quality_check", runtime_context)


def get_groundwater_kml_summary_tool(
    *, runtime_context: ToolRuntimeContext | None = None
):
    return _get_tool("groundwater_kml_summary", runtime_context)
