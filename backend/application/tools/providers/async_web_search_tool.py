"""异步 Web 搜索工具 - 必应中国国内搜索

通过 ddgs 调用必应（backend=bing, region=cn-zh）。
"""

import asyncio
import json
import logging
import re
from typing import Any

from langchain.tools import tool

logger = logging.getLogger(__name__)

BING_BACKEND = "bing"
SEARCH_REGION = "cn-zh"
DEFAULT_TIMEOUT = 20
MAX_RESULTS = 5


def _sanitize_search_query(query: str) -> str:
    cleaned = re.sub(r"<runtime_current_time>[\s\S]*?</runtime_current_time>", "", query or "", flags=re.IGNORECASE)
    cleaned = re.sub(r"<(?:think|thought)>[\s\S]*?</(?:think|thought)>", "", cleaned, flags=re.IGNORECASE)
    cleaned = re.sub(r"<(?:think|thought)>[\s\S]*$", "", cleaned, flags=re.IGNORECASE)
    cleaned = re.sub(r"^```(?:\w+)?\s*|\s*```$", "", cleaned.strip())
    cleaned = re.sub(r'["\'""'']', "", cleaned)
    cleaned = re.sub(r"^(?:搜索词|检索词|query|search query)\s*[:：]\s*", "", cleaned, flags=re.IGNORECASE)
    lines = [line.strip(" \t-:：") for line in cleaned.splitlines() if line.strip()]
    return (lines[-1] if lines else "").strip()


async def _search_bing_async(
    query: str,
    max_results: int = 5,
    region: str = SEARCH_REGION,
    safesearch: str = "moderate",
    timeout: int = DEFAULT_TIMEOUT,
    timelimit: str | None = "y",
) -> list[dict]:
    """必应国内搜索（ddgs backend=bing, region=cn-zh）。"""
    try:
        from ddgs import DDGS
    except ImportError:
        logger.error("ddgs library not installed. Run: pip install ddgs")
        return []

    def _sync_search():
        ddgs = DDGS(timeout=timeout)
        try:
            results = ddgs.text(
                query,
                region=region,
                safesearch=safesearch,
                max_results=max_results,
                backend=BING_BACKEND,
                timelimit=timelimit,
            )
            return list(results) if results else []
        except Exception as e:
            logger.error("必应搜索失败: %s", e)
            return []

    try:
        raw = await asyncio.wait_for(
            asyncio.to_thread(_sync_search),
            timeout=timeout + 5,
        )
    except asyncio.TimeoutError:
        logger.warning("必应搜索超时 (>%ss): %s", timeout, query)
        return []
    except Exception as e:
        logger.error("必应搜索异常: %s", e)
        return []

    return [
        {
            "title": r.get("title", ""),
            "url": r.get("href", r.get("link", "")),
            "content": r.get("body", r.get("snippet", "")),
        }
        for r in raw
    ]


async def execute_web_search(
    query: str,
    *,
    max_results: int = MAX_RESULTS,
    timelimit: str | None = "y",
) -> str:
    """Execute web search for workflow orchestration without tool-call indirection."""
    sanitized_query = _sanitize_search_query(query)
    if not sanitized_query:
        return json.dumps({"error": "Empty search query", "query": ""}, ensure_ascii=False)

    bounded_results = max(1, min(max_results, MAX_RESULTS))
    logger.info("[AsyncWebSearch] 编排层开始搜索: %s", sanitized_query)
    results = await _search_bing_async(
        query=sanitized_query,
        max_results=bounded_results,
        timelimit=timelimit,
    )
    if not results:
        return json.dumps(
            {"error": "No results found or search timeout", "query": sanitized_query},
            ensure_ascii=False,
        )
    return json.dumps(
        {
            "query": sanitized_query,
            "engine": "bing(cn-zh)",
            "total_results": len(results),
            "results": results,
        },
        indent=2,
        ensure_ascii=False,
    )


@tool("web_search", parse_docstring=True)
async def async_web_search_tool(
    query: str,
    max_results: int = MAX_RESULTS,
    timelimit: str | None = "y",
) -> str:
    """Search the web for information. Use this tool to find current information, news, articles, and facts from the internet.

    Args:
        query: Search keywords describing what you want to find. Be specific for better results. When asked for "latest" or recent information, ALWAYS include the current year (e.g., "2026") in the query string, do NOT use "2024" or "2025".
        max_results: Maximum number of results to return. Default is 5.
        timelimit: Time range limit for search. Options: 'd' (day), 'w' (week), 'm' (month), 'y' (year).
                   Defaults to 'y' (recent year) if not specified.
    """
    from backend.core.tools.request_limits import consume_tool_budget

    allowed, _remaining = consume_tool_budget("web_search")
    if not allowed:
        logger.warning("[AsyncWebSearch] 尝试调用被拦截，预算为0")
        return json.dumps(
            {"error": f"⚠️ web_search 调用次数已达上限（剩余: {_remaining}）。请基于已有检索结果回答，或结束本轮对话。"},
            ensure_ascii=False,
        )

    return await execute_web_search(
        query,
        max_results=max_results,
        timelimit=timelimit,
    )


def get_async_web_search_tool() -> Any:
    """获取异步 web_search 工具实例"""
    return async_web_search_tool
