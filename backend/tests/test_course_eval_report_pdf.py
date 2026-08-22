"""course_eval export_report_pdf 工具测试。

覆盖：属主隔离、缺 docx 版本、取最新版本、转换失败降级、登记失败降级、
成功返回下载链接，以及「转换在 DB 会话之外执行」这条约束（LibreOffice 冷启动
是秒级的，包在会话里会让同任务写工具长时间等锁）。
"""

from __future__ import annotations

import json

import pytest
from sqlalchemy.ext.asyncio import async_sessionmaker, create_async_engine
from sqlalchemy.pool import StaticPool

from backend.db.models import User
from backend.db.models.course_eval import (
    CeAchievementConfig,
    CeAuditLog,
    CeCourse,
    CeCourseGoal,
    CeExamItem,
    CeExamPaper,
    CeExamSection,
    CeGoalMapping,
    CeMaterial,
    CeReport,
    CeReportTemplate,
    CeReportVersion,
    CeScoreRecord,
    CeSemesterTask,
    CeStatsSnapshot,
    CeStudent,
)
from backend.domain.tools.runtime_context import ToolRuntimeContext

DOCX_STUB = b"PK\x03\x04stub-docx"
PDF_STUB = b"%PDF-1.7 stub"


@pytest.fixture
async def db_factory():
    engine = create_async_engine(
        "sqlite+aiosqlite://",
        connect_args={"check_same_thread": False},
        poolclass=StaticPool,
    )
    async with engine.begin() as conn:
        for table in (
            User.__table__,
            CeCourse.__table__,
            CeSemesterTask.__table__,
            CeAuditLog.__table__,
            CeMaterial.__table__,
            CeExamPaper.__table__,
            CeExamSection.__table__,
            CeExamItem.__table__,
            CeStudent.__table__,
            CeScoreRecord.__table__,
            CeStatsSnapshot.__table__,
            CeCourseGoal.__table__,
            CeGoalMapping.__table__,
            CeAchievementConfig.__table__,
            CeReport.__table__,
            CeReportTemplate.__table__,
            CeReportVersion.__table__,
        ):
            await conn.run_sync(table.create)
    factory = async_sessionmaker(engine, expire_on_commit=False)
    yield factory
    await engine.dispose()


@pytest.fixture
def ctx():
    return ToolRuntimeContext(
        request_id="r1",
        user_id="u1",
        agent_code="course_eval",
        domain="course-outcome-evaluation",
        thread_id="t1",
    )


def _patch_factory(monkeypatch, db_factory):
    monkeypatch.setattr(
        "backend.db.connection.get_async_session_factory",
        lambda: db_factory,
    )


async def _seed_task(factory, *, owner="u1"):
    async with factory() as session:
        course = CeCourse(code="C1", name="材料力学", owner_user_id=owner)
        session.add(course)
        await session.flush()
        task = CeSemesterTask(
            course_id=course.course_id,
            semester="2024-2025-1",
            class_no="c1",
            status="report_draft",
            owner_user_id=owner,
        )
        session.add(task)
        await session.flush()
        await session.commit()
        return task.semester_task_id


async def _seed_report_versions(factory, task_id, *, versions):
    """versions: [(version_no, docx_file_key)]"""
    async with factory() as session:
        template = CeReportTemplate(
            template_type="paper_analysis",
            version="1",
            active=True,
        )
        session.add(template)
        await session.flush()
        report = CeReport(
            semester_task_id=task_id,
            template_id=template.template_id,
            report_type="paper_analysis",
            status="draft",
            current_version_no=1,
            created_by="u1",
        )
        session.add(report)
        await session.flush()
        for version_no, file_key in versions:
            session.add(
                CeReportVersion(
                    report_id=report.report_id,
                    version_no=version_no,
                    content_json="{}",
                    docx_file_key=file_key,
                    created_by="u1",
                )
            )
        await session.commit()


def _tool(ctx):
    from backend.application.course_eval import tools as ce_tools

    return ce_tools.get_export_report_pdf_tool(runtime_context=ctx)


@pytest.fixture
def stub_pipeline(monkeypatch):
    """把文件读取/登记/转换都替换为可观测替身。"""
    from backend.application.course_eval.agent_tools import _shared

    state = {"registered": [], "converted": [], "read": []}

    async def _fake_read(session, file_id, user_id):
        state["read"].append((file_id, user_id))
        return DOCX_STUB, "材料力学试卷分析表_2024-2025-1_v2.docx"

    async def _fake_register(runtime_context, *, filename, payload):
        state["registered"].append({"filename": filename, "payload": payload})
        return "uf_pdf_1", f"/api/v2/files/uf_pdf_1/download?name={filename}"

    def _fake_convert(docx_bytes):
        state["converted"].append(docx_bytes)
        return PDF_STUB

    monkeypatch.setattr(_shared, "_read_material_bytes", _fake_read)
    monkeypatch.setattr(_shared, "_register_report_artifact", _fake_register)
    monkeypatch.setattr(
        "backend.services.document_conversion.convert_docx_to_pdf", _fake_convert
    )
    return state


async def test_exports_latest_version_pdf(monkeypatch, db_factory, ctx, stub_pipeline):
    _patch_factory(monkeypatch, db_factory)
    task_id = await _seed_task(db_factory)
    await _seed_report_versions(
        db_factory, task_id, versions=[(1, "uf_docx_1"), (2, "uf_docx_2")]
    )

    payload = json.loads(await _tool(ctx).ainvoke({"semester_task_id": task_id}))

    assert payload["version_no"] == 2
    assert payload["filename"] == "材料力学试卷分析表_2024-2025-1_v2.pdf"
    assert payload["download_url"].startswith("/api/v2/files/uf_pdf_1/download")
    # 取的是最新版本的 docx，转换输入即读到的字节
    assert stub_pipeline["read"] == [("uf_docx_2", "u1")]
    assert stub_pipeline["converted"] == [DOCX_STUB]
    assert stub_pipeline["registered"][0]["payload"] == PDF_STUB


