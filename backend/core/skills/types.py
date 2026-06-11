"""Skill data model for local skill discovery and execution."""

from dataclasses import dataclass, field
from pathlib import Path


@dataclass
class Skill:
    """Represents a skill with its metadata and file path."""

    name: str
    description: str
    license: str | None
    skill_dir: Path
    skill_file: Path
    relative_path: Path
    category: str  # 'public' or 'custom'
    enabled: bool = False
    tool_names: list[str] = field(default_factory=list)
    owner_id: str | None = None

    @property
    def skill_path(self) -> str:
        path = self.relative_path.as_posix()
        return "" if path == "." else path

    def get_container_path(self, container_base_path: str = "/mnt/skills") -> str:
        category_base = f"{container_base_path}/{self.category}"
        skill_path = self.skill_path
        if skill_path:
            return f"{category_base}/{skill_path}"
        return category_base

    def get_container_file_path(self, container_base_path: str = "/mnt/skills") -> str:
        return f"{self.get_container_path(container_base_path)}/SKILL.md"

    def __repr__(self) -> str:
        return f"Skill(name={self.name!r}, description={self.description!r}, category={self.category!r})"
