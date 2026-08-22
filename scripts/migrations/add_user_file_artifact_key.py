#!/usr/bin/env python3
"""Add an idempotency key for generated user-file artifacts."""

from __future__ import annotations

import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parents[2]
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))


def main() -> int:
    from sqlalchemy import inspect, text

    from backend.db.connection import get_engine

    engine = get_engine()
    inspector = inspect(engine)
    columns = {column["name"] for column in inspector.get_columns("user_files")}
    indexes = {index["name"] for index in inspector.get_indexes("user_files")}
    constraints = {
        constraint.get("name")
        for constraint in inspector.get_unique_constraints("user_files")
    }

    with engine.begin() as connection:
        if "artifact_key" not in columns:
            connection.execute(
                text("ALTER TABLE user_files ADD COLUMN artifact_key VARCHAR(64) NULL")
            )
            print("Added user_files.artifact_key")
        if "uq_user_files_artifact_key" not in indexes | constraints:
            connection.execute(
                text(
                    "ALTER TABLE user_files ADD UNIQUE INDEX "
                    "uq_user_files_artifact_key (artifact_key)"
                )
            )
            print("Added unique index user_files.uq_user_files_artifact_key")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
