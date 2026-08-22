"""学术检索管线:源回退、并发执行、标题指纹去重、排序、统计与 Markdown 结果渲染。(拆自 academic_search_tool.py)"""

from __future__ import annotations

from backend.config.loggers import get_logger

from datetime import datetime
from typing import Optional
import asyncio
import hashlib
import httpx
from backend.application.tools.providers.academic_search_sources import (
    DEFAULT_SOURCES,
    DEFAULT_TIMEOUT,
    MAX_RETRIES,
    Paper,
    RETRY_DELAY,
    RateLimitError,
    SOURCE_MAP,
)

logger = get_logger(__name__)


async def _search_with_fallback(
    client: httpx.AsyncClient,
    source_name: str,
    search_fn,
    query: str,
    max_results: int,
    year_range: tuple[int, int] = None,
) -> tuple[str, list[Paper], Optional[str]]:
    """带容错的单源检索

    Returns:
        (source_name, papers, error_msg)
    """
    for attempt in range(MAX_RETRIES):
        try:
            papers = await search_fn(client, query, max_results, year_range)
            return source_name, papers, None

        except RateLimitError as e:
            return source_name, [], f"限流(建议{e.retry_after}s后重试)"

        except httpx.TimeoutException:
            if attempt == MAX_RETRIES - 1:
                return source_name, [], "超时"

        except httpx.HTTPStatusError as e:
            if attempt == MAX_RETRIES - 1:
                return source_name, [], f"HTTP {e.response.status_code}"

        except Exception as e:
            if attempt == MAX_RETRIES - 1:
                return source_name, [], f"{type(e).__name__}: {str(e)[:80]}"

        if attempt < MAX_RETRIES - 1:
            await asyncio.sleep(RETRY_DELAY)

    return source_name, [], "未知错误"


async def _async_search(
    query: str,
    max_results: int = 5,
    databases: list[str] = None,
    year_range: tuple[int, int] = None,
    sort_by: str = "relevance",
    min_citations: int = 0,
    open_access_only: bool = False,
) -> tuple[list[Paper], dict]:
    """异步并发检索核心函数

    Returns:
        (papers, stats) 论文列表和统计信息
    """
    db_list = databases or DEFAULT_SOURCES

    async with httpx.AsyncClient(
        trust_env=True,
        timeout=DEFAULT_TIMEOUT,
        follow_redirects=True,
    ) as client:
        # 并发查询所有数据源
        tasks = []
        for db_name in db_list:
            search_fn = SOURCE_MAP.get(db_name)
            if search_fn:
                tasks.append(
                    _search_with_fallback(client, db_name, search_fn, query, max_results, year_range)
                )

        results = await asyncio.gather(*tasks)

    # 合并结果
    all_papers = []
    source_status = {}
    for source_name, papers, error_msg in results:
        if error_msg:
            source_status[source_name] = f"❌ {error_msg}"
            logger.warning(f"{source_name} 检索失败: {error_msg}")
        else:
            source_status[source_name] = f"✅ {len(papers)}篇"
            all_papers.extend(papers)

    # 去重
    all_papers = _deduplicate(all_papers)

    # 过滤
    if min_citations > 0:
        all_papers = [p for p in all_papers if p.citation_count >= min_citations]
    if open_access_only:
        all_papers = [p for p in all_papers if p.is_open_access]

    # 排序
    all_papers = _sort_papers(all_papers, sort_by)
    all_papers = all_papers[:max_results]

    # 统计
    stats = _generate_statistics(all_papers, source_status)

    return all_papers, stats


def _title_fingerprint(title: str) -> str:
    """标题指纹：忽略大小写和标点"""
    normalized = "".join(c.lower() for c in title if c.isalnum())
    return hashlib.md5(normalized.encode()).hexdigest()


def _deduplicate(papers: list[Paper]) -> list[Paper]:
    """基于 DOI + 标题指纹去重，保留引用数最多的版本"""
    seen_dois = set()
    seen_titles = {}
    result = []

    for paper in papers:
        if paper.doi and paper.doi in seen_dois:
            continue

        fp = _title_fingerprint(paper.title)
        if fp in seen_titles:
            existing = seen_titles[fp]
            if paper.citation_count > existing.citation_count:
                result.remove(existing)
                seen_titles[fp] = paper
                result.append(paper)
            continue

        if paper.doi:
            seen_dois.add(paper.doi)
        seen_titles[fp] = paper
        result.append(paper)

    return result


