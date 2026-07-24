"""知识库管理 API 路由"""

import asyncio
import hashlib
import json
from typing import Any, Dict, List, Literal, Optional

from fastapi import APIRouter, Depends, HTTPException, Query
from pydantic import BaseModel, Field
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from backend.api.dependencies.auth import ApiKeyAuth, get_db_session, require_admin
from backend.config.settings import settings
from backend.config.loggers import get_logger
from backend.db.models import (
    KnowledgeBase,
    KnowledgeBaseDocument,
    KnowledgeBaseStatus,
    SelfIngestChunk,
)
from backend.db.redis import get_redis
from backend.domain.knowledge_base.backend_policy import SELF_BACKEND
from backend.rag.knowledge_base import get_kb_manager

router = APIRouter(prefix="/knowledge_bases", tags=["知识库管理"])
logger = get_logger(__name__)

_GRAPH_CACHE_SCHEMA_VERSION = 7
_GRAPH_NODE_FETCH_BATCH_SIZE = 500
_NEBULA_V2_EDGE_VISIBLE_FILTER = (
    "coalesce(e.relation_v2.is_visible, true) == true "
    'AND coalesce(e.relation_v2.status, "weak_evidence") IN ["active", "weak_evidence"]'
)
class KnowledgeBaseCreateRequest(BaseModel):
    """创建知识库请求"""

    kb_code: str = Field(..., description="知识库编码", min_length=1, max_length=50)
    kb_name: str = Field(..., description="知识库名称", min_length=1, max_length=100)
    subject: Optional[str] = Field(None, description="学科分类", max_length=50)
    description: Optional[str] = Field(None, description="描述")
    owner_id: Optional[str] = Field(None, description="所有者 ID")
    is_default: bool = Field(False, description="是否为默认知识库")
    ingestion_backend: Literal["legacy_lightrag", "self_ingestion"] = Field(
        "legacy_lightrag",
        description="入库后端",
    )
    retrieval_backend: Literal["legacy_lightrag", "self_ingestion"] = Field(
        "legacy_lightrag",
        description="查询后端",
    )
    self_ingestion_enabled: bool = Field(False, description="是否允许自研入库")
    self_retriever_enabled: bool = Field(False, description="是否允许自研查询")


class KnowledgeBaseResponse(BaseModel):
    """知识库响应"""

    kb_id: str
    kb_code: str
    kb_name: str
    subject: Optional[str] = None
    description: Optional[str] = None
    owner_id: Optional[str] = None
    nebula_space: Optional[str] = None
    milvus_collection_prefix: Optional[str] = None
    redis_key_prefix: Optional[str] = None
    ingestion_backend: str = "legacy_lightrag"
    retrieval_backend: str = "legacy_lightrag"
    self_ingestion_enabled: bool = False
    self_retriever_enabled: bool = False
    doc_count: int = 0
    entity_count: int = 0
    relation_count: int = 0
    chunk_count: int = 0
    status: str
    is_default: bool = False
    created_at: str
    updated_at: str


class KnowledgeBaseListResponse(BaseModel):
    """知识库列表响应"""

    items: List[KnowledgeBaseResponse]
    total: int


class ExtractionSchemaOption(BaseModel):
    """自研入库可选抽取 Schema（决定 LLM 提示词与实体/关系类型）"""

    schema_id: str
    subject: str
    version: str
    schema_version: str
    label: str
    entity_type_count: int = 0
    relation_type_count: int = 0


class ExtractionSchemaDetail(ExtractionSchemaOption):
    """抽取 Schema 管理详情。"""

    raw_yaml: str


class ExtractionSchemaCreateRequest(BaseModel):
    """创建抽取 Schema。"""

    schema_id: str = Field(..., description="Schema ID，只允许小写字母、数字、下划线", min_length=2, max_length=64)
    label: str = Field(..., description="展示名称", min_length=1, max_length=80)
    subject: Optional[str] = Field(None, description="适用领域", max_length=80)
    version: str = Field("1.0", description="版本号", max_length=20)
    raw_yaml: Optional[str] = Field(None, description="完整 YAML；不传则生成基础模板")


class ExtractionSchemaUpdateRequest(BaseModel):
    """更新抽取 Schema。"""

    raw_yaml: str = Field(..., description="完整 YAML")


class DocumentResponse(BaseModel):
    """文档响应"""

    id: int
    kb_id: str
    doc_id: str
    file_name: str
    file_path: Optional[str] = None
    document_date: Optional[str] = None
    parse_status: int = 0
    storage_status: int = 0
    error_message: Optional[str] = None
    chunk_count: int = 0
    entity_count: int = 0
    relation_count: int = 0
    created_at: str
    updated_at: Optional[str] = None


class DocumentListResponse(BaseModel):
    """文档列表响应"""

    items: List[DocumentResponse]
    total: int


class KnowledgeGraphNodeResponse(BaseModel):
    """知识图谱节点响应"""

    id: str
    label: str
    entity_type: str = "unknown"
    description: str = ""
    source_id: str = ""
    file_path: str = ""
    degree: int = 0


