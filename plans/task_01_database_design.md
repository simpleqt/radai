# 任务一：数据库设计

> **文档状态**：📦 任务归档（2026-03 多库改造已落地）。现行 schema 以 `backend/db/models/` 与 Alembic 为准。

## 任务概述
在现有 MySQL 数据库基础上，新增知识库管理表，并创建 NebulaGraph Schema，为多知识库功能提供数据存储基础。

## 前置条件
- MySQL 服务已运行 (`<minio_server_ip>:3307`)
- NebulaGraph 服务已运行 (`<minio_server_ip>:9669`)
- 项目数据库连接已配置完成

## 现有数据库结构分析

### 已有表结构
| 表名 | 说明 |
|-----|------|
| `users` | 用户表 |
| `api_keys` | API Key 表 |
| `documents` | 文档信息表 |
| `tasks` | 任务信息表 |
| `extraction_results` | 抽取结果表 |
| `task_logs` | 任务日志表 |
| `file_info` | 文件信息表 |

### 现有模型基类
```python
# backend/db/models/__init__.py
class Base(DeclarativeBase):
    pass
```

### 现有配置
```python
# backend/config/settings.py
mysql_host: str = "<minio_server_ip>"
mysql_port: int = 3307
mysql_user: str = "doc_agent"
mysql_database: str = "document_processor"
```

---

## 任务详情

### 1. 新增 SQLAlchemy 模型

**文件位置**: `backend/db/models/knowledge_base.py` (新建)

#### 1.1 知识库状态枚举

```python
import enum

class KnowledgeBaseStatus(enum.Enum):
    """知识库状态"""
    CREATING = "creating"
    ACTIVE = "active"
    INACTIVE = "inactive"
    DELETING = "deleting"
```

#### 1.2 知识库表模型

```python
from sqlalchemy import Column, String, Text, Integer, DateTime, Enum, Boolean
from sqlalchemy.orm import relationship
from datetime import datetime
import uuid

from backend.db.models import Base
from backend.db.models.knowledge_base import KnowledgeBaseStatus


class KnowledgeBase(Base):
    """知识库管理表"""
    
    __tablename__ = "knowledge_bases"
    
    kb_id = Column(String(36), primary_key=True, default=lambda: str(uuid.uuid4()))
    kb_code = Column(String(50), unique=True, nullable=False, index=True, comment="知识库编码，用于存储空间命名")
    kb_name = Column(String(100), nullable=False, comment="知识库名称")
    subject = Column(String(50), nullable=True, comment="学科分类")
    description = Column(Text, nullable=True, comment="描述")
    owner_id = Column(String(36), nullable=True, index=True, comment="所有者ID")
    
    # 存储配置
    nebula_space = Column(String(50), nullable=True, comment="NebulaGraph Space名称")
    milvus_collection_prefix = Column(String(50), nullable=True, comment="Milvus Collection前缀")
    redis_key_prefix = Column(String(50), nullable=True, comment="Redis Key前缀")
    
    # 统计信息
    doc_count = Column(Integer, default=0, comment="文档数量")
    entity_count = Column(Integer, default=0, comment="实体数量")
    relation_count = Column(Integer, default=0, comment="关系数量")
    
    # 状态
    status = Column(
        Enum(KnowledgeBaseStatus), 
        default=KnowledgeBaseStatus.CREATING,
        index=True,
        comment="知识库状态"
    )
    is_default = Column(Boolean, default=False, comment="是否为默认知识库")
    
    created_at = Column(DateTime, default=datetime.now, index=True)
    updated_at = Column(DateTime, default=datetime.now, onupdate=datetime.now)
    
    # 关联关系
    documents = relationship("KnowledgeBaseDocument", back_populates="knowledge_base", cascade="all, delete-orphan")
    
    __table_args__ = (
        Index("idx_kb_code", "kb_code"),
        Index("idx_kb_status", "status"),
        Index("idx_kb_owner", "owner_id"),
        Index("idx_kb_created", "created_at"),
        {"comment": "知识库管理表"}
    )
    
    def __repr__(self):
        return f"<KnowledgeBase(kb_id={self.kb_id}, kb_code={self.kb_code}, kb_name={self.kb_name})>"
```

#### 1.3 知识库文档关联表模型

