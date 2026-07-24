from __future__ import annotations

import logging
import os
import threading
import time
import weakref
from pathlib import Path
from typing import Any

from backend.core.skills.loader import (
    get_managed_skills_root_path,
    get_skills_root_path,
    load_skills,
)
from backend.core.skills.types import Skill

logger = logging.getLogger(__name__)


def _positive_float_env(name: str, default: float) -> float:
    try:
        return max(1.0, float(os.getenv(name, str(default)) or default))
    except (TypeError, ValueError):
        return default


_PROVIDER_INSTANCES: weakref.WeakSet[ScopedSkillsProvider] = weakref.WeakSet()
_PROVIDER_LOCK = threading.RLock()
_MAX_SKILL_CONTENT_CHARS = max(
    1_000, int(os.getenv("AGENT_MAX_SKILL_CONTENT_CHARS", "60000"))
)
_MAX_REFERENCE_CONTENT_CHARS = max(
    1_000, int(os.getenv("AGENT_MAX_SKILL_REFERENCE_CHARS", "50000"))
)
_SKILL_CACHE_TTL_SECONDS = _positive_float_env(
    "AGENT_SKILL_CACHE_TTL_SECONDS",
    30.0,
)


def invalidate_all_skill_caches() -> None:
    """Invalidate every live provider in this process after a catalog change."""
    with _PROVIDER_LOCK:
        providers = list(_PROVIDER_INSTANCES)
    for provider in providers:
        provider.invalidate_cache()


