"""API keys must be stored as lookup digests, not as usable credentials.

The tests run against a real SQLite schema so the dual-read window (rows created
before the backfill) and the hashed-only path are both exercised end to end.
"""

from __future__ import annotations

import re

import pytest
from sqlalchemy import inspect, select
from sqlalchemy.ext.asyncio import async_sessionmaker, create_async_engine

from backend.application import auth_service as auth_service_module
from backend.application.api_key_credentials import (
    api_key_digest,
    api_key_prefix,
    hashed_storage_available,
    mask_api_key,
    reset_hashed_storage_capability,
)
from backend.db import migrate
from backend.db.models import ApiKey, User
from backend.db.models.base import Base


@pytest.fixture(autouse=True)
def _reset_capability_probe():
    reset_hashed_storage_capability()
    yield
    reset_hashed_storage_capability()


@pytest.fixture()
async def session_factory():
    engine = create_async_engine("sqlite+aiosqlite:///:memory:")
    async with engine.begin() as connection:
        await connection.run_sync(
            Base.metadata.create_all,
            tables=[User.__table__, ApiKey.__table__],
        )
    factory = async_sessionmaker(engine, expire_on_commit=False)
    try:
        yield factory
    finally:
        await engine.dispose()


async def _resolve(monkeypatch, session_factory, raw_key: str):
    """Resolve a key through the real service against the test schema."""

    from contextlib import asynccontextmanager

    @asynccontextmanager
    async def _session_context():
        async with session_factory() as session:
            yield session

    monkeypatch.setattr(
        "backend.db.connection.read_only_session_context",
        _session_context,
    )
    service = auth_service_module.AuthService()
    return await service.resolve_api_key(raw_key)


# ── 摘要与展示 ────────────────────────────────────────────────────────


def test_digest_is_stable_and_not_reversible():
    raw = "ak_" + "s" * 43
    digest = api_key_digest(raw)

    assert digest == api_key_digest(f"  {raw}  ")
    assert re.fullmatch(r"[0-9a-f]{64}", digest)
    assert raw not in digest


def test_prefix_and_mask_never_expose_a_usable_key():
    raw = "ak_abcdefghijklmnopqrstuvwxyz"
    prefix = api_key_prefix(raw)

    assert len(prefix) == 12
    assert raw.startswith(prefix)
    assert mask_api_key(prefix=prefix) == f"{prefix}..."
    assert raw not in mask_api_key(prefix=prefix)


# ── 存储与查找 ────────────────────────────────────────────────────────


@pytest.mark.asyncio
async def test_hashed_row_authenticates_without_storing_the_key(
    monkeypatch, session_factory
):
    raw_key = "ak_hashed_only_key_value"
    async with session_factory() as session:
        session.add(
            User(user_id="u1", username="alice", api_key="managed_marker", role="admin")
        )
        session.add(
            ApiKey(
                api_key=None,
                api_key_hash=api_key_digest(raw_key),
                api_key_prefix=api_key_prefix(raw_key),
                user_id="u1",
                is_active=True,
                rate_limit=100,
            )
        )
        await session.commit()

    resolved = await _resolve(monkeypatch, session_factory, raw_key)
    assert resolved is not None
    assert resolved.user_id == "u1"
    assert resolved.role == "admin"

    async with session_factory() as session:
        stored = (await session.execute(select(ApiKey.api_key))).scalars().all()
    assert stored == [None]


@pytest.mark.asyncio
async def test_legacy_plaintext_row_still_authenticates_during_backfill(
    monkeypatch, session_factory
):
    raw_key = "ak_legacy_plaintext_key"
    async with session_factory() as session:
        session.add(
            User(user_id="u2", username="bob", api_key="managed_marker2", role="user")
        )
        session.add(ApiKey(api_key=raw_key, user_id="u2", is_active=True))
        await session.commit()

    resolved = await _resolve(monkeypatch, session_factory, raw_key)
    assert resolved is not None
    assert resolved.user_id == "u2"


