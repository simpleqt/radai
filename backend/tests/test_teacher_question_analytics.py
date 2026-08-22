"""教师提问分析路由的 HTTP 契约测试。

覆盖：
1. 角色门禁——user/student 403，teacher/admin 放行，agent_code 非 fmca 422；
2. 提问列表/总览（仅 fmca 数据可见）；
3. 报告生命周期——创建 running → 后台完成、窗口校验、单飞约束、删除与 404；
4. 迁移 SQL↔ORM 列比对 + 账本登记。
"""

from __future__ import annotations

import re
from collections.abc import AsyncIterator
from contextlib import asynccontextmanager
from typing import Any

import httpx
import pytest
from fastapi import FastAPI
from sqlalchemy import select
from sqlalchemy.ext.asyncio import async_sessionmaker, create_async_engine

from backend.api.dependencies.auth import ApiKeyAuth, verify_api_key
from backend.api.routers import teacher_analytics as ta_router
from backend.db import migrate
from backend.db.connection import get_async_session
from backend.db.models import (
    CourseQuestionRecord,
    CourseQuestionReport,
    QuestionBankAttempt,
    QuestionBankQuestion,
    User,
)

TEACHER_ID = "teacher-1"
STUDENT_ID = "student-1"
ADMIN_ID = "admin-1"


def _auth(user_id: str, role: str) -> ApiKeyAuth:
    return ApiKeyAuth(api_key=f"key-{user_id}", user_id=user_id, role=role)


async def _build_app() -> tuple[FastAPI, dict[str, ApiKeyAuth], Any]:
    engine = create_async_engine("sqlite+aiosqlite:///:memory:")
    async with engine.begin() as connection:
        for table in (
            User.__table__,
            CourseQuestionRecord.__table__,
            CourseQuestionReport.__table__,
            # /insights 现包含练习维度统计（题库为空时也要能查询）。
            QuestionBankQuestion.__table__,
            QuestionBankAttempt.__table__,
        ):
            await connection.run_sync(table.create)
    session_factory = async_sessionmaker(engine, expire_on_commit=False)

    async with session_factory() as session:
        session.add_all(
            [
                User(
                    user_id=TEACHER_ID,
                    username="teacher",
                    api_key="key-t",
                    role="teacher",
                    is_active=True,
                ),
                User(
                    user_id=STUDENT_ID,
                    username="student",
                    api_key="key-s",
                    role="user",
                    is_active=True,
                ),
            ]
        )
        await session.commit()

    app = FastAPI()
    app.include_router(ta_router.router)

    # 审计写入依赖 MySQL AUTO_INCREMENT 的 BIGINT 主键，SQLite 无法自增；
    # 本测试聚焦路由契约，审计副作用以 no-op 隔离。
    async def _noop_audit(*args: Any, **kwargs: Any) -> None:
        return None

    ta_router.write_admin_audit = _noop_audit  # type: ignore[method-assign]

    identity: dict[str, ApiKeyAuth] = {"auth": _auth(TEACHER_ID, "teacher")}
    # 覆盖底层认证，让 require_teacher 依赖在其上执行真实角色校验。
    app.dependency_overrides[verify_api_key] = lambda: identity["auth"]

    async def _session_override() -> AsyncIterator[Any]:
        async with session_factory() as session:
            yield session

    app.dependency_overrides[get_async_session] = _session_override
    return app, identity, session_factory


@pytest.mark.asyncio
async def test_role_gate_and_agent_code_scope() -> None:
    app, identity, _sf = await _build_app()
    transport = httpx.ASGITransport(app=app)
    async with httpx.AsyncClient(transport=transport, base_url="http://test") as client:
        identity["auth"] = _auth(STUDENT_ID, "user")
        forbidden = await client.get("/teacher/analytics/overview")
        assert forbidden.status_code == 403

        identity["auth"] = _auth(STUDENT_ID, "student")
        forbidden_student = await client.get("/teacher/analytics/overview")
        assert forbidden_student.status_code == 403

        identity["auth"] = _auth(TEACHER_ID, "teacher")
        ok = await client.get("/teacher/analytics/overview")
        assert ok.status_code == 200

        identity["auth"] = _auth(ADMIN_ID, "admin")
        ok_admin = await client.get("/teacher/analytics/overview")
        assert ok_admin.status_code == 200

        wrong_agent = await client.get(
            "/teacher/analytics/overview", params={"agent_code": "ne"}
        )
        assert wrong_agent.status_code == 422


