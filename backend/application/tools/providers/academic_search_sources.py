"""学术检索数据源:Paper/Database 模型、限流异常与六个学术源的检索实现。(拆自 academic_search_tool.py)"""

from __future__ import annotations

from backend.config.loggers import get_logger

from dataclasses import asdict
from dataclasses import dataclass
from dataclasses import field
from enum import Enum
from typing import Optional
import httpx
import os
import xml.etree.ElementTree as ET

logger = get_logger(__name__)


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


DEFAULT_TIMEOUT = 15


MAX_RETRIES = 2


RETRY_DELAY = 0.5


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
