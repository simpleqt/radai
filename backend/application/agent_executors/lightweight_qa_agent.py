"""Lightweight QA executor used by platform streaming runtime."""

from __future__ import annotations

import asyncio
import json
import logging
import re
import time
import uuid
from typing import TYPE_CHECKING, Any, Literal

if TYPE_CHECKING:
    from backend.services.chat_file_index import ChatFileIndex

from langchain_core.messages import AIMessage, HumanMessage, SystemMessage
from langchain_core.runnables import RunnableConfig
from langgraph.graph.state import CompiledStateGraph

from backend.core.models import create_chat_model
from backend.application.agent_executors.stream_utils import extract_web_search_metadata

logger = logging.getLogger(__name__)

# ---------------------------------------------------------------------------
# 模块级压缩缓存：thread_id -> (摘要文本, 已压缩的消息数量)
# 压缩在后台异步执行，结果下一轮生效
# ---------------------------------------------------------------------------
_compress_cache: dict[str, tuple[str, int]] = {}
_compress_tasks: dict[str, "asyncio.Task[None]"] = {}


def _get_thread_id(effective_config: RunnableConfig) -> str:
    configurable = (effective_config or {}).get("configurable") or {}
    return str(configurable.get("thread_id") or "default")


# ---------------------------------------------------------------------------
# System / Answer 模板（NE 轻量回答结构；系统角色头由 prompt_templates.ne.build_system_prompt 提供）
# ---------------------------------------------------------------------------
PromptDomain = Literal["ne", "csh"]

LIGHTWEIGHT_ANSWER_TEMPLATE_PROMPT = """\
<lightweight_answer_template>
你必须严格按照以下学术标准和结构模板生成回答，不可有任何偏差。

## 一、 回答结构要求（强制）

### 1. 开头结论（强制）
- 回答开头必须先给出“结论段”，但不要写“结论：”字样。
- 结论段控制在 1-3 句话，直接回答用户问题核心，不要先写背景铺垫。
- 若结论依赖前提条件，需在该段同时说明适用边界。

### 2. 整体结构
直接采用"一、二、三、四"等数字序号作为主标题，形成连贯的层次结构：
- 主标题（一、二、三、四...）：主题模块
- 二级标题（1. 2. 3...）：具体要点
- 三级内容：详细说明

### 3. 结尾总结
在回答的最后，必须包含一个独立的总结段落，以"总结："或"综上所述"开头，概括回答的核心要点。

### 4. 参考来源格式
在最后标注文献来源：
- 使用数字编号 [1], [2], [3]...
- 只显示具体文章/文献名称（不含路径和后缀），必须与上下文「可引用文献」或片段标题「来自：XXX」中的名称一致
- **严禁**使用知识库/容器名称作为引用（如"核电子学库"、"期刊论文库"）
- 正文中的 [n] 与末尾「参考文档」列表必须编号一一对应，不得遗漏或混用
- 必须使用 Markdown 列表逐条输出，每条单独一行，格式为：`- [n] 文献名`
- `参考文档：` 与第一条引用之间保留一个空行
- 禁止把多条引用写在同一段落中（例如 `[1] ... [2] ... [3] ...`）

示例：
参考文档：

- [1] 核辐射与放射性勘查-第十章-能量色散X射线荧光方法
- [2] 粒子探测器与数据获取

---

## 二、 图片引用与排版规则（强制要求）
- **仅限使用检索到的图片**：严禁伪造、虚构或从外部互联网引用任何未在当前上下文中提供的图片。你只能使用上下文中明确列出的 `[IMAGE:xxx]` 标识符或图片 URL。无标识符或图片时绝对禁止产出图片。
- **插入格式**：使用标准 Markdown 语法 `![图片描述]([IMAGE:xxx])` 或 `![图片描述](图片URL)`。
- **整合原则**：图片必须与上下文相关，但必须使用块级展示，不得与正文在同一行混排。
- **换行硬约束**：图片前后各保留 1 个空行，且图片语法单独占一行。
- **禁止行内图片**：禁止输出“……如图所示![图](URL)……”或“（![图](URL)）”这类写法。
- **图下注释**：每张图片下方必须紧跟一行图名/图注，格式为 `*图n：图名*`（例如 `*图1：电荷灵敏前置放大器原理图*`）。
- **图注编号一致性**：图注编号必须与文中引用一致（如“如图2所示”对应 `*图2：...*`），禁止编号跳跃或重复。
- **上下文关联**：每张图片前必须有至少 1-2 句文字说明，解释图片内容及其与当前论述的关系。
- **一致性**：确保图片的 `alt` 描述与上下文内容高度匹配。

推荐图片段落模板：

说明文字（1-2句，解释该图与当前论述的关系）。

![图1：电荷灵敏前置放大器原理图]([IMAGE:xxx])

*图1：电荷灵敏前置放大器原理图*

---

## 三、 公式渲染规则
- **行内公式**：必须使用单美元符号包裹，例如 $E=mc^2$
- **行间公式**：必须使用双美元符号包裹，例如 $$E=mc^2$$
- **禁止裸文本**：禁止把上/下标写成裸文本（如 ^252Cf、x_0、10^6 n·s^-1），必须放入公式环境。
- **同位素**：同位素必须写为 LaTeX 兼容形式，例如 $^{252}\\mathrm{Cf}$、$^{60}\\mathrm{Co}$。
- **科学计数法**：优先写为 $a \\times 10^b$，单位建议使用 $\\mathrm{...}$ 包裹。
- **禁止 HTML 标签**：禁止输出 HTML 上下标标签，包括 `<sup>...</sup>`、`<sub>...</sub>` 及其变体（如 `< sup >`、`&lt;sup&gt;`）。
- **规范化**：若上下文中出现 OCR/抽取噪声形式（如 `cm < sup > 3 < / sup >`），需要在回答中规范化为 KaTeX 写法（如 $\\mathrm{cm^3}$），数值本身保持不变。

---

## 四、 回答聚焦与数值精确性
- **直接回答**：始终围绕用户原始问题展开，避免无关的背景介绍或发散。
- **数值精确性**：使用知识库中的原始数值，必须原样引用，严禁简化、修改或四舍五入。
- **对比分析表格要求**：当回答涉及两个或多个概念的比较时，使用清晰的表格进行对比。
</lightweight_answer_template>
"""


def _build_lightweight_system_header_and_template(
    *,
    prompt_domain: PromptDomain,
    use_search: bool,
    lightweight_has_kb: bool,
) -> tuple[str, str]:
    """按域返回轻量模式系统头与回答结构模板。"""
    domain = str(prompt_domain or "ne").strip().lower()
    has_kb = bool(lightweight_has_kb)
    if domain == "csh":
        from backend.application.agents.prompt_templates import csh

        header = csh.build_system_prompt(
            subagent_enabled=False,
            use_search=use_search,
            available_skills=None,
            task_type=None,
            lightweight_mode=True,
            lightweight_has_kb=has_kb,
        )
        return header, csh.CSH_LIGHTWEIGHT_ANSWER_TEMPLATE_PROMPT

    from backend.application.agents.prompt_templates import ne

    header = ne.build_system_prompt(
        subagent_enabled=False,
        use_search=use_search,
        available_skills=None,
        task_type=None,
        lightweight_mode=True,
        lightweight_has_kb=has_kb,
    )
    return header, LIGHTWEIGHT_ANSWER_TEMPLATE_PROMPT


def _get_lightweight_search_prefix(prompt_domain: PromptDomain) -> str:
    """按域返回检索词生成任务的前缀说明（NE / CSH 不可混用）。"""
    domain = str(prompt_domain or "ne").strip().lower()
    if domain == "csh":
        from backend.application.agents.prompt_templates import csh

        return csh.LIGHTWEIGHT_SEARCH_QUERY_PREFIX
    from backend.application.agents.prompt_templates import ne

    return ne.LIGHTWEIGHT_SEARCH_QUERY_PREFIX


def _get_lightweight_file_search_prefix(prompt_domain: PromptDomain) -> str:
    domain = str(prompt_domain or "ne").strip().lower()
    if domain == "csh":
        from backend.application.agents.prompt_templates import csh

        return csh.LIGHTWEIGHT_FILE_SEARCH_PREFIX
    from backend.application.agents.prompt_templates import ne

    return ne.LIGHTWEIGHT_FILE_SEARCH_PREFIX


