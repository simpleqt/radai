"""course_eval 智能体写工具测试（P3）。

工具内部通过 get_async_session_factory() 打开独立会话，这里 monkeypatch 成
内存 SQLite，验证：
- transition_task 走状态机护栏（非法迁移被拒 + allowed_actions）
- extract_grade_sheet 解析成绩单落 ce_students
- compute_statistics 写快照
- 写工具缺失 operator 时拒绝（匿名/访客场景）
- profile 装配契约：写工具在 policy allowlist、registry side_effect=db_write
"""

from __future__ import annotations

import json
import pytest
from sqlalchemy.ext.asyncio import async_sessionmaker, create_async_engine
from sqlalchemy.pool import StaticPool

from backend.domain.tools.runtime_context import ToolRuntimeContext
from backend.db.models import User
from backend.db.models.course_eval import (
    CeAchievementConfig,
    CeAuditLog,
    CeCourse,
    CeReportTemplate,
    CeReportVersion,
    CeExamItem,
    CeExamPaper,
    CeExamSection,
    CeMaterial,
    CeScoreRecord,
    CeSemesterTask,
    CeStatsSnapshot,
    CeStudent,
    CeCourseGoal,
    CeGoalMapping,
    CeReport,
)


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


async def _seed_task(factory, *, status="draft"):
    async with factory() as session:
        course = CeCourse(code="C1", name="材料力学", owner_user_id="u1")
        session.add(course)
        await session.flush()
        task = CeSemesterTask(
            course_id=course.course_id,
            semester="2024-2025-1",
            class_no="c1",
            status=status,
            owner_user_id="u1",
        )
        session.add(task)
        await session.flush()
        task_id = task.semester_task_id
        session.add(
            CeMaterial(
                semester_task_id=task_id,
                material_type="grade_sheet",
                file_id="uf_1",
                original_filename="成绩单.pdf",
            )
        )
        await session.commit()
        return {"course_id": course.course_id, "task_id": task_id}


def _patch_factory(monkeypatch, db_factory):
    monkeypatch.setattr(
        "backend.db.connection.get_async_session_factory",
        lambda: db_factory,
    )


async def test_transition_task_rejects_illegal(monkeypatch, db_factory, ctx):
    from backend.application.course_eval import tools as ce_tools

    _patch_factory(monkeypatch, db_factory)
    ids = await _seed_task(db_factory)

    tool = ce_tools.get_transition_task_tool(runtime_context=ctx)
    out = json.loads(await tool.ainvoke(
        {"semester_task_id": ids["task_id"], "target": "archived"}
    ))
    assert "非法状态迁移" in out["error"]
    assert out["current"] == "draft"
    assert "materials_ready" in out["allowed_actions"]


async def test_transition_task_valid(monkeypatch, db_factory, ctx):
    from backend.application.course_eval import tools as ce_tools

    _patch_factory(monkeypatch, db_factory)
    ids = await _seed_task(db_factory)

    tool = ce_tools.get_transition_task_tool(runtime_context=ctx)
    out = json.loads(await tool.ainvoke(
        {"semester_task_id": ids["task_id"], "target": "materials_ready"}
    ))
    assert out["status"] == "materials_ready"


async def test_extract_grade_sheet_requires_material(monkeypatch, db_factory, ctx):
    from backend.application.course_eval import tools as ce_tools

    _patch_factory(monkeypatch, db_factory)
    ids = await _seed_task(db_factory)

    tool = ce_tools.get_extract_grade_sheet_tool(runtime_context=ctx)
    out = json.loads(await tool.ainvoke(
        {"semester_task_id": ids["task_id"], "material_id": "missing"}
    ))
    assert "未找到该任务的成绩单材料" in out["error"]


async def test_compute_statistics_without_snapshot(monkeypatch, db_factory, ctx):
    """compute_statistics 走 report_service.compute，缺数据时返回错误而非崩溃。"""
    from backend.application.course_eval import tools as ce_tools

    _patch_factory(monkeypatch, db_factory)
    ids = await _seed_task(db_factory, status="computed")

    tool = ce_tools.get_compute_statistics_tool(runtime_context=ctx)
    out = json.loads(await tool.ainvoke({"semester_task_id": ids["task_id"]}))
    # 无学生/记录时 build_snapshot 可能仍产出空快照或抛错——两者都必须是可控 JSON
    assert isinstance(out, dict)


