"""Generated agent prompt provider."""

from __future__ import annotations

AGENT_ROLE_NAME = '遥感滑坡分析智能体'
AGENT_DOMAIN = 'remote-sensing-landslide'


def build_system_prompt(
    subagent_enabled: bool = False,
    use_search: bool = True,
    available_skills: set[str] | None = None,
    task_type: str | None = None,
    *,
    lightweight_mode: bool = False,
    lightweight_has_kb: bool = False,
) -> str:
    """Build the generated domain prompt through the common base template."""
    from backend.core.prompts.system_template import apply_prompt_template

    base = apply_prompt_template(
        agent_name=AGENT_ROLE_NAME,
        subagent_enabled=subagent_enabled,
        max_concurrent_subagents=5,
        available_skills=available_skills,
    )
    retrieval = (
        "Use the authorized knowledge bases first and preserve source evidence."
        if lightweight_has_kb or not lightweight_mode
        else "No knowledge base evidence is currently bound; state uncertainty clearly."
    )
    network = (
        "Public web search may be used only when current external facts are required."
        if use_search
        else "Public web search is disabled; do not invent external sources."
    )
    return (
        base
        + f"\n<agent_domain>{AGENT_DOMAIN}</agent_domain>\n"
        + f"<retrieval_policy>{retrieval} {network}</retrieval_policy>\n"
    )