class KnowledgeGraphEdgeResponse(BaseModel):
    """知识图谱关系响应"""

    source: str
    target: str
    relation_type: str = "related_to"
    description: str = ""
    source_id: str = ""
    weight: float = 1.0


class KnowledgeGraphResponse(BaseModel):
    """知识图谱可视化抽样响应"""

    kb_id: str
    kb_name: str
    total_nodes: int = 0
    total_edges: int = 0
    sampled_nodes: int = 0
    sampled_edges: int = 0
    truncated: bool = False
    cache_hit: bool = False
    cache_ttl_seconds: int = 0
    nodes: List[KnowledgeGraphNodeResponse]
    edges: List[KnowledgeGraphEdgeResponse]


def kb_to_response(kb: KnowledgeBase) -> KnowledgeBaseResponse:
    """将知识库对象转换为响应模型"""
    return KnowledgeBaseResponse(
        kb_id=kb.kb_id,
        kb_code=kb.kb_code,
        kb_name=kb.kb_name,
        subject=kb.subject,
        description=kb.description,
        owner_id=kb.owner_id,
        nebula_space=kb.nebula_space,
        milvus_collection_prefix=kb.milvus_collection_prefix,
        redis_key_prefix=kb.redis_key_prefix,
        ingestion_backend=getattr(kb, "ingestion_backend", None) or "legacy_lightrag",
        retrieval_backend=getattr(kb, "retrieval_backend", None) or "legacy_lightrag",
        self_ingestion_enabled=bool(getattr(kb, "self_ingestion_enabled", False)),
        self_retriever_enabled=bool(getattr(kb, "self_retriever_enabled", False)),
        doc_count=kb.doc_count,
        entity_count=kb.entity_count,
        relation_count=kb.relation_count,
        chunk_count=kb.chunk_count,
        status=kb.status.value if kb.status else "creating",
        is_default=kb.is_default,
        created_at=kb.created_at.isoformat() if kb.created_at else "",
        updated_at=kb.updated_at.isoformat() if kb.updated_at else "",
    )


def doc_to_response(doc: KnowledgeBaseDocument) -> DocumentResponse:
    """将文档对象转换为响应模型"""
    return DocumentResponse(
        id=doc.id,
        kb_id=doc.kb_id,
        doc_id=doc.doc_id,
        file_name=doc.file_name,
        file_path=doc.file_path,
        document_date=doc.document_date.isoformat() if doc.document_date else None,
        parse_status=doc.parse_status,
        storage_status=doc.storage_status,
        error_message=doc.error_message,
        chunk_count=doc.chunk_count or 0,
        entity_count=doc.entity_count or 0,
        relation_count=doc.relation_count or 0,
        created_at=doc.created_at.isoformat() if doc.created_at else "",
        updated_at=doc.updated_at.isoformat() if doc.updated_at else "",
    )


def _safe_nebula_string(value: Any) -> str:
    """Convert a Nebula value into a displayable string."""
    try:
        if value is None or value.is_empty():
            return ""
        if hasattr(value, "is_string") and value.is_string():
            return value.as_string()
        if hasattr(value, "is_int") and value.is_int():
            return str(value.as_int())
        if hasattr(value, "is_double") and value.is_double():
            return str(value.as_double())
        if hasattr(value, "is_bool") and value.is_bool():
            return str(value.as_bool())
    except Exception:
        return ""
    return ""


def _safe_nebula_float(value: Any, default: float = 1.0) -> float:
    try:
        if value is None or value.is_empty():
            return default
        if hasattr(value, "is_double") and value.is_double():
            return float(value.as_double())
        if hasattr(value, "is_int") and value.is_int():
            return float(value.as_int())
        text = _safe_nebula_string(value)
        return float(text) if text else default
    except Exception:
        return default


def _knowledge_graph_cache_key(kb: KnowledgeBase, node_limit: int, edge_limit: int) -> str:
    """Build a versioned key so graph mutations naturally stop hitting stale slices."""
    updated_at = getattr(kb, "updated_at", None)
    version_payload = ":".join(
        [
            str(getattr(kb, "kb_id", "")),
            str(getattr(kb, "nebula_space", "") or ""),
            str(getattr(kb, "ingestion_backend", "") or ""),
            str(getattr(kb, "retrieval_backend", "") or ""),
            updated_at.isoformat() if updated_at else "",
            str(getattr(kb, "entity_count", 0) or 0),
            str(getattr(kb, "relation_count", 0) or 0),
        ]
    )
    version = hashlib.sha1(version_payload.encode("utf-8")).hexdigest()[:16]
    namespace = getattr(kb, "redis_key_prefix", None) or getattr(kb, "kb_id", "unknown")
    return (
        f"{namespace}:admin:knowledge-graph:v{_GRAPH_CACHE_SCHEMA_VERSION}:"
        f"{version}:{node_limit}:{edge_limit}"
    )


