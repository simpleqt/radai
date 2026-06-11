"""Skill file parser for backend-managed skills."""

import logging
import re
from pathlib import Path

import yaml

from backend.core.skills.types import Skill

logger = logging.getLogger(__name__)


def _normalize_scalar(value: str) -> str:
    text = value.strip()
    if len(text) >= 2 and text[0] == text[-1] and text[0] in {'"', "'"}:
        text = text[1:-1].strip()
    return text


def parse_skill_file(
    skill_file: Path, category: str, relative_path: Path | None = None
) -> Skill | None:
    if not skill_file.exists() or skill_file.name != "SKILL.md":
        return None
    try:
        content = skill_file.read_text(encoding="utf-8")
        front_matter_match = re.match(r"^---\s*\n(.*?)\n---\s*\n", content, re.DOTALL)
        if not front_matter_match:
            return None
        front_matter = front_matter_match.group(1)
        try:
            metadata = yaml.safe_load(front_matter)
        except yaml.YAMLError as exc:
            logger.error("Error parsing YAML in %s: %s", skill_file, exc)
            return None
        if not isinstance(metadata, dict):
            return None
        name = _normalize_scalar(str(metadata.get("name", "")))
        description = _normalize_scalar(str(metadata.get("description", "")))
        if not name or not description:
            return None
        license_text = (
            _normalize_scalar(str(metadata.get("license", "")))
            if metadata.get("license") else None
        )
        owner_id = (
            _normalize_scalar(str(metadata.get("owner_id", "")))
            if metadata.get("owner_id")
            else None
        )
        tool_names: list[str] = []
        seen: set[str] = set()
        raw_tools = metadata.get("tools")
        if isinstance(raw_tools, str):
            n = _normalize_scalar(raw_tools)
            if n:
                tool_names.append(n)
                seen.add(n)
        elif isinstance(raw_tools, list):
            for item in raw_tools:
                tn = ""
                if isinstance(item, str):
                    tn = _normalize_scalar(item)
                elif isinstance(item, dict):
                    tn = _normalize_scalar(str(item.get("name", "")))
                if tn and tn not in seen:
                    tool_names.append(tn)
                    seen.add(tn)
        return Skill(
            name=name, description=description, license=license_text,
            skill_dir=skill_file.parent, skill_file=skill_file,
            relative_path=relative_path or Path(skill_file.parent.name),
            category=category, enabled=True, tool_names=tool_names, owner_id=owner_id,
        )
    except Exception as e:
        logger.error("Error parsing skill file %s: %s", skill_file, e)
        return None
