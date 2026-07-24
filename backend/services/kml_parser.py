"""Safe, dependency-free KML parsing for chat files and project packages."""

from __future__ import annotations

import html
import math
import re
import xml.etree.ElementTree as ET
from collections import Counter
from pathlib import Path
from typing import Any


GEOMETRY_TYPES = {"Point", "LineString", "Polygon", "LinearRing"}


def _local_name(tag: str) -> str:
    return tag.rsplit("}", 1)[-1]


def _child_text(element: ET.Element, name: str) -> str:
    for child in element:
        if _local_name(child.tag) == name:
            return (child.text or "").strip()
    return ""


def _coordinates(text: str) -> list[tuple[float, float, float | None]]:
    result: list[tuple[float, float, float | None]] = []
    for token in re.split(r"\s+", (text or "").strip()):
        if not token:
            continue
        parts = token.split(",")
        if len(parts) < 2:
            continue
        try:
            longitude = float(parts[0])
            latitude = float(parts[1])
            altitude = float(parts[2]) if len(parts) > 2 and parts[2] else None
        except ValueError:
            continue
        if not (math.isfinite(longitude) and math.isfinite(latitude)):
            continue
        if altitude is not None and not math.isfinite(altitude):
            altitude = None
        result.append((longitude, latitude, altitude))
    return result


def parse_kml(data: bytes, *, source_file: str = "") -> dict[str, Any]:
    """Parse KML placemarks into feature and vertex records.

    The parser intentionally ignores styles and remote links. It extracts only
    local XML metadata and coordinates, which keeps uploaded KML analysis
    deterministic and avoids network access.
    """

    try:
        root = ET.fromstring(data.lstrip(b"\xef\xbb\xbf"))
    except ET.ParseError as exc:
        raise ValueError(f"KML XML 格式无效: {exc}") from exc
    if _local_name(root.tag).lower() != "kml":
        raise ValueError("文件根元素不是 KML")

    parent_by_child = {child: parent for parent in root.iter() for child in parent}
    document_name = ""
    for element in root.iter():
        if _local_name(element.tag) == "Document":
            document_name = _child_text(element, "name")
            break

    features: list[dict[str, Any]] = []
    vertices: list[dict[str, Any]] = []
    for placemark_index, placemark in enumerate(
        (item for item in root.iter() if _local_name(item.tag) == "Placemark"),
        start=1,
    ):
        folder_names: list[str] = []
        ancestor = parent_by_child.get(placemark)
        while ancestor is not None:
            if _local_name(ancestor.tag) == "Folder":
                folder_name = _child_text(ancestor, "name")
                if folder_name:
                    folder_names.append(folder_name)
            ancestor = parent_by_child.get(ancestor)
        folder_names.reverse()

        name = _child_text(placemark, "name")
        description = _child_text(placemark, "description")
        coordinate_system = _child_text(placemark, "OvCoordType")
        feature_id = f"kml-feature-{placemark_index:04d}"
        feature_vertices: list[dict[str, Any]] = []
        geometry_types: list[str] = []
        geometry_index = 0

        for geometry in placemark.iter():
            geometry_type = _local_name(geometry.tag)
            if geometry_type not in GEOMETRY_TYPES or geometry_type == "LinearRing":
                continue
            coordinate_nodes = [
                item
                for item in geometry.iter()
                if _local_name(item.tag) == "coordinates"
            ]
            geometry_coordinates = [
                coordinate
                for node in coordinate_nodes
                for coordinate in _coordinates(node.text or "")
            ]
            if not geometry_coordinates:
                continue
            geometry_index += 1
            geometry_types.append(geometry_type)
            for vertex_index, (longitude, latitude, altitude) in enumerate(
                geometry_coordinates,
                start=1,
            ):
                vertex = {
                    "feature_id": feature_id,
                    "feature_name": name or None,
                    "folder_path": "/".join(folder_names),
                    "category": folder_names[-1] if folder_names else None,
                    "geometry_type": geometry_type,
                    "geometry_index": geometry_index,
                    "vertex_index": vertex_index,
                    "longitude": longitude,
                    "latitude": latitude,
                    "altitude_m": altitude,
                    "source_file": source_file or None,
                }
                feature_vertices.append(vertex)
                vertices.append(vertex)

        if not feature_vertices:
            continue
        longitudes = [item["longitude"] for item in feature_vertices]
        latitudes = [item["latitude"] for item in feature_vertices]
        altitude_values = [
            item["altitude_m"]
            for item in feature_vertices
            if item["altitude_m"] is not None
        ]
        geometry_type = "+".join(dict.fromkeys(geometry_types))
        features.append(
            {
                "feature_id": feature_id,
                "feature_name": name or None,
                "is_named": bool(name),
                "folder_path": "/".join(folder_names),
                "category": folder_names[-1] if folder_names else None,
                "geometry_type": geometry_type,
                "coordinate_count": len(feature_vertices),
                "longitude": longitudes[0] if geometry_type == "Point" else None,
                "latitude": latitudes[0] if geometry_type == "Point" else None,
                "altitude_m": (
                    altitude_values[0]
                    if geometry_type == "Point" and altitude_values
                    else None
                ),
                "centroid_longitude": sum(longitudes) / len(longitudes),
                "centroid_latitude": sum(latitudes) / len(latitudes),
                "min_longitude": min(longitudes),
                "max_longitude": max(longitudes),
                "min_latitude": min(latitudes),
                "max_latitude": max(latitudes),
                "coordinate_system": coordinate_system or None,
                "description": description or None,
                "source_file": source_file or None,
            }
        )

    if not features:
        raise ValueError("KML 中未找到带坐标的 Placemark")

    all_longitudes = [item["longitude"] for item in vertices]
    all_latitudes = [item["latitude"] for item in vertices]
    return {
        "document_name": document_name or Path(source_file).stem,
        "features": features,
        "vertices": vertices,
        "summary": {
            "feature_count": len(features),
            "named_feature_count": sum(bool(item["is_named"]) for item in features),
            "unnamed_feature_count": sum(not item["is_named"] for item in features),
            "geometry_counts": dict(Counter(item["geometry_type"] for item in features)),
            "category_counts": dict(Counter(item["category"] or "未分组" for item in features)),
            "vertex_count": len(vertices),
            "bounds": {
                "min_longitude": min(all_longitudes),
                "max_longitude": max(all_longitudes),
                "min_latitude": min(all_latitudes),
                "max_latitude": max(all_latitudes),
            },
        },
    }