async def _read_knowledge_graph_cache(cache_key: str) -> Optional[Dict[str, Any]]:
    if not settings.redis_host:
        return None
    client = get_redis()
    try:
        raw = await asyncio.wait_for(client.get(cache_key), timeout=0.8)
        if not raw:
            return None
        cached = await asyncio.to_thread(json.loads, raw)
        if not isinstance(cached, dict) or not isinstance(cached.get("nodes"), list):
            return None
        if not isinstance(cached.get("edges"), list):
            return None
        return cached
    except Exception as exc:
        logger.debug("[知识图谱] Redis 缓存读取失败: key=%s, error=%s", cache_key, exc)
        return None
    finally:
        await client.aclose()


async def _write_knowledge_graph_cache(cache_key: str, graph_slice: Dict[str, Any]) -> None:
    if not settings.redis_host:
        return
    ttl = max(30, int(settings.knowledge_graph_cache_ttl_seconds or 600))
    client = get_redis()
    try:
        payload = await asyncio.to_thread(
            json.dumps,
            graph_slice,
            ensure_ascii=False,
            separators=(",", ":"),
        )
        await asyncio.wait_for(client.setex(cache_key, ttl, payload), timeout=1.5)
    except Exception as exc:
        logger.debug("[知识图谱] Redis 缓存写入失败: key=%s, error=%s", cache_key, exc)
    finally:
        await client.aclose()


def _escape_nebula_vid(value: str) -> str:
    return value.replace("\\", "\\\\").replace('"', '\\"')


def _fetch_knowledge_graph_from_nebula(
    kb: KnowledgeBase,
    node_limit: int,
    edge_limit: int,
) -> Dict[str, Any]:
    """Fetch a bounded graph slice from NebulaGraph for admin visualization."""
    from nebula3.Config import Config
    from nebula3.gclient.net import ConnectionPool

    nodes: Dict[str, Dict[str, Any]] = {}
    selected_node_ids: Dict[str, None] = {}
    edges: List[Dict[str, Any]] = []
    edge_keys: set[tuple[str, str, str, str]] = set()
    degree: Dict[str, int] = {}

    config = Config()
    config.max_connection_pool_size = 4
    pool = ConnectionPool()
    ok = pool.init([(settings.nebula_host, settings.nebula_graph_port)], config)
    if not ok:
        raise RuntimeError("连接 NebulaGraph 失败")

    session = pool.get_session(settings.nebula_user, settings.nebula_password)
    try:
        result = session.execute(f"USE `{kb.nebula_space}`")
        if not result.is_succeeded():
            raise RuntimeError(f"NebulaGraph Space 不可用: {result.error_msg()}")

        if edge_limit > 0:
            edge_query = (
                "MATCH (src:entity)-[e:relation]->(dst:entity) "
                "RETURN id(src) AS src_id, id(dst) AS dst_id, "
                "e.relation_type AS relation_type, e.description AS rel_desc, "
                "e.source_id AS rel_source_id, e.weight AS weight "
                f"LIMIT {edge_limit}"
            )
            edge_result = session.execute(edge_query)
            if edge_result.is_succeeded():
                for i in range(edge_result.row_size()):
                    row = edge_result.row_values(i)
                    src_id = _safe_nebula_string(row[0])
                    dst_id = _safe_nebula_string(row[1])
                    if not src_id or not dst_id:
                        continue

                    if src_id not in selected_node_ids and len(selected_node_ids) < node_limit:
                        selected_node_ids[src_id] = None
                    if dst_id not in selected_node_ids and len(selected_node_ids) < node_limit:
                        selected_node_ids[dst_id] = None

                    if src_id in selected_node_ids and dst_id in selected_node_ids:
                        relation_type = _safe_nebula_string(row[2]) or "related_to"
                        relation_source_id = _safe_nebula_string(row[4])
                        edge_key = (src_id, dst_id, relation_type, relation_source_id)
                        if edge_key in edge_keys:
                            continue
                        edge_keys.add(edge_key)
                        degree[src_id] = degree.get(src_id, 0) + 1
                        degree[dst_id] = degree.get(dst_id, 0) + 1
                        edges.append(
                            {
                                "source": src_id,
                                "target": dst_id,
                                "relation_type": relation_type,
                                "description": _safe_nebula_string(row[3])[:500],
                                "source_id": relation_source_id,
                                "weight": _safe_nebula_float(row[5], 1.0),
                            }
                        )
            else:
                logger.warning("[知识图谱] 关系抽样查询失败: %s", edge_result.error_msg())

        # Edge rows no longer repeat large descriptions. Fetch selected vertex properties
        # once, in bounded native FETCH batches.
        selected_ids = list(selected_node_ids)
        for offset in range(0, len(selected_ids), _GRAPH_NODE_FETCH_BATCH_SIZE):
            batch = selected_ids[offset : offset + _GRAPH_NODE_FETCH_BATCH_SIZE]
            vids = ", ".join(f'"{_escape_nebula_vid(node_id)}"' for node_id in batch)
            node_result = session.execute(
                "FETCH PROP ON entity "
                f"{vids} "
                "YIELD properties(vertex).entity_id, properties(vertex).entity_type, "
                "properties(vertex).description, properties(vertex).source_id, "
                "properties(vertex).file_path"
            )
            if not node_result.is_succeeded():
                logger.warning("[知识图谱] 节点属性批量查询失败: %s", node_result.error_msg())
                continue
            for row_index in range(node_result.row_size()):
                row = node_result.row_values(row_index)
                entity_id = _safe_nebula_string(row[0])
                if not entity_id:
                    continue
                nodes[entity_id] = {
                    "id": entity_id,
                    "label": entity_id,
                    "entity_type": _safe_nebula_string(row[1]) or "unknown",
                    "description": _safe_nebula_string(row[2])[:500],
                    "source_id": _safe_nebula_string(row[3]),
                    "file_path": _safe_nebula_string(row[4]),
                }

        if len(nodes) < node_limit:
            remaining = node_limit - len(nodes)
            node_query = (
                "MATCH (n:entity) "
                "RETURN n.entity.entity_id AS entity_id, n.entity.entity_type AS entity_type, "
                "n.entity.description AS description, n.entity.source_id AS source_id, "
                "n.entity.file_path AS file_path "
                f"LIMIT {remaining + len(selected_node_ids)}"
            )
            node_result = session.execute(node_query)
            if node_result.is_succeeded():
                for i in range(node_result.row_size()):
                    if len(nodes) >= node_limit:
                        break
                    row = node_result.row_values(i)
                    entity_id = _safe_nebula_string(row[0])
                    if not entity_id or entity_id in nodes:
                        continue
                    selected_node_ids[entity_id] = None
                    nodes[entity_id] = {
                        "id": entity_id,
                        "label": entity_id,
                        "entity_type": _safe_nebula_string(row[1]) or "unknown",
                        "description": _safe_nebula_string(row[2])[:500],
                        "source_id": _safe_nebula_string(row[3]),
                        "file_path": _safe_nebula_string(row[4]),
                    }
            else:
                logger.warning("[知识图谱] 节点补充查询失败: %s", node_result.error_msg())

    finally:
        session.release()
        pool.close()

    for node_id, node in nodes.items():
        node["degree"] = degree.get(node_id, 0)

    # FETCH may encounter a legacy vertex without entity_id; never return dangling edges.
    edges = [edge for edge in edges if edge["source"] in nodes and edge["target"] in nodes]

    return {
        "nodes": list(nodes.values()),
        "edges": edges,
    }