@pytest.mark.asyncio
async def test_questions_list_and_overview() -> None:
    app, identity, session_factory = await _build_app()
    async with session_factory() as session:
        session.add(
            CourseQuestionRecord(
                id="q-1",
                user_id=STUDENT_ID,
                username="student",
                agent_code="fmca",
                thread_id="thread-1",
                question="什么是合力与分力？",
                question_len=10,
            )
        )
        session.add(
            CourseQuestionRecord(
                id="q-2",
                user_id=STUDENT_ID,
                username="student",
                agent_code="fmca",
                thread_id="thread-2",
                question="载荷 50% 处的弯矩怎么求",
                question_len=14,
            )
        )
        session.add(
            CourseQuestionRecord(
                id="q-3",
                user_id=STUDENT_ID,
                username="student",
                agent_code="ne",
                thread_id="thread-3",
                question="NE 智能体的提问不应被统计",
                question_len=16,
            )
        )
        await session.commit()

    transport = httpx.ASGITransport(app=app)
    async with httpx.AsyncClient(transport=transport, base_url="http://test") as client:
        listed = await client.get("/teacher/analytics/questions")
        assert listed.status_code == 200
        body = listed.json()
        assert body["total"] == 2
        assert body["items"][0]["agent_code"] == "fmca"

        overview = await client.get("/teacher/analytics/overview")
        assert overview.status_code == 200
        assert overview.json()["total"] == 2

        # LIKE 通配符按字面匹配：搜索 "%" 只命中含字面百分号的行。
        literal = await client.get(
            "/teacher/analytics/questions", params={"keyword": "%"}
        )
        assert literal.status_code == 200
        assert literal.json()["total"] == 1
        assert "50%" in literal.json()["items"][0]["question"]

        keyword = await client.get(
            "/teacher/analytics/questions", params={"keyword": "合力"}
        )
        assert keyword.json()["total"] == 1

        wrong_agent = await client.get(
            "/teacher/analytics/questions", params={"agent_code": "ne"}
        )
        assert wrong_agent.status_code == 422


@pytest.mark.asyncio
async def test_report_window_validation_and_creation(monkeypatch: pytest.MonkeyPatch) -> None:
    # 阻止后台任务触碰真实数据库：测试 SQLite 会话与全局 async_session_context
    # 不是同一连接，补一个占位实现；同时清掉进程内冷却状态。
    from backend.application.question_analytics import service as qa_service

    async def _noop_generation(report_id: str) -> None:
        return None

    monkeypatch.setattr(qa_service, "_run_generation", _noop_generation)
    monkeypatch.setattr(qa_service, "_USER_LAST_CREATE", {})

    app, identity, session_factory = await _build_app()
    transport = httpx.ASGITransport(app=app)
    async with httpx.AsyncClient(transport=transport, base_url="http://test") as client:
        inverted = await client.post(
            "/teacher/analytics/reports",
            json={"from": "2026-08-15T00:00:00", "to": "2026-08-01T00:00:00"},
        )
        assert inverted.status_code == 422

        created = await client.post(
            "/teacher/analytics/reports",
            json={"from": "2026-08-01T00:00:00", "to": "2026-08-15T00:00:00"},
        )
        assert created.status_code == 201
        assert created.json()["status"] == "running"

        # 同一教师冷却期内再创建 → 429。
        second = await client.post(
            "/teacher/analytics/reports",
            json={"from": "2026-08-01T00:00:00", "to": "2026-08-15T00:00:00"},
        )
        assert second.status_code == 429

        # 另一位教师不受个人冷却影响，但被全局单飞（running 存在）拒绝 → 409。
        identity["auth"] = _auth(TEACHER_ID + "-b", "teacher")
        third = await client.post(
            "/teacher/analytics/reports",
            json={"from": "2026-08-01T00:00:00", "to": "2026-08-15T00:00:00"},
        )
        assert third.status_code == 409

        report_id = created.json()["id"]

        # 属主隔离：另一位教师看不到、也删不掉他人报告（404），列表也不可见。
        foreign_detail = await client.get(f"/teacher/analytics/reports/{report_id}")
        assert foreign_detail.status_code == 404
        foreign_delete = await client.delete(f"/teacher/analytics/reports/{report_id}")
        assert foreign_delete.status_code == 404
        foreign_list = await client.get("/teacher/analytics/reports")
        assert all(item["id"] != report_id for item in foreign_list.json()["items"])

        # 属主可见可删。
        identity["auth"] = _auth(TEACHER_ID, "teacher")
        detail = await client.get(f"/teacher/analytics/reports/{report_id}")
        assert detail.status_code == 200
        assert "result" in detail.json()

        # admin 可见他人报告。
        identity["auth"] = _auth(ADMIN_ID, "admin")
        admin_detail = await client.get(f"/teacher/analytics/reports/{report_id}")
        assert admin_detail.status_code == 200

        identity["auth"] = _auth(TEACHER_ID, "teacher")
        deleted = await client.delete(f"/teacher/analytics/reports/{report_id}")
        assert deleted.status_code == 200

        gone = await client.get(f"/teacher/analytics/reports/{report_id}")
        assert gone.status_code == 404


