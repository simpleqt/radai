# 任务二：NebulaGraph 存储实现

> **文档状态**：📦 任务归档。现行实现见 `backend/rag/storages/nebula_storage.py`、[docs/知识库算法和架构.md](../docs/知识库算法和架构.md) §6。

## 任务概述
实现 LightRAG 的 `BaseGraphStorage` 接口，使用 NebulaGraph 作为图数据库存储后端，支持多知识库物理隔离。

## 前置条件
- 任务一（数据库设计）完成
- NebulaGraph 服务可用 (`<minio_server_ip>:9669`)
- `nebula3-python` 库已安装

## 现有架构分析

### LightRAG 存储架构
项目使用 LightRAG 框架，通过配置参数选择存储后端：

```python
# backend/rag/service.py (第534行)
if os.getenv("NEO4J_URI"):
    lightrag_kwargs.update({"graph_storage": "Neo4JStorage"})
```

### BaseGraphStorage 接口方法

| 方法 | 说明 |
|-----|------|
| `initialize()` | 初始化存储 |
| `finalize()` | 关闭存储 |
| `drop()` | 删除存储 |
| `has_node(node_id)` | 检查节点是否存在 |
| `has_edge(src_id, tgt_id)` | 检查边是否存在 |
| `node_degree(node_id)` | 获取节点度数 |
| `node_degrees_batch(node_ids)` | 批量获取节点度数 |
| `edge_degree(src_id, tgt_id)` | 获取边的度数 |
| `edge_degrees_batch(edges)` | 批量获取边的度数 |
| `get_node(node_id)` | 获取节点属性 |
| `get_nodes_batch(node_ids)` | 批量获取节点 |
| `get_edge(src_id, tgt_id)` | 获取边属性 |
| `get_edges_batch(edges)` | 批量获取边 |
| `get_node_edges(node_id)` | 获取节点的所有边 |
| `get_nodes_edges_batch(node_ids)` | 批量获取节点的边 |
| `upsert_node(node_id, node_data)` | 插入或更新节点 |
| `upsert_edge(src_id, tgt_id, edge_data)` | 插入或更新边 |
| `delete_node(node_id)` | 删除节点 |
| `remove_nodes(nodes)` | 批量删除节点 |
| `remove_edges(edges)` | 批量删除边 |
| `get_all_labels()` | 获取所有标签 |
| `get_knowledge_graph(node_label, max_depth, max_nodes)` | 获取知识图谱子图 |
| `get_all_nodes()` | 获取所有节点 |
| `get_all_edges()` | 获取所有边 |
| `get_popular_labels(limit)` | 获取热门标签 |
| `search_labels(query, limit)` | 搜索标签 |
| `index_done_callback()` | 索引完成回调 |

### Neo4JStorage 构造函数签名

```python
def __init__(self, namespace, global_config, embedding_func, workspace=None):
    """
    Args:
        namespace: 命名空间（用于集合命名）
        global_config: 全局配置字典
        embedding_func: 嵌入函数
        workspace: 工作空间名称（用于多知识库隔离）
    """
```

---

## 任务详情

### 1. 创建存储模块目录

**文件位置**: `backend/rag/storages/__init__.py` (新建)

```python
"""
RAG 存储后端模块

支持的图存储后端（LightRAG 上游可选；**本项目生产仅用 NebulaGraph**）：
- Neo4JStorage: Neo4j（迁移前背景）
- NebulaGraphStorage: NebulaGraph（多知识库，现行默认）
"""

from backend.rag.storages.nebula_storage import NebulaGraphStorage

__all__ = ["NebulaGraphStorage"]
```

### 2. 实现 NebulaGraphStorage 类

**文件位置**: `backend/rag/storages/nebula_storage.py` (新建)

