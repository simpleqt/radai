"""System prompt template for the lead agent.

Provides a self-contained prompt assembly using local core/runtime modules.
"""

from __future__ import annotations

import logging
from datetime import datetime

logger = logging.getLogger(__name__)

# ---------------------------------------------------------------------------
# Subagent section builder
# ---------------------------------------------------------------------------

def _build_subagent_section(max_concurrent: int) -> str:
    n = max_concurrent
    return f"""<subagent_system>
**SUBAGENT MODE ACTIVE — DECOMPOSE, DELEGATE, SYNTHESIZE**

You are running with subagent capabilities enabled. Your role is to be a **task orchestrator**:
1. **DECOMPOSE**: Break complex tasks into parallel sub-tasks
2. **DELEGATE**: Launch multiple subagents simultaneously using parallel `task` calls
3. **SYNTHESIZE**: Collect and integrate results into a coherent answer

**HARD CONCURRENCY LIMIT: MAXIMUM {n} ACTIVE `task` CALLS.**
- The runtime preserves delegated calls and queues work above {n}; do not rely on this queue for unrelated work.
- Prefer batches of ≤{n} independent sub-tasks so progress remains visible and prompt context stays focused.

**Available Subagents:**
- **general-purpose**: For ANY non-trivial task — web research, code exploration, file operations, analysis, etc.
- **deep-research**: For deep research sub-tasks with extended retrieval budget.

**When to use subagents (parallel `task` calls):**
- Complex research requiring multiple information sources
- Multi-aspect analysis with independent dimensions
- Comprehensive investigations needing thorough coverage

**When NOT to use subagents:**
- Task cannot decompose into 2+ parallel sub-tasks
- Ultra-simple actions (read one file, quick edits)
- Sequential dependencies where each step depends on prior results
</subagent_system>"""


# ---------------------------------------------------------------------------
# Template
# ---------------------------------------------------------------------------

_SYSTEM_PROMPT_TEMPLATE = """<role>
You are {agent_name}, a professional domain agent.
</role>

<thinking_style>
- Think concisely and strategically BEFORE taking action
- Break down the task: what is clear? what is ambiguous? what is missing?
- If anything is unclear, state your assumptions briefly and continue when safe
{subagent_thinking}- Never write your full final answer in thinking — only outline
- After thinking, you MUST provide your actual response to the user
</thinking_style>

{skills_section}

{subagent_section}

<response_style>
- Clear and concise; avoid over-formatting unless requested
- Natural tone using paragraphs and prose
- Action-oriented: focus on delivering results, not explaining processes
- Language consistency: use the same language as the user
</response_style>

<critical_reminders>
- Do not call clarification tools; if required details are missing, ask in the visible answer or proceed with explicit assumptions when safe
{subagent_reminder}- Be direct and helpful; avoid unnecessary meta-commentary
- Use Markdown images and Mermaid diagrams when helpful
- Prefer parallel tool calling for better performance
- Always respond: thinking is internal; you MUST provide a visible response
</critical_reminders>
"""


def apply_prompt_template(
    subagent_enabled: bool = False,
    max_concurrent_subagents: int = 5,
    *,
    agent_name: str | None = None,
    available_skills: set[str] | None = None,
) -> str:
    """Build the system prompt using the template.

    This is the local backend implementation of the lead-agent prompt template.
    """
    effective_name = agent_name or "核电子学智能体"
    n = max_concurrent_subagents

    subagent_section = _build_subagent_section(n) if subagent_enabled else ""
    subagent_reminder = (
        f"- **Orchestrator Mode**: Decompose complex tasks into parallel sub-tasks. "
        f"HARD LIMIT: max {n} `task` calls per response.\n"
        if subagent_enabled
        else ""
    )
    subagent_thinking = (
        f"- **DECOMPOSITION CHECK**: Can this break into 2+ parallel sub-tasks? "
        f"If count > {n}, plan batches of ≤{n} and only launch first batch now.\n"
        if subagent_enabled
        else ""
    )

    skills_section = _build_skills_section(available_skills)

    prompt = _SYSTEM_PROMPT_TEMPLATE.format(
        agent_name=effective_name,
        skills_section=skills_section,
        subagent_section=subagent_section,
        subagent_reminder=subagent_reminder,
        subagent_thinking=subagent_thinking,
    )

    return prompt + f"\n<current_date>{datetime.now().strftime('%Y-%m-%d, %A')}</current_date>"


def _build_skills_section(available_skills: set[str] | None = None) -> str:
    """Build the <skill_system> prompt section.

    Tries to load skills from the local skills provider if available,
    otherwise returns an empty section.
    """
    try:
        from backend.core.skills.scoped_skills_provider import ScopedSkillsProvider
        provider = ScopedSkillsProvider()
        skills = provider.get_available_skills_for_user(selected_skills=available_skills)
        if not skills:
            return ""
        return provider.build_skills_prompt_section(skills) or ""
    except Exception:
        logger.debug("Skills section generation skipped (provider unavailable)")
        return ""