@pytest.mark.asyncio
async def test_stale_running_report_is_recovered(monkeypatch: pytest.MonkeyPatch) -> None:
    """进程重启遗留的 running 报告在新建前被自动置 failed，不再永久 409。"""
    from datetime import datetime, timedelta

    from backend.application.question_analytics import service as qa_service

    async def _noop_generation(report_id: str) -> None:
        return None

    monkeypatch.setattr(qa_service, "_run_generation", _noop_generation)
    monkeypatch.setattr(qa_service, "_USER_LAST_CREATE", {})

    app, identity, session_factory = await _build_app()
    async with session_factory() as session:
        session.add(
            CourseQuestionReport(
                id="stale-report",
                agent_code="fmca",
                created_by="ghost",
                range_start=datetime(2026, 7, 1),
                range_end=datetime(2026, 7, 30),
                question_count=0,
                status="running",
                created_at=datetime.now() - timedelta(hours=3),
            )
        )
        await session.commit()

    transport = httpx.ASGITransport(app=app)
    async with httpx.AsyncClient(transport=transport, base_url="http://test") as client:
        created = await client.post(
            "/teacher/analytics/reports",
            json={"from": "2026-08-01T00:00:00", "to": "2026-08-15T00:00:00"},
        )
        assert created.status_code == 201

    async with session_factory() as session:
        stale = await session.get(CourseQuestionReport, "stale-report")
        assert stale is not None
        assert stale.status == "failed"
        assert stale.finished_at is not None


@pytest.mark.asyncio
async def test_audit_failure_does_not_break_reads(monkeypatch: pytest.MonkeyPatch) -> None:
    """审计写入失败（如只读会话）必须降级，读接口仍返回 200。"""
    app, identity, _session_factory = await _build_app()

    async def _broken_audit(*args: Any, **kwargs: Any) -> None:
        raise RuntimeError("read-only session")

    monkeypatch.setattr(ta_router, "write_admin_audit", _broken_audit)
    transport = httpx.ASGITransport(app=app)
    async with httpx.AsyncClient(transport=transport, base_url="http://test") as client:
        listed = await client.get("/teacher/analytics/questions")
        assert listed.status_code == 200
        overview = await client.get("/teacher/analytics/overview")
        assert overview.status_code == 200


def test_collect_gate_and_keyword_filter() -> None:
    from backend.application.question_analytics.collector import (
        _filter_stopwords,
        should_collect_question,
    )

    assert should_collect_question("fmca", "user") is True
    assert should_collect_question("fmca", "student") is True
    assert should_collect_question("fmca", "teacher") is False
    assert should_collect_question("fmca", "admin") is False
    assert should_collect_question("ne", "user") is False

    # 功能字噪声 bigram 被过滤，实义力学词保留。
    tokens = _filter_stopwords(
        ["合力", "分解", "分力", "么是", "是合", "力与", "何分", "第二", "定律", "ma"]
    )
    assert "合力" in tokens
    assert "分力" in tokens
    assert "分解" in tokens
    assert not any(t in {"么是", "是合", "力与", "何分"} for t in tokens)


def test_uniform_sample_ids_and_cluster_samples() -> None:
    from backend.application.question_analytics.service import (
        _cluster_samples,
        uniform_sample_ids,
    )

    ids = [f"id-{i}" for i in range(101)]
    sampled = uniform_sample_ids(ids, 10)
    assert len(sampled) == 10
    # 均匀覆盖：首尾保留，间隔约 10。
    assert sampled[0] == "id-0"
    assert sampled[-1] == "id-100"
    assert len(set(sampled)) == 10
    # 不超限时原样返回。
    assert uniform_sample_ids(ids[:3], 10) == ids[:3]

    clusters = [
        {
            "label": "合力",
            "count": 2,
            "ids": ["a", "b", "ghost"],
            "keywords": [],
        }
    ]
    _cluster_samples(clusters, {"a": "问A" * 200, "b": "问B"})
    assert clusters[0]["samples"] == ["问A" * 100, "问B"]  # 截断到 200 字
    # 不存在的 id 被跳过，不会进样本。


