from __future__ import annotations

import argparse
import os
from pathlib import Path

import pytest

from deploy.production import render_service_env


def _args(root: Path, *, enable_outbox: bool = False) -> argparse.Namespace:
    groundwater_root = root.parent / "shared" / "groundwater" / "liutang"
    groundwater_root.mkdir(parents=True, exist_ok=True)
    (groundwater_root / "manifest.json").write_text("{}\n", encoding="utf-8")
    return argparse.Namespace(
        source=root / "config" / "service.env",
        output=root / "config" / "service.env",
        runtime_root=root,
        enable_task_outbox=enable_outbox,
        groundwater_builtin_root=groundwater_root,
    )


def test_render_preserves_existing_values_and_applies_production_isolation(
    tmp_path: Path,
) -> None:
    root = tmp_path / "runtime"
    config = root / "config"
    config.mkdir(parents=True)
    source = config / "service.env"
    source.write_text("NO_PROXY=localhost\nCUSTOM_SETTING='kept value'\n")
    os.chmod(source, 0o600)

    result = render_service_env.render(_args(root, enable_outbox=True))
    values = render_service_env.read_env(source)

    assert result["external_service_values_changed"] is False
    assert values["CUSTOM_SETTING"] == "kept value"
    assert values["AGENT_CONVERSATION_STORE_BACKEND"] == "sqlite"
    assert values["AGENT_CONVERSATION_SQLITE_PATH"].startswith(str(root))
    assert values["MYSQL_SESSION_READ_ONLY"] == "false"
    assert values["STARTUP_SCHEMA_INITIALIZATION_ENABLED"] == "false"
    assert values["TASK_DISPATCH_OUTBOX_ENABLED"] == "true"
    assert values["API_DOCS_ENABLED"] == "false"
    assert values["GROUNDWATER_BUILTIN_ROOT"] == str(
        root.parent / "shared" / "groundwater" / "liutang"
    )
    assert source.stat().st_mode & 0o077 == 0


def test_render_rejects_output_outside_runtime(tmp_path: Path) -> None:
    root = tmp_path / "runtime"
    config = root / "config"
    config.mkdir(parents=True)
    source = config / "service.env"
    source.write_text("SAFE=value\n")
    os.chmod(source, 0o600)
    args = _args(root)
    args.output = tmp_path / "outside.env"

    with pytest.raises(render_service_env.ServiceEnvError):
        render_service_env.render(args)


def test_render_keeps_task_dispatch_outbox_disabled_by_default(
    tmp_path: Path,
) -> None:
    root = tmp_path / "runtime"
    config = root / "config"
    config.mkdir(parents=True)
    source = config / "service.env"
    source.write_text("SAFE=value\n", encoding="utf-8")
    os.chmod(source, 0o600)

    result = render_service_env.render(_args(root))
    values = render_service_env.read_env(source)

    assert values["TASK_DISPATCH_OUTBOX_ENABLED"] == "false"
    assert result["task_dispatch_outbox_enabled"] is False


def test_render_rejects_missing_groundwater_dataset(tmp_path: Path) -> None:
    root = tmp_path / "runtime"
    config = root / "config"
    config.mkdir(parents=True)
    source = config / "service.env"
    source.write_text("SAFE=value\n")
    os.chmod(source, 0o600)
    args = _args(root)
    (args.groundwater_builtin_root / "manifest.json").unlink()

    with pytest.raises(render_service_env.ServiceEnvError):
        render_service_env.render(args)
