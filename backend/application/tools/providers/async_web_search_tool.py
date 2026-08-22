"""异步 Web 搜索工具。

搜索词由调用该工具的智能体自主决定；本模块只负责执行单次检索。
"""

import asyncio
import json
import logging
import os
import re
import threading
from typing import Any
from weakref import WeakKeyDictionary

import httpx
from langchain.tools import tool

from backend.config.log_privacy import text_fingerprint

logger = logging.getLogger(__name__)

SEARCH_PROVIDER = "firecrawl"
DEFAULT_TIMEOUT = 20
MAX_RESULTS = 5
SEARCH_ATTEMPTS = 2
FIRECRAWL_API_URL = os.getenv(
    "FIRECRAWL_API_URL",
    "http://127.0.0.1:3002",
).strip()
FIRECRAWL_API_KEY = os.getenv("FIRECRAWL_API_KEY", "").strip()
FIRECRAWL_LOCATION = os.getenv("FIRECRAWL_SEARCH_LOCATION", "China").strip()
FIRECRAWL_RESULT_CONTENT_CHARS = max(
    500,
    int(os.getenv("FIRECRAWL_RESULT_CONTENT_CHARS", "2000") or "2000"),
)
FIRECRAWL_SEARCH_CONCURRENCY = 3
MAX_SEARCH_QUERY_CHARS = 2000
WEB_SEARCH_MIN_RELEVANCE = float(
    os.getenv("WEB_SEARCH_MIN_RELEVANCE", "0.02") or "0.02"
)
WEB_SEARCH_NO_PROGRESS_LIMIT = max(
    1,
    int(os.getenv("WEB_SEARCH_NO_PROGRESS_LIMIT", "2") or "2"),
)
_RELEVANCE_CONTENT_CHARS = 1200
_SEARCH_LIMITERS_LOCK = threading.Lock()
_SEARCH_LIMITERS: WeakKeyDictionary[
    asyncio.AbstractEventLoop,
    dict[str, asyncio.Semaphore],
] = WeakKeyDictionary()


def _firecrawl_search_limiter() -> asyncio.Semaphore:
    loop = asyncio.get_running_loop()
    with _SEARCH_LIMITERS_LOCK:
        loop_limiters = _SEARCH_LIMITERS.get(loop)
        if loop_limiters is None:
            loop_limiters = {}
            _SEARCH_LIMITERS[loop] = loop_limiters
        limiter = loop_limiters.get(SEARCH_PROVIDER)
        if limiter is None:
            limiter = asyncio.Semaphore(FIRECRAWL_SEARCH_CONCURRENCY)
            loop_limiters[SEARCH_PROVIDER] = limiter
        return limiter


def _sanitize_search_query(query: str) -> str:
    cleaned = re.sub(
        r"<runtime_current_time>[\s\S]*?</runtime_current_time>",
        "",
        query or "",
        flags=re.IGNORECASE,
    )
    cleaned = re.sub(
        r"<(?:think|thought)>[\s\S]*?</(?:think|thought)>",
        "",
        cleaned,
        flags=re.IGNORECASE,
    )
    cleaned = re.sub(r"<(?:think|thought)>[\s\S]*$", "", cleaned, flags=re.IGNORECASE)
    cleaned = re.sub(r"^```(?:\w+)?\s*|\s*```$", "", cleaned.strip())
    cleaned = re.sub(r'["\'“”‘’]', "", cleaned)
    cleaned = re.sub(
        r"^(?:搜索词|检索词|query|search query)\s*[:：]\s*",
        "",
        cleaned,
        flags=re.IGNORECASE,
    )
    lines = [line.strip(" \t-:：") for line in cleaned.splitlines() if line.strip()]
    return (lines[-1] if lines else "").strip()


def _filter_relevant_results(
    query: str,
    results: list[dict[str, str]],
    *,
    min_relevance: float = WEB_SEARCH_MIN_RELEVANCE,
) -> tuple[list[dict[str, str]], int]:
    """Drop results with no meaningful term overlap with the query.

    Firecrawl can return hot-topic/dictionary/off-topic pages that are
    unrelated to a research intent (observed as 虎牙/网络用语/词典 pages).
    The filter reuses the ledger's CJK-aware n-gram overlap so a page must
    share at least one substantive term with the query to enter context.
    """

    from backend.core.agents.context.ledger import _token_overlap

    kept: list[dict[str, str]] = []
    for result in results:
        title = str(result.get("title") or "")
        url = str(result.get("url") or "")
        content = str(result.get("content") or "")[:_RELEVANCE_CONTENT_CHARS]
        score = max(
            _token_overlap(str(query or ""), f"{title} {url}"),
            _token_overlap(str(query or ""), content),
        )
        if score >= max(0.0, float(min_relevance)):
            kept.append(result)
    return kept, max(0, len(results) - len(kept))