@pytest.mark.asyncio
async def test_record_question_stores_real_student_name(monkeypatch: pytest.MonkeyPatch) -> None:
    """学生姓名不匿名：落库时优先 display_name，否则 username，且不截断成单字。"""
    import backend.db.connection as db_connection
    from backend.application.question_analytics.collector import record_question

    engine = create_async_engine("sqlite+aiosqlite:///:memory:")
    async with engine.begin() as connection:
        await connection.run_sync(User.__table__.create)
        await connection.run_sync(CourseQuestionRecord.__table__.create)
    session_factory = async_sessionmaker(engine, expire_on_commit=False)

    async with session_factory() as session:
        session.add(
            User(
                user_id="stu-1",
                username="zhangsan",
                display_name="张三",
                api_key="key-stu-1",
                role="user",
                is_active=True,
            )
        )
        await session.commit()

    @asynccontextmanager
    async def _session_ctx():
        async with session_factory() as session:
            yield session

    monkeypatch.setattr(db_connection, "async_session_context", _session_ctx)

    await record_question(
        question="什么是合力？",
        user_id="stu-1",
        agent_code="fmca",
        role="user",
        thread_id="t-1",
        workflow=None,
        model_name=None,
    )

    async with session_factory() as session:
        row = (await session.execute(select(CourseQuestionRecord))).scalars().one()
        assert row.username == "张三"


@pytest.mark.asyncio
async def test_conversation_view_is_fmca_scoped(monkeypatch: pytest.MonkeyPatch) -> None:
    """完整对话查看：仅 fmca 提问记录存在的会话可读，其他一律 404。"""
    from langchain_core.messages import AIMessage, HumanMessage

    import backend.application.checkpointing.checkpointer as checkpointer_module

    class _FakeCheckpointer:
        async def aget(self, config):
            assert config["configurable"]["thread_id"].startswith("u_")
            return {
                "channel_values": {
                    "messages": [
                        HumanMessage(content="什么是合力？"),
                        AIMessage(content="合力是多个力的矢量和。"),
                    ]
                }
            }

    monkeypatch.setattr(
        checkpointer_module,
        "get_checkpointer_from_request",
        lambda request: _FakeCheckpointer(),
    )

    app, identity, session_factory = await _build_app()
    async with session_factory() as session:
        session.add(
            CourseQuestionRecord(
                id="q-conv-1",
                user_id=STUDENT_ID,
                username="student",
                agent_code="fmca",
                thread_id="fmca-thread-1",
                question="什么是合力？",
                question_len=6,
            )
        )
        # 其他智能体的提问：采集门禁保证不会入库，但即使有人手动塞入
        # agent_code=ne 的行，fmca 硬过滤也不会放行。
        session.add(
            CourseQuestionRecord(
                id="q-conv-2",
                user_id=STUDENT_ID,
                username="student",
                agent_code="ne",
                thread_id="ne-thread-1",
                question="NE 的问题",
                question_len=6,
            )
        )
        await session.commit()

    transport = httpx.ASGITransport(app=app)
    async with httpx.AsyncClient(transport=transport, base_url="http://test") as client:
        # fmca 会话：教师可读，返回完整消息。
        ok = await client.get(
            "/teacher/analytics/conversations/fmca-thread-1",
            params={"user_id": STUDENT_ID},
        )
        assert ok.status_code == 200
        body = ok.json()
        assert body["username"] == "student"
        turns = body["turns"]
        assert len(turns) == 1
        assert turns[0]["question"] == "什么是合力？"
        assert turns[0]["repeat_count"] == 1
        assert turns[0]["has_answer"] is True
        assert [answer["kind"] for answer in turns[0]["answers"]] == ["answer"]
        assert turns[0]["answers"][0]["content"] == "合力是多个力的矢量和。"

        # 其他智能体/不存在的会话：404，不泄露存在性。
        ne_thread = await client.get(
            "/teacher/analytics/conversations/ne-thread-1",
            params={"user_id": STUDENT_ID},
        )
        assert ne_thread.status_code == 404

        unknown = await client.get(
            "/teacher/analytics/conversations/fmca-thread-1",
            params={"user_id": "someone-else"},
        )
        assert unknown.status_code == 404

        # 访客会话：checkpoint 在 Guest API 进程，明确不支持。
        guest = await client.get(
            "/teacher/analytics/conversations/fmca-thread-1",
            params={"user_id": "guest_abc"},
        )
        assert guest.status_code == 404

        # 学生角色 403。
        identity["auth"] = _auth(STUDENT_ID, "user")
        forbidden = await client.get(
            "/teacher/analytics/conversations/fmca-thread-1",
            params={"user_id": STUDENT_ID},
        )
        assert forbidden.status_code == 403


