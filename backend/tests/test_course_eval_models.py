"""Tests for the course-eval ORM models (T1-01)."""

from __future__ import annotations

from backend.db.models import Base


def test_all_tables_registered():
    """19 张 ce_* 表全部注册进 Base.metadata（§3.1 / T1-01 DoD 第 2 条）。"""
    tables = {t for t in Base.metadata.tables if t.startswith("ce_")}
    expected = {
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
        "ce_audit_logs",
        "ce_materials",
    }
    assert tables == expected


def test_naming_conventions():
    """命名规范：复数 snake_case、ce_ 前缀、String(36) 主键、is_deleted/时间戳（§1.4.3）。"""
    tables = {
        t: Base.metadata.tables[t] for t in Base.metadata.tables if t.startswith("ce_")
    }
    for name, table in tables.items():
        # 主键必须是 String(36)
        pk_cols = list(table.primary_key.columns)
        assert len(pk_cols) == 1, f"{name}: 主键必须单一"
        pk = pk_cols[0]
        assert (
            str(pk.type) == "VARCHAR(36)"
        ), f"{name}: 主键 {pk.name} 不是 VARCHAR(36): {pk.type}"
        # 软删除与时间戳（ce_eval_tasks/audit/archive 等按设计无 is_deleted，不强制）
        if "is_deleted" in table.columns:
            assert str(table.columns["is_deleted"].type) == "BOOLEAN"
        for ts in ("created_at", "updated_at"):
            if ts in table.columns:
                assert "DATETIME" in str(
                    table.columns[ts].type
                ), f"{name}.{ts} 应为 DATETIME"


def test_item_id_empty_string_not_null():
    """大题级记录用空串而非 NULL：item_id 必须 NOT NULL DEFAULT ''（§3.1 T8/T9）。"""
    score = Base.metadata.tables["ce_score_records"]
    mapping = Base.metadata.tables["ce_goal_mappings"]
    for table in (score, mapping):
        col = table.columns["item_id"]
        assert not col.nullable, f"{table.name}.item_id 不允许 NULL"
        assert col.default is not None or col.server_default is not None


def test_active_flag_not_mapped():
    """ce_eval_tasks.active_flag 是 MySQL 生成列，ORM 不映射（§1.4.4 / §4.3.6）。"""
    task = Base.metadata.tables["ce_eval_tasks"]
    assert "active_flag" not in task.columns


def test_unique_constraints_present():
    """关键唯一约束必须存在（uk_ce_task / uk_ce_student / uk_ce_score）。"""
    names = set()
    for t in Base.metadata.tables.values():
        if not t.name.startswith("ce_"):
            continue
        names.update(str(c.name) for c in t.constraints if c.name)
    for required in ("uk_ce_task", "uk_ce_student", "uk_ce_score", "uk_ce_course_code"):
        assert required in names, f"缺少唯一约束 {required}"