@pytest.mark.asyncio
async def test_unknown_key_is_rejected(monkeypatch, session_factory):
    async with session_factory() as session:
        session.add(
            User(user_id="u3", username="carol", api_key="managed_marker3", role="user")
        )
        session.add(
            ApiKey(
                api_key=None,
                api_key_hash=api_key_digest("ak_real"),
                user_id="u3",
                is_active=True,
            )
        )
        await session.commit()

    assert await _resolve(monkeypatch, session_factory, "ak_forged") is None


@pytest.mark.asyncio
async def test_capability_probe_detects_hash_column(session_factory):
    async with session_factory() as session:
        assert await hashed_storage_available(session) is True


@pytest.mark.asyncio
async def test_capability_probe_falls_back_when_column_is_absent(monkeypatch):
    class _LegacySession:
        async def run_sync(self, fn):
            class _Inspector:
                def get_table_names(self):
                    return ["api_keys"]

                def get_columns(self, _table):
                    return [{"name": "api_key"}, {"name": "user_id"}]

            class _Bind:
                pass

            monkeypatch.setattr(
                "backend.application.api_key_credentials.inspect",
                lambda _bind: _Inspector(),
            )
            return fn(self)

        bind = None

    assert await hashed_storage_available(_LegacySession()) is False


# ── 写入路径与迁移登记 ────────────────────────────────────────────────


@pytest.mark.asyncio
async def test_admin_write_path_persists_only_the_digest(session_factory):
    from backend.api.routers.admin_accounts import _api_key_storage_fields

    raw_key = "ak_new_admin_key_value"
    async with session_factory() as session:
        fields = await _api_key_storage_fields(session, raw_key)

    assert fields["api_key"] is None
    assert fields["api_key_hash"] == api_key_digest(raw_key)
    assert fields["api_key_prefix"] == api_key_prefix(raw_key)


@pytest.mark.asyncio
async def test_admin_write_path_stays_compatible_with_legacy_schema(monkeypatch):
    from backend.api.routers import admin_accounts

    async def _legacy(_session):
        return False

    monkeypatch.setattr(admin_accounts, "hashed_storage_available", _legacy)
    fields = await admin_accounts._api_key_storage_fields(object(), "ak_legacy")

    assert fields == {"api_key": "ak_legacy"}


def test_orm_exposes_hash_columns_and_nullable_plaintext():
    columns = ApiKey.__table__.columns
    assert "api_key_hash" in columns
    assert "api_key_prefix" in columns
    assert columns["api_key"].nullable is True
    assert columns["api_key_hash"].unique is True


def test_repr_never_prints_a_full_key():
    record = ApiKey(api_key=None, api_key_hash="a" * 64, api_key_prefix="ak_abcdefghi")
    assert "ak_abcdefghi" in repr(record)
    assert "a" * 64 not in repr(record)


def test_hash_migration_is_registered_and_backfills_existing_rows():
    versions = [migration.version for migration in migrate.MIGRATIONS]
    assert versions == sorted(versions)
    assert "202608130002_add_api_key_hash" in versions

    sql = (migrate.MIGRATIONS_DIR / "add_api_key_hash.sql").read_text(encoding="utf-8")
    assert "SHA2(`api_key`, 256)" in sql
    assert "MODIFY COLUMN `api_key` VARCHAR(100) NULL" in sql
    # Dropping the plaintext column removes the rollback path, so it must stay
    # an explicit operator action rather than an automatic deployment step.
    assert "DROP COLUMN" not in sql.upper()
    assert all(
        migration.path.name != "drop_api_key_plaintext.sql"
        for migration in migrate.MIGRATIONS
    )
    assert (migrate.MIGRATIONS_DIR / "drop_api_key_plaintext.sql").exists()
    assert "202608130002_add_api_key_hash" in migrate.validate_migration_package()


@pytest.mark.asyncio
async def test_sqlite_contract_matches_orm_columns(session_factory):
    async with session_factory() as session:
        names = await session.run_sync(
            lambda sync_session: {
                column["name"]
                for column in inspect(sync_session.bind).get_columns("api_keys")
            }
        )
    assert {"api_key", "api_key_hash", "api_key_prefix"} <= names
