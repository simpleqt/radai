"""Snapshot formal MySQL conversation metadata into the isolated test catalog.

The source transaction is read-only.  The first successful snapshot is kept on
normal test upgrades so test-only edits are not overwritten.  ``--force`` is an
explicit operator action and creates a SQLite-aware backup before replacement.
"""

from __future__ import annotations

import argparse
import json
import os
import sqlite3
import sys
import tempfile
import time
from pathlib import Path
from typing import Any

REPO_ROOT = Path(__file__).resolve().parents[2]
if str(REPO_ROOT) not in sys.path:
    sys.path.insert(0, str(REPO_ROOT))

from backend.application.conversations.local_catalog import (  # noqa: E402
    SCHEMA_SQL,
    encode_metadata,
)

FORMAT_VERSION = 1


class CatalogMigrationError(RuntimeError):
    """A catalog migration safety or integrity condition failed."""


def required_env(name: str) -> str:
    value = os.getenv(name, "").strip()
    if not value:
        raise CatalogMigrationError(f"required setting is missing: {name}")
    return value


def _json_metadata(value: Any) -> dict[str, Any] | None:
    if value is None or value == "":
        return None
    if isinstance(value, dict):
        return value
    try:
        parsed = json.loads(str(value))
    except (TypeError, ValueError, json.JSONDecodeError):
        return None
    return parsed if isinstance(parsed, dict) else None


def _iso(value: Any) -> str:
    if hasattr(value, "isoformat"):
        return str(value.isoformat())
    text = str(value or "").strip()
    return text or time.strftime("%Y-%m-%dT%H:%M:%S")


def inspect_catalog(path: Path, *, quick: bool = False) -> dict[str, Any]:
    if not path.is_file():
        return {"present": False}
    uri = f"file:{path.resolve()}?mode=ro"
    with sqlite3.connect(uri, uri=True, timeout=30) as database:
        check = "quick_check" if quick else "integrity_check"
        integrity_row = database.execute(f"PRAGMA {check}").fetchone()
        table_row = database.execute(
            "SELECT 1 FROM sqlite_master WHERE type='table' AND name='conversations'"
        ).fetchone()
        if not table_row:
            raise CatalogMigrationError("local catalog has no conversations table")
        count, users, active = database.execute(
            """
            SELECT COUNT(*), COUNT(DISTINCT user_id),
                   SUM(CASE WHEN is_deleted = 0 THEN 1 ELSE 0 END)
            FROM conversations
            """
        ).fetchone()
    return {
        "present": True,
        "integrity": integrity_row[0] if integrity_row else "unknown",
        "rows": int(count or 0),
        "users": int(users or 0),
        "active_rows": int(active or 0),
        "bytes": path.stat().st_size,
    }


def atomic_json(path: Path, payload: dict[str, Any]) -> None:
    path.parent.mkdir(parents=True, exist_ok=True, mode=0o700)
    temporary = path.with_name(f".{path.name}.{os.getpid()}.tmp")
    temporary.write_text(
        json.dumps(payload, ensure_ascii=False, indent=2) + "\n",
        encoding="utf-8",
    )
    os.chmod(temporary, 0o600)
    temporary.replace(path)


def backup_sqlite(source: Path, target: Path) -> None:
    target.parent.mkdir(parents=True, exist_ok=True, mode=0o700)
    source_uri = f"file:{source.resolve()}?mode=ro"
    with sqlite3.connect(source_uri, uri=True, timeout=30) as source_db:
        source_db.execute("PRAGMA query_only=ON")
        with sqlite3.connect(target, timeout=30) as target_db:
            source_db.backup(target_db)
    os.chmod(target, 0o600)