@pytest.mark.asyncio
async def test_conversation_turns_grouping(monkeypatch: pytest.MonkeyPatch) -> None:
    """对话按问答轮次重组：重复合并、过程/报错与最终回答分离。"""
    from langchain_core.messages import AIMessage, HumanMessage, ToolMessage

    import backend.application.checkpointing.checkpointer as checkpointer_module

    question_1 = "请就静力学部分出一套测试题，总分100。"
    error_text = (
        "本轮已执行检索和工具调用，但模型最终答案没有正常收尾，"
        "系统已停止继续等待。请重新发起本问题。"
    )

    class _FakeCheckpointer:
        async def aget(self, config):
            return {
                "channel_values": {
                    "messages": [
                        HumanMessage(content=question_1),
                        ToolMessage(content="tool result", tool_call_id="t1"),
                        AIMessage(content="现在让我来生成所需的图表，并搜索相关的知识库图片。"),
                        AIMessage(content=""),
                        # 失败重试：同一问题连续再发两次
                        HumanMessage(content=question_1),
                        HumanMessage(content=question_1),
                        AIMessage(content=f"\n\n{error_text}"),
                        HumanMessage(content="请输出修改后的测试题"),
                        AIMessage(content="以下是修改后的测试题：……"),
                        # 报错兜底文案混在正文尾部：应剥离为独立 error 条目
                        HumanMessage(content="再出一道摩擦题"),
                        AIMessage(content=f"摩擦题如下：……\n\n{error_text}"),
                    ]
                }
            }

    monkeypatch.setattr(
        checkpointer_module,
        "get_checkpointer_from_request",
        lambda request: _FakeCheckpointer(),
    )

    app, identity, session_factory = await _build_app()
    async with session_factory() as session:
        session.add(
            CourseQuestionRecord(
                id="q-turns-1",
                user_id=STUDENT_ID,
                username="student",
                agent_code="fmca",
                thread_id="fmca-thread-turns",
                question=question_1,
                question_len=len(question_1),
            )
        )
        await session.commit()

    transport = httpx.ASGITransport(app=app)
    async with httpx.AsyncClient(transport=transport, base_url="http://test") as client:
        response = await client.get(
            "/teacher/analytics/conversations/fmca-thread-turns",
            params={"user_id": STUDENT_ID},
        )
        assert response.status_code == 200
        turns = response.json()["turns"]
        assert len(turns) == 3

        # 轮 1：三次重复合并为一次；只有过程播报与报错，无有效回答。
        first = turns[0]
        assert first["question"] == question_1
        assert first["repeat_count"] == 3
        assert first["has_answer"] is False
        assert [answer["kind"] for answer in first["answers"]] == [
            "process",
            "error",
        ]

        # 轮 2：单一最终回答。
        second = turns[1]
        assert second["question"] == "请输出修改后的测试题"
        assert second["repeat_count"] == 1
        assert second["has_answer"] is True
        assert [answer["kind"] for answer in second["answers"]] == ["answer"]

        # 轮 3：正文尾部混排的报错文案被剥离，正文保留为 answer。
        third = turns[2]
        assert third["question"] == "再出一道摩擦题"
        assert third["has_answer"] is True
        kinds = [answer["kind"] for answer in third["answers"]]
        assert kinds == ["answer", "error"]
        assert third["answers"][0]["content"] == "摩擦题如下：……"
        assert third["answers"][1]["content"].startswith("本轮已执行检索和工具调用")


@pytest.mark.asyncio
async def test_insights_prebuilt_dashboard_data() -> None:
    """预制大屏数据：进入即见（默认 30 天窗），教师可读、学生 403、天数有界。"""
    app, identity, session_factory = await _build_app()
    async with session_factory() as session:
        session.add(
            CourseQuestionRecord(
                id="q-ins-1",
                user_id=STUDENT_ID,
                username="student",
                agent_code="fmca",
                thread_id="t-1",
                question="什么是合力与分力？",
                question_len=10,
            )
        )
        session.add(
            CourseQuestionRecord(
                id="q-ins-2",
                user_id=STUDENT_ID,
                username="student",
                agent_code="fmca",
                thread_id="t-2",
                question="什么是合力与分力!",
                question_len=10,
            )
        )
        await session.commit()

    transport = httpx.ASGITransport(app=app)
    async with httpx.AsyncClient(transport=transport, base_url="http://test") as client:
        data = await client.get("/teacher/analytics/insights")
        assert data.status_code == 200
        body = data.json()
        assert body["days"] == 30
        assert body["overview"]["total"] == 2
        assert body["question_count"] == 2
        # 归一化后两条相同提问合并为一个高频问题。
        assert any(item["count"] == 2 for item in body["top_questions"])
        assert isinstance(body["trend"], list)
        assert isinstance(body["top_keywords"], list)

        bounded = await client.get("/teacher/analytics/insights", params={"days": 5})
        assert bounded.status_code == 422

        custom = await client.get("/teacher/analytics/insights", params={"days": 7})
        assert custom.status_code == 200
        assert custom.json()["days"] == 7

        identity["auth"] = _auth(STUDENT_ID, "user")
        forbidden = await client.get("/teacher/analytics/insights")
        assert forbidden.status_code == 403


