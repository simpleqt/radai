#!/usr/bin/env python3
"""Create course-eval tables (19 tables across 3 SQL files) with idempotency.

Each child SQL file uses CREATE TABLE IF NOT EXISTS, and this wrapper also
guards on `inspect(engine).get_table_names()` so re-runs are no-ops. The
recorded checksum covers all three SQL files via MIGRATION_DEPENDENCIES.
"""

from __future__ import annotations

import sys
from pathlib import Path

from sqlalchemy import inspect, text

ROOT = Path(__file__).resolve().parents[2]
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

BASE_SQL = Path(__file__).with_name("add_course_eval_base_tables.sql")
DATA_SQL = Path(__file__).with_name("add_course_eval_data_tables.sql")
REPORT_SQL = Path(__file__).with_name("add_course_eval_report_tables.sql")

# 第一张表作为本迁移执行单元的哨兵：已存在即视为已执行，跳过全部
_SENTINEL = "ce_courses"


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
    existing = set(inspect(engine).get_table_names())
    if _SENTINEL in existing:
        print("Skip course-eval tables: ce_courses already exists")
        return 0

    with engine.begin() as conn:
        for path in (BASE_SQL, DATA_SQL, REPORT_SQL):
            statements = _split_sql(path.read_text(encoding="utf-8"))
            for statement in statements:
                conn.execute(text(statement))
    created_now = sorted(
        set(inspect(engine).get_table_names())
        & {
            "ce_courses",
            "ce_semester_tasks",
            "ce_course_goals",
            "ce_exam_papers",
            "ce_exam_sections",
            "ce_exam_items",
            "ce_students",
            "ce_score_records",
            "ce_goal_mappings",
            "ce_achievement_configs",
            "ce_report_templates",
            "ce_reports",
            "ce_report_versions",
            "ce_stats_snapshots",
            "ce_eval_tasks",
            "ce_improvement_items",
            "ce_audit_logs",
            "ce_materials",
            "ce_archives",
        }
    )
    print(f"Created course-eval tables ({len(created_now)}): {', '.join(created_now)}")
    return 0


if __name__ == "__main__":
    sys.exit(main())
