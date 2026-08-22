"""``/conversations/{thread_id}/feedback`` 路由的 HTTP 契约测试。

覆盖：
1. 提交/覆盖（同一 message_key 幂等 upsert，不产生第二行）；
2. 输入护栏——非法 rating 422、reasons/comment/摘录超限截断；
3. 撤回删除与按会话列出（仅本人可见，跨用户隔离）；
4. 迁移 SQL 与 ORM 列集合一致，且迁移已登记到账本。
"""

from __future__ import annotations

import re
from collections.abc import AsyncIterator
from typing import Any

import httpx
import pytest
from fastapi import FastAPI
from sqlalchemy import select
from sqlalchemy.ext.asyncio import async_sessionmaker, create_async_engine

from backend.api.routers import message_feedback as feedback_router
from backend.api.routers.auth import get_current_user
from backend.db import migrate
from backend.db.connection import get_async_session
from backend.db.models import MessageFeedback, User

OWNER_ID = "user-owner"
OTHER_ID = "user-other"


def _user(user_id: str, name: str) -> User:
    return User(
        user_id=user_id,
        username=name,
        api_key=f"key-{name}",
        role="user",
        is_active=True,
    )


async def _build_app() -> tuple[FastAPI, dict[str, User], Any]:
    engine = create_async_engine("sqlite+aiosqlite:///:memory:")
    async with engine.begin() as connection:
        for table in (User.__table__, MessageFeedback.__table__):
            await connection.run_sync(table.create)
    session_factory = async_sessionmaker(engine, expire_on_commit=False)

    async with session_factory() as session:
        session.add_all([_user(OWNER_ID, "owner"), _user(OTHER_ID, "other")])
        await session.commit()

    app = FastAPI()
    app.include_router(feedback_router.router)

    identity: dict[str, User] = {"user": _user(OWNER_ID, "owner")}
    app.dependency_overrides[get_current_user] = lambda: identity["user"]

    async def _session_override() -> AsyncIterator[Any]:
        async with session_factory() as session:
            yield session

    app.dependency_overrides[get_async_session] = _session_override
    return app, identity, session_factory


@pytest.mark.asyncio
async def test_submit_is_idempotent_upsert_and_listable() -> None:
    app, _identity, session_factory = await _build_app()
    transport = httpx.ASGITransport(app=app)
    async with httpx.AsyncClient(transport=transport, base_url="http://test") as client:
        first = await client.post(
            "/conversations/thread-1/feedback",
            json={
                "message_key": "msg-1",
                "rating": "down",
                "reasons": ["答非所问", "引用有误"],
                "comment": "引用第 3 条对不上原文",
                "agent_code": "ne",
                "model_name": "qwen-test",
                "question_excerpt": "问题原文",
                "answer_excerpt": "回答原文",
            },
        )
        assert first.status_code == 200
        assert first.json()["rating"] == "down"

        second = await client.post(
            "/conversations/thread-1/feedback",
            json={"message_key": "msg-1", "rating": "up"},
        )
        assert second.status_code == 200
        assert second.json()["rating"] == "up"

        listed = await client.get("/conversations/thread-1/feedback")
        assert listed.status_code == 200
        items = listed.json()["items"]
        assert len(items) == 1
        assert items[0]["message_key"] == "msg-1"
        assert items[0]["rating"] == "up"
        # 覆盖提交未携带 reasons/comment 时清空旧值，避免残留误导聚合。
        assert items[0]["reasons"] is None

    async with session_factory() as session:
        rows = (await session.execute(select(MessageFeedback))).scalars().all()
        assert len(rows) == 1


@pytest.mark.asyncio
async def test_rating_validation_and_bounded_fields() -> None:
    app, _identity, session_factory = await _build_app()
    transport = httpx.ASGITransport(app=app)
    async with httpx.AsyncClient(transport=transport, base_url="http://test") as client:
        invalid = await client.post(
            "/conversations/thread-1/feedback",
            json={"message_key": "msg-1", "rating": "meh"},
        )
        assert invalid.status_code == 422

        oversized = await client.post(
            "/conversations/thread-1/feedback",
            json={
                "message_key": "msg-2",
                "rating": "down",
                "reasons": [f"原因{i}" * 30 for i in range(9)],
                "comment": "评" * 5000,
                "question_excerpt": "问" * 2000,
                "answer_excerpt": "答" * 5000,
            },
        )
        assert oversized.status_code == 200

    async with session_factory() as session:
        row = (
            await session.execute(
                select(MessageFeedback).where(MessageFeedback.message_key == "msg-2")
            )
        ).scalar_one()
        assert len(row.reasons) <= 5
        assert all(len(reason) <= 50 for reason in row.reasons)
        assert len(row.comment) <= 2000
        assert len(row.question_excerpt) <= 500
        assert len(row.answer_excerpt) <= 1000