```python
from sqlalchemy import Column, String, Text, Integer, BigInteger, DateTime, ForeignKey, Index, SmallInteger
from sqlalchemy.orm import relationship
from datetime import datetime


class KnowledgeBaseDocument(Base):
    """知识库文档关联表"""
    
    __tablename__ = "kb_documents"
    
    id = Column(BigInteger, primary_key=True, autoincrement=True)
    kb_id = Column(String(36), ForeignKey("knowledge_bases.kb_id"), nullable=False, index=True, comment="知识库ID")
    doc_id = Column(String(64), nullable=False, comment="文档ID (LightRAG doc_id)")
    file_name = Column(String(255), nullable=False)
    file_path = Column(String(500), nullable=True)
    file_size = Column(BigInteger, nullable=True)
    
    # 处理状态
    parse_status = Column(SmallInteger, default=0, index=True, comment="解析状态: 0-待处理, 1-成功, 2-失败")
    storage_status = Column(SmallInteger, default=0, index=True, comment="入库状态: 0-待入库, 1-已入库, 2-失败")
    error_message = Column(Text, nullable=True)
    
    # 统计
    chunk_count = Column(Integer, default=0)
    entity_count = Column(Integer, default=0)
    
    created_at = Column(DateTime, default=datetime.now)
    updated_at = Column(DateTime, default=datetime.now, onupdate=datetime.now)
    
    # 关联关系
    knowledge_base = relationship("KnowledgeBase", back_populates="documents")
    
    __table_args__ = (
        Index("uk_kb_doc", "kb_id", "doc_id", unique=True),
        Index("idx_kb_doc_parse", "parse_status"),
        Index("idx_kb_doc_storage", "storage_status"),
        {"comment": "知识库文档关联表"}
    )
    
    def __repr__(self):
        return f"<KnowledgeBaseDocument(kb_id={self.kb_id}, doc_id={self.doc_id}, file_name={self.file_name})>"
```

### 2. 更新模型导出

**文件位置**: `backend/db/models/__init__.py` (修改)

在现有文件末尾添加：

```python
from backend.db.models.knowledge_base import KnowledgeBase, KnowledgeBaseDocument, KnowledgeBaseStatus

__all__ = [
    # 现有导出...
    "KnowledgeBase",
    "KnowledgeBaseDocument", 
    "KnowledgeBaseStatus",
]
```

### 3. 新增配置项

**文件位置**: `backend/config/settings.py` (修改)

在 Settings 类中添加：

```python
# ==================== NebulaGraph 配置 ====================
nebula_host: str = Field(default="<minio_server_ip>", description="NebulaGraph 主机地址")
nebula_graph_port: int = Field(default=9669, description="NebulaGraph Graph 服务端口")
nebula_user: str = Field(default="root", description="NebulaGraph 用户名")
nebula_password: str = Field(default="", description="NebulaGraph 密码")
nebula_max_connection_pool_size: int = Field(default=100, description="NebulaGraph 连接池大小")

# ==================== 默认知识库配置 ====================
default_kb_code: str = Field(default="default", description="默认知识库编码")
```

### 4. 新增 CRUD 操作

**文件位置**: `backend/db/crud/knowledge_base_crud.py` (新建)

