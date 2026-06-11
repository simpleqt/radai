# 多知识库入库流程与关键词索引动态更新方案

> **文档状态**：📦 设计/归档（2026-03 前后）。**现行实现**见 `backend/rag/sync/rag_index_sync_manager.py`、`backend/rag/keywords/per_kb_index.py`。  
> **Milvus 命名（现行）**：`rag_{kb_code}_keyword_index`、chunk 向量库由 LightRAG 按 `kb_code` 隔离（非下文 `rag_{kb_code}_entities` 旧称）。  
> **检索**：[docs/知识库算法和架构.md](../docs/知识库算法和架构.md)

## 一、现状分析

### 1.1 多知识库支持现状

当前系统已支持多知识库隔离，具体实现如下：

| 存储组件 | 隔离方式 | 命名规则 |
|---------|---------|---------|
| NebulaGraph | 独立 Space | `rag_{kb_code}` |
| Milvus | 独立 Collection | `rag_{kb_code}_keyword_index`、LightRAG `chunks_vdb`（按 kb 隔离） |
| Redis | Key 前缀隔离 | `rag_{kb_code}:keyword_index:*` |
| 文件系统 | 独立工作目录 | `{work_dir}/{kb_code}/` |

**关键代码位置**：
- [manager.py](file://backend/rag/knowledge_base/manager.py): 知识库管理器，支持多知识库实例缓存
- [nebula_storage.py](file://backend/rag/storages/nebula_storage.py): NebulaGraph 存储，每个实例绑定独立 Space
- [per_kb_index.py](file://backend/rag/keywords/per_kb_index.py): 单知识库关键词索引

### 1.2 关键词索引现状

当前关键词索引系统已具备增量更新能力：

```python
# backend/rag/keywords/index_manager.py
async def add_entities(
    self,
    kb_code: str,
    kb_id: str,
    entities: List[EntityData],
) -> int:
    """向指定知识库增量添加实体"""
    index = await self.get_index(kb_code, kb_id)
    return await index.add_entities(entities, self._embedding_func)
```

**已支持的更新操作**：
- `add_entities()`: 增量添加实体到 BM25 和向量索引
- `_save_to_redis()`: 持久化索引到 Redis
- `_save_to_milvus()`: 持久化向量到 Milvus

### 1.3 入库流程现状

当前入库流程：

```
文档入库 (service.py)
    ↓
LightRAG.ainsert()
    ↓
实体提取 (operate.py: _merge_nodes_then_upsert)
    ↓
存储到图数据库 (knowledge_graph_inst.upsert_node)
    ↓
存储到向量数据库 (entity_vdb.upsert)
    ↓
[缺失] 关键词索引更新
```

**问题**：入库流程中没有回调机制来更新关键词索引。

---

## 二、问题分析

### 2.1 核心问题

1. **入库后关键词索引未更新**：新入库的实体不会自动添加到关键词索引
2. **启动时重复加载**：每次启动都需要从 NebulaGraph 重新加载所有实体
3. **索引不一致**：图数据库、向量数据库、关键词索引之间可能不一致

### 2.2 影响范围

- 新入库文档的关键词无法被检索
- 需要手动重建索引才能检索新实体
- 系统重启后性能下降（需要重新加载）

### 2.3 约束条件

**核心要求**：方案不能影响正常查询服务
- 入库和索引更新必须是异步的，不能阻塞查询
- 定期同步必须在低峰期执行，不能影响在线服务
- 资源使用必须有上限控制

---

## 三、解决方案

### 3.1 设计原则

1. **异步非阻塞**：入库后通过消息队列或后台任务更新索引
2. **最终一致性**：允许短暂不一致，通过定期同步兜底
3. **资源隔离**：索引更新与查询使用不同的资源池
4. **统一命名**：所有新增模块统一使用 `rag` 前缀

### 3.2 方案架构

```
┌─────────────────────────────────────────────────────────────────┐
│                         文档入库流程                             │
│                                                                 │
│  service.py.process_multimodal()                                │
│       ↓                                                         │
│  LightRAG.ainsert() → 实体提取 → 图数据库 → 向量库              │
│       ↓                                                         │
│  [触发] RAGIndexSyncManager.sync_after_insert()                │
│       ↓ 异步，非阻塞                                            │
│  后台任务队列（Celery/Asyncio Task）                            │
│       ↓                                                         │
│  增量更新关键词索引                                             │
└─────────────────────────────────────────────────────────────────┘
                              ↓
┌─────────────────────────────────────────────────────────────────┐
│                       定期同步任务                              │
│                                                                 │
│  RAGIndexSyncScheduler (定时任务)                               │
│       ↓                                                         │
│  低峰期执行（如凌晨 2:00）                                      │
│       ↓                                                         │
│  全量同步关键词索引                                             │
│       ↓                                                         │
│  校验一致性                                                     │
└─────────────────────────────────────────────────────────────────┘
```

### 3.3 统一命名规范

| 模块/类/方法 | 命名规则 | 示例 |
|-------------|---------|------|
| 同步管理器 | `{功能}SyncManager` | `RAGIndexSyncManager` |
| 调度器 | `{功能}Scheduler` | `RAGIndexSyncScheduler` |
| 后台任务 | `{功能}Task` | `SyncKeywordIndexTask` |
| 配置项 | `RAG_{模块}_{配置}` | `RAG_INDEX_SYNC_INTERVAL` |
| Redis Key | `rag:index:sync:{kb_code}` | `rag:index:sync:rag_nuclear_electronics` |

---

## 四、详细设计

### 4.1 核心组件

#### 4.1.1 索引同步管理器

```python
# backend/rag/sync/rag_index_sync_manager.py

import asyncio
import logging
from datetime import datetime
from typing import List, Optional, Dict, Any
from dataclasses import dataclass

from backend.config.loggers import get_logger

logger = get_logger(__name__)


@dataclass
class SyncConfig:
    """同步配置"""
    enabled: bool = True
    sync_on_insert: bool = True
    batch_size: int = 100
    max_concurrent: int = 3
    retry_times: int = 3
    retry_delay: float = 1.0


class RAGIndexSyncManager:
    """
    RAG 索引同步管理器
    
    职责：
    1. 入库后异步增量同步
    2. 提供查询接口
    3. 状态管理
    """
    
    _instance: Optional["RAGIndexSyncManager"] = None
    
    def __new__(cls):
        if cls._instance is None:
            cls._instance = super().__new__(cls)
            cls._instance._initialized = False
        return cls._instance
    
    def __init__(self):
        if self._initialized:
            return
        
        self._initialized = True
        self._sync_tasks: Dict[str, asyncio.Task] = {}
        self._sync_status: Dict[str, Dict[str, Any]] = {}
        self._lock = asyncio.Lock()
        self._config = SyncConfig()
        
        logger.info("[RAGIndexSyncManager] 初始化完成")
    
    def set_config(self, config: SyncConfig) -> None:
        """设置同步配置"""
        self._config = config
    
    async def sync_after_insert(
        self,
        kb_code: str,
        kb_id: str,
        doc_ids: List[str],
    ) -> bool:
        """
        入库后触发异步增量同步
        
        Args:
            kb_code: 知识库编码
            kb_id: 知识库 ID
            doc_ids: 新入库的文档 ID 列表
            
        Returns:
            是否成功触发同步
        """
        if not self._config.enabled or not self._config.sync_on_insert:
            logger.debug("[RAGIndexSyncManager] 同步已禁用，跳过")
            return False
        
        # 检查是否已有同步任务在运行
        async with self._lock:
            if kb_code in self._sync_tasks:
                if not self._sync_tasks[kb_code].done():
                    logger.debug(f"[RAGIndexSyncManager] 同步任务进行中，跳过: kb={kb_code}")
                    return False
            
            # 创建异步任务
            task = asyncio.create_task(
                self._do_sync(kb_code, kb_id, doc_ids)
            )
            self._sync_tasks[kb_code] = task
        
        logger.info(f"[RAGIndexSyncManager] 触发增量同步: kb={kb_code}, docs={len(doc_ids)}")
        return True
    
    async def _do_sync(
        self,
        kb_code: str,
        kb_id: str,
        doc_ids: List[str],
    ) -> None:
        """执行增量同步"""
        try:
            self._sync_status[kb_code] = {
                "status": "running",
                "started_at": datetime.now().isoformat(),
                "doc_ids": doc_ids,
            }
            
            # 1. 获取新增实体
            new_entities = await self._fetch_new_entities(kb_code, doc_ids)
            
            if not new_entities:
                logger.info(f"[RAGIndexSyncManager] 无新增实体: kb={kb_code}")
                return
            
            # 2. 批量增量更新
            await self._batch_add_entities(kb_code, kb_id, new_entities)
            
            self._sync_status[kb_code]["status"] = "completed"
            self._sync_status[kb_code]["completed_at"] = datetime.now().isoformat()
            self._sync_status[kb_code]["entity_count"] = len(new_entities)
            
            logger.info(
                f"[RAGIndexSyncManager] 同步完成: kb={kb_code}, "
                f"entities={len(new_entities)}"
            )
            
        except Exception as e:
            logger.error(f"[RAGIndexSyncManager] 同步失败: kb={kb_code}, error={e}")
            self._sync_status[kb_code] = {
                "status": "failed",
                "error": str(e),
            }
        finally:
            async with self._lock:
                if kb_code in self._sync_tasks:
                    del self._sync_tasks[kb_code]
    
    async def _fetch_new_entities(
        self,
        kb_code: str,
        doc_ids: List[str],
    ) -> List[Dict[str, str]]:
        """从图数据库获取新增实体"""
        from backend.rag.storages.nebula_storage import NebulaGraphStorage
        
        storage = NebulaGraphStorage(space=kb_code)
        
        all_entities = []
        for doc_id in doc_ids:
            entities = await storage.get_nodes_by_source_id(doc_id)
            all_entities.extend(entities)
        
        return all_entities
    
    async def _batch_add_entities(
        self,
        kb_code: str,
        kb_id: str,
        entities: List[Dict[str, str]],
    ) -> None:
        """批量添加实体到索引"""
        from backend.rag.keywords.index_manager import get_keyword_index_manager
        from backend.rag.keywords.per_kb_index import EntityData
        
        index_manager = get_keyword_index_manager()
        
        # 分批处理
        batch_size = self._config.batch_size
        for i in range(0, len(entities), batch_size):
            batch = entities[i:i + batch_size]
            
            entity_data_list = [
                EntityData(
                    entity_name=e["entity_id"],
                    entity_type=e.get("entity_type", "unknown"),
                    description=e.get("description", ""),
                    source_id=e.get("source_id", ""),
                    file_path=e.get("file_path", ""),
                )
                for e in batch
            ]
            
            await index_manager.add_entities(kb_code, kb_id, entity_data_list)
            
            logger.debug(
                f"[RAGIndexSyncManager] 批次完成: kb={kb_code}, "
                f"batch={i//batch_size + 1}, count={len(batch)}"
            )
    
    async def get_sync_status(self, kb_code: str) -> Optional[Dict[str, Any]]:
        """获取同步状态"""
        return self._sync_status.get(kb_code)
    
    async def force_sync(
        self,
        kb_code: str,
        kb_id: str,
    ) -> bool:
        """
        强制全量同步
        
        Args:
            kb_code: 知识库编码
            kb_id: 知识库 ID
            
        Returns:
            是否成功触发
        """
        from backend.rag.storages.nebula_storage import NebulaGraphStorage
        
        storage = NebulaGraphStorage(space=kb_code)
        all_entities = await storage.get_all_nodes()
        
        existing_entities = set()
        index_manager = get_keyword_index_manager()
        index = await index_manager.get_index(kb_code, kb_id)
        if index:
            existing_entities = set(index.entity_names)
        
        new_entities = [
            e for e in all_entities
            if e["entity_id"] not in existing_entities
        ]
        
        if new_entities:
            await self._batch_add_entities(kb_code, kb_id, new_entities)
        
        return True


def get_rag_index_sync_manager() -> RAGIndexSyncManager:
    """获取 RAG 索引同步管理器单例"""
    return RAGIndexSyncManager()
```

#### 4.1.2 索引同步调度器

```python
# backend/rag/sync/rag_index_sync_scheduler.py

import asyncio
import logging
from datetime import datetime, time
from typing import Optional, List

from backend.config.loggers import get_logger

logger = get_logger(__name__)


class RAGIndexSyncScheduler:
    """
    RAG 索引同步调度器
    
    职责：
    1. 定时触发全量同步
    2. 调度时间管理
    3. 资源使用控制
    """
    
    _instance: Optional["RAGIndexSyncScheduler"] = None
    
    def __new__(cls):
        if cls._instance is None:
            cls._instance = super().__new__(cls)
            cls._instance._initialized = False
        return cls._instance
    
    def __init__(self):
        if self._initialized:
            return
        
        self._initialized = True
        self._running = False
        self._task: Optional[asyncio.Task] = None
        self._sync_time = time(2, 0)  # 默认凌晨 2:00
        self._sync_kb_codes: List[str] = []
        
        logger.info("[RAGIndexSyncScheduler] 初始化完成")
    
    def set_sync_time(self, sync_time: time) -> None:
        """设置同步时间"""
        self._sync_time = sync_time
        logger.info(f"[RAGIndexSyncScheduler] 同步时间设置为: {sync_time}")
    
    async def start(self, kb_codes: List[str]) -> None:
        """启动调度器"""
        if self._running:
            logger.warning("[RAGIndexSyncScheduler] 调度器已启动")
            return
        
        self._running = True
        self._sync_kb_codes = kb_codes
        self._task = asyncio.create_task(self._run_loop())
        
        logger.info(
            f"[RAGIndexSyncScheduler] 调度器已启动, "
            f"同步时间: {self._sync_time}, 知识库: {kb_codes}"
        )
    
    async def stop(self) -> None:
        """停止调度器"""
        self._running = False
        if self._task:
            self._task.cancel()
            try:
                await self._task
            except asyncio.CancelledError:
                pass
        logger.info("[RAGIndexSyncScheduler] 调度器已停止")
    
    async def _run_loop(self) -> None:
        """调度循环"""
        while self._running:
            try:
                now = datetime.now()
                target = datetime.combine(now.date(), self._sync_time)
                
                if now > target:
                    target = target.replace(day=now.day + 1)
                
                wait_seconds = (target - now).total_seconds()
                
                logger.debug(
                    f"[RAGIndexSyncScheduler] 下次同步时间: {target}, "
                    f"等待: {wait_seconds}秒"
                )
                
                await asyncio.sleep(wait_seconds)
                
                if self._running:
                    await self._do_scheduled_sync()
                    
            except asyncio.CancelledError:
                break
            except Exception as e:
                logger.error(f"[RAGIndexSyncScheduler] 调度错误: {e}")
                await asyncio.sleep(60)
    
    async def _do_scheduled_sync(self) -> None:
        """执行定时同步"""
        logger.info("[RAGIndexSyncScheduler] 开始定时同步")
        
        from backend.rag.sync.rag_index_sync_manager import get_rag_index_sync_manager
        from backend.rag.knowledge_base.manager import get_knowledge_base_manager
        
        sync_manager = get_rag_index_sync_manager()
        
        for kb_code in self._sync_kb_codes:
            try:
                # 获取 kb_id
                kb_manager = get_knowledge_base_manager()
                session = None  # 需要从依赖注入获取
                
                # 这里需要获取 kb_id
                # 简化处理：使用 kb_code 作为 kb_id
                await sync_manager.force_sync(kb_code, kb_code)
                
            except Exception as e:
                logger.error(
                    f"[RAGIndexSyncScheduler] 同步失败: kb={kb_code}, error={e}"
                )
        
        logger.info("[RAGIndexSyncScheduler] 定时同步完成")
    
    async def trigger_manual_sync(self, kb_code: str, kb_id: str) -> bool:
        """手动触发同步"""
        from backend.rag.sync.rag_index_sync_manager import get_rag_index_sync_manager
        
        sync_manager = get_rag_index_sync_manager()
        return await sync_manager.force_sync(kb_code, kb_id)


def get_rag_index_sync_scheduler() -> RAGIndexSyncScheduler:
    """获取 RAG 索引同步调度器单例"""
    return RAGIndexSyncScheduler()
```

#### 4.1.3 NebulaGraph 存储扩展

```python
# backend/rag/storages/nebula_storage.py

class NebulaGraphStorage:
    # ... 现有方法 ...
    
    async def get_nodes_by_source_id(self, source_id: str) -> List[dict]:
        """根据 source_id 获取实体"""
        session = self._get_session()
        try:
            result = session.execute(f"USE `{self._space}`")
            if not result.is_succeeded():
                return []
            
            query = f'''
                MATCH (e:entity)
                WHERE e.source_id CONTAINS "{self._escape_string(source_id)}"
                RETURN e.entity_id as entity_id, e.entity_type as entity_type,
                       e.description as description, e.source_id as source_id,
                       e.file_path as file_path
                LIMIT 1000
            '''
            result = session.execute(query)
            
            entities = []
            for i in range(result.row_size()):
                row = result.row_values(i)
                entities.append({
                    "entity_id": self._safe_get_string(row[0]),
                    "entity_type": self._safe_get_string(row[1]),
                    "description": self._safe_get_string(row[2]),
                    "source_id": self._safe_get_string(row[3]),
                    "file_path": self._safe_get_string(row[4]),
                })
            
            return entities
        finally:
            session.release()
    
    async def get_all_nodes(self) -> List[dict]:
        """获取所有实体"""
        session = self._get_session()
        try:
            result = session.execute(f"USE `{self._space}`")
            if not result.is_succeeded():
                return []
            
            all_entities = []
            batch_size = 1000
            offset = 0
            
            while True:
                query = f'''
                    LOOKUP ON entity
                    YIELD properties(vertex).entity_id,
                          properties(vertex).entity_type,
                          properties(vertex).description,
                          properties(vertex).source_id,
                          properties(vertex).file_path
                    LIMIT {batch_size} OFFSET {offset}
                '''
                result = session.execute(query)
                
                if not result.is_succeeded() or result.row_size() == 0:
                    break
                
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
                
                offset += batch_size
                if result.row_size() < batch_size:
                    break
            
            return all_entities
        finally:
            session.release()
```

### 4.2 服务集成

#### 4.2.1 入库服务修改

```python
# backend/rag/service.py

class RAGService:
    async def process_multimodal(
        self,
        file_path: str,
        kb_id: str | None = None,
        **kwargs: Any
    ) -> Dict[str, Any]:
        # ... 现有逻辑 ...
        
        # 执行入库
        await insert_text_content_with_multimodal_content(
            lightrag=rag.lightrag,
            input=_text,
            multimodal_content=multimodal_items,
            ids=doc_id,
            file_paths=str(resolved),
            use_semantic_chunking=use_semantic_chunking,
            semantic_chunking_threshold=semantic_chunking_threshold,
            embedding_batch_size=embedding_batch_size,
        )
        
        # 异步触发索引同步（非阻塞）
        if doc_id:
            await self._trigger_index_sync(kb_id, doc_id)
        
        # ... 后续逻辑 ...
    
    async def _trigger_index_sync(self, kb_id: str, doc_id: str) -> None:
        """触发索引同步"""
        try:
            from backend.rag.sync.rag_index_sync_manager import get_rag_index_sync_manager
            
            # 获取 kb_code
            kb_manager = get_knowledge_base_manager()
            session = None  # 需要从依赖注入获取
            kb = await kb_manager.get_knowledge_base(session, kb_id)
            
            if kb:
                sync_manager = get_rag_index_sync_manager()
                await sync_manager.sync_after_insert(kb.kb_code, kb_id, [doc_id])
                
        except Exception as e:
            logger.warning(f"[RAGService] 触发索引同步失败: {e}")
```

#### 4.2.2 应用生命周期集成

```python
# backend/main.py

from contextlib import asynccontextmanager
from fastapi import FastAPI


@asynccontextmanager
async def lifespan(app: FastAPI):
    # 启动时
    await _startup()
    yield
    # 关闭时
    await _shutdown()


async def _startup():
    """应用启动初始化"""
    # ... 现有初始化 ...
    
    # 启动索引同步调度器
    from backend.rag.sync.rag_index_sync_scheduler import get_rag_index_sync_scheduler
    
    scheduler = get_rag_index_sync_scheduler()
    
    # 从配置或数据库获取需要同步的知识库列表
    kb_codes = ["rag_nuclear_electronics"]  # 从配置获取
    
    await scheduler.start(kb_codes)


async def _shutdown():
    """应用关闭清理"""
    # ... 现有清理 ...
    
    # 停止索引同步调度器
    from backend.rag.sync.rag_index_sync_scheduler import get_rag_index_sync_scheduler
    
    scheduler = get_rag_index_sync_scheduler()
    await scheduler.stop()
```

### 4.3 配置管理

```python
# backend/config/settings.py

from pydantic import BaseModel
from typing import Optional


class RAGIndexSyncSettings(BaseModel):
    """RAG 索引同步配置"""
    
    enabled: bool = True
    sync_on_insert: bool = True
    sync_interval_hours: int = 24
    sync_time: str = "02:00"
    batch_size: int = 100
    max_concurrent: int = 3
    retry_times: int = 3
    retry_delay: float = 1.0


class Settings(BaseSettings):
    # ... 现有配置 ...
    
    # RAG 索引同步配置
    rag_index_sync: RAGIndexSyncSettings = RAGIndexSyncSettings()


settings = Settings()
```

---

## 五、实施步骤

### 5.1 第一阶段：核心组件开发

1. 创建 `backend/rag/sync/` 目录
2. 实现 `RAGIndexSyncManager` 类
3. 实现 `RAGIndexSyncScheduler` 类
4. 扩展 `NebulaGraphStorage` 添加查询方法

### 5.2 第二阶段：服务集成

1. 修改 `service.py` 添加入库同步触发
2. 修改 `main.py` 集成生命周期管理
3. 添加配置加载逻辑

### 5.3 第三阶段：测试验证

1. 单元测试
2. 集成测试
3. 性能测试（不影响查询）
4. 故障恢复测试

---

## 六、测试验证

### 6.1 功能测试

| 测试项 | 验证方法 | 预期结果 |
|-------|---------|---------|
| 入库后同步 | 入库文档，检查索引 | 新实体出现在关键词索引中 |
| 定时同步 | 等待定时任务执行 | 索引与图数据库一致 |
| 手动同步 | 调用 API 触发 | 增量同步成功 |
| 查询不受影响 | 并发入库和查询 | 查询响应时间 < 500ms |

### 6.2 性能指标

| 指标 | 目标值 | 说明 |
|-----|-------|------|
| 同步触发延迟 | < 100ms | 入库后触发同步的延迟 |
| 同步吞吐量 | > 100 entities/s | 每秒处理实体数 |
| 内存占用 | < 500MB | 同步任务内存峰值 |
| 查询影响 | < 5% | 同步期间查询性能下降比例 |

---

## 七、风险评估与缓解

| 风险 | 影响 | 缓解措施 |
|-----|------|---------|
| 同步任务异常 | 索引不完整 | 定期全量同步兜底 |
| 资源竞争 | 查询性能下降 | 独立资源池，限流控制 |
| 并发冲突 | 数据不一致 | 乐观锁 + 重试机制 |
| 服务崩溃 | 同步中断 | 任务状态持久化 |

---

## 八、API 接口

### 8.1 同步状态查询

```
GET /api/rag/index-sync/status/{kb_code}

Response:
{
    "kb_code": "rag_nuclear_electronics",
    "status": "completed",
    "started_at": "2024-01-01T02:00:00",
    "completed_at": "2024-01-01T02:05:00",
    "entity_count": 1000
}
```

### 8.2 手动触发同步

```
POST /api/rag/index-sync/trigger
Body:
{
    "kb_code": "rag_nuclear_electronics",
    "kb_id": "xxx",
    "mode": "full"  // full: 全量, incremental: 增量
}

Response:
{
    "success": true,
    "message": "同步任务已触发"
}
```

---

## 九、总结

本方案通过以下设计确保不影响正常查询服务：

1. **异步非阻塞**：入库后通过异步任务触发同步，不阻塞入库流程
2. **独立资源池**：同步任务使用独立资源，不影响查询性能
3. **限流控制**：批量处理 + 并发控制，避免资源耗尽
4. **定时同步兜底**：低峰期全量同步，确保最终一致性
5. **统一命名规范**：所有模块使用 `rag` 前缀，便于维护

**命名规范总结**：
- 目录：`backend/rag/sync/`
- 类：`RAGIndexSyncManager`, `RAGIndexSyncScheduler`
- 配置：`rag_index_sync.*`
- Redis Key：`rag:index:sync:{kb_code}`
