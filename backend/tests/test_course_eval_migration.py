"""Tests for the course-eval migration package (T1-02).

迁移文件一旦执行就不能改（checksum 校验），SQL↔ORM 逐字段比对是最后一道闸。
比对方式：从 3 个 SQL 文件中解析每张表的列名集合，与 ORM metadata 比对。
MySQL 特有但 ORM 不映射的生成列（ce_eval_tasks.active_flag）在比对时豁免。
"""

from __future__ import annotations

import re
from backend.db import migrate
from backend.db.models import Base

MIGRATIONS_DIR = migrate.MIGRATIONS_DIR

SQL_FILES = [
    MIGRATIONS_DIR / "add_course_eval_base_tables.sql",
    MIGRATIONS_DIR / "add_course_eval_data_tables.sql",
    MIGRATIONS_DIR / "add_course_eval_report_tables.sql",
]

# MySQL 生成列，只存在于 SQL，ORM 有意不映射（§1.4.4 / §4.3.6）
_SQL_ONLY_COLUMNS = {"ce_eval_tasks": {"active_flag"}}

# SQL 物理表保留但 ORM 已移除（2026-08-11 对话式改造删除归档/改进台账）：
# 迁移文件执行后不可改（checksum），物理表留作历史数据，不参与 ORM 语义。
_SQL_ONLY_TABLES = {"ce_archives", "ce_improvement_items"}


def _parse_table_columns(sql: str) -> dict[str, set[str]]:
    """解析 CREATE TABLE 语句，返回 {表名: {列名}}。只取建表块内的顶层列定义。"""
    tables: dict[str, set[str]] = {}
    for m in re.finditer(
        r"CREATE TABLE IF NOT EXISTS\s+(\w+)\s*\((.*?)\)\s*ENGINE", sql, re.S
    ):
        table_name = m.group(1)
        cols: set[str] = set()
        for line in m.group(2).splitlines():
            stripped = line.strip().rstrip(",").strip()
            if not stripped:
                continue
            upper = stripped.upper()
            if upper.startswith(("KEY ", "CONSTRAINT ", "UNIQUE ", "PRIMARY ")):
                continue
            match = re.match(r"`?([A-Za-z_][A-Za-z0-9_]*)", stripped)
            if match:
                cols.add(match.group(1))
        tables[table_name] = cols
    return tables


def test_sql_matches_orm_schema():
    """3 个迁移文件的每张表列集合与 ORM metadata 完全一致（除生成列白名单）。"""
    sql = "\n".join(p.read_text(encoding="utf-8") for p in SQL_FILES)
    sql_tables = _parse_table_columns(sql)

    orm_tables = {
        name: set(table.columns.keys())
        for name, table in Base.metadata.tables.items()
        if name.startswith("ce_")
    }

    assert set(sql_tables) - _SQL_ONLY_TABLES == set(orm_tables), (
        f"表集合不一致: SQL-only={set(sql_tables) - _SQL_ONLY_TABLES - set(orm_tables)} "
        f"ORM-only={set(orm_tables) - set(sql_tables)}"
    )
    for table_name, sql_cols in sql_tables.items():
        if table_name in _SQL_ONLY_TABLES:
            continue
        expected = sql_cols - _SQL_ONLY_COLUMNS.get(table_name, set())
        actual = orm_tables[table_name]
        assert expected == actual, (
            f"{table_name}: SQL 有而 ORM 无={expected - actual}，"
            f"ORM 有而 SQL 无={actual - expected}"
        )


def test_migration_is_registered():
    """MIGRATIONS 末尾注册、版本号升序、依赖登记 3 个子文件。"""
    versions = [m.version for m in migrate.MIGRATIONS]
    assert versions == sorted(versions), "MIGRATIONS 必须按版本号升序"
    assert "202608110001_add_course_eval_tables" in versions

    deps = migrate.MIGRATION_DEPENDENCIES["202608110001_add_course_eval_tables"]
    assert set(deps) == {
        MIGRATIONS_DIR / "add_course_eval_base_tables.sql",
        MIGRATIONS_DIR / "add_course_eval_data_tables.sql",
        MIGRATIONS_DIR / "add_course_eval_report_tables.sql",
    }


def test_each_sql_file_uses_innodb_utf8mb4():
    """InnoDB + utf8mb4_unicode_ci + 表注释（T1-02 DoD 第 1 条）。"""
    for path in SQL_FILES:
        content = path.read_text(encoding="utf-8")
        assert content.count("ENGINE=InnoDB") == content.count(
            "CREATE TABLE"
        ), f"{path.name}: 每张表必须 ENGINE=InnoDB"
        assert content.count("utf8mb4_unicode_ci") == content.count("CREATE TABLE")
        assert content.count("COMMENT=") == content.count("CREATE TABLE")


def test_split_sql_handles_comments_and_statements():
    """幂等包装的语句拆分器：跳过注释、按分号拆分。"""
    import importlib.util

    wrapper_path = MIGRATIONS_DIR / "add_course_eval_tables.py"
    spec = importlib.util.spec_from_file_location(
        "_add_course_eval_tables", wrapper_path
    )
    module = importlib.util.module_from_spec(spec)
    assert spec.loader is not None
    spec.loader.exec_module(module)

    sql = "-- 注释\nCREATE TABLE a (x INT);\n\nCREATE TABLE b (y INT);"
    statements = module._split_sql(sql)
    assert len(statements) == 2
    assert statements[0].startswith("CREATE TABLE a")