```python
"""知识库 CRUD 操作"""

from datetime import datetime
from typing import List, Optional, Tuple

from sqlalchemy import func, select, update, delete
from sqlalchemy.ext.asyncio import AsyncSession

from backend.db.models import KnowledgeBase, KnowledgeBaseDocument, KnowledgeBaseStatus


class KnowledgeBaseCRUD:
    """知识库 CRUD 操作类"""
    
    @staticmethod
    async def create(
        session: AsyncSession,
        kb_code: str,
        kb_name: str,
        subject: Optional[str] = None,
        description: Optional[str] = None,
        owner_id: Optional[str] = None,
        is_default: bool = False,
    ) -> KnowledgeBase:
        """创建知识库"""
        kb = KnowledgeBase(
            kb_code=kb_code,
            kb_name=kb_name,
            subject=subject,
            description=description,
            owner_id=owner_id,
            nebula_space=kb_code,
            milvus_collection_prefix=kb_code,
            redis_key_prefix=kb_code,
            is_default=is_default,
            status=KnowledgeBaseStatus.CREATING,
        )
        session.add(kb)
        await session.commit()
        await session.refresh(kb)
        return kb
    
    @staticmethod
    async def get_by_id(session: AsyncSession, kb_id: str) -> Optional[KnowledgeBase]:
        """根据 ID 获取知识库"""
        result = await session.execute(
            select(KnowledgeBase).where(KnowledgeBase.kb_id == kb_id)
        )
        return result.scalar_one_or_none()
    
    @staticmethod
    async def get_by_code(session: AsyncSession, kb_code: str) -> Optional[KnowledgeBase]:
        """根据编码获取知识库"""
        result = await session.execute(
            select(KnowledgeBase).where(KnowledgeBase.kb_code == kb_code)
        )
        return result.scalar_one_or_none()
    
    @staticmethod
    async def get_default(session: AsyncSession) -> Optional[KnowledgeBase]:
        """获取默认知识库"""
        result = await session.execute(
            select(KnowledgeBase).where(KnowledgeBase.is_default == True)
        )
        return result.scalar_one_or_none()
    
    @staticmethod
    async def list(
        session: AsyncSession,
        status: Optional[KnowledgeBaseStatus] = None,
        owner_id: Optional[str] = None,
        skip: int = 0,
        limit: int = 20,
    ) -> Tuple[List[KnowledgeBase], int]:
        """获取知识库列表"""
        conditions = []
        if status:
            conditions.append(KnowledgeBase.status == status)
        if owner_id:
            conditions.append(KnowledgeBase.owner_id == owner_id)
        
        count_stmt = select(func.count(KnowledgeBase.kb_id))
        if conditions:
            count_stmt = count_stmt.where(*conditions)
        total = await session.execute(count_stmt)
        total = total.scalar() or 0
        
        stmt = select(KnowledgeBase)
        if conditions:
            stmt = stmt.where(*conditions)
        stmt = stmt.offset(skip).limit(limit).order_by(KnowledgeBase.created_at.desc())
        
        result = await session.execute(stmt)
        return list(result.scalars().all()), total
    
    @staticmethod
    async def update_status(
        session: AsyncSession,
        kb_id: str,
        status: KnowledgeBaseStatus,
    ) -> Optional[KnowledgeBase]:
        """更新知识库状态"""
        await session.execute(
            update(KnowledgeBase)
            .where(KnowledgeBase.kb_id == kb_id)
            .values(status=status, updated_at=datetime.now())
        )
        await session.commit()
        return await KnowledgeBaseCRUD.get_by_id(session, kb_id)
    
    @staticmethod
    async def update_stats(
        session: AsyncSession,
        kb_id: str,
        doc_count: Optional[int] = None,
        entity_count: Optional[int] = None,
        relation_count: Optional[int] = None,
    ) -> Optional[KnowledgeBase]:
        """更新知识库统计信息"""
        update_data = {"updated_at": datetime.now()}
        if doc_count is not None:
            update_data["doc_count"] = doc_count
        if entity_count is not None:
            update_data["entity_count"] = entity_count
        if relation_count is not None:
            update_data["relation_count"] = relation_count
        
        await session.execute(
            update(KnowledgeBase)
            .where(KnowledgeBase.kb_id == kb_id)
            .values(**update_data)
        )
        await session.commit()
        return await KnowledgeBaseCRUD.get_by_id(session, kb_id)
    
    @staticmethod
    async def delete(session: AsyncSession, kb_id: str) -> bool:
        """删除知识库"""
        result = await session.execute(
            delete(KnowledgeBase).where(KnowledgeBase.kb_id == kb_id)
        )
        await session.commit()
        return result.rowcount > 0


class KnowledgeBaseDocumentCRUD:
    """知识库文档 CRUD 操作类"""
    
    @staticmethod
    async def create(
        session: AsyncSession,
        kb_id: str,
        doc_id: str,
        file_name: str,
        file_path: Optional[str] = None,
        file_size: Optional[int] = None,
    ) -> KnowledgeBaseDocument:
        """创建文档记录"""
        doc = KnowledgeBaseDocument(
            kb_id=kb_id,
            doc_id=doc_id,
            file_name=file_name,
            file_path=file_path,
            file_size=file_size,
        )
        session.add(doc)
        await session.commit()
        await session.refresh(doc)
        return doc
    
    @staticmethod
    async def get_by_doc_id(session: AsyncSession, kb_id: str, doc_id: str) -> Optional[KnowledgeBaseDocument]:
        """根据文档ID获取"""
        result = await session.execute(
            select(KnowledgeBaseDocument)
            .where(KnowledgeBaseDocument.kb_id == kb_id)
            .where(KnowledgeBaseDocument.doc_id == doc_id)
        )
        return result.scalar_one_or_none()
    
    @staticmethod
    async def list_by_kb(
        session: AsyncSession,
        kb_id: str,
        parse_status: Optional[int] = None,
        storage_status: Optional[int] = None,
        skip: int = 0,
        limit: int = 20,
    ) -> Tuple[List[KnowledgeBaseDocument], int]:
        """获取知识库的文档列表"""
        conditions = [KnowledgeBaseDocument.kb_id == kb_id]
        if parse_status is not None:
            conditions.append(KnowledgeBaseDocument.parse_status == parse_status)
        if storage_status is not None:
            conditions.append(KnowledgeBaseDocument.storage_status == storage_status)
        
        count_stmt = select(func.count(KnowledgeBaseDocument.id)).where(*conditions)
        total = await session.execute(count_stmt)
        total = total.scalar() or 0
        
        stmt = (
            select(KnowledgeBaseDocument)
            .where(*conditions)
            .offset(skip)
            .limit(limit)
            .order_by(KnowledgeBaseDocument.created_at.desc())
        )
        result = await session.execute(stmt)
        return list(result.scalars().all()), total
    
    @staticmethod
    async def update_status(
        session: AsyncSession,
        kb_id: str,
        doc_id: str,
        parse_status: Optional[int] = None,
        storage_status: Optional[int] = None,
        error_message: Optional[str] = None,
    ) -> Optional[KnowledgeBaseDocument]:
        """更新文档状态"""
        update_data = {"updated_at": datetime.now()}
        if parse_status is not None:
            update_data["parse_status"] = parse_status
        if storage_status is not None:
            update_data["storage_status"] = storage_status
        if error_message is not None:
            update_data["error_message"] = error_message
        
        await session.execute(
            update(KnowledgeBaseDocument)
            .where(KnowledgeBaseDocument.kb_id == kb_id)
            .where(KnowledgeBaseDocument.doc_id == doc_id)
            .values(**update_data)
        )
        await session.commit()
        return await KnowledgeBaseDocumentCRUD.get_by_doc_id(session, kb_id, doc_id)
    
    @staticmethod
    async def delete(session: AsyncSession, kb_id: str, doc_id: str) -> bool:
        """删除文档记录"""
        result = await session.execute(
            delete(KnowledgeBaseDocument)
            .where(KnowledgeBaseDocument.kb_id == kb_id)
            .where(KnowledgeBaseDocument.doc_id == doc_id)
        )
        await session.commit()
        return result.rowcount > 0


knowledge_base_crud = KnowledgeBaseCRUD()
kb_document_crud = KnowledgeBaseDocumentCRUD()
```