def _fetch_self_graph_from_nebula_v2(
    kb: KnowledgeBase,
    node_limit: int,
    edge_limit: int,
) -> Dict[str, Any]:
    """Fetch self-ingestion graph_v2 from NebulaGraph entity_v2/relation_v2."""
    from nebula3.Config import Config
    from nebula3.gclient.net import ConnectionPool

    nodes: Dict[str, Dict[str, Any]] = {}
    selected_node_ids: Dict[str, None] = {}
    edges: List[Dict[str, Any]] = []
    edge_keys: set[tuple[str, str, str, str]] = set()
    degree: Dict[str, int] = {}

    config = Config()
    config.max_connection_pool_size = 4
    pool = ConnectionPool()
    ok = pool.init([(settings.nebula_host, settings.nebula_graph_port)], config)
    if not ok:
        raise RuntimeError("连接 NebulaGraph 失败")

    session = pool.get_session(settings.nebula_user, settings.nebula_password)
    try:
        result = session.execute(f"USE `{kb.nebula_space}`")
        if not result.is_succeeded():
            raise RuntimeError(f"NebulaGraph Space 不可用: {result.error_msg()}")

        if edge_limit > 0:
            edge_query = (
                "MATCH (src)-[e:relation_v2]->(dst) "
                f"WHERE {_NEBULA_V2_EDGE_VISIBLE_FILTER} "
                "RETURN id(src) AS src_id, id(dst) AS dst_id, "
                "e.relation_v2.relation_type AS relation_type, "
                "e.relation_v2.description AS rel_desc, "
                "e.relation_v2.evidence_chunk_ids AS evidence_chunk_ids, "
                "e.relation_v2.weight AS weight "
                f"LIMIT {edge_limit}"
            )
            edge_result = session.execute(edge_query)
            if edge_result.is_succeeded():
                for i in range(edge_result.row_size()):
                    row = edge_result.row_values(i)
                    src_id = _safe_nebula_string(row[0])
                    dst_id = _safe_nebula_string(row[1])
                    if not src_id or not dst_id:
                        continue
                    if src_id not in selected_node_ids and len(selected_node_ids) < node_limit:
                        selected_node_ids[src_id] = None
                    if dst_id not in selected_node_ids and len(selected_node_ids) < node_limit:
                        selected_node_ids[dst_id] = None
                    if src_id in selected_node_ids and dst_id in selected_node_ids:
                        relation_type = _safe_nebula_string(row[2]) or "related_to"
                        evidence_chunk_ids = _safe_nebula_string(row[4])
                        edge_key = (src_id, dst_id, relation_type, evidence_chunk_ids)
                        if edge_key in edge_keys:
                            continue
                        edge_keys.add(edge_key)
                        degree[src_id] = degree.get(src_id, 0) + 1
                        degree[dst_id] = degree.get(dst_id, 0) + 1
                        edges.append(
                            {
                                "source": src_id,
                                "target": dst_id,
                                "relation_type": relation_type,
                                "description": _safe_nebula_string(row[3])[:500],
                                "source_id": evidence_chunk_ids,
                                "weight": _safe_nebula_float(row[5], 1.0),
                            }
                        )
            else:
                logger.warning("[知识图谱] self v2 关系抽样查询失败: %s", edge_result.error_msg())

        selected_ids = list(selected_node_ids)
        for offset in range(0, len(selected_ids), _GRAPH_NODE_FETCH_BATCH_SIZE):
            batch = selected_ids[offset : offset + _GRAPH_NODE_FETCH_BATCH_SIZE]
            vids = ", ".join(f'"{_escape_nebula_vid(node_id)}"' for node_id in batch)
            node_result = session.execute(
                "FETCH PROP ON entity_v2 "
                f"{vids} "
                "YIELD properties(vertex).entity_id, properties(vertex).entity_name, "
                "properties(vertex).entity_type, properties(vertex).description, "
                "properties(vertex).source_id, properties(vertex).chunk_ids"
            )
            if not node_result.is_succeeded():
                logger.warning("[知识图谱] self v2 节点属性批量查询失败: %s", node_result.error_msg())
                continue
            for row_index in range(node_result.row_size()):
                row = node_result.row_values(row_index)
                entity_id = _safe_nebula_string(row[0])
                if not entity_id:
                    continue
                nodes[entity_id] = {
                    "id": entity_id,
                    "label": _safe_nebula_string(row[1]) or entity_id,
                    "entity_type": _safe_nebula_string(row[2]) or "term",
                    "description": _safe_nebula_string(row[3])[:500],
                    "source_id": _safe_nebula_string(row[4]),
                    "file_path": _safe_nebula_string(row[5]),
                }

        if len(nodes) < node_limit:
            remaining = node_limit - len(nodes)
            node_query = (
                "MATCH (n) "
                "WHERE n.entity_v2.is_visible == true "
                "AND n.entity_v2.entity_id IS NOT NULL "
                "RETURN n.entity_v2.entity_id AS entity_id, "
                "n.entity_v2.entity_name AS entity_name, "
                "n.entity_v2.entity_type AS entity_type, "
                "n.entity_v2.description AS description, "
                "n.entity_v2.source_id AS source_id, "
                "n.entity_v2.chunk_ids AS chunk_ids "
                f"LIMIT {remaining + len(selected_node_ids)}"
            )
            node_result = session.execute(node_query)
            if node_result.is_succeeded():
                for i in range(node_result.row_size()):
                    if len(nodes) >= node_limit:
                        break
                    row = node_result.row_values(i)
                    entity_id = _safe_nebula_string(row[0])
                    if not entity_id or entity_id in nodes:
                        continue
                    nodes[entity_id] = {
                        "id": entity_id,
                        "label": _safe_nebula_string(row[1]) or entity_id,
                        "entity_type": _safe_nebula_string(row[2]) or "term",
                        "description": _safe_nebula_string(row[3])[:500],
                        "source_id": _safe_nebula_string(row[4]),
                        "file_path": _safe_nebula_string(row[5]),
                    }
            else:
                logger.warning("[知识图谱] self v2 节点补充查询失败: %s", node_result.error_msg())
    finally:
        session.release()
        pool.close()

    for node_id, node in nodes.items():
        node["degree"] = degree.get(node_id, 0)
    edges = [edge for edge in edges if edge["source"] in nodes and edge["target"] in nodes]
    return {"nodes": list(nodes.values()), "edges": edges}


