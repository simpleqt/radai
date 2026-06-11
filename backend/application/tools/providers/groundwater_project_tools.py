"""Request-scoped groundwater project analysis tools."""

from __future__ import annotations

import json
import math
from typing import Any

from langchain_core.tools import tool

from backend.application.groundwater import GroundwaterProjectService
from backend.application.tools.providers.project_table_tools import _execute, _records
from backend.domain.tools.runtime_context import ToolRuntimeContext


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
            if resource in (context.manifest.get("datasets") or {}):
                raise ValueError(
                    "结构化表格资源必须使用 inspect_project_tables/query_project_tables"
                )
            return _json(service.read_resource(context, resource, limit=limit))
        except Exception as exc:
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
            points = _execute(
                context,
                "SELECT * FROM points WHERE coordinate_profile IS NOT NULL ",
                1000,
            )
            points = _records(points)
            candidates: list[dict[str, Any]] = []
            for row in points:
                if str(row.get("coordinate_profile") or "") != coordinate_profile:
                    continue
                x_value = row.get("x_coord")
                y_value = row.get("y_coord")
                if x_value in (None, "") or y_value in (None, ""):
                    continue
                distance = math.hypot(float(x_value) - target_x, float(y_value) - target_y)
                if distance <= radius_m:
                    candidates.append({**row, "distance_m": round(distance, 2)})
            candidates.sort(key=lambda item: item["distance_m"])
            selected = candidates[: max(1, min(neighbor_count, 20))]

            wells = _records(_execute(context, "SELECT * FROM constructed_wells", 1000))
            geotechnical = _records(_execute(
                context, "SELECT * FROM geotechnical_boreholes", 1000
            ))
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
            return _json({"error": str(exc)})

    @tool("groundwater_quality_check")
    def groundwater_quality_check() -> str:
        """读取当前项目的数据质量问题和坐标确认状态。"""
        try:
            _, context = _project_context(runtime_context)
            issues = _records(_execute(context, "SELECT * FROM quality_issues", 1000))
            return _json(
                {
                    "project_id": context.project_id,
                    "data_version": context.data_version,
                    "quality_status": context.manifest.get("quality_status"),
                    "coordinate_reference": context.manifest.get("coordinate_reference"),
                    "issues": issues,
                    "standard_kb_required": True,
                }
            )
        except Exception as exc:
            return _json({"error": str(exc)})

    return [
        groundwater_project_skill_read,
        groundwater_prediction,
        groundwater_quality_check,
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


def get_groundwater_prediction_tool(
    *, runtime_context: ToolRuntimeContext | None = None
):
    return _get_tool("groundwater_prediction", runtime_context)


def get_groundwater_quality_check_tool(
    *, runtime_context: ToolRuntimeContext | None = None
):
    return _get_tool("groundwater_quality_check", runtime_context)
