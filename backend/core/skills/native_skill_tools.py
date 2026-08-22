from __future__ import annotations

import asyncio
import json
import logging
import re
from collections.abc import Callable
from typing import Any

from langchain_core.tools import tool

from backend.core.skills.scoped_skills_provider import ScopedSkillsProvider
from backend.core.skills.skill_runtime_state import mark_skill_loaded

logger = logging.getLogger(__name__)

_SKILL_NAME_RE = re.compile(r"[A-Za-z0-9][A-Za-z0-9_.-]*")


def normalize_skill_tool_name(raw: Any) -> str:
    """Normalize model-emitted skill names before authorization/provider lookup."""
    text = str(raw or "").strip().strip("\"'`")
    if not text:
        return ""
    match = _SKILL_NAME_RE.search(text)
    return match.group(0) if match else text


def create_native_skill_tools(
    skills_provider: ScopedSkillsProvider,
    selected_skills: set[str] | None = None,
) -> list[Callable[..., Any]]:
    """创建非沙箱模式下的 Skill 文件读取工具集。"""

    @tool("read_local_skill_file")
    async def read_local_skill_file(skill_name: str) -> str:
        """读取指定 Skill 的 SKILL.md 内容。"""
        normalized_skill_name = normalize_skill_tool_name(skill_name)
        logger.info(
            "[NativeSkillTools] read_local_skill_file called: raw_skill=%s normalized_skill=%s",
            skill_name,
            normalized_skill_name,
        )
        if selected_skills is not None and normalized_skill_name not in selected_skills:
            raise ValueError(f"Skill '{normalized_skill_name}' is not allowed in this session")
        content = await asyncio.to_thread(
            skills_provider.read_skill_content,
            normalized_skill_name,
        )
        mark_skill_loaded(normalized_skill_name)
        logger.info("[NativeSkillTools] skill loaded into request context: %s", normalized_skill_name)
        return content

    @tool("read_skill_reference")
    async def read_skill_reference(skill_name: str, reference_path: str) -> str:
        """读取 Skill 目录下的参考文件。"""
        normalized_skill_name = normalize_skill_tool_name(skill_name)
        logger.info(
            "[NativeSkillTools] read_skill_reference called: raw_skill=%s normalized_skill=%s path=%s",
            skill_name,
            normalized_skill_name,
            reference_path,
        )
        if selected_skills is not None and normalized_skill_name not in selected_skills:
            raise ValueError(f"Skill '{normalized_skill_name}' is not allowed in this session")
        return await asyncio.to_thread(
            skills_provider.read_skill_reference,
            normalized_skill_name,
            reference_path,
        )

    @tool("list_available_skills")
    async def list_available_skills() -> str:
        """列出当前可用 Skills。"""
        logger.info("[NativeSkillTools] list_available_skills called")
        skills = await asyncio.to_thread(
            skills_provider.get_available_skills_for_user,
            selected_skills=selected_skills,
        )
        skill_infos = [
            {
                "name": skill.name,
                "description": skill.description,
                "category": skill.category,
            }
            for skill in skills
        ]
        return json.dumps(skill_infos, ensure_ascii=False, indent=2)

    return [read_local_skill_file, read_skill_reference, list_available_skills]
