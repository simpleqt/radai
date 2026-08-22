"""Tests for the course-eval Agent tools (T4-01，智能体化后)。

智能体化后工具集 = 7 读工具（真实读库）+ 8 写工具（走 service 层）。
写工具的全链路行为见 test_course_eval_agent_tools.py；这里验证注册与契约。
"""

from __future__ import annotations

from backend.application.course_eval.tools import get_course_eval_tools

READ_TOOLS = {
    "list_course_eval_tasks",
    "get_course_stats",
    "get_achievement",
    "suggest_goal_mapping",
    "draft_report_narrative",
    "check_report_quality",
    "load_previous_reports",
}

WRITE_TOOLS = {
    "transition_task",
    "extract_grade_sheet",
    "extract_paper",
    "extract_syllabus",
    "run_validation",
    "compute_statistics",
    "generate_report",
    "export_report_pdf",
}


def test_all_tools_registered():
    tools = get_course_eval_tools()
    names = {t.name for t in tools}
    assert READ_TOOLS | WRITE_TOOLS == names


def test_read_and_write_tools_both_present():
    names = {t.name for t in get_course_eval_tools()}
    assert READ_TOOLS <= names
    assert WRITE_TOOLS <= names


def test_suggest_goal_mapping_is_readonly():
    """映射建议只产出建议不落库（写操作走 REST PUT /mappings）。"""
    tool = next(t for t in get_course_eval_tools() if t.name == "suggest_goal_mapping")
    assert "PUT /mappings" in tool.description or "REST" in tool.description


def test_transition_tool_requires_task_and_target():
    tool = next(t for t in get_course_eval_tools() if t.name == "transition_task")
    assert "semester_task_id" in tool.args
    assert "target" in tool.args


def test_extract_tools_require_material():
    for name in ("extract_grade_sheet", "extract_paper", "extract_syllabus"):
        tool = next(t for t in get_course_eval_tools() if t.name == name)
        assert "semester_task_id" in tool.args
        assert "material_id" in tool.args


def test_skill_frontmatter_has_domain():
    """course-outcome-eval SKILL frontmatter 必须含 domain（前端技能可见性依赖）。

    缺 domain 时前端技能选择器按 agent domain 过滤后不可见（显示「没有匹配的技能」），
    但服务端 default_skills 仍加载——UI/服务不一致。
    """
    import re
    from pathlib import Path

    skill = Path(
        __file__).resolve().parents[2] / "backend/skills/public/course-outcome-eval/SKILL.md"
    text = skill.read_text(encoding="utf-8")
    m = re.search(r"^domain:\s*(.+)$", text, re.M)
    assert m, "SKILL.md frontmatter 缺少 domain"
    assert m.group(1).strip() == "course-outcome-evaluation"
