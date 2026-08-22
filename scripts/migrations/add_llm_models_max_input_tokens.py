#!/usr/bin/env python3
"""为 llm_models 表添加 max_input_tokens 列并将全部模型更新为 128K。"""

from __future__ import annotations

import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parents[2]
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

DEFAULT_MAX_INPUT_TOKENS = 131072


def main() -> int:
    from sqlalchemy import inspect, text

    from backend.db.connection import get_engine

    engine = get_engine()
    inspector = inspect(engine)
    columns = {col["name"] for col in inspector.get_columns("llm_models")}

    with engine.begin() as conn:
        if "max_input_tokens" not in columns:
            conn.execute(
                text(
                    """
                    ALTER TABLE llm_models
                    ADD COLUMN max_input_tokens INT NOT NULL DEFAULT :default_tokens
                    COMMENT '最大输入 token（上下文窗口，默认 128K）'
                    AFTER max_tokens
                    """
                ),
                {"default_tokens": DEFAULT_MAX_INPUT_TOKENS},
            )
            print("Added column llm_models.max_input_tokens")
        else:
            print("Column llm_models.max_input_tokens already exists")

        result = conn.execute(
            text("UPDATE llm_models SET max_input_tokens = :tokens"),
            {"tokens": DEFAULT_MAX_INPUT_TOKENS},
        )
        print(f"Updated {result.rowcount} rows to max_input_tokens={DEFAULT_MAX_INPUT_TOKENS}")

    return 0


if __name__ == "__main__":
    raise SystemExit(main())
