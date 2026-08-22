"""学生练习路由的 HTTP 契约测试。

覆盖：
1. 脱敏纪律——题目下发不含 answer/analysis，判分提交后才返回；
2. 判分规则——选择题精确匹配、填空数值容差、主观题自评；
3. 做题记录属主隔离与错题本；
4. 未发布题目 404（不泄露存在性）；
5. qb-image 签名代理——密钥为空 fail-closed、有效签名经 fetch 服务。
"""

from __future__ import annotations

from collections.abc import AsyncIterator
from datetime import datetime, timedelta
from typing import Any

import httpx
import pytest
from fastapi import FastAPI
from sqlalchemy.ext.asyncio import async_sessionmaker, create_async_engine

from backend.api.dependencies.auth import ApiKeyAuth, verify_api_key
from backend.api.routers import practice as practice_router
from backend.api.routers import qb_images as qb_images_router
from backend.db.connection import get_async_session
from backend.db.models import (
    QuestionBankAttempt,
    QuestionBankQuestion,
    User,
)

STUDENT_ID = "stu-practice"
OTHER_ID = "stu-other"

PNG_BYTES = b"\x89PNG\r\n\x1a\nfakeimage"


@pytest.fixture(autouse=True)
def _relax_visual_rate_limit(monkeypatch: pytest.MonkeyPatch) -> None:
    """视觉判分限流在测试里放宽：强制进程内桶 + 提高上限，避免真实 Redis
    污染与跨用例累计导致的偶发 429。"""
    monkeypatch.setenv("HTTP_RATE_LIMIT_REDIS_ENABLED", "false")
    monkeypatch.setattr(practice_router, "_VISUAL_RATE_PER_MINUTE", 1000)
    practice_router.request_rate_limiter._buckets.clear()


def _auth(user_id: str, role: str = "user") -> ApiKeyAuth:
    return ApiKeyAuth(api_key=f"key-{user_id}", user_id=user_id, role=role)


async def _build_app() -> tuple[FastAPI, dict[str, ApiKeyAuth], async_sessionmaker]:
    engine = create_async_engine("sqlite+aiosqlite:///:memory:")
    async with engine.begin() as connection:
        for table in (
            User.__table__,
            QuestionBankQuestion.__table__,
            QuestionBankAttempt.__table__,
        ):
            await connection.run_sync(table.create)
    session_factory = async_sessionmaker(engine, expire_on_commit=False)

    async with session_factory() as session:
        session.add_all(
            [
                User(user_id=STUDENT_ID, username="zhangsan", api_key="key-a", role="user", is_active=True),
                User(user_id=OTHER_ID, username="lisi", api_key="key-b", role="user", is_active=True),
            ]
        )
        session.add_all(
            [
                QuestionBankQuestion(
                    id="q-choice",
                    question_type="选择题",
                    stem="跨中集中力简支梁的最大弯矩？",
                    options_json=[{"key": "A", "text": "PL/4"}, {"key": "B", "text": "PL/8"}],
                    answer="A",
                    analysis="Mmax=PL/4。",
                    difficulty="easy",
                    knowledge_points_json=["弯曲内力"],
                    stem_hash="h1",
                    created_by="teacher",
                ),
                QuestionBankQuestion(
                    id="q-fill",
                    question_type="填空题",
                    stem="Mmax = ____ kN·m（P=10kN，L=4m）",
                    answer="10",
                    stem_hash="h2",
                    created_by="teacher",
                ),
                QuestionBankQuestion(
                    id="q-subjective",
                    question_type="计算题",
                    stem="求固定端弯矩并画出弯矩图",
                    answer="6kN·m",
                    analysis="M=F×L。",
                    stem_hash="h3",
                    created_by="teacher",
                ),
                QuestionBankQuestion(
                    id="q-disabled",
                    question_type="简答题",
                    stem="已下线的题",
                    answer="x",
                    status="disabled",
                    stem_hash="h4",
                    created_by="teacher",
                ),
            ]
        )
        await session.commit()

    app = FastAPI()
    app.include_router(practice_router.router)
    app.include_router(qb_images_router.router, prefix="/api/v2")
    identity: dict[str, ApiKeyAuth] = {"auth": _auth(STUDENT_ID)}
    app.dependency_overrides[verify_api_key] = lambda: identity["auth"]

    async def _session_override() -> AsyncIterator[Any]:
        async with session_factory() as session:
            yield session

    app.dependency_overrides[get_async_session] = _session_override
    return app, identity, session_factory


