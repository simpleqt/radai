"""Tracked database migration runner.

This is intentionally small: existing migration files remain the source of
truth, while this runner adds a ``schema_migrations`` table so environments can
record which migrations have been applied.
"""

from __future__ import annotations

import argparse
import hashlib
import importlib.util
import sys
from contextlib import contextmanager
from dataclasses import dataclass
from datetime import datetime
from pathlib import Path

from sqlalchemy import text

from backend.db.connection import get_engine

ROOT = Path(__file__).resolve().parents[2]
MIGRATIONS_DIR = ROOT / "scripts" / "migrations"


@dataclass(frozen=True)
class Migration:
    version: str
    path: Path


MIGRATIONS: tuple[Migration, ...] = (
    Migration(
        "202607100001_add_admin_management_tables",
        MIGRATIONS_DIR / "add_admin_management_tables.sql",
    ),
    Migration(
        "202607100002_add_context_artifacts_table",
        MIGRATIONS_DIR / "add_context_artifacts_table.sql",
    ),
    Migration(
        "202607100003_add_learning_style_tables",
        MIGRATIONS_DIR / "add_learning_style_tables.sql",
    ),
    Migration(
        "202607100004_add_custom_learning_tables",
        MIGRATIONS_DIR / "add_custom_learning_tables.sql",
    ),
    Migration(
        "202607100005_add_skill_management_tables",
        MIGRATIONS_DIR / "add_skill_management_tables.sql",
    ),
    Migration(
        "202607100006_add_user_display_name",
        MIGRATIONS_DIR / "add_user_display_name.sql",
    ),
    Migration(
        "202607100007_add_llm_models_max_input_tokens",
        MIGRATIONS_DIR / "add_llm_models_max_input_tokens.py",
    ),
    Migration(
        "202607100008_add_fun_asr_nano_model",
        MIGRATIONS_DIR / "add_fun_asr_nano_model.sql",
    ),
    Migration(
        "202607100009_add_qwen36_35b_explore_model",
        MIGRATIONS_DIR / "add_qwen36_35b_explore_model.sql",
    ),
    Migration(
        "202607100010_run_self_ingestion_migrations",
        MIGRATIONS_DIR / "run_all_self_ingestion_migrations.py",
    ),
    Migration(
        "202607100011_add_extraction_task_unique",
        MIGRATIONS_DIR / "add_extraction_task_unique.py",
    ),
    Migration(
        "202607100012_expand_self_ingestion_content_columns",
        MIGRATIONS_DIR / "expand_self_ingestion_content_columns.py",
    ),
    Migration(
        "202607100013_add_resource_access_acl_tables",
        MIGRATIONS_DIR / "add_resource_access_acl_tables.sql",
    ),
    Migration(
        "202607100014_add_task_outbox_scan_index",
        MIGRATIONS_DIR / "add_task_outbox_scan_index.py",
    ),
    Migration(
        "202607110001_add_agent_runtime_tables",
        MIGRATIONS_DIR / "add_agent_runtime_tables.sql",
    ),
    Migration(
        "202607110002_add_agent_trace_index_tables",
        MIGRATIONS_DIR / "add_agent_trace_index_tables.sql",
    ),
    Migration(
        "202607210001_add_conversation_history_index",
        MIGRATIONS_DIR / "add_conversation_history_index.py",
    ),
    Migration(
        "202607230001_expand_kb_pending_files_file_type",
        MIGRATIONS_DIR / "expand_kb_pending_files_file_type.py",
    ),
    Migration(
        "202607230002_add_user_file_artifact_key",
        MIGRATIONS_DIR / "add_user_file_artifact_key.py",
    ),
)

# Python wrapper migrations can execute other source files.  Their recorded
# checksum must cover the full executable migration unit, otherwise a change in
# a child script would silently bypass drift detection.
MIGRATION_DEPENDENCIES: dict[str, tuple[Path, ...]] = {
    "202607100010_run_self_ingestion_migrations": (
        MIGRATIONS_DIR / "ensure_self_ingestion_phase1.py",
        MIGRATIONS_DIR / "add_self_ingestion_compat_columns.py",
        MIGRATIONS_DIR / "add_self_ingestion_fact_trace_columns.py",
        MIGRATIONS_DIR / "add_kb_document_sort_order.py",
        MIGRATIONS_DIR / "drop_self_ingestion_mysql_graph_tables.py",
    ),
}


def _ensure_schema_migrations(conn) -> None:
    conn.execute(
        text(
            """
            CREATE TABLE IF NOT EXISTS schema_migrations (
                version VARCHAR(128) PRIMARY KEY,
                name VARCHAR(255) NOT NULL,
                checksum CHAR(64) NOT NULL,
                applied_at DATETIME NOT NULL,
                execution_ms INTEGER NOT NULL DEFAULT 0
            ) ENGINE=InnoDB DEFAULT CHARSET=utf8mb4
            """
        )
    )


def _checksum(path: Path) -> str:
    return hashlib.sha256(path.read_bytes()).hexdigest()


def _migration_checksum(migration: Migration) -> str:
    dependencies = MIGRATION_DEPENDENCIES.get(migration.version, ())
    if not dependencies:
        return _checksum(migration.path)
    hasher = hashlib.sha256()
    paths = (migration.path, *dependencies)
    for path in paths:
        if not path.exists():
            raise FileNotFoundError(path)
        # Include file names and separators so concatenation boundaries cannot
        # produce the same digest for a different dependency layout.
        hasher.update(path.name.encode("utf-8"))
        hasher.update(b"\0")
        hasher.update(path.read_bytes())
        hasher.update(b"\0")
    return hasher.hexdigest()


