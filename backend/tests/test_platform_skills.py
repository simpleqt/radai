"""HTML PPT 与技能清单契约（拆自 test_agent_platform_standardization.py）。"""

from backend.application.skills.manifest_loader import SkillManifestLoader
from backend.application.tools.providers.html_ppt_tool import (
    FULL_DECKS_DIR,
    SKILL_ROOT,
    _clean_tool_text_arg,
    _parse_slides_outline,
    _resolve_effective_outline,
    _safe_html_filename,
)
from backend.application.tools.registry import ToolRegistry
from backend.domain.agents.workflows import SUPPORTED_WORKFLOWS
from pathlib import Path


def test_html_ppt_filename_strips_malformed_parameter_suffix():
    filename = _safe_html_filename(
        "groundwater-remediation-tech-selection.html</parameter",
        "基于水文地质条件的地下水修复技术选择</parameter",
    )

    assert filename == "groundwater-remediation-tech-selection.html"
    assert "parameter" not in filename


def test_html_ppt_provider_resolves_skill_assets_from_backend_root():
    assert SKILL_ROOT == Path("backend/skills/public/html-ppt").resolve()
    assert FULL_DECKS_DIR.exists()
    assert (FULL_DECKS_DIR / "tech-sharing" / "index.html").exists()


def test_html_ppt_free_text_args_are_not_identifier_normalized():
    outline = """# 封面
液体闪烁计数器适用场景介绍
---
# 低能 beta 测量
- 3H
- 14C
"""

    assert _clean_tool_text_arg("tech-sharing</parameter>") == "tech-sharing"
    assert _clean_tool_text_arg(outline) == outline.strip()


def test_html_ppt_weak_outline_falls_back_to_multi_slide_outline():
    outline = _resolve_effective_outline(
        title="液体闪烁计数器适用场景介绍",
        source_text="做一份 PPT 介绍液体闪烁计数器适用的场景",
        template="tech-sharing",
        slides_outline="18.6",
    )

    slides = _parse_slides_outline(outline)

    assert len(outline) > 80
    assert len(slides) >= 3


def test_skill_manifest_tools_are_reachable_through_profile_policy():
    registry = ToolRegistry()
    loader = SkillManifestLoader(tool_registry=registry)
    profiles = {
        "nuclear-electronics": ("ne_default", "nuclear-electronics"),
        "csh-hydrogeology": ("csh_default", "csh-hydrogeology"),
        "groundwater-project-analysis": (
            "groundwater_default",
            "groundwater-project-analysis",
        ),
    }

    for manifest in loader.list_manifests():
        if not manifest.tools:
            continue
        policy_name, domain = profiles.get(manifest.domain, ("ne_default", "nuclear-electronics"))
        workflow = manifest.preferred_workflow or "full"
        selected_skills = {manifest.name}
        resolved = registry.resolve_policy_tools(
            policy_name=policy_name,
            domain=domain,
            workflow=workflow,
            selected_skills=selected_skills,
        )
        resolved_names = {spec.name for spec in resolved}
        assert set(manifest.tools) <= resolved_names, manifest.name


def test_skill_manifest_loader_reads_existing_skill_front_matter():
    manifest = SkillManifestLoader().load("nuclear-electronics")

    assert manifest.name == "nuclear-electronics"
    assert manifest.domain == "nuclear-electronics"
    assert manifest.tools == ["multi_kb_search", "web_search"]
    assert manifest.allowed_workflows == list(SUPPORTED_WORKFLOWS)


def test_skill_manifest_loader_reads_workflow_policy():
    manifest = SkillManifestLoader().load("deep-research")

    assert manifest.preferred_workflow == "plan_solve"
    assert manifest.allowed_workflows == ["deep_research", "plan_solve"]
    assert manifest.kb_required is True


def test_skill_manifest_loader_validates_all_existing_skill_tools():
    manifests = SkillManifestLoader().list_manifests()

    names = {manifest.name for manifest in manifests}
    assert "nuclear-electronics" in names
    assert "general-table-analysis" in names
    assert len(names) == len(manifests)
    csh = next(manifest for manifest in manifests if manifest.name == "csh-hydrogeology")
    assert csh.category == "custom"
    assert csh.tools == ["multi_kb_search", "web_search"]
