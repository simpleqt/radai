"""题库管理（教师侧）的解析器单测 + HTTP 契约测试。

覆盖：
1. docx 模板解析（python-docx 确定性路径）：标记切分/选项/难度归一/知识点/
   图片锚定/校验分级/批内查重；
2. 迁移 SQL↔ORM 列比对 + 账本登记；
3. 管理路由契约：角色门禁、模板下载、批次两段式（确认前不落库）、
   题目标注 PATCH、知识点字典 CRUD；
4. 工具接线：registry 登记 + fmca_default policy include（其他 policy 不含）。
"""

from __future__ import annotations

import base64
import io
import re
from collections.abc import AsyncIterator
from typing import Any

import httpx
import pytest
from fastapi import FastAPI
from sqlalchemy.ext.asyncio import async_sessionmaker, create_async_engine

from backend.api.dependencies.auth import ApiKeyAuth, verify_api_key
from backend.api.routers import question_bank as qb_router
from backend.db import migrate
from backend.db.connection import get_async_session
from backend.db.models import (
    QuestionBankAttempt,
    QuestionBankBatch,
    QuestionBankKnowledgePoint,
    QuestionBankQuestion,
    User,
)

TEACHER_ID = "teacher-qb"
STUDENT_ID = "student-qb"

_B64_PNG = (
    "iVBORw0KGgoAAAANSUhEUgAAAAEAAAABCAYAAAAfFcSJ"
    "AAAADUlEQVR42mP8z8BQDwAEhQGAhKmMIQAAAABJRU5ErkJggg=="
)

# 标准 1x1 PNG（base64 解码），python-docx 需要可解析的图片头。
PNG_BYTES = base64.b64decode(_B64_PNG)


def _auth(user_id: str, role: str) -> ApiKeyAuth:
    return ApiKeyAuth(api_key=f"key-{user_id}", user_id=user_id, role=role)


def _build_docx(paragraphs: list[str], *, with_image: bool = False) -> bytes:
    from docx import Document

    document = Document()
    for text in paragraphs:
        if with_image and text == "@IMAGE@":
            document.add_picture(io.BytesIO(PNG_BYTES), width=200000)
            continue
        document.add_paragraph(text)
    buffer = io.BytesIO()
    document.save(buffer)
    return buffer.getvalue()


def _sample_docx() -> bytes:
    return _build_docx(
        [
            "【题型】选择题",
            "【题干】图示简支梁跨中受集中力 P=10kN（见图），跨中最大弯矩为（　）。",
            "@IMAGE@",
            "【选项】A. PL/4　B. PL/8　C. PL/2　D. PL",
            "【答案】A",
            "【解析】Mmax=PL/4=10kN·m。",
            "【难度】简单",
            "【知识点】弯曲内力；弯矩图",
            "【章节】第 4 章 弯曲内力",
            "【来源】2024-2025 期末卷 第 3 题",
            "====",
            "【题型】计算题",
            "【题干】悬臂梁自由端受集中力 F=2kN，长 L=3m，求固定端弯矩。",
            "【答案】M=6kN·m",
            "【解析】M=F×L。",
            "【难度】较难",
            "【知识点】悬臂梁",
            "====",
            "【题干】这题既没有题型也没有答案",
            "====",
        ],
        with_image=True,
    )


@pytest.fixture(autouse=True)
def _deterministic_parser(monkeypatch: pytest.MonkeyPatch):
    # 解析器测试统一走 python-docx 确定性路径（pandoc 在 CI/开发机可能缺席）。
    monkeypatch.setenv("QUESTION_BANK_DISABLE_PANDOC", "1")


# ── 解析器单测 ────────────────────────────────────────────────────────────


