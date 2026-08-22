#!/usr/bin/env python3
"""Create immutable, SQLite-aware snapshots of production runtime state."""

from __future__ import annotations

import argparse
import hashlib
import json
import os
import sqlite3
import tempfile
import time
from dataclasses import dataclass
from pathlib import Path
from typing import Any


@dataclass(frozen=True)
class DatabaseSpec:
    relative_path: Path
    required: bool


DATABASES = (
    DatabaseSpec(Path("checkpoints.db"), True),
    DatabaseSpec(Path("guest_checkpoints.db"), False),
    DatabaseSpec(Path("agent_tasks.db"), False),
    DatabaseSpec(Path("langchain.db"), False),
    DatabaseSpec(Path("conversations/conversations.db"), True),
)


def sha256(path: Path) -> str:
    digest = hashlib.sha256()
    with path.open("rb") as handle:
        for chunk in iter(lambda: handle.read(8 * 1024 * 1024), b""):
            digest.update(chunk)
    return digest.hexdigest()


def inspect_database(path: Path) -> dict[str, Any]:
    uri = f"file:{path.resolve()}?mode=ro"
    with sqlite3.connect(uri, uri=True, timeout=120) as database:
        result = database.execute("PRAGMA quick_check").fetchone()
        tables = {
            str(row[0])
            for row in database.execute(
                "SELECT name FROM sqlite_master WHERE type='table'"
            ).fetchall()
        }
        details: dict[str, Any] = {
            "quick_check": result[0] if result else "unknown",
            "bytes": path.stat().st_size,
            "table_count": len(tables),
        }
        if "checkpoints" in tables:
            details["checkpoint_rows"] = int(
                database.execute("SELECT COUNT(*) FROM checkpoints").fetchone()[0]
            )
            details["thread_rows"] = int(
                database.execute(
                    "SELECT COUNT(DISTINCT thread_id) FROM checkpoints"
                ).fetchone()[0]
            )
        if "conversations" in tables:
            details["conversation_rows"] = int(
                database.execute("SELECT COUNT(*) FROM conversations").fetchone()[0]
            )
        return details


def backup_database(source: Path, target: Path) -> None:
    source_uri = f"file:{source.resolve()}?mode=ro"
    target.parent.mkdir(parents=True, exist_ok=True, mode=0o700)
    fd, temporary_name = tempfile.mkstemp(
        prefix=f".{target.name}.", suffix=".snapshot", dir=target.parent
    )
    os.close(fd)
    temporary = Path(temporary_name)
    temporary.unlink()
    try:
        with sqlite3.connect(source_uri, uri=True, timeout=120) as source_db:
            source_db.execute("PRAGMA query_only=ON")
            with sqlite3.connect(temporary, timeout=120) as target_db:
                source_db.backup(target_db, pages=8192, sleep=0.02)
                target_db.execute("PRAGMA wal_checkpoint(TRUNCATE)")
        os.chmod(temporary, 0o600)
        details = inspect_database(temporary)
        if details["quick_check"] != "ok":
            raise RuntimeError(f"quick_check failed for {source}")
        temporary.replace(target)
        os.chmod(target, 0o600)
    finally:
        if temporary.exists():
            temporary.unlink()


def atomic_json(path: Path, payload: dict[str, Any]) -> None:
    path.parent.mkdir(parents=True, exist_ok=True, mode=0o700)
    temporary = path.with_name(f".{path.name}.{os.getpid()}.tmp")
    temporary.write_text(
        json.dumps(payload, ensure_ascii=False, indent=2) + "\n", encoding="utf-8"
    )
    os.chmod(temporary, 0o600)
    temporary.replace(path)
    os.chmod(path, 0o600)


def snapshot(source_dir: Path, backup_root: Path, state_dir: Path) -> dict[str, Any]:
    source_dir = source_dir.resolve(strict=True)
    backup_root = backup_root.resolve(strict=False)
    state_dir = state_dir.resolve(strict=False)
    backup_root.mkdir(parents=True, exist_ok=True, mode=0o700)
    stamp = time.strftime("%Y%m%d-%H%M%S")
    final_dir = backup_root / stamp
    partial_dir = backup_root / f".{stamp}.{os.getpid()}.partial"
    if final_dir.exists() or partial_dir.exists():
        raise RuntimeError("snapshot destination already exists")
    partial_dir.mkdir(mode=0o700)
    manifest: dict[str, Any] = {
        "format_version": 1,
        "created_at_unix": int(time.time()),
        "source_dir": str(source_dir),
        "databases": {},
    }
    try:
        for spec in DATABASES:
            source = source_dir / spec.relative_path
            if not source.is_file():
                if spec.required:
                    raise FileNotFoundError(f"required database is missing: {source}")
                continue
            target = partial_dir / spec.relative_path
            backup_database(source, target)
            details = inspect_database(target)
            details["sha256"] = sha256(target)
            manifest["databases"][spec.relative_path.as_posix()] = details
        atomic_json(partial_dir / "manifest.json", manifest)
        partial_dir.replace(final_dir)
    except Exception:
        # Keep an incomplete directory as forensic evidence; it is never
        # accepted because its name ends in .partial and has no state pointer.
        raise
    pointer = {
        "format_version": 1,
        "latest_snapshot": str(final_dir),
        "created_at_unix": manifest["created_at_unix"],
        "databases": manifest["databases"],
    }
    atomic_json(state_dir / "sqlite-snapshot-latest.json", pointer)
    print(json.dumps(pointer, ensure_ascii=False, indent=2))
    return pointer


def verify(snapshot_dir: Path) -> dict[str, Any]:
    snapshot_dir = snapshot_dir.resolve(strict=True)
    manifest_path = snapshot_dir / "manifest.json"
    manifest = json.loads(manifest_path.read_text(encoding="utf-8"))
    results: dict[str, Any] = {}
    for relative, expected in (manifest.get("databases") or {}).items():
        relative_path = Path(str(relative))
        if relative_path.is_absolute() or ".." in relative_path.parts:
            raise RuntimeError("snapshot database path is not confined")
        path = (snapshot_dir / relative_path).resolve(strict=True)
        try:
            path.relative_to(snapshot_dir)
        except ValueError as exc:
            raise RuntimeError("snapshot database path is not confined") from exc
        details = inspect_database(path)
        if details["quick_check"] != "ok" or sha256(path) != expected.get("sha256"):
            raise RuntimeError(f"snapshot verification failed: {relative}")
        results[relative] = details
    if not results:
        raise RuntimeError("snapshot manifest contains no databases")
    payload = {
        "status": "verified",
        "snapshot": str(snapshot_dir),
        "databases": results,
    }
    print(json.dumps(payload, ensure_ascii=False, indent=2))
    return payload


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(description=__doc__)
    commands = parser.add_subparsers(dest="command", required=True)
    create = commands.add_parser("create")
    create.add_argument("--source-dir", type=Path, required=True)
    create.add_argument("--backup-root", type=Path, required=True)
    create.add_argument("--state-dir", type=Path, required=True)
    check = commands.add_parser("verify")
    check.add_argument("--snapshot-dir", type=Path, required=True)
    return parser


def main() -> int:
    args = build_parser().parse_args()
    if args.command == "create":
        snapshot(args.source_dir, args.backup_root, args.state_dir)
    else:
        verify(args.snapshot_dir)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
