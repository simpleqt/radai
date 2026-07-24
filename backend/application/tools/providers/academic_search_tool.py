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

import asyncio
import hashlib
import logging
import os
import xml.etree.ElementTree as ET
from dataclasses import asdict, dataclass, field
from datetime import datetime
from enum import Enum
from typing import Optional

import httpx
from langchain_core.tools import tool

logger = logging.getLogger(__name__)


# ============================================================
# 数据模型
# ============================================================

class Database(Enum):
    SEMANTIC_SCHOLAR = "semantic_scholar"
    CROSSREF = "crossref"
    PUBMED = "pubmed"
    ARXIV = "arxiv"
    BAIDU_SCHOLAR = "baidu_scholar"
    OPENALEX = "openalex"


@dataclass
class Paper:
    """论文数据类 —— 统一各数据源的论文结构"""
    title: str
    authors: list[str] = field(default_factory=list)
    abstract: Optional[str] = None
    year: Optional[int] = None
    venue: Optional[str] = None
    doi: Optional[str] = None
    arxiv_id: Optional[str] = None
    pmid: Optional[str] = None
    citation_count: int = 0
    url: Optional[str] = None
    open_access_pdf: Optional[str] = None
    is_open_access: bool = False
    source_database: Database = Database.SEMANTIC_SCHOLAR

    def to_dict(self) -> dict:
        data = asdict(self)
        data["source_database"] = self.source_database.value
        return data


class RateLimitError(Exception):
    """API 限流错误，触发数据源切换"""

    def __init__(self, source: str, retry_after: int = 5):
        self.source = source
        self.retry_after = retry_after
        super().__init__(f"{source} API 限流，建议 {retry_after}s 后重试")


# ============================================================
# 配置
# ============================================================

DEFAULT_TIMEOUT = 15
MAX_RETRIES = 2
RETRY_DELAY = 0.5


# ============================================================
# 异步数据源适配器
# ============================================================

async def _search_semantic_scholar(
    client: httpx.AsyncClient, query: str, max_results: int,
    year_range: tuple[int, int] = None,
) -> list[Paper]:
    """Semantic Scholar API 异步检索

    无 API Key 时限流严格（约1次/5秒），建议配置 SEMANTIC_SCHOLAR_API_KEY。
    申请地址: https://www.semanticscholar.org/product/api#api-key-form
    """
    url = "https://api.semanticscholar.org/graph/v1/paper/search"
    params = {
        "query": query,
        "limit": min(max_results * 2, 100),
        "fields": "paperId,title,abstract,authors,year,venue,citationCount,url,openAccessPdf,isOpenAccess,externalIds",
    }
    if year_range:
        params["year"] = f"{year_range[0]}-{year_range[1]}"

    api_key = os.getenv("SEMANTIC_SCHOLAR_API_KEY", "")
    headers = {"x-api-key": api_key} if api_key else {}

    if not api_key:
        logger.info("Semantic Scholar: 未配置 API Key，可能触发限流。建议设置 SEMANTIC_SCHOLAR_API_KEY")

    resp = await client.get(url, params=params, headers=headers)

    if resp.status_code == 429:
        retry_after = int(resp.headers.get("Retry-After", 5))
        logger.warning(f"Semantic Scholar 限流，{retry_after}s 后可重试（配置 API Key 可提高限额）")
        raise RateLimitError("Semantic Scholar", retry_after)
    if resp.status_code in (400, 401, 403):
        logger.warning(f"Semantic Scholar API 错误 {resp.status_code}: {resp.text[:200]}")
        return []
    resp.raise_for_status()

    papers = []
    for item in resp.json().get("data", []):
        authors = [a.get("name", "") for a in (item.get("authors") or []) if a.get("name")]
        ext_ids = item.get("externalIds") or {}
        oa_pdf = item.get("openAccessPdf") or {}

        papers.append(Paper(
            title=item.get("title", "无标题"),
            authors=authors,
            abstract=item.get("abstract"),
            year=item.get("year"),
            venue=item.get("venue", ""),
            doi=ext_ids.get("DOI"),
            citation_count=item.get("citationCount", 0),
            url=item.get("url"),
            open_access_pdf=oa_pdf.get("url"),
            is_open_access=item.get("isOpenAccess", False),
            source_database=Database.SEMANTIC_SCHOLAR,
        ))
    return papers


