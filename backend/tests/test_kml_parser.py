import os
from pathlib import Path

import pytest

from backend.services.chat_file_parser import ChatFileParser
from backend.services.kml_parser import kml_to_analysis_text, parse_kml


SAMPLE_KML = b"""<?xml version="1.0" encoding="UTF-8"?>
<kml xmlns="http://www.opengis.net/kml/2.2">
  <Document>
    <name>sample-map</name>
    <Folder>
      <name>injection</name>
      <Placemark>
        <name>Z01</name>
        <OvCoordType>CGCS2000</OvCoordType>
        <Point><coordinates>108.1,24.1,0</coordinates></Point>
      </Placemark>
      <Placemark>
        <LineString><coordinates>108.1,24.1,0 108.2,24.2,0</coordinates></LineString>
      </Placemark>
    </Folder>
  </Document>
</kml>
"""


def test_parse_kml_extracts_features_vertices_and_summary() -> None:
    parsed = parse_kml(SAMPLE_KML, source_file="sample.kml")

    assert parsed["document_name"] == "sample-map"
    assert parsed["summary"]["feature_count"] == 2
    assert parsed["summary"]["named_feature_count"] == 1
    assert parsed["summary"]["geometry_counts"] == {"Point": 1, "LineString": 1}
    assert len(parsed["vertices"]) == 3
    assert parsed["features"][0]["feature_name"] == "Z01"
    assert parsed["features"][0]["coordinate_system"] == "CGCS2000"
    assert parsed["features"][1]["coordinate_count"] == 2


def test_kml_analysis_text_contains_semantic_fields() -> None:
    content = kml_to_analysis_text(SAMPLE_KML, source_file="sample.kml")

    assert "KML 地理数据：sample-map" in content
    assert "地物数：2" in content
    assert "Z01" in content
    assert "injection" in content
    assert "108.10000000" in content


@pytest.mark.asyncio
async def test_chat_file_parser_accepts_kml() -> None:
    result = await ChatFileParser().parse(
        SAMPLE_KML,
        "sample.kml",
        "application/vnd.google-earth.kml+xml",
    )

    assert result.category == "short_doc"
    assert result.method == "direct"
    assert result.content is not None
    assert "LineString" in result.content


def test_liutang_kml_expected_feature_counts() -> None:
    configured_root = os.getenv("GROUNDWATER_BUILTIN_ROOT", "").strip()
    if configured_root:
        path = (
            Path(configured_root).expanduser()
            / "attachments"
            / "文件11_北部区域具体的地理位置图.kml"
        )
    else:
        path = (
            Path(__file__).resolve().parents[2]
            / "六塘项目数据信息"
            / "文件11_北部区域具体的地理位置图.kml"
        )
    if not path.is_file():
        pytest.skip("optional Liutang groundwater dataset is not installed")
    parsed = parse_kml(path.read_bytes(), source_file=path.name)

    assert parsed["summary"]["feature_count"] == 141
    assert parsed["summary"]["geometry_counts"] == {"Point": 128, "LineString": 13}
    assert parsed["summary"]["named_feature_count"] == 66