### 5. 数据库初始化脚本

**文件位置**: `backend/db/init_knowledge_base.py` (新建)

```python
"""知识库表初始化脚本"""

import asyncio
from sqlalchemy import text
from backend.db.connection import get_async_engine
from backend.db.models import Base


async def init_knowledge_base_tables():
    """创建知识库相关表"""
    engine = get_async_engine()
    
    async with engine.begin() as conn:
        # 创建 knowledge_bases 表
        await conn.execute(text("""
            CREATE TABLE IF NOT EXISTS knowledge_bases (
                kb_id VARCHAR(36) PRIMARY KEY,
                kb_code VARCHAR(50) UNIQUE NOT NULL,
                kb_name VARCHAR(100) NOT NULL,
                subject VARCHAR(50),
                description TEXT,
                owner_id VARCHAR(36),
                nebula_space VARCHAR(50),
                milvus_collection_prefix VARCHAR(50),
                redis_key_prefix VARCHAR(50),
                doc_count INT DEFAULT 0,
                entity_count INT DEFAULT 0,
                relation_count INT DEFAULT 0,
                status ENUM('creating', 'active', 'inactive', 'deleting') DEFAULT 'creating',
                is_default TINYINT(1) DEFAULT 0,
                created_at DATETIME DEFAULT CURRENT_TIMESTAMP,
                updated_at DATETIME DEFAULT CURRENT_TIMESTAMP ON UPDATE CURRENT_TIMESTAMP,
                INDEX idx_kb_code (kb_code),
                INDEX idx_kb_status (status),
                INDEX idx_kb_owner (owner_id),
                INDEX idx_kb_created (created_at)
            ) ENGINE=InnoDB DEFAULT CHARSET=utf8mb4 COMMENT='知识库管理表'
        """))
        
        # 创建 kb_documents 表
        await conn.execute(text("""
            CREATE TABLE IF NOT EXISTS kb_documents (
                id BIGINT AUTO_INCREMENT PRIMARY KEY,
                kb_id VARCHAR(36) NOT NULL,
                doc_id VARCHAR(64) NOT NULL,
                file_name VARCHAR(255) NOT NULL,
                file_path VARCHAR(500),
                file_size BIGINT,
                parse_status SMALLINT DEFAULT 0,
                storage_status SMALLINT DEFAULT 0,
                error_message TEXT,
                chunk_count INT DEFAULT 0,
                entity_count INT DEFAULT 0,
                created_at DATETIME DEFAULT CURRENT_TIMESTAMP,
                updated_at DATETIME DEFAULT CURRENT_TIMESTAMP ON UPDATE CURRENT_TIMESTAMP,
                UNIQUE KEY uk_kb_doc (kb_id, doc_id),
                INDEX idx_kb_id (kb_id),
                INDEX idx_parse_status (parse_status),
                INDEX idx_storage_status (storage_status),
                FOREIGN KEY (kb_id) REFERENCES knowledge_bases(kb_id) ON DELETE CASCADE
            ) ENGINE=InnoDB DEFAULT CHARSET=utf8mb4 COMMENT='知识库文档关联表'
        """))
        
        print("知识库表创建成功！")
        print("  ✓ knowledge_bases")
        print("  ✓ kb_documents")


async def create_default_knowledge_base():
    """创建默认知识库"""
    from backend.db.connection import get_async_session_factory
    from backend.db.crud.knowledge_base_crud import KnowledgeBaseCRUD
    
    factory = get_async_session_factory()
    async with factory() as session:
        crud = KnowledgeBaseCRUD()
        
        # 检查是否已存在
        existing = await crud.get_by_code(session, "default")
        if existing:
            print(f"默认知识库已存在: {existing.kb_id}")
            return existing
        
        # 创建默认知识库
        kb = await crud.create(
            session=session,
            kb_code="default",
            kb_name="默认知识库",
            description="系统默认知识库",
            is_default=True,
        )
        
        # 更新状态为 active
        from backend.db.models import KnowledgeBaseStatus
        await crud.update_status(session, kb.kb_id, KnowledgeBaseStatus.ACTIVE)
        
        print(f"默认知识库创建成功: {kb.kb_id}")
        return kb


async def main():
    """主函数"""
    print("=" * 50)
    print("知识库表初始化")
    print("=" * 50)
    
    await init_knowledge_base_tables()
    await create_default_knowledge_base()
    
    print("\n完成！")


if __name__ == "__main__":
    asyncio.run(main())
```