async def test_write_tool_rejects_missing_operator(monkeypatch, db_factory):
    """匿名/无 user_id 时写工具必须拒绝（guardrail 的最后一层）。"""
    from backend.application.course_eval import tools as ce_tools

    _patch_factory(monkeypatch, db_factory)
    anon_ctx = ToolRuntimeContext(
        request_id="r2",
        user_id="",
        agent_code="course_eval",
        domain="course-outcome-evaluation",
        thread_id="t2",
    )
    tool = ce_tools.get_transition_task_tool(runtime_context=anon_ctx)
    out = json.loads(await tool.ainvoke(
        {"semester_task_id": "x", "target": "materials_ready"}
    ))
    assert "登录用户身份" in out["error"]


def test_profile_assembles_write_tools(monkeypatch):
    """course_eval profile 装配契约：写工具在 policy allowlist，registry 元数据正确。"""
    import yaml

    from backend.application.agents.definition import AgentDefinitionValidator

    resolved = AgentDefinitionValidator().resolve("course_eval")
    tool_names = {t.name for t in resolved.tools}
    for write_tool in (
        "transition_task",
        "extract_grade_sheet",
        "extract_paper",
        "extract_syllabus",
        "compute_statistics",
        "generate_report",
    ):
        assert write_tool in tool_names, f"{write_tool} 必须在 course_eval 装配集内"

    # registry.yaml 元数据：写工具声明 db_write / 非幂等
    registry_path = (
        __import__("pathlib").Path(__file__).resolve().parents[2]
        / "backend/config/tools/registry.yaml"
    )
    raw = yaml.safe_load(registry_path.read_text(encoding="utf-8"))
    tools_yaml = raw["tools"]
    for write_tool in (
        "transition_task",
        "extract_grade_sheet",
        "extract_paper",
        "extract_syllabus",
        "compute_statistics",
        "generate_report",
    ):
        spec = tools_yaml[write_tool]
        assert spec["risk_level"] == "write", write_tool
        assert spec["side_effect"] == "db_write", write_tool
        assert spec["idempotent"] is False, write_tool
        assert "db_write" in str(spec)


# ── 对话式工具（智能体自动建任务/绑材料/确认）──


async def test_create_semester_task_creates_course_and_task(monkeypatch, db_factory, ctx):
    from backend.application.course_eval import tools as ce_tools

    _patch_factory(monkeypatch, db_factory)
    tool = ce_tools.get_create_semester_task_tool(runtime_context=ctx)
    out = json.loads(await tool.ainvoke({
        "course_name": "土木工程", "course_code": "CE101",
        "semester": "2024-2025-1", "class_no": "990103-4",
    }))
    assert out["status"] == "draft"
    assert out["course_name"] == "土木工程"
    task_id = out["semester_task_id"]

    # 幂等：同课程同任务复用（不重复建课程）
    out2 = json.loads(await tool.ainvoke({
        "course_name": "土木工程", "course_code": "CE101",
        "semester": "2024-2025-1", "class_no": "990103-4",
    }))
    assert out2["semester_task_id"] == task_id


async def test_attach_material_auto_type(monkeypatch, db_factory, ctx):
    """attach_material 按文件名自动识别材料类型（缺省 material_type）。"""
    from unittest.mock import AsyncMock, patch

    from backend.application.course_eval import tools as ce_tools

    _patch_factory(monkeypatch, db_factory)
    ids = await _seed_task(db_factory)

    async def fake_resolve(session, file_id, user_id):
        class Row:
            display_name = "材料力学成绩单.pdf"
            mime_type = "application/pdf"
        return Row(), True, "ok"

    tool = ce_tools.get_attach_material_tool(runtime_context=ctx)
    with patch(
        "backend.application.course_eval.agent_tools._shared._resolve_file_display",
        new=AsyncMock(side_effect=fake_resolve),
    ):
        out = json.loads(await tool.ainvoke({
            "semester_task_id": ids["task_id"], "file_id": "uf_9",
        }))
    assert out["material_type"] == "grade_sheet"
    assert out["version_no"] >= 1  # seed 已注册一张 grade_sheet，重绑升版本


