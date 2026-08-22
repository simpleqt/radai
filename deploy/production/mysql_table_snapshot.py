#!/usr/bin/env python3
"""Create and verify a private, consistent snapshot of one MySQL table."""

from __future__ import annotations

import argparse
import base64
import gzip
import hashlib
import json
import os
import re
import shutil
import tempfile
import time
from datetime import date, datetime
from datetime import time as datetime_time
from decimal import Decimal
from pathlib import Path
from typing import Any

from sqlalchemy import inspect, text

IDENTIFIER = re.compile(r"^[A-Za-z_][A-Za-z0-9_]*$")


def sha256(path: Path) -> str:
    digest = hashlib.sha256()
    with path.open("rb") as source:
        for chunk in iter(lambda: source.read(1024 * 1024), b""):
            digest.update(chunk)
    return digest.hexdigest()


def encode_value(value: Any) -> Any:
    if value is None or isinstance(value, (str, int, float, bool)):
        return value
    if isinstance(value, Decimal):
        return {"$type": "decimal", "value": str(value)}
    if isinstance(value, datetime):
        return {"$type": "datetime", "value": value.isoformat()}
    if isinstance(value, date):
        return {"$type": "date", "value": value.isoformat()}
    if isinstance(value, datetime_time):
        return {"$type": "time", "value": value.isoformat()}
    if isinstance(value, (bytes, bytearray, memoryview)):
        return {
            "$type": "bytes-base64",
            "value": base64.b64encode(bytes(value)).decode("ascii"),
        }
    if isinstance(value, (dict, list)):
        return value
    raise TypeError(f"unsupported database value type: {type(value).__name__}")


def atomic_json(path: Path, payload: dict[str, Any]) -> None:
    temporary = path.with_name(f".{path.name}.{os.getpid()}.tmp")
    temporary.write_text(
        json.dumps(payload, ensure_ascii=False, indent=2) + "\n", encoding="utf-8"
    )
    os.chmod(temporary, 0o600)
    temporary.replace(path)
    os.chmod(path, 0o600)


def _read_transaction(connection, dialect: str) -> None:
    if dialect == "mysql":
        connection.exec_driver_sql(
            "SET SESSION TRANSACTION ISOLATION LEVEL REPEATABLE READ"
        )
        connection.commit()
        connection.exec_driver_sql(
            "START TRANSACTION WITH CONSISTENT SNAPSHOT, READ ONLY"
        )
    else:
        connection.exec_driver_sql("PRAGMA query_only=ON")
        connection.exec_driver_sql("BEGIN")


def _create_statement(connection, table: str, dialect: str) -> str:
    if dialect == "mysql":
        row = connection.exec_driver_sql(f"SHOW CREATE TABLE `{table}`").fetchone()
        if not row:
            raise RuntimeError(f"cannot read CREATE TABLE for {table}")
        return str(row[1])
    row = connection.execute(
        text("SELECT sql FROM sqlite_master WHERE type='table' AND name=:name"),
        {"name": table},
    ).fetchone()
    if not row or not row[0]:
        raise RuntimeError(f"cannot read CREATE TABLE for {table}")
    return str(row[0])


