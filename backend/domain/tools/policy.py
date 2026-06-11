"""Tool policy contract."""

from __future__ import annotations

from dataclasses import dataclass, field


@dataclass(frozen=True)
class ToolPolicy:
    name: str
    include: set[str] = field(default_factory=set)
    exclude: set[str] = field(default_factory=set)
    include_when_skill_selected: dict[str, set[str]] = field(default_factory=dict)

    def allowed_tool_names(self, *, selected_skills: set[str]) -> set[str]:
        names = set(self.include)
        for skill_name, tool_names in self.include_when_skill_selected.items():
            if skill_name in selected_skills:
                names.update(tool_names)
        names.difference_update(self.exclude)
        return names

