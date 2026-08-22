"""``/custom-learning`` 路由的 HTTP 契约测试。

覆盖三类此前只有 helper 级护栏的行为：

1. 题目下发脱敏——``answer`` / ``analysis`` / ``reference`` 等判分依据
   不允许出现在任何客户端可见的题目响应中（quiz、include_quiz、next-action）；
2. 属主校验——他人的计划一律 404；
3. 列表响应瘦身——只有选中节点携带正文（explanation / explore_cache /
   resources），其余节点只回 content_status 标志。
"""

from __future__ import annotations

from collections.abc import AsyncIterator
from typing import Any

import httpx
import pytest
from fastapi import FastAPI
from sqlalchemy.ext.asyncio import async_sessionmaker, create_async_engine

from backend.api.routers import custom_learning as custom_learning_router
from backend.api.routers.auth import get_current_user
from backend.db.connection import get_async_session
from backend.db.models import (
    LearningNodeAttempt,
    LearningPlan,
    LearningPlanAdjustment,
    LearningPlanNode,
    User,
)

OWNER_ID = "user-owner"
OTHER_ID = "user-other"
PRIVATE_QUESTION_FIELDS = ("answer", "analysis", "reference", "reference_answer", "scoring_points")


def _owner_user() -> User:
    return User(user_id=OWNER_ID, username="owner", api_key="key-owner", role="user", is_active=True)


def _other_user() -> User:
    return User(user_id=OTHER_ID, username="other", api_key="key-other", role="user", is_active=True)


async def _build_app() -> tuple[FastAPI, dict[str, User]]:
    engine = create_async_engine("sqlite+aiosqlite:///:memory:")
    async with engine.begin() as connection:
        for table in (
            User.__table__,
            LearningPlan.__table__,
            LearningPlanNode.__table__,
            LearningNodeAttempt.__table__,
            LearningPlanAdjustment.__table__,
        ):
            await connection.run_sync(table.create)
    session_factory = async_sessionmaker(engine, expire_on_commit=False)

    async with session_factory() as session:
        session.add_all([_owner_user(), _other_user()])
        session.add(
            LearningPlan(
                plan_id="plan-1",
                user_id=OWNER_ID,
                title="脉冲形成专项",
                domain="ne",
                goal="掌握脉冲形成",
                duration_days=3,
                daily_minutes=60,
                status="active",
                current_node_id="node-1",
            )
        )
        session.add(
            LearningPlanNode(
                node_id="node-1",
                plan_id="plan-1",
                order_index=0,
                title="脉冲形成",
                subtitle="电流脉冲到电压脉冲",
                status="learning",
                explanation="这里是当前节点的完整详解正文",
                resources=[{"doc_id": "doc-1", "title": "补充资料"}],
                explore_cache={
                    "detail_content": "知识库详解正文",
                    "map": {"root": {"title": "脉冲形成"}, "children": []},
                },
                quiz_questions=[
                    {
                        "id": "q1",
                        "type": "choice",
                        "question": "选择题题干",
                        "options": [{"key": "A", "text": "对"}, {"key": "B", "text": "错"}],
                        "answer": "A",
                        "analysis": "只有服务端可见的解析",
                    },
                    {
                        "id": "q2",
                        "type": "short",
                        "question": "简答题题干",
                        "reference": "只有服务端可见的参考要点",
                    },
                ],
            )
        )
        session.add(
            LearningPlanNode(
                node_id="node-2",
                plan_id="plan-1",
                order_index=1,
                title="成形滤波",
                subtitle="平滑与成形",
                status="open",
                explanation="第二个节点的详解正文",
                explore_cache={"detail_content": "第二个节点的知识库详解"},
            )
        )
        await session.commit()

    app = FastAPI()
    app.include_router(custom_learning_router.router)

    identity: dict[str, User] = {"user": _owner_user()}
    app.dependency_overrides[get_current_user] = lambda: identity["user"]

    async def _session_override() -> AsyncIterator[Any]:
        async with session_factory() as session:
            yield session
            await session.commit()

    app.dependency_overrides[get_async_session] = _session_override
    return app, identity


def _assert_public_questions(questions: list[dict[str, Any]]) -> None:
    assert questions, "题目列表不应为空"
    for question in questions:
        assert question.get("id")
        assert question.get("question")
        for private_field in PRIVATE_QUESTION_FIELDS:
            assert private_field not in question, f"{private_field} 泄漏到了客户端响应: {question}"


@pytest.mark.asyncio
async def test_quiz_responses_never_expose_grading_fields() -> None:
    app, _identity = await _build_app()
    transport = httpx.ASGITransport(app=app)
    async with httpx.AsyncClient(transport=transport, base_url="http://test") as client:
        quiz = await client.post("/custom-learning/nodes/node-1/quiz", json={})
        assert quiz.status_code == 200
        _assert_public_questions(quiz.json()["questions"])

        node = await client.get("/custom-learning/nodes/node-1", params={"include_quiz": "true"})
        assert node.status_code == 200
        _assert_public_questions(node.json()["questions"])

        next_action = await client.post(
            "/custom-learning/plans/plan-1/next-action",
            json={"node_id": "node-1"},
        )
        assert next_action.status_code == 200
        body = next_action.json()
        assert body["action"] == "await_quiz"
        _assert_public_questions(body["result"]["questions"])


@pytest.mark.asyncio
async def test_foreign_plan_and_node_return_404() -> None:
    app, identity = await _build_app()
    identity["user"] = _other_user()
    transport = httpx.ASGITransport(app=app)
    async with httpx.AsyncClient(transport=transport, base_url="http://test") as client:
        dashboard = await client.get("/custom-learning/plans/plan-1/dashboard")
        assert dashboard.status_code == 404
        quiz = await client.post("/custom-learning/nodes/node-1/quiz", json={})
        assert quiz.status_code == 404


@pytest.mark.asyncio
async def test_dashboard_slims_non_selected_nodes() -> None:
    app, _identity = await _build_app()
    transport = httpx.ASGITransport(app=app)
    async with httpx.AsyncClient(transport=transport, base_url="http://test") as client:
        dashboard = await client.get(
            "/custom-learning/plans/plan-1/dashboard",
            params={"node_id": "node-1"},
        )
        assert dashboard.status_code == 200
        body = dashboard.json()
        nodes = {node["node_id"]: node for node in body["plan"]["nodes"]}

        selected = nodes["node-1"]
        assert selected["explanation"]
        assert selected["explore_cache"].get("detail_content")
        assert selected["resources"]

        slim = nodes["node-2"]
        assert slim["explanation"] is None
        assert slim["explore_cache"] == {}
        assert slim["resources"] == []
        assert slim["content_status"]["explanation_ready"] is True
        assert slim["content_status"]["detail_ready"] is True

        assert body["current_node"]["node_id"] == "node-1"
        assert body["current_node"]["explanation"]