async def _search_crossref(
    client: httpx.AsyncClient, query: str, max_results: int,
    year_range: tuple[int, int] = None,
) -> list[Paper]:
    """Crossref API 异步检索"""
    url = "https://api.crossref.org/works"
    params = {
        "query": query,
        "rows": min(max_results * 2, 100),
        "sort": "relevance",
    }
    if year_range:
        params["filter"] = f"from-pub-date:{year_range[0]},until-pub-date:{year_range[1]}"

    email = os.getenv("CROSSREF_API_EMAIL", "openclaw@example.com")
    headers = {"User-Agent": f"AcademicSearchTool/1.0 (mailto:{email})"}

    resp = await client.get(url, params=params, headers=headers)

    if resp.status_code == 429:
        retry_after = int(resp.headers.get("Retry-After", 5))
        raise RateLimitError("Crossref", retry_after)
    resp.raise_for_status()

    papers = []
    for item in resp.json().get("message", {}).get("items", []):
        authors = [
            f"{a.get('given', '')} {a.get('family', '')}".strip()
            for a in (item.get("author") or [])
            if f"{a.get('given', '')} {a.get('family', '')}".strip()
        ]

        year = None
        for key in ("published-print", "published-online", "issued"):
            date_parts = item.get(key, {}).get("date-parts", [[None]])
            if date_parts and date_parts[0] and date_parts[0][0]:
                year = date_parts[0][0]
                break

        abstract_raw = item.get("abstract") or ""
        abstract_clean = abstract_raw.replace("<jats:p>", "").replace("</jats:p>", "")
        abstract_clean = abstract_clean.replace("<jats:italic>", "").replace("</jats:italic>", "")

        venue = ", ".join(item.get("container-title", []))

        papers.append(Paper(
            title=item.get("title", ["无标题"])[0] if item.get("title") else "无标题",
            authors=authors,
            abstract=abstract_clean if abstract_clean else None,
            year=year,
            venue=venue or None,
            doi=item.get("DOI"),
            url=item.get("URL"),
            citation_count=item.get("is-referenced-by-count", 0),
            source_database=Database.CROSSREF,
        ))
    return papers


async def _search_arxiv(
    client: httpx.AsyncClient, query: str, max_results: int,
    year_range: tuple[int, int] = None,
) -> list[Paper]:
    """arXiv API 异步检索"""
    url = "https://export.arxiv.org/api/query"
    params = {
        "search_query": f"all:{query}",
        "max_results": min(max_results * 2, 100),
        "sortBy": "relevance",
        "sortOrder": "descending",
    }

    resp = await client.get(url, params=params)

    if resp.status_code == 429:
        raise RateLimitError("arXiv")
    resp.raise_for_status()

    papers = []
    root = ET.fromstring(resp.text)
    ns = {"atom": "http://www.w3.org/2005/Atom"}

    for entry in root.findall("atom:entry", ns):
        title_el = entry.find("atom:title", ns)
        title = title_el.text.strip().replace("\n", " ") if title_el is not None and title_el.text else "无标题"

        authors = []
        for a in entry.findall("atom:author", ns):
            name_el = a.find("atom:name", ns)
            if name_el is not None and name_el.text:
                authors.append(name_el.text)

        abstract_el = entry.find("atom:summary", ns)
        abstract = abstract_el.text.strip().replace("\n", " ") if abstract_el is not None and abstract_el.text else None

        id_el = entry.find("atom:id", ns)
        arxiv_id = None
        if id_el is not None and id_el.text:
            arxiv_id = id_el.text.split("/")[-1]
            if "v" in arxiv_id:
                arxiv_id = arxiv_id.split("v")[0]

        published_el = entry.find("atom:published", ns)
        year = int(published_el.text[:4]) if published_el is not None and published_el.text else None

        if year_range and year:
            if not (year_range[0] <= year <= year_range[1]):
                continue

        pdf_link = None
        for link in entry.findall("atom:link", ns):
            if link.get("title") == "pdf":
                pdf_link = link.get("href")
                break

        papers.append(Paper(
            title=title,
            authors=authors,
            abstract=abstract,
            year=year,
            arxiv_id=arxiv_id,
            url=f"https://arxiv.org/abs/{arxiv_id}" if arxiv_id else None,
            open_access_pdf=pdf_link,
            is_open_access=True,
            source_database=Database.ARXIV,
        ))
    return papers


