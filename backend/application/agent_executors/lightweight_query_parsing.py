"""轻量问答查询清洗、检索词/文件名解析与表格意图判定(纯函数)。"""

from __future__ import annotations

import logging
import asyncio
import json
import re
import time
import uuid

from backend.application.agents.prompt_registry import PromptTemplateRegistry
from backend.config.log_privacy import text_fingerprint
from backend.core.agents.middlewares.runtime_budget import (
    charge_agent_model_response,
    check_agent_runtime_budget,
)
from backend.core.models import create_chat_model_async
from typing import Any
from backend.application.agent_executors.lightweight_prompts import (
    PromptDomain,
    _get_lightweight_file_search_prefix,
)

from typing import TYPE_CHECKING

if TYPE_CHECKING:
    from backend.services.chat_file_index import ChatFileIndex


logger = logging.getLogger(__name__)


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
    event_id = str(
        uuid.uuid5(uuid.NAMESPACE_DNS, f"lightweight:{kind}:{title}:{detail}:{tool}")
    )
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
        reasoning = additional_kwargs.get("reasoning_content") or additional_kwargs.get(
            "reasoning"
        )
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
    cleaned = re.sub(
        r"<(?:think|thought)>[\s\S]*?</(?:think|thought)>",
        "",
        text,
        flags=re.IGNORECASE,
    )
    cleaned = re.sub(r"<(?:think|thought)>[\s\S]*$", "", cleaned, flags=re.IGNORECASE)
    return cleaned.strip()


def _clean_question(text: str) -> str:
    """清理问题文本，去掉注入的运行时元数据与文档摘要预览，仅保留用户原始问题。"""
    if not text:
        return ""
    # 去掉时间上下文标签
    cleaned = re.sub(
        r"<runtime_current_time>[\s\S]*?</runtime_current_time>",
        "",
        text,
        flags=re.IGNORECASE,
    )
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
    cleaned = re.sub(
        r"<uploaded_document[^>]*>[\s\S]*?</uploaded_document>",
        "",
        cleaned,
        flags=re.IGNORECASE,
    )
    # 去掉长文档预览内容前缀
    cleaned = re.sub(
        r"已上传长文档[：:][^\n]+\n文档前 \d+ 字内容概要[^\n]*\n\"\"\"[\s\S]*?\"\"\"",
        "",
        cleaned,
        flags=re.IGNORECASE,
    )
    # 去掉科研规划标签
    cleaned = re.sub(
        r"\[ConfirmedResearchPlan\][\s\S]*?\[/ConfirmedResearchPlan\]",
        "",
        cleaned,
        flags=re.IGNORECASE,
    )
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
    cleaned = re.sub(
        r"<(?:think|thought)>[\s\S]*?</(?:think|thought)>",
        "",
        text or "",
        flags=re.IGNORECASE,
    )
    cleaned = re.sub(r"<(?:think|thought)>[\s\S]*$", "", cleaned, flags=re.IGNORECASE)
    cleaned = re.sub(r"^```(?:\w+)?\s*|\s*```$", "", cleaned.strip())
    cleaned = re.sub(r'["\'""' "]", "", cleaned)
    cleaned = re.sub(
        r"^(?:搜索词|检索词|query|search query)\s*[:：]\s*",
        "",
        cleaned,
        flags=re.IGNORECASE,
    )
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


def _kb_result_warning(kb_context: str) -> str | None:
    """Return a warning when retrieval produced no usable evidence."""

    text = str(kb_context or "").strip()
    warning_markers = (
        "本轮知识库检索上下文已达上限",
        "本轮剩余检索上下文预算不足",
        "知识库检索次数已达上限",
        "本次请求的知识库检索已取消",
        "没有可用的默认知识库",
        "没有可用的所选知识库",
        "搜索超时，请稍后重试",
    )
    if any(marker in text for marker in warning_markers):
        return " ".join(text.split())[:500]
    return None


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
            search_query = _sanitize_search_query(stripped[len("QUERY:") :])
            in_keywords = False
            continue
        if upper.startswith("KEYWORDS:") or stripped in {"关键词:", "关键词："}:
            in_keywords = True
            continue

        if not in_keywords and not search_query:
            for prefix in ("QUERY:", "query:", "Query:", "检索词:", "检索词："):
                if stripped.startswith(prefix):
                    search_query = _sanitize_search_query(stripped[len(prefix) :])
                    break
            if search_query:
                continue

        in_keywords = True
        kw = re.sub(r"^[\d\.\-\*\s]+", "", stripped).strip().strip(".,;：:")
        kw = re.sub(r'["\'""' "]", "", kw)
        if kw and len(kw) > 1 and kw.upper() != _SKIP_KB_TOKEN:
            keywords.append(kw)

    return search_query, keywords[:8]