def kml_to_analysis_text(data: bytes, *, source_file: str = "") -> str:
    """Render KML as compact semantic text suitable for chat indexing."""

    parsed = parse_kml(data, source_file=source_file)
    summary = parsed["summary"]
    bounds = summary["bounds"]
    lines = [
        f"# KML 地理数据：{parsed['document_name']}",
        "",
        f"- 来源文件：{source_file or '未命名 KML'}",
        f"- 地物数：{summary['feature_count']}",
        f"- 已命名地物：{summary['named_feature_count']}",
        f"- 未命名地物：{summary['unnamed_feature_count']}",
        f"- 几何类型统计：{summary['geometry_counts']}",
        f"- 分组统计：{summary['category_counts']}",
        (
            "- 坐标范围："
            f"经度 {bounds['min_longitude']:.8f} 至 {bounds['max_longitude']:.8f}；"
            f"纬度 {bounds['min_latitude']:.8f} 至 {bounds['max_latitude']:.8f}"
        ),
        "",
        "## 地物明细",
    ]
    for feature in parsed["features"]:
        label = feature["feature_name"] or feature["feature_id"]
        point = ""
        if feature["longitude"] is not None and feature["latitude"] is not None:
            point = f"；坐标=({feature['longitude']:.8f}, {feature['latitude']:.8f})"
        lines.append(
            f"- {html.unescape(str(label))}；分组={feature['folder_path']}；"
            f"类型={feature['geometry_type']}；坐标数={feature['coordinate_count']}"
            f"{point}；坐标标注={feature['coordinate_system'] or '未声明'}"
        )
    return "\n".join(lines)