### 6. NebulaGraph Schema 脚本

**文件位置**: `backend/scripts/nebula_schema.py` (新建)

```python
"""NebulaGraph Schema 初始化脚本"""

import os
import asyncio
from typing import List

from nebula3.gclient.net import ConnectionPool
from nebula3.Config import Config as NebulaConfig

from backend.config.settings import settings


class NebulaSchemaManager:
    """NebulaGraph Schema 管理器"""
    
    def __init__(self):
        self.host = settings.nebula_host
        self.port = settings.nebula_graph_port
        self.user = settings.nebula_user
        self.password = settings.nebula_password
        self._pool = None
    
    def connect(self):
        """建立连接"""
        config = NebulaConfig()
        config.max_connection_pool_size = settings.nebula_max_connection_pool_size
        
        self._pool = ConnectionPool()
        ok = self._pool.init([(self.host, self.port)], config)
        if not ok:
            raise RuntimeError(f"无法连接到 NebulaGraph: {self.host}:{self.port}")
        print(f"已连接到 NebulaGraph: {self.host}:{self.port}")
    
    def close(self):
        """关闭连接"""
        if self._pool:
            self._pool.close()
    
    def _get_session(self):
        """获取会话"""
        return self._pool.get_session(self.user, self.password)
    
    def create_space(self, space_name: str):
        """创建 Space"""
        session = self._get_session()
        try:
            nGQL = f'CREATE SPACE IF NOT EXISTS `{space_name}` (vid_type=FIXED_STRING(256))'
            result = session.execute(nGQL)
            if result.is_succeeded():
                print(f"  ✓ Space '{space_name}' 创建成功")
            else:
                print(f"  ✗ Space '{space_name}' 创建失败: {result.error_msg()}")
        finally:
            session.release()
    
    def drop_space(self, space_name: str):
        """删除 Space"""
        session = self._get_session()
        try:
            nGQL = f'DROP SPACE IF EXISTS `{space_name}`'
            result = session.execute(nGQL)
            if result.is_succeeded():
                print(f"  ✓ Space '{space_name}' 删除成功")
            else:
                print(f"  ✗ Space '{space_name}' 删除失败: {result.error_msg()}")
        finally:
            session.release()
    
    def create_schema(self, space_name: str):
        """在指定 Space 中创建 Schema"""
        session = self._get_session()
        try:
            schema_statements = [
                f'USE `{space_name}`',
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
            
            for stmt in schema_statements:
                result = session.execute(stmt)
                if not result.is_succeeded() and "Existed" not in result.error_msg():
                    print(f"    警告: {result.error_msg()}")
            
            print(f"  ✓ Schema 创建完成: {space_name}")
        finally:
            session.release()
    
    def init_knowledge_base(self, kb_code: str):
        """初始化知识库的 NebulaGraph 存储"""
        print(f"初始化知识库 '{kb_code}' 的 NebulaGraph 存储...")
        self.create_space(kb_code)
        self.create_schema(kb_code)
    
    def delete_knowledge_base(self, kb_code: str):
        """删除知识库的 NebulaGraph 存储"""
        print(f"删除知识库 '{kb_code}' 的 NebulaGraph 存储...")
        self.drop_space(kb_code)


def init_default_kb():
    """初始化默认知识库"""
    manager = NebulaSchemaManager()
    try:
        manager.connect()
        manager.init_knowledge_base("default")
        print("\n默认知识库 NebulaGraph 存储初始化完成！")
    finally:
        manager.close()


if __name__ == "__main__":
    init_default_kb()
```

