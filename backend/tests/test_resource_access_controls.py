from __future__ import annotations

from datetime import datetime, timedelta
from types import SimpleNamespace

import pytest
from fastapi import HTTPException
from sqlalchemy import create_engine, select
from sqlalchemy.ext.asyncio import async_sessionmaker, create_async_engine
from sqlalchemy.orm import sessionmaker
from starlette.requests import Request

from backend.api.dependencies.auth import (
    ApiKeyAuth,
    filter_accessible_kb_ids,
    require_kb_access,
)
from backend.db.models import (
    KnowledgeBase,
    KnowledgeBaseAcl,
    KnowledgeBaseStatus,
    UserFile,
    UserFilePermission,
)
from backend.services import table_analysis_files


@pytest.mark.asyncio
async def test_kb_read_access_uses_public_owner_and_explicit_acl_only():
    engine = create_async_engine("sqlite+aiosqlite:///:memory:")
    async with engine.begin() as connection:
        await connection.run_sync(
            lambda sync_connection: KnowledgeBase.__table__.create(sync_connection)
        )
        await connection.run_sync(
            lambda sync_connection: KnowledgeBaseAcl.__table__.create(sync_connection)
        )

    factory = async_sessionmaker(engine, expire_on_commit=False)
    async with factory() as session:
        session.add_all(
            [
                KnowledgeBase(
                    kb_id="public-kb",
                    kb_code="public-code",
                    kb_name="Public",
                    owner_id=None,
                    is_default=False,
                    status=KnowledgeBaseStatus.ACTIVE,
                ),
                KnowledgeBase(
                    kb_id="default-kb",
                    kb_code="default-code",
                    kb_name="Default",
                    owner_id="owner-1",
                    is_default=True,
                    status=KnowledgeBaseStatus.ACTIVE,
                ),
                KnowledgeBase(
                    kb_id="private-kb",
                    kb_code="private-code",
                    kb_name="Private",
                    owner_id="owner-1",
                    is_default=False,
                    status=KnowledgeBaseStatus.ACTIVE,
                ),
                KnowledgeBase(
                    kb_id="inactive-kb",
                    kb_code="inactive-code",
                    kb_name="Inactive",
                    owner_id=None,
                    is_default=True,
                    status=KnowledgeBaseStatus.INACTIVE,
                ),
                KnowledgeBaseAcl(
                    id=1,
                    kb_id="private-kb",
                    subject_type="user",
                    subject_id="grantee-1",
                    permission="read",
                    effect="allow",
                    created_by="owner-1",
                ),
                KnowledgeBaseAcl(
                    id=2,
                    kb_id="private-kb",
                    subject_type="user",
                    subject_id="dual-grantee",
                    permission="read",
                    effect="allow",
                    created_by="owner-1",
                ),
                KnowledgeBaseAcl(
                    id=3,
                    kb_id="private-kb",
                    subject_type="role",
                    subject_id="restricted",
                    permission="read",
                    effect="deny",
                    created_by="owner-1",
                ),
            ]
        )
        await session.commit()

        candidates = ["public-kb", "default-kb", "private-kb", "inactive-kb"]
        assert await filter_accessible_kb_ids(
            session,
            candidates,
            ApiKeyAuth(api_key="", user_id="guest", role="user"),
        ) == ["public-kb", "default-kb"]
        assert await filter_accessible_kb_ids(
            session,
            candidates,
            ApiKeyAuth(api_key="", user_id="owner-1", role="user"),
        ) == ["public-kb", "default-kb", "private-kb"]
        assert await filter_accessible_kb_ids(
            session,
            candidates,
            ApiKeyAuth(api_key="", user_id="grantee-1", role="user"),
        ) == ["public-kb", "default-kb", "private-kb"]
        assert await filter_accessible_kb_ids(
            session,
            candidates,
            ApiKeyAuth(api_key="", user_id="dual-grantee", role="restricted"),
        ) == ["public-kb", "default-kb"]

        with pytest.raises(HTTPException) as denied:
            await require_kb_access(
                session,
                "private-kb",
                ApiKeyAuth(api_key="", user_id="stranger", role="user"),
            )
        assert denied.value.status_code == 403

    await engine.dispose()


@pytest.mark.asyncio
async def test_missing_kb_acl_table_fails_closed_without_poisoning_transaction():
    engine = create_async_engine("sqlite+aiosqlite:///:memory:")
    async with engine.begin() as connection:
        await connection.run_sync(
            lambda sync_connection: KnowledgeBase.__table__.create(sync_connection)
        )

    factory = async_sessionmaker(engine, expire_on_commit=False)
    async with factory() as session:
        session.add(
            KnowledgeBase(
                kb_id="private-kb",
                kb_code="private-only-code",
                kb_name="Private",
                owner_id="owner-1",
                is_default=False,
                status=KnowledgeBaseStatus.ACTIVE,
            )
        )
        await session.commit()

        assert await filter_accessible_kb_ids(
            session,
            ["private-kb"],
            ApiKeyAuth(api_key="", user_id="stranger", role="user"),
        ) == []
        # The ACL lookup runs under a savepoint; a missing legacy table must not
        # invalidate the outer request transaction.
        assert (await session.execute(select(KnowledgeBase.kb_id))).scalar_one() == "private-kb"

    await engine.dispose()