@pytest.mark.asyncio
async def test_sanitized_listing_and_solution_channel() -> None:
    app, _identity, session_factory = await _build_app()
    transport = httpx.ASGITransport(app=app)
    async with httpx.AsyncClient(transport=transport, base_url="http://test") as client:
        listed = await client.get("/practice/questions")
        assert listed.status_code == 200
        body = listed.json()
        # disabled 不可见：三题。
        assert body["total"] == 3
        for item in body["items"]:
            assert "answer" not in item
            assert "analysis" not in item

        detail = await client.get("/practice/questions/q-choice")
        assert detail.status_code == 200
        assert "answer" not in detail.json()

        solution = await client.get("/practice/questions/q-choice", params={"with_solution": 1})
        assert solution.status_code == 200
        assert solution.json()["answer"] == "A"
        assert solution.json()["analysis"] == "Mmax=PL/4。"

        disabled = await client.get("/practice/questions/q-disabled")
        assert disabled.status_code == 404

    # 解答模式浏览落一条 mode="browse" 的 attempt（教师分析信号），is_correct
    # 为 None，不会进入错题本/正确率分母；普通详情浏览不落记录。
    async with session_factory() as session:
        rows = (
            (
                await session.execute(
                    QuestionBankAttempt.__table__.select().where(
                        QuestionBankAttempt.question_id == "q-choice"
                    )
                )
            )
            .mappings()
            .all()
        )
        assert len(rows) == 1
        assert rows[0]["mode"] == "browse"
        assert rows[0]["is_correct"] is None


@pytest.mark.asyncio
async def test_grading_rules() -> None:
    app, _identity, session_factory = await _build_app()
    transport = httpx.ASGITransport(app=app)
    async with httpx.AsyncClient(transport=transport, base_url="http://test") as client:
        wrong = await client.post("/practice/questions/q-choice/check", json={"answer": "B"})
        assert wrong.status_code == 200
        assert wrong.json()["is_correct"] is False
        assert wrong.json()["answer"] == "A"

        right = await client.post("/practice/questions/q-choice/check", json={"answer": " a "})
        assert right.json()["is_correct"] is True

        # 填空：数值容差（"10.0" == "10"），文本归一。
        fill = await client.post("/practice/questions/q-fill/check", json={"answer": "10.0"})
        assert fill.json()["is_correct"] is True

        # 主观题：不自判，is_correct=None；自评显式提交。
        subjective = await client.post(
            "/practice/questions/q-subjective/check",
            json={"answer": "M=F×L=6kN·m"},
        )
        assert subjective.json()["is_correct"] is None
        assert subjective.json()["is_self_review"] is True

        self_bad = await client.post(
            "/practice/questions/q-subjective/check",
            json={"answer": "（自评）", "self_review": False},
        )
        assert self_bad.json()["is_correct"] is False


@pytest.mark.asyncio
async def test_attempts_ownership_and_wrong_book() -> None:
    app, identity, session_factory = await _build_app()
    async with session_factory() as session:
        session.add_all(
            [
                QuestionBankAttempt(
                    id="a-1",
                    user_id=OTHER_ID,
                    username="lisi",
                    question_id="q-choice",
                    answer_json={"answer": "B"},
                    is_correct=False,
                ),
                QuestionBankAttempt(
                    id="a-2",
                    user_id=STUDENT_ID,
                    username="zhangsan",
                    question_id="q-choice",
                    answer_json={"answer": "A"},
                    is_correct=True,
                    created_at=datetime.now() - timedelta(days=1),
                ),
                QuestionBankAttempt(
                    id="a-3",
                    user_id=STUDENT_ID,
                    username="zhangsan",
                    question_id="q-choice",
                    answer_json={"answer": "B"},
                    is_correct=False,
                ),
                QuestionBankAttempt(
                    id="a-4",
                    user_id=STUDENT_ID,
                    username="zhangsan",
                    question_id="q-fill",
                    answer_json={"answer": "9"},
                    is_correct=False,
                ),
            ]
        )
        await session.commit()

    transport = httpx.ASGITransport(app=app)
    async with httpx.AsyncClient(transport=transport, base_url="http://test") as client:
        mine = await client.get("/practice/attempts")
        assert mine.status_code == 200
        # 属主隔离：只看到本人 3 条，他人记录不可见。
        assert mine.json()["total"] == 3

        wrong = await client.get("/practice/attempts/wrong")
        assert wrong.status_code == 200
        wrong_items = wrong.json()["items"]
        wrong_ids = {item["id"] for item in wrong_items}
        # q-choice 最近一次作答（a-3）为错、q-fill 为错 → 两题都在错题本；
        # a-2 的正确作答因不是最近一次，不能把 q-choice 移出。
        assert wrong_ids == {"q-fill", "q-choice"}
        assert "answer" not in wrong_items[0]