def _applied_migrations(conn) -> dict[str, str]:
    _ensure_schema_migrations(conn)
    result = conn.execute(text("SELECT version, checksum FROM schema_migrations"))
    return {str(row[0]): str(row[1]) for row in result.fetchall()}


def _verify_applied_checksums(applied: dict[str, str]) -> None:
    tracked = {migration.version: migration for migration in MIGRATIONS}
    for version, recorded_checksum in applied.items():
        migration = tracked.get(version)
        if migration is None:
            continue
        if not migration.path.exists():
            raise FileNotFoundError(migration.path)
        current_checksum = _migration_checksum(migration)
        if recorded_checksum != current_checksum:
            raise RuntimeError(
                "已执行迁移文件发生变更: "
                f"{version} {migration.path.name}; 请新增迁移而不是修改历史文件"
            )


@contextmanager
def _migration_lock(engine, *, timeout_seconds: int = 60):
    """Serialize migration runners with a MySQL session advisory lock."""
    if engine.dialect.name != "mysql":
        yield
        return

    lock_name = "document_processing_agent_schema_migrations"
    with engine.connect() as conn:
        acquired = conn.execute(
            text("SELECT GET_LOCK(:name, :timeout)"),
            {"name": lock_name, "timeout": timeout_seconds},
        ).scalar()
        if acquired != 1:
            raise RuntimeError("无法获取数据库迁移锁，请稍后重试")
        try:
            yield
        finally:
            conn.execute(text("SELECT RELEASE_LOCK(:name)"), {"name": lock_name})


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


def _run_sql_migration(conn, path: Path) -> None:
    for statement in _split_sql(path.read_text(encoding="utf-8")):
        conn.execute(text(statement))


def _run_python_migration(path: Path) -> None:
    module_name = f"_tracked_migration_{path.stem}"
    spec = importlib.util.spec_from_file_location(module_name, path)
    if spec is None or spec.loader is None:
        raise RuntimeError(f"无法加载迁移脚本: {path}")
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    main = getattr(module, "main", None)
    if main is None:
        raise RuntimeError(f"迁移脚本缺少 main(): {path}")
    result = main()
    if result not in (None, 0):
        raise RuntimeError(f"迁移脚本返回非零状态 {result}: {path}")


def _record_migration(
    conn, migration: Migration, checksum: str, execution_ms: int
) -> None:
    conn.execute(
        text(
            """
            INSERT INTO schema_migrations(version, name, checksum, applied_at, execution_ms)
            VALUES (:version, :name, :checksum, :applied_at, :execution_ms)
            ON DUPLICATE KEY UPDATE
                name = VALUES(name),
                checksum = VALUES(checksum),
                execution_ms = VALUES(execution_ms)
            """
        ),
        {
            "version": migration.version,
            "name": migration.path.name,
            "checksum": checksum,
            "applied_at": datetime.now(),
            "execution_ms": execution_ms,
        },
    )


def mark_existing() -> int:
    engine = get_engine()
    with _migration_lock(engine):
        with engine.begin() as conn:
            applied = _applied_migrations(conn)
            _verify_applied_checksums(applied)
            for migration in MIGRATIONS:
                if not migration.path.exists():
                    raise FileNotFoundError(migration.path)
                if migration.version in applied:
                    print(f"already marked {migration.version} {migration.path.name}")
                    continue
                _record_migration(conn, migration, _migration_checksum(migration), 0)
                print(f"marked {migration.version} {migration.path.name}")
    return 0


def run_pending(*, dry_run: bool = False) -> int:
    engine = get_engine()
    with _migration_lock(engine):
        with engine.begin() as conn:
            applied = _applied_migrations(conn)
            _verify_applied_checksums(applied)
            pending = [
                migration
                for migration in MIGRATIONS
                if migration.version not in applied
            ]

        if not pending:
            print("No pending migrations")
            return 0

        for migration in pending:
            if not migration.path.exists():
                raise FileNotFoundError(migration.path)
            checksum = _migration_checksum(migration)
            if dry_run:
                print(f"pending {migration.version} {migration.path.name}")
                continue

            started_at = datetime.now()
            if migration.path.suffix == ".sql":
                with engine.begin() as conn:
                    _run_sql_migration(conn, migration.path)
            elif migration.path.suffix == ".py":
                _run_python_migration(migration.path)
            else:
                raise RuntimeError(f"不支持的迁移文件类型: {migration.path}")

            execution_ms = int((datetime.now() - started_at).total_seconds() * 1000)
            with engine.begin() as conn:
                _record_migration(conn, migration, checksum, execution_ms)
            print(
                f"applied {migration.version} {migration.path.name} ({execution_ms}ms)"
            )

    return 0


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description="Run tracked database migrations")
    parser.add_argument("--dry-run", action="store_true", help="只列出待执行迁移")
    parser.add_argument(
        "--mark-existing",
        action="store_true",
        help="仅写入 schema_migrations 基线，不执行迁移",
    )
    args = parser.parse_args(argv)

    if args.mark_existing:
        return mark_existing()
    return run_pending(dry_run=args.dry_run)


if __name__ == "__main__":
    raise SystemExit(main(sys.argv[1:]))