async def _fetch_self_chunk_graph_from_mysql(
    session: AsyncSession,
    kb_id: str,
    node_limit: int,
    edge_limit: int,
) -> Dict[str, Any]:
    """Fetch a v2 fallback graph from visible self-ingestion chunks."""

    stmt = (
        select(SelfIngestChunk)
        .where(
            SelfIngestChunk.kb_id == kb_id,
            SelfIngestChunk.is_active == True,
            SelfIngestChunk.is_visible == True,
            SelfIngestChunk.doc_status == "visible",
        )
        .order_by(SelfIngestChunk.doc_id, SelfIngestChunk.chunk_index)
        .limit(node_limit)
    )
    chunks = list((await session.execute(stmt)).scalars().all())
    nodes = [
        {
            "id": chunk.chunk_id,
            "label": f"{chunk.file_name}#{chunk.chunk_index}",
            "entity_type": "chunk_v2",
            "description": (chunk.content or "")[:500],
            "source_id": chunk.doc_id,
            "file_path": chunk.file_path or "",
            "degree": 0,
        }
        for chunk in chunks
    ]

    edges: List[Dict[str, Any]] = []
    by_doc: Dict[str, List[SelfIngestChunk]] = {}
    for chunk in chunks:
        by_doc.setdefault(chunk.doc_id, []).append(chunk)
    degree: Dict[str, int] = {}
    for doc_chunks in by_doc.values():
        doc_chunks.sort(key=lambda item: item.chunk_index)
        for src, dst in zip(doc_chunks, doc_chunks[1:]):
            if len(edges) >= edge_limit:
                break
            degree[src.chunk_id] = degree.get(src.chunk_id, 0) + 1
            degree[dst.chunk_id] = degree.get(dst.chunk_id, 0) + 1
            edges.append(
                {
                    "source": src.chunk_id,
                    "target": dst.chunk_id,
                    "relation_type": "next_chunk",
                    "description": "self_ingestion chunk sequence",
                    "source_id": src.doc_id,
                    "weight": 1.0,
                }
            )

    for node in nodes:
        node["degree"] = degree.get(node["id"], 0)
    return {"nodes": nodes, "edges": edges}