@pytest.mark.asyncio
async def test_qb_image_signature_fail_closed(monkeypatch: pytest.MonkeyPatch) -> None:
    app, _identity, _sf = await _build_app()
    from backend.application.question_bank import images as qb_images

    transport = httpx.ASGITransport(app=app)
    async with httpx.AsyncClient(transport=transport, base_url="http://test") as client:
        monkeypatch.setattr(qb_images, "_signature_secret", lambda: b"")

        forged = await client.get(
            "/api/v2/files/qb-image",
            params={
                "name": "question-bank/b1/q1/1.png",
                "exp": "9999999999",
                "sig": "0" * 64,
            },
        )
        assert forged.status_code == 403

        # 非法 object_name（路径穿越）直接 400。
        traversal = await client.get(
            "/api/v2/files/qb-image",
            params={"name": "../../etc/passwd", "exp": "9999999999", "sig": "x"},
        )
        assert traversal.status_code == 400

        # 有效签名 + fetch 命中 → 200 图片字节。
        async def _fake_fetch(object_name: str) -> bytes | None:
            return PNG_BYTES if object_name == "question-bank/b1/q1/1.png" else None

        monkeypatch.setattr(qb_images, "_signature_secret", lambda: b"test-secret")
        monkeypatch.setattr(qb_images_router, "fetch_qb_image", _fake_fetch)
        url = qb_images.build_qb_image_url("question-bank/b1/q1/1.png")
        ok = await client.get(url)
        assert ok.status_code == 200
        assert ok.content == PNG_BYTES
        assert ok.headers["content-type"].startswith("image/png")

        missing = await client.get(
            qb_images.build_qb_image_url("question-bank/b1/q9/1.png")
        )
        assert missing.status_code == 404


def test_registry_tool_uses_qb_image_urls() -> None:
    """工具图片令牌链路：注册 URL 来自 qb-image 签名代理。"""
    from backend.application.question_bank.images import build_qb_image_url

    url = build_qb_image_url("question-bank/b1/q1/1.png")
    assert url.startswith("/api/v2/files/qb-image?")
    assert "sig=" in url and "exp=" in url


def test_parse_grading_json_variants() -> None:
    from backend.application.question_bank.visual_grading import (
        VisualGradingError,
        parse_grading_json,
    )

    clean = parse_grading_json(
        '{"verdict": "correct", "score": 90, "comment": "受力图与平衡方程均正确",'
        ' "explanation": "$F_A=5\\\\mathrm{kN}$"}'
    )
    assert clean == {
        "verdict": "correct",
        "score": 90,
        "comment": "受力图与平衡方程均正确",
        "explanation": "$F_A=5\\mathrm{kN}$",
    }

    fenced = parse_grading_json(
        '评阅如下：```json\n{"verdict": "WRONG", "score": 250, "comment": "c", "explanation": "e"}\n```'
    )
    assert fenced["verdict"] == "incorrect"
    assert fenced["score"] == 100  # 越界收敛到 [0,100]

    synonyms = parse_grading_json('{"verdict": "对", "score": null, "comment": "c", "explanation": "e"}')
    assert synonyms["verdict"] == "correct"
    assert synonyms["score"] is None

    with pytest.raises(VisualGradingError):
        parse_grading_json("完全不是 JSON 的输出")
    with pytest.raises(VisualGradingError):
        parse_grading_json('{"verdict": "correct", "score": 1, "comment": "", "explanation": ""}')


@pytest.mark.asyncio
async def test_visual_grading_extracts_list_content_blocks() -> None:
    """部分网关把模型回复包成内容块列表：必须提取 text 再解析（曾致生产 503）。"""
    from types import SimpleNamespace

    from backend.application.question_bank import visual_grading as vg
    from backend.db.models import QuestionBankQuestion

    raw_json = (
        '{"verdict": "correct", "score": 95, "comment": "全部正确",'
        ' "explanation": "过程规范。"}'
    )

    async def _fake_model_factory(**_kwargs):
        async def _ainvoke(_messages):
            # 形态一：字符串
            return SimpleNamespace(content=raw_json)

        return SimpleNamespace(ainvoke=_ainvoke)

    question = QuestionBankQuestion(
        id="q-x", question_type="计算题", stem="s", answer="a", stem_hash="h",
        created_by="t",
    )
    image = vg.PreparedImage(base64="Zg==", mime_type="image/png")
    graded = await vg.grade_visual_answer(
        question, given_text="", images=[image], model_factory=_fake_model_factory
    )
    assert graded["verdict"] == "correct"

    async def _block_model_factory(**_kwargs):
        async def _ainvoke(_messages):
            # 形态二：内容块列表（生产网关实测出现过）
            return SimpleNamespace(
                content=[{"type": "text", "text": f"批改结果：\n{raw_json}"}]
            )

        return SimpleNamespace(ainvoke=_ainvoke)

    graded_blocks = await vg.grade_visual_answer(
        question, given_text="", images=[image], model_factory=_block_model_factory
    )
    assert graded_blocks["verdict"] == "correct"
    assert graded_blocks["score"] == 95