def test_migration_sql_matches_orm_and_is_tracked() -> None:
    sql_path = migrate.MIGRATIONS_DIR / "add_teacher_question_analytics.sql"
    sql = sql_path.read_text(encoding="utf-8")

    orm_tables = {
        "course_question_records": CourseQuestionRecord.__table__,
        "course_question_reports": CourseQuestionReport.__table__,
    }
    for table_name, table in orm_tables.items():
        block = re.search(
            rf"CREATE TABLE IF NOT EXISTS\s+{table_name}\s*\((.*?)\)\s*ENGINE",
            sql,
            re.S,
        )
        assert block is not None, f"未找到 {table_name} 建表语句"

        sql_columns: set[str] = set()
        for line in block.group(1).splitlines():
            stripped = line.strip().rstrip(",").strip()
            if not stripped:
                continue
            if stripped.upper().startswith(
                ("KEY ", "CONSTRAINT ", "UNIQUE ", "PRIMARY ")
            ):
                continue
            match = re.match(r"`?([A-Za-z_][A-Za-z0-9_]*)", stripped)
            if match:
                sql_columns.add(match.group(1))
        assert sql_columns == set(table.columns.keys())

    assert any(
        item.version == "202608150001_add_teacher_question_analytics"
        for item in migrate.MIGRATIONS
    )
    manifest = migrate.validate_migration_package()
    assert "202608150001_add_teacher_question_analytics" in manifest


def test_user_id_widen_migration_tracked_and_matches_orm() -> None:
    """访客 user_id（guest_+32hex=38 字符）超 VARCHAR(36)，加宽迁移必须登记且与 ORM 一致。"""
    orm_length = CourseQuestionRecord.__table__.columns["user_id"].type.length
    assert orm_length == 64

    sql_path = migrate.MIGRATIONS_DIR / "widen_course_question_user_id.sql"
    sql = sql_path.read_text(encoding="utf-8")
    assert re.search(
        r"MODIFY\s+COLUMN\s+user_id\s+VARCHAR\(64\)\s+NOT NULL",
        sql,
        re.IGNORECASE,
    ), "加宽迁移必须 MODIFY user_id 到 VARCHAR(64) NOT NULL"

    assert any(
        item.version == "202608170001_widen_course_question_user_id"
        for item in migrate.MIGRATIONS
    )
    manifest = migrate.validate_migration_package()
    assert "202608170001_widen_course_question_user_id" in manifest


def test_guest_user_id_length_fits_column() -> None:
    """端到端护栏：38 字符访客 id 必须能放进 ORM 列宽。"""
    guest_user_id = f"guest_{'a' * 32}"
    length = CourseQuestionRecord.__table__.columns["user_id"].type.length
    assert len(guest_user_id) <= length


# ── 题库/练习融合分析（2026-08-17 增量） ──────────────────────────────────


def _practice_test_tables() -> tuple:
    from backend.db.models import (
        QuestionBankAttempt,
        QuestionBankQuestion,
    )
    return QuestionBankAttempt, QuestionBankQuestion