def _graph_slice_has_nodes(graph_slice: Dict[str, Any]) -> bool:
    return bool(graph_slice.get("nodes"))


async def _fetch_legacy_graph_slice(
    manager: Any,
    kb: KnowledgeBase,
    node_limit: int,
    edge_limit: int,
) -> Dict[str, Any]:
    """Fetch legacy/Nebula graph data for mixed-backend knowledge bases."""

    graph_slice: Dict[str, Any] = {"nodes": [], "edges": []}
    if kb.nebula_space:
        try:
            return await asyncio.to_thread(
                _fetch_knowledge_graph_from_nebula,
                kb,
                node_limit,
                edge_limit,
            )
        except Exception as exc:
            logger.warning(
                "[知识图谱] NebulaGraph 抽样失败，尝试 legacy 实体回退: kb_id=%s, error=%s",
                kb.kb_id,
                exc,
            )

    try:
        entities = await manager._fetch_entities_from_kb(kb)
        graph_slice["nodes"] = [
            {
                "id": item.entity_name,
                "label": item.entity_name,
                "entity_type": item.entity_type or "unknown",
                "description": (item.description or "")[:500],
                "source_id": item.source_id or "",
                "file_path": item.file_path or "",
                "degree": 0,
            }
            for item in entities[:node_limit]
        ]
    except Exception as exc:
        logger.warning("[知识图谱] legacy 实体抽样失败: kb_id=%s, error=%s", kb.kb_id, exc)
    return graph_slice


@router.post("", response_model=KnowledgeBaseResponse)
async def create_knowledge_base(
    request: KnowledgeBaseCreateRequest,
    auth: ApiKeyAuth = Depends(require_admin),
    session: AsyncSession = Depends(get_db_session),
):
    """创建知识库"""
    manager = get_kb_manager()
    try:
        kb = await manager.create_knowledge_base(
            session=session,
            kb_code=request.kb_code,
            kb_name=request.kb_name,
            subject=request.subject,
            description=request.description,
            owner_id=request.owner_id,
            is_default=request.is_default,
            ingestion_backend=request.ingestion_backend,
            retrieval_backend=request.retrieval_backend,
            self_ingestion_enabled=request.self_ingestion_enabled,
            self_retriever_enabled=request.self_retriever_enabled,
        )
        return kb_to_response(kb)
    except ValueError as e:
        raise HTTPException(status_code=400, detail=str(e))
    except RuntimeError:
        logger.exception("创建知识库失败")
        raise HTTPException(status_code=500, detail="创建知识库失败") from None


@router.get("", response_model=KnowledgeBaseListResponse)
async def list_knowledge_bases(
    status: Optional[str] = Query(None, description="状态过滤"),
    owner_id: Optional[str] = Query(None, description="所有者 ID"),
    q: Optional[str] = Query(None, description="名称、编码或学科搜索"),
    skip: int = Query(0, ge=0, description="跳过数量"),
    limit: int = Query(20, ge=1, le=100, description="返回数量"),
    auth: ApiKeyAuth = Depends(require_admin),
    session: AsyncSession = Depends(get_db_session),
):
    """列出知识库"""
    manager = get_kb_manager()
    
    status_enum = None
    if status:
        try:
            status_enum = KnowledgeBaseStatus(status)
        except ValueError:
            raise HTTPException(status_code=400, detail=f"无效的状态值: {status}")
    
    kbs, total = await manager.list_knowledge_bases(
        session=session,
        status=status_enum,
        owner_id=owner_id,
        query=q,
        skip=skip,
        limit=limit,
    )
    return KnowledgeBaseListResponse(
        items=[kb_to_response(kb) for kb in kbs],
        total=total,
    )


@router.get("/default", response_model=KnowledgeBaseResponse)
async def get_default_knowledge_base(
    auth: ApiKeyAuth = Depends(require_admin),
    session: AsyncSession = Depends(get_db_session),
):
    """获取默认知识库"""
    manager = get_kb_manager()
    kb = await manager.get_default_knowledge_base(session)
    if not kb:
        raise HTTPException(status_code=404, detail="默认知识库不存在")
    return kb_to_response(kb)


