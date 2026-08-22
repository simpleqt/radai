from __future__ import annotations

import asyncio
import json

from backend.core.skills.loader import load_skills
from backend.core.skills.skill_runtime_state import (
    reset_request_skill_state,
    set_request_skill_state,
)
from backend.application.tools.tool_assembly_pipeline import ToolAssemblyPipeline
from backend.application.agents.profile_loader import AgentProfileLoader
from backend.application.agents.prompt_templates.csh import build_system_prompt


def test_csh_hydrogeology_skill_is_available() -> None:
    skills = {skill.name: skill for skill in load_skills(enabled_only=True)}

    skill = skills["csh-hydrogeology"]
    assert skill.category in {"public", "custom"}
    if skill.category == "public":
        assert "multi_kb_search" in skill.tool_names
        assert "web_search" in skill.tool_names
    assert "水文地质" in skill.description or "地下水" in skill.description


def test_csh_full_prompt_requires_faithful_complete_source_tables() -> None:
    prompt = build_system_prompt()

    assert "来源表格/清单忠实转录" in prompt
    assert "跨页续表" in prompt
    assert "禁止把原表改写成另一套分类" in prompt
    assert "只用于辅助检索" in prompt
    assert "合并单元格、箭头或嵌套布局" in prompt


def test_csh_full_agent_defaults_to_hydrogeology_skill() -> None:
    profile = AgentProfileLoader().load("csh")
    selected = set(profile.default_skills)

    assert selected == {"csh-hydrogeology", "general-table-analysis"}
    assert profile.default_workflow == "full"
    assert "lightweight" in profile.allowed_workflows


def test_ne_and_csh_default_to_general_table_analysis_skill() -> None:
    loader = AgentProfileLoader()

    assert "general-table-analysis" in loader.load("ne").default_skills
    assert "general-table-analysis" in loader.load("csh").default_skills


def test_general_table_analysis_skill_is_available() -> None:
    skills = {skill.name: skill for skill in load_skills(enabled_only=True)}

    skill = skills["general-table-analysis"]
    assert skill.category == "public"
    assert "inspect_excel_data" in skill.tool_names
    assert "query_excel_data" in skill.tool_names
    assert "visualize_excel_data" in skill.tool_names
    assert "通用表格分析" in skill.description


def test_csh_multi_kb_search_requires_hydrogeology_skill_loaded() -> None:
    pipeline = ToolAssemblyPipeline(
        kb_ids=["kb-csh"],
        selected_skills={"csh-hydrogeology"},
        domain="csh-hydrogeology",
    )
    search_tool = pipeline._create_multi_kb_search_tool()

    token = set_request_skill_state({"csh-hydrogeology"})
    try:
        result = asyncio.run(search_tool.ainvoke({"query": "地下水污染运移"}))
    finally:
        reset_request_skill_state(token)

    payload = json.loads(result)
    assert payload["error"] == "skill_not_loaded"
    assert payload["required_skill"] == "csh-hydrogeology"
    assert 'read_local_skill_file("csh-hydrogeology")' in payload["message"]
