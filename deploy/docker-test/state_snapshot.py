"""Create or restore SQLite-aware test-runtime cutover snapshots."""

from __future__ import annotations

import argparse
import json
import os
import sqlite3
import sys
import time
from pathlib import Path
from typing import Any

from seed_state import (
    SeedError,
    atomic_json,
    catalog_info,
    checkpoint_info,
    ensure_ports_stopped,
    online_backup,
)

DATABASES = (
    ("checkpoints/checkpoints.db", "checkpoint"),
    ("checkpoints/guest_checkpoints.db", "checkpoint"),
    ("conversations/conversations.db", "catalog"),
)


def database_info(path: Path, kind: str) -> dict[str, Any]:
    if kind == "checkpoint":
        return checkpoint_info(path)
    return catalog_info(path)


def validate_runtime_root(path: Path) -> Path:
    resolved = path.resolve()
    if resolved == Path("/") or len(resolved.parts) < 4:
        raise SeedError(f"unsafe runtime root: {resolved}")
    return resolved


def snapshot(source_root: Path, output_dir: Path) -> dict[str, Any]:
    source_root = validate_runtime_root(source_root)
    output_dir = output_dir.resolve()
    if output_dir.exists() and any(output_dir.iterdir()):
        raise SeedError(f"snapshot directory is not empty: {output_dir}")
    output_dir.mkdir(parents=True, exist_ok=True, mode=0o700)
    os.chmod(output_dir, 0o700)

    databases: dict[str, dict[str, Any]] = {}
    for relative_name, kind in DATABASES:
        source = source_root / "data" / relative_name
        target = output_dir / relative_name
        online_backup(source, target)
        databases[relative_name] = database_info(target, kind)

    manifest = {
        "format_version": 1,
        "status": "snapshotted",
        "created_at_unix": int(time.time()),
        "method": "sqlite-online-backup",
        "source_runtime": str(source_root),
        "snapshot_dir": str(output_dir),
        "databases": databases,
    }
    atomic_json(output_dir / "manifest.json", manifest)
    return manifest


def restore(
    snapshot_dir: Path, target_root: Path, guard_ports: list[int]
) -> dict[str, Any]:
    snapshot_dir = snapshot_dir.resolve()
    target_root = validate_runtime_root(target_root)
    manifest_path = snapshot_dir / "manifest.json"
    if not manifest_path.is_file():
        raise SeedError(f"snapshot manifest is missing: {manifest_path}")
    manifest = json.loads(manifest_path.read_text(encoding="utf-8"))
    if manifest.get("status") != "snapshotted":
        raise SeedError("snapshot manifest status is invalid")
    ensure_ports_stopped(guard_ports)

    restored: dict[str, dict[str, Any]] = {}
    for relative_name, kind in DATABASES:
        source = snapshot_dir / relative_name
        expected = dict((manifest.get("databases") or {}).get(relative_name) or {})
        source_info = database_info(source, kind)
        if source_info.get("sha256") != expected.get("sha256"):
            raise SeedError(f"snapshot checksum mismatch: {relative_name}")
        target = target_root / "data" / relative_name
        online_backup(source, target)
        restored_info = database_info(target, kind)
        if restored_info.get("sha256") != source_info.get("sha256"):
            raise SeedError(f"restored checksum mismatch: {relative_name}")
        restored[relative_name] = restored_info

    result = {
        "format_version": 1,
        "status": "restored",
        "restored_at_unix": int(time.time()),
        "snapshot_dir": str(snapshot_dir),
        "target_runtime": str(target_root),
        "databases": restored,
    }
    atomic_json(target_root / "state" / "last-state-restore.json", result)
    return result


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(description=__doc__)
    subparsers = parser.add_subparsers(dest="command", required=True)

    snapshot_parser = subparsers.add_parser("snapshot")
    snapshot_parser.add_argument("--source-root", type=Path, required=True)
    snapshot_parser.add_argument("--output-dir", type=Path, required=True)

    restore_parser = subparsers.add_parser("restore")
    restore_parser.add_argument("--snapshot-dir", type=Path, required=True)
    restore_parser.add_argument("--target-root", type=Path, required=True)
    restore_parser.add_argument("--guard-port", type=int, action="append", default=[])
    return parser


def main() -> int:
    args = build_parser().parse_args()
    try:
        if args.command == "snapshot":
            result = snapshot(args.source_root, args.output_dir)
        else:
            result = restore(args.snapshot_dir, args.target_root, args.guard_port)
    except (SeedError, OSError, ValueError, sqlite3.Error, json.JSONDecodeError) as exc:
        print(f"Docker test state snapshot failed: {exc}", file=sys.stderr)
        return 1
    summary = {
        "status": result["status"],
        "created_at_unix": result.get("created_at_unix"),
        "restored_at_unix": result.get("restored_at_unix"),
        "snapshot_dir": result.get("snapshot_dir"),
        "database_counts": {
            name: details.get("checkpoint_rows", details.get("rows"))
            for name, details in result["databases"].items()
        },
    }
    print(json.dumps(summary, ensure_ascii=False, indent=2))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
