#!/usr/bin/env python3
"""Create skill-management tables with MySQL 5.7-compatible idempotency."""

from __future__ import annotations

import sys
from pathlib import Path

from sqlalchemy import inspect, text

ROOT = Path(__file__).resolve().parents[2]
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

SQL_PATH = Path(__file__).with_suffix(".sql")


def _split_sql(sql: str) -> list[str]:
    statements: list[str] = []
    current: list[str] = []
    for raw_line in sql.splitlines():
        line = raw_line.strip()
        if not line or line.startswith("--"):
            continue
        current.append(raw_line)
        if line.endswith(";"):
            statement = "\n".join(current).strip().rstrip(";").strip()
            if statement:
                statements.append(statement)
            current = []
    tail = "\n".join(current).strip()
    if tail:
        statements.append(tail)
    return statements


def main() -> int:
    from backend.db.connection import get_engine

    engine = get_engine()
    statements = _split_sql(SQL_PATH.read_text(encoding="utf-8"))
    if not statements or not statements[0].lstrip().upper().startswith(
        "CREATE TABLE IF NOT EXISTS SKILLS"
    ):
        raise RuntimeError("技能管理迁移必须先创建 skills 表")

    # MySQL 5.7 does not support ADD COLUMN IF NOT EXISTS.  Create the table
    # first, inspect the actual legacy schema, and issue a plain ALTER only
    # when the column is absent.
    with engine.begin() as conn:
        conn.execute(text(statements[0]))
        columns = {column["name"] for column in inspect(conn).get_columns("skills")}
        if "display_description" not in columns:
            conn.execute(
                text(
                    "ALTER TABLE skills "
                    "ADD COLUMN display_description VARCHAR(1000) NOT NULL "
                    "DEFAULT '' AFTER description"
                )
            )
            print("Added column skills.display_description")
        else:
            print("Column skills.display_description already exists")

        for statement in statements[1:]:
            conn.execute(text(statement))

    print("Skill-management tables are ready")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