def _firecrawl_search_endpoint() -> str:
    base_url = FIRECRAWL_API_URL.rstrip("/")
    if base_url.endswith("/v2"):
        return f"{base_url}/search"
    return f"{base_url}/v2/search"


def _firecrawl_tbs(timelimit: str | None) -> str | None:
    if timelimit in {"d", "w", "m", "y"}:
        return f"qdr:{timelimit}"
    return None


async def _search_firecrawl_async(
    query: str,
    *,
    max_results: int,
    timeout: int,
    timelimit: str | None,
) -> list[dict[str, str]]:
    """Search and scrape result pages through the configured Firecrawl service."""

    payload: dict[str, Any] = {
        "query": query,
        "limit": max_results,
        "scrapeOptions": {
            "formats": ["markdown"],
            "onlyMainContent": True,
        },
    }
    if FIRECRAWL_LOCATION:
        payload["location"] = FIRECRAWL_LOCATION
    if tbs := _firecrawl_tbs(timelimit):
        payload["tbs"] = tbs
    headers = {"Content-Type": "application/json"}
    if FIRECRAWL_API_KEY:
        headers["Authorization"] = f"Bearer {FIRECRAWL_API_KEY}"

    async with httpx.AsyncClient(timeout=timeout + 10, trust_env=True) as client:
        response = await client.post(
            _firecrawl_search_endpoint(),
            json=payload,
            headers=headers,
        )
    response.raise_for_status()
    body = response.json()
    data = body.get("data") if isinstance(body, dict) else None
    if isinstance(data, dict):
        raw_results = data.get("web") or []
    elif isinstance(data, list):
        raw_results = data
    else:
        raw_results = []
    results: list[dict[str, str]] = []
    for item in raw_results:
        if not isinstance(item, dict):
            continue
        url = str(item.get("url") or "").strip()
        if not url:
            continue
        content = str(item.get("markdown") or item.get("description") or "").strip()
        results.append(
            {
                "title": str(item.get("title") or "").strip(),
                "url": url,
                "content": content[:FIRECRAWL_RESULT_CONTENT_CHARS],
                "provider": "firecrawl",
            }
        )
    return results


async def _search_web_async(
    query: str,
    max_results: int = 5,
    timeout: int = DEFAULT_TIMEOUT,
    timelimit: str | None = None,
    *,
    isolated: bool = False,
) -> list[dict[str, str]]:
    """Execute all general web discovery through the local Firecrawl service."""

    del isolated  # Compatibility argument; Firecrawl is already an isolated service.
    if len(query) > MAX_SEARCH_QUERY_CHARS:
        raise ValueError(
            f"Search query exceeds the {MAX_SEARCH_QUERY_CHARS}-character limit"
        )
    async with _firecrawl_search_limiter():
        for attempt in range(1, SEARCH_ATTEMPTS + 1):
            try:
                results = await _search_firecrawl_async(
                    query,
                    max_results=max_results,
                    timeout=timeout,
                    timelimit=timelimit,
                )
                if results:
                    return results
            except asyncio.CancelledError:
                raise
            except Exception as exc:
                logger.warning(
                    "Firecrawl 搜索失败(attempt=%s/%s): %s",
                    attempt,
                    SEARCH_ATTEMPTS,
                    exc,
                )
    logger.warning(
        "Firecrawl 两次尝试后仍无可用结果: query_meta=%s",
        text_fingerprint(query),
    )
    return []