```python
"""
NebulaGraph 存储实现

实现 LightRAG 的 BaseGraphStorage 接口，支持多知识库物理隔离。
每个 workspace 对应一个 NebulaGraph Space。
"""

import asyncio
import os
from dataclasses import dataclass, field
from datetime import datetime
from typing import Any, Dict, List, Optional, Tuple

from lightrag.base import BaseGraphStorage, KnowledgeGraph
from lightrag.utils import logger

try:
    from nebula3.gclient.net import ConnectionPool
    from nebula3.Config import Config as NebulaConfig
    NEBULA_AVAILABLE = True
except ImportError:
    NEBULA_AVAILABLE = False
    logger.warning("nebula3-python not installed, NebulaGraph storage unavailable")


@dataclass
class NebulaGraphStorage(BaseGraphStorage):
    """
    NebulaGraph 存储实现
    
    每个 workspace 对应一个 NebulaGraph Space
    实现完全物理隔离的多知识库支持
    
    Attributes:
        namespace: 命名空间（用于集合命名）
        global_config: 全局配置字典
        embedding_func: 嵌入函数（NebulaGraph 暂不使用，但接口需要）
        workspace: 工作空间名称，对应 NebulaGraph Space 名称
    """
    
    namespace: str = field(default="base")
    global_config: Dict[str, Any] = field(default_factory=dict)
    embedding_func: Any = field(default=None)
    workspace: str = field(default="base")
    
    _pool: Optional[ConnectionPool] = field(default=None, init=False)
    _host: str = field(default="", init=False)
    _port: int = field(default=9669, init=False)
    _user: str = field(default="root", init=False)
    _password: str = field(default="", init=False)
    _space: str = field(default="", init=False)
    _initialized: bool = field(default=False, init=False)
    
    def __post_init__(self):
        if not NEBULA_AVAILABLE:
            raise ImportError(
                "nebula3-python is required for NebulaGraph storage. "
                "Install it with: pip install nebula3-python"
            )
        
        self._host = os.getenv("NEBULA_HOST", "localhost")
        self._port = int(os.getenv("NEBULA_GRAPH_PORT", "9669"))
        self._user = os.getenv("NEBULA_USER", "root")
        self._password = os.getenv("NEBULA_PASSWORD", "")
        self._space = self.workspace or self.namespace
    
    async def initialize(self):
        """初始化连接池和 Space"""
        if self._initialized:
            return
        
        config = NebulaConfig()
        config.max_connection_pool_size = int(
            os.getenv("NEBULA_MAX_CONNECTION_POOL_SIZE", "100")
        )
        
        self._pool = ConnectionPool()
        ok = self._pool.init([(self._host, self._port)], config)
        if not ok:
            raise RuntimeError(
                f"Failed to connect to NebulaGraph at {self._host}:{self._port}"
            )
        
        await self._ensure_space_and_schema()
        self._initialized = True
        logger.info(f"[{self._space}] NebulaGraph storage initialized")
    
    async def _ensure_space_and_schema(self):
        """确保 Space 和 Schema 存在"""
        session = self._pool.get_session(self._user, self._password)
        try:
            # 创建 Space
            session.execute(
                f'CREATE SPACE IF NOT EXISTS `{self._space}` (vid_type=FIXED_STRING(256))'
            )
            logger.debug(f"[{self._space}] Space created or verified")
        except Exception as e:
            if "Existed" not in str(e):
                logger.warning(f"[{self._space}] Space creation warning: {e}")
        finally:
            session.release()
        
        await self._ensure_schema()
    
    async def _ensure_schema(self):
        """确保 Schema 存在"""
        schema_statements = [
            f'USE `{self._space}`',
            '''
            CREATE TAG IF NOT EXISTS entity (
                entity_id STRING,
                entity_type STRING,
                description STRING,
                source_id STRING,
                file_path STRING,
                created_at TIMESTAMP
            )
            ''',
            '''
            CREATE EDGE IF NOT EXISTS relation (
                relation_type STRING,
                description STRING,
                source_id STRING,
                file_path STRING,
                weight DOUBLE,
                created_at TIMESTAMP
            )
            ''',
            'CREATE TAG INDEX IF NOT EXISTS entity_id_idx ON entity(entity_id)',
            'CREATE TAG INDEX IF NOT EXISTS entity_type_idx ON entity(entity_type)',
            'CREATE EDGE INDEX IF NOT EXISTS relation_source_idx ON relation(source_id)',
        ]
        
        session = self._pool.get_session(self._user, self._password)
        try:
            for stmt in schema_statements:
                try:
                    session.execute(stmt)
                except Exception as e:
                    if "Existed" not in str(e):
                        logger.debug(f"Schema statement warning: {e}")
            logger.debug(f"[{self._space}] Schema verified")
        finally:
            session.release()
    
    async def finalize(self):
        """关闭连接池"""
        if self._pool:
            self._pool.close()
            self._pool = None
            self._initialized = False
            logger.info(f"[{self._space}] NebulaGraph storage finalized")
    
    async def drop(self):
        """删除 Space（危险操作）"""
        if not self._pool:
            return
        
        session = self._pool.get_session(self._user, self._password)
        try:
            session.execute(f'DROP SPACE IF EXISTS `{self._space}`')
            logger.info(f"[{self._space}] Space dropped")
        finally:
            session.release()
    
    def _get_session(self):
        """获取会话"""
        if not self._pool:
            raise RuntimeError("NebulaGraph not initialized")
        return self._pool.get_session(self._user, self._password)
    
    def _escape_string(self, s: str) -> str:
        """转义字符串中的特殊字符"""
        if s is None:
            return ""
        return s.replace("\\", "\\\\").replace('"', '\\"')
    
    # ==================== 节点操作 ====================
    
    async def has_node(self, node_id: str) -> bool:
        """检查节点是否存在"""
        session = self._get_session()
        try:
            session.execute(f'USE `{self._space}`')
            result = session.execute(
                f'MATCH (n:entity) WHERE n.entity_id == "{self._escape_string(node_id)}" '
                f'RETURN count(n) AS cnt'
            )
            if result.is_succeeded() and result.row_size() > 0:
                return result.row_values(0)[0].as_int() > 0
            return False
        finally:
            session.release()
    
    async def get_node(self, node_id: str) -> Optional[Dict[str, str]]:
        """获取节点属性"""
        session = self._get_session()
        try:
            session.execute(f'USE `{self._space}`')
            result = session.execute(
                f'MATCH (n:entity) WHERE n.entity_id == "{self._escape_string(node_id)}" '
                f'RETURN n.entity_id, n.entity_type, n.description, n.source_id, n.file_path'
            )
            if result.is_succeeded() and result.row_size() > 0:
                row = result.row_values(0)
                return {
                    "entity_id": row[0].as_string() if not row[0].is_empty() else "",
                    "entity_type": row[1].as_string() if not row[1].is_empty() else "",
                    "description": row[2].as_string() if not row[2].is_empty() else "",
                    "source_id": row[3].as_string() if not row[3].is_empty() else "",
                    "file_path": row[4].as_string() if not row[4].is_empty() else "",
                }
            return None
        finally:
            session.release()
    
    async def get_nodes_batch(self, node_ids: List[str]) -> Dict[str, Dict[str, str]]:
        """批量获取节点"""
        results = {}
        for node_id in node_ids:
            node = await self.get_node(node_id)
            if node:
                results[node_id] = node
        return results
    
    async def upsert_node(self, node_id: str, node_data: Dict[str, str]) -> None:
        """插入或更新节点"""
        session = self._get_session()
        try:
            session.execute(f'USE `{self._space}`')
            
            entity_type = self._escape_string(node_data.get("entity_type", "unknown"))
            description = self._escape_string(node_data.get("description", ""))
            source_id = self._escape_string(node_data.get("source_id", ""))
            file_path = self._escape_string(node_data.get("file_path", ""))
            
            nGQL = f'''
            INSERT VERTEX entity (
                entity_id, entity_type, description, source_id, file_path, created_at
            ) VALUES (
                "{self._escape_string(node_id)}": (
                    "{self._escape_string(node_id)}",
                    "{entity_type}",
                    "{description}",
                    "{source_id}",
                    "{file_path}",
                    timestamp()
                )
            )
            '''
            session.execute(nGQL)
        finally:
            session.release()
    
    async def delete_node(self, node_id: str) -> None:
        """删除节点"""
        session = self._get_session()
        try:
            session.execute(f'USE `{self._space}`')
            # 先删除相关的边
            session.execute(
                f'MATCH (n:entity)-[r]-(m) WHERE n.entity_id == "{self._escape_string(node_id)}" '
                f'DELETE r'
            )
            # 再删除节点
            session.execute(
                f'MATCH (n:entity) WHERE n.entity_id == "{self._escape_string(node_id)}" '
                f'DELETE n'
            )
        finally:
            session.release()
    
    async def remove_nodes(self, nodes: List[str]) -> None:
        """批量删除节点"""
        for node_id in nodes:
            await self.delete_node(node_id)
    
    async def node_degree(self, node_id: str) -> int:
        """获取节点度数"""
        session = self._get_session()
        try:
            session.execute(f'USE `{self._space}`')
            result = session.execute(
                f'MATCH (n:entity)-[r]-() WHERE n.entity_id == "{self._escape_string(node_id)}" '
                f'RETURN count(r) AS degree'
            )
            if result.is_succeeded() and result.row_size() > 0:
                return result.row_values(0)[0].as_int()
            return 0
        finally:
            session.release()
    
    async def node_degrees_batch(self, node_ids: List[str]) -> Dict[str, int]:
        """批量获取节点度数"""
        results = {}
        for node_id in node_ids:
            results[node_id] = await self.node_degree(node_id)
        return results
    
    # ==================== 边操作 ====================
    
    async def has_edge(self, source_node_id: str, target_node_id: str) -> bool:
        """检查边是否存在"""
        session = self._get_session()
        try:
            session.execute(f'USE `{self._space}`')
            result = session.execute(
                f'MATCH (s:entity)-[r:relation]->(t:entity) '
                f'WHERE s.entity_id == "{self._escape_string(source_node_id)}" '
                f'AND t.entity_id == "{self._escape_string(target_node_id)}" '
                f'RETURN count(r) AS cnt'
            )
            if result.is_succeeded() and result.row_size() > 0:
                return result.row_values(0)[0].as_int() > 0
            return False
        finally:
            session.release()
    
    async def get_edge(self, source_node_id: str, target_node_id: str) -> Optional[Dict[str, str]]:
        """获取边属性"""
        session = self._get_session()
        try:
            session.execute(f'USE `{self._space}`')
            result = session.execute(
                f'MATCH (s:entity)-[r:relation]->(t:entity) '
                f'WHERE s.entity_id == "{self._escape_string(source_node_id)}" '
                f'AND t.entity_id == "{self._escape_string(target_node_id)}" '
                f'RETURN r.relation_type, r.description, r.source_id, r.weight'
            )
            if result.is_succeeded() and result.row_size() > 0:
                row = result.row_values(0)
                return {
                    "relation_type": row[0].as_string() if not row[0].is_empty() else "",
                    "description": row[1].as_string() if not row[1].is_empty() else "",
                    "source_id": row[2].as_string() if not row[2].is_empty() else "",
                    "weight": str(row[3].as_double()) if not row[3].is_empty() else "1.0",
                }
            return None
        finally:
            session.release()
    
    async def get_edges_batch(self, edges: List[Tuple[str, str]]) -> Dict[Tuple[str, str], Dict[str, str]]:
        """批量获取边"""
        results = {}
        for src_id, tgt_id in edges:
            edge = await self.get_edge(src_id, tgt_id)
            if edge:
                results[(src_id, tgt_id)] = edge
        return results
    
    async def upsert_edge(self, source_node_id: str, target_node_id: str, edge_data: Dict[str, str]) -> None:
        """插入或更新边"""
        session = self._get_session()
        try:
            session.execute(f'USE `{self._space}`')
            
            relation_type = self._escape_string(edge_data.get("relation_type", "related_to"))
            description = self._escape_string(edge_data.get("description", ""))
            source_id = self._escape_string(edge_data.get("source_id", ""))
            file_path = self._escape_string(edge_data.get("file_path", ""))
            weight = float(edge_data.get("weight", "1.0"))
            
            nGQL = f'''
            INSERT EDGE relation (
                relation_type, description, source_id, file_path, weight, created_at
            ) VALUES (
                "{self._escape_string(source_node_id)}"->"{self._escape_string(target_node_id)}": (
                    "{relation_type}",
                    "{description}",
                    "{source_id}",
                    "{file_path}",
                    {weight},
                    timestamp()
                )
            )
            '''
            session.execute(nGQL)
        finally:
            session.release()
    
    async def remove_edges(self, edges: List[Tuple[str, str]]) -> None:
        """批量删除边"""
        session = self._get_session()
        try:
            session.execute(f'USE `{self._space}`')
            for src_id, tgt_id in edges:
                session.execute(
                    f'MATCH (s:entity)-[r:relation]->(t:entity) '
                    f'WHERE s.entity_id == "{self._escape_string(src_id)}" '
                    f'AND t.entity_id == "{self._escape_string(tgt_id)}" '
                    f'DELETE r'
                )
        finally:
            session.release()
    
    async def edge_degree(self, src_id: str, tgt_id: str) -> int:
        """获取边的度数（两端节点度数之和）"""
        src_degree = await self.node_degree(src_id)
        tgt_degree = await self.node_degree(tgt_id)
        return src_degree + tgt_degree
    
    async def edge_degrees_batch(self, edges: List[Tuple[str, str]]) -> Dict[Tuple[str, str], int]:
        """批量获取边的度数"""
        results = {}
        for src_id, tgt_id in edges:
            results[(src_id, tgt_id)] = await self.edge_degree(src_id, tgt_id)
        return results
    
    async def get_node_edges(self, source_node_id: str) -> List[Tuple[str, str]]:
        """获取节点的所有边"""
        session = self._get_session()
        try:
            session.execute(f'USE `{self._space}`')
            result = session.execute(
                f'MATCH (s:entity)-[r:relation]-(t:entity) '
                f'WHERE s.entity_id == "{self._escape_string(source_node_id)}" '
                f'RETURN t.entity_id'
            )
            edges = []
            if result.is_succeeded():
                for i in range(result.row_size()):
                    target_id = result.row_values(i)[0].as_string()
                    edges.append((source_node_id, target_id))
            return edges
        finally:
            session.release()
    
    async def get_nodes_edges_batch(self, node_ids: List[str]) -> Dict[str, List[Tuple[str, str]]]:
        """批量获取节点的边"""
        results = {}
        for node_id in node_ids:
            results[node_id] = await self.get_node_edges(node_id)
        return results
    
    # ==================== 图查询操作 ====================
    
    async def get_all_labels(self) -> List[str]:
        """获取所有标签（实体类型）"""
        session = self._get_session()
        try:
            session.execute(f'USE `{self._space}`')
            result = session.execute(
                f'MATCH (n:entity) RETURN DISTINCT n.entity_type AS type'
            )
            labels = []
            if result.is_succeeded():
                for i in range(result.row_size()):
                    label = result.row_values(i)[0].as_string()
                    if label:
                        labels.append(label)
            return sorted(labels)
        finally:
            session.release()
    
    async def get_all_nodes(self) -> List[Dict]:
        """获取所有节点"""
        session = self._get_session()
        try:
            session.execute(f'USE `{self._space}`')
            result = session.execute(
                f'MATCH (n:entity) RETURN n.entity_id, n.entity_type, n.description, n.source_id, n.file_path'
            )
            nodes = []
            if result.is_succeeded():
                for i in range(result.row_size()):
                    row = result.row_values(i)
                    nodes.append({
                        "entity_id": row[0].as_string() if not row[0].is_empty() else "",
                        "entity_type": row[1].as_string() if not row[1].is_empty() else "",
                        "description": row[2].as_string() if not row[2].is_empty() else "",
                        "source_id": row[3].as_string() if not row[3].is_empty() else "",
                        "file_path": row[4].as_string() if not row[4].is_empty() else "",
                    })
            return nodes
        finally:
            session.release()
    
    async def get_all_edges(self) -> List[Dict]:
        """获取所有边"""
        session = self._get_session()
        try:
            session.execute(f'USE `{self._space}`')
            result = session.execute(
                f'MATCH (s:entity)-[r:relation]->(t:entity) '
                f'RETURN s.entity_id, t.entity_id, r.relation_type, r.description, r.weight'
            )
            edges = []
            if result.is_succeeded():
                for i in range(result.row_size()):
                    row = result.row_values(i)
                    edges.append({
                        "src_id": row[0].as_string() if not row[0].is_empty() else "",
                        "tgt_id": row[1].as_string() if not row[1].is_empty() else "",
                        "relation_type": row[2].as_string() if not row[2].is_empty() else "",
                        "description": row[3].as_string() if not row[3].is_empty() else "",
                        "weight": row[4].as_double() if not row[4].is_empty() else 1.0,
                    })
            return edges
        finally:
            session.release()
    
    async def get_knowledge_graph(
        self, 
        node_label: str, 
        max_depth: int = 3, 
        max_nodes: int = 1000
    ) -> KnowledgeGraph:
        """获取知识图谱子图"""
        session = self._get_session()
        try:
            session.execute(f'USE `{self._space}`')
            
            # 构建查询条件
            if node_label == "*":
                where_clause = ""
            else:
                where_clause = f'WHERE n.entity_type == "{self._escape_string(node_label)}"'
            
            # 获取节点
            nodes_result = session.execute(
                f'MATCH (n:entity) {where_clause} '
                f'RETURN n.entity_id, n.entity_type, n.description '
                f'LIMIT {max_nodes}'
            )
            
            nodes = []
            node_ids = set()
            if nodes_result.is_succeeded():
                for i in range(nodes_result.row_size()):
                    row = nodes_result.row_values(i)
                    entity_id = row[0].as_string() if not row[0].is_empty() else ""
                    node_ids.add(entity_id)
                    nodes.append({
                        "id": entity_id,
                        "entity_type": row[1].as_string() if not row[1].is_empty() else "",
                        "description": row[2].as_string() if not row[2].is_empty() else "",
                    })
            
            # 获取边
            edges = []
            if node_ids:
                ids_str = ','.join([f'"{self._escape_string(nid)}"' for nid in node_ids])
                edges_result = session.execute(
                    f'MATCH (s:entity)-[r:relation]->(t:entity) '
                    f'WHERE s.entity_id IN [{ids_str}] AND t.entity_id IN [{ids_str}] '
                    f'RETURN s.entity_id, t.entity_id, r.relation_type, r.description'
                )
                if edges_result.is_succeeded():
                    for i in range(edges_result.row_size()):
                        row = edges_result.row_values(i)
                        edges.append({
                            "source": row[0].as_string() if not row[0].is_empty() else "",
                            "target": row[1].as_string() if not row[1].is_empty() else "",
                            "type": row[2].as_string() if not row[2].is_empty() else "",
                            "description": row[3].as_string() if not row[3].is_empty() else "",
                        })
            
            return KnowledgeGraph(
                nodes=nodes,
                edges=edges,
                is_truncated=len(nodes) >= max_nodes
            )
        finally:
            session.release()
    
    async def get_popular_labels(self, limit: int = 300) -> List[str]:
        """获取热门标签（按节点度数排序）"""
        session = self._get_session()
        try:
            session.execute(f'USE `{self._space}`')
            result = session.execute(
                f'MATCH (n:entity)-[r]-() '
                f'RETURN n.entity_type AS type, count(r) AS degree '
                f'ORDER BY degree DESC '
                f'LIMIT {limit}'
            )
            labels = []
            if result.is_succeeded():
                for i in range(result.row_size()):
                    label = result.row_values(i)[0].as_string()
                    if label:
                        labels.append(label)
            return labels
        finally:
            session.release()
    
    async def search_labels(self, query: str, limit: int = 50) -> List[str]:
        """搜索标签（模糊匹配）"""
        session = self._get_session()
        try:
            session.execute(f'USE `{self._space}`')
            result = session.execute(
                f'MATCH (n:entity) '
                f'WHERE n.entity_type CONTAINS "{self._escape_string(query)}" '
                f'RETURN DISTINCT n.entity_type AS type '
                f'LIMIT {limit}'
            )
            labels = []
            if result.is_succeeded():
                for i in range(result.row_size()):
                    label = result.row_values(i)[0].as_string()
                    if label:
                        labels.append(label)
            return labels
        finally:
            session.release()
    
    async def index_done_callback(self):
        """索引完成回调（NebulaGraph 自动持久化，无需额外操作）"""
        pass
```