# ---------------------------------------------------------------------------
# 工具函数
# ---------------------------------------------------------------------------
def _make_process_event(
    kind: str,
    title: str,
    detail: str,
    *,
    tool: str | None = None,
    level: str = "info",
    duration_ms: int | None = None,
    summary: str | None = None,
    metadata: dict[str, Any] | None = None,
) -> dict[str, Any]:
    event_id = str(uuid.uuid5(uuid.NAMESPACE_DNS, f"lightweight:{kind}:{title}:{detail}:{tool}"))
    event: dict[str, Any] = {
        "id": event_id,
        "kind": kind,
        "title": title,
        "detail": detail,
        "level": level,
        "timestamp": int(time.time() * 1000),
    }
    if tool:
        event["tool"] = tool
    if duration_ms is not None:
        event["duration_ms"] = duration_ms
    if summary:
        event["summary"] = summary
    if metadata:
        event["metadata"] = metadata
    return event


def _stringify_message_content(content: Any) -> str:
    if isinstance(content, str):
        return content
    if isinstance(content, list):
        return "".join(_stringify_message_content(item) for item in content)
    if isinstance(content, dict):
        if isinstance(content.get("text"), str):
            return content["text"]
        if "content" in content:
            return _stringify_message_content(content.get("content"))
        if isinstance(content.get("output_text"), str):
            return content["output_text"]
    if hasattr(content, "content"):
        return _stringify_message_content(getattr(content, "content"))
    if hasattr(content, "text") and isinstance(getattr(content, "text"), str):
        return getattr(content, "text")
    return ""


def _extract_reasoning_content(message: Any) -> str:
    # 1. Direct attribute extraction
    direct = getattr(message, "reasoning_content", None)
    if direct:
        return _stringify_message_content(direct)

    # 2. Fallback to additional_kwargs
    additional_kwargs = getattr(message, "additional_kwargs", None)
    if isinstance(additional_kwargs, dict):
        reasoning = additional_kwargs.get("reasoning_content") or additional_kwargs.get("reasoning")
        if reasoning:
            return _stringify_message_content(reasoning)
    return ""


def _summarize_text(value: Any, max_length: int = 500) -> str:
    text = _stringify_message_content(value) if not isinstance(value, str) else value
    text = " ".join(str(text or "").split())
    return f"{text[:max_length]}..." if len(text) > max_length else text


def _strip_thinking_markup(text: str) -> str:
    if not text:
        return ""
    cleaned = re.sub(r"<(?:think|thought)>[\s\S]*?</(?:think|thought)>", "", text, flags=re.IGNORECASE)
    cleaned = re.sub(r"<(?:think|thought)>[\s\S]*$", "", cleaned, flags=re.IGNORECASE)
    return cleaned.strip()


def _clean_question(text: str) -> str:
    """清理问题文本，去掉注入的运行时元数据与文档摘要预览，仅保留用户原始问题。"""
    if not text:
        return ""
    # 去掉时间上下文标签
    cleaned = re.sub(r"<runtime_current_time>[\s\S]*?</runtime_current_time>", "", text, flags=re.IGNORECASE)
    # 去掉文件提示（"[用户已上传长文档：xxx，如需查找请使用 chat_file_search 工具]"）
    cleaned = re.sub(r"\[用户已上传长文档[：:][^\]]+\]", "", cleaned)
    # 去掉表格文件运行时提示，轻量模式会从 thread_id 单独读取表格文件清单。
    cleaned = re.sub(
        r"\[用户已上传可分析表格文件\][\s\S]*?(?=<runtime_current_time>|\[ConfirmedResearchPlan\]|$)",
        "",
        cleaned,
        flags=re.IGNORECASE,
    )
    # 去掉短文档注入的 <uploaded_document> 块
    cleaned = re.sub(r"<uploaded_document[^>]*>[\s\S]*?</uploaded_document>", "", cleaned, flags=re.IGNORECASE)
    # 去掉长文档预览内容前缀
    cleaned = re.sub(r"已上传长文档[：:][^\n]+\n文档前 \d+ 字内容概要[^\n]*\n\"\"\"[\s\S]*?\"\"\"", "", cleaned, flags=re.IGNORECASE)
    # 去掉科研规划标签
    cleaned = re.sub(r"\[ConfirmedResearchPlan\][\s\S]*?\[/ConfirmedResearchPlan\]", "", cleaned, flags=re.IGNORECASE)
    return cleaned.strip()


_SKIP_KB_TOKEN = "SKIP_KB"


def _is_skip_kb_decision(raw: str) -> bool:
    """判断模型是否决定跳过知识库/联网检索（SKIP_KB 为内部协议，非用户检索词）。"""
    text = (raw or "").strip()
    if not text:
        return False
    if text.upper().startswith(_SKIP_KB_TOKEN):
        return True
    lines = [line.strip() for line in text.splitlines() if line.strip()]
    if lines and lines[-1].upper() == _SKIP_KB_TOKEN:
        return True
    return bool(re.search(r"(?m)^\s*SKIP_KB\s*$", text, flags=re.IGNORECASE))


def _sanitize_search_query(text: str) -> str:
    """清理模型输出的搜索词，去掉思考标签、引号、前缀说明等。"""
    cleaned = re.sub(r"<(?:think|thought)>[\s\S]*?</(?:think|thought)>", "", text or "", flags=re.IGNORECASE)
    cleaned = re.sub(r"<(?:think|thought)>[\s\S]*$", "", cleaned, flags=re.IGNORECASE)
    cleaned = re.sub(r"^```(?:\w+)?\s*|\s*```$", "", cleaned.strip())
    cleaned = re.sub(r'["\'""'']', "", cleaned)
    cleaned = re.sub(r"^(?:搜索词|检索词|query|search query)\s*[:：]\s*", "", cleaned, flags=re.IGNORECASE)
    lines = [line.strip(" \t-:：") for line in cleaned.splitlines() if line.strip()]
    result = (lines[-1] if lines else "").strip()
    if result.upper() == _SKIP_KB_TOKEN:
        return ""
    return result


def _display_search_detail(search_query: str, *, skip_kb: bool, fallback: str) -> str:
    """生成面向用户的检索词展示文案，避免泄露内部协议 SKIP_KB。"""
    q = (search_query or "").strip()
    if skip_kb or q.upper() == _SKIP_KB_TOKEN:
        return fallback[:80]
    return q[:80]


def _trim_prompt_context(text: str, max_chars: int, *, truncated_note: str) -> str:
    """压缩注入到主模型的上下文，避免轻量模式系统提示过大。"""
    cleaned = str(text or "").strip()
    if not cleaned or len(cleaned) <= max_chars:
        return cleaned

    head_chars = max(1, int(max_chars * 0.7))
    tail_chars = max(1, max_chars - head_chars - len(truncated_note) - 2)
    return f"{cleaned[:head_chars]}\n{truncated_note}\n{cleaned[-tail_chars:]}"


def _compact_kb_context(text: str) -> str:
    """移除检索结果中仅供调试/重复约束的冗余文案，保留真正证据内容。"""
    compact = str(text or "").strip()
    if not compact:
        return ""

    compact = re.sub(
        r"^## 检索到的上下文 \(来自 \d+ 个活跃知识库\)\s*",
        "",
        compact,
    )
    compact = re.sub(
        r"\n## 本轮可用图片白名单[\s\S]*?(?=\n## |\Z)",
        "\n",
        compact,
    )
    compact = re.sub(
        r"\n\n> \*\*强制规则\*\*：\n(?:>.*(?:\n|$))*",
        "\n",
        compact,
    )
    compact = re.sub(
        r"<lightweight_answer_template>[\s\S]*?</lightweight_answer_template>",
        "",
        compact,
        flags=re.IGNORECASE,
    )
    compact = re.sub(r"\n## 检索统计[\s\S]*$", "", compact).strip()
    return compact


def _summarize_kb_for_process_ui(kb_context: str, *, max_length: int = 500) -> str:
    """生成面向过程面板的知识库摘要，避免泄露长证据或跨域模板噪声。"""
    compact = _compact_kb_context(kb_context)
    if not compact:
        return ""

    titles = re.findall(r"### 片段 \d+（来自：([^）]+)）", compact)
    if titles:
        unique_titles: list[str] = []
        seen: set[str] = set()
        for title in titles:
            name = title.strip()
            if name and name not in seen:
                seen.add(name)
                unique_titles.append(name)
        lines = [f"命中 {len(unique_titles)} 个文献/片段："] + [
            f"- {name}" for name in unique_titles[:8]
        ]
        summary = "\n".join(lines)
        if len(summary) <= max_length:
            return summary
        return f"{summary[: max_length - 3]}..."

    cleaned = re.sub(r"(?m)^>.*$", "", compact)
    cleaned = re.sub(r"(?m)^#{1,6}\s+.*$", "", cleaned)
    cleaned = " ".join(cleaned.split())
    if not cleaned:
        return "已获取知识库相关片段"
    if len(cleaned) > max_length:
        return f"{cleaned[: max_length - 3]}..."
    return cleaned


