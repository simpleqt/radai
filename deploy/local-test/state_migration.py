"""Online migration of live conversation checkpoints into the test runtime.

This module copies only LangGraph SQLite checkpoints.  The companion
``conversation_catalog_migration.py`` takes a read-only MySQL snapshot into a
separate test-runtime SQLite catalog.  SQLite's online backup API snapshots
the live WAL databases consistently without stopping or locking production
services for the duration of a raw file copy.
"""

from __future__ import annotations

import argparse
import hashlib
import json
import os
import socket
import sqlite3
import sys
import tempfile
import time
from pathlib import Path
from typing import Any

FORMAT_VERSION = 2
DEFAULT_DATABASES = ("checkpoints.db", "guest_checkpoints.db")
TEST_PORTS = (18082, 18083, 18086)


class MigrationError(RuntimeError):
    """A checkpoint migration safety condition failed."""


def assert_test_apis_stopped(ports: tuple[int, ...] = TEST_PORTS) -> None:
    listening: list[int] = []
    for port in ports:
        with socket.socket(socket.AF_INET, socket.SOCK_STREAM) as probe:
            probe.settimeout(0.2)
            if probe.connect_ex(("127.0.0.1", port)) == 0:
                listening.append(port)
    if listening:
        raise MigrationError(
            "test API processes must be stopped before checkpoint replacement; "
            f"listening ports: {listening}"
        )


def sqlite_info(path: Path, *, quick: bool = False) -> dict[str, Any]:
    if not path.is_file():
        return {"present": False}
    uri = f"file:{path.resolve()}?mode=ro"
    with sqlite3.connect(uri, uri=True, timeout=60) as database:
        check_name = "quick_check" if quick else "integrity_check"
        integrity = database.execute(f"PRAGMA {check_name}").fetchone()
        tables = {
            str(row[0])
            for row in database.execute(
                "SELECT name FROM sqlite_master WHERE type='table'"
            ).fetchall()
        }
        checkpoint_rows = None
        thread_rows = None
        if "checkpoints" in tables:
            checkpoint_rows = int(
                database.execute("SELECT COUNT(*) FROM checkpoints").fetchone()[0]
            )
            thread_rows = int(
                database.execute(
                    "SELECT COUNT(DISTINCT thread_id) FROM checkpoints"
                ).fetchone()[0]
            )
    return {
        "present": True,
        "integrity": integrity[0] if integrity else "unknown",
        "bytes": path.stat().st_size,
        "checkpoint_rows": checkpoint_rows,
        "thread_rows": thread_rows,
    }


def online_backup(source_path: Path, target_path: Path) -> None:
    if not source_path.is_file():
        raise MigrationError(f"source checkpoint does not exist: {source_path}")
    target_path.parent.mkdir(parents=True, exist_ok=True, mode=0o700)
    source_uri = f"file:{source_path.resolve()}?mode=ro"
    last_reported = -1

    def progress(status: int, remaining: int, total: int) -> None:
        nonlocal last_reported
        copied = total - remaining
        percent = 100 if total == 0 else int(copied * 100 / total)
        bucket = percent // 10
        if bucket != last_reported or remaining == 0:
            last_reported = bucket
            print(
                f"checkpoint backup {source_path.name}: "
                f"{percent}% ({copied}/{total} pages, status={status})",
                file=sys.stderr,
                flush=True,
            )

    with sqlite3.connect(source_uri, uri=True, timeout=60) as source:
        source.execute("PRAGMA query_only=ON")
        with sqlite3.connect(target_path, timeout=60) as target:
            source.backup(
                target,
                pages=4096,
                progress=progress,
                sleep=0.02,
            )
            target.execute("PRAGMA wal_checkpoint(TRUNCATE)")
    os.chmod(target_path, 0o600)
    info = sqlite_info(target_path, quick=True)
    if info["integrity"] != "ok":
        raise MigrationError(
            f"SQLite quick_check failed after backup: {source_path.name}"
        )


def file_sha256(path: Path) -> str:
    digest = hashlib.sha256()
    with path.open("rb") as source:
        for chunk in iter(lambda: source.read(8 * 1024 * 1024), b""):
            digest.update(chunk)
    return digest.hexdigest()


def atomic_json(path: Path, payload: dict[str, Any]) -> None:
    temporary = path.with_name(f".{path.name}.{os.getpid()}.tmp")
    temporary.write_text(
        json.dumps(payload, ensure_ascii=False, indent=2) + "\n",
        encoding="utf-8",
    )
    os.chmod(temporary, 0o600)
    temporary.replace(path)


