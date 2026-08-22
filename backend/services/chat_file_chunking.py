"""聊天文件索引底座:chunk/检索结果模型、简易分词与余弦、文本切分、查询预处理与泛化摘要判定。(拆自 chat_file_index.py)"""

from __future__ import annotations

from backend.config.loggers import get_logger

from dataclasses import dataclass
from typing import Dict
from typing import List
import math
import re

logger = get_logger(__name__)


@dataclass
class ChunkMeta:
    file_id: str
    file_name: str
    chunk_index: int
    content: str


@dataclass
class SearchResult:
    content: str
    file_name: str
    file_id: str
    chunk_index: int
    score: float


def _tokenize_simple(text: str) -> List[str]:
    """轻量分词：中文按字+bigram，英文按单词，数字保留"""
    words: List[str] = []
    for m in re.finditer(r"[\u4e00-\u9fff]+", text):
        s = m.group()
        words.extend(s)           # 单字
        for i in range(len(s) - 1):
            words.append(s[i : i + 2])   # bigram
    for m in re.finditer(r"[a-zA-Z]{2,}", text):
        words.append(m.group().lower())
    for m in re.finditer(r"\d+(?:\.\d+)?", text):
        words.append(m.group())
    return words


def _cosine_sim(a: Dict[str, float], b: Dict[str, float]) -> float:
    """两个稀疏 TF-IDF 向量的余弦相似度"""
    if not a or not b:
        return 0.0
    common = set(a) & set(b)
    if not common:
        return 0.0
    dot = sum(a[k] * b[k] for k in common)
    norm_a = math.sqrt(sum(v * v for v in a.values()))
    norm_b = math.sqrt(sum(v * v for v in b.values()))
    if norm_a < 1e-9 or norm_b < 1e-9:
        return 0.0
    return dot / (norm_a * norm_b)


_FIGURE_QUERY_RE = re.compile(
    r"(图片|图表|配图|插图|附图|对比图|结构图|示意图|结果图|figure|fig\.?|illustration|image)",
    re.IGNORECASE,
)


_TABLE_QUERY_RE = re.compile(
    r"(表格|数据表|table|tab\.?)",
    re.IGNORECASE,
)


_FIGURE_CONTENT_PATTERNS = [
    re.compile(r"\[图片[:：][^\]]+\]", re.IGNORECASE),
    re.compile(r"!\[[^\]]*\]\([^)]+\)", re.IGNORECASE),
    re.compile(r"\b(?:figure|fig\.?)\s*\d+[:.\-\s]?", re.IGNORECASE),
    re.compile(r"图\s*\d+[:：.\-\s]?", re.IGNORECASE),
]


_TABLE_CONTENT_PATTERNS = [
    re.compile(r"\b(?:table|tab\.?)\s*\d+[:.\-\s]?", re.IGNORECASE),
    re.compile(r"表\s*\d+[:：.\-\s]?", re.IGNORECASE),
]


_RETRIEVAL_STOP_WORDS = {
    "这篇", "这份", "这个", "该", "本", "论文", "文档", "文件", "文章", "报告",
    "里面", "其中", "关于", "有关", "请问", "一下", "一下子", "什么", "哪些",
    "哪个", "哪里", "有没有", "有无", "如何", "怎么", "为什么", "多少", "几个",
    "是什么", "有哪些", "有什么", "讲了什么", "说了什么", "写了什么", "介绍",
    "描述", "分析", "总结", "概述", "内容", "信息", "具体", "相关",
    "please", "show", "tell", "what", "which", "where", "does", "is", "are",
    "the", "a", "an", "in", "of", "to", "for", "about", "with", "from",
}


_IMAGE_QUERY_KEYWORDS = [
    "figure", "fig", "image", "illustration", "caption", "图片", "图", "图注",
]


_TABLE_QUERY_KEYWORDS = [
    "table", "tab", "dataset", "statistics", "表格", "表", "数据表",
]


_SUMMARY_QUERY_TERMS = "abstract introduction methodology method experiment results conclusion summary main contribution"


def split_text(
    text: str,
    chunk_size: int = 800,
    chunk_overlap: int = 100,
) -> List[str]:
    """智能分块 - 尊重段落和句子边界"""
    if chunk_size <= 0:
        raise ValueError("chunk_size must be positive")

    paragraphs = re.split(r"\n{2,}", text)
    chunks: List[str] = []
    current = ""

    for para in paragraphs:
        if len(current) + len(para) <= chunk_size:
            current += ("\n\n" + para) if current else para
        else:
            if current:
                chunks.append(current.strip())
            if len(para) > chunk_size:
                sentences = re.split(r"(?<=[。！？.!?])\s*", para)
                current = ""
                for sent in sentences:
                    if len(current) + len(sent) <= chunk_size:
                        current += sent
                    else:
                        if current:
                            chunks.append(current.strip())
                        current = sent
            else:
                current = para

    if current.strip():
        chunks.append(current.strip())
    return _apply_chunk_overlap(chunks, chunk_overlap)


