"""探索式学习（Exploratory Learning）相关 Prompt 构建器。

所有需要结构化输出的能力（主题、脑图、节点展开、习题）均强约束输出 JSON，
便于服务层用稳健解析（正则提取 + json.loads + 兜底）处理。知识点详解走流式 Markdown。
"""

from __future__ import annotations


def _context_block(context: str) -> str:
    """将知识库检索上下文包装为提示块；为空时提示基于学科通用知识作答。"""
    ctx = (context or "").strip()
    if not ctx:
        return "（请基于该学科的通用专业知识进行作答，结构清晰、准确，但不要编造具体数值或型号。）"
    return ctx


def build_map_prompt(*, subject: str, topic: str, max_children: int, context: str) -> str:
    """脑图态：为根主题生成第一层子知识点。"""
    return f"""你是「{subject}」领域的课程设计专家。学习者想以「{topic}」为主题开展探究式学习。

请将「{topic}」拆解为 {max_children} 个以内的**一级子知识点**，构成思维导图的第一层。

要求：
1. 子知识点应是「{topic}」的合理组成部分或关键维度，逻辑清晰、覆盖全面。
2. root.summary 用一句话概括该主题（不超过 40 字）。
3. 每个子节点 title 不超过 14 字；summary 一句话说明（不超过 40 字）。
4. expandable 表示该节点是否还能继续向下拆解（通常为 true）。
5. 基于你的专业知识快速拆解，不要编造。严格只输出 JSON，不要任何额外说明。

{_context_block(context)}

输出 JSON 格式：
{{"root": {{"title": "{topic}", "summary": "一句话概述"}},
 "children": [{{"title": "子知识点", "summary": "一句话说明", "expandable": true}}]}}"""


def build_expand_prompt(*, subject: str, node_title: str, path: list[str], max_children: int, context: str) -> str:
    """脑图态：展开某节点的下一层子知识点。"""
    path_str = " → ".join([p for p in path if p]) or node_title
    return f"""你是「{subject}」领域的课程设计专家。当前思维导图路径为：{path_str}。

请将节点「{node_title}」进一步拆解为 {max_children} 个以内的**下一级子知识点**。

要求：
1. 子知识点必须是「{node_title}」的细化或子领域，与上层路径保持一致，避免重复上层概念。
2. 每个子节点 title 不超过 14 字；summary 一句话说明（不超过 40 字）。
3. 若该节点已属于足够细的叶子概念、不宜再拆，可返回空数组。
4. 基于你的专业知识快速拆解，不要编造。严格只输出 JSON，不要任何额外说明。

{_context_block(context)}

输出 JSON 格式：
{{"children": [{{"title": "子知识点", "summary": "一句话说明", "expandable": true}}]}}"""


def build_detail_prompt(*, subject: str, node_title: str, path: list[str], context: str) -> str:
    """知识点详解：流式 Markdown 长文。"""
    path_str = " → ".join([p for p in path if p]) or node_title
    return f"""你是「{subject}」领域的资深讲师。请为学习者撰写知识点「{node_title}」的详解。
当前知识脉络：{path_str}。

写作要求：
1. 使用规范的 Markdown：以 `## {node_title}详解` 作为标题，按「基本定义与核心功能」「典型结构 / 原理」「关键要点」等小节组织（用 `###` 二级小标题）。
2. 内容必须**严格依据下方知识库内容**；知识库未覆盖处可用学科常识补充，但不得编造具体数值、型号或文献。
3. 如需公式，使用 KaTeX 语法（行内 `$...$`，行间 `$$...$$`）。
4. 语言专业、条理清晰，篇幅约 400-800 字。
5. 不要在正文末尾编造参考文献列表。

知识库内容：
{_context_block(context)}

现在开始撰写「{node_title}」的详解："""