class ScopedSkillsProvider:
    """Skills 作用域封装，支持多用户场景下的 Skills 隔离与组合配置。"""

    def __init__(
        self,
        skills_base_path: Path | None = None,
        container_path: str = "/mnt/skills",
        user_id: str | None = None,
        authorized_skill_names: set[str] | None = None,
    ):
        self._skills_base_path = skills_base_path or get_skills_root_path()
        self._container_path = container_path
        self._user_id = str(user_id or "").strip() or None
        self._authorized_skill_names = (
            {str(name).strip() for name in authorized_skill_names if str(name).strip()}
            if authorized_skill_names is not None
            else None
        )
        self._skills_cache: list[Skill] | None = None
        self._skills_cache_loaded_at = 0.0
        self._cache_lock = threading.RLock()
        with _PROVIDER_LOCK:
            _PROVIDER_INSTANCES.add(self)

    def invalidate_cache(self) -> None:
        with self._cache_lock:
            self._skills_cache = None
            self._skills_cache_loaded_at = 0.0

    def _load_all_skills(self) -> list[Skill]:
        """加载所有 Skills（带缓存）。"""
        with self._cache_lock:
            cache_expired = (
                self._skills_cache is not None
                and time.monotonic() - self._skills_cache_loaded_at
                >= _SKILL_CACHE_TTL_SECONDS
            )
            if self._skills_cache is None or cache_expired:
                by_name = {
                    skill.name: skill
                    for skill in load_skills(skills_path=self._skills_base_path)
                }
                managed_root = get_managed_skills_root_path()
                if managed_root != self._skills_base_path and managed_root.exists():
                    for skill in load_skills(skills_path=managed_root):
                        by_name[skill.name] = skill
                self._skills_cache = sorted(by_name.values(), key=lambda item: item.name)
                self._skills_cache_loaded_at = time.monotonic()
                logger.info(
                    "[ScopedSkillsProvider] 已加载 Skills 总数=%d, 根目录=%s",
                    len(self._skills_cache),
                    self._skills_base_path,
                )
            return list(self._skills_cache)

    def get_available_skills_for_user(
        self,
        user_id: str | None = None,
        selected_skills: set[str] | None = None,
    ) -> list[Skill]:
        """获取用户可用 Skills 列表。"""
        effective_user_id = str(user_id or self._user_id or "").strip() or None
        all_skills = self._load_all_skills()

        if self._authorized_skill_names is not None:
            enabled_skills = [
                skill
                for skill in all_skills
                if skill.enabled and skill.name in self._authorized_skill_names
            ]
        else:
            enabled_skills = [
                skill
                for skill in all_skills
                if skill.enabled
                and (
                    skill.category == "public"
                    or (
                        skill.category == "custom"
                        and effective_user_id is not None
                        and skill.owner_id == effective_user_id
                    )
                )
            ]

        if selected_skills is not None:
            enabled_skills = [skill for skill in enabled_skills if skill.name in selected_skills]

        logger.info(
            "[ScopedSkillsProvider] get_available_skills_for_user: user_id=%s selected=%s matched=%s",
            effective_user_id,
            sorted(selected_skills) if selected_skills is not None else None,
            [skill.name for skill in enabled_skills],
        )

        return enabled_skills

    def build_skills_prompt_section(self, skills: list[Skill]) -> str:
        """构建 Skills prompt section，保持与当前运行时一致的结构。"""
        if not skills:
            logger.warning("[ScopedSkillsProvider] build_skills_prompt_section: skills 为空")
            return ""

        logger.info(
            "[ScopedSkillsProvider] build_skills_prompt_section: skills=%s",
            [skill.name for skill in skills],
        )

        skill_items = "\n".join(
            f"    <skill>\n"
            f"        <name>{skill.name}</name>\n"
            f"        <description>{skill.description}</description>\n"
            f"        <location>{skill.get_container_file_path(self._container_path)}</location>\n"
            f"    </skill>"
            for skill in skills
        )
        skills_list = f"<available_skills>\n{skill_items}\n</available_skills>"

        return f"""<skill_system>
You have access to skills that provide optimized workflows for specific tasks. Each skill contains best practices, frameworks, and references to additional resources.

**Progressive Loading Pattern:**
1. 💥 CRITICAL: DO NOT attempt to call the skill name as a tool or function! Skills are Markdown files, not executable tools.
2. When a user query matches a skill's use case, you must immediately call the `read_local_skill_file` tool with the skill name.
3. Read and understand the skill's workflow and instructions.
4. The skill file contains references to external resources under the same folder.
5. Load referenced resources only when needed during execution.
6. Follow the skill's instructions precisely.

**Skills are located at:** {self._container_path}

{skills_list}

</skill_system>"""

    @staticmethod
    def validate_skill_selection(skills: list[Skill]) -> None:
        """Validate dependency/conflict declarations before agent assembly."""
        selected = {skill.name for skill in skills}
        missing: dict[str, list[str]] = {}
        conflicts: dict[str, list[str]] = {}
        for skill in skills:
            unresolved = sorted(set(skill.dependencies) - selected)
            if unresolved:
                missing[skill.name] = unresolved
            active_conflicts = sorted(set(skill.conflicts) & selected)
            if active_conflicts:
                conflicts[skill.name] = active_conflicts
        if missing:
            raise ValueError(f"Skill dependencies are not selected: {missing}")
        if conflicts:
            raise ValueError(f"Conflicting Skills selected: {conflicts}")

    def read_skill_content(self, skill_name: str) -> str:
        """读取指定 Skill 的 SKILL.md 内容。"""
        skills = self.get_available_skills_for_user(selected_skills={skill_name})
        if not skills:
            raise ValueError(f"Skill '{skill_name}' not found or not enabled")

        skill = skills[0]
        logger.info("[ScopedSkillsProvider] Read skill content: %s", skill_name)
        return self._read_bounded(skill.skill_file, _MAX_SKILL_CONTENT_CHARS)

    def read_skill_reference(self, skill_name: str, reference_path: str) -> str:
        """读取 Skill 目录下的参考文件。"""
        skills = self.get_available_skills_for_user(selected_skills={skill_name})
        if not skills:
            raise ValueError(f"Skill '{skill_name}' not found or not enabled")

        skill = skills[0]
        raw_path = (reference_path or "").strip()
        if not raw_path:
            raw_path = "references"

        # Allow common shorthand. Example: "references" -> "references.md" if present.
        candidates = [skill.skill_dir / raw_path]
        if not Path(raw_path).suffix:
            candidates.append(skill.skill_dir / f"{raw_path}.md")

        ref_file: Path | None = None
        skill_dir_resolved = skill.skill_dir.resolve()
        for candidate in candidates:
            try:
                resolved = candidate.resolve()
                resolved.relative_to(skill_dir_resolved)
            except ValueError as exc:
                raise PermissionError(f"Path traversal detected: {reference_path}") from exc

            if resolved.exists() and resolved.is_file():
                ref_file = resolved
                break

        if ref_file is None:
            index_file = skill.skill_dir / "references" / "index.md"
            if index_file.is_file():
                logger.warning(
                    "[ScopedSkillsProvider] Skill reference not found, returning index: skill=%s path=%s",
                    skill_name,
                    reference_path,
                )
                index_content = self._read_bounded(
                    index_file,
                    _MAX_REFERENCE_CONTENT_CHARS,
                )
                return (
                    f"Reference file not found: {reference_path}.\n"
                    "Load references one at a time. Catalog:\n\n"
                    f"{index_content}"
                )

            available_refs = sorted(
                str(p.relative_to(skill.skill_dir))
                for p in skill.skill_dir.rglob("*.md")
                if p.is_file() and p.name.upper() != "SKILL.md"
            )
            if not available_refs:
                logger.warning(
                    "[ScopedSkillsProvider] Skill reference not found and no extra refs: skill=%s path=%s",
                    skill_name,
                    reference_path,
                )
                return (
                    f"Reference file not found: {reference_path}. "
                    "This skill currently has no additional reference files; please continue using SKILL.md only."
                )

            preview = available_refs[:8]
            suffix = f" (+{len(available_refs) - len(preview)} more)" if len(available_refs) > len(preview) else ""
            logger.warning(
                "[ScopedSkillsProvider] Skill reference not found: skill=%s path=%s available=%s",
                skill_name,
                reference_path,
                available_refs,
            )
            return (
                f"Reference file not found: {reference_path}. "
                f"Load one reference at a time. Examples: {', '.join(preview)}{suffix}"
            )

        logger.info(
            "[ScopedSkillsProvider] Read skill reference: skill=%s path=%s",
            skill_name,
            reference_path,
        )
        return self._read_bounded(ref_file, _MAX_REFERENCE_CONTENT_CHARS)

    @staticmethod
    def _read_bounded(path: Path, max_chars: int) -> str:
        size = path.stat().st_size
        if size > max_chars * 4:
            raise ValueError(
                f"Skill resource exceeds the {max_chars} character safety limit: {path.name}"
            )
        content = path.read_text(encoding="utf-8")
        if len(content) > max_chars:
            raise ValueError(
                f"Skill resource exceeds the {max_chars} character safety limit: {path.name}"
            )
        return content

    def get_skill_info(self, skill_name: str) -> dict[str, Any] | None:
        """获取 Skill 元信息。"""
        skills = self.get_available_skills_for_user(selected_skills={skill_name})
        if not skills:
            return None

        skill = skills[0]
        return {
            "name": skill.name,
            "description": skill.description,
            "category": skill.category,
            "enabled": skill.enabled,
            "skill_dir": str(skill.skill_dir),
            "skill_file": str(skill.skill_file),
        }
