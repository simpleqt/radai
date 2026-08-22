#!/usr/bin/env python3
"""Add the optional per-key MCP tool allowlist."""

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
        print("Skip api_keys.mcp_allowed_tools: api_keys table is missing")
        return 0

    columns = {column["name"] for column in inspector.get_columns("api_keys")}
    if "mcp_allowed_tools" in columns:
        print("Column api_keys.mcp_allowed_tools already exists")
        return 0

    with engine.begin() as connection:
        connection.execute(
            text(
                """
                ALTER TABLE api_keys
                ADD COLUMN mcp_allowed_tools JSON NULL
                COMMENT 'MCP 工具白名单；NULL 表示不限制'
                AFTER access_scope
                """
            )
        )
    print("Added column api_keys.mcp_allowed_tools")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