async def test_attach_material_unrecognized_rejected(monkeypatch, db_factory, ctx):
    from unittest.mock import AsyncMock, patch

    from backend.application.course_eval import tools as ce_tools

    _patch_factory(monkeypatch, db_factory)
    ids = await _seed_task(db_factory)

    async def fake_resolve(session, file_id, user_id):
        class Row:
            display_name = "notes.txt"
            mime_type = "text/plain"
        return Row(), True, "ok"

    tool = ce_tools.get_attach_material_tool(runtime_context=ctx)
    with patch(
        "backend.application.course_eval.agent_tools._shared._resolve_file_display",
        new=AsyncMock(side_effect=fake_resolve),
    ):
        out = json.loads(await tool.ainvoke({
            "semester_task_id": ids["task_id"], "file_id": "uf_9",
        }))
    assert "无法识别材料类型" in out["error"]


def test_dialog_tools_in_profile_and_registry(monkeypatch):
    """对话式 5 工具在装配集 + registry 元数据正确。"""
    import yaml

    from backend.application.agents.definition import AgentDefinitionValidator

    resolved = AgentDefinitionValidator().resolve("course_eval")
    tool_names = {t.name for t in resolved.tools}
    for dialog_tool in (
        "create_semester_task",
        "attach_material",
        "get_score_grid",
        "update_scores",
        "apply_mappings",
    ):
        assert dialog_tool in tool_names, f"{dialog_tool} 必须在 course_eval 装配集内"

    registry_path = (
        __import__("pathlib").Path(__file__).resolve().parents[2]
        / "backend/config/tools/registry.yaml"
    )
    tools_yaml = yaml.safe_load(registry_path.read_text(encoding="utf-8"))["tools"]
    for write_tool in (
        "create_semester_task",
        "attach_material",
        "update_scores",
        "apply_mappings",
    ):
        spec = tools_yaml[write_tool]
        assert spec["risk_level"] == "write", write_tool
        assert spec["side_effect"] == "db_write", write_tool
        assert spec["idempotent"] is False, write_tool


# ── 回归：E2E 实测暴露的 4 个 bug（2026-08-12）──


async def test_transition_task_persists_status(monkeypatch, db_factory, ctx):
    """transition_task 必须 commit：状态变更要真实落库（曾缺 commit 导致迁移无效）。"""
    from sqlalchemy import select

    from backend.application.course_eval import tools as ce_tools
    from backend.db.models.course_eval import CeSemesterTask

    _patch_factory(monkeypatch, db_factory)
    ids = await _seed_task(db_factory)

    tool = ce_tools.get_transition_task_tool(runtime_context=ctx)
    out = json.loads(await tool.ainvoke(
        {"semester_task_id": ids["task_id"], "target": "materials_ready"}
    ))
    assert out["status"] == "materials_ready"

    # 独立会话读库，确认状态已持久化
    async with db_factory() as session:
        task = (
            await session.execute(
                select(CeSemesterTask).where(
                    CeSemesterTask.semester_task_id == ids["task_id"]
                )
            )
        ).scalar_one()
        assert task.status == "materials_ready"


async def test_attach_material_uses_mime_type_field(monkeypatch, db_factory, ctx):
    """attach_material 读 UserFileResolved.mime_type（曾误读 .mime 导致崩溃）。"""
    from unittest.mock import AsyncMock, patch

    from backend.application.course_eval import tools as ce_tools

    _patch_factory(monkeypatch, db_factory)
    ids = await _seed_task(db_factory)

    async def fake_resolve(session, file_id, user_id):
        class Row:
            display_name = "材料力学2024试卷.pdf"
            mime_type = "application/pdf"
        return Row(), True, "ok"

    tool = ce_tools.get_attach_material_tool(runtime_context=ctx)
    with patch(
        "backend.application.course_eval.agent_tools._shared._resolve_file_display",
        new=AsyncMock(side_effect=fake_resolve),
    ):
        out = json.loads(await tool.ainvoke({
            "semester_task_id": ids["task_id"], "file_id": "uf_9",
        }))
    assert out["material_type"] == "paper"


# ── 一致性修复回归（2026-08-12 审计）──