def snapshot_table(engine, table: str, backup_root: Path) -> dict[str, Any]:
    if not IDENTIFIER.fullmatch(table):
        raise ValueError("table name is not a safe SQL identifier")
    if not inspect(engine).has_table(table):
        raise RuntimeError(f"table does not exist: {table}")

    backup_root = backup_root.resolve(strict=False)
    backup_root.mkdir(parents=True, exist_ok=True, mode=0o700)
    os.chmod(backup_root, 0o700)
    stamp = time.strftime("%Y%m%d-%H%M%S")
    final_dir = backup_root / table / stamp
    partial_dir = backup_root / table / f".{stamp}.{os.getpid()}.partial"
    final_dir.parent.mkdir(parents=True, exist_ok=True, mode=0o700)
    os.chmod(final_dir.parent, 0o700)
    if final_dir.exists() or partial_dir.exists():
        raise RuntimeError("snapshot destination already exists")
    partial_dir.mkdir(mode=0o700)
    data_path = partial_dir / f"{table}.jsonl.gz"
    row_count = 0
    try:
        with engine.connect() as connection:
            dialect = connection.dialect.name
            _read_transaction(connection, dialect)
            try:
                create_sql = _create_statement(connection, table, dialect)
                quoted = connection.dialect.identifier_preparer.quote(table)
                result = connection.execution_options(stream_results=True).execute(
                    text(f"SELECT * FROM {quoted}")
                )
                columns = list(result.keys())
                fd, temporary_name = tempfile.mkstemp(
                    prefix=f".{table}.", suffix=".gz", dir=partial_dir
                )
                os.close(fd)
                temporary = Path(temporary_name)
                try:
                    with gzip.open(temporary, "wt", encoding="utf-8") as output:
                        output.write(
                            json.dumps(
                                {
                                    "record": "header",
                                    "format_version": 1,
                                    "table": table,
                                    "created_at_unix": int(time.time()),
                                    "columns": columns,
                                    "create_sql": create_sql,
                                },
                                ensure_ascii=False,
                            )
                            + "\n"
                        )
                        for row in result:
                            output.write(
                                json.dumps(
                                    {
                                        "record": "row",
                                        "values": [
                                            encode_value(value) for value in row
                                        ],
                                    },
                                    ensure_ascii=False,
                                )
                                + "\n"
                            )
                            row_count += 1
                    os.chmod(temporary, 0o600)
                    temporary.replace(data_path)
                finally:
                    if temporary.exists():
                        temporary.unlink()
            finally:
                connection.rollback()

        manifest = {
            "format_version": 1,
            "table": table,
            "created_at_unix": int(time.time()),
            "row_count": row_count,
            "data_file": data_path.name,
            "sha256": sha256(data_path),
        }
        atomic_json(partial_dir / "manifest.json", manifest)
        partial_dir.replace(final_dir)
        payload = {**manifest, "snapshot_dir": str(final_dir)}
        print(json.dumps(payload, ensure_ascii=False, indent=2))
        return payload
    except Exception:
        if partial_dir.exists():
            shutil.rmtree(partial_dir)
        raise


def verify_snapshot(snapshot_dir: Path) -> dict[str, Any]:
    snapshot_dir = snapshot_dir.resolve(strict=True)
    manifest_path = snapshot_dir / "manifest.json"
    manifest = json.loads(manifest_path.read_text(encoding="utf-8"))
    data_file = Path(str(manifest["data_file"]))
    if data_file.name != str(data_file) or data_file.is_absolute():
        raise RuntimeError("table snapshot data file is not confined")
    data_path = (snapshot_dir / data_file).resolve(strict=True)
    if data_path.parent != snapshot_dir:
        raise RuntimeError("table snapshot data file is not confined")
    if sha256(data_path) != manifest.get("sha256"):
        raise RuntimeError("table snapshot checksum mismatch")
    rows = 0
    header: dict[str, Any] | None = None
    with gzip.open(data_path, "rt", encoding="utf-8") as source:
        for line_number, line in enumerate(source, start=1):
            record = json.loads(line)
            if line_number == 1:
                header = record
                if (
                    record.get("record") != "header"
                    or record.get("format_version") != 1
                ):
                    raise RuntimeError("invalid table snapshot header")
            elif record.get("record") == "row":
                rows += 1
            else:
                raise RuntimeError("invalid table snapshot record")
    if header is None or header.get("table") != manifest.get("table"):
        raise RuntimeError("table snapshot manifest mismatch")
    if rows != int(manifest.get("row_count", -1)):
        raise RuntimeError("table snapshot row count mismatch")
    payload = {
        "status": "verified",
        "snapshot_dir": str(snapshot_dir),
        "table": manifest["table"],
        "row_count": rows,
        "sha256": manifest["sha256"],
    }
    print(json.dumps(payload, ensure_ascii=False, indent=2))
    return payload


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(description=__doc__)
    commands = parser.add_subparsers(dest="command", required=True)
    create = commands.add_parser("create")
    create.add_argument("--table", required=True)
    create.add_argument("--backup-root", type=Path, required=True)
    check = commands.add_parser("verify")
    check.add_argument("--snapshot-dir", type=Path, required=True)
    return parser


def main() -> int:
    args = build_parser().parse_args()
    if args.command == "verify":
        verify_snapshot(args.snapshot_dir)
        return 0
    from backend.db.connection import get_engine

    snapshot_table(get_engine(), args.table, args.backup_root)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
