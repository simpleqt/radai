# 任务六：部署与配置

> **文档状态**：📦 任务归档（2026-03 多知识库改造）。  
> **现行运维**：[docs/docs_backend/deployment_runbook.md](../docs/docs_backend/deployment_runbook.md) · `Makefile` · [plans/DEPLOYMENT.md](./DEPLOYMENT.md)（环境地址需按实际 `.env` 核对）  
> **检索配置**：生产见 [docs/知识库算法和架构.md](../docs/知识库算法和架构.md)（DirectRetriever、`USE_DIRECT_RETRIEVER`、Profile）

> ⚠️ 本文示例含历史内网地址与占位密码，**勿将真实密钥写入仓库**；以 `backend/.env` 为准。

## 任务概述
配置部署环境，编写启动脚本，确保多知识库功能可以正常运行。

## 前置条件
- 任务一至五完成
- NebulaGraph 服务可用（<minio_server_ip>:9669）

## 任务详情

### 1. 环境变量配置

**文件位置**: `backend/.env`

```bash
# ============ NebulaGraph 配置 ============
NEBULA_HOST=<minio_server_ip>
NEBULA_GRAPH_PORT=9669
NEBULA_USER=root
NEBULA_PASSWORD=<从密钥管理注入，勿提交仓库>
NEBULA_MAX_CONNECTION_POOL_SIZE=100

# ============ Milvus 配置 ============
MILVUS_HOST=localhost
MILVUS_PORT=19530

# ============ Redis 配置 ============
REDIS_HOST=localhost
REDIS_PORT=6379
REDIS_DB=0

# ============ MySQL 配置 ============
MYSQL_HOST=localhost
MYSQL_PORT=3306
MYSQL_USER=root
MYSQL_PASSWORD=your_password
MYSQL_DATABASE=lightrag

# ============ 知识库配置 ============
DEFAULT_KB_CODE=default

# ============ RAG 检索（生产，2026-05 起） ============
USE_DIRECT_RETRIEVER=true
# RAG_RETRIEVAL_PROFILE=domain
# RAG_KB_RETRIEVAL_PROFILE_MAP={}
# MULTI_KB_PATH_A_QUERY_ONLY=1
# MULTI_KB_SEARCH_DEFAULT_TOP_K=10
# MULTI_KB_SEARCH_DEFAULT_CHUNK_TOP_K=6
# RAG_DEFAULT_RETRIEVAL_PROFILE=default

# ============ LLM 配置 ============
# 保持现有配置
```

### 2. 依赖安装

**文件位置**: `backend/requirements.txt` (更新)

```txt
# 新增依赖
nebula3-python>=3.4.0

# 现有依赖保持不变
```

### 3. 数据库初始化脚本

**实际落地**：`make init-db` → `backend/db/init_db.py`；知识库表亦可参考 `backend/db/init_knowledge_base.py`。

**下文为任务期草案**（`backend/scripts/init_knowledge_base_tables.py` 可能未按此路径保留）：

```python
"""
初始化知识库相关数据库表

执行方式：
python -m scripts.init_knowledge_base_tables
"""
import asyncio
from sqlalchemy import text
from db.database import engine
from core.config import settings

async def init_tables():
    """创建知识库管理表"""
    async with engine.begin() as conn:
        # 读取 SQL 文件或直接执行
        await conn.execute(text("""
            CREATE TABLE IF NOT EXISTS knowledge_bases (...)
        """))
        print("知识库表创建成功")

if __name__ == "__main__":
    asyncio.run(init_tables())
```

### 4. 服务启动脚本

**文件位置**: `backend/scripts/start_knowledge_base_services.sh`

```bash
#!/bin/bash

# 多知识库服务启动脚本

echo "Starting Knowledge Base Services..."

# 1. 初始化数据库表
echo "Initializing database tables..."
python -m scripts.init_knowledge_base_tables

# 2. 验证 NebulaGraph 连接
echo "Verifying NebulaGraph connection..."
python -c "
from nebula3.gclient.net import ConnectionPool
from nebula3.Config import Config
import os

config = Config()
pool = ConnectionPool()
ok = pool.init([(os.getenv('NEBULA_HOST', 'localhost'), int(os.getenv('NEBULA_GRAPH_PORT', '9669')))], config)
print('NebulaGraph connection:', 'OK' if ok else 'FAILED')
pool.close()
"

# 3. 创建默认知识库（如果不存在）
echo "Ensuring default knowledge base exists..."
python -m scripts.ensure_default_kb

# 4. 启动 API 服务
echo "Starting API server..."
# 使用现有启动方式

echo "Knowledge Base Services started successfully!"
```

