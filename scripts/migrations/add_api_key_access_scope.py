#!/usr/bin/env python3
"""Add an explicit access scope to API keys.

Existing keys remain full-access for backward compatibility. Administrators
can create ``mcp_only`` keys after this migration is applied.
"""

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
    if "api_keys" not in inspector.get_table_names():
        print("Skip api_keys.access_scope: api_keys table is missing")
        return 0

    columns = {column["name"] for column in inspector.get_columns("api_keys")}
    with engine.begin() as connection:
        if "access_scope" not in columns:
            connection.execute(
                text(
                    """
                    ALTER TABLE api_keys
                    ADD COLUMN access_scope VARCHAR(20) NOT NULL DEFAULT 'full'
                    COMMENT '访问范围: full/mcp_only'
                    AFTER key_name
                    """
                )
            )
            print("Added column api_keys.access_scope")
        else:
            print("Column api_keys.access_scope already exists")

        result = connection.execute(
            text(
                """
                UPDATE api_keys
                SET access_scope = 'full'
                WHERE access_scope IS NULL OR access_scope = ''
                """
            )
        )
        print(f"Normalized {result.rowcount} API key scope rows")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
