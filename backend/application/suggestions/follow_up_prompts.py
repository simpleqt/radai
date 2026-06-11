"""Follow-up suggestion prompt templates by agent mode / skill."""

BASE_FOLLOW_UP_PROMPT = """根据给出的用户问题与助手回答，生成 {count} 条用户可能继续追问的具体、有针对性的中文问句。

【生成要求】
1. 必须针对当前对话的上下文（包括回答中的专业词汇和段落），生成具体、能深度探索的追问。
2. 问句长度为 8～40 字，采用用户第一人称口吻（例如："这个方法的具体步骤是什么？"、"在...情况下该如何处理？"）。
3. 问句之间必须互不重复、角度不同、且具有实际可回答性，避免空泛无意义的句子（严禁生成如「还有其他问题吗？」、「请问还有什么我可以帮您的？」等）。
4. 绝对禁止直接照抄任何占位符或格式示例内容。
5. 如果涉及科学公式、化学元素、同位素符号（如氦-3应表示为$^3\\text{{He}}$）、数学公式或物理公式，必须使用标准 LaTeX 格式（用单美元符号 $ 包裹，例如 $^3\\text{{He}}$、$K_{{sp}}$），以便前端能够正确渲染上标/下标。

【输出格式】
只输出标准的 JSON，格式如下（请将数组中的占位符替换为您实际生成的具体问句）：
{{"suggestions": ["具体追问问句A", "具体追问问句B", "具体追问问句C"]}}
禁止输出思考过程、解释、Markdown、代码块或 <think> 标签；第一个字符必须是 {{，最后一个字符必须是 }}。

用户问题：
{user_question}

助手回答：
{assistant_answer}
{tool_summary_block}"""

CONTEXT_FOLLOW_UP_PROMPT = """根据给出的用户问题与当前上下文，生成 {count} 条用户可能继续追问的具体、有针对性的中文问句。

【生成要求】
1. 必须围绕当前用户问题和上下文信息生成，不依赖助手最终回答。
2. 问句长度为 8～40 字，采用用户第一人称口吻，问题要具体、可继续回答。
3. 问句之间必须互不重复、角度不同，避免空泛问法。
4. 绝对禁止直接照抄任何占位符或格式示例内容。
5. 如果涉及科学公式、化学元素、同位素符号、数学公式或物理公式，必须使用标准 LaTeX 格式（用单美元符号 $ 包裹）。

【输出格式】
只输出标准的 JSON，格式如下（请将数组中的占位符替换为您实际生成的具体问句）：
{{"suggestions": ["具体追问问句A", "具体追问问句B", "具体追问问句C"]}}
禁止输出思考过程、解释、Markdown、代码块或 <think> 标签；第一个字符必须是 {{，最后一个字符必须是 }}。

用户问题：
{user_question}

当前上下文：
{context_summary}
{tool_summary_block}"""

ACADEMIC_SKILL_HINT = """
侧重：论文作者、实验方法与结果、相关工作、是否有更新研究。"""

KB_HINT = """
侧重：文献细节、标准规范引用、实验数据、与相关概念对比。"""

GENERAL_HINT = """
侧重：概念延伸、参数对比、应用场景、注意事项。"""

STRICT_JSON_OUTPUT_REMINDER = """
最终只输出 JSON 对象，不输出思考过程、解释、Markdown、代码块或 <think> 标签。"""


def build_follow_up_prompt(
    *,
    count: int,
    user_question: str,
    assistant_answer: str,
    tool_summary: str = "",
    selected_skills: list[str] | None = None,
) -> str:
    skills = {str(s).strip().lower() for s in (selected_skills or []) if str(s).strip()}
    hint = GENERAL_HINT
    if "academic-search" in skills or "academic_search" in skills:
        hint = ACADEMIC_SKILL_HINT
    elif tool_summary and any(k in tool_summary for k in ("论文", "文献", "检索")):
        hint = ACADEMIC_SKILL_HINT
    elif any(k in (tool_summary or "") for k in ("知识库", "标准", "规范")):
        hint = KB_HINT

    tool_summary_block = ""
    if tool_summary.strip():
        tool_summary_block = f"\n工具检索摘要：\n{tool_summary.strip()}\n"

    return (
        BASE_FOLLOW_UP_PROMPT.format(
            count=count,
            user_question=user_question,
            assistant_answer=assistant_answer,
            tool_summary_block=tool_summary_block,
        )
        + hint
        + STRICT_JSON_OUTPUT_REMINDER
    )


def build_follow_up_context_prompt(
    *,
    count: int,
    user_question: str,
    context_summary: str = "",
    tool_summary: str = "",
    selected_skills: list[str] | None = None,
) -> str:
    skills = {str(s).strip().lower() for s in (selected_skills or []) if str(s).strip()}
    hint = GENERAL_HINT
    combined = f"{context_summary}\n{tool_summary}"
    if "academic-search" in skills or "academic_search" in skills:
        hint = ACADEMIC_SKILL_HINT
    elif any(k in combined for k in ("论文", "文献", "检索")):
        hint = ACADEMIC_SKILL_HINT
    elif any(k in combined for k in ("知识库", "标准", "规范")):
        hint = KB_HINT

    tool_summary_block = ""
    if tool_summary.strip():
        tool_summary_block = f"\n工具/检索摘要：\n{tool_summary.strip()}\n"

    return (
        CONTEXT_FOLLOW_UP_PROMPT.format(
            count=count,
            user_question=user_question,
            context_summary=context_summary.strip() or "暂无额外上下文",
            tool_summary_block=tool_summary_block,
        )
        + hint
        + STRICT_JSON_OUTPUT_REMINDER
    )