def _apply_chunk_overlap(chunks: List[str], chunk_overlap: int) -> List[str]:
    """为相邻块补充字符级重叠，降低边界信息截断。"""
    if chunk_overlap <= 0 or len(chunks) <= 1:
        return chunks

    normalized_overlap = max(1, chunk_overlap)
    overlapped_chunks: List[str] = [chunks[0]]

    for chunk in chunks[1:]:
        prev_chunk = overlapped_chunks[-1]
        prefix = prev_chunk[-normalized_overlap:]
        if prefix and not chunk.startswith(prefix):
            overlapped_chunks.append(f"{prefix}{chunk}")
        else:
            overlapped_chunks.append(chunk)

    return overlapped_chunks


# 通用总结型查询模式：不含实质内容关键词，BM25 无法有效检索
_GENERIC_QUERY_PATTERNS = re.compile(
    r"^(这|那|此|该|本)?"
    r"(篇|份|个|部|章|段|节|文件|文档|论文|报告|文章|资料|附件|内容|文本)?"
    r"(讲|说|写|介绍|描述|解释|分析|总结|阐述|叙述)?(了|的)?"
    r"(什么|啥|内容|信息|主题|主旨|重点|要点|概要|摘要|大意|意思)$",
    re.IGNORECASE,
)


_GENERIC_KEYWORDS = {
    "总结", "摘要", "概述", "summary", "summarize", "abstract",
    "讲了什么", "说了什么", "写了什么", "主要内容", "主要讲",
    "介绍了什么", "概括", "大意", "要点", "核心内容",
    "这篇", "这份", "这个文档", "这个文件", "上传的",
}


def prepare_search_inputs(
    query: str,
    keywords: List[str] | None = None,
) -> tuple[str, List[str]]:
    """将自然语言问题整理成更适合检索的 query/keywords。

    无论上层是否直接传入用户原句，底层都会先做这一步，避免把整句自然语言
    原封不动喂给 BM25。
    """
    raw_query = " ".join((query or "").split()).strip()
    normalized_keywords: list[str] = []
    seen_keywords: set[str] = set()

    def _push_keyword(value: str) -> None:
        kw = " ".join((value or "").split()).strip().strip(".,;:：()[]{}<>")
        if len(kw) < 2:
            return
        lowered = kw.lower()
        if lowered in seen_keywords:
            return
        seen_keywords.add(lowered)
        normalized_keywords.append(kw)

    for kw in keywords or []:
        if isinstance(kw, str):
            _push_keyword(kw)

    # 保留引号中的精确短语，优先级很高
    for pattern in (
        r'"([^"\n]{2,80})"',
        r"'([^'\n]{2,80})'",
        r"“([^”\n]{2,80})”",
        r"‘([^’\n]{2,80})’",
    ):
        for match in re.findall(pattern, raw_query):
            _push_keyword(match)

    # 英文术语、缩写、数字编号
    for token in re.findall(r"[A-Za-z][A-Za-z0-9_\-]{1,40}", raw_query):
        if token.lower() not in _RETRIEVAL_STOP_WORDS:
            _push_keyword(token)
    for token in re.findall(r"(?:fig(?:ure)?|table|tab\.?)\s*\d+", raw_query, re.IGNORECASE):
        _push_keyword(token)
    for token in re.findall(r"图\s*\d+|表\s*\d+", raw_query):
        _push_keyword(token)

    # 中文术语候选
    for token in re.findall(
        r"[\u4e00-\u9fff]{2,12}(?:方法|模型|框架|算法|实验|结果|图片|图表|图像|图注|摘要|引言|结论|对比图|数据集)",
        raw_query,
    ):
        if token not in _RETRIEVAL_STOP_WORDS:
            _push_keyword(token)
    for token in re.findall(r"[\u4e00-\u9fff]{2,12}", raw_query):
        if token not in _RETRIEVAL_STOP_WORDS:
            _push_keyword(token)

    if _FIGURE_QUERY_RE.search(raw_query):
        for kw in _IMAGE_QUERY_KEYWORDS:
            _push_keyword(kw)
    if _TABLE_QUERY_RE.search(raw_query):
        for kw in _TABLE_QUERY_KEYWORDS:
            _push_keyword(kw)

    if _is_generic_summary_query(raw_query):
        for token in _SUMMARY_QUERY_TERMS.split():
            _push_keyword(token)
        return _SUMMARY_QUERY_TERMS, normalized_keywords[:12]

    if normalized_keywords:
        # 用提炼后的关键词组合构造检索 query，避免直接把整句问句交给 BM25
        rewritten_query = " ".join(normalized_keywords[:8])
        return rewritten_query, normalized_keywords[:12]

    return raw_query, normalized_keywords[:12]


def _is_generic_summary_query(query: str) -> bool:
    """判断是否为通用总结/概述类查询，这类查询不含实质关键词，BM25 效果差。"""
    q = query.strip().lower()
    # 极短查询（≤15字符）且命中模式
    if len(q) <= 25 and _GENERIC_QUERY_PATTERNS.search(q):
        return True
    # 包含通用关键词
    for kw in _GENERIC_KEYWORDS:
        if kw in q:
            return True
    return False
