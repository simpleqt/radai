"""
关键词索引数据模型
"""

from dataclasses import dataclass, field
from datetime import datetime
from enum import Enum
from typing import Dict, List, Optional


class ExtractionMode(Enum):
    """关键词提取模式"""
    VECTOR = "vector"
    BM25 = "bm25"
    GRAPH = "graph"
    HYBRID = "hybrid"


class QueryType(Enum):
    """查询意图类型"""
    GENERAL = "general"
    PARAMETER = "parameter"
    CONCEPT = "concept"
    EQUIPMENT = "equipment"
    COMPARISON = "comparison"


@dataclass
class EntityData:
    """实体数据"""
    entity_name: str
    entity_type: str
    description: str
    source_id: str
    file_path: str


@dataclass
class IndexBuildResult:
    """索引构建结果"""
    entity_count: int = 0
    vector_count: int = 0
    bm25_index_size: int = 0
    build_time_ms: float = 0.0
    status: str = "pending"
    error: Optional[str] = None


@dataclass
class KeywordExtractionResult:
    """关键词提取结果"""
    high_level_keywords: List[str] = field(default_factory=list)
    low_level_keywords: List[str] = field(default_factory=list)
    entity_scores: Dict[str, float] = field(default_factory=dict)
    weighted_seed_scores: Dict[str, float] = field(default_factory=dict)
    extraction_mode: ExtractionMode = ExtractionMode.HYBRID
    query_type: QueryType = QueryType.GENERAL
    extraction_time_ms: float = 0.0
    kb_code: str = ""
    kb_id: str = ""


@dataclass
class KeywordIndexStats:
    """关键词索引统计信息"""
    kb_id: str
    kb_code: str
    entity_count: int = 0
    vector_count: int = 0
    bm25_doc_count: int = 0
    bm25_term_count: int = 0
    last_updated: Optional[datetime] = None
    index_size_mb: float = 0.0
    status: str = "not_built"


@dataclass
class VectorSearchResult:
    """向量搜索结果"""
    entity_name: str
    score: float
    entity_type: Optional[str] = None
    description: Optional[str] = None


@dataclass
class BM25SearchResult:
    """BM25搜索结果"""
    entity_name: str
    score: float
    doc_index: int = 0


@dataclass
class HybridSearchResult:
    """混合搜索结果"""
    entity_name: str
    vector_score: float = 0.0
    bm25_score: float = 0.0
    rrf_score: float = 0.0
    final_score: float = 0.0