### 3. 注册存储后端到 LightRAG

**文件位置**: `backend/rag/service.py` (修改)

在 `initialize` 方法中添加 NebulaGraph 支持：

```python
# 在第534行附近修改
# 原代码：
if os.getenv("NEO4J_URI"):
    lightrag_kwargs.update({"graph_storage": "Neo4JStorage"})

# 修改为：
graph_storage_type = os.getenv("GRAPH_STORAGE_TYPE", "Neo4JStorage")
if graph_storage_type == "NebulaGraphStorage":
    from backend.rag.storages import NebulaGraphStorage
    lightrag_kwargs.update({"graph_storage": "NebulaGraphStorage"})
elif os.getenv("NEO4J_URI"):
    lightrag_kwargs.update({"graph_storage": "Neo4JStorage"})
```

### 4. 更新环境变量配置

**文件位置**: `backend/.env` (修改)

```bash
# Graph DB Configuration
# 图存储类型选择: Neo4JStorage 或 NebulaGraphStorage
GRAPH_STORAGE_TYPE=NebulaGraphStorage

# Neo4j 配置（备用）
NEO4J_URI=bolt://<minio_server_ip>:7687
NEO4J_USERNAME=neo4j
NEO4J_PASSWORD=qwertyuiop@222neo4j

# NebulaGraph 配置（多知识库场景）
NEBULA_HOST=<minio_server_ip>
NEBULA_GRAPH_PORT=9669
NEBULA_USER=root
NEBULA_PASSWORD=qwertyuiop@222nebula
NEBULA_MAX_CONNECTION_POOL_SIZE=100
```