async def test_missing_docx_version_is_reported(
    monkeypatch, db_factory, ctx, stub_pipeline
):
    _patch_factory(monkeypatch, db_factory)
    task_id = await _seed_task(db_factory)
    await _seed_report_versions(db_factory, task_id, versions=[(1, "")])

    payload = json.loads(await _tool(ctx).ainvoke({"semester_task_id": task_id}))

    assert "generate_report" in payload["error"]
    assert not stub_pipeline["converted"]


async def test_conversion_failure_degrades_to_docx(monkeypatch, db_factory, ctx):
    from backend.application.course_eval.agent_tools import _shared

    _patch_factory(monkeypatch, db_factory)
    task_id = await _seed_task(db_factory)
    await _seed_report_versions(db_factory, task_id, versions=[(1, "uf_docx_1")])

    async def _fake_read(session, file_id, user_id):
        return DOCX_STUB, "报告.docx"

    registered: list = []

    async def _fake_register(runtime_context, *, filename, payload):
        registered.append(filename)
        return "x", "y"

    monkeypatch.setattr(_shared, "_read_material_bytes", _fake_read)
    monkeypatch.setattr(_shared, "_register_report_artifact", _fake_register)
    monkeypatch.setattr(
        "backend.services.document_conversion.convert_docx_to_pdf",
        lambda docx_bytes: None,
    )

    payload = json.loads(await _tool(ctx).ainvoke({"semester_task_id": task_id}))

    assert "PDF 转换不可用" in payload["error"]
    assert "docx" in payload["error"]
    assert not registered


async def test_registration_failure_is_reported(monkeypatch, db_factory, ctx):
    from backend.application.course_eval.agent_tools import _shared

    _patch_factory(monkeypatch, db_factory)
    task_id = await _seed_task(db_factory)
    await _seed_report_versions(db_factory, task_id, versions=[(1, "uf_docx_1")])

    async def _fake_read(session, file_id, user_id):
        return DOCX_STUB, "报告.docx"

    async def _fake_register(runtime_context, *, filename, payload):
        return None

    monkeypatch.setattr(_shared, "_read_material_bytes", _fake_read)
    monkeypatch.setattr(_shared, "_register_report_artifact", _fake_register)
    monkeypatch.setattr(
        "backend.services.document_conversion.convert_docx_to_pdf",
        lambda docx_bytes: PDF_STUB,
    )

    payload = json.loads(await _tool(ctx).ainvoke({"semester_task_id": task_id}))

    assert "登记下载文件失败" in payload["error"]


async def test_non_owner_is_denied(monkeypatch, db_factory, stub_pipeline):
    _patch_factory(monkeypatch, db_factory)
    task_id = await _seed_task(db_factory, owner="teacher-a")
    await _seed_report_versions(db_factory, task_id, versions=[(1, "uf_docx_1")])

    intruder = ToolRuntimeContext(
        request_id="r2",
        user_id="teacher-b",
        agent_code="course_eval",
        domain="course-outcome-evaluation",
        thread_id="t2",
    )
    payload = json.loads(await _tool(intruder).ainvoke({"semester_task_id": task_id}))

    assert "error" in payload
    assert not stub_pipeline["converted"]


class _TrackedSession:
    """代理会话，记录 async with 的进出（dunder 必须定义在类型上才生效）。"""

    def __init__(self, session, state):
        self._session = session
        self._state = state

    async def __aenter__(self):
        self._state["open"] += 1
        return await self._session.__aenter__()

    async def __aexit__(self, *args):
        self._state["open"] -= 1
        return await self._session.__aexit__(*args)

    def __getattr__(self, item):
        return getattr(self._session, item)


async def test_conversion_runs_outside_db_session(
    monkeypatch, db_factory, ctx, stub_pipeline
):
    """转换期间不得持有会话：LibreOffice 秒级冷启动会阻塞同任务写工具等锁。"""
    from backend.application.course_eval.agent_tools import _shared

    _patch_factory(monkeypatch, db_factory)
    state = {"open": 0, "during_convert": None}
    real_open_session = _shared._open_session

    async def _counting_open_session():
        return _TrackedSession(await real_open_session(), state)

    def _fake_convert(docx_bytes):
        state["during_convert"] = state["open"]
        return PDF_STUB

    monkeypatch.setattr(_shared, "_open_session", _counting_open_session)
    monkeypatch.setattr(
        "backend.services.document_conversion.convert_docx_to_pdf", _fake_convert
    )

    task_id = await _seed_task(db_factory)
    await _seed_report_versions(db_factory, task_id, versions=[(1, "uf_docx_1")])
    await _tool(ctx).ainvoke({"semester_task_id": task_id})

    assert state["during_convert"] == 0


# ── 注册表 / 策略接线 ─────────────────────────────────────────────────


def test_export_report_pdf_spec_contract():
    from backend.application.tools.registry import ToolRegistry

    spec = ToolRegistry().get("export_report_pdf")
    assert spec.risk_level == "write"
    assert spec.side_effect == "artifact_write"
    assert spec.domains == {"course-outcome-evaluation"}


def test_export_report_pdf_in_course_eval_policy():
    from backend.application.tools.registry import ToolRegistry

    names = {
        t.name
        for t in ToolRegistry().resolve_policy_tools(
            policy_name="course_eval_default",
            domain="course-outcome-evaluation",
            workflow="full",
            selected_skills=set(),
        )
    }
    assert "export_report_pdf" in names