def _parse_file_search_terms(raw: str) -> tuple[str, list[str]]:
    """解析模型输出的文档检索词与关键词列表。"""
    cleaned = _strip_thinking_markup(raw or "").strip()
    search_query = ""
    keywords: list[str] = []
    in_keywords = False

    for line in cleaned.splitlines():
        stripped = line.strip()
        if not stripped:
            continue

        upper = stripped.upper()
        if upper.startswith("QUERY:"):
            search_query = _sanitize_search_query(stripped[len("QUERY:"):])
            in_keywords = False
            continue
        if upper.startswith("KEYWORDS:") or stripped in {"关键词:", "关键词："}:
            in_keywords = True
            continue

        if not in_keywords and not search_query:
            for prefix in ("QUERY:", "query:", "Query:", "检索词:", "检索词："):
                if stripped.startswith(prefix):
                    search_query = _sanitize_search_query(stripped[len(prefix):])
                    break
            if search_query:
                continue

        in_keywords = True
        kw = re.sub(r"^[\d\.\-\*\s]+", "", stripped).strip().strip(".,;：:")
        kw = re.sub(r'["\'""'']', "", kw)
        if kw and len(kw) > 1 and kw.upper() != _SKIP_KB_TOKEN:
            keywords.append(kw)

    return search_query, keywords[:8]


_TABLE_ANALYSIS_KEYWORDS = (
    "表格", "excel", "xlsx", "xls", "csv", "sheet", "工作表", "列", "字段",
    "统计", "汇总", "分组", "筛选", "排序", "top", "最大", "最小", "平均",
    "均值", "总和", "求和", "缺失", "相关", "趋势", "图表", "画图", "可视化",
    "柱状图", "折线图", "散点图", "热力图",
)
_TABLE_GENERIC_ANALYSIS_KEYWORDS = ("分析一下", "总结一下", "看一下", "概括一下", "有什么结论")
_TABLE_STRUCTURE_KEYWORDS = ("有哪些列", "什么列", "列名", "字段", "sheet", "工作表", "结构")
_TABLE_CHART_KEYWORDS = ("画图", "图表", "可视化", "趋势图", "柱状图", "折线图", "散点图", "热力图")
_TABLE_SQL_KEYWORDS = ("分组", "筛选", "排序", "top", "最大", "最小", "平均", "均值", "总和", "求和", "统计")


def _is_table_analysis_query(question: str, table_files: list[dict[str, Any]]) -> bool:
    if not table_files:
        return False
    q = str(question or "").strip().lower()
    if not q:
        return False
    if any(keyword in q for keyword in _TABLE_ANALYSIS_KEYWORDS):
        return True
    return any(keyword in q for keyword in _TABLE_GENERIC_ANALYSIS_KEYWORDS)


def _pick_table_file(question: str, table_files: list[dict[str, Any]]) -> dict[str, Any] | None:
    if not table_files:
        return None
    q = str(question or "")
    for item in table_files:
        file_name = str(item.get("file_name") or "")
        file_id = str(item.get("file_id") or "")
        if file_name and file_name in q:
            return item
        if file_id and file_id in q:
            return item
    return table_files[0]


def _extract_json_object(text: str) -> dict[str, Any] | None:
    raw = str(text or "").strip()
    if not raw:
        return None
    raw = re.sub(r"^```(?:json)?\s*", "", raw, flags=re.IGNORECASE)
    raw = re.sub(r"\s*```$", "", raw)
    match = re.search(r"\{[\s\S]*\}", raw)
    candidate = match.group(0) if match else raw
    try:
        parsed = json.loads(candidate)
    except Exception:
        return None
    return parsed if isinstance(parsed, dict) else None


async def _extract_file_search_terms(
    query: str,
    file_idx: "ChatFileIndex",  # type: ignore[name-defined]  # avoid circular import
    model_name: str | None,
    timeout: float = 8.0,
    *,
    prompt_domain: PromptDomain = "ne",
) -> tuple[str, list[str]]:
    """用模型生成面向上传文档的检索语句与关键词。

    返回：(search_query, keywords)
      - search_query: 供 BM25/TF-IDF 使用的检索语句（非用户原句）
      - keywords: 精确匹配关键词，支持跨语言
    """
    fallback_query = (query or "").strip()
    if not fallback_query:
        return "", []
    if not file_idx or file_idx.chunk_count == 0:
        return fallback_query, []

    # 1. 优先检测是否为通用总结/概述型查询，立即返回默认的检索词与空关键词，避免 LLM 调用和可能出现的超时/格式错误
    from backend.services.chat_file_index import _is_generic_summary_query
    if _is_generic_summary_query(fallback_query):
        logger.info(
            "[_extract_file_search_terms] 检测到通用总结/概述型问题，直接旁路 LLM 提取以秒级响应"
        )
        return "abstract introduction summary main contribution", []

    file_names = [c.file_name for c in file_idx._chunks[:1]]
    file_hint = file_names[0] if file_names else "未知文件"

    prefix_preview = ""
    try:
        from backend.config.settings import settings
        prefix_len = min(getattr(settings, "long_doc_prefix_length", 1000), 1000)
        preview_chunks = sorted(file_idx._chunks, key=lambda c: c.chunk_index)
        preview_text = ""
        for chunk in preview_chunks:
            preview_text += chunk.content + "\n"
            if len(preview_text) >= prefix_len:
                break
        prefix_preview = preview_text[:prefix_len].strip()
    except Exception:
        prefix_preview = ""

    prompt_lines = [
        _get_lightweight_file_search_prefix(prompt_domain).strip(),
        "任务：根据用户问题，生成在上传文档中检索用的查询语句和关键词。",
        f"文件名：{file_hint}",
    ]
    if prefix_preview:
        prompt_lines.append(
            f"文档开头预览（用于判断文档语言与主题）：\n\"\"\"\n{prefix_preview}\n\"\"\""
        )
    prompt_lines += [
        f"用户问题：{fallback_query}",
        "",
        "输出规则（严禁分析过程，严格两行格式）：",
        "  QUERY: <检索语句，5-30字，优先使用文档语言中的术语>",
        "  KEYWORDS:",
        "  <关键词1>",
        "  <关键词2>",
        "",
        "注意：",
        "- 用户问“讲了什么/总结/概述”时，QUERY 应转为 abstract introduction summary main contribution 等文档检索词",
        "- 若文档是英文，QUERY 与 KEYWORDS 优先英文；可同时给出中英文关键词",
        "- 禁止直接复述用户原句作为 QUERY",
        "输出：",
    ]

    try:
        from backend.core.agents.config_compat import get_default_model_name
        model = create_chat_model(name=model_name or get_default_model_name(), thinking_enabled=False)
        response = await asyncio.wait_for(
            model.ainvoke("\n".join(prompt_lines), config={"max_tokens": 120}),
            timeout=timeout,
        )
        raw = _stringify_message_content(getattr(response, "content", response))
        search_query, keywords = _parse_file_search_terms(raw)
        if not search_query:
            search_query = fallback_query

        logger.info(
            "[_extract_file_search_terms] question=%s → query=%s, keywords=%s",
            fallback_query[:50],
            search_query[:80],
            keywords,
        )
        return search_query, keywords
    except asyncio.TimeoutError:
        logger.warning("[_extract_file_search_terms] 检索词生成超时，回退到规则提取关键词")
    except Exception as exc:
        logger.warning("[_extract_file_search_terms] 检索词生成失败: %s", exc)

    # 2. 兜底逻辑：如果模型生成超时或失败，通过正则分词提取核心的高价值概念作为关键词，避免直接使用原句进行 BM25 检索无结果
    fallback_kws = []
    # 提取汉字连续串(不含标点)与英文单词作为关键词候选，并过滤高频无意义词
    stop_words = {
        "是什么", "介绍", "什么是", "怎么", "如何", "为什么", "什么", "的", "了", "在", "是", "有", 
        "关于", "对于", "如何", "讲了什么", "总结", "概述", "分析", "论文", "文档", "文章", "报告",
        "吗", "呢", "吧", "呀", "啊", "谁", "哪里", "哪个", "怎么做", "怎样", "请问"
    }
    for m in re.finditer(r"[\u4e00-\u9fff]{2,}", fallback_query):
        w = m.group()
        if w not in stop_words:
            fallback_kws.append(w)
    for m in re.finditer(r"[a-zA-Z]{3,}", fallback_query):
        w = m.group()
        if w.lower() not in stop_words:
            fallback_kws.append(w)
    fallback_kws = sorted(list(set(fallback_kws)), key=len, reverse=True)[:5]

    return fallback_query, fallback_kws