### 5. 更新配置类

**文件位置**: `backend/config/settings.py` (修改)

```python
# ==================== 图存储配置 ====================
graph_storage_type: str = Field(
    default="Neo4JStorage", 
    description="图存储类型: Neo4JStorage 或 NebulaGraphStorage"
)

# NebulaGraph 配置
nebula_host: str = Field(default="<minio_server_ip>", description="NebulaGraph 主机地址")
nebula_graph_port: int = Field(default=9669, description="NebulaGraph Graph 服务端口")
nebula_user: str = Field(default="root", description="NebulaGraph 用户名")
nebula_password: str = Field(default="", description="NebulaGraph 密码")
nebula_max_connection_pool_size: int = Field(default=100, description="NebulaGraph 连接池大小")
```

---

## 文件清单

| 操作 | 文件路径 | 说明 |
|-----|---------|------|
| 新建 | `backend/rag/storages/__init__.py` | 存储模块初始化 |
| 新建 | `backend/rag/storages/nebula_storage.py` | NebulaGraph 存储实现 |
| 修改 | `backend/rag/service.py` | 注册 NebulaGraph 存储后端 |
| 修改 | `backend/config/settings.py` | 添加 NebulaGraph 配置 |
| 修改 | `backend/.env` | 添加环境变量 |

