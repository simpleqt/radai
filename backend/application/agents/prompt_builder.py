"""Platform prompt builder for profile-driven agents."""

from __future__ import annotations

import logging
import re
from dataclasses import dataclass
from typing import Any

from backend.application.agents.prompt_registry import (
    PromptProvider,
    PromptTemplateRegistry,
)
from backend.config.log_privacy import text_fingerprint
from backend.core.agents.context.project_instructions import load_project_instructions
from backend.core.skills.scoped_skills_provider import ScopedSkillsProvider

logger = logging.getLogger(__name__)


@dataclass(frozen=True)
class PromptBuildContext:
    domain: str
    selected_skills: set[str] | None
    subagent_enabled: bool
    use_search: bool
    prompt_profile: str | None = None
    task_type: str | None = None
    lightweight_mode: bool = False
    lightweight_has_kb: bool = False
    guardrail_instructions: str = ""
    time_context: str = ""
    request_metadata: dict[str, Any] | None = None


class PromptBuilder:
    """Build system prompts from profile/domain context.

    Domain templates live in application prompt template modules; agent classes
    no longer own prompt assembly or skill-enforcement patching.
    """

    DEEP_RESEARCH_TASK_TYPE = "deep_research"

    def __init__(
        self,
        *,
        skills_provider: ScopedSkillsProvider | None = None,
        prompt_registry: PromptTemplateRegistry | None = None,
        prompt_provider: PromptProvider | None = None,
    ):
        self._skills_provider = skills_provider or ScopedSkillsProvider()
        self._prompt_registry = (
            prompt_registry
            if prompt_registry is not None
            else (None if prompt_provider is not None else PromptTemplateRegistry())
        )
        self._prompt_provider = prompt_provider

    def build(self, context: PromptBuildContext) -> str:
        base_prompt = self._build_base_prompt(context)
        runtime_context = self._build_runtime_context(context)
        if context.lightweight_mode:
            logger.info("[PromptBuilder] 轻量问答模式：跳过 Skills section 注入")
            return base_prompt + runtime_context

        skills = self._get_selected_skill_defs(context.selected_skills)
        logger.info(
            "[PromptBuilder] 可用 Skills(过滤后): domain=%s skills=%s",
            context.domain,
            [skill.name for skill in skills],
        )
        prompt = self._patch_skills_section(
            base_prompt,
            self._skills_provider.build_skills_prompt_section(skills),
            selected_skills=context.selected_skills,
        )
        prompt += self._build_skill_enforcement(context)
        prompt += runtime_context
        logger.info(
            "[PromptBuilder] System Prompt 构建完成: domain=%s length=%d prompt_meta=%s",
            context.domain,
            len(prompt),
            text_fingerprint(prompt),
        )
        return prompt

    @staticmethod
    def build_runtime_context(context: PromptBuildContext) -> str:
        chunks = [context.time_context, context.guardrail_instructions]
        metadata = context.request_metadata or {}
        project_cwd = metadata.get("cwd")
        if project_cwd:
            try:
                project_instructions = load_project_instructions(project_cwd)
            except Exception:
                logger.exception("[PromptBuilder] 项目 AGENTS.md 加载失败")
                project_instructions = None
            if project_instructions is not None and project_instructions.content:
                chunks.append("\n" + project_instructions.content)
        raw_file_ids = metadata.get("file_ids")
        if isinstance(raw_file_ids, (list, tuple, set)):
            file_ids = [
                str(item).strip() for item in raw_file_ids if str(item or "").strip()
            ]
        else:
            value = str(raw_file_ids or "").strip()
            file_ids = [value] if value else []
        if file_ids:
            lines = [
                "\n<uploaded_table_files>",
                "The current request includes uploaded file_id values. For Excel/CSV/table analysis tools, pass file_id exactly; do not invent dataset_code or file_path.",
            ]
            lines.extend(f"- file_id: {file_id}" for file_id in file_ids[:10])
            lines.append("</uploaded_table_files>\n")
            chunks.append("\n".join(lines))
        uploaded_manifest = metadata.get("_uploaded_file_manifest")
        if isinstance(uploaded_manifest, dict) and uploaded_manifest:
            manifest_lines = [
                "\n<uploaded_file_manifest>",
                "当前请求携带以下上传文件（file_id 与文件名对照）：",
            ]
            for fid, info in list(uploaded_manifest.items())[:10]:
                name = str(info.get("display_name") or fid)
                mime = str(info.get("mime_type") or "")
                size = info.get("size_bytes")
                manifest_lines.append(
                    f"- file_id: {fid}  display_name: {name}  mime_type: {mime}"
                    + (f"  size_bytes: {size}" if size is not None else "")
                )
            manifest_lines.append(
                "需要把文件绑定为课程评价材料时，用 attach_material 传入对应 file_id；"
                "材料类型按文件名自动识别（含「成绩单」=grade_sheet、含「大纲」=syllabus、"
                "含「试卷」且为 PDF=paper）。"
            )
            manifest_lines.append("</uploaded_file_manifest>\n")
            chunks.append("\n".join(manifest_lines))
        image_analysis = metadata.get("_uploaded_image_analysis_context")
        if isinstance(image_analysis, str) and image_analysis.strip():
            chunks.append(image_analysis)
        return "".join(chunks)

    @staticmethod
    def _build_runtime_context(context: PromptBuildContext) -> str:
        """Backward-compatible private alias used by older extensions/tests."""

        return PromptBuilder.build_runtime_context(context)

    def _build_base_prompt(self, context: PromptBuildContext) -> str:
        build_system_prompt = self._resolve_prompt_builder(
            context.prompt_profile,
            fallback_key=context.domain,
        )
        return build_system_prompt(
            subagent_enabled=context.subagent_enabled,
            use_search=context.use_search,
            available_skills=context.selected_skills,
            task_type=context.task_type,
            lightweight_mode=context.lightweight_mode,
            lightweight_has_kb=context.lightweight_has_kb,
        )

    def _resolve_prompt_builder(
        self, prompt_profile: str | None, *, fallback_key: str | None = None
    ):
        if self._prompt_provider is not None:
            return self._prompt_provider
        if self._prompt_registry is None:
            raise ValueError("Prompt registry is unavailable")
        # A declared prompt profile is authoritative. Falling back to the
        # domain after a typo would silently run a different agent persona.
        if str(prompt_profile or "").strip():
            return self._prompt_registry.resolve_provider(prompt_profile)
        return self._prompt_registry.resolve_provider(None, fallback_key=fallback_key)

    def _get_selected_skill_defs(self, selected_skills: set[str] | None) -> list[Any]:
        if not selected_skills:
            return []
        return self._skills_provider.get_available_skills_for_user(
            selected_skills=selected_skills,
        )

    def _patch_skills_section(
        self,
        base_prompt: str,
        skills_section: str,
        *,
        selected_skills: set[str] | None,
    ) -> str:
        if not skills_section:
            logger.warning(
                "[PromptBuilder] Skills section 为空，selected_skills=%s。请检查 skill 是否启用。",
                sorted(selected_skills) if selected_skills is not None else None,
            )
            return base_prompt
        if "<skill_system>" in base_prompt:
            return re.sub(
                r"<skill_system>.*?</skill_system>",
                skills_section,
                base_prompt,
                flags=re.DOTALL,
            )
        return base_prompt + "\n" + skills_section

    def _build_skill_enforcement(self, context: PromptBuildContext) -> str:
        selected = context.selected_skills or set()
        chunks: list[str] = []

        domain_skill = str(context.domain or "").strip()
        if domain_skill and domain_skill in selected:
            chunks.append(
                f"""

<skill_enforcement>
- You MUST call read_local_skill_file("{domain_skill}") before any multi_kb_search call and before drafting the final answer for this domain.
- If the domain skill is not loaded yet, do not continue with retrieval/answer drafting.
</skill_enforcement>
"""
            )

        if "html-ppt" in selected:
            chunks.append(
                """

<skill_enforcement_ppt>
- If the user asks for PPT, slides, deck, or presentation, you MUST call read_local_skill_file("html-ppt") IMMEDIATELY.
- You MUST use `generate_html_ppt` to deliver the final presentation.
</skill_enforcement_ppt>
"""
            )

        if "academic-search" in selected:
            chunks.append(
                """

<skill_enforcement_academic>
- For paper/literature/citation/DOI/arXiv/review questions, you MUST call `academic_search`.
- Do NOT use `web_search` as a substitute for structured academic retrieval.
- You MAY call `read_local_skill_file("academic-search")` to review tool parameters when needed.
</skill_enforcement_academic>
"""
            )

        if "general-table-analysis" in selected:
            chunks.append(
                """

<skill_enforcement_table>
- If the user asks to analyze an uploaded Excel/CSV/table file, you MUST call read_local_skill_file("general-table-analysis") before using table analysis tools.
- For table questions, inspect the table schema before SQL/statistical analysis.
- Use uploaded file_id values provided by the runtime; do not invent file paths.
</skill_enforcement_table>
"""
            )

        if context.task_type == self.DEEP_RESEARCH_TASK_TYPE:
            chunks.append(
                """

<deep_research_kb_query_policy>
- In deep-research mode, first extract keywords by yourself, then call `multi_kb_search` for KB retrieval.
- Provide `high_level_keywords` (3-8 broad concepts) and `low_level_keywords` (5-15 concrete terms).
- `query_type` should be one of: `general`, `parameter`, `concept`, `equipment`, `comparison`.
- Use `top_k=10`, `chunk_top_k=6` unless evidence is clearly insufficient.
</deep_research_kb_query_policy>
"""
            )

        return "".join(chunks)
