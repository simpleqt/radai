from __future__ import annotations

from pathlib import Path

import pytest

from deploy.production import run_with_env
from deploy.production.render_service_env import ServiceEnvError


def test_build_environment_parses_metacharacters_as_data(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
):
    path = tmp_path / "backend.env"
    path.write_text(
        "PLAIN=value&with#characters\nQUOTED='space and # hash'\n",
        encoding="utf-8",
    )
    path.chmod(0o600)
    monkeypatch.setenv("UNCHANGED", "yes")

    environment = run_with_env.build_environment([path])

    assert environment["PLAIN"] == "value&with#characters"
    assert environment["QUOTED"] == "space and # hash"
    assert environment["UNCHANGED"] == "yes"


def test_build_environment_rejects_group_readable_file(tmp_path: Path):
    path = tmp_path / "backend.env"
    path.write_text("SAFE=value\n", encoding="utf-8")
    path.chmod(0o640)

    with pytest.raises(ServiceEnvError, match="permissions"):
        run_with_env.build_environment([path])
