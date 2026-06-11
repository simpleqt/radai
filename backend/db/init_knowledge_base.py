"""知识库表初始化脚本"""

import asyncio

from sqlalchemy import text

from backend.db.connection import get_async_engine, get_async_session_factory
from backend.db.models import KnowledgeBaseStatus


async def init_knowledge_base_tables():
    """创建知识库相关表"""
    engine = get_async_engine()

    async with engine.begin() as conn:
        await conn.execute(
            text("""
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
        """)
        )

        await conn.execute(
            text("""
            CREATE TABLE IF NOT EXISTS kb_documents (
                id BIGINT AUTO_INCREMENT PRIMARY KEY,
                kb_id VARCHAR(36) NOT NULL,
                doc_id VARCHAR(64) NOT NULL,
                file_name VARCHAR(255) NOT NULL,
                file_path VARCHAR(500),
                file_size BIGINT,
                document_date DATE NULL,
                parse_status SMALLINT DEFAULT 0,
                storage_status SMALLINT DEFAULT 0,
                error_message TEXT,
                chunk_count INT DEFAULT 0,
                entity_count INT DEFAULT 0,
                relation_count INT DEFAULT 0,
                created_at DATETIME DEFAULT CURRENT_TIMESTAMP,
                updated_at DATETIME DEFAULT CURRENT_TIMESTAMP ON UPDATE CURRENT_TIMESTAMP,
                UNIQUE KEY uk_kb_doc (kb_id, doc_id),
                INDEX idx_kb_id (kb_id),
                INDEX idx_parse_status (parse_status),
                INDEX idx_storage_status (storage_status),
                INDEX idx_kb_doc_date (kb_id, document_date),
                FOREIGN KEY (kb_id) REFERENCES knowledge_bases(kb_id) ON DELETE CASCADE
            ) ENGINE=InnoDB DEFAULT CHARSET=utf8mb4 COMMENT='知识库文档关联表'
        """)
        )

        print("知识库表创建成功！")
        print("  ✓ knowledge_bases")
        print("  ✓ kb_documents")


async def create_default_knowledge_base():
    """创建默认知识库"""
    from backend.db.crud.knowledge_base_crud import KnowledgeBaseCRUD

    factory = get_async_session_factory()
    async with factory() as session:
        crud = KnowledgeBaseCRUD()

        existing = await crud.get_by_code(session, "default")
        if existing:
            print(f"默认知识库已存在: {existing.kb_id}")
            return existing

        kb = await crud.create(
            session=session,
            kb_code="default",
            kb_name="默认知识库",
            description="系统默认知识库",
            is_default=True,
        )

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