@pytest.mark.asyncio
async def test_visual_check_contract(monkeypatch: pytest.MonkeyPatch) -> None:
    # 本测试断言「判分后落 attempt 行」：跨运行的判分缓存会让第二次
    # 起直接命中缓存跳过落库，测试内禁用缓存。
    async def _no_read(_key):
        return None

    async def _no_write(_key, _resp):
        return None

    from backend.api.routers import practice as _practice_router

    monkeypatch.setattr(_practice_router, "_read_grade_cache", _no_read)
    monkeypatch.setattr(_practice_router, "_write_grade_cache", _no_write)
    app, _identity, session_factory = await _build_app()
    from backend.api.routers import practice as practice_router_module
    from backend.application.question_bank import visual_grading as vg

    async def _fake_prepare(content: bytes, mime_type: str) -> vg.PreparedImage:
        return vg.PreparedImage(base64="ZmFrZQ==", mime_type="image/png")

    async def _fake_grade(question, *, given_text, images, model_factory=None):
        assert question.id == "q-subjective"
        assert len(images) == 1
        return {
            "verdict": "partial",
            "score": 60,
            "comment": "支座反力正确，弯矩单位写错",
            "explanation": "弯矩单位应为 kN·m。",
        }

    monkeypatch.setattr(practice_router_module, "prepare_grading_image", _fake_prepare)
    monkeypatch.setattr(practice_router_module, "grade_visual_answer", _fake_grade)

    transport = httpx.ASGITransport(app=app)
    async with httpx.AsyncClient(transport=transport, base_url="http://test") as client:
        no_image = await client.post("/practice/questions/q-subjective/check-visual")
        assert no_image.status_code == 422

        too_many = await client.post(
            "/practice/questions/q-subjective/check-visual",
            files=[("images", ("a.png", PNG_BYTES, "image/png"))] * 4,
        )
        assert too_many.status_code == 422

        ok = await client.post(
            "/practice/questions/q-subjective/check-visual",
            data={"answer": "F_A=5kN，M_C=10kN"},
            files=[("images", ("work.png", PNG_BYTES, "image/png"))],
        )
        assert ok.status_code == 200, ok.text
        body = ok.json()
        assert body["visual_grading"]["verdict"] == "partial"
        assert body["visual_grading"]["score"] == 60
        assert body["is_correct"] is False  # partial 不计入正确
        # 判分后才下发参考答案与解析。
        assert body["answer"] == "6kN·m"
        assert body["analysis"] == "M=F×L。"

    async with session_factory() as session:
        rows = (
            (
                await session.execute(
                    QuestionBankAttempt.__table__.select().where(
                        QuestionBankAttempt.question_id == "q-subjective"
                    )
                )
            )
            .mappings()
            .all()
        )
        visual_rows = [row for row in rows if "visual" in (row["answer_json"] or {})]
        assert len(visual_rows) == 1
        assert visual_rows[0]["is_correct"] in (False, 0)
        assert visual_rows[0]["answer_json"]["visual"]["score"] == 60


@pytest.mark.asyncio
async def test_visual_check_unavailable(monkeypatch: pytest.MonkeyPatch) -> None:
    app, _identity, session_factory = await _build_app()
    from backend.api.routers import practice as practice_router_module
    from backend.application.question_bank import visual_grading as vg

    async def _fake_prepare(content: bytes, mime_type: str) -> vg.PreparedImage:
        return vg.PreparedImage(base64="ZmFrZQ==", mime_type="image/png")

    async def _fail_grade(question, *, given_text, images, model_factory=None):
        raise vg.VisualGradingError("图片判分服务暂不可用，请稍后重试")

    monkeypatch.setattr(practice_router_module, "prepare_grading_image", _fake_prepare)
    monkeypatch.setattr(practice_router_module, "grade_visual_answer", _fail_grade)

    transport = httpx.ASGITransport(app=app)
    async with httpx.AsyncClient(transport=transport, base_url="http://test") as client:
        unavailable = await client.post(
            "/practice/questions/q-subjective/check-visual",
            files=[("images", ("work.png", PNG_BYTES, "image/png"))],
        )
        assert unavailable.status_code == 503
        assert "不可用" in unavailable.json()["detail"]

    async with session_factory() as session:
        rows = (
            (
                await session.execute(
                    QuestionBankAttempt.__table__.select().where(
                        QuestionBankAttempt.question_id == "q-subjective"
                    )
                )
            )
            .mappings()
            .all()
        )
        assert rows == []  # 判分失败不落记录