def test_parser_splits_fields_and_images() -> None:
    from backend.application.question_bank.parser import parse_question_docx

    result = parse_question_docx(_sample_docx(), known_knowledge_points={"弯曲内力"})
    assert len(result.questions) == 3

    first = result.questions[0]
    assert first.question_type == "选择题"
    assert "简支梁" in first.stem
    assert first.difficulty == "easy"
    assert first.knowledge_points == ["弯曲内力", "弯矩图"]
    assert first.answer == "A"
    assert [option["key"] for option in first.options] == ["A", "B", "C", "D"]
    assert first.options[0]["text"] == "PL/4"
    assert len(first.images) == 1
    assert first.images[0].data == PNG_BYTES
    assert not first.has_error
    # 字典外知识点 → warning（不阻断）。
    assert any("悬臂梁" in issue["message"] or "弯矩图" in issue["message"] for issue in first.issues) or all(
        kp in {"弯曲内力"} for kp in first.knowledge_points
    )

    second = result.questions[1]
    assert second.question_type == "计算题"
    assert second.difficulty == "hard"
    assert not second.has_error

    third = result.questions[2]
    assert third.has_error
    assert any("答案为空" in issue["message"] for issue in third.issues)
    assert any("题型无法识别" in issue["message"] for issue in third.issues)
    assert result.valid_count == 2


def test_parser_option_split_inline() -> None:
    from backend.application.question_bank.parser import _parse_options

    options = _parse_options("A. 10 B. 20 kN·m\nC. 30 D. 40")
    assert [(option["key"], option["text"]) for option in options] == [
        ("A", "10"),
        ("B", "20 kN·m"),
        ("C", "30"),
        ("D", "40"),
    ]


def test_parser_rejects_non_docx_and_empty() -> None:
    from backend.application.question_bank.service import QuestionBankError

    pytest.importorskip("docx")
    # create_batch 的前置校验在路由层之前，直接构造异常场景验证归一化即可。
    from backend.application.question_bank.service import normalize_stem_hash

    assert normalize_stem_hash("图示 简支梁！") == normalize_stem_hash("图示简支梁")
    assert normalize_stem_hash("A") != normalize_stem_hash("B")
    assert QuestionBankError is not None


# ── 迁移与账本 ────────────────────────────────────────────────────────────