# ---------------------------------------------------------------------------
# LightweightQAAgent
# ---------------------------------------------------------------------------
class LightweightQAAgent:
    STREAM_HEARTBEAT_INTERVAL_SECONDS = 15

    def __init__(
        self,
        *,
        graph: CompiledStateGraph,
        model_name: str | None,
        kb_ids: list[str],
        use_search: bool,
        node_name: str = "model",
        history_limit: int = 16,
        history_compress_chars: int = 6000,
        thread_id: str | None = None,
        thinking_enabled: bool = False,
        prompt_domain: PromptDomain = "ne",
        guardrail_policy: str = "default",
        runtime_permissions: dict[str, Any] | None = None,
        guardrail_instructions: str = "",
        time_context: str = "",
        allowed_tools: list[str] | None = None,
        use_direct_query: bool = False,  # 优化: 探索式学习直接使用知识点标题作为检索词
    ) -> None:
        self._graph = graph
        self._model_name = model_name
        self._kb_ids = kb_ids
        self._use_search = use_search
        self._node_name = node_name
        self._history_limit = history_limit
        self._history_compress_chars = history_compress_chars
        self._thread_id = thread_id
        self._thinking_enabled = thinking_enabled
        self._guardrail_policy = str(guardrail_policy or "default")
        self._runtime_permissions = dict(runtime_permissions or {})
        self._guardrail_instructions = str(guardrail_instructions or "")
        self._time_context = str(time_context or "")
        self._allowed_tools = list(allowed_tools or [])
        self._prompt_domain: PromptDomain = (
            "csh" if str(prompt_domain or "ne").strip().lower() == "csh" else "ne"
        )
        self._use_direct_query = use_direct_query  # 优化标记

    # ------------------------------------------------------------------
    # 核心流式查询：直接检索 → 流式回答，无模型决策环节
    # ------------------------------------------------------------------
    async def aquery_stream(
        self,
        *,
        question: str,
        effective_config: RunnableConfig,
        stream_started_at: float | None = None,
        image_contents: list[dict] | None = None,
    ):
        stream_started_at = stream_started_at or time.monotonic()

        yield {"type": "status", "content": "已接收问题，正在分析..."}

        phase_started_at = time.monotonic()

        # 1) 加载历史上下文
        history_text = await self._load_and_compress_history(effective_config)
        logger.info(
            "[LightweightQAAgent] 阶段耗时: history_load=%dms",
            int((time.monotonic() - phase_started_at) * 1000),
        )
        clean_q = _clean_question(question)

        # 2) 检查是否有会话级上传文档索引（提前检查，影响搜索词生成策略）
        file_index_context = ""
        table_analysis_context = ""
        has_file_index = False
        file_idx_ref = None
        table_files: list[dict[str, Any]] = []
        if self._thread_id:
            try:
                from backend.services.chat_file_index import get_index_manager
                file_idx_ref = get_index_manager().get(self._thread_id)
                if file_idx_ref and file_idx_ref.chunk_count > 0:
                    has_file_index = True
            except Exception:
                pass
            try:
                from backend.services.table_analysis_files import list_table_files_by_thread

                table_files = list_table_files_by_thread(self._thread_id)
            except Exception as exc:
                logger.warning("[LightweightQAAgent] 表格文件清单读取失败: %s", exc)

        # 3) 生成 KB/Web 检索词
        # - 仅知识库、首轮无历史/无上传文档：直接用用户原句（KB 内部有关键词提取）
        # - 启用联网搜索，或有历史/上传文档：由模型提炼检索词
        # - 优化: 探索式学习模式直接使用知识点标题,跳过LLM提取
        has_retrieval = bool(self._kb_ids or self._use_search)
        has_history = bool(history_text and history_text != "无")
        skip_kb = False
        table_intent = _is_table_analysis_query(clean_q, table_files)
        
        if self._use_direct_query:
            # 探索式学习: 直接使用知识点标题,跳过LLM提取检索词
            search_query = clean_q
            logger.info(
                "[LightweightQAAgent] 探索式学习直查模式: query=%s",
                search_query[:80],
            )
        else:
            needs_model_query = self._use_search or has_history or has_file_index
            if has_retrieval and needs_model_query:
                phase_started_at = time.monotonic()
                search_query, skip_kb = await self._extract_search_query_v2(
                    clean_q, history_text, has_file_index=has_file_index,
                    file_idx=file_idx_ref,
                )
                logger.info(
                    "[LightweightQAAgent] 阶段耗时: search_query=%dms, skip_kb=%s, query=%s",
                    int((time.monotonic() - phase_started_at) * 1000),
                    skip_kb,
                    search_query[:80],
                )
            elif has_retrieval:
                search_query = clean_q
                logger.info(
                    "[LightweightQAAgent] 仅知识库首轮，旁路检索词 LLM 提取: query=%s",
                    search_query[:80],
                )
            else:
                search_query = clean_q

        if table_intent:
            yield {"type": "status", "content": "轻量问答模式：正在分析上传表格..."}
            yield {
                "type": "event",
                "event": _make_process_event(
                    kind="tool_start",
                    title="表格分析开始",
                    detail=f"已识别 {len(table_files)} 个可分析表格文件",
                    tool="table_analysis",
                ),
            }
            phase_started_at = time.monotonic()
            try:
                table_analysis_context = await self._run_table_analysis(clean_q, table_files)
                summary = _summarize_text(table_analysis_context, max_length=500)
                yield {
                    "type": "event",
                    "event": _make_process_event(
                        kind="tool_end",
                        title="表格分析完成",
                        detail="已获取表格结构化分析结果",
                        tool="table_analysis",
                        level="success",
                        duration_ms=int((time.monotonic() - phase_started_at) * 1000),
                        summary=summary,
                    ),
                }
                if summary:
                    yield {
                        "type": "event",
                        "event": _make_process_event(
                            kind="tool_result",
                            title="表格分析摘要",
                            detail=summary,
                            tool="table_analysis",
                            metadata={"summary": summary},
                        ),
                    }
                skip_kb = True
            except Exception as exc:
                logger.warning("[LightweightQAAgent] 表格分析失败: %s", exc)
                yield {
                    "type": "event",
                    "event": _make_process_event(
                        kind="error",
                        title="表格分析失败",
                        detail=str(exc),
                        tool="table_analysis",
                        level="error",
                    ),
                }

        # 4) 生成上传文档检索词（避免直接用用户原句作为检索词）
        file_search_query = clean_q
        file_keywords: list[str] = []
        if has_file_index and file_idx_ref:
            from backend.services.chat_file_index import _is_generic_summary_query
            if _is_generic_summary_query(clean_q):
                # 通用总结/概述类查询：直接旁路 LLM，使用文档结构关键词实现极速秒级响应，检索词优雅展示
                file_search_query = "abstract introduction summary main contribution"
                file_keywords = []
                logger.info("[LightweightQAAgent] 通用总结/概述型问题，直接旁路文档检索词 LLM 提取")
            else:
                yield {"type": "status", "content": "轻量问答模式：正在分析文档检索需求..."}
                phase_started_at = time.monotonic()
                file_search_query, file_keywords = await _extract_file_search_terms(
                    clean_q,
                    file_idx_ref,
                    self._model_name,
                    prompt_domain=self._prompt_domain,
                )
                logger.info(
                    "[LightweightQAAgent] 阶段耗时: file_search_terms=%dms, query=%s",
                    int((time.monotonic() - phase_started_at) * 1000),
                    file_search_query[:80],
                )

        # 5) 并发执行检索（KB / Web / 上传文档三路并行，skip_kb 时跳过前两路）
        kb_context = ""
        web_context = ""

        async def _do_kb_search() -> str:
            return await self._run_kb_search(search_query)

        async def _do_web_search() -> str:
            return await self._run_web_search(search_query)

        async def _do_file_search() -> str:
            """直接检索会话级文档索引（内部自动做多轮召回与合并）。"""
            try:
                from backend.services.chat_file_index import get_index_manager, prepare_search_inputs
                file_idx = get_index_manager().get(self._thread_id)
                if not file_idx or file_idx.chunk_count == 0:
                    return ""

                effective_query, effective_keywords = prepare_search_inputs(
                    file_search_query,
                    file_keywords,
                )
                results = file_idx.search(
                    effective_query, top_k=7, keywords=effective_keywords
                )
                if not results:
                    return ""

                is_fallback = all(r.score == 0.0 for r in results)
                is_kw = effective_keywords and not is_fallback
                parts = []
                for i, r in enumerate(results, 1):
                    if is_fallback:
                        label = "（文档开头）"
                    elif is_kw:
                        label = f"（关键词命中：{r.score:.0%}）"
                    else:
                        label = f"（相关度：{r.score:.2f}）"
                    parts.append(f"### 片段 {i}（来自：{r.file_name}{label}）\n\n{r.content}")

                if is_fallback:
                    header = "以下是上传文档的开头内容（通常含摘要/引言）：\n\n"
                elif is_kw:
                    header = f"关键词/模式匹配成功（关键词：{', '.join(effective_keywords[:5])}）：\n\n"
                else:
                    header = "以下是从上传文档中检索到的相关内容（已做查询改写与混合检索）：\n\n"
                return header + "\n\n---\n\n".join(parts)
            except Exception as e:
                logger.warning("[LightweightQAAgent] 文件索引检索失败: %s", e)
                return ""

        search_coros: list[tuple[str, Any]] = []
        if self._kb_ids and not skip_kb:
            search_coros.append(("kb", _do_kb_search()))
        if self._use_search and not skip_kb:
            search_coros.append(("web", _do_web_search()))
        if has_file_index:
            search_coros.append(("file", _do_file_search()))

        if search_coros:
            status_parts = []
            if self._kb_ids and not skip_kb:
                status_parts.append("知识库")
            if self._use_search and not skip_kb:
                status_parts.append("联网")
            if has_file_index:
                status_parts.append("上传文档")
            if skip_kb and has_file_index:
                status_parts = ["上传文档（已跳过知识库）"]
            yield {"type": "status", "content": f"轻量问答模式：正在检索（{'、'.join(status_parts)}）..."}

            # 发出检索开始事件（skip_kb 时不展示 KB/联网步骤）
            display_query = _display_search_detail(
                search_query, skip_kb=skip_kb, fallback=clean_q,
            )
            if self._kb_ids and not skip_kb:
                yield {
                    "type": "event",
                    "event": _make_process_event(
                        kind="tool_start",
                        title="知识库检索开始",
                        detail=f"检索词: {display_query}",
                        tool="multi_kb_search",
                    ),
                }
            if self._use_search and not skip_kb:
                yield {
                    "type": "event",
                    "event": _make_process_event(
                        kind="tool_start",
                        title="联网搜索开始",
                        detail=f"检索词: {display_query}",
                        tool="web_search",
                    ),
                }
            if has_file_index:
                yield {
                    "type": "event",
                    "event": _make_process_event(
                        kind="tool_start",
                        title="上传文档检索开始",
                        detail=f"检索词: {file_search_query[:80]}",
                        tool="chat_file_search",
                    ),
                }

            # 并发执行所有检索
            phase_started_at = time.monotonic()
            results = await asyncio.gather(
                *[coro for _, coro in search_coros],
                return_exceptions=True,
            )

            for (label, _), result in zip(search_coros, results):
                elapsed_ms = int((time.monotonic() - phase_started_at) * 1000)
                if isinstance(result, BaseException):
                    logger.warning("[LightweightQAAgent] 检索异常 (%s): %s", label, result)
                    result = ""

                if label == "kb":
                    kb_context = str(result)
                    summary = _summarize_kb_for_process_ui(kb_context, max_length=500)
                    yield {
                        "type": "event",
                        "event": _make_process_event(
                            kind="tool_end",
                            title="知识库检索完成",
                            detail="已获取所选知识库的相关片段",
                            tool="multi_kb_search",
                            level="success",
                            duration_ms=elapsed_ms,
                            summary=summary,
                        ),
                    }
                    if summary:
                        yield {
                            "type": "event",
                            "event": _make_process_event(
                                kind="tool_result",
                                title="知识库结果摘要",
                                detail=summary,
                                tool="multi_kb_search",
                                metadata={"summary": summary},
                            ),
                        }
                elif label == "web":
                    web_context = str(result)
                    web_meta = extract_web_search_metadata(result if not isinstance(result, BaseException) else web_context)
                    total = web_meta.get("total_results")
                    url_count = len(web_meta.get("urls") or [])
                    summary = _summarize_text(web_context, max_length=500)
                    yield {
                        "type": "event",
                        "event": _make_process_event(
                            kind="tool_end",
                            title="联网搜索完成",
                            detail=(
                                f"已获取 {total} 条网页结果（{url_count} 个来源链接）"
                                if isinstance(total, int)
                                else "已获取互联网搜索结果"
                            ),
                            tool="web_search",
                            level="success",
                            duration_ms=elapsed_ms,
                            summary=summary,
                            metadata=web_meta,
                        ),
                    }
                    if summary or web_meta:
                        yield {
                            "type": "event",
                            "event": _make_process_event(
                                kind="tool_result",
                                title="联网结果摘要",
                                detail=summary or f"共 {url_count} 个网页来源",
                                tool="web_search",
                                metadata={**web_meta, "summary": summary},
                            ),
                        }
                elif label == "file":
                    file_index_context = str(result)
                    summary = _summarize_text(file_index_context, max_length=300)
                    yield {
                        "type": "event",
                        "event": _make_process_event(
                            kind="tool_end",
                            title="上传文档检索完成",
                            detail=f"从上传文档中找到 {len(file_index_context.split(chr(10)) if file_index_context else [])} 行相关内容",
                            tool="chat_file_search",
                            level="success",
                            duration_ms=elapsed_ms,
                            summary=summary,
                        ),
                    }
                    if summary:
                        yield {
                            "type": "event",
                            "event": _make_process_event(
                                kind="tool_result",
                                title="文档检索结果摘要",
                                detail=summary,
                                tool="chat_file_search",
                                metadata={"summary": summary},
                            ),
                        }

            logger.info(
                "[LightweightQAAgent] 阶段耗时: retrieval=%dms, kb_chars=%d, web_chars=%d, file_chars=%d",
                int((time.monotonic() - phase_started_at) * 1000),
                len(kb_context),
                len(web_context),
                len(file_index_context),
            )

        phase_started_at = time.monotonic()

        # 3) 构建 messages
        # 注意：部分 Qwen 兼容端点（如 qwen3.5-35b）严格要求 SystemMessage 只能出现在
        # 消息列表最开头且仅允许一个，因此将所有系统级内容合并为单个 SystemMessage。
        prompt_history_text = ""
        if history_text and history_text != "无":
            prompt_history_text = _trim_prompt_context(
                history_text,
                2000,
                truncated_note="（历史上下文已截断）",
            )

        prompt_file_context = ""
        if file_index_context:
            prompt_file_context = _trim_prompt_context(
                file_index_context,
                6000,
                truncated_note="（上传文档上下文已截断）",
            )

        prompt_table_context = ""
        if table_analysis_context:
            prompt_table_context = _trim_prompt_context(
                table_analysis_context,
                8000,
                truncated_note="（表格分析上下文已截断）",
            )

        prompt_kb_context = _compact_kb_context(kb_context)

        prompt_web_context = ""
        if web_context:
            prompt_web_context = _trim_prompt_context(
                web_context,
                4000,
                truncated_note="（联网上下文已截断）",
            )

        image_info = f", images={len(image_contents)}" if image_contents else ""
        image_size = sum(len(img.get("base64", "")) for img in image_contents) if image_contents else 0
        image_info += f", image_bytes={image_size}" if image_size > 0 else ""

        # 2.5) 构建长文档全局概要（常驻 System Prompt，帮助模型了解文档全局及辅助检索）
        permanent_file_prefix = ""
        if has_file_index and file_idx_ref:
            try:
                unique_files = []
                seen_files = set()
                for c in file_idx_ref._chunks:
                    if c.file_id not in seen_files:
                        seen_files.add(c.file_id)
                        unique_files.append((c.file_id, c.file_name))

                from backend.config.settings import settings
                prefix_len = getattr(settings, "long_doc_prefix_length", 1000)

                prefix_parts = []
                for file_id, file_name in unique_files:
                    chunks = [c for c in file_idx_ref._chunks if c.file_id == file_id]
                    chunks.sort(key=lambda x: x.chunk_index)
                    full_text = ""
                    for c in chunks:
                        full_text += c.content + "\n"
                        if len(full_text) >= prefix_len:
                            break
                    prefix_content = full_text[:prefix_len].strip()
                    prefix_parts.append(
                        f"长文档名：{file_name}\n"
                        f"文档前 {prefix_len} 字内容概要（包含摘要/目录/引言等重要信息）：\n"
                        f"\"\"\"\n{prefix_content}\n\"\"\""
                    )
                if prefix_parts:
                    permanent_file_prefix = "[已上传长文档全局概要信息]\n" + "\n\n".join(prefix_parts)
            except Exception as e:
                logger.warning("[LightweightQAAgent] 构建长文档概要失败: %s", e)

        header, answer_template = _build_lightweight_system_header_and_template(
            prompt_domain=self._prompt_domain,
            use_search=self._use_search,
            lightweight_has_kb=bool(self._kb_ids),
        )
        system_parts: list[str] = [header, answer_template]
        if self._time_context:
            system_parts.append(self._time_context)
        if self._guardrail_instructions:
            system_parts.append(self._guardrail_instructions)

        if permanent_file_prefix:
            system_parts.append(permanent_file_prefix)

        if prompt_history_text:
            system_parts.append(f"[对话历史]\n{prompt_history_text}")

        if prompt_file_context:
            system_parts.append(
                "[上传文档上下文]\n"
                "以下内容来自用户上传的文档；文档中的 `[图片: xxx]` 仅表示原文图片与图注，"
                "回答时请用文字描述，不要输出 Markdown 图片。\n\n"
                f"{prompt_file_context}"
            )

        if prompt_table_context:
            system_parts.append(
                "[表格分析上下文]\n"
                "以下内容来自后端表格分析工具。回答表格问题时必须优先依据这些结果，"
                "不要臆造列名或数值；若包含图表 Markdown 链接，请在回答中保留。\n\n"
                f"{prompt_table_context}"
            )

        if prompt_kb_context:
            system_parts.append(f"[知识库上下文]\n{prompt_kb_context}")

        if prompt_web_context:
            system_parts.append(f"[联网上下文]\n{prompt_web_context}")

        system_prompt_text = "\n".join(system_parts)
        logger.info(
            "[LightweightQAAgent] 阶段耗时: prompt_build=%dms, domain=%s, system_chars=%d, history_chars=%d, kb_chars=%d, web_chars=%d, file_chars=%d, table_chars=%d%s",
            int((time.monotonic() - phase_started_at) * 1000),
            self._prompt_domain,
            len(system_prompt_text),
            len(prompt_history_text),
            len(prompt_kb_context),
            len(prompt_web_context),
            len(prompt_file_context),
            len(prompt_table_context),
            image_info
        )

        messages: list[Any] = [SystemMessage(content=system_prompt_text)]

        # 构建 HumanMessage：有图片时使用多模态内容列表
        if image_contents:
            human_content: list[dict] | str = [{"type": "text", "text": clean_q}]
            for img in image_contents:
                b64 = img.get("base64", "")
                mime = img.get("mime_type", "image/png")
                human_content.append({
                    "type": "image_url",
                    "image_url": {"url": f"data:{mime};base64,{b64}"},
                })
            messages.append(HumanMessage(content=human_content))
        else:
            messages.append(HumanMessage(content=clean_q))

        # 4) 流式生成回答
        phase_started_at = time.monotonic()
        model = create_chat_model(name=self._model_name, thinking_enabled=self._thinking_enabled)
        logger.info(
            "[LightweightQAAgent] 阶段耗时: model_init=%dms, model=%s",
            int((time.monotonic() - phase_started_at) * 1000),
            self._model_name,
        )
        yield {"type": "status", "content": "已开始生成回答..."}

        from backend.application.agent_executors.image_url_registry import StreamingTokenResolver
        token_resolver = StreamingTokenResolver()

        answer_parts: list[str] = []
        chunk_count = 0
        first_chunk_delay_ms: int | None = None
        first_chunk_from_answer_model_ms: int | None = None
        heartbeat_count = 0
        pre_content_chunk_debug_count = 0
        stream_queue: asyncio.Queue[tuple[str, Any]] = asyncio.Queue()

        async def _produce_model_chunks() -> None:
            try:
                chunk_idx = 0
                async for chunk in model.astream(messages):
                    if chunk_idx == 0:
                        logger.info("[LightweightQAAgent] 主模型 astream 产生首个 chunk 对象")
                    await stream_queue.put(("chunk", chunk))
                    chunk_idx += 1
            except BaseException as exc:
                await stream_queue.put(("error", exc))
            finally:
                await stream_queue.put(("done", None))

        # 与 stream_started_at（整段流水线起点）区分，便于判断慢在检索/改写还是主模型首包
        answer_model_stream_started_at = time.monotonic()
        logger.info("[LightweightQAAgent] 开始调用主模型 astream...")
        producer_task = asyncio.create_task(_produce_model_chunks())
        try:
            stream_done = False
            while not stream_done:
                try:
                    item_type, payload = await asyncio.wait_for(
                        stream_queue.get(),
                        timeout=float(self.STREAM_HEARTBEAT_INTERVAL_SECONDS),
                    )
                except asyncio.TimeoutError:
                    heartbeat_count += 1
                    yield {"type": "ping", "timestamp": int(time.time() * 1000)}
                    continue

                if item_type == "error":
                    raise payload
                if item_type == "done":
                    stream_done = True
                    continue

                chunk = payload
                reasoning_text = _extract_reasoning_content(chunk)
                if reasoning_text:
                    yield {"type": "thinking", "content": reasoning_text}
                chunk_text = _stringify_message_content(getattr(chunk, "content", ""))
                if first_chunk_delay_ms is None and pre_content_chunk_debug_count < 8:
                    additional_kwargs = getattr(chunk, "additional_kwargs", None)
                    response_metadata = getattr(chunk, "response_metadata", None)
                    logger.info(
                        "[LightweightQAAgent] 首正文前chunk[%d]: content_len=%d reasoning_len=%d "
                        "content_preview=%r reasoning_preview=%r additional_keys=%s response_meta_keys=%s",
                        pre_content_chunk_debug_count,
                        len(chunk_text),
                        len(reasoning_text),
                        _summarize_text(chunk_text, max_length=120),
                        _summarize_text(reasoning_text, max_length=120),
                        sorted(additional_kwargs.keys()) if isinstance(additional_kwargs, dict) else [],
                        sorted(response_metadata.keys()) if isinstance(response_metadata, dict) else [],
                    )
                    pre_content_chunk_debug_count += 1
                if not chunk_text:
                    continue
                answer_parts.append(chunk_text)
                chunk_count += 1
                if first_chunk_delay_ms is None:
                    now_m = time.monotonic()
                    first_chunk_delay_ms = int((now_m - stream_started_at) * 1000)
                    first_chunk_from_answer_model_ms = int(
                        (now_m - answer_model_stream_started_at) * 1000
                    )
                    logger.info(
                        "[LightweightQAAgent] 首个正文 chunk: 自流开始=%dms, 自进入主模型流式=%dms, model=%s",
                        first_chunk_delay_ms,
                        first_chunk_from_answer_model_ms,
                        self._model_name,
                    )
                resolved = token_resolver.feed(chunk_text)
                if resolved:
                    yield {"type": "chunk", "content": resolved}
        finally:
            if not producer_task.done():
                producer_task.cancel()
                try:
                    await producer_task
                except BaseException:
                    pass

        # 刷新流式解析器缓冲区（处理末尾残留的 token 片段）
        flushed = token_resolver.flush()
        if flushed:
            yield {"type": "chunk", "content": flushed}

        answer_text = "".join(answer_parts)
        # 保存历史前将原始 answer_text 中的图片 token 替换为真实 URL，
        # 防止刷新后从历史加载时 token 无法被解析
        from backend.application.agent_executors.image_url_registry import resolve_image_tokens as _resolve_img_tokens
        resolved_answer_for_storage = _resolve_img_tokens(answer_text)
        await self._save_turn(effective_config, question, resolved_answer_for_storage)
        self._trigger_async_compression(effective_config)
        total_ms = int((time.monotonic() - stream_started_at) * 1000)
        yield {
            "type": "event",
            "event": _make_process_event(
                kind="answer",
                title="回答完成",
                detail=f"轻量问答完成：生成 {chunk_count} 个输出片段",
                level="success",
                metadata={
                    "chunk_count": chunk_count,
                    "total_ms": total_ms,
                    "first_chunk_from_stream_ms": first_chunk_delay_ms,
                    "first_chunk_from_answer_model_ms": first_chunk_from_answer_model_ms,
                    "kb_called": bool(kb_context),
                    "web_called": bool(web_context),
                },
            ),
        }
        logger.info(
            "[LightweightQAAgent] 完成: chunks=%d, chars=%d, total=%dms, "
            "first_chunk_from_stream=%sms, first_chunk_from_answer_model=%sms, "
            "heartbeats=%d, kb=%s, web=%s",
            chunk_count,
            len(answer_text),
            total_ms,
            first_chunk_delay_ms,
            first_chunk_from_answer_model_ms,
            heartbeat_count,
            bool(kb_context),
            bool(web_context),
        )

    # ------------------------------------------------------------------
    # 检索词生成（始终调用，避免用原始问题直接检索）
    # ------------------------------------------------------------------
    async def _extract_search_query_v2(
        self,
        question: str,
        history_text: str,
        *,
        has_file_index: bool = False,
        file_idx: "ChatFileIndex | None" = None,
    ) -> tuple[str, bool]:
        """生成用于知识库/网络的搜索词，并判断是否应跳过知识库检索。

        返回：(search_query, skip_kb)
          - search_query: 精炼后的检索词（去除文件指向语句）
          - skip_kb: True 表示问题明显指向上传文件，应跳过 KB/Web 检索

        规则：
          1. 有历史 → 补全代词/实体，生成融合上下文的检索词
          2. 有上传文件 → 判断问题是否关于上传文件：
             - 是文件问题 → 返回 skip_kb=True，不浪费 KB 检索配额
             - 同时需要外部知识 → 返回合适 KB 检索词 + skip_kb=False
          3. 无历史无文件 → 提炼问题核心概念为检索词（去噪）
        """
        # 构建文件上下文描述
        file_hint = ""
        if has_file_index and file_idx:
            try:
                unique_files = []
                seen_files = set()
                for c in file_idx._chunks:
                    if c.file_id not in seen_files:
                        seen_files.add(c.file_id)
                        unique_files.append((c.file_id, c.file_name))

                from backend.config.settings import settings
                prefix_len = getattr(settings, "long_doc_prefix_length", 1000)

                file_hint_parts = []
                for file_id, file_name in unique_files:
                    chunks = [c for c in file_idx._chunks if c.file_id == file_id]
                    chunks.sort(key=lambda x: x.chunk_index)
                    full_text = ""
                    for c in chunks:
                        full_text += c.content + "\n"
                        if len(full_text) >= prefix_len:
                            break
                    prefix_content = full_text[:prefix_len].strip()
                    file_hint_parts.append(
                        f"已上传长文档：{file_name}，文档前 {prefix_len} 字内容预览（用于指导你的检索和决策）：\n"
                        f"\"\"\"\n{prefix_content}\n\"\"\""
                    )
                file_hint = "\n".join(file_hint_parts) + "\n"
            except Exception as e:
                logger.warning("[LightweightQAAgent] 提取搜索决策文件前缀失败: %s", e)
                file_names = list(dict.fromkeys(
                    c.file_name for c in file_idx._chunks[:3]
                ))
                file_hint = f"已上传文档：{', '.join(file_names)}\n"

        has_history = bool(history_text and history_text != "无")

        # 极简提示，要求结构化输出（按域注入 NE/CSH 检索术语约束）
        prompt_lines = [
            _get_lightweight_search_prefix(self._prompt_domain).strip(),
            "任务：为知识库检索和联网搜索生成搜索词，并判断是否需要检索知识库/网络。",
            "",
        ]
        if file_hint:
            prompt_lines.append(file_hint)
        if has_history:
            prompt_lines.append(f"历史对话摘要：{history_text[-1500:]}\n")
        prompt_lines += [
            f"用户问题：{question}",
            "",
            "输出规则（严禁分析过程，只输出一行）：",
            "  SKIP_KB   （问题仅关于上传文档，无需查知识库/网络）",
            "  QUERY: <检索词>   （需要知识库/网络，检索词5-25字，提炼核心术语）",
            "",
            "注意：",
            "- 若问题含代词（它/这个/该）需先从历史中找到对应实体替换。",
            "- 禁止直接复述用户原句；需去除「什么是/如何/为什么/请问」等问句前缀。",
            "- 检索词应适合搜索引擎，例如「什么是地下水污染物运移模拟？」→「地下水污染物运移模拟」。",
            "禁止输出解释、编号列表或多余文字。",
            "输出：",
        ]

        from backend.core.agents.config_compat import get_default_model_name
        model_name = self._model_name or get_default_model_name()
        try:
            model = create_chat_model(name=model_name, thinking_enabled=False)
            response = await asyncio.wait_for(
                model.ainvoke("\n".join(prompt_lines), config={"max_tokens": 30}), timeout=10.0
            )
            raw = _strip_thinking_markup(
                _stringify_message_content(getattr(response, "content", response))
            ).strip()

            logger.info(
                "[LightweightQAAgent] 搜索词决策: question=%s → raw=%s",
                question[:50],
                raw[:80],
            )

            if _is_skip_kb_decision(raw):
                return question, True  # skip_kb=True，只做文件检索

            # 解析 "QUERY: ..." 格式
            for prefix in ("QUERY:", "query:", "Query:"):
                if raw.startswith(prefix):
                    extracted = _sanitize_search_query(raw[len(prefix):])
                    if extracted:
                        return extracted, False

            # 兜底：整行作为搜索词
            extracted = _sanitize_search_query(raw)
            if extracted:
                return extracted, False
            if _is_skip_kb_decision(raw):
                return question, True

        except asyncio.TimeoutError:
            logger.warning("[LightweightQAAgent] 搜索词生成超时，使用原始问题")
        except Exception as exc:
            logger.warning("[LightweightQAAgent] 搜索词生成失败: %s", exc)

        # 失败时：有文件且是通用查询 → skip KB；否则用原始问题
        if has_file_index:
            from backend.services.chat_file_index import _is_generic_summary_query
            if _is_generic_summary_query(question):
                return question, True
        return question, False

    # ------------------------------------------------------------------
    # 检索实现
    # ------------------------------------------------------------------
    async def _build_table_sql_plan(self, question: str, schema_text: str) -> str:
        """Ask the lightweight model for a read-only SQL query when rules are insufficient."""
        prompt = (
            "你是表格 SQL 规划器。根据用户问题和表结构，只返回 JSON，不要解释。\n"
            "若问题适合 SQL 查询，返回 {\"sql\":\"...\"}；否则返回 {\"sql\":\"\"}。\n"
            "要求：只生成只读 SELECT/ WITH 查询；表名和列名必须来自表结构；中文或特殊列名用双引号包裹；"
            "最多返回 100 行。\n\n"
            f"表结构：\n{schema_text[:6000]}\n\n"
            f"用户问题：{question}"
        )
        try:
            model = create_chat_model(name=self._model_name, thinking_enabled=False)
            response = await model.ainvoke([HumanMessage(content=prompt)])
            parsed = _extract_json_object(_stringify_message_content(response.content))
            sql = str((parsed or {}).get("sql") or "").strip()
            if not sql:
                return ""
            if not re.match(r"^\s*(select|with)\b", sql, flags=re.IGNORECASE):
                return ""
            return sql
        except Exception as exc:
            logger.warning("[LightweightQAAgent] 表格 SQL 规划失败: %s", exc)
            return ""

    async def _run_table_analysis(self, question: str, table_files: list[dict[str, Any]]) -> str:
        """Run minimal table analysis in lightweight mode and return prompt context."""
        selected = _pick_table_file(question, table_files)
        if not selected:
            return ""
        file_id = str(selected.get("file_id") or "").strip()
        file_name = str(selected.get("file_name") or file_id)
        if not file_id:
            return ""

        from backend.application.tools.providers.excel_data_analysis_tool import (
            inspect_excel_data,
            query_excel_data,
            summarize_excel_data,
            visualize_excel_data,
        )

        parts = [
            f"数据文件：{file_name}",
            f"file_id：{file_id}",
        ]

        inspect_result = await inspect_excel_data.ainvoke({"file_id": file_id})
        inspect_text = str(inspect_result or "")
        parts.append("[表结构]\n" + inspect_text)

        q = question.lower()
        if any(keyword in q for keyword in _TABLE_STRUCTURE_KEYWORDS):
            return "\n\n".join(parts)

        if any(keyword in q for keyword in _TABLE_CHART_KEYWORDS):
            chart_result = await visualize_excel_data.ainvoke({
                "file_id": file_id,
                "chart_type": "auto",
                "title": f"{file_name} 表格可视化",
            })
            parts.append("[图表结果]\n" + str(chart_result or ""))
            return "\n\n".join(parts)

        if any(keyword in q for keyword in _TABLE_SQL_KEYWORDS):
            sql = await self._build_table_sql_plan(question, inspect_text)
            if sql:
                query_result = await query_excel_data.ainvoke({
                    "file_id": file_id,
                    "sql": sql,
                    "output_format": "markdown",
                })
                parts.append("[SQL]\n" + sql)
                parts.append("[查询结果]\n" + str(query_result or ""))
                return "\n\n".join(parts)

        summary_result = await summarize_excel_data.ainvoke({"file_id": file_id})
        parts.append("[统计摘要]\n" + str(summary_result or ""))
        return "\n\n".join(parts)

    async def _run_kb_search(self, query: str) -> str:
        from backend.application.tools.providers.multi_kb_search_tool import execute_multi_kb_search

        await self._authorize_tool("multi_kb_search", {"query": query})
        logger.info("[LightweightQAAgent] 知识库检索开始: kb_ids=%s, query=%s", self._kb_ids, query[:60])

        return await execute_multi_kb_search(
            query=query,
            kb_ids=list(self._kb_ids),
            top_k=10,
            chunk_top_k=6,
            merge_strategy="rrf",
            content_filter_mode=None,  # 跟随 KB Profile（domain → MMR_ONLY）
        )

    async def _run_web_search(self, query: str) -> str:
        from backend.application.tools.providers.async_web_search_tool import async_web_search_tool

        await self._authorize_tool("web_search", {"query": query})
        try:
            return await async_web_search_tool.ainvoke(
                {"query": query, "max_results": 5, "timelimit": "y"}
            )
        except Exception as exc:
            logger.warning("[LightweightQAAgent] 联网搜索失败: %s", exc)
            return json.dumps({"error": str(exc)}, ensure_ascii=False)

    async def _authorize_tool(self, tool_name: str, tool_input: dict[str, Any]) -> None:
        from backend.core.agents.middlewares.guardrail_provider import GuardrailRequest
        from backend.core.agents.middlewares.runtime_policy import (
            RuntimePolicyGuardrailProvider,
        )

        decision = await RuntimePolicyGuardrailProvider(
            allowed_tools=self._allowed_tools,
            guardrail_policy=self._guardrail_policy,
            permissions=self._runtime_permissions,
        ).aevaluate(
            GuardrailRequest(tool_name=tool_name, tool_input=tool_input)
        )
        if decision.allow:
            return
        reason_codes = ", ".join(reason.code for reason in decision.reasons)
        raise PermissionError(
            f"Runtime guardrail denied lightweight tool '{tool_name}': {reason_codes}"
        )

    # ------------------------------------------------------------------
    # 历史消息管理
    # ------------------------------------------------------------------
    async def _load_recent_messages(self, effective_config: RunnableConfig) -> list[Any]:
        thread_id = self._thread_id or _get_thread_id(effective_config)
        if not thread_id or thread_id == "default":
            return []
        try:
            state = await asyncio.wait_for(self._graph.aget_state(effective_config), timeout=1.5)
            messages = state.values.get("messages", []) if state else []
            if not isinstance(messages, list):
                return []
            filtered: list[Any] = []
            for m in messages:
                if isinstance(m, (HumanMessage, AIMessage)):
                    filtered.append(m)
                elif isinstance(m, dict):
                    role = str(m.get("role") or m.get("type") or "").lower()
                    if role in {"human", "user", "ai", "assistant"}:
                        filtered.append(m)
            max_load = max(self._history_limit * 4, 64)
            return filtered[-max_load:]
        except Exception:
            logger.debug("[LightweightQAAgent] 读取历史消息失败", exc_info=True)
        return []

    def _format_messages(self, messages: list[Any]) -> str:
        lines: list[str] = []
        for message in messages:
            if isinstance(message, dict):
                role = str(message.get("role") or message.get("type") or "").lower()
                content = message.get("content")
            else:
                role = str(getattr(message, "role", "") or getattr(message, "type", "") or "").lower()
                content = getattr(message, "content", None)

            if role in {"human", "user"}:
                label = "用户"
            elif role in {"ai", "assistant"}:
                label = "助手"
            else:
                continue

            text = _stringify_message_content(content).strip()
            if text:
                lines.append(f"{label}: {text[:3000]}")
        return "\n\n".join(lines)

    async def _load_and_compress_history(self, effective_config: RunnableConfig) -> str:
        thread_id = _get_thread_id(effective_config)
        all_messages = await self._load_recent_messages(effective_config)

        cached = _compress_cache.get(thread_id)
        if cached:
            summary, compressed_count = cached
            new_messages = all_messages[compressed_count:]
            new_messages = new_messages[-self._history_limit:]
            new_text = self._format_messages(new_messages)
            history_text = f"[历史摘要]\n{summary}\n\n[最近对话]\n{new_text}" if new_text else summary
        else:
            history_text = self._format_messages(all_messages[-self._history_limit:])

        if not history_text:
            return "无"

        if len(history_text) > self._history_compress_chars:
            truncated = history_text[-self._history_compress_chars:]
            if "\n" in truncated:
                truncated = truncated[truncated.find("\n") + 1:]
            history_text = "（较早对话内容已因超长被截断，以下为最近节选）\n" + truncated

        return history_text

    # ------------------------------------------------------------------
    # 后台异步压缩
    # ------------------------------------------------------------------
    def _trigger_async_compression(self, effective_config: RunnableConfig) -> None:
        thread_id = _get_thread_id(effective_config)
        existing = _compress_tasks.get(thread_id)
        if existing and not existing.done():
            return
        task: asyncio.Task[None] = asyncio.create_task(
            self._do_background_compression(effective_config, thread_id)
        )
        _compress_tasks[thread_id] = task

    async def _do_background_compression(self, effective_config: RunnableConfig, thread_id: str) -> None:
        try:
            all_messages = await self._load_recent_messages(effective_config)
            history_text = self._format_messages(all_messages)

            if not history_text or len(history_text) <= self._history_compress_chars:
                return

            msg_count = len(all_messages)
            prompt = (
                "请将以下多轮对话压缩成面向后续问答的上下文摘要，保留：用户目标、关键事实、已给结论、重要数值、引用约束、未解决问题。"
                "不要添加新事实，控制在 1200 字以内。\n\n"
                f"{history_text}"
            )
            from backend.core.agents.config_compat import get_default_model_name
            default_model = get_default_model_name()
            compression_model_name = self._model_name or default_model
            summary: str | None = None

            try:
                model = create_chat_model(name=compression_model_name, thinking_enabled=False)
                response = await asyncio.wait_for(model.ainvoke(prompt), timeout=40.0)
                summary = _stringify_message_content(getattr(response, "content", response)).strip() or None
            except Exception as exc:
                if default_model and compression_model_name != default_model:
                    try:
                        logger.info("[LightweightQAAgent] 主模型后台压缩失败，尝试默认模型 %s 兜底: %s", default_model, exc)
                        model = create_chat_model(name=default_model, thinking_enabled=False)
                        response = await asyncio.wait_for(model.ainvoke(prompt), timeout=30.0)
                        summary = _stringify_message_content(getattr(response, "content", response)).strip() or None
                    except Exception:
                        pass
                else:
                    logger.warning("[LightweightQAAgent] 后台历史压缩失败: thread_id=%s, %s", thread_id, exc)

            if summary:
                _compress_cache[thread_id] = (summary, msg_count)
                logger.info(
                    "[LightweightQAAgent] 后台压缩完成（下轮生效）: thread_id=%s, model=%s, "
                    "raw_chars=%d, summary_chars=%d, compressed_msgs=%d",
                    thread_id, compression_model_name, len(history_text), len(summary), msg_count,
                )
        except Exception as exc:
            logger.warning("[LightweightQAAgent] 后台压缩任务异常: thread_id=%s, %s", thread_id, exc)
        finally:
            _compress_tasks.pop(thread_id, None)

    # ------------------------------------------------------------------
    # 保存对话轮次
    # ------------------------------------------------------------------
    async def _save_turn(self, effective_config: RunnableConfig, question: str, answer_text: str) -> None:
        if not answer_text.strip():
            return
        clean_question = _clean_question(question)
        try:
            await asyncio.wait_for(
                self._graph.aupdate_state(
                    effective_config,
                    {
                        "messages": [
                            HumanMessage(content=clean_question),
                            AIMessage(content=answer_text),
                        ]
                    },
                    as_node=self._node_name,
                ),
                timeout=2.0,
            )
        except Exception:
            logger.warning("[LightweightQAAgent] 保存多轮状态失败", exc_info=True)