---

## 验收标准

### 功能验收
- [ ] 所有 `BaseGraphStorage` 接口方法实现完成
- [ ] 连接池正确管理（初始化、获取会话、释放）
- [ ] Space 和 Schema 自动创建
- [ ] 节点 CRUD 操作正常
- [ ] 边 CRUD 操作正常
- [ ] 批量操作正常
- [ ] 图查询操作正常

### 性能验收
- [ ] 连接池复用正常
- [ ] 无内存泄漏
- [ ] 并发访问安全

### 单元测试
- [ ] 节点操作测试通过
- [ ] 边操作测试通过
- [ ] 图查询测试通过
- [ ] 测试覆盖率 > 80%

---

## 执行步骤

```bash
# 1. 安装依赖
pip install nebula3-python

# 2. 测试 NebulaGraph 连接
cd backend
python -c "
from nebula3.gclient.net import ConnectionPool
from nebula3.Config import Config
import os

config = Config()
pool = ConnectionPool()
ok = pool.init([(os.getenv('NEBULA_HOST', '<minio_server_ip>'), 9669)], config)
print('NebulaGraph connection:', 'OK' if ok else 'FAILED')
pool.close()
"

# 3. 运行单元测试
pytest tests/test_nebula_storage.py -v
```

---

## 依赖关系
- 依赖任务一（数据库设计）

## 预计工期
2 天