async def _search_pubmed(
    client: httpx.AsyncClient, query: str, max_results: int,
    year_range: tuple[int, int] = None,
) -> list[Paper]:
    """PubMed API 异步检索（ESearch + EFetch 两步）"""
    pubmed_query = query
    if year_range:
        pubmed_query += f" AND ({year_range[0]}[Date - Publication] : {year_range[1]}[Date - Publication])"

    search_url = "https://eutils.ncbi.nlm.nih.gov/entrez/eutils/esearch.fcgi"
    search_params = {
        "db": "pubmed",
        "term": pubmed_query,
        "retmax": min(max_results, 100),
        "sort": "relevance",
        "retmode": "json",
    }
    api_key = os.getenv("PUBMED_API_KEY", "")
    if api_key:
        search_params["api_key"] = api_key

    resp = await client.get(search_url, params=search_params)

    if resp.status_code == 429:
        raise RateLimitError("PubMed")
    resp.raise_for_status()

    id_list = resp.json().get("esearchresult", {}).get("idlist", [])
    if not id_list:
        return []

    fetch_url = "https://eutils.ncbi.nlm.nih.gov/entrez/eutils/efetch.fcgi"
    fetch_params = {
        "db": "pubmed",
        "id": ",".join(id_list),
        "retmode": "xml",
        "rettype": "abstract",
    }
    if api_key:
        fetch_params["api_key"] = api_key

    resp = await client.get(fetch_url, params=fetch_params)

    if resp.status_code == 429:
        raise RateLimitError("PubMed")
    resp.raise_for_status()

    papers = []
    root = ET.fromstring(resp.text)

    for article in root.findall(".//PubmedArticle"):
        medline = article.find(".//MedlineCitation")
        art = medline.find(".//Article") if medline is not None else None
        if art is None:
            continue

        title_el = art.find(".//ArticleTitle")
        title = title_el.text if title_el is not None and title_el.text else "无标题"

        authors = []
        for author in art.findall(".//Author"):
            last = author.find("LastName")
            fore = author.find("ForeName")
            if last is not None and last.text:
                name = f"{fore.text} {last.text}" if fore is not None and fore.text else last.text
                authors.append(name.strip())

        abstract_parts = []
        for abs_el in art.findall(".//AbstractText"):
            if abs_el.text:
                abstract_parts.append(abs_el.text)
        abstract = " ".join(abstract_parts) if abstract_parts else None

        year = None
        pub_date = art.find(".//PubDate")
        if pub_date is not None:
            year_el = pub_date.find("Year")
            if year_el is not None and year_el.text:
                year = int(year_el.text)

        journal = art.find(".//Journal/Title")
        venue = journal.text if journal is not None else None

        pmid_el = medline.find(".//PMID") if medline is not None else None
        pmid = pmid_el.text if pmid_el is not None else None

        papers.append(Paper(
            title=title,
            authors=authors,
            abstract=abstract,
            year=year,
            venue=venue,
            pmid=pmid,
            url=f"https://pubmed.ncbi.nlm.nih.gov/{pmid}/" if pmid else None,
            source_database=Database.PUBMED,
        ))
    return papers


async def _search_baidu_scholar(
    client: httpx.AsyncClient, query: str, max_results: int,
    year_range: tuple[int, int] = None,
) -> list[Paper]:
    """百度学术检索 API（千帆平台）— ⚠️ 已废弃

    千帆平台已下线此 API 端点，调用会返回 404。
    保留此函数以备服务恢复，但已从 DEFAULT_SOURCES 中移除。
    如需使用，需在 databases 参数中显式指定 "baidu_scholar"。

    原文档: https://cloud.baidu.com/doc/qianfan/s/Amkw9qpzd
    认证方式 (二选一):
      - BAIDU_QIANFAN_API_KEY: 千帆应用 API Key (推荐)
      - BAIDU_QIANFAN_AK + BAIDU_QIANFAN_SK: OAuth AK/SK
    特色: 支持 AI 摘要（aiAbstract），中文学术文献覆盖好。
    """
    url = "https://qianfan.baidubce.com/v2/tools/baidu_scholar/search"
    params = {
        "wd": query,
        "enable_abstract": "true",
    }

    api_key = os.getenv("BAIDU_QIANFAN_API_KEY", "")
    headers = {"Authorization": f"Bearer {api_key}"} if api_key else {}

    if not api_key:
        logger.info("百度学术: 未配置 BAIDU_QIANFAN_API_KEY，跳过")
        return []

    resp = await client.get(url, params=params, headers=headers)

    if resp.status_code == 429:
        raise RateLimitError("Baidu Scholar", 60)
    if resp.status_code in (400, 401, 403):
        logger.warning(f"百度学术 API 错误 {resp.status_code}: {resp.text[:200]}")
        return []
    resp.raise_for_status()

    data = resp.json()
    if data.get("code") != "0":
        logger.warning(f"百度学术 API 返回错误: code={data.get('code')}, message={data.get('message')}")
        return []

    papers = []
    for item in data.get("data", [])[:max_results]:
        publish_year = item.get("publishYear")
        year = publish_year if publish_year and publish_year > 0 else None

        if year_range and year:
            if not (year_range[0] <= year <= year_range[1]):
                continue

        abstract = item.get("abstract") or item.get("aiAbstract") or None
        venue = (item.get("publishInfo") or {}).get("journalName") or None

        papers.append(Paper(
            title=item.get("title", "无标题"),
            abstract=abstract if abstract else None,
            year=year,
            venue=venue if venue else None,
            doi=item.get("doi") or None,
            url=item.get("url"),
            is_open_access=bool(item.get("url")),
            source_database=Database.BAIDU_SCHOLAR,
        ))
    return papers


