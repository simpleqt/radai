"""
学术文献检索工具 —— 基于 LangChain @tool + 异步并发

参考 academic-literature-search skill 的架构设计，核心特性：
- 异步并发检索：asyncio.gather 同时查询多个数据源
- Paper 数据类：统一结构化论文数据
- 丰富格式化输出：Markdown 格式，含统计信息
- 多维排序：relevance / citations / year
- 容错机制：限流自动切换 + 超时重试 + 智能退避
- 结果去重：基于 DOI + 标题指纹

数据源：
- Semantic Scholar: AI/CS 领域强项，免费 API
- Crossref: 期刊/DOI 覆盖广，免费 API
- arXiv: 预印本，免费 API
- PubMed: 生物医学，免费 API
- OpenAlex: 覆盖面广（2.5亿+学术作品），免费 API
"""

from __future__ import annotations

from backend.config.loggers import get_logger

from langchain_core.tools import tool

from backend.application.tools.providers.academic_search_sources import (  # noqa: F401  (兼容 re-export)
    DEFAULT_SOURCES,
    DEFAULT_TIMEOUT,
    Database,
    MAX_RETRIES,
    Paper,
    RETRY_DELAY,
    RateLimitError,
    SOURCE_MAP,
    _reconstruct_abstract,
    _search_arxiv,
    _search_baidu_scholar,
    _search_crossref,
    _search_openalex,
    _search_pubmed,
    _search_semantic_scholar,
)
from backend.application.tools.providers.academic_search_pipeline import (  # noqa: F401  (兼容 re-export)
    _async_search,
    _deduplicate,
    _format_markdown,
    _generate_statistics,
    _search_with_fallback,
    _sort_papers,
    _title_fingerprint,
)

logger = get_logger(__name__)


@tool("academic_search")
async def academic_search(
    query: str,
    max_results: int = 5,
    databases: list[str] = None,
    year_range: str = "",
    sort_by: str = "citations",
    min_citations: int = 0,
    open_access_only: bool = False,
) -> str:
    """搜索学术文献。支持多数据源并发检索（Semantic Scholar、arXiv、Crossref、PubMed、OpenAlex、百度学术），
    自动容错切换。适用于查找论文、研究进展、学术引用等场景。

    Args:
        query: 搜索关键词，英文如 "large language models"，中文如 "深度学习"
        max_results: 最大返回结果数，默认5
        databases: 数据源列表，默认全部使用。可选: ["semantic_scholar", "arxiv", "crossref", "pubmed", "openalex", "baidu_scholar"]
        year_range: 年份范围，格式 "2020-2024"，留空不限
        sort_by: 排序方式，可选 "relevance"(默认)、"citations"、"year"
        min_citations: 最小引用数过滤，默认0不过滤
        open_access_only: 是否仅返回开放获取文献，默认False

    Returns:
        Markdown 格式的文献搜索结果，含统计信息
    """
    from backend.core.tools.request_limits import consume_tool_budget

    allowed, remaining = consume_tool_budget("academic_search")
    if not allowed:
        logger.warning("[AcademicSearch] 调用被拦截，预算为 0")
        return (
            f"⚠️ academic_search 调用次数已达上限（剩余: {remaining}）。"
            "请基于已有检索结果回答，或结束本轮对话。"
        )

    parsed_year_range = None
    if year_range and "-" in year_range:
        try:
            parts = year_range.split("-")
            parsed_year_range = (int(parts[0].strip()), int(parts[1].strip()))
        except (ValueError, IndexError):
            pass

    papers, stats = await _async_search(
        query=query,
        max_results=max_results,
        databases=databases,
        year_range=parsed_year_range,
        sort_by=sort_by,
        min_citations=min_citations,
        open_access_only=open_access_only,
    )

    return _format_markdown(papers, query, stats)


async def _run_test(name: str, **kwargs):
    """运行单个测试用例"""
    print(f"\n{'='*60}")
    print(f"🧪 测试: {name}")
    print(f"{'='*60}")
    print(f"参数: {kwargs}")

    try:
        result = await academic_search.ainvoke(kwargs)
        print(result)
    except Exception as e:
        print(f"❌ 测试失败: {type(e).__name__}: {e}")


async def run_all_tests():
    """运行全部功能测试"""
    print("\n" + "🚀" * 20)
    print("学术文献检索工具 —— 功能测试")
    print("🚀" * 20)

    # 测试1：基本搜索
    await _run_test(
        "基本搜索（默认参数）",
        query="机器学习",
        max_results=10,
        databases=["openalex"],
        year_range="2022-2026",
        sort_by="citations",
    )

    print("\n" + "✅" * 20)
    print("全部测试完成！")
    print("✅" * 20)


if __name__ == "__main__":
    # print 直写 stdout 会绕过日志系统（无脱敏/轮转），仅供手动调试使用。
    import asyncio

    asyncio.run(run_all_tests())