@pytest.mark.asyncio
async def test_guest_query_rejects_private_selected_kb_before_streaming(monkeypatch):
    from backend.api.routers import guest_agent

    async def deny_all(_session, _kb_ids, _auth):
        return []

    monkeypatch.setattr(guest_agent, "filter_accessible_kb_ids", deny_all)
    request = Request({"type": "http", "headers": []})
    with pytest.raises(HTTPException) as denied:
        await guest_agent.query_guest(
            guest_agent.GuestQueryRequest(
                question="test",
                kb_ids=["private-kb"],
            ),
            request,
            ApiKeyAuth(api_key="", user_id="guest", role="user"),
            session=object(),
        )
    assert denied.value.status_code == 403


@pytest.mark.asyncio
async def test_explore_download_uses_kb_acl_not_agent_binding(monkeypatch):
    from backend.api.routers import explore

    checked: list[tuple[str, str, str]] = []

    async def deny_private(_session, kb_id, auth, *, access):
        checked.append((kb_id, auth.user_id, access))
        raise HTTPException(status_code=403, detail="无权访问该知识库")

    async def unexpected_download(*_args, **_kwargs):
        raise AssertionError("ACL 拒绝后不得读取知识库原文")

    monkeypatch.setattr(explore, "require_kb_access", deny_private)
    monkeypatch.setattr(explore.svc, "download_kb_resource", unexpected_download)

    with pytest.raises(HTTPException) as denied:
        await explore.explore_resource_download(
            kb_id="private-agent-bound-kb",
            doc_id="secret-doc",
            user=SimpleNamespace(user_id="stranger", role="user"),
            session=object(),
        )

    assert denied.value.status_code == 403
    assert checked == [("private-agent-bound-kb", "stranger", "read")]


@pytest.mark.asyncio
async def test_pending_file_delete_rechecks_current_kb_write_access(monkeypatch):
    from backend.api.routers import uploads
    from backend.api.routers import uploads_batch_ingest

    pending_file = SimpleNamespace(
        file_id="pending-1",
        kb_id="transferred-kb",
        created_by="former-owner",
        status=0,
        minio_path="pending/transferred-kb/pending-1/file.pdf",
    )

    class Result:
        def scalar_one_or_none(self):
            return pending_file

    class Session:
        async def execute(self, _statement):
            return Result()

    checked: list[tuple[str, str, str]] = []

    async def deny_stale_uploader(_session, kb_id, auth, *, access):
        checked.append((kb_id, auth.user_id, access))
        raise HTTPException(status_code=403, detail="无权修改该知识库")

    monkeypatch.setattr(uploads_batch_ingest, "require_kb_access", deny_stale_uploader)
    monkeypatch.setattr(
        uploads_batch_ingest,
        "_get_minio_client",
        lambda: (_ for _ in ()).throw(AssertionError("授权失败后不得删除对象")),
    )

    with pytest.raises(HTTPException) as denied:
        await uploads.delete_pending_file(
            "pending-1",
            auth=ApiKeyAuth(api_key="", user_id="former-owner", role="user"),
            session=Session(),
        )

    assert denied.value.status_code == 403
    assert checked == [("transferred-kb", "former-owner", "write")]


def test_table_file_resolution_checks_owner_and_unexpired_grant(tmp_path, monkeypatch):
    table_path = tmp_path / "private.xlsx"
    table_path.write_bytes(b"placeholder")

    engine = create_engine("sqlite:///:memory:")
    UserFile.__table__.create(engine)
    UserFilePermission.__table__.create(engine)
    factory = sessionmaker(bind=engine, expire_on_commit=False)
    with factory() as session:
        session.add(
            UserFile(
                id=1,
                file_id="uf_private",
                owner_user_id="owner-1",
                source_type="local",
                local_abs_path=str(table_path),
                display_name="private.xlsx",
                mime_type="application/vnd.openxmlformats-officedocument.spreadsheetml.sheet",
                status="active",
            )
        )
        session.add_all(
            [
                UserFilePermission(
                    id=1,
                    file_id="uf_private",
                    grantee_user_id="grantee-1",
                    permission="read",
                    expires_at=datetime.now() + timedelta(hours=1),
                ),
                UserFilePermission(
                    id=2,
                    file_id="uf_private",
                    grantee_user_id="expired-1",
                    permission="read",
                    expires_at=datetime.now() - timedelta(hours=1),
                ),
            ]
        )
        session.commit()

    monkeypatch.setattr(table_analysis_files, "get_session_factory", lambda: factory)

    owner_source, _ = table_analysis_files.resolve_table_file_source(
        "uf_private",
        user_id="owner-1",
    )
    grant_source, _ = table_analysis_files.resolve_table_file_source(
        "uf_private",
        user_id="grantee-1",
    )
    assert owner_source == grant_source == str(table_path)

    for denied_user in ("stranger", "expired-1", "guest", None):
        with pytest.raises(table_analysis_files.TableFileAccessError):
            table_analysis_files.resolve_table_file_source(
                "uf_private",
                user_id=denied_user,
            )

    assert [item["file_id"] for item in table_analysis_files.list_table_files_by_ids(
        ["uf_private"],
        user_id="grantee-1",
    )] == ["uf_private"]
    with pytest.raises(table_analysis_files.TableFileAccessError):
        table_analysis_files.resolve_table_file_reference(
            str(table_path),
            user_id="stranger",
        )

    engine.dispose()
