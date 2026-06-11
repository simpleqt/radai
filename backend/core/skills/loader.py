"""Skill loader - scans public/custom directories, parses SKILL.md."""

import logging
import os
from pathlib import Path

from backend.core.skills.parser import parse_skill_file
from backend.core.skills.types import Skill

logger = logging.getLogger(__name__)


def get_skills_root_path() -> Path:
    current = Path(__file__).resolve()
    for parent in current.parents:
        backend_skills = parent / "backend" / "skills"
        if backend_skills.is_dir():
            return backend_skills
    return current.parents[3] / "backend" / "skills"


def load_skills(
    skills_path: Path | None = None, enabled_only: bool = False
) -> list[Skill]:
    if skills_path is None:
        skills_path = get_skills_root_path()
    if not skills_path.exists():
        return []

    skills: list[Skill] = []
    for category in ["public", "custom"]:
        category_path = skills_path / category
        if not category_path.exists() or not category_path.is_dir():
            continue
        for current_root, dir_names, file_names in os.walk(
            category_path, followlinks=True
        ):
            dir_names[:] = sorted(
                name for name in dir_names if not name.startswith(".")
            )
            if "SKILL.md" not in file_names:
                continue
            skill_file = Path(current_root) / "SKILL.md"
            relative_path = skill_file.parent.relative_to(category_path)
            skill = parse_skill_file(
                skill_file, category=category, relative_path=relative_path
            )
            if skill:
                skills.append(skill)

    try:
        from backend.core.config.extensions_config import ExtensionsConfig
        ext_cfg = ExtensionsConfig.from_file()
        for skill in skills:
            skill.enabled = ext_cfg.is_skill_enabled(skill.name, skill.category)
    except ImportError:
        logger.debug("ExtensionsConfig unavailable, skills default to enabled")
    except Exception as e:
        logger.warning("Failed to load extensions config: %s", e)

    if enabled_only:
        skills = [s for s in skills if s.enabled]
    skills.sort(key=lambda s: s.name)
    return skills