---

## 文件清单

| 操作 | 文件路径 | 说明 |
|-----|---------|------|
| 新建 | `backend/db/models/knowledge_base.py` | 知识库模型定义 |
| 修改 | `backend/db/models/__init__.py` | 导出新模型 |
| 新建 | `backend/db/crud/knowledge_base_crud.py` | CRUD 操作 |
| 修改 | `backend/config/settings.py` | 添加 NebulaGraph 配置 |
| 新建 | `backend/db/init_knowledge_base.py` | 表初始化脚本 |
| 新建 | `backend/scripts/nebula_schema.py` | NebulaGraph Schema 脚本 |

---

## 验收标准

### 数据库表
- [ ] `knowledge_bases` 表创建成功
- [ ] `kb_documents` 表创建成功
- [ ] 外键约束正常工作
- [ ] 索引创建成功

### SQLAlchemy 模型
- [ ] 模型可正常导入
- [ ] 模型字段映射正确
- [ ] 关联关系定义正确

### CRUD 操作
- [ ] 知识库 CRUD 操作正常
- [ ] 文档关联 CRUD 操作正常
- [ ] 分页查询正常

### NebulaGraph
- [ ] 连接 NebulaGraph 成功
- [ ] Space 创建成功
- [ ] Schema 创建成功

### 单元测试
- [ ] 模型测试通过
- [ ] CRUD 测试通过

---

## 执行步骤

```bash
# 1. 创建数据库表
cd backend
python -m db.init_knowledge_base

# 2. 初始化 NebulaGraph Schema
python -m scripts.nebula_schema

# 3. 验证表结构
mysql -h <minio_server_ip> -P 3307 -u doc_agent -p
USE document_processor;
SHOW TABLES;
DESC knowledge_bases;
DESC kb_documents;
```

---

## 依赖关系
- 无前置依赖，为其他任务的基础

## 预计工期
1 天