@router.get("/extraction_schemas", response_model=List[ExtractionSchemaOption])
async def list_extraction_schemas(
    auth: ApiKeyAuth = Depends(require_admin),
):
    """列出自研入库可用的抽取 Schema（入库提示词模板）。"""
    from backend.rag.ingestion.schema import list_extraction_schemas as _list_schemas

    schemas = await asyncio.to_thread(_list_schemas)
    return [ExtractionSchemaOption(**item) for item in schemas]


@router.post("/extraction_schemas", response_model=ExtractionSchemaDetail)
async def create_extraction_schema(
    payload: ExtractionSchemaCreateRequest,
    auth: ApiKeyAuth = Depends(require_admin),
):
    """新建自研入库抽取 Schema（入库提示词模板）。"""
    from backend.rag.ingestion.schema import (
        build_extraction_schema_yaml,
        create_extraction_schema as _create_schema,
    )

    raw_yaml = payload.raw_yaml or await asyncio.to_thread(
        build_extraction_schema_yaml,
        payload.schema_id,
        label=payload.label,
        subject=payload.subject,
        version=payload.version,
    )
    try:
        schema = await asyncio.to_thread(_create_schema, payload.schema_id, raw_yaml)
        return ExtractionSchemaDetail(**schema)
    except FileExistsError:
        raise HTTPException(status_code=409, detail="该 Schema ID 已存在") from None
    except ValueError as exc:
        raise HTTPException(status_code=400, detail=str(exc)) from exc


@router.get("/extraction_schemas/{schema_id}", response_model=ExtractionSchemaDetail)
async def get_extraction_schema(
    schema_id: str,
    auth: ApiKeyAuth = Depends(require_admin),
):
    """获取抽取 Schema 的完整 YAML。"""
    from backend.rag.ingestion.schema import get_extraction_schema as _get_schema

    try:
        schema = await asyncio.to_thread(_get_schema, schema_id)
        return ExtractionSchemaDetail(**schema)
    except FileNotFoundError:
        raise HTTPException(status_code=404, detail="Schema 不存在") from None
    except ValueError as exc:
        raise HTTPException(status_code=400, detail=str(exc)) from exc


@router.put("/extraction_schemas/{schema_id}", response_model=ExtractionSchemaDetail)
async def update_extraction_schema(
    schema_id: str,
    payload: ExtractionSchemaUpdateRequest,
    auth: ApiKeyAuth = Depends(require_admin),
):
    """更新抽取 Schema 的完整 YAML。"""
    from backend.rag.ingestion.schema import update_extraction_schema as _update_schema

    try:
        schema = await asyncio.to_thread(_update_schema, schema_id, payload.raw_yaml)
        return ExtractionSchemaDetail(**schema)
    except FileNotFoundError:
        raise HTTPException(status_code=404, detail="Schema 不存在") from None
    except ValueError as exc:
        raise HTTPException(status_code=400, detail=str(exc)) from exc


@router.get("/{kb_id}", response_model=KnowledgeBaseResponse)
async def get_knowledge_base(
    kb_id: str,
    auth: ApiKeyAuth = Depends(require_admin),
    session: AsyncSession = Depends(get_db_session),
):
    """获取知识库详情"""
    manager = get_kb_manager()
    kb = await manager.get_knowledge_base(session, kb_id)
    if not kb:
        raise HTTPException(status_code=404, detail="知识库不存在")
    return kb_to_response(kb)


