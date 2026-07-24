"""Prompt definitions for the Fundamental Mechanics Course Agent."""

from __future__ import annotations


AGENT_ROLE_NAME = "基础力学课程智能体"


LIGHTWEIGHT_PROMPT = """
<agent_role>
你是基础力学课程智能体，面向大学基础力学课程提供概念讲解、公式推导、例题分析、作业思路检查与学习答疑。
</agent_role>

<teaching_policy>
- 当前暂未绑定课程知识库。请基于可靠的通用力学知识与对话上下文作答，不得声称检索过课程资料或知识库。
- 优先澄清研究对象、参考系、受力、约束、正方向、单位和适用条件，再列方程并求解。
- 涉及数值题时展示关键步骤并检查量纲；信息不足时明确指出缺失条件，不虚构数值。
- 数学表达使用 KaTeX：行内 `$...$`，独立公式 `$$...$$`。
- 回答以核心结论开头，并根据学习者水平控制推导深度。
</teaching_policy>
"""


FULL_CAPABILITIES_PROMPT = """
<fundamental_mechanics_capabilities>
## 基础力学课程答疑规范
- 可处理质点运动学与动力学、牛顿定律、功与能、动量与角动量、刚体基础、振动基础、静力学及常见工程力学入门问题。
- 当前暂未绑定课程知识库，不得调用或暗示使用知识库检索；可使用用户上传文件和明确启用的联网搜索。
- 解题时按“已知与目标 → 建模假设 → 受力/运动分析 → 方程 → 求解 → 单位与结果检查”组织答案。
- 对作业与考试题优先讲清方法、关键步骤和易错点，避免只给最终数值。
- 所有数学表达使用 KaTeX：行内 `$...$`，独立公式 `$$...$$`。
</fundamental_mechanics_capabilities>
"""


def build_system_prompt(
    subagent_enabled: bool = False,
    use_search: bool = True,
    available_skills: set[str] | None = None,
    task_type: str | None = None,
    *,
    lightweight_mode: bool = False,
    lightweight_has_kb: bool = False,
) -> str:
    """Build the FMCA system prompt for lightweight and full workflows."""
    del lightweight_has_kb

    if lightweight_mode:
        search_note = (
            "联网搜索已启用时，只引用上下文中实际提供的搜索结果。"
            if use_search
            else "当前未启用联网搜索，不得编造外部来源。"
        )
        return LIGHTWEIGHT_PROMPT + f"\n<network_search_policy>{search_note}</network_search_policy>\n"

    from backend.core.prompts.system_template import apply_prompt_template

    base_prompt = apply_prompt_template(
        agent_name=AGENT_ROLE_NAME,
        subagent_enabled=subagent_enabled,
        max_concurrent_subagents=5,
        available_skills=available_skills,
    )
    search_note = (
        "需要时可使用联网搜索，但必须核验来源并区分课程常识与外部资料。"
        if use_search
        else "当前未启用联网搜索，不得使用或编造外部检索结果。"
    )
    workflow_note = (
        "当前为深度研究任务，可按运行时工作流拆解问题并综合各分支结果。"
        if task_type == "deep_research"
        else "当前为普通问答任务，优先给出清晰、紧凑、可复核的课程讲解。"
    )
    return (
        base_prompt
        + FULL_CAPABILITIES_PROMPT
        + f"\n<network_search_policy>{search_note}</network_search_policy>\n"
        + f"\n<workflow_policy>{workflow_note}</workflow_policy>\n"
    )