def snapshot_mysql(target: Path) -> dict[str, Any]:
    import pymysql

    connection = pymysql.connect(
        host=required_env("MYSQL_HOST"),
        port=int(required_env("MYSQL_PORT")),
        user=required_env("MYSQL_USER"),
        password=required_env("MYSQL_PASSWORD"),
        database=required_env("MYSQL_DATABASE"),
        charset="utf8mb4",
        connect_timeout=10,
        read_timeout=120,
        write_timeout=10,
        autocommit=False,
    )
    target.parent.mkdir(parents=True, exist_ok=True, mode=0o700)
    fd, temporary_name = tempfile.mkstemp(
        prefix=f".{target.name}.", suffix=".tmp", dir=target.parent
    )
    os.close(fd)
    temporary = Path(temporary_name)
    try:
        with connection.cursor() as cursor:
            cursor.execute("SET SESSION TRANSACTION READ ONLY")
            cursor.execute("START TRANSACTION WITH CONSISTENT SNAPSHOT")
            cursor.execute(
                """
                SELECT id, user_id, thread_id, title, metadata, is_deleted,
                       created_at, updated_at
                FROM conversations
                ORDER BY id
                """
            )
            rows = cursor.fetchall()

        with sqlite3.connect(temporary, timeout=30) as database:
            database.executescript(SCHEMA_SQL)
            database.executemany(
                """
                INSERT INTO conversations(
                    id, user_id, thread_id, title, metadata,
                    is_deleted, created_at, updated_at
                ) VALUES (?, ?, ?, ?, ?, ?, ?, ?)
                """,
                [
                    (
                        str(row[0]),
                        str(row[1]),
                        str(row[2]),
                        str(row[3] or "新对话"),
                        encode_metadata(_json_metadata(row[4])),
                        int(bool(row[5])),
                        _iso(row[6]),
                        _iso(row[7]),
                    )
                    for row in rows
                ],
            )
            database.commit()
            database.execute("PRAGMA wal_checkpoint(TRUNCATE)")
        os.chmod(temporary, 0o600)
        info = inspect_catalog(temporary, quick=True)
        if info.get("integrity") != "ok" or info.get("rows") != len(rows):
            raise CatalogMigrationError("local catalog snapshot verification failed")
        temporary.replace(target)
        os.chmod(target, 0o600)
        return info
    finally:
        connection.close()
        if temporary.exists():
            temporary.unlink()


def verify(args: argparse.Namespace) -> dict[str, Any]:
    target = args.target.resolve()
    marker = args.marker.resolve()
    if not marker.is_file():
        raise CatalogMigrationError(f"catalog migration marker is missing: {marker}")
    manifest = json.loads(marker.read_text(encoding="utf-8"))
    if manifest.get("format_version") != FORMAT_VERSION:
        raise CatalogMigrationError("unsupported catalog migration format")
    info = inspect_catalog(target, quick=bool(args.quick))
    if info.get("integrity") != "ok":
        raise CatalogMigrationError("local conversation catalog integrity check failed")
    baseline = int((manifest.get("catalog") or {}).get("rows") or 0)
    if int(info.get("rows") or 0) < baseline:
        raise CatalogMigrationError("local conversation catalog fell below migrated baseline")
    return {
        "status": "verified",
        "format_version": FORMAT_VERSION,
        "source_mode": "formal-mysql-read-only-snapshot",
        "catalog": info,
        "marker": str(marker),
    }


def migrate(args: argparse.Namespace) -> dict[str, Any]:
    target = args.target.resolve()
    marker = args.marker.resolve()
    if marker.is_file() and not args.force:
        result = verify(
            argparse.Namespace(target=target, marker=marker, quick=True)
        )
        return {
            "status": "preserved",
            "reason": "initial conversation catalog migration already completed",
            "verification": result,
        }

    backup_path = None
    if target.is_file():
        backup_dir = args.backup_dir.resolve()
        backup_path = backup_dir / (
            f"{target.name}.before-{int(time.time())}-{os.getpid()}.sqlite"
        )
        backup_sqlite(target, backup_path)

    info = snapshot_mysql(target)
    manifest = {
        "format_version": FORMAT_VERSION,
        "status": "migrated",
        "created_at_unix": int(time.time()),
        "source_mode": "formal-mysql-read-only-snapshot",
        "target": str(target),
        "catalog": info,
        "replaced_backup": str(backup_path) if backup_path else None,
    }
    atomic_json(marker, manifest)
    return manifest


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser()
    commands = parser.add_subparsers(dest="command", required=True)

    migration = commands.add_parser("migrate")
    migration.add_argument("--target", type=Path, required=True)
    migration.add_argument("--marker", type=Path, required=True)
    migration.add_argument("--backup-dir", type=Path, required=True)
    migration.add_argument("--force", action="store_true")
    migration.set_defaults(handler=migrate)

    verification = commands.add_parser("verify")
    verification.add_argument("--target", type=Path, required=True)
    verification.add_argument("--marker", type=Path, required=True)
    verification.add_argument("--quick", action="store_true")
    verification.set_defaults(handler=verify)
    return parser


def main() -> int:
    args = build_parser().parse_args()
    try:
        result = args.handler(args)
    except (
        CatalogMigrationError,
        OSError,
        ValueError,
        sqlite3.Error,
        json.JSONDecodeError,
    ) as exc:
        print(f"conversation catalog migration failed: {exc}", file=sys.stderr)
        return 1
    print(json.dumps(result, ensure_ascii=False, indent=2))
    return 0


if __name__ == "__main__":
    sys.exit(main())