@pytest.mark.asyncio
async def test_practice_stats_and_cross_insights() -> None:
    """练习维度统计 + 提问×做题交叉分类（确定性，无 LLM）。"""
    from datetime import datetime, timedelta

    from backend.application.question_analytics.practice import (
        cross_insights,
        practice_deterministic_stats,
    )

    QuestionBankAttempt, QuestionBankQuestion = _practice_test_tables()
    engine = create_async_engine("sqlite+aiosqlite:///:memory:")
    async with engine.begin() as connection:
        await connection.run_sync(QuestionBankQuestion.__table__.create)
        await connection.run_sync(QuestionBankAttempt.__table__.create)
    session_factory = async_sessionmaker(engine, expire_on_commit=False)

    now = datetime.now()
    async with session_factory() as session:
        session.add_all(
            [
                QuestionBankQuestion(
                    id="pq-1",
                    question_type="选择题",
                    stem="平面力系平衡方程",
                    answer="A",
                    difficulty="easy",
                    chapter="第 3 章",
                    knowledge_points_json=["平面力系的平衡"],
                    stem_hash="ph1",
                    created_by="t",
                ),
                QuestionBankQuestion(
                    id="pq-2",
                    question_type="计算题",
                    stem="应力圆绘制",
                    answer="x",
                    difficulty="hard",
                    knowledge_points_json=["应力状态分析"],
                    stem_hash="ph2",
                    created_by="t",
                ),
            ]
        )
        # 平衡：3 错 1 对（错误率 0.75 ≥ 0.4 且 ≥3 次 → weak）。
        for index in range(4):
            session.add(
                QuestionBankAttempt(
                    id=f"pa-{index}",
                    user_id=f"stu-{index}",
                    question_id="pq-1",
                    answer_json={"answer": "B"},
                    is_correct=index == 0,
                )
            )
        # 应力：3 错（weak）。
        for index in range(3):
            session.add(
                QuestionBankAttempt(
                    id=f"pb-{index}",
                    user_id=f"stu-{index}",
                    question_id="pq-2",
                    answer_json={"answer": "wrong"},
                    is_correct=False,
                )
            )
        await session.commit()

        stats = await practice_deterministic_stats(
            session,
            range_start=now - timedelta(days=7),
            range_end=now + timedelta(minutes=1),
        )
        assert stats["attempt_count"] == 7
        assert stats["graded_count"] == 7
        assert stats["active_students"] == 4
        assert stats["correct_rate"] == round(1 / 7, 4)
        kp_stats = {item["knowledge_point"]: item for item in stats["by_knowledge_point"]}
        assert kp_stats["平面力系的平衡"]["error_rate"] == 0.75
        assert stats["top_wrong_questions"][0]["question_id"] in {"pq-1", "pq-2"}

        # 学生提问里提到了「平面力系的平衡」→ asked_and_wrong；
        # 「应力状态分析」没人提 → wrong_only（沉默薄弱点）。
        question_texts = ["老师，平面力系的平衡方程怎么列？", "摩擦系数怎么取值？"]
        cross = cross_insights(stats, question_texts=question_texts)
        categories = {item["knowledge_point"]: item["category"] for item in cross}
        assert categories["平面力系的平衡"] == "asked_and_wrong"
        assert categories["应力状态分析"] == "wrong_only"
        asked_only_present = [item for item in cross if item["category"] == "asked_only"]
        assert not asked_only_present  # 样本里没有第三类