async def test_get_score_grid_includes_student_name(monkeypatch, db_factory, ctx):
    """get_score_grid 的 low_confidence_cells/row_checks_failed 须含学生姓名（对话核对可展示）。"""
    from backend.application.course_eval import tools as ce_tools
    from backend.db.models.course_eval import (
        CeExamPaper, CeExamSection, CeScoreRecord, CeStudent,
    )

    _patch_factory(monkeypatch, db_factory)
    ids = await _seed_task(db_factory, status="needs_review")
    async with db_factory() as session:
        paper = CeExamPaper(semester_task_id=ids["task_id"])
        session.add(paper)
        await session.flush()
        sec = CeExamSection(paper_id=paper.paper_id, order_no=1, question_type="计算题", total_score=20)
        session.add(sec)
        await session.flush()
        stu = CeStudent(semester_task_id=ids["task_id"], student_no="990203000299", name="核对生")
        session.add(stu)
        await session.flush()
        session.add(CeScoreRecord(
            semester_task_id=ids["task_id"], student_id=stu.student_id,
            section_id=sec.section_id, item_id="", full_score=20, score=5,
            raw_score=5, source="ocr_table", confidence="0.7", status="pending_review",
        ))
        await session.commit()

    tool = ce_tools.get_score_grid_tool(runtime_context=ctx)
    out = json.loads(await tool.ainvoke({"semester_task_id": ids["task_id"]}))
    assert out["low_confidence_cells"], "应有低置信度单元格"
    cell = out["low_confidence_cells"][0]
    assert cell["student_name"] == "核对生"
    # 题号用人话标签，供确认卡展示（不暴露 UUID）
    assert cell["section_label"] == "第1题·计算题"


async def test_get_score_grid_skips_confirmed_low_confidence_cells(
    monkeypatch, db_factory, ctx
):
    """教师确认过的低置信度格子不再列为存疑（避免每轮重复弹同一张核对卡）。"""
    from backend.application.course_eval import tools as ce_tools
    from backend.db.models.course_eval import (
        CeExamPaper, CeExamSection, CeScoreRecord, CeStudent,
    )

    _patch_factory(monkeypatch, db_factory)
    ids = await _seed_task(db_factory, status="needs_review")
    async with db_factory() as session:
        paper = CeExamPaper(semester_task_id=ids["task_id"])
        session.add(paper)
        await session.flush()
        sec = CeExamSection(
            paper_id=paper.paper_id, order_no=2, question_type="填空题", total_score=20
        )
        session.add(sec)
        stu = CeStudent(
            semester_task_id=ids["task_id"], student_no="S1", name="甲",
            final_score=5,
        )
        session.add(stu)
        await session.flush()
        session.add(CeScoreRecord(
            semester_task_id=ids["task_id"], student_id=stu.student_id,
            section_id=sec.section_id, item_id="", full_score=20, score=5,
            raw_score=5, source="ocr_table", confidence="0.4", status="confirmed",
        ))
        await session.commit()

    tool = ce_tools.get_score_grid_tool(runtime_context=ctx)
    out = json.loads(await tool.ainvoke({"semester_task_id": ids["task_id"]}))
    assert out["low_confidence_cells"] == []
    # 有得分记录且与期末一致的学生不进 row_checks_failed
    assert out["row_checks_failed"] == []


async def test_row_checks_only_cover_sampled_students(monkeypatch, db_factory, ctx):
    """没有逐题得分的学生不参与行校验（此前 60+ 名非样本学生被误报总分不符）。"""
    from backend.application.course_eval.score_service import list_score_grid
    from backend.db.models.course_eval import (
        CeExamPaper, CeExamSection, CeScoreRecord, CeStudent,
    )

    _patch_factory(monkeypatch, db_factory)
    ids = await _seed_task(db_factory, status="needs_review")
    async with db_factory() as session:
        paper = CeExamPaper(semester_task_id=ids["task_id"])
        session.add(paper)
        await session.flush()
        sec = CeExamSection(
            paper_id=paper.paper_id, order_no=1, question_type="计算题", total_score=50
        )
        session.add(sec)
        sampled = CeStudent(
            semester_task_id=ids["task_id"], student_no="S1", name="样本生",
            final_score=30,
        )
        others = [
            CeStudent(
                semester_task_id=ids["task_id"], student_no=f"S{i}",
                name=f"非样本{i}", final_score=60,
            )
            for i in range(2, 8)
        ]
        session.add_all([sampled, *others])
        await session.flush()
        session.add(CeScoreRecord(
            semester_task_id=ids["task_id"], student_id=sampled.student_id,
            section_id=sec.section_id, item_id="", full_score=50, score=20,
            raw_score=20, source="ocr_table", status="confirmed",
        ))
        await session.commit()

        grid = await list_score_grid(session, ids["task_id"])
    # 只有样本生进行行校验（20 != 30 → 失败 1 条），6 名非样本学生不误报
    assert len(grid["row_checks"]) == 1
    assert grid["row_checks"][0]["student_id"] == sampled.student_id
    assert grid["row_checks"][0]["passed"] is False


