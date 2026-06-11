"""知识库 CRUD 操作"""

from datetime import date, datetime
from typing import List, Optional, Tuple

from sqlalchemy import delete, func, select, update
from sqlalchemy.ext.asyncio import AsyncSession

from backend.db.models import (
    KnowledgeBase,
    KnowledgeBaseDocument,
    KnowledgeBaseStatus,
    PendingFile,
)


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
    async def get_by_code(
        session: AsyncSession, kb_code: str
    ) -> Optional[KnowledgeBase]:
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
    async def update(
        session: AsyncSession,
        kb_id: str,
        **kwargs
    ) -> Optional[KnowledgeBase]:
        """更新知识库信息"""
        update_data = {"updated_at": datetime.now()}
        
        # 允许的字段
        allowed_fields = ["kb_name", "subject", "description", "owner_id", "is_default"]
        for field in allowed_fields:
            if field in kwargs and kwargs[field] is not None:
                update_data[field] = kwargs[field]
        
        if not update_data:
            return await KnowledgeBaseCRUD.get_by_id(session, kb_id)
        
        await session.execute(
            update(KnowledgeBase)
            .where(KnowledgeBase.kb_id == kb_id)
            .values(**update_data)
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
        chunk_count: Optional[int] = None,
    ) -> Optional[KnowledgeBase]:
        """更新知识库统计信息"""
        update_data = {"updated_at": datetime.now()}
        if doc_count is not None:
            update_data["doc_count"] = doc_count
        if entity_count is not None:
            update_data["entity_count"] = entity_count
        if relation_count is not None:
            update_data["relation_count"] = relation_count
        if chunk_count is not None:
            update_data["chunk_count"] = chunk_count

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
        # Delete child rows first to satisfy FK constraints.
        await session.execute(
            delete(PendingFile).where(PendingFile.kb_id == kb_id)
        )
        await session.execute(
            delete(KnowledgeBaseDocument).where(KnowledgeBaseDocument.kb_id == kb_id)
        )

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
        document_date: Optional[date] = None,
        category_path: Optional[str] = None,
        chunk_count: int = 0,
        entity_count: int = 0,
        relation_count: int = 0,
    ) -> KnowledgeBaseDocument:
        """创建文档记录"""
        doc = KnowledgeBaseDocument(
            kb_id=kb_id,
            doc_id=doc_id,
            file_name=file_name,
            file_path=file_path,
            file_size=file_size,
            document_date=document_date,
            category_path=category_path,
            chunk_count=chunk_count,
            entity_count=entity_count,
            relation_count=relation_count,
            parse_status=1,
            storage_status=1,
        )
        session.add(doc)
        await session.commit()
        await session.refresh(doc)
        return doc

    @staticmethod
    async def get_by_doc_id(
        session: AsyncSession, kb_id: str, doc_id: str
    ) -> Optional[KnowledgeBaseDocument]:
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

    @staticmethod
    async def upsert(
        session: AsyncSession,
        kb_id: str,
        doc_id: str,
        file_name: str,
        file_path: Optional[str] = None,
        file_size: Optional[int] = None,
        document_date: Optional[date] = None,
        category_path: Optional[str] = None,
        chunk_count: int = 0,
        entity_count: int = 0,
        relation_count: int = 0,
    ) -> KnowledgeBaseDocument:
        """创建或更新文档记录"""
        existing = await KnowledgeBaseDocumentCRUD.get_by_doc_id(session, kb_id, doc_id)
        
        if existing:
            update_data = {
                "file_name": file_name,
                "file_path": file_path,
                "file_size": file_size,
                "document_date": document_date,
                "category_path": category_path,
                "chunk_count": chunk_count,
                "entity_count": entity_count,
                "relation_count": relation_count,
                "updated_at": datetime.now(),
            }
            await session.execute(
                update(KnowledgeBaseDocument)
                .where(KnowledgeBaseDocument.id == existing.id)
                .values(**update_data)
            )
            await session.commit()
            await session.refresh(existing)
            return existing
        else:
            return await KnowledgeBaseDocumentCRUD.create(
                session=session,
                kb_id=kb_id,
                doc_id=doc_id,
                file_name=file_name,
                file_path=file_path,
                file_size=file_size,
                document_date=document_date,
                category_path=category_path,
                chunk_count=chunk_count,
                entity_count=entity_count,
                relation_count=relation_count,
            )


knowledge_base_crud = KnowledgeBaseCRUD()
kb_document_crud = KnowledgeBaseDocumentCRUD()