async def execute_web_search(
    query: str,
    *,
    max_results: int = MAX_RESULTS,
    timelimit: str | None = None,
    isolated: bool = False,
) -> str:
    """Execute web search for workflow orchestration without tool-call indirection."""
    sanitized_query = _sanitize_search_query(query)
    if not sanitized_query:
        return json.dumps(
            {"error": "Empty search query", "query": ""}, ensure_ascii=False
        )
    if len(sanitized_query) > MAX_SEARCH_QUERY_CHARS:
        return json.dumps(
            {
                "error": (
                    f"Search query exceeds the {MAX_SEARCH_QUERY_CHARS}-character limit"
                ),
                "query": sanitized_query[:MAX_SEARCH_QUERY_CHARS],
            },
            ensure_ascii=False,
        )

    bounded_results = max(1, min(max_results, MAX_RESULTS))
    logger.info(
        "[AsyncWebSearch] 开始 Firecrawl 搜索: %s",
        sanitized_query,
    )
    results = await _search_web_async(
        query=sanitized_query,
        max_results=bounded_results,
        timelimit=timelimit,
        isolated=isolated,
    )
    if not results:
        return json.dumps(
            {"error": "No results found or search timeout", "query": sanitized_query},
            ensure_ascii=False,
        )
    kept_results, filtered_count = _filter_relevant_results(
        sanitized_query,
        results,
    )
    if not kept_results:
        return json.dumps(
            {
                "error": (
                    "搜索结果与查询相关性过低，已全部过滤（共 "
                    f"{len(results)} 条）。请停止继续联网检索，"
                    "基于已有知识库与网页证据整理结论。"
                ),
                "query": sanitized_query,
                "filtered_count": filtered_count,
            },
            ensure_ascii=False,
        )
    return json.dumps(
        {
            "query": sanitized_query,
            "engine": SEARCH_PROVIDER,
            "total_results": len(kept_results),
            "filtered_count": filtered_count,
            "results": kept_results,
        },
        indent=2,
        ensure_ascii=False,
    )


@tool("web_search", parse_docstring=True)
async def async_web_search_tool(
    query: str,
    max_results: int = MAX_RESULTS,
    timelimit: str | None = None,
) -> str:
    """Search the web for one focused intent, returning titles, URLs, and page evidence.

    Args:
        query: One focused search intent expressed with specific entities and constraints. For multi-part research, call this tool separately for each intent, inspect the returned snippets, and revise the next query when results are off-topic. When asked for current information, include the current year (2026) only when it improves the specific query.
        max_results: Maximum number of results to return. Default is 5.
        timelimit: Optional time range limit. Options: 'd' (day), 'w' (week), 'm' (month), 'y' (year).
                   Leave unset for comprehensive research; use a limit only when recency is part of the intent.
    """
    from backend.core.tools.request_limits import (
        consume_tool_budget,
        exhaust_tool_budget,
        get_web_search_max_results,
        increment_web_search_no_progress_streak,
        reset_web_search_no_progress_streak,
    )

    allowed, _remaining = consume_tool_budget("web_search")
    if not allowed:
        logger.warning("[AsyncWebSearch] 尝试调用被拦截，预算为0")
        return json.dumps(
            {
                "error": f"⚠️ web_search 调用次数已达上限（剩余: {_remaining}）。请基于已有检索结果回答，或结束本轮对话。"
            },
            ensure_ascii=False,
        )

    runtime_max_results = get_web_search_max_results()
    if runtime_max_results is not None:
        max_results = min(max_results, runtime_max_results)

    result = await execute_web_search(
        query,
        max_results=max_results,
        timelimit=timelimit,
    )
    try:
        payload = json.loads(result)
    except (TypeError, json.JSONDecodeError):
        payload = None
    if isinstance(payload, dict) and payload.get("error"):
        streak = increment_web_search_no_progress_streak()
        if streak >= WEB_SEARCH_NO_PROGRESS_LIMIT:
            exhaust_tool_budget("web_search")
            return json.dumps(
                {
                    "error": (
                        f"连续 {streak} 次联网检索未获得相关结果，已停止联网；"
                        "请基于已有知识库与网页证据作答，不要再次调用 web_search。"
                    ),
                    "query": _sanitize_search_query(query),
                    "no_progress_streak": streak,
                    "retrieval_budget": {"remaining": 0, "exhausted": True},
                },
                ensure_ascii=False,
            )
    elif isinstance(payload, dict) and payload.get("results"):
        reset_web_search_no_progress_streak()
    if _remaining == 0:
        try:
            payload = json.loads(result)
        except (TypeError, json.JSONDecodeError):
            return result
        if isinstance(payload, dict) and not payload.get("error"):
            payload["retrieval_budget"] = {
                "remaining": 0,
                "exhausted": True,
                "instruction": "停止继续检索，立即基于现有网页证据整理结论。",
            }
            return json.dumps(payload, indent=2, ensure_ascii=False)
    return result


def get_async_web_search_tool() -> Any:
    """获取异步 web_search 工具实例"""
    return async_web_search_tool