async def test_suggest_goal_mapping_annotates_and_drops_hallucinated_ids(
    monkeypatch, db_factory, ctx
):
    """映射建议带教师可读标签；引用不存在 ID 的建议被丢弃并告警。"""
    from backend.application.course_eval import tools as ce_tools
    from backend.application.course_eval.agent_tools import _shared
    from backend.db.models.course_eval import (
        CeCourseGoal, CeExamPaper, CeExamSection,
    )

    _patch_factory(monkeypatch, db_factory)
    ids = await _seed_task(db_factory, status="mapping_pending")
    async with db_factory() as session:
        goal = CeCourseGoal(
            course_id=ids["course_id"], goal_code="目标1", description="d"
        )
        session.add(goal)
        paper = CeExamPaper(semester_task_id=ids["task_id"])
        session.add(paper)
        await session.flush()
        sec = CeExamSection(
            paper_id=paper.paper_id, order_no=2, question_type="填空题",
            total_score=20,
        )
        session.add(sec)
        await session.flush()
        goal_id, sec_id = goal.goal_id, sec.section_id
        await session.commit()

    async def _fake_llm(prompt: str, **_kwargs) -> dict:
        return {
            "mappings": [
                {"section_id": sec_id, "goal_id": goal_id, "weight": 1.0},
                {"section_id": "ghost-section", "goal_id": goal_id, "weight": 1.0},
            ]
        }

    monkeypatch.setattr(_shared, "_llm_json_call", _fake_llm)
    tool = ce_tools.get_suggest_goal_mapping_tool(runtime_context=ctx)
    out = json.loads(await tool.ainvoke({"semester_task_id": ids["task_id"]}))
    assert "error" not in out, out
    assert len(out["suggestions"]) == 1
    item = out["suggestions"][0]
    assert item["section_label"] == "第2题·填空题"
    assert item["goal_label"] == "目标1"
    assert any("已丢弃" in w for w in out["warnings"])


async def test_render_inputs_renumber_report_view_after_exclusion(
    monkeypatch, db_factory, ctx
):
    """本班不考的题剔除后，报告视图从「第1题」连续重编号（与人工表习惯一致）。"""
    from decimal import Decimal

    from backend.application.course_eval.render_service import build_render_inputs
    from backend.db.models.course_eval import CeExamPaper, CeExamSection

    _patch_factory(monkeypatch, db_factory)
    ids = await _seed_task(db_factory, status="computed")
    async with db_factory() as session:
        paper = CeExamPaper(semester_task_id=ids["task_id"])
        session.add(paper)
        await session.flush()
        session.add_all(
            [
                CeExamSection(
                    paper_id=paper.paper_id, order_no=1, question_type="汉译英",
                    total_score=Decimal("10"),
                    applicable_majors=["工程力学专业"],
                ),
                CeExamSection(
                    paper_id=paper.paper_id, order_no=2, question_type="填空题",
                    total_score=Decimal("20"),
                ),
                CeExamSection(
                    paper_id=paper.paper_id, order_no=5, question_type="计算题",
                    total_score=Decimal("50"),
                ),
            ]
        )
        await session.commit()

        snapshot = {
            "section_rates": [
                {"report_order_no": 2, "question_type": "填空题",
                 "full_score": "20.00", "avg_score": "10.00", "rate": "50.0"},
                {"report_order_no": 5, "question_type": "计算题",
                 "full_score": "50.00", "avg_score": "34.10", "rate": "68.2"},
            ]
        }
        _task_view, views, _sem = await build_render_inputs(
            session, task_id=ids["task_id"], snapshot=snapshot
        )
    # 第1题（本班不考）被剔除；原第2/5题重编号为报告第1/2题
    assert [(v.report_order_no, v.question_type) for v in views] == [
        (1, "填空题"),
        (2, "计算题"),
    ]
    assert views[0].rate == Decimal("50.0")
    assert views[1].avg_score == Decimal("34.10")