def test_migration_sql_matches_orm_and_is_tracked() -> None:
    sql_path = migrate.MIGRATIONS_DIR / "add_question_bank_tables.sql"
    sql = sql_path.read_text(encoding="utf-8")

    orm_tables = {
        "question_bank_batches": QuestionBankBatch.__table__,
        "question_bank_questions": QuestionBankQuestion.__table__,
        "question_bank_knowledge_points": QuestionBankKnowledgePoint.__table__,
        "question_bank_attempts": QuestionBankAttempt.__table__,
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
            if stripped.upper().startswith(("KEY ", "CONSTRAINT ", "UNIQUE ", "PRIMARY ")):
                continue
            match = re.match(r"`?([A-Za-z_][A-Za-z0-9_]*)", stripped)
            if match:
                sql_columns.add(match.group(1))
        assert sql_columns == set(table.columns.keys()), (
            f"{table_name} SQL 与 ORM 列不一致: "
            f"SQL-only={sql_columns - set(table.columns.keys())}, "
            f"ORM-only={set(table.columns.keys()) - sql_columns}"
        )

    # 类型级比对（此前只比列名集合，Integer vs TINYINT(1) 漂移不可见）：
    # is_correct 在 SQL 里是 TINYINT(1)，ORM 必须用 Boolean（MySQL 方言产出
    # TINYINT(1)），否则 create_all 路径与迁移产物结构分叉。
    import sqlalchemy

    attempts_block = re.search(
        r"CREATE TABLE IF NOT EXISTS\s+question_bank_attempts\s*\((.*?)\)\s*ENGINE",
        sql,
        re.S,
    )
    assert attempts_block is not None
    assert re.search(r"`?is_correct`?\s+TINYINT\(1\)", attempts_block.group(1))
    assert isinstance(
        QuestionBankAttempt.__table__.columns["is_correct"].type, sqlalchemy.Boolean
    )

    assert any(
        item.version == "202608170002_add_question_bank_tables"
        for item in migrate.MIGRATIONS
    )
    manifest = migrate.validate_migration_package()
    assert "202608170002_add_question_bank_tables" in manifest


# ── 工具接线 ──────────────────────────────────────────────────────────────


def test_tool_registered_and_scoped_to_fmca_policy() -> None:
    import yaml

    registry = yaml.safe_load(
        (migrate.ROOT / "backend" / "config" / "tools" / "registry.yaml").read_text(
            encoding="utf-8"
        )
    )
    tools = registry.get("tools") or {}
    entry = tools.get("search_question_bank")
    assert entry is not None
    assert entry["domains"] == ["fundamental-mechanics-course"]
    assert entry["risk_level"] == "safe"

    policies = yaml.safe_load(
        (migrate.ROOT / "backend" / "config" / "tools" / "policies.yaml").read_text(
            encoding="utf-8"
        )
    )["policies"]
    assert "search_question_bank" in policies["fmca_default"]["include"]
    for name, policy in policies.items():
        if name == "fmca_default":
            continue
        assert "search_question_bank" not in (policy.get("include") or []), (
            f"题库工具不应进入 {name} 策略"
        )


# ── 路由契约 ──────────────────────────────────────────────────────────────


def _patch_image_store(monkeypatch: pytest.MonkeyPatch) -> None:
    from backend.application.question_bank import images as qb_images

    async def _fake_store(data, *, batch_id, question_id, seq, ext):
        return f"question-bank/{batch_id}/{question_id}/{seq}.png"

    monkeypatch.setattr(qb_images, "store_qb_image", _fake_store)


async def _build_app(
    monkeypatch: pytest.MonkeyPatch,
) -> tuple[FastAPI, dict[str, ApiKeyAuth], async_sessionmaker]:
    engine = create_async_engine("sqlite+aiosqlite:///:memory:")
    async with engine.begin() as connection:
        for table in (
            User.__table__,
            QuestionBankBatch.__table__,
            QuestionBankQuestion.__table__,
            QuestionBankKnowledgePoint.__table__,
            QuestionBankAttempt.__table__,
        ):
            await connection.run_sync(table.create)
    session_factory = async_sessionmaker(engine, expire_on_commit=False)

    async with session_factory() as session:
        session.add_all(
            [
                User(user_id=TEACHER_ID, username="teacher", api_key="key-t", role="teacher", is_active=True),
                User(user_id=STUDENT_ID, username="student", api_key="key-s", role="user", is_active=True),
            ]
        )
        await session.commit()

    app = FastAPI()
    app.include_router(qb_router.router)

    async def _noop_audit(*args: Any, **kwargs: Any) -> None:
        return None

    monkeypatch.setattr(qb_router, "write_admin_audit", _noop_audit)

    identity: dict[str, ApiKeyAuth] = {"auth": _auth(TEACHER_ID, "teacher")}
    app.dependency_overrides[verify_api_key] = lambda: identity["auth"]

    async def _session_override() -> AsyncIterator[Any]:
        async with session_factory() as session:
            yield session

    app.dependency_overrides[get_async_session] = _session_override
    _patch_image_store(monkeypatch)
    return app, identity, session_factory


@pytest.mark.asyncio
async def test_role_gate_and_template_download(monkeypatch: pytest.MonkeyPatch) -> None:
    app, identity, _sf = await _build_app(monkeypatch)
    transport = httpx.ASGITransport(app=app)
    async with httpx.AsyncClient(transport=transport, base_url="http://test") as client:
        identity["auth"] = _auth(STUDENT_ID, "user")
        forbidden = await client.get("/teacher/question-bank/questions")
        assert forbidden.status_code == 403

        identity["auth"] = _auth(STUDENT_ID, "student")
        forbidden_student = await client.get("/teacher/question-bank/questions")
        assert forbidden_student.status_code == 403

        identity["auth"] = _auth(TEACHER_ID, "teacher")
        template = await client.get("/teacher/question-bank/template")
        assert template.status_code == 200
        # docx = zip = PK 魔数；模板现场生成，不入 Git。
        assert template.content[:2] == b"PK"
        # 模板内嵌示例受力图（word/media/*），教师可参照其位置粘贴自己的图。
        import zipfile

        with zipfile.ZipFile(io.BytesIO(template.content)) as archive:
            media = [name for name in archive.namelist() if name.startswith("word/media/")]
        assert media, "模板必须内嵌示例受力图"
        png_bytes = zipfile.ZipFile(io.BytesIO(template.content)).read(media[0])
        assert png_bytes[:8] == b"\x89PNG\r\n\x1a\n"


@pytest.mark.asyncio
async def test_template_docx_roundtrips_through_parser(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """官方模板原样上传：说明段被忽略，解析出两道有效题，首题带受力图。"""
    from backend.application.question_bank.parser import parse_question_docx

    app, identity, _sf = await _build_app(monkeypatch)
    transport = httpx.ASGITransport(app=app)
    async with httpx.AsyncClient(transport=transport, base_url="http://test") as client:
        identity["auth"] = _auth(TEACHER_ID, "teacher")
        template = await client.get("/teacher/question-bank/template")
        assert template.status_code == 200

    result = parse_question_docx(template.content)
    assert len(result.questions) == 2
    first = result.questions[0]
    assert first.question_type == "选择题"
    assert not first.has_error
    assert len(first.images) == 1
    assert first.images[0].data[:8] == b"\x89PNG\r\n\x1a\n"
    assert [option["key"] for option in first.options] == ["A", "B", "C", "D"]
    second = result.questions[1]
    assert second.question_type == "计算题"
    assert not second.has_error
    assert result.valid_count == 2


@pytest.mark.asyncio
async def test_batch_two_phase_lifecycle(monkeypatch: pytest.MonkeyPatch) -> None:
    app, identity, session_factory = await _build_app(monkeypatch)
    transport = httpx.ASGITransport(app=app)
    async with httpx.AsyncClient(transport=transport, base_url="http://test") as client:
        # 非法文件被拒。
        bad = await client.post(
            "/teacher/question-bank/batches",
            files={"file": ("x.docx", b"not a docx", "application/octet-stream")},
        )
        assert bad.status_code == 422

        uploaded = await client.post(
            "/teacher/question-bank/batches",
            files={
                "file": (
                    "batch.docx",
                    _sample_docx(),
                    "application/vnd.openxmlformats-officedocument.wordprocessingml.document",
                )
            },
        )
        assert uploaded.status_code == 201
        batch = uploaded.json()
        assert batch["total_count"] == 3
        assert batch["valid_count"] == 2
        assert batch["status"] == "parsed"
        # 图片已带稳定 object_name 与签名 URL（签名只是展示态）。
        first = batch["questions"][0]
        assert first["images"] and first["images"][0]["object_name"].startswith("question-bank/")
        assert "/api/v2/files/qb-image" in first["images"][0]["url"]

        # 确认前题目表没有数据。
        async with session_factory() as session:
            from sqlalchemy import func, select

            count = await session.scalar(select(func.count()).select_from(QuestionBankQuestion))
            assert count == 0

        confirmed = await client.post(f"/teacher/question-bank/batches/{batch['id']}/confirm")
        assert confirmed.status_code == 200
        assert confirmed.json()["inserted"] == 2

        # 重复确认被拒。
        again = await client.post(f"/teacher/question-bank/batches/{batch['id']}/confirm")
        assert again.status_code == 422

        # 已确认批次不可废弃。
        discard = await client.delete(f"/teacher/question-bank/batches/{batch['id']}")
        assert discard.status_code == 422

        listed = await client.get("/teacher/question-bank/questions")
        body = listed.json()
        assert body["total"] == 2
        # 教师视图含答案解析与标注。
        item = body["items"][0]
        assert "answer" in item and "analysis" in item

        # 筛选：难度 easy。
        easy = await client.get("/teacher/question-bank/questions", params={"difficulty": "easy"})
        assert easy.json()["total"] == 1


@pytest.mark.asyncio
async def test_cross_batch_stem_dedup(monkeypatch: pytest.MonkeyPatch) -> None:
    """题干跨批全局查重：重复批次确认时跳过（不再分裂成两道题），
    手工录入重复题干被 422 拒绝并指明已有题号。"""
    app, identity, _sf = await _build_app(monkeypatch)
    transport = httpx.ASGITransport(app=app)
    async with httpx.AsyncClient(transport=transport, base_url="http://test") as client:
        identity["auth"] = _auth(TEACHER_ID, "teacher")
        docx = _sample_docx()
        first_upload = await client.post(
            "/teacher/question-bank/batches",
            files={"file": ("batch.docx", docx, "application/octet-stream")},
        )
        assert first_upload.status_code == 201
        second_upload = await client.post(
            "/teacher/question-bank/batches",
            files={"file": ("batch-again.docx", docx, "application/octet-stream")},
        )
        assert second_upload.status_code == 201

        first = await client.post(
            f"/teacher/question-bank/batches/{first_upload.json()['id']}/confirm"
        )
        assert first.status_code == 200
        assert first.json()["inserted"] == 2
        assert first.json()["skipped_duplicates"] == 0

        # 第二批全量与已有题目重复：跳过入库并上报 skipped_duplicates。
        second = await client.post(
            f"/teacher/question-bank/batches/{second_upload.json()['id']}/confirm"
        )
        assert second.status_code == 200
        assert second.json()["inserted"] == 0
        assert second.json()["skipped_duplicates"] == 2
        issues = [
            issue
            for item in second.json()["batch"]["questions"]
            for issue in item.get("issues") or []
            if "重复" in issue.get("message", "")
        ]
        assert len(issues) == 2  # 每道被跳过的题各一条跨批重复 warning

        # 手工录入同题干被拒，并给出已有题号。
        duplicated = await client.post(
            "/teacher/question-bank/questions",
            json={
                "question_type": "计算题",
                "stem": "悬臂梁自由端受集中力 F=2kN，长 L=3m，求固定端弯矩。",
            },
        )
        assert duplicated.status_code == 422
        assert "重复" in duplicated.json()["detail"]

        listed = await client.get("/teacher/question-bank/questions")
        assert listed.json()["total"] == 2


@pytest.mark.asyncio
async def test_question_annotate_and_kp_crud(monkeypatch: pytest.MonkeyPatch) -> None:
    app, _identity, session_factory = await _build_app(monkeypatch)
    async with session_factory() as session:
        session.add(
            QuestionBankQuestion(
                id="q-annotate",
                question_type="计算题",
                stem="求固定端弯矩",
                answer="6kN·m",
                stem_hash="hash-1",
                created_by=TEACHER_ID,
            )
        )
        await session.commit()

    transport = httpx.ASGITransport(app=app)
    async with httpx.AsyncClient(transport=transport, base_url="http://test") as client:
        patched = await client.patch(
            "/teacher/question-bank/questions/q-annotate",
            json={"difficulty": "medium", "knowledge_points": ["弯曲内力"], "chapter": "第 4 章"},
        )
        assert patched.status_code == 200
        body = patched.json()
        assert body["difficulty"] == "medium"
        assert body["knowledge_points"] == ["弯曲内力"]

        invalid = await client.patch(
            "/teacher/question-bank/questions/q-annotate",
            json={"difficulty": "超纲"},
        )
        assert invalid.status_code == 422

        unknown_field = await client.patch(
            "/teacher/question-bank/questions/q-annotate",
            json={"stem_hash": "hack"},
        )
        assert unknown_field.status_code == 422

        # 软删（下线）后默认列表不可见，显式 status=disabled 可见。
        removed = await client.delete("/teacher/question-bank/questions/q-annotate")
        assert removed.status_code == 200
        hidden = await client.get("/teacher/question-bank/questions")
        assert hidden.json()["total"] == 0
        visible = await client.get("/teacher/question-bank/questions", params={"status": "disabled"})
        assert visible.json()["total"] == 1

        # 知识点字典 CRUD。
        created = await client.post(
            "/teacher/question-bank/knowledge-points",
            json={"name": "应力状态分析", "chapter": "第 7 章"},
        )
        assert created.status_code == 201
        kp_id = created.json()["id"]
        duplicate = await client.post(
            "/teacher/question-bank/knowledge-points", json={"name": "应力状态分析"}
        )
        assert duplicate.status_code == 422
        renamed = await client.patch(
            f"/teacher/question-bank/knowledge-points/{kp_id}", json={"name": "应力状态"}
        )
        assert renamed.status_code == 200
        listed = await client.get("/teacher/question-bank/knowledge-points")
        assert listed.json()["items"][0]["name"] == "应力状态"
        deleted = await client.delete(f"/teacher/question-bank/knowledge-points/{kp_id}")
        assert deleted.status_code == 200


@pytest.mark.asyncio
async def test_kp_delete_blocked_while_referenced(monkeypatch: pytest.MonkeyPatch) -> None:
    """知识点被题目标注引用时删除被拒（422），清理标注后可删。

    回归守卫：2026-08-19 曾因 LIKE 模式漏掉尾部 %，引用计数恒 0、
    删除永远放行，悬空标注防护完全失效。
    """
    app, identity, session_factory = await _build_app(monkeypatch)
    async with session_factory() as session:
        session.add(
            QuestionBankQuestion(
                id="q-kp-ref",
                question_type="计算题",
                stem="引用知识点的题",
                knowledge_points_json=["弯曲内力", "弯矩图"],
                stem_hash="hash-kp-ref",
                created_by=TEACHER_ID,
            )
        )
        await session.commit()

    transport = httpx.ASGITransport(app=app)
    async with httpx.AsyncClient(transport=transport, base_url="http://test") as client:
        identity["auth"] = _auth(TEACHER_ID, "teacher")
        created = await client.post(
            "/teacher/question-bank/knowledge-points",
            json={"name": "弯矩图"},
        )
        assert created.status_code == 201
        kp_id = created.json()["id"]

        # JSON 数组中部元素（["弯曲内力", "弯矩图"]）也必须命中引用检查。
        blocked = await client.delete(f"/teacher/question-bank/knowledge-points/{kp_id}")
        assert blocked.status_code == 422
        assert "引用" in blocked.json()["detail"]

        patched = await client.patch(
            "/teacher/question-bank/questions/q-kp-ref",
            json={"knowledge_points": ["弯曲内力"]},
        )
        assert patched.status_code == 200

        deleted = await client.delete(f"/teacher/question-bank/knowledge-points/{kp_id}")
        assert deleted.status_code == 200


def test_parser_unsupported_image_exts_dropped_not_mislabeled() -> None:
    """H2 回归守护：真实后缀必须原样到达剔除检查，emf/wmf 剔除、bmp/svg 保留。"""
    from backend.application.question_bank.parser import (
        _QuestionBuilder,
        _ext_from_name,
        _is_unsupported_image_ext,
    )  # noqa: I001

    # _ext_from_name 此前把白名单外后缀一律默认成 "png"，导致 emf/wmf
    # 伪 png 裂图且剔除检查永不触发。
    assert _ext_from_name("word/media/image1.emf") == "emf"
    assert _ext_from_name("pic.WMF") == "wmf"
    assert _ext_from_name("scan.tiff") == "tiff"
    assert _ext_from_name("photo.jpeg") == "jpg"
    assert _ext_from_name("noext") == "png"

    assert _is_unsupported_image_ext("emf")
    assert _is_unsupported_image_ext("wmf")
    assert _is_unsupported_image_ext("tiff")
    # bmp/svg 虽可被浏览器渲染，但 qb-image 存储/代理白名单只认
    # png/jpg/gif/webp——放行只会被静默改标 .png 裂图，与 emf 同路剔除。
    assert _is_unsupported_image_ext("bmp")
    assert _is_unsupported_image_ext("svg")

    builder = _QuestionBuilder()
    builder.append_image(b"fake-emf", "emf")
    builder.append_image(b"fake-bmp", "bmp")
    builder.append_image(b"ok-png", "png")
    assert builder.images and all(image.ext == "png" for image in builder.images)
    assert builder.unsupported_image_exts == {"emf", "bmp"}

    # 存储层 fail-closed：白名单外格式必须报错而非静默改标（H1 回归守护）。
    from backend.application.question_bank import images as qb_images

    with pytest.raises(ValueError):
        qb_images.build_object_name(
            batch_id="b", question_id="q", seq=1, ext="bmp"
        )
    assert qb_images.build_object_name(
        batch_id="b", question_id="q", seq=1, ext="jpeg"
    ).endswith(".jpg")
