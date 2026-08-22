"""Seed the Docker test runtime from the live isolated-test SQLite state.

The source databases stay online and are opened read-only. SQLite's backup API
produces consistent copies without sharing WAL files between the host and
container runtimes. The Docker shadow API ports must be stopped before a
target database can be replaced.
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


class SeedError(RuntimeError):
    """A state seeding safety or integrity check failed."""


def ensure_ports_stopped(ports: list[int]) -> None:
    listening: list[int] = []
    for port in ports:
        with socket.socket(socket.AF_INET, socket.SOCK_STREAM) as probe:
            probe.settimeout(0.2)
            if probe.connect_ex(("127.0.0.1", port)) == 0:
                listening.append(port)
    if listening:
        raise SeedError(
            "Docker test APIs must be stopped before state replacement; "
            f"listening ports: {listening}"
        )


def atomic_json(path: Path, payload: dict[str, Any]) -> None:
    path.parent.mkdir(parents=True, exist_ok=True, mode=0o700)
    descriptor, temporary_name = tempfile.mkstemp(
        prefix=f".{path.name}.", suffix=".tmp", dir=path.parent
    )
    try:
        with os.fdopen(descriptor, "w", encoding="utf-8") as output:
            json.dump(payload, output, ensure_ascii=False, indent=2)
            output.write("\n")
        os.chmod(temporary_name, 0o600)
        Path(temporary_name).replace(path)
        os.chmod(path, 0o600)
    finally:
        temporary = Path(temporary_name)
        if temporary.exists():
            temporary.unlink()


def online_backup(source: Path, target: Path) -> None:
    if not source.is_file():
        raise SeedError(f"source SQLite database is missing: {source}")
    target.parent.mkdir(parents=True, exist_ok=True, mode=0o700)
    descriptor, temporary_name = tempfile.mkstemp(
        prefix=f".{target.name}.", suffix=".tmp", dir=target.parent
    )
    os.close(descriptor)
    temporary = Path(temporary_name)
    try:
        temporary.unlink()
        source_uri = f"file:{source.resolve()}?mode=ro"
        with sqlite3.connect(source_uri, uri=True, timeout=60) as source_db:
            source_db.execute("PRAGMA query_only=ON")
            with sqlite3.connect(temporary, timeout=60) as target_db:
                source_db.backup(target_db, pages=4096, sleep=0.02)
                target_db.execute("PRAGMA wal_checkpoint(TRUNCATE)")
        with sqlite3.connect(temporary, timeout=30) as target_db:
            integrity = target_db.execute("PRAGMA quick_check").fetchone()
        if not integrity or integrity[0] != "ok":
            raise SeedError(f"SQLite quick_check failed: {source.name}")
        os.chmod(temporary, 0o600)
        temporary.replace(target)
        os.chmod(target, 0o600)
    finally:
        if temporary.exists():
            temporary.unlink()


def sha256(path: Path) -> str:
    digest = hashlib.sha256()
    with path.open("rb") as source:
        for chunk in iter(lambda: source.read(8 * 1024 * 1024), b""):
            digest.update(chunk)
    return digest.hexdigest()


def checkpoint_info(path: Path) -> dict[str, Any]:
    uri = f"file:{path.resolve()}?mode=ro"
    with sqlite3.connect(uri, uri=True, timeout=30) as database:
        integrity = database.execute("PRAGMA quick_check").fetchone()
        tables = {
            str(row[0])
            for row in database.execute(
                "SELECT name FROM sqlite_master WHERE type='table'"
            ).fetchall()
        }
        if "checkpoints" not in tables:
            raise SeedError(f"checkpoint table is missing: {path.name}")
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
        "sha256": sha256(path),
    }


def catalog_info(path: Path) -> dict[str, Any]:
    uri = f"file:{path.resolve()}?mode=ro"
    with sqlite3.connect(uri, uri=True, timeout=30) as database:
        integrity = database.execute("PRAGMA quick_check").fetchone()
        table = database.execute(
            "SELECT 1 FROM sqlite_master WHERE type='table' AND name='conversations'"
        ).fetchone()
        if not table:
            raise SeedError("conversation catalog has no conversations table")
        rows, users, active_rows = database.execute(
            """
            SELECT COUNT(*), COUNT(DISTINCT user_id),
                   SUM(CASE WHEN is_deleted = 0 THEN 1 ELSE 0 END)
            FROM conversations
            """
        ).fetchone()
    return {
        "present": True,
        "integrity": integrity[0] if integrity else "unknown",
        "rows": int(rows or 0),
        "users": int(users or 0),
        "active_rows": int(active_rows or 0),
        "bytes": path.stat().st_size,
        "sha256": sha256(path),
    }


def verify_existing(args: argparse.Namespace) -> dict[str, Any] | None:
    checkpoint_marker = args.target_state / "checkpoint-migration.json"
    catalog_marker = args.target_state / "conversation-catalog-migration.json"
    if args.force or not checkpoint_marker.is_file() or not catalog_marker.is_file():
        return None
    checkpoint_manifest = json.loads(checkpoint_marker.read_text(encoding="utf-8"))
    catalog_manifest = json.loads(catalog_marker.read_text(encoding="utf-8"))
    checkpoint_counts: dict[str, int] = {}
    for name, expected in dict(checkpoint_manifest.get("databases") or {}).items():
        if name not in {"checkpoints.db", "guest_checkpoints.db"}:
            raise SeedError(f"unexpected checkpoint database in marker: {name}")
        current = checkpoint_info(args.target_checkpoints / name)
        if str(current.get("integrity")) != "ok":
            raise SeedError(f"checkpoint database failed quick_check: {name}")
        # 检查点保留 GC（doc-agent checkpoints-gc / Agent API 进程内维护调度）
        # 按线程只保留最近 N 个检查点,总行数会合法下降;状态丢失的判据是
        # 「线程消失」——GC 始终保留每线程最新一个检查点,行数守卫会把正常
        # 维护误判成数据丢失并卡死后续所有部署(2026-08-13 实发)。
        baseline_threads = int((expected or {}).get("thread_rows") or 0)
        if current["thread_rows"] < baseline_threads:
            raise SeedError(f"checkpoint threads fell below baseline: {name}")
        checkpoint_counts[name] = int(current["checkpoint_rows"])
    current_catalog = catalog_info(args.target_conversations / "conversations.db")
    baseline_rows = int((catalog_manifest.get("catalog") or {}).get("rows") or 0)
    if current_catalog["rows"] < baseline_rows:
        raise SeedError("conversation catalog rows fell below baseline")
    return {
        "status": "preserved",
        "checkpoint_rows": checkpoint_counts,
        "conversation_rows": current_catalog["rows"],
    }


def seed(args: argparse.Namespace) -> dict[str, Any]:
    existing = verify_existing(args)
    if existing is not None:
        return existing

    ensure_ports_stopped(args.guard_port)
    args.target_checkpoints.mkdir(parents=True, exist_ok=True, mode=0o700)
    args.target_conversations.mkdir(parents=True, exist_ok=True, mode=0o700)
    args.target_state.mkdir(parents=True, exist_ok=True, mode=0o700)

    migrated: dict[str, dict[str, Any]] = {}
    for name in ("checkpoints.db", "guest_checkpoints.db"):
        source = args.source_checkpoints / name
        if not source.is_file() and name == "guest_checkpoints.db":
            continue
        target = args.target_checkpoints / name
        online_backup(source, target)
        migrated[name] = checkpoint_info(target)
    if "checkpoints.db" not in migrated:
        raise SeedError("main checkpoint database was not seeded")

    source_catalog = args.source_conversations / "conversations.db"
    target_catalog = args.target_conversations / "conversations.db"
    online_backup(source_catalog, target_catalog)
    catalog = catalog_info(target_catalog)

    created_at = int(time.time())
    checkpoint_manifest = {
        "format_version": 2,
        "status": "migrated",
        "created_at_unix": created_at,
        "source_mode": "isolated-test-sqlite-online-snapshot",
        "source_dir": str(args.source_checkpoints.resolve()),
        "target_dir": str(args.target_checkpoints.resolve()),
        "mysql_metadata_mode": "shared-existing-read-only",
        "databases": migrated,
        "replaced_backups": {},
    }
    catalog_manifest = {
        "format_version": 1,
        "status": "migrated",
        "created_at_unix": created_at,
        "source_mode": "isolated-test-sqlite-online-snapshot",
        "source": str(source_catalog.resolve()),
        "target": str(target_catalog.resolve()),
        "catalog": catalog,
        "replaced_backup": None,
    }
    atomic_json(args.target_state / "checkpoint-migration.json", checkpoint_manifest)
    atomic_json(
        args.target_state / "conversation-catalog-migration.json",
        catalog_manifest,
    )
    return {
        "status": "seeded",
        "source_mode": "isolated-test-sqlite-online-snapshot",
        "checkpoint_rows": {
            name: details["checkpoint_rows"] for name, details in migrated.items()
        },
        "conversation_rows": catalog["rows"],
    }


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--source-checkpoints", type=Path, required=True)
    parser.add_argument("--source-conversations", type=Path, required=True)
    parser.add_argument("--target-checkpoints", type=Path, required=True)
    parser.add_argument("--target-conversations", type=Path, required=True)
    parser.add_argument("--target-state", type=Path, required=True)
    parser.add_argument("--guard-port", type=int, action="append", default=[])
    parser.add_argument("--force", action="store_true")
    return parser


def main() -> int:
    try:
        result = seed(build_parser().parse_args())
    except (SeedError, OSError, sqlite3.Error, json.JSONDecodeError) as exc:
        print(f"Docker test state seed failed: {exc}", file=sys.stderr)
        return 1
    print(json.dumps(result, ensure_ascii=False, indent=2))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
