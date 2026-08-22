from __future__ import annotations

from sqlalchemy import create_engine, inspect

from backend.db import migrate


def test_tracked_migration_package_is_complete_and_valid():
    manifest = migrate.validate_migration_package()

    assert list(manifest) == [migration.version for migration in migrate.MIGRATIONS]
    assert all(len(checksum) == 64 for checksum in manifest.values())


def test_verify_package_does_not_load_database_runtime(monkeypatch):
    def fail_if_database_is_loaded():
        raise AssertionError("offline package verification connected to a database")

    monkeypatch.setattr(migrate, "_get_engine", fail_if_database_is_loaded)

    assert migrate.main(["--verify-package"]) == 0


def test_dry_run_does_not_create_schema_migrations(monkeypatch):
    engine = create_engine("sqlite+pysqlite:///:memory:")
    monkeypatch.setattr(migrate, "_get_engine", lambda: engine)

    try:
        assert migrate.run_pending(dry_run=True) == 0
        assert not inspect(engine).has_table("schema_migrations")
    finally:
        engine.dispose()


def test_check_mode_returns_nonzero_when_migrations_are_pending(monkeypatch):
    engine = create_engine("sqlite+pysqlite:///:memory:")
    monkeypatch.setattr(migrate, "_get_engine", lambda: engine)

    try:
        assert migrate.run_pending(dry_run=True, require_clean=True) == 3
        assert not inspect(engine).has_table("schema_migrations")
    finally:
        engine.dispose()


def test_mark_existing_requires_explicit_schema_acknowledgement(monkeypatch):
    def fail_if_database_is_loaded():
        raise AssertionError("database must not load before acknowledgement")

    monkeypatch.setattr(migrate, "_get_engine", fail_if_database_is_loaded)

    try:
        migrate.mark_existing([migrate.MIGRATIONS[0].version])
    except RuntimeError as exc:
        assert "acknowledge-existing-schema" in str(exc)
    else:
        raise AssertionError("unsafe migration ledger backfill was accepted")