@pytest.mark.asyncio
async def test_report_generation_includes_practice_fusion(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """报告生成把练习统计/交叉洞察/推荐习题并入 result_json；空题库时降级占位。"""
    from datetime import datetime, timedelta

    import backend.db.connection as db_connection
    from backend.application.question_analytics import service as qa_service
    from backend.db.models import (
        CourseQuestionRecord,
        CourseQuestionReport,
        QuestionBankQuestion,
    )

    QuestionBankAttempt, _QuestionBankQuestion = _practice_test_tables()
    engine = create_async_engine("sqlite+aiosqlite:///:memory:")
    async with engine.begin() as connection:
        for table in (
            CourseQuestionRecord.__table__,
            CourseQuestionReport.__table__,
            QuestionBankQuestion.__table__,
            QuestionBankAttempt.__table__,
        ):
            await connection.run_sync(table.create)
    session_factory = async_sessionmaker(engine, expire_on_commit=False)

    now = datetime.now()
    async with session_factory() as session:
        session.add(
            CourseQuestionRecord(
                id="qr-1",
                user_id="stu-1",
                username="stu",
                agent_code="fmca",
                thread_id="t-1",
                question="平面力系的平衡方程怎么列？",
                question_len=12,
            )
        )
        session.add(
            QuestionBankQuestion(
                id="pq-1",
                question_type="选择题",
                stem="平衡方程",
                answer="A",
                difficulty="easy",
                knowledge_points_json=["平面力系的平衡"],
                stem_hash="ph1",
                created_by="t",
            )
        )
        for index in range(3):
            session.add(
                QuestionBankAttempt(
                    id=f"pa-{index}",
                    user_id=f"stu-{index}",
                    question_id="pq-1",
                    answer_json={"answer": "B"},
                    is_correct=False,
                )
            )
        session.add(
            CourseQuestionReport(
                id="report-fusion",
                agent_code="fmca",
                created_by="teacher-1",
                range_start=now - timedelta(days=7),
                # 终点后移 1 分钟：落库行的 created_at 在测试取 now() 之后。
                range_end=now + timedelta(minutes=1),
                question_count=0,
                status="running",
            )
        )
        await session.commit()

    @asynccontextmanager
    async def _session_ctx():
        async with session_factory() as session:
            yield session

    monkeypatch.setattr(db_connection, "async_session_context", _session_ctx)

    async def _offline_clustering(questions, *, weak_points=None):
        # 离线桩：不触模型/数据库，直接给可用形状（真实降级路径由 LLM 失败分支覆盖）。
        return {
            "clusters": [
                {
                    "label": "平衡方程",
                    "count": len(questions),
                    "ids": [q["id"] for q in questions],
                    "keywords": [],
                }
            ],
            "summary": "离线桩总结。",
            "llm_status": "ok",
            "model_name": "stub",
        }

    monkeypatch.setattr(qa_service.clustering, "run_llm_clustering", _offline_clustering)

    await qa_service._run_generation("report-fusion")

    async with session_factory() as session:
        report = await session.get(CourseQuestionReport, "report-fusion")
        assert report is not None
        assert report.status == "done"
        result = report.result_json
        assert report.question_count == 1
        # 练习融合章节。
        practice = result["practice_summary"]
        assert practice["attempt_count"] == 3
        cross = result["cross_insights"]
        assert cross and cross[0]["category"] == "asked_and_wrong"
        recommended = result["recommended_exercises"]
        assert recommended
        assert recommended[0]["knowledge_point"] == "平面力系的平衡"
        assert recommended[0]["questions"][0]["id"] == "pq-1"


@pytest.mark.asyncio
async def test_insights_endpoint_includes_practice(monkeypatch: pytest.MonkeyPatch) -> None:
    """insights 大屏带练习维度；题库为空时 empty 占位（提问分析独立可用）。"""

    from backend.db.models import QuestionBankAttempt, QuestionBankQuestion

    engine = create_async_engine("sqlite+aiosqlite:///:memory:")
    async with engine.begin() as connection:
        for table in (
            User.__table__,
            CourseQuestionRecord.__table__,
            CourseQuestionReport.__table__,
            QuestionBankQuestion.__table__,
            QuestionBankAttempt.__table__,
        ):
            await connection.run_sync(table.create)
    session_factory = async_sessionmaker(engine, expire_on_commit=False)
    async with session_factory() as session:
        session.add_all(
            [
                User(user_id=TEACHER_ID, username="t", api_key="k", role="teacher", is_active=True),
                CourseQuestionRecord(
                    id="q-p1",
                    user_id=STUDENT_ID,
                    username="s",
                    agent_code="fmca",
                    thread_id="t",
                    question="什么是合力？",
                    question_len=6,
                ),
            ]
        )
        await session.commit()

    app = FastAPI()
    app.include_router(ta_router.router)

    async def _noop_audit(*args: Any, **kwargs: Any) -> None:
        return None

    monkeypatch.setattr(ta_router, "write_admin_audit", _noop_audit)
    identity: dict[str, ApiKeyAuth] = {"auth": _auth(TEACHER_ID, "teacher")}
    app.dependency_overrides[verify_api_key] = lambda: identity["auth"]

    async def _session_override() -> AsyncIterator[Any]:
        async with session_factory() as session:
            yield session

    app.dependency_overrides[get_async_session] = _session_override

    transport = httpx.ASGITransport(app=app)
    async with httpx.AsyncClient(transport=transport, base_url="http://test") as client:
        empty = await client.get("/teacher/analytics/insights")
        assert empty.status_code == 200
        body = empty.json()
        assert body["practice"]["empty"] is True
        assert body["practice"]["attempt_count"] == 0

        practice_overview = await client.get("/teacher/analytics/practice/overview")
        assert practice_overview.status_code == 200
        assert practice_overview.json()["stats"]["empty"] is True
        assert practice_overview.json()["recommended"] == []


def test_merge_clusters_drops_empty_after_hallucination_filter() -> None:
    """全幻觉 id 的簇不再以 count=0 进入排序与 summary。"""
    from backend.application.question_analytics.clustering import _merge_clusters

    merged = _merge_clusters(
        [
            [
                {"label": "纯幻觉簇", "ids": ["ghost-1", "ghost-2"], "keywords": ["x"]},
                {
                    "label": "真实簇",
                    "ids": ["q1", "q2", "ghost-1"],
                    "keywords": ["y"],
                },
                # 跨簇重复 id 只计一次
                {"label": "真实簇", "ids": ["q2"], "keywords": ["z"]},
            ]
        ],
        valid_ids={"q1", "q2"},
    )
    assert [c["label"] for c in merged] == ["真实簇"]
    assert merged[0]["count"] == 2
    assert merged[0]["ids"] == ["q1", "q2"]