@router.get("/{kb_id}/graph", response_model=KnowledgeGraphResponse)
async def get_knowledge_base_graph(
    kb_id: str,
    node_limit: int = Query(1000, ge=20, le=2000, description="最多返回节点数"),
    edge_limit: int = Query(3000, ge=0, le=6000, description="最多返回关系数"),
    refresh: bool = Query(False, description="跳过 Redis 缓存并重新查询图谱"),
    auth: ApiKeyAuth = Depends(require_admin),
    session: AsyncSession = Depends(get_db_session),
):
    """获取知识库知识图谱可视化切片。"""
    manager = get_kb_manager()
    kb = await manager.get_knowledge_base(session, kb_id)
    if not kb:
        raise HTTPException(status_code=404, detail="知识库不存在")

    graph_slice: Dict[str, Any] = {"nodes": [], "edges": []}
    cache_key = _knowledge_graph_cache_key(kb, node_limit, edge_limit)
    cache_hit = False

    if not refresh:
        cached_slice = await _read_knowledge_graph_cache(cache_key)
        if cached_slice is not None:
            graph_slice = cached_slice
            cache_hit = True

    graph_backend = getattr(kb, "retrieval_backend", None) or getattr(kb, "ingestion_backend", None)
    ingestion_backend = getattr(kb, "ingestion_backend", None)

    if not cache_hit and (graph_backend in {SELF_BACKEND, "hybrid"} or ingestion_backend == SELF_BACKEND):
        if kb.nebula_space:
            try:
                graph_slice = await asyncio.to_thread(
                    _fetch_self_graph_from_nebula_v2,
                    kb,
                    node_limit,
                    edge_limit,
                )
            except Exception as exc:
                logger.warning(
                    "[知识图谱] self v2 NebulaGraph 抽样失败: kb_id=%s, error=%s",
                    kb.kb_id,
                    exc,
                )
        if not _graph_slice_has_nodes(graph_slice):
            graph_slice = await _fetch_self_chunk_graph_from_mysql(session, kb_id, node_limit, edge_limit)
    elif not cache_hit and kb.nebula_space:
        graph_slice = await _fetch_legacy_graph_slice(
            manager,
            kb,
            node_limit,
            edge_limit,
        )
    elif not cache_hit:
        graph_slice = await _fetch_legacy_graph_slice(
            manager,
            kb,
            node_limit,
            edge_limit,
        )

    if not cache_hit:
        await _write_knowledge_graph_cache(cache_key, graph_slice)

    nodes = graph_slice.get("nodes", [])
    edges = graph_slice.get("edges", [])

    return KnowledgeGraphResponse(
        kb_id=kb.kb_id,
        kb_name=kb.kb_name,
        total_nodes=kb.entity_count or 0,
        total_edges=kb.relation_count or 0,
        sampled_nodes=len(nodes),
        sampled_edges=len(edges),
        truncated=(kb.entity_count or 0) > len(nodes) or (kb.relation_count or 0) > len(edges),
        cache_hit=cache_hit,
        cache_ttl_seconds=max(30, int(settings.knowledge_graph_cache_ttl_seconds or 600)),
        nodes=[KnowledgeGraphNodeResponse(**node) for node in nodes],
        edges=[KnowledgeGraphEdgeResponse(**edge) for edge in edges],
    )


class KnowledgeBaseUpdateRequest(BaseModel):
    """更新知识库请求"""

    kb_name: Optional[str] = Field(None, description="知识库名称", max_length=100)
    subject: Optional[str] = Field(None, description="学科分类", max_length=50)
    description: Optional[str] = Field(None, description="描述")
    ingestion_backend: Optional[Literal["legacy_lightrag", "self_ingestion"]] = Field(
        None,
        description="入库后端",
    )
    retrieval_backend: Optional[Literal["legacy_lightrag", "self_ingestion"]] = Field(
        None,
        description="查询后端",
    )
    self_ingestion_enabled: Optional[bool] = Field(None, description="是否允许自研入库")
    self_retriever_enabled: Optional[bool] = Field(None, description="是否允许自研查询")


@router.put("/{kb_id}", response_model=KnowledgeBaseResponse)
async def update_knowledge_base(
    kb_id: str,
    request: KnowledgeBaseUpdateRequest,
    auth: ApiKeyAuth = Depends(require_admin),
    session: AsyncSession = Depends(get_db_session),
):
    """更新知识库信息"""
    from backend.db.crud.knowledge_base_crud import KnowledgeBaseCRUD
    
    crud = KnowledgeBaseCRUD()
    kb = await crud.get_by_id(session, kb_id)
    if not kb:
        raise HTTPException(status_code=404, detail="知识库不存在")
    
    update_data = request.model_dump(exclude_unset=True)
    if not update_data:
        return kb_to_response(kb)
    
    try:
        updated_kb = await crud.update(session, kb_id, **update_data)
    except ValueError as e:
        raise HTTPException(status_code=400, detail=str(e))
    logger.info(f"知识库更新成功: {kb_id} by user {auth.user_id}")
    return kb_to_response(updated_kb)


@router.delete("/{kb_id}")
async def delete_knowledge_base(
    kb_id: str,
    auth: ApiKeyAuth = Depends(require_admin),
    session: AsyncSession = Depends(get_db_session),
):
    """删除知识库"""
    manager = get_kb_manager()
    try:
        success = await manager.delete_knowledge_base(session, kb_id)
        if not success:
            raise HTTPException(status_code=404, detail="知识库不存在")
        return {"success": True, "message": f"知识库 {kb_id} 已删除"}
    except ValueError as e:
        raise HTTPException(status_code=400, detail=str(e))


@router.post("/{kb_id}/switch", response_model=KnowledgeBaseResponse)
async def switch_knowledge_base(
    kb_id: str,
    auth: ApiKeyAuth = Depends(require_admin),
    session: AsyncSession = Depends(get_db_session),
):
    """切换当前知识库"""
    manager = get_kb_manager()
    try:
        kb = await manager.switch_knowledge_base(session, kb_id)
        return kb_to_response(kb)
    except ValueError as e:
        raise HTTPException(status_code=400, detail=str(e))


@router.post("/{kb_id}/stats", response_model=KnowledgeBaseResponse)
async def update_knowledge_base_stats(
    kb_id: str,
    auth: ApiKeyAuth = Depends(require_admin),
    session: AsyncSession = Depends(get_db_session),
):
    """更新知识库统计信息"""
    manager = get_kb_manager()
    kb = await manager.update_knowledge_base_stats(session, kb_id)
    if not kb:
        raise HTTPException(status_code=404, detail="知识库不存在")
    return kb_to_response(kb)