### 5. 默认知识库初始化

**文件位置**: `backend/scripts/ensure_default_kb.py`

```python
"""
确保默认知识库存在

执行方式：
python -m scripts.ensure_default_kb
"""
import asyncio
from db.database import get_db
from rag.knowledge_base_manager import KnowledgeBaseManager
from api.schemas.knowledge_base import KnowledgeBaseCreate

async def ensure_default_kb():
    async for db in get_db():
        kb_manager = KnowledgeBaseManager(db)
        
        # 检查是否存在
        default_kb = await kb_manager.get_by_code("default")
        if default_kb:
            print(f"默认知识库已存在: {default_kb.kb_id}")
            return
        
        # 创建默认知识库
        kb_data = KnowledgeBaseCreate(
            kb_code="default",
            kb_name="默认知识库",
            description="系统默认知识库",
            is_default=True
        )
        new_kb = await kb_manager.create_knowledge_base(kb_data)
        print(f"默认知识库创建成功: {new_kb.kb_id}")
        break

if __name__ == "__main__":
    asyncio.run(ensure_default_kb())
```

### 6. 健康检查脚本

**文件位置**: `backend/scripts/health_check.py`

```python
"""
健康检查脚本

检查所有依赖服务是否可用
"""
import asyncio
import os

async def check_mysql():
    """检查 MySQL 连接"""
    try:
        from db.database import engine
        async with engine.connect() as conn:
            await conn.execute(text("SELECT 1"))
        return True, "MySQL: OK"
    except Exception as e:
        return False, f"MySQL: FAILED - {e}"

async def check_nebula():
    """检查 NebulaGraph 连接"""
    try:
        from nebula3.gclient.net import ConnectionPool
        from nebula3.Config import Config
        config = Config()
        pool = ConnectionPool()
        ok = pool.init([(os.getenv('NEBULA_HOST'), int(os.getenv('NEBULA_GRAPH_PORT')))], config)
        pool.close()
        return ok, f"NebulaGraph: {'OK' if ok else 'FAILED'}"
    except Exception as e:
        return False, f"NebulaGraph: FAILED - {e}"

async def check_milvus():
    """检查 Milvus 连接"""
    # 实现检查逻辑
    pass

async def check_redis():
    """检查 Redis 连接"""
    # 实现检查逻辑
    pass

async def main():
    checks = [
        check_mysql(),
        check_nebula(),
        check_milvus(),
        check_redis(),
    ]
    results = await asyncio.gather(*checks)
    
    print("\n=== Health Check Results ===")
    all_ok = True
    for ok, msg in results:
        print(msg)
        if not ok:
            all_ok = False
    
    return 0 if all_ok else 1

if __name__ == "__main__":
    exit(asyncio.run(main()))
```

### 7. Docker 配置（可选）

**文件位置**: `backend/docker-compose.kb.yml`

```yaml
version: '3.8'

services:
  # NebulaGraph 已在远程服务器，无需本地部署
  
  # 如果需要本地测试环境
  nebula-test:
    image: vesoft/nebula-graphd:v3.4.0
    # ... 配置
```

## 部署检查清单

- [ ] 环境变量配置正确
- [ ] nebula3-python 依赖安装成功
- [ ] MySQL 表创建成功
- [ ] NebulaGraph 连接正常
- [ ] Milvus 连接正常
- [ ] Redis 连接正常
- [ ] 默认知识库创建成功
- [ ] API 服务启动正常
- [ ] 健康检查通过

## 验收标准
- [ ] 所有环境变量配置完成
- [ ] 依赖安装无错误
- [ ] 数据库初始化脚本可执行
- [ ] 服务启动脚本可执行
- [ ] 健康检查脚本返回正常
- [ ] 默认知识库自动创建

## 依赖关系
- 依赖任务一至五

## 预计工期
1 天

---

## 现行运维文档

多库部署、进程管理、健康检查与 **RAG/Profile 环境变量** 以现行手册为准：

- [docs/docs_backend/deployment_runbook.md](../docs/docs_backend/deployment_runbook.md)
- [plans/DEPLOYMENT.md](./DEPLOYMENT.md)（基础设施地址需与 `backend/.env` 核对）
- [docs/知识库算法和架构.md](../docs/知识库算法和架构.md)（检索生产配置）
