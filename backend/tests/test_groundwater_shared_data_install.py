from __future__ import annotations

import importlib.util
import json
import os
import stat
from pathlib import Path

import pytest


def _load_installer():
    path = (
        Path(__file__).resolve().parents[2]
        / "deploy"
        / "shared-data"
        / "install_groundwater.py"
    )
    spec = importlib.util.spec_from_file_location("groundwater_shared_data", path)
    assert spec is not None and spec.loader is not None
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


installer = _load_installer()


def _dataset(root: Path, content: str = "v1") -> Path:
    root.mkdir(parents=True)
    manifest = {
        "schema_version": "1.0",
        "project": {"name": "test"},
        "data_version": 1,
        "datasets": {},
        "record_counts": {},
    }
    (root / "manifest.json").write_text(
        json.dumps(manifest, ensure_ascii=False), encoding="utf-8"
    )
    (root / "attachments").mkdir()
    (root / "attachments" / "sample.kml").write_text(content, encoding="utf-8")
    return root


def _make_writable(root: Path) -> None:
    if not root.exists():
        return
    paths = sorted(root.rglob("*"), key=lambda path: len(path.parts))
    for path in paths:
        if not path.is_symlink():
            os.chmod(path, 0o700 if path.is_dir() else 0o600)


def test_install_is_immutable_atomic_and_idempotent(tmp_path: Path) -> None:
    source = _dataset(tmp_path / "source")
    shared = tmp_path / "shared"
    try:
        first = installer.install_dataset(source, shared)
        second = installer.install_dataset(source, shared)

        assert first["tree_sha256"] == second["tree_sha256"]
        assert first["version_reused"] is False
        assert second["version_reused"] is True
        assert (shared / "liutang").is_symlink()
        target = (shared / "liutang").resolve(strict=True)
        assert target.name == first["tree_sha256"]
        for path in (target, *target.rglob("*")):
            assert stat.S_IMODE(path.lstat().st_mode) & 0o222 == 0
        assert installer.verify_dataset(shared)["status"] == "verified"
    finally:
        _make_writable(shared)


def test_install_preserves_previous_version_and_switches_alias(tmp_path: Path) -> None:
    source = _dataset(tmp_path / "source")
    shared = tmp_path / "shared"
    try:
        first = installer.install_dataset(source, shared)
        (source / "attachments" / "sample.kml").write_text("v2", encoding="utf-8")
        second = installer.install_dataset(source, shared)

        assert first["tree_sha256"] != second["tree_sha256"]
        assert (shared / "versions" / first["tree_sha256"]).is_dir()
        assert (shared / "liutang").resolve(strict=True).name == second["tree_sha256"]
    finally:
        _make_writable(shared)


def test_install_rejects_source_symlink(tmp_path: Path) -> None:
    source = _dataset(tmp_path / "source")
    linked = tmp_path / "linked"
    linked.symlink_to(source, target_is_directory=True)

    with pytest.raises(installer.SharedDataError):
        installer.install_dataset(linked, tmp_path / "shared")