_TYPE_LABELS = {
    "choice": "单选题（4 个选项，唯一正确答案）",
    "multi": "多选题（4 个选项，2 个及以上正确答案）",
    "judge": "判断题（答案为「正确」或「错误」）",
    "short": "简答题（提供参考答案与采分点）",
    "calc": "计算题（提供解题步骤与最终答案）",
}

_DIFFICULTY_LABELS = {
    "all": "难度分布合理（简单/中等/困难均可）",
    "easy": "全部为简单题",
    "medium": "全部为中等难度题",
    "hard": "全部为困难题",
}


def build_quiz_prompt(
    *,
    subject: str,
    node_title: str,
    types: list[str],
    difficulty: str,
    count: int,
    context: str,
) -> str:
    """习题练习：生成题库（多题型）。"""
    type_desc = "、".join(_TYPE_LABELS.get(t, t) for t in types) or "选择题与简答题"
    diff_desc = _DIFFICULTY_LABELS.get(difficulty, _DIFFICULTY_LABELS["all"])
    return f"""你是「{subject}」领域的命题专家。请围绕知识点「{node_title}」命制 {count} 道练习题，用于巩固学习成果。

题型要求：覆盖以下题型 —— {type_desc}。
难度要求：{diff_desc}。

通用要求：
1. 题目必须**基于下方知识库内容与学科常识**，答案准确，不得编造。
2. 每道题包含字段：type（choice/multi/judge/short/calc 之一）、difficulty（easy/medium/hard）、stem（题干）。
3. choice/multi 题需 options（含 key=A/B/C/D 与 text）与 answer（单选为单个字母，多选为字母数组或如 "AC" 的字符串）。
4. judge 题 answer 为「正确」或「错误」。
5. short/calc 题提供 reference_answer（参考答案）与 scoring_points（采分点数组）。
6. 每题都应有 explanation（解析，简明说明为何如此）。
7. 每道题题干不超过 60 字；选择题选项不超过 24 字；explanation 不超过 48 字；reference_answer 不超过 120 字；scoring_points 每条不超过 24 字。
8. JSON 字符串中不要使用 LaTeX/KaTeX 反斜杠公式；公式请写成纯文本形式，例如 Vn=sqrt(4*k*T*R*B)。
9. 必须生成恰好 {count} 道题。严格只输出可被 json.loads 解析的 JSON，不要任何额外说明。

知识库内容：
{_context_block(context)}

输出 JSON 格式：
{{"questions": [
  {{"type": "choice", "difficulty": "easy", "stem": "题干",
    "options": [{{"key": "A", "text": "选项"}}, {{"key": "B", "text": "选项"}}, {{"key": "C", "text": "选项"}}, {{"key": "D", "text": "选项"}}],
    "answer": "B", "explanation": "解析"}},
  {{"type": "short", "difficulty": "medium", "stem": "题干",
    "reference_answer": "参考答案", "scoring_points": ["采分点1", "采分点2"], "explanation": "解析"}}
]}}"""


def build_grade_prompt(*, subject: str, stem: str, reference_answer: str, scoring_points: list[str], user_answer: str) -> str:
    """主观题评分。"""
    points = "\n".join(f"- {p}" for p in scoring_points if str(p).strip()) or "（无显式采分点，请按参考答案酌情评分）"
    return f"""你是「{subject}」领域的阅卷老师。请对学习者的主观题作答进行评分。

题目：{stem}

参考答案：{reference_answer}

采分点：
{points}

学习者的作答：
{user_answer or "（未作答）"}

评分要求：
1. score 为 0-100 的整数。
2. verdict 为「正确」「部分正确」「错误」之一。
3. feedback 给出简明点评（不超过 120 字）。
4. missing_points 列出学习者遗漏或答错的关键点（数组，可为空）。
5. 严格只输出 JSON。

输出 JSON 格式：
{{"score": 85, "verdict": "部分正确", "feedback": "点评", "missing_points": ["遗漏点1"]}}"""