async def _search_openalex(
    client: httpx.AsyncClient, query: str, max_results: int,
    year_range: tuple[int, int] = None,
) -> list[Paper]:
    """OpenAlex API 异步检索

    免费 API Key，每天 $1 免费额度（约 1000 次搜索）。
    申请地址: https://openalex.org/settings/api
    特色: 覆盖面广（2.5亿+学术作品），支持语义搜索，引用数据丰富。
    """
    url = "https://api.openalex.org/works"
    params = {
        "search": query,
        "per_page": min(max_results * 2, 100),
        "select": "id,title,authorships,abstract_inverted_index,publication_year,primary_location,doi,cited_by_count,open_access,ids",
    }

    api_key = os.getenv("OPENALEX_API_KEY", "")
    if api_key:
        params["api_key"] = api_key

    if not api_key:
        logger.info("OpenAlex: 未配置 API Key，使用免费额度。建议设置 OPENALEX_API_KEY")

    filters = []
    if year_range:
        filters.append(f"from_publication_date:{year_range[0]}-01-01")
        filters.append(f"to_publication_date:{year_range[1]}-12-31")
    if filters:
        params["filter"] = ",".join(filters)

    resp = await client.get(url, params=params)

    if resp.status_code == 429:
        retry_after = int(resp.headers.get("Retry-After", 60))
        logger.warning(f"OpenAlex 限流，{retry_after}s 后可重试")
        raise RateLimitError("OpenAlex", retry_after)
    if resp.status_code in (400, 401, 403):
        logger.warning(f"OpenAlex API 错误 {resp.status_code}: {resp.text[:200]}")
        return []
    resp.raise_for_status()

    data = resp.json()
    papers = []
    for item in data.get("results", []):
        authors = []
        for authorship in (item.get("authorships") or []):
            author_obj = authorship.get("author") or {}
            name = author_obj.get("display_name", "")
            if name:
                authors.append(name)

        abstract = _reconstruct_abstract(item.get("abstract_inverted_index"))

        venue = None
        primary_loc = item.get("primary_location") or {}
        source = primary_loc.get("source") or {}
        venue = source.get("display_name")

        doi = item.get("doi")
        if doi and doi.startswith("https://doi.org/"):
            doi = doi[len("https://doi.org/"):]

        ids = item.get("ids") or {}
        pmid = ids.get("pmid")
        if pmid and pmid.startswith("https://pubmed.ncbi.nlm.nih.gov/"):
            pmid = pmid.rstrip("/").split("/")[-1]

        oa = item.get("open_access") or {}
        is_oa = oa.get("is_oa", False)
        oa_url = oa.get("oa_url")

        openalex_id = item.get("id", "")
        if openalex_id.startswith("https://openalex.org/"):
            openalex_id = openalex_id[len("https://openalex.org/"):]

        papers.append(Paper(
            title=item.get("title", "无标题"),
            authors=authors,
            abstract=abstract,
            year=item.get("publication_year"),
            venue=venue,
            doi=doi,
            pmid=pmid,
            citation_count=item.get("cited_by_count", 0),
            url=item.get("doi") or item.get("id"),
            open_access_pdf=oa_url,
            is_open_access=is_oa,
            source_database=Database.OPENALEX,
        ))
    return papers


def _reconstruct_abstract(inverted_index: dict) -> Optional[str]:
    """从 OpenAlex 倒排索引重建摘要文本

    OpenAlex 将摘要存储为倒排索引格式:
    {"word1": [0, 5], "word2": [1, 3], ...}
    需要按位置排序后拼接为完整文本。
    """
    if not inverted_index:
        return None

    position_word = []
    for word, positions in inverted_index.items():
        for pos in positions:
            position_word.append((pos, word))

    position_word.sort(key=lambda x: x[0])
    return " ".join(word for _, word in position_word)


# ============================================================
# 检索引擎
# ============================================================

# 数据源映射
SOURCE_MAP = {
    "semantic_scholar": _search_semantic_scholar,
    "arxiv": _search_arxiv,
    "crossref": _search_crossref,
    "pubmed": _search_pubmed,
    "baidu_scholar": _search_baidu_scholar,
    "openalex": _search_openalex,
}

DEFAULT_SOURCES = ["arxiv", "crossref", "openalex", "semantic_scholar", "pubmed"]


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


# ============================================================
# 去重 / 排序 / 统计
# ============================================================

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


# ============================================================
# 格式化输出
# ============================================================

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


# ============================================================
# LangChain Tool 定义
# ============================================================

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


# ============================================================
# 功能测试
# ============================================================

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
    from dotenv import load_dotenv
    load_dotenv()
    asyncio.run(run_all_tests())