def _sort_papers(papers: list[Paper], sort_by: str) -> list[Paper]:
    """多维排序：relevance / citations / year"""
    if sort_by == "citations":
        return sorted(papers, key=lambda p: p.citation_count, reverse=True)
    elif sort_by == "year":
        return sorted(papers, key=lambda p: p.year or 0, reverse=True)
    else:
        return sorted(papers, key=lambda p: p.citation_count, reverse=True)


def _generate_statistics(papers: list[Paper], source_status: dict) -> dict:
    """生成统计信息"""
    if not papers:
        return {"total_papers": 0, "source_status": source_status}

    years = [p.year for p in papers if p.year]
    citations = [p.citation_count for p in papers]

    year_dist = {}
    if years:
        year_dist = {
            "earliest": min(years),
            "latest": max(years),
        }

    citation_stats = {
        "total": sum(citations),
        "average": round(sum(citations) / len(citations), 1) if citations else 0,
        "max": max(citations) if citations else 0,
    }

    sources = {}
    for p in papers:
        src = p.source_database.value
        sources[src] = sources.get(src, 0) + 1

    open_access_count = sum(1 for p in papers if p.is_open_access)

    return {
        "total_papers": len(papers),
        "year_distribution": year_dist,
        "citation_statistics": citation_stats,
        "source_distribution": sources,
        "open_access_count": open_access_count,
        "open_access_percentage": round(open_access_count / len(papers) * 100, 1) if papers else 0,
        "source_status": source_status,
    }


def _format_markdown(papers: list[Paper], query: str, stats: dict) -> str:
    """Markdown 格式化输出（参考 agent.py 的 OutputFormatter）"""
    lines = ["# 📚 文献检索结果\n"]

    lines.append(f"**检索词**: {query}")
    lines.append(f"**检索时间**: {datetime.now().strftime('%Y-%m-%d %H:%M:%S')}")
    lines.append(f"**结果数量**: {len(papers)}篇")

    # 统计摘要
    if stats.get("year_distribution"):
        yd = stats["year_distribution"]
        lines.append(f"**年份范围**: {yd['earliest']} - {yd['latest']}")
    if stats.get("citation_statistics"):
        cs = stats["citation_statistics"]
        lines.append(f"**引用统计**: 总计 {cs['total']}, 平均 {cs['average']}, 最高 {cs['max']}")
    if stats.get("open_access_percentage"):
        lines.append(f"**开放获取**: {stats['open_access_count']}篇 ({stats['open_access_percentage']}%)")

    # 数据源状态
    if stats.get("source_status"):
        status_parts = [f"{k} {v}" for k, v in stats["source_status"].items()]
        lines.append(f"**数据源**: {', '.join(status_parts)}")

    lines.append("")
    lines.append("---\n")

    if not papers:
        lines.append("未找到相关文献。建议：\n")
        lines.append("- 尝试使用英文关键词")
        lines.append("- 扩大年份范围")
        lines.append("- 减少引用数过滤条件")
        return "\n".join(lines)

    # 论文列表
    for i, paper in enumerate(papers, 1):
        lines.append(f"### {i}. {paper.title}")

        # 作者
        if paper.authors:
            authors_str = ", ".join(paper.authors)
            lines.append(f"- **作者**: {authors_str}")

        # 出版信息
        pub_info = []
        if paper.year:
            pub_info.append(f"{paper.year}年")
        if paper.venue:
            pub_info.append(paper.venue)
        if pub_info:
            lines.append(f"- **出版信息**: {' | '.join(pub_info)}")

        # 引用数
        if paper.citation_count > 0:
            lines.append(f"- **引用数**: {paper.citation_count}")

        # 标识符
        identifiers = []
        if paper.doi:
            identifiers.append(f"DOI: {paper.doi}")
        if paper.pmid:
            identifiers.append(f"PMID: {paper.pmid}")
        if paper.arxiv_id:
            identifiers.append(f"arXiv: {paper.arxiv_id}")
        if identifiers:
            lines.append(f"- **标识符**: {' | '.join(identifiers)}")

        # 摘要
        if paper.abstract:
            lines.append(f"- **摘要**: {paper.abstract}")

        # 链接
        links = []
        if paper.url:
            links.append(f"[原文链接]({paper.url})")
        if paper.open_access_pdf:
            links.append(f"[PDF下载]({paper.open_access_pdf})")
        if paper.is_open_access:
            links.append("🔓 开放获取")
        if links:
            lines.append(f"- **访问**: {' | '.join(links)}")

        lines.append("")

    return "\n".join(lines)