def migrate(args: argparse.Namespace) -> dict[str, Any]:
    source_dir = args.source_dir.resolve()
    target_dir = args.target_dir.resolve()
    marker = args.marker.resolve()
    if marker.is_file() and not args.force:
        verified = verify(
            argparse.Namespace(target_dir=target_dir, marker=marker, quick=True)
        )
        return {
            "status": "preserved",
            "reason": "initial migration already completed",
            "verification": verified,
        }

    assert_test_apis_stopped()
    target_dir.mkdir(parents=True, exist_ok=True, mode=0o700)
    os.chmod(target_dir, 0o700)
    backup_dir = args.backup_dir.resolve()
    backup_dir.mkdir(parents=True, exist_ok=True, mode=0o700)

    replaced_backups: dict[str, str] = {}
    migrated: dict[str, dict[str, Any]] = {}
    for name in DEFAULT_DATABASES:
        source_path = source_dir / name
        if not source_path.is_file():
            if name == "guest_checkpoints.db":
                continue
            raise MigrationError(
                f"required source checkpoint is missing: {source_path}"
            )

        target_path = target_dir / name
        if target_path.is_file():
            backup_path = (
                backup_dir / f"{name}.before-{int(time.time())}-{os.getpid()}.sqlite"
            )
            online_backup(target_path, backup_path)
            replaced_backups[name] = str(backup_path)

        fd, temporary_name = tempfile.mkstemp(
            prefix=f".{name}.",
            suffix=".tmp",
            dir=target_dir,
        )
        os.close(fd)
        temporary = Path(temporary_name)
        try:
            temporary.unlink()
            online_backup(source_path, temporary)
            target_info = sqlite_info(temporary, quick=True)
            temporary.replace(target_path)
            os.chmod(target_path, 0o600)
            migrated[name] = {
                **target_info,
                "sha256": file_sha256(target_path),
            }
        finally:
            if temporary.exists():
                temporary.unlink()

    if "checkpoints.db" not in migrated:
        raise MigrationError("main checkpoint database was not migrated")
    manifest = {
        "format_version": FORMAT_VERSION,
        "status": "migrated",
        "created_at_unix": int(time.time()),
        "source_dir": str(source_dir),
        "target_dir": str(target_dir),
        "mysql_metadata_mode": "shared-existing-read-only",
        "databases": migrated,
        "replaced_backups": replaced_backups,
    }
    marker.parent.mkdir(parents=True, exist_ok=True, mode=0o700)
    atomic_json(marker, manifest)
    return manifest


def verify(args: argparse.Namespace) -> dict[str, Any]:
    target_dir = args.target_dir.resolve()
    marker = args.marker.resolve()
    if not marker.is_file():
        raise MigrationError(f"migration marker is missing: {marker}")
    manifest = json.loads(marker.read_text(encoding="utf-8"))
    if manifest.get("format_version") != FORMAT_VERSION:
        raise MigrationError(
            f"unsupported migration format: {manifest.get('format_version')}"
        )
    databases: dict[str, dict[str, Any]] = {}
    for name, expected in dict(manifest.get("databases") or {}).items():
        if name not in DEFAULT_DATABASES:
            raise MigrationError(f"unsafe database name in marker: {name}")
        path = target_dir / name
        info = sqlite_info(path, quick=bool(args.quick))
        if not info["present"] or info["integrity"] != "ok":
            raise MigrationError(f"checkpoint verification failed: {name}")
        for field in ("checkpoint_rows", "thread_rows"):
            if info[field] is None or info[field] < int(expected.get(field) or 0):
                raise MigrationError(
                    f"checkpoint verification fell below migrated baseline: "
                    f"{name} {field}"
                )
        databases[name] = info
    if "checkpoints.db" not in databases:
        raise MigrationError("main checkpoint database is absent from marker")
    return {
        "status": "verified",
        "format_version": FORMAT_VERSION,
        "mysql_metadata_mode": "shared-existing-read-only",
        "databases": databases,
        "marker": str(marker),
    }


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser()
    subparsers = parser.add_subparsers(dest="command", required=True)

    migration = subparsers.add_parser("migrate")
    migration.add_argument("--source-dir", type=Path, required=True)
    migration.add_argument("--target-dir", type=Path, required=True)
    migration.add_argument("--marker", type=Path, required=True)
    migration.add_argument("--backup-dir", type=Path, required=True)
    migration.add_argument("--force", action="store_true")
    migration.set_defaults(handler=migrate)

    verification = subparsers.add_parser("verify")
    verification.add_argument("--target-dir", type=Path, required=True)
    verification.add_argument("--marker", type=Path, required=True)
    verification.add_argument("--quick", action="store_true")
    verification.set_defaults(handler=verify)
    return parser


def main() -> int:
    args = build_parser().parse_args()
    try:
        result = args.handler(args)
    except (MigrationError, OSError, sqlite3.Error, json.JSONDecodeError) as exc:
        print(f"checkpoint migration failed: {exc}", file=sys.stderr)
        return 1
    print(json.dumps(result, ensure_ascii=False, indent=2))
    return 0


if __name__ == "__main__":
    sys.exit(main())
