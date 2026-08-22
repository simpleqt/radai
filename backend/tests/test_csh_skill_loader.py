from __future__ import annotations

from pathlib import Path

from backend.core.skills.loader import load_skills


def test_csh_hydrogeology_skill_is_loadable_from_backend_skills() -> None:
    skills = {
        skill.name: skill
        for skill in load_skills(
            skills_path=Path("backend/skills"),
            enabled_only=True,
        )
    }

    skill = skills["csh-hydrogeology"]
    assert skill.category == "custom"
    assert skill.skill_file.as_posix().endswith(
        "backend/skills/custom/csh-hydrogeology/SKILL.md"
    )
    assert "水文地质" in skill.description or "地下水" in skill.description
    content = skill.skill_file.read_text(encoding="utf-8")
    assert "来源表格与清单完整性" in content
    assert "跨页续表" in content
    assert "只用于辅助检索" in content
    assert "不猜测并列或从属关系" in content
