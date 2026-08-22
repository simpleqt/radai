"""Distribution and hermetic-test contracts for the agent platform."""

from __future__ import annotations

import os
from pathlib import Path, PurePosixPath

try:
    import tomllib
except ModuleNotFoundError:  # Python 3.10 compatibility
    import tomli as tomllib

PROJECT_ROOT = Path(__file__).resolve().parents[2]


def test_wheel_package_data_covers_runtime_yaml_registries():
    pyproject = tomllib.loads(
        (PROJECT_ROOT / "pyproject.toml").read_text(encoding="utf-8")
    )
    patterns = pyproject["tool"]["setuptools"]["package-data"]["backend"]

    for relative_path in (
        "config/prompts/registry.yaml",
        "config/mcp/registry.yaml",
    ):
        assert (PROJECT_ROOT / "backend" / relative_path).is_file()
        assert any(
            PurePosixPath(relative_path).match(pattern) for pattern in patterns
        ), f"backend/{relative_path} is not covered by wheel package-data"


def test_unit_suite_forces_local_runtime_backends():
    assert os.environ["AGENT_RESOURCE_LEASE_BACKEND"] == "memory"
    assert os.environ["AGENT_RUNTIME_STORE_BACKEND"] == "disabled"
    assert os.environ["LOG_KAFKA_ENABLED"] == "false"
