"""
NebulaGraph 存储实现

实现 LightRAG 的 BaseGraphStorage 接口，支持多知识库物理隔离。
每个 workspace 对应一个 NebulaGraph Space。
"""

import asyncio
import contextvars
from concurrent.futures import ThreadPoolExecutor
from dataclasses import dataclass, field
from typing import Any, Dict, List, Optional, Tuple

from lightrag.base import BaseGraphStorage
from lightrag.types import KnowledgeGraph, KnowledgeGraphEdge, KnowledgeGraphNode
from lightrag.utils import logger

try:
    from nebula3.gclient.net import ConnectionPool
    from nebula3.Config import Config as NebulaConfig

    NEBULA_AVAILABLE = True
except ImportError:
    NEBULA_AVAILABLE = False

_executor = ThreadPoolExecutor(max_workers=10, thread_name_prefix="nebula_")


def resolve_graph_workspace(
    workspace: str,
    namespace: str,
    global_config: Optional[Dict[str, Any]] = None,
) -> str:
    """Resolve the Nebula space independently from the shared LightRAG workspace."""
    addon_params = (global_config or {}).get("addon_params") or {}
    graph_workspace = str(addon_params.get("graph_workspace") or "").strip()
    return graph_workspace or workspace or namespace


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
    _lock: asyncio.Lock = field(default_factory=asyncio.Lock, init=False)

    def __post_init__(self):
        if not NEBULA_AVAILABLE:
            raise ImportError(
                "nebula3-python is required for NebulaGraph storage. "
                "Install it with: pip install nebula3-python"
            )

        from backend.config.settings import settings
        self._host = settings.nebula_host
        self._port = settings.nebula_graph_port
        self._user = settings.nebula_user
        self._password = settings.nebula_password
        self._max_connection_pool_size = settings.nebula_max_connection_pool_size
        self._space = resolve_graph_workspace(
            self.workspace,
            self.namespace,
            self.global_config,
        )

    async def initialize(self):
        """初始化连接池和 Space"""
        if self._initialized:
            return

        async with self._lock:
            if self._initialized:
                return

            config = NebulaConfig()
            config.max_connection_pool_size = self._max_connection_pool_size

            context = contextvars.copy_context()
            self._pool = await asyncio.get_running_loop().run_in_executor(
                _executor,
                context.run,
                self._sync_init_pool,
                config,
            )

            await self._ensure_space_and_schema()
            self._initialized = True
            logger.info(f"[{self._space}] NebulaGraph storage initialized")

    def _sync_init_pool(self, config):
        """同步初始化连接池"""
        pool = ConnectionPool()
        ok = pool.init([(self._host, self._port)], config)
        if not ok:
            raise RuntimeError(
                f"Failed to connect to NebulaGraph at {self._host}:{self._port}"
            )
        return pool

    async def _ensure_space_and_schema(self):
        """确保 Space 和 Schema 存在"""
        await self._execute_async(self._sync_ensure_space)
        await asyncio.sleep(5)
        await self._ensure_schema()

    def _sync_ensure_space(self):
        """同步创建 Space"""
        session = self._pool.get_session(self._user, self._password)
        try:
            result = session.execute(
                f"CREATE SPACE IF NOT EXISTS `{self._space}` (vid_type=FIXED_STRING(256))"
            )
            if result.is_succeeded():
                logger.debug(f"[{self._space}] Space created")
            else:
                logger.debug(f"[{self._space}] Space may already exist: {result.error_msg()}")
        except Exception as e:
            if "Existed" not in str(e):
                logger.warning(f"[{self._space}] Space creation warning: {e}")
        finally:
            session.release()

    async def _ensure_schema(self):
        """确保 Schema 存在"""
        await self._execute_async(self._sync_ensure_schema_inner)
        await asyncio.sleep(5)

    def _sync_ensure_schema_inner(self):
        """同步创建 Schema"""
        session = self._pool.get_session(self._user, self._password)
        try:
            result = session.execute(f"USE `{self._space}`")
            if not result.is_succeeded():
                logger.warning(f"[{self._space}] USE statement failed: {result.error_msg()}")
        finally:
            session.release()

        schema_statements = [
            """
            CREATE TAG IF NOT EXISTS entity (
                entity_id STRING,
                entity_type STRING,
                description STRING,
                source_id STRING,
                file_path STRING,
                created_at TIMESTAMP
            )
            """,
            """
            CREATE EDGE IF NOT EXISTS relation (
                relation_type STRING,
                description STRING,
                source_id STRING,
                file_path STRING,
                weight DOUBLE,
                created_at TIMESTAMP
            )
            """,
            "CREATE TAG INDEX IF NOT EXISTS entity_id_idx ON entity(entity_id)",
            "CREATE TAG INDEX IF NOT EXISTS entity_type_idx ON entity(entity_type)",
            "CREATE TAG INDEX IF NOT EXISTS entity_source_idx ON entity(source_id(256))",
            "CREATE EDGE INDEX IF NOT EXISTS relation_source_idx ON relation(source_id(256))",
        ]

        for stmt in schema_statements:
            session = self._pool.get_session(self._user, self._password)
            try:
                session.execute(f"USE `{self._space}`")
                result = session.execute(stmt)
                if not result.is_succeeded() and "Existed" not in result.error_msg():
                    logger.debug(f"Schema statement warning: {result.error_msg()}")
            except Exception as e:
                if "Existed" not in str(e):
                    logger.debug(f"Schema statement warning: {e}")
            finally:
                session.release()

        logger.debug(f"[{self._space}] Schema verified")

    async def finalize(self):
        """关闭连接池"""
        if self._pool:
            await self._execute_async(self._sync_close_pool)
            self._initialized = False
            logger.info(f"[{self._space}] NebulaGraph storage finalized")

    def _sync_close_pool(self):
        """同步关闭连接池"""
        self._pool.close()
        self._pool = None

    async def drop(self):
        """删除 Space（危险操作）"""
        if not self._pool:
            return
        await self._execute_async(self._sync_drop_space)

    def _sync_drop_space(self):
        """同步删除 Space"""
        session = self._pool.get_session(self._user, self._password)
        try:
            session.execute(f"DROP SPACE IF EXISTS `{self._space}`")
            logger.info(f"[{self._space}] Space dropped")
        finally:
            session.release()

    async def _execute_async(self, func, *args):
        """在线程池中执行同步函数"""
        context = contextvars.copy_context()
        loop = asyncio.get_running_loop()
        return await loop.run_in_executor(_executor, context.run, func, *args)

    def _get_session(self):
        """获取会话（已执行 USE space）"""
        if not self._pool:
            raise RuntimeError("NebulaGraph not initialized")
        
        session = self._pool.get_session(self._user, self._password)
        result = session.execute(f"USE `{self._space}`")
        if not result.is_succeeded():
            session.release()
            raise RuntimeError(f"Failed to use space {self._space}: {result.error_msg()}")
        
        return session
    
    def _release_session(self, session):
        """释放会话"""
        try:
            session.release()
        except Exception:
            pass

    def _escape_string(self, s: str) -> str:
        """转义字符串中的特殊字符"""
        if s is None:
            return ""
        return (
            s.replace("\\", "\\\\")
            .replace('"', '\\"')
            .replace("\n", "\\n")
            .replace("\r", "\\r")
            .replace("\t", "\\t")
        )

    def _safe_get_string(self, value) -> str:
        """安全获取字符串值"""
        try:
            if value is None or value.is_empty():
                return ""
            return value.as_string()
        except Exception:
            return ""

    def _safe_get_double(self, value, default: float = 1.0) -> float:
        """安全获取浮点数值"""
        try:
            if value is None or value.is_empty():
                return default
            return value.as_double()
        except Exception:
            return default

    async def has_node(self, node_id: str) -> bool:
        """检查节点是否存在"""
        return await self._execute_async(self._sync_has_node, node_id)

    def _sync_has_node(self, node_id: str) -> bool:
        session = self._get_session()
        try:
            result = session.execute(
                f'FETCH PROP ON entity "{self._escape_string(node_id)}" YIELD vertex as v'
            )
            return result.is_succeeded() and result.row_size() > 0
        except Exception:
            return False
        finally:
            self._release_session(session)

    async def get_node(self, node_id: str) -> Optional[Dict[str, str]]:
        """获取节点属性"""
        return await self._execute_async(self._sync_get_node, node_id)

    def _sync_get_node(self, node_id: str) -> Optional[Dict[str, str]]:
        session = self._get_session()
        try:
            result = session.execute(
                f'FETCH PROP ON entity "{self._escape_string(node_id)}" '
                f'YIELD properties(vertex).entity_id, properties(vertex).entity_type, '
                f'properties(vertex).description, properties(vertex).source_id, properties(vertex).file_path'
            )
            if result.is_succeeded() and result.row_size() > 0:
                row = result.row_values(0)
                return {
                    "entity_id": self._safe_get_string(row[0]),
                    "entity_type": self._safe_get_string(row[1]),
                    "description": self._safe_get_string(row[2]),
                    "source_id": self._safe_get_string(row[3]),
                    "file_path": self._safe_get_string(row[4]),
                }
            return None
        except Exception:
            return None
        finally:
            self._release_session(session)

    async def get_nodes_batch(self, node_ids: List[str]) -> Dict[str, Dict[str, str]]:
        """批量获取节点"""
        if not node_ids:
            return {}
        return await self._execute_async(self._sync_get_nodes_batch, node_ids)

    def _sync_get_nodes_batch(self, node_ids: List[str]) -> Dict[str, Dict[str, str]]:
        results = {}
        session = self._get_session()
        try:
            batch_size = 500
            for i in range(0, len(node_ids), batch_size):
                batch = node_ids[i:i + batch_size]
                vids = ', '.join(f'"{self._escape_string(nid)}"' for nid in batch)
                nGQL = f'FETCH PROP ON entity {vids} YIELD properties(vertex).entity_id, properties(vertex).entity_type, properties(vertex).description, properties(vertex).source_id, properties(vertex).file_path'
                result = session.execute(nGQL)
                
                if result.is_succeeded():
                    for j in range(result.row_size()):
                        row = result.row_values(j)
                        entity_id = self._safe_get_string(row[0])
                        if entity_id:
                            results[entity_id] = {
                                "entity_id": entity_id,
                                "entity_type": self._safe_get_string(row[1]),
                                "description": self._safe_get_string(row[2]),
                                "source_id": self._safe_get_string(row[3]),
                                "file_path": self._safe_get_string(row[4]),
                            }
        except Exception as e:
            logger.warning(f"[{self._space}] get_nodes_batch failed: {e}")
        finally:
            self._release_session(session)
        return results

    async def upsert_node(self, node_id: str, node_data: Dict[str, str]) -> None:
        """插入或更新节点"""
        await self._execute_async(self._sync_upsert_node, node_id, node_data)

    def _sync_upsert_node(self, node_id: str, node_data: Dict[str, str]) -> None:
        session = self._get_session()
        try:
            entity_type = self._escape_string(node_data.get("entity_type", "unknown"))
            description = self._escape_string(node_data.get("description", ""))
            source_id = self._escape_string(node_data.get("source_id", ""))
            file_path = self._escape_string(node_data.get("file_path", ""))

            nGQL = (
                f'INSERT VERTEX entity (entity_id, entity_type, description, source_id, file_path, created_at) '
                f'VALUES "{self._escape_string(node_id)}": ('
                f'"{self._escape_string(node_id)}", '
                f'"{entity_type}", '
                f'"{description}", '
                f'"{source_id}", '
                f'"{file_path}", '
                f'timestamp())'
            )
            result = session.execute(nGQL)
            if not result.is_succeeded():
                logger.warning(f"[{self._space}] Insert node failed: {result.error_msg()}")
        except Exception as e:
            logger.warning(f"[{self._space}] upsert_node failed: {e}")
        finally:
            self._release_session(session)

    async def delete_node(self, node_id: str) -> None:
        """删除节点"""
        await self._execute_async(self._sync_delete_node, node_id)

    def _sync_delete_node(self, node_id: str) -> None:
        session = self._get_session()
        try:
            session.execute(
                f'DELETE VERTEX IF EXISTS "{self._escape_string(node_id)}"'
            )
        except Exception as e:
            logger.warning(f"[{self._space}] delete_node failed: {e}")
        finally:
            self._release_session(session)

    async def remove_nodes(self, nodes: List[str]) -> None:
        """批量删除节点"""
        if not nodes:
            return
        await self._execute_async(self._sync_remove_nodes, nodes)

    def _sync_remove_nodes(self, nodes: List[str]) -> None:
        session = self._get_session()
        try:
            batch_size = 100
            for i in range(0, len(nodes), batch_size):
                batch = nodes[i:i + batch_size]
                vids = ', '.join(f'"{self._escape_string(nid)}"' for nid in batch)
                nGQL = f'DELETE VERTEX IF EXISTS {vids}'
                result = session.execute(nGQL)
                if not result.is_succeeded():
                    logger.warning(f"[{self._space}] Batch delete nodes failed: {result.error_msg()}")
        except Exception as e:
            logger.warning(f"[{self._space}] remove_nodes failed: {e}")
        finally:
            self._release_session(session)

    async def node_degree(self, node_id: str) -> int:
        """获取节点度数"""
        return await self._execute_async(self._sync_node_degree, node_id)

    def _sync_node_degree(self, node_id: str) -> int:
        session = self._get_session()
        try:
            result = session.execute(
                f'GO FROM "{self._escape_string(node_id)}" OVER relation BIDIRECT '
                f'YIELD $$.entity.entity_id as target_id | '
                f'YIELD count(*) AS degree'
            )
            if result.is_succeeded() and result.row_size() > 0:
                return result.row_values(0)[0].as_int()
            return 0
        except Exception:
            return 0
        finally:
            self._release_session(session)

    async def node_degrees_batch(self, node_ids: List[str]) -> Dict[str, int]:
        """批量获取节点度数"""
        if not node_ids:
            return {}
        return await self._execute_async(self._sync_node_degrees_batch, node_ids)

    def _sync_node_degrees_batch(self, node_ids: List[str]) -> Dict[str, int]:
        results = {nid: 0 for nid in node_ids}
        session = self._get_session()
        try:
            batch_size = 512
            for i in range(0, len(node_ids), batch_size):
                batch = node_ids[i:i + batch_size]
                vids = ', '.join(f'"{self._escape_string(nid)}"' for nid in batch)
                # 优化：直接使用 id($^) 获取起始节点 ID，避免从属性中读取，并按起始节点分组
                nGQL = (
                    f'GO FROM {vids} OVER relation BIDIRECT '
                    f'YIELD id($^) as src_id '
                    f'| GROUP BY $-.src_id '
                    f'YIELD $-.src_id as entity_id, count(*) as degree'
                )
                result = session.execute(nGQL)
                
                if result.is_succeeded():
                    for j in range(result.row_size()):
                        row = result.row_values(j)
                        entity_id = self._safe_get_string(row[0])
                        degree = row[1].as_int() if not row[1].is_empty() else 0
                        if entity_id in results:
                            results[entity_id] = degree
        except Exception as e:
            logger.warning(f"[{self._space}] node_degrees_batch failed: {e}")
        finally:
            self._release_session(session)
        return results

    async def has_edge(self, source_node_id: str, target_node_id: str) -> bool:
        """检查边是否存在"""
        return await self._execute_async(self._sync_has_edge, source_node_id, target_node_id)

    def _sync_has_edge(self, source_node_id: str, target_node_id: str) -> bool:
        session = self._get_session()
        try:
            result = session.execute(
                f'FETCH PROP ON relation "{self._escape_string(source_node_id)}"->"{self._escape_string(target_node_id)}" '
                f'YIELD edge as e'
            )
            return result.is_succeeded() and result.row_size() > 0
        except Exception:
            return False
        finally:
            self._release_session(session)

    async def get_edge(
        self, source_node_id: str, target_node_id: str
    ) -> Optional[Dict[str, str]]:
        """获取边属性"""
        return await self._execute_async(self._sync_get_edge, source_node_id, target_node_id)

    def _sync_get_edge(self, source_node_id: str, target_node_id: str) -> Optional[Dict[str, str]]:
        session = self._get_session()
        try:
            result = session.execute(
                f'FETCH PROP ON relation "{self._escape_string(source_node_id)}"->"{self._escape_string(target_node_id)}" '
                f'YIELD properties(edge).relation_type, properties(edge).description, '
                f'properties(edge).source_id, properties(edge).weight'
            )
            if result.is_succeeded() and result.row_size() > 0:
                row = result.row_values(0)
                return {
                    "relation_type": self._safe_get_string(row[0]),
                    "description": self._safe_get_string(row[1]),
                    "source_id": self._safe_get_string(row[2]),
                    "weight": self._safe_get_double(row[3]),
                }
            return None
        except Exception:
            return None
        finally:
            self._release_session(session)

    async def get_edges_batch(
        self, pairs: List[Dict[str, str]]
    ) -> Dict[Tuple[str, str], Dict[str, str]]:
        """批量获取边"""
        if not pairs:
            return {}
        return await self._execute_async(self._sync_get_edges_batch, pairs)

    def _sync_get_edges_batch(self, pairs: List[Dict[str, str]]) -> Dict[Tuple[str, str], Dict[str, str]]:
        results = {}
        session = self._get_session()
        try:
            all_src_ids = set()
            pair_set = set()
            for pair in pairs:
                src_id = pair.get("src", "")
                tgt_id = pair.get("tgt", "")
                if src_id and tgt_id:
                    all_src_ids.add(src_id)
                    pair_set.add((src_id, tgt_id))
            
            if not all_src_ids:
                return {}
            
            batch_size = 400
            all_src_list = list(all_src_ids)
            
            for i in range(0, len(all_src_list), batch_size):
                batch = all_src_list[i:i + batch_size]
                vids = ', '.join(f'"{self._escape_string(vid)}"' for vid in batch)
                
                nGQL = f'GO FROM {vids} OVER relation YIELD id($^) as src, id($$) as tgt, properties(edge).relation_type, properties(edge).description, properties(edge).source_id, properties(edge).weight'
                result = session.execute(nGQL)
                
                if result.is_succeeded():
                    for j in range(result.row_size()):
                        row = result.row_values(j)
                        src = self._safe_get_string(row[0])
                        tgt = self._safe_get_string(row[1])
                        
                        if src and tgt:
                            key = (src, tgt) if (src, tgt) in pair_set else (tgt, src) if (tgt, src) in pair_set else None
                            if key and key not in results:
                                results[key] = {
                                    "relation_type": self._safe_get_string(row[2]),
                                    "description": self._safe_get_string(row[3]),
                                    "source_id": self._safe_get_string(row[4]),
                                    "weight": self._safe_get_double(row[5]),
                                }
        except Exception as e:
            logger.warning(f"[{self._space}] get_edges_batch failed: {e}")
        finally:
            self._release_session(session)
        return results

    async def upsert_edge(
        self, source_node_id: str, target_node_id: str, edge_data: Dict[str, str]
    ) -> None:
        """插入或更新边"""
        await self._execute_async(self._sync_upsert_edge, source_node_id, target_node_id, edge_data)

    def _sync_upsert_edge(self, source_node_id: str, target_node_id: str, edge_data: Dict[str, str]) -> None:
        session = self._get_session()
        try:
            relation_type = self._escape_string(edge_data.get("relation_type", "") or edge_data.get("keywords", "related_to"))
            description = self._escape_string(edge_data.get("description", ""))
            source_id = self._escape_string(edge_data.get("source_id", ""))
            file_path = self._escape_string(edge_data.get("file_path", ""))
            weight = float(edge_data.get("weight", "1.0"))

            nGQL = (
                f'INSERT EDGE relation (relation_type, description, source_id, file_path, weight, created_at) '
                f'VALUES "{self._escape_string(source_node_id)}"->"{self._escape_string(target_node_id)}": ('
                f'"{relation_type}", '
                f'"{description}", '
                f'"{source_id}", '
                f'"{file_path}", '
                f'{weight}, '
                f'timestamp())'
            )
            result = session.execute(nGQL)
            if not result.is_succeeded():
                logger.warning(f"[{self._space}] Insert edge failed: {result.error_msg()}")
        except Exception as e:
            logger.warning(f"[{self._space}] upsert_edge failed: {e}")
        finally:
            self._release_session(session)

    async def delete_edge(self, source_node_id: str, target_node_id: str) -> None:
        """删除边"""
        await self._execute_async(self._sync_delete_edge, source_node_id, target_node_id)

    def _sync_delete_edge(self, source_node_id: str, target_node_id: str) -> None:
        session = self._get_session()
        try:
            session.execute(
                f'DELETE EDGE relation "{self._escape_string(source_node_id)}"->"{self._escape_string(target_node_id)}"'
            )
        except Exception as e:
            logger.warning(f"[{self._space}] delete_edge failed: {e}")
        finally:
            self._release_session(session)

    async def remove_edges(self, edges: List[Tuple[str, str]]) -> None:
        """批量删除边"""
        if not edges:
            return
        await self._execute_async(self._sync_remove_edges, edges)

    def _sync_remove_edges(self, edges: List[Tuple[str, str]]) -> None:
        session = self._get_session()
        try:
            batch_size = 100
            for i in range(0, len(edges), batch_size):
                batch = edges[i:i + batch_size]
                edge_exprs = []
                for src, tgt in batch:
                    edge_exprs.append(f'"{self._escape_string(src)}"->"{self._escape_string(tgt)}"')
                
                if edge_exprs:
                    nGQL = f'DELETE EDGE relation {", ".join(edge_exprs)}'
                    result = session.execute(nGQL)
                    if not result.is_succeeded():
                        logger.warning(f"[{self._space}] Batch delete edges failed: {result.error_msg()}")
        except Exception as e:
            logger.warning(f"[{self._space}] remove_edges failed: {e}")
        finally:
            self._release_session(session)

    async def get_node_edges(self, source_node_id: str) -> List[Tuple[str, str]]:
        """获取节点的所有边"""
        return await self._execute_async(self._sync_get_node_edges, source_node_id)

    def _sync_get_node_edges(self, source_node_id: str) -> List[Tuple[str, str]]:
        session = self._get_session()
        try:
            result = session.execute(
                f'GO FROM "{self._escape_string(source_node_id)}" OVER relation BIDIRECT '
                f'YIELD id($^) as src, id($$) as tgt'
            )
            edges = []
            if result.is_succeeded():
                for i in range(result.row_size()):
                    row = result.row_values(i)
                    src = self._safe_get_string(row[0])
                    tgt = self._safe_get_string(row[1])
                    if src and tgt:
                        edges.append((src, tgt))
            return edges
        except Exception as e:
            logger.warning(f"[{self._space}] get_node_edges failed: {e}")
            return []
        finally:
            self._release_session(session)

    async def get_nodes_edges_batch(
        self, node_ids: List[str]
    ) -> Dict[str, List[Tuple[str, str]]]:
        """批量获取节点的所有边"""
        if not node_ids:
            return {}
        return await self._execute_async(self._sync_get_nodes_edges_batch, node_ids)

    def _sync_get_nodes_edges_batch(self, node_ids: List[str]) -> Dict[str, List[Tuple[str, str]]]:
        results: Dict[str, List[Tuple[str, str]]] = {nid: [] for nid in node_ids}
        session = self._get_session()
        try:
            max_edges_per_node = 100
            batch_size = 256
            
            for i in range(0, len(node_ids), batch_size):
                batch = node_ids[i:i + batch_size]
                vids = ', '.join(f'"{self._escape_string(nid)}"' for nid in batch)
                
                # 优化：使用原生批量查询 GO FROM 代替循环查询，极大减少数据库往返次数
                # id($^) 为起始节点，id($$) 为目的节点
                nGQL = (
                    f'GO FROM {vids} OVER relation BIDIRECT '
                    f'YIELD id($^) as src, id($$) as tgt '
                    f'| LIMIT {len(batch) * max_edges_per_node}'
                )
                result = session.execute(nGQL)
                
                if result.is_succeeded():
                    for j in range(result.row_size()):
                        row = result.row_values(j)
                        src = self._safe_get_string(row[0])
                        tgt = self._safe_get_string(row[1])
                        
                        if src and tgt and src in results:
                            # 限制单个节点的边数量
                            if len(results[src]) < max_edges_per_node:
                                results[src].append((src, tgt))
                
        except Exception as e:
            logger.warning(f"[{self._space}] get_nodes_edges_batch failed: {e}")
        finally:
            self._release_session(session)
        return results

    async def get_nodes_by_source_id(self, source_id: str) -> List[Dict[str, Any]]:
        """根据 source_id 获取实体"""
        return await self._execute_async(self._sync_get_nodes_by_source_id, source_id)

    def _sync_get_nodes_by_source_id(self, source_id: str) -> List[Dict[str, Any]]:
        entities = []
        session = self._get_session()
        try:
            escaped_source_id = self._escape_string(source_id)
            query = (
                f'MATCH (n:entity) '
                f'WHERE n.entity.source_id CONTAINS "{escaped_source_id}" '
                f'RETURN n.entity.entity_id, n.entity.entity_type, '
                f'n.entity.description, n.entity.source_id, n.entity.file_path '
                f'LIMIT 5000'
            )
            result = session.execute(query)
            
            if result.is_succeeded():
                for i in range(result.row_size()):
                    row = result.row_values(i)
                    entity = {
                        "entity_id": self._safe_get_string(row[0]),
                        "entity_type": self._safe_get_string(row[1]),
                        "description": self._safe_get_string(row[2]),
                        "source_id": self._safe_get_string(row[3]),
                        "file_path": self._safe_get_string(row[4]),
                    }
                    if entity["entity_id"]:
                        entities.append(entity)
            
            logger.debug(f"[NebulaGraph] get_nodes_by_source_id: source_id={source_id}, count={len(entities)}")
            return entities
        except Exception as e:
            logger.warning(f"[{self._space}] get_nodes_by_source_id failed: {e}")
            return []
        finally:
            self._release_session(session)

    async def get_all_nodes(self) -> List[Dict[str, Any]]:
        """获取所有节点（使用分页）"""
        return await self.get_all_nodes_paginated()

    async def get_all_edges(self) -> List[Dict[str, Any]]:
        """获取所有边（使用分页）"""
        return await self.get_all_edges_paginated()

    async def get_all_edges_paginated(
        self, max_edges: int = 500000, page_size: int = 5000
    ) -> List[Dict[str, Any]]:
        """分页获取所有边"""
        return await self._execute_async(self._sync_get_all_edges_paginated, max_edges, page_size)

    def _sync_get_all_edges_paginated(self, max_edges: int, page_size: int) -> List[Dict[str, Any]]:
        all_edges = []
        session = self._get_session()
        try:
            offset = 0
            while True:
                query = (
                    f'MATCH (src:entity)-[e:relation]->(tgt:entity) '
                    f'RETURN id(src) AS src_id, id(tgt) AS tgt_id, '
                    f'e.relation_type AS relation_type, e.description AS description, '
                    f'e.source_id AS source_id, e.weight AS weight '
                    f'ORDER BY src_id '
                    f'SKIP {offset} LIMIT {page_size}'
                )
                result = session.execute(query)
                
                if not result.is_succeeded():
                    break
                
                batch_count = 0
                for i in range(result.row_size()):
                    row = result.row_values(i)
                    edge = {
                        "src_id": self._safe_get_string(row[0]),
                        "tgt_id": self._safe_get_string(row[1]),
                        "relation_type": self._safe_get_string(row[2]),
                        "description": self._safe_get_string(row[3]),
                        "source_id": self._safe_get_string(row[4]),
                        "weight": self._safe_get_double(row[5]),
                    }
                    if edge["src_id"] and edge["tgt_id"]:
                        all_edges.append(edge)
                        batch_count += 1
                
                if batch_count < page_size:
                    break
                
                offset += page_size
                
                if len(all_edges) >= max_edges:
                    logger.warning(f"[NebulaGraph] 边数量超过 {max_edges}，停止获取")
                    break
            
            logger.info(f"[NebulaGraph] 获取所有边: space={self._space}, count={len(all_edges)}")
            return all_edges
        except Exception as e:
            logger.error(f"[NebulaGraph] get_all_edges 失败: {e}")
            return []
        finally:
            self._release_session(session)

    async def get_all_nodes_paginated(
        self, max_entities: int = 500000, page_size: int = 5000
    ) -> List[Dict[str, Any]]:
        """分页获取所有节点"""
        return await self._execute_async(self._sync_get_all_nodes_paginated, max_entities, page_size)

    def _sync_get_all_nodes_paginated(self, max_entities: int, page_size: int) -> List[Dict[str, Any]]:
        all_entities = []
        session = self._get_session()
        try:
            offset = 0
            while True:
                query = (
                    f'MATCH (n:entity) '
                    f'RETURN n.entity.entity_id AS entity_id, n.entity.entity_type AS entity_type, '
                    f'n.entity.description AS description, n.entity.source_id AS source_id, '
                    f'n.entity.file_path AS file_path '
                    f'ORDER BY entity_id '
                    f'SKIP {offset} LIMIT {page_size}'
                )
                result = session.execute(query)
                
                if not result.is_succeeded():
                    break
                
                batch_count = 0
                for i in range(result.row_size()):
                    row = result.row_values(i)
                    entity = {
                        "entity_id": self._safe_get_string(row[0]),
                        "entity_type": self._safe_get_string(row[1]),
                        "description": self._safe_get_string(row[2]),
                        "source_id": self._safe_get_string(row[3]),
                        "file_path": self._safe_get_string(row[4]),
                    }
                    if entity["entity_id"]:
                        all_entities.append(entity)
                        batch_count += 1
                
                if batch_count < page_size:
                    break
                
                offset += page_size
                
                if len(all_entities) >= max_entities:
                    logger.warning(f"[NebulaGraph] 实体数量超过 {max_entities}，停止获取")
                    break
            
            logger.info(f"[NebulaGraph] 获取所有实体: space={self._space}, count={len(all_entities)}")
            return all_entities
        except Exception as e:
            logger.error(f"[NebulaGraph] get_all_nodes 失败: {e}")
            return []
        finally:
            self._release_session(session)

    async def index_done_callback(self) -> None:
        """索引完成回调"""
        pass

    async def edge_degree(self, src_id: str, tgt_id: str) -> int:
        """获取边的度数（两个节点度数之和）"""
        src_degree = await self.node_degree(src_id)
        tgt_degree = await self.node_degree(tgt_id)
        return src_degree + tgt_degree

    async def edge_degrees_batch(
        self, edge_pairs: List[Tuple[str, str]]
    ) -> Dict[Tuple[str, str], int]:
        """批量获取边的度数"""
        if not edge_pairs:
            return {}
        
        all_node_ids = set()
        for src, tgt in edge_pairs:
            all_node_ids.add(src)
            all_node_ids.add(tgt)
        
        node_degrees = await self.node_degrees_batch(list(all_node_ids))
        
        results = {}
        for src, tgt in edge_pairs:
            results[(src, tgt)] = node_degrees.get(src, 0) + node_degrees.get(tgt, 0)
        
        return results

    async def get_all_labels(self) -> List[str]:
        """获取所有标签（实体类型）"""
        return await self._execute_async(self._sync_get_all_labels)

    def _sync_get_all_labels(self) -> List[str]:
        session = self._get_session()
        try:
            query = (
                'MATCH (n:entity) '
                'RETURN DISTINCT n.entity.entity_type AS entity_type '
                'ORDER BY entity_type'
            )
            result = session.execute(query)
            
            labels = []
            if result.is_succeeded():
                for i in range(result.row_size()):
                    row = result.row_values(i)
                    label = self._safe_get_string(row[0])
                    if label:
                        labels.append(label)
            
            return labels
        except Exception as e:
            logger.warning(f"[{self._space}] get_all_labels failed: {e}")
            return []
        finally:
            self._release_session(session)

    async def get_knowledge_graph(
        self,
        node_label: str,
        max_depth: int = 3,
        max_nodes: int = 1000,
    ) -> KnowledgeGraph:
        """
        获取知识图谱子图
        
        Args:
            node_label: 起始节点标签
            max_depth: 最大遍历深度
            max_nodes: 最大节点数
            
        Returns:
            KnowledgeGraph 对象
        """
        return await self._execute_async(
            self._sync_get_knowledge_graph, node_label, max_depth, max_nodes
        )

    def _sync_get_knowledge_graph(
        self, node_label: str, max_depth: int, max_nodes: int
    ) -> KnowledgeGraph:
        session = self._get_session()
        try:
            nodes_dict: Dict[str, KnowledgeGraphNode] = {}
            edges_dict: Dict[str, KnowledgeGraphEdge] = {}
            
            if node_label == "*":
                query = (
                    f'MATCH (n:entity) '
                    f'RETURN n.entity.entity_id, n.entity.entity_type, n.entity.description, '
                    f'n.entity.source_id, n.entity.file_path '
                    f'LIMIT {max_nodes}'
                )
            else:
                query = (
                    f'MATCH (n:entity) '
                    f'WHERE n.entity.entity_type == "{self._escape_string(node_label)}" '
                    f'RETURN n.entity.entity_id, n.entity.entity_type, n.entity.description, '
                    f'n.entity.source_id, n.entity.file_path '
                    f'LIMIT {max_nodes}'
                )
            result = session.execute(query)
            
            if result.is_succeeded():
                for i in range(result.row_size()):
                    row = result.row_values(i)
                    entity_id = self._safe_get_string(row[0])
                    if entity_id and entity_id not in nodes_dict:
                        nodes_dict[entity_id] = KnowledgeGraphNode(
                            id=entity_id,
                            labels=[self._safe_get_string(row[1])],
                            properties={
                                "description": self._safe_get_string(row[2]),
                                "source_id": self._safe_get_string(row[3]),
                                "file_path": self._safe_get_string(row[4]),
                            }
                        )
            
            if nodes_dict:
                node_ids = list(nodes_dict.keys())
                vids = ', '.join(f'"{self._escape_string(nid)}"' for nid in node_ids)
                
                edge_query = (
                    f'GO FROM {vids} OVER relation BIDIRECT '
                    f'YIELD id($^) as src, id($$) as tgt, '
                    f'properties(edge).relation_type, properties(edge).description, '
                    f'properties(edge).source_id, properties(edge).weight'
                )
                result = session.execute(edge_query)
                
                if result.is_succeeded():
                    for i in range(result.row_size()):
                        row = result.row_values(i)
                        src = self._safe_get_string(row[0])
                        tgt = self._safe_get_string(row[1])
                        edge_id = f"{src}->{tgt}"
                        
                        if edge_id not in edges_dict:
                            edges_dict[edge_id] = KnowledgeGraphEdge(
                                id=edge_id,
                                type=self._safe_get_string(row[2]),
                                source=src,
                                target=tgt,
                                properties={
                                    "description": self._safe_get_string(row[3]),
                                    "source_id": self._safe_get_string(row[4]),
                                    "weight": self._safe_get_double(row[5]),
                                }
                            )
            
            is_truncated = len(nodes_dict) >= max_nodes
            return KnowledgeGraph(
                nodes=list(nodes_dict.values()),
                edges=list(edges_dict.values()),
                is_truncated=is_truncated
            )
        except Exception as e:
            logger.warning(f"[{self._space}] get_knowledge_graph failed: {e}")
            return KnowledgeGraph()
        finally:
            self._release_session(session)

    async def get_popular_labels(self, limit: int = 300) -> List[str]:
        """
        获取热门标签（按实体数量排序）
        
        Returns:
            标签列表
        """
        return await self._execute_async(self._sync_get_popular_labels, limit)

    def _sync_get_popular_labels(self, limit: int) -> List[str]:
        session = self._get_session()
        try:
            query = (
                f'MATCH (n:entity) '
                f'RETURN n.entity.entity_type AS entity_type, count(*) AS cnt '
                f'ORDER BY cnt DESC '
                f'LIMIT {limit}'
            )
            result = session.execute(query)
            
            labels = []
            if result.is_succeeded():
                for i in range(result.row_size()):
                    row = result.row_values(i)
                    label = self._safe_get_string(row[0])
                    if label:
                        labels.append(label)
            
            return labels
        except Exception as e:
            logger.warning(f"[{self._space}] get_popular_labels failed: {e}")
            return []
        finally:
            self._release_session(session)

    async def search_labels(self, query: str, limit: int = 10) -> List[str]:
        """
        搜索标签
        
        Args:
            query: 搜索关键词
            limit: 返回数量限制
            
        Returns:
            匹配的标签列表
        """
        return await self._execute_async(self._sync_search_labels, query, limit)

    def _sync_search_labels(self, query: str, limit: int) -> List[str]:
        session = self._get_session()
        try:
            escaped_query = self._escape_string(query)
            nGQL = (
                f'MATCH (n:entity) '
                f'WHERE n.entity.entity_type CONTAINS "{escaped_query}" '
                f'RETURN DISTINCT n.entity.entity_type AS entity_type '
                f'LIMIT {limit}'
            )
            result = session.execute(nGQL)
            
            labels = []
            if result.is_succeeded():
                for i in range(result.row_size()):
                    row = result.row_values(i)
                    label = self._safe_get_string(row[0])
                    if label:
                        labels.append(label)
            
            return labels
        except Exception as e:
            logger.warning(f"[{self._space}] search_labels failed: {e}")
            return []
        finally:
            self._release_session(session)