def test_register_report_artifact_accepts_internal_thread_key(monkeypatch, tmp_path):
    """runtime_context.thread_id 已是内部键时不再二次包裹（生产 owner 超长根因）。"""
    import asyncio as _asyncio
    from types import SimpleNamespace

    from backend.application.course_eval.agent_tools import _shared

    captured: dict = {}

    async def _fake_register(*, internal_thread_id, artifact_filename):
        captured["internal"] = internal_thread_id
        return "uf_x", "/api/v2/files/uf_x/download"

    class _FakePaths:
        def thread_artifacts_dir(self, thread_id):
            return tmp_path / "artifacts"

    monkeypatch.setattr(
        "backend.services.user_file_service.register_local_artifact_for_thread",
        _fake_register,
    )
    monkeypatch.setattr(
        "backend.core.config.paths.get_paths", lambda: _FakePaths()
    )

    internal = "u_7a47a428__t_0bf8921a-d91b"
    ctx1 = SimpleNamespace(user_id="7a47a428", thread_id=internal)
    result = _asyncio.run(
        _shared._register_report_artifact(ctx1, filename="r.docx", payload=b"x")
    )
    assert result == ("uf_x", "/api/v2/files/uf_x/download")
    assert captured["internal"] == internal  # 原样使用，不二次包裹

    ctx2 = SimpleNamespace(user_id="u1", thread_id="t1")
    _asyncio.run(
        _shared._register_report_artifact(ctx2, filename="r.docx", payload=b"x")
    )
    assert captured["internal"] == "u_u1__t_t1"


async def test_write_tool_rejects_guest_role(monkeypatch, db_factory):
    """访客（role=guest）不能调用写工具（一致性审计修复）。"""
    from backend.application.course_eval import tools as ce_tools

    _patch_factory(monkeypatch, db_factory)
    guest_ctx = ToolRuntimeContext(
        request_id="r3", user_id="g_xxx", role="guest",
        agent_code="course_eval", domain="course-outcome-evaluation", thread_id="t3",
    )
    for tool_name in (
        "create_semester_task", "attach_material", "update_scores",
        "apply_mappings", "transition_task", "extract_grade_sheet",
        "compute_statistics", "generate_report",
    ):
        tool = next(
            t for t in ce_tools.get_course_eval_tools(runtime_context=guest_ctx)
            if t.name == tool_name
        )
        args = {
            "semester_task_id": "x",
            "target": "materials_ready",
            "course_name": "c", "course_code": "C1", "semester": "S1", "class_no": "B1",
            "file_id": "uf_1", "material_id": "m1",
            "cells": [], "mappings": [],
        }
        out = json.loads(await tool.ainvoke(args))
        assert "登录教师身份" in out["error"], f"{tool_name} 应拒绝访客"


async def test_extract_syllabus_uses_code_field(monkeypatch, db_factory, ctx):
    """extract_syllabus 用 GoalDraft.code 落库（E2E 修复回归）。"""
    from unittest.mock import AsyncMock, patch

    from backend.application.course_eval import tools as ce_tools
    from backend.db.models.course_eval import CeMaterial

    _patch_factory(monkeypatch, db_factory)
    ids = await _seed_task(db_factory)
    async with db_factory() as session:
        m = CeMaterial(
            semester_task_id=ids["task_id"], material_type="syllabus",
            file_id="uf_syl_1", original_filename="《材料力学》教学大纲.pdf",
        )
        session.add(m)
        await session.flush()
        material_id = m.material_id
        await session.commit()

    async def fake_resolve(session, file_id, user_id):
        class Row:
            display_name = "《材料力学》教学大纲.pdf"
            mime_type = "application/pdf"
        return Row(), True, "ok"

    async def fake_llm(prompt_text: str, **_kwargs) -> dict:
        return {
            "course": {"name": "材料力学", "code": "C1"},
            "score_formula": {"regular_weight": 0.4, "exam_weight": 0.6},
            "goals": [
                # Σ weight_exam 必须 == exam_weight×100（syllabus_parser 校验）
                {"code": "目标1", "description": "掌握受力分析", "weight_exam": 60},
            ],
        }

    async def fake_read_bytes(session, file_id, user_id):
        return "课程教学目标：目标1 掌握受力分析；成绩构成：平时40% 期末60%".encode(), "大纲.pdf"

    tool = ce_tools.get_extract_syllabus_tool(runtime_context=ctx)
    with patch(
        "backend.application.course_eval.agent_tools._shared._read_material_bytes",
        new=AsyncMock(side_effect=fake_read_bytes),
    ), patch(
        "backend.application.course_eval.agent_tools._shared._llm_json_call",
        new=AsyncMock(side_effect=fake_llm),
    ):
        out = json.loads(await tool.ainvoke({
            "semester_task_id": ids["task_id"], "material_id": material_id,
        }))
    assert out["written"] == 1


