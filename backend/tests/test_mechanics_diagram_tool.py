from __future__ import annotations

import json

import numpy as np
import pytest

from backend.application.tools.providers.mechanics_diagram_tool import (
    _compute_reactions,
    _normalize_diagram_type,
    _parse_spec,
    _render_figure,
    _shear_moment_curves,
)


def test_normalize_diagram_type_aliases() -> None:
    assert _normalize_diagram_type("受力图") == "free_body"
    assert _normalize_diagram_type("剪力图") == "shear"
    assert _normalize_diagram_type("弯矩") == "moment"
    assert _normalize_diagram_type("内力图") == "combined"


def test_normalize_diagram_type_rejects_unknown() -> None:
    with pytest.raises(ValueError, match="未知 diagram_type"):
        _normalize_diagram_type("temperature")


def test_parse_spec_accepts_full_json() -> None:
    spec = _parse_spec(
        json.dumps(
            {
                "length": 4,
                "supports": [
                    {"x": 0, "type": "pinned", "label": "A"},
                    {"x": 4, "type": "roller", "label": "B"},
                ],
                "loads": [{"x": 2, "value": 10, "label": "P=10kN"}],
                "uniforms": [{"start": 0, "end": 2, "value": 5, "label": "q=5kN/m"}],
                "moments": [{"x": 1, "value": 6, "label": "M=6kN·m"}],
            }
        )
    )

    assert spec["length"] == 4
    assert spec["supports"][0]["type"] == "pinned"
    assert spec["loads"][0]["value"] == 10
    assert spec["uniforms"][0]["value"] == 5
    assert spec["moments"][0]["value"] == 6


def test_parse_spec_accepts_units_in_numeric_fields() -> None:
    spec = _parse_spec(
        json.dumps(
            {
                "length": "4m",
                "supports": [
                    {"x": "0m", "type": "pinned"},
                    {"x": "4m", "type": "roller"},
                ],
                "loads": [{"x": "2m", "value": "10 kN", "label": "P=10kN"}],
                "uniforms": [
                    {"start": "1m", "end": "3m", "value": "5kN/m", "label": "q=5kN/m"}
                ],
                "moments": [{"x": "1m", "value": "6 kN·m", "label": "M=6kN·m"}],
            }
        )
    )

    assert spec["length"] == 4
    assert spec["supports"][0]["x"] == 0
    assert spec["supports"][1]["x"] == 4
    assert spec["loads"][0]["value"] == 10
    assert spec["uniforms"][0]["start"] == 1
    assert spec["uniforms"][0]["end"] == 3
    assert spec["uniforms"][0]["value"] == 5
    assert spec["moments"][0]["value"] == 6


def test_parse_spec_rejects_invalid_json_or_bad_beam() -> None:
    with pytest.raises(ValueError, match="合法 JSON"):
        _parse_spec("not-json")
    with pytest.raises(ValueError, match="至少需要一个支座"):
        _parse_spec(json.dumps({"length": 2, "supports": []}))


def test_reactions_simply_supported_center_load() -> None:
    spec = _parse_spec(
        json.dumps(
            {
                "length": 4,
                "supports": [
                    {"x": 0, "type": "pinned"},
                    {"x": 4, "type": "roller"},
                ],
                "loads": [{"x": 2, "value": 10}],
            }
        )
    )
    reactions = _compute_reactions(spec)

    assert reactions[0]["value"] == pytest.approx(5.0)
    assert reactions[1]["value"] == pytest.approx(5.0)


def test_reactions_cantilever() -> None:
    spec = _parse_spec(
        json.dumps(
            {
                "length": 4,
                "supports": [{"x": 0, "type": "fixed"}],
                "loads": [{"x": 2, "value": 10}],
            }
        )
    )
    reactions = _compute_reactions(spec)

    assert reactions[0]["value"] == pytest.approx(10.0)
    assert reactions[0]["moment"] == pytest.approx(-20.0)


def test_shear_moment_curves_center_load() -> None:
    spec = _parse_spec(
        json.dumps(
            {
                "length": 4,
                "supports": [
                    {"x": 0, "type": "pinned"},
                    {"x": 4, "type": "roller"},
                ],
                "loads": [{"x": 2, "value": 10}],
            }
        )
    )
    reactions = _compute_reactions(spec)
    xs, shear, moment = _shear_moment_curves(spec, reactions)

    near_left = xs[1]
    near_center_left = xs[np.searchsorted(xs, 1.999)]
    near_center_right = xs[np.searchsorted(xs, 2.001)]
    assert shear[np.searchsorted(xs, near_left)] == pytest.approx(5.0)
    assert shear[np.searchsorted(xs, near_center_left)] == pytest.approx(5.0)
    assert shear[np.searchsorted(xs, near_center_right)] == pytest.approx(-5.0)
    assert moment[np.searchsorted(xs, 2.0)] == pytest.approx(10.0, abs=0.15)
    assert moment[-1] == pytest.approx(0.0, abs=0.1)


@pytest.mark.parametrize(
    "diagram_type",
    ["free_body", "beam", "shear", "moment", "combined"],
)
def test_render_figure_produces_png(diagram_type: str) -> None:
    spec = _parse_spec(
        json.dumps(
            {
                "length": 4,
                "supports": [
                    {"x": 0, "type": "pinned"},
                    {"x": 4, "type": "roller"},
                ],
                "loads": [{"x": 2, "value": 10, "label": "P=10kN"}],
            }
        )
    )
    reactions = _compute_reactions(spec)
    png = _render_figure(diagram_type, spec, reactions, "测试梁")

    assert png[:8] == b"\x89PNG\r\n\x1a\n"