@pytest.mark.asyncio
async def test_delete_and_cross_user_isolation() -> None:
    app, identity, _session_factory = await _build_app()
    transport = httpx.ASGITransport(app=app)
    async with httpx.AsyncClient(transport=transport, base_url="http://test") as client:
        submitted = await client.post(
            "/conversations/thread-1/feedback",
            json={"message_key": "msg-1", "rating": "up"},
        )
        assert submitted.status_code == 200

        # 另一个用户看不到该会话反馈。
        identity["user"] = _user(OTHER_ID, "other")
        foreign = await client.get("/conversations/thread-1/feedback")
        assert foreign.json()["items"] == []

        # 他人撤回不影响属主数据。
        foreign_delete = await client.delete(
            "/conversations/thread-1/feedback/msg-1"
        )
        assert foreign_delete.status_code == 200

        identity["user"] = _user(OWNER_ID, "owner")
        still_there = await client.get("/conversations/thread-1/feedback")
        assert len(still_there.json()["items"]) == 1

        own_delete = await client.delete("/conversations/thread-1/feedback/msg-1")
        assert own_delete.status_code == 200
        emptied = await client.get("/conversations/thread-1/feedback")
        assert emptied.json()["items"] == []


def test_migration_sql_matches_orm_and_is_tracked() -> None:
    """迁移文件执行后不可改，SQL↔ORM 列比对是唯一的事前闸门。"""
    sql_path = migrate.MIGRATIONS_DIR / "add_message_feedback_table.sql"
    sql = sql_path.read_text(encoding="utf-8")

    block = re.search(
        r"CREATE TABLE IF NOT EXISTS\s+message_feedback\s*\((.*?)\)\s*ENGINE",
        sql,
        re.S,
    )
    assert block is not None, "未找到 message_feedback 建表语句"

    sql_columns: set[str] = set()
    for line in block.group(1).splitlines():
        stripped = line.strip().rstrip(",").strip()
        if not stripped:
            continue
        if stripped.upper().startswith(("KEY ", "CONSTRAINT ", "UNIQUE ", "PRIMARY ")):
            continue
        match = re.match(r"`?([A-Za-z_][A-Za-z0-9_]*)", stripped)
        if match:
            sql_columns.add(match.group(1))

    assert sql_columns == set(MessageFeedback.__table__.columns.keys())
    assert any(
        item.version == "202608130001_add_message_feedback_table"
        for item in migrate.MIGRATIONS
    )
    # 离线完整性校验必须能覆盖新迁移（版本序、文件类型、语法）。
    manifest = migrate.validate_migration_package()
    assert "202608130001_add_message_feedback_table" in manifest


def test_feedback_routes_mounted_on_agent_role_only():
    """七轮审查 H1 回归护栏：路由本体自 8-13 起从未被任何 app 挂载。

    契约测试直接 include 路由本体测试，发现不了"漏挂载"；这里对真实
    agent 角色应用断言两条路径存在，并断言 Guest 应用不携带反馈路由。
    """
    from backend.api import guest_http_server, http_server

    def _paths(application) -> set[str]:
        return {getattr(route, "path", "") for route in application.routes}

    agent_paths = _paths(http_server.app)
    assert "/api/v2/conversations/{thread_id}/feedback" in agent_paths
    assert "/api/v2/conversations/{thread_id}/feedback/{message_key}" in agent_paths

    guest_paths = _paths(guest_http_server.app)
    assert "/api/v2/conversations/{thread_id}/feedback" not in guest_paths
    assert "/api/v2/conversations/{thread_id}/feedback/{message_key}" not in guest_paths
