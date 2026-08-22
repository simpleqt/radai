"""
对话级文件轻量检索索引

BM25 关键词召回 + TF-IDF 余弦相似度精排，纯 Python 实现，
零外部依赖（不调 embedding API，不需要 Milvus/Redis）。
索引生命周期与会话绑定，LRU 池控制内存上限。
"""

from __future__ import annotations

from backend.services.chat_file_chunking import (  # noqa: F401  (兼容 re-export)
    ChunkMeta,
    SearchResult,
    _FIGURE_CONTENT_PATTERNS,
    _FIGURE_QUERY_RE,
    _GENERIC_KEYWORDS,
    _GENERIC_QUERY_PATTERNS,
    _IMAGE_QUERY_KEYWORDS,
    _RETRIEVAL_STOP_WORDS,
    _SUMMARY_QUERY_TERMS,
    _TABLE_CONTENT_PATTERNS,
    _TABLE_QUERY_KEYWORDS,
    _TABLE_QUERY_RE,
    _apply_chunk_overlap,
    _cosine_sim,
    _is_generic_summary_query,
    _tokenize_simple,
    prepare_search_inputs,
    split_text,
)
from backend.services.chat_file_search import (  # noqa: F401  (兼容 re-export)
    ChatFileIndex,
)
from backend.services.chat_file_index_manager import (  # noqa: F401  (兼容 re-export)
    ChatFileIndexManager,
    get_index_manager,
)