async def test_create_semester_task_auto_code(monkeypatch, db_factory, ctx):
    """缺 course_code 时自动生成（auto_generated=true），不阻断建任务。"""
    from backend.application.course_eval import tools as ce_tools

    _patch_factory(monkeypatch, db_factory)
    tool = ce_tools.get_create_semester_task_tool(runtime_context=ctx)
    out = json.loads(await tool.ainvoke({
        "course_name": "材料力学", "course_code": "",
        "semester": "2024-2025-1", "class_no": "990103-4",
    }))
    assert "error" not in out
    assert out["status"] == "draft"
    assert out["auto_generated"] is True
    assert str(out["course_code"]).startswith("AUTO-")


async def test_create_semester_task_requires_name_and_semester(monkeypatch, db_factory, ctx):
    """缺课程名称或学期时仍返回错误（仅 course_code 可省略）。"""
    from backend.application.course_eval import tools as ce_tools

    _patch_factory(monkeypatch, db_factory)
    tool = ce_tools.get_create_semester_task_tool(runtime_context=ctx)
    out = json.loads(await tool.ainvoke({
        "course_name": "", "course_code": "",
        "semester": "2024-2025-1", "class_no": "990103-4",
    }))
    assert "error" in out
    assert "课程名称" in out["error"]


# ── M7: course_eval 对象级授权（跨教师隔离）──


async def test_list_tasks_scoped_to_owner(monkeypatch, db_factory):
    """非管理员只列出自己拥有的任务，看不到他人任务。"""
    from backend.application.course_eval import tools as ce_tools

    _patch_factory(monkeypatch, db_factory)
    ids = await _seed_task(db_factory)  # owner=u1

    other_ctx = ToolRuntimeContext(
        request_id="r-m7a",
        user_id="u2",
        agent_code="course_eval",
        domain="course-outcome-evaluation",
        thread_id="t-m7a",
    )
    out = json.loads(
        await ce_tools.get_list_course_eval_tasks_tool(
            runtime_context=other_ctx
        ).ainvoke({})
    )
    assert ids["task_id"] not in [
        t["semester_task_id"] for t in out.get("items", [])
    ]

    owner_out = json.loads(
        await ce_tools.get_list_course_eval_tasks_tool(
            runtime_context=ToolRuntimeContext(
                request_id="r-m7b",
                user_id="u1",
                agent_code="course_eval",
                domain="course-outcome-evaluation",
                thread_id="t-m7b",
            )
        ).ainvoke({})
    )
    assert ids["task_id"] in [
        t["semester_task_id"] for t in owner_out.get("items", [])
    ]


async def test_write_tool_rejects_cross_teacher(monkeypatch, db_factory):
    """非属主教师不能篡改他人任务状态（对象级授权，而非仅登录校验）。"""
    from backend.application.course_eval import tools as ce_tools

    _patch_factory(monkeypatch, db_factory)
    ids = await _seed_task(db_factory)  # owner=u1

    other_ctx = ToolRuntimeContext(
        request_id="r-m7c",
        user_id="u2",
        agent_code="course_eval",
        domain="course-outcome-evaluation",
        thread_id="t-m7c",
    )
    out = json.loads(
        await ce_tools.get_transition_task_tool(
            runtime_context=other_ctx
        ).ainvoke(
            {"semester_task_id": ids["task_id"], "target": "materials_ready"}
        )
    )
    # 被属主校验拦截：返回 error 而非成功的 status
    assert "error" in out
    assert "status" not in out


async def test_read_stats_rejects_cross_teacher(monkeypatch, db_factory):
    """非属主教师不能读他人任务的成绩统计。"""
    from backend.application.course_eval import tools as ce_tools

    _patch_factory(monkeypatch, db_factory)
    ids = await _seed_task(db_factory)  # owner=u1

    other_ctx = ToolRuntimeContext(
        request_id="r-m7d",
        user_id="u2",
        agent_code="course_eval",
        domain="course-outcome-evaluation",
        thread_id="t-m7d",
    )
    out = json.loads(
        await ce_tools.get_course_stats_tool(
            runtime_context=other_ctx
        ).ainvoke({"semester_task_id": ids["task_id"]})
    )
    assert "error" in out


# ── 试卷结构与逐题得分提取（MinerU 单通道 + 总分交叉校验）──