_TABLE_ANALYSIS_KEYWORDS = (
    "表格",
    "excel",
    "xlsx",
    "xls",
    "csv",
    "sheet",
    "工作表",
    "列",
    "字段",
    "统计",
    "汇总",
    "分组",
    "筛选",
    "排序",
    "top",
    "最大",
    "最小",
    "平均",
    "均值",
    "总和",
    "求和",
    "缺失",
    "相关",
    "趋势",
    "图表",
    "画图",
    "可视化",
    "柱状图",
    "折线图",
    "散点图",
    "热力图",
)


_TABLE_GENERIC_ANALYSIS_KEYWORDS = (
    "分析一下",
    "总结一下",
    "看一下",
    "概括一下",
    "有什么结论",
)


_TABLE_STRUCTURE_KEYWORDS = (
    "有哪些列",
    "什么列",
    "列名",
    "字段",
    "sheet",
    "工作表",
    "结构",
)


_TABLE_CHART_KEYWORDS = (
    "画图",
    "图表",
    "可视化",
    "趋势图",
    "柱状图",
    "折线图",
    "散点图",
    "热力图",
)


_TABLE_SQL_KEYWORDS = (
    "分组",
    "筛选",
    "排序",
    "top",
    "最大",
    "最小",
    "平均",
    "均值",
    "总和",
    "求和",
    "统计",
)


def _is_table_analysis_query(question: str, table_files: list[dict[str, Any]]) -> bool:
    if not table_files:
        return False
    q = str(question or "").strip().lower()
    if not q:
        return False
    if any(keyword in q for keyword in _TABLE_ANALYSIS_KEYWORDS):
        return True
    return any(keyword in q for keyword in _TABLE_GENERIC_ANALYSIS_KEYWORDS)


def _pick_table_file(
    question: str, table_files: list[dict[str, Any]]
) -> dict[str, Any] | None:
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
    file_idx: "ChatFileIndex",
    model_name: str | None,
    timeout: float = 8.0,
    *,
    prompt_domain: PromptDomain | None = None,
    prompt_profile: str | None = None,
    domain: str | None = None,
    prompt_registry: PromptTemplateRegistry | None = None,
    file_search_prefix: str | None = None,
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

    effective_file_search_prefix = str(file_search_prefix or "").strip()
    if not effective_file_search_prefix:
        effective_file_search_prefix = _get_lightweight_file_search_prefix(
            prompt_domain,
            prompt_profile=prompt_profile,
            domain=domain,
            prompt_registry=prompt_registry,
        ).strip()
    prompt_lines = [
        effective_file_search_prefix,
        "任务：根据用户问题，生成在上传文档中检索用的查询语句和关键词。",
        f"文件名：{file_hint}",
    ]
    if prefix_preview:
        prompt_lines.append(
            f'文档开头预览（用于判断文档语言与主题）：\n"""\n{prefix_preview}\n"""'
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
        model = await create_chat_model_async(
            name=model_name or "MiniMax-M2.7", thinking_enabled=False
        )
        await check_agent_runtime_budget()
        response = await asyncio.wait_for(
            model.ainvoke("\n".join(prompt_lines), config={"max_tokens": 120}),
            timeout=timeout,
        )
        await charge_agent_model_response(response)
        raw = _stringify_message_content(getattr(response, "content", response))
        search_query, keywords = _parse_file_search_terms(raw)
        if not search_query:
            search_query = fallback_query

        logger.info(
            "[_extract_file_search_terms] question_meta=%s query_meta=%s keyword_count=%d",
            text_fingerprint(fallback_query),
            text_fingerprint(search_query),
            len(keywords),
        )
        return search_query, keywords
    except asyncio.TimeoutError:
        logger.warning(
            "[_extract_file_search_terms] 检索词生成超时，回退到规则提取关键词"
        )
    except Exception as exc:
        logger.warning("[_extract_file_search_terms] 检索词生成失败: %s", exc)

    # 2. 兜底逻辑：如果模型生成超时或失败，通过正则分词提取核心的高价值概念作为关键词，避免直接使用原句进行 BM25 检索无结果
    fallback_kws = []
    # 提取汉字连续串(不含标点)与英文单词作为关键词候选，并过滤高频无意义词
    stop_words = {
        "是什么",
        "介绍",
        "什么是",
        "怎么",
        "如何",
        "为什么",
        "什么",
        "的",
        "了",
        "在",
        "是",
        "有",
        "关于",
        "对于",
        "如何",
        "讲了什么",
        "总结",
        "概述",
        "分析",
        "论文",
        "文档",
        "文章",
        "报告",
        "吗",
        "呢",
        "吧",
        "呀",
        "啊",
        "谁",
        "哪里",
        "哪个",
        "怎么做",
        "怎样",
        "请问",
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
