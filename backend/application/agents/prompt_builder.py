"""Platform prompt builder for profile-driven agents."""

from __future__ import annotations

import logging
import re
from dataclasses import dataclass

from backend.application.agents.prompt_registry import PromptTemplateRegistry
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
    ):
        self._skills_provider = skills_provider or ScopedSkillsProvider()
        self._prompt_registry = prompt_registry or PromptTemplateRegistry()

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
            "[PromptBuilder] System Prompt 构建完成: domain=%s length=%d head=%s tail=%s",
            context.domain,
            len(prompt),
            prompt[:100].replace("\n", " "),
            prompt[-200:].replace("\n", " "),
        )
        return prompt

    @staticmethod
    def _build_runtime_context(context: PromptBuildContext) -> str:
        return f"{context.time_context}{context.guardrail_instructions}"

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

    def _resolve_prompt_builder(self, prompt_profile: str | None, *, fallback_key: str | None = None):
        return self._prompt_registry.resolve_provider(prompt_profile, fallback_key=fallback_key)

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

        if "nuclear-electronics" in selected:
            chunks.append(
                """

<skill_enforcement>
- For nuclear-electronics related questions, you MUST call read_local_skill_file("nuclear-electronics") before any multi_kb_search call and before drafting the final answer.
- If not loaded yet, do not continue with retrieval/answer drafting.
</skill_enforcement>
"""
            )

        if "csh-hydrogeology" in selected:
            chunks.append(
                """

<skill_enforcement>
- For contaminated-site hydrogeology, groundwater, soil pollution, contaminant transport, risk assessment, or remediation questions, you MUST call read_local_skill_file("csh-hydrogeology") before any multi_kb_search call and before drafting the final answer.
- If not loaded yet, do not continue with retrieval/answer drafting.
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
