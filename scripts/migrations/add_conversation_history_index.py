#!/usr/bin/env python3
"""Add the composite index used by paginated conversation history queries."""

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
    indexes = {index["name"] for index in inspector.get_indexes("conversations")}
    index_name = "idx_conversation_user_history"
    if index_name in indexes:
        print(f"Index conversations.{index_name} already exists")
        return 0

    with engine.begin() as connection:
        if engine.dialect.name == "mysql":
            connection.execute(
                text(
                    "ALTER TABLE conversations "
                    "ADD INDEX idx_conversation_user_history "
                    "(user_id, is_deleted, updated_at, id)"
                )
            )
        else:
            connection.execute(
                text(
                    "CREATE INDEX idx_conversation_user_history "
                    "ON conversations (user_id, is_deleted, updated_at, id)"
                )
            )
    print(f"Added conversations.{index_name}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
