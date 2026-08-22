#!/usr/bin/env python3
"""Idempotently create the context_artifacts metadata table."""

from __future__ import annotations

import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parents[2]
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))


def main() -> None:
    from sqlalchemy import inspect, text

    from backend.db.connection import get_engine

    sql_path = Path(__file__).with_suffix(".sql")
    engine = get_engine()
    inspector = inspect(engine)
    if "context_artifacts" in set(inspector.get_table_names()):
        print("Table context_artifacts already exists")
        return
    with engine.begin() as conn:
        conn.execute(text(sql_path.read_text(encoding="utf-8")))
    print("Created table context_artifacts")


if __name__ == "__main__":
    main()
