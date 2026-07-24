"""知识库 CRUD 操作"""

from datetime import date, datetime
from typing import List, Optional, Tuple

from sqlalchemy import delete, func, or_, select, update
from sqlalchemy.dialects.mysql import insert as mysql_insert
from sqlalchemy.dialects.postgresql import insert as postgresql_insert
from sqlalchemy.dialects.sqlite import insert as sqlite_insert
from sqlalchemy.exc import IntegrityError
from sqlalchemy.ext.asyncio import AsyncSession

from backend.db.models import (
    AgentKnowledgeBase,
    KnowledgeBase,
    KnowledgeBaseDirectory,
    KnowledgeBaseDocument,
    KnowledgeBaseStatus,
    PendingFile,
    SelfIngestChunk,
    SelfIngestJob,
)
from backend.db.unit_of_work import flush_or_commit
from backend.domain.knowledge_base.backend_policy import (
    LEGACY_BACKEND,
    VALID_INGESTION_BACKENDS,
    VALID_RETRIEVAL_BACKENDS,
    validate_backend_isolation,
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
        ingestion_backend: str = LEGACY_BACKEND,
        retrieval_backend: str = LEGACY_BACKEND,
        self_ingestion_enabled: bool = False,
        self_retriever_enabled: bool = False,
    ) -> KnowledgeBase:
        """创建知识库"""
        if ingestion_backend not in VALID_INGESTION_BACKENDS:
            raise ValueError(f"无效的入库后端: {ingestion_backend}")
        if retrieval_backend not in VALID_RETRIEVAL_BACKENDS:
            raise ValueError(f"无效的查询后端: {retrieval_backend}")
        validate_backend_isolation(
            ingestion_backend=ingestion_backend,
            retrieval_backend=retrieval_backend,
            self_ingestion_enabled=self_ingestion_enabled,
            self_retriever_enabled=self_retriever_enabled,
        )

        kb = KnowledgeBase(
            kb_code=kb_code,
            kb_name=kb_name,
            subject=subject,
            description=description,
            owner_id=owner_id,
            nebula_space=kb_code,
            milvus_collection_prefix=kb_code,
            redis_key_prefix=kb_code,
            ingestion_backend=ingestion_backend,
            retrieval_backend=retrieval_backend,
            self_ingestion_enabled=self_ingestion_enabled,
            self_retriever_enabled=self_retriever_enabled,
            is_default=is_default,
            status=KnowledgeBaseStatus.CREATING,
        )
        session.add(kb)
        await flush_or_commit(session)
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
        query: Optional[str] = None,
        skip: int = 0,
        limit: int = 20,
    ) -> Tuple[List[KnowledgeBase], int]:
        """获取知识库列表"""
        conditions = []
        if status:
            conditions.append(KnowledgeBase.status == status)
        if owner_id:
            conditions.append(KnowledgeBase.owner_id == owner_id)
        if query:
            like = f"%{query.strip()}%"
            conditions.append(
                or_(
                    KnowledgeBase.kb_name.ilike(like),
                    KnowledgeBase.kb_code.ilike(like),
                    KnowledgeBase.subject.ilike(like),
                )
            )

        count_stmt = select(func.count(KnowledgeBase.kb_id))
        if conditions:
            count_stmt = count_stmt.where(*conditions)
        total = await session.execute(count_stmt)
        total = total.scalar() or 0

        stmt = select(KnowledgeBase)
        if conditions:
            stmt = stmt.where(*conditions)
        stmt = stmt.offset(skip).limit(limit).order_by(
            KnowledgeBase.doc_count.desc(),
            KnowledgeBase.created_at.desc(),
        )

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
        await flush_or_commit(session)
        return await KnowledgeBaseCRUD.get_by_id(session, kb_id)

    @staticmethod
    async def update(
        session: AsyncSession,
        kb_id: str,
        **kwargs
    ) -> Optional[KnowledgeBase]:
        """更新知识库信息"""
        current = await KnowledgeBaseCRUD.get_by_id(session, kb_id)
        if not current:
            return None

        update_data = {"updated_at": datetime.now()}
        
        # 允许的字段
        allowed_fields = [
            "kb_name",
            "subject",
            "description",
            "owner_id",
            "is_default",
            "ingestion_backend",
            "retrieval_backend",
            "self_ingestion_enabled",
            "self_retriever_enabled",
        ]
        for field in allowed_fields:
            if field in kwargs and kwargs[field] is not None:
                if field == "ingestion_backend" and kwargs[field] not in VALID_INGESTION_BACKENDS:
                    raise ValueError(f"无效的入库后端: {kwargs[field]}")
                if field == "retrieval_backend" and kwargs[field] not in VALID_RETRIEVAL_BACKENDS:
                    raise ValueError(f"无效的查询后端: {kwargs[field]}")
                update_data[field] = kwargs[field]

        final_ingestion_backend = update_data.get("ingestion_backend", current.ingestion_backend) or LEGACY_BACKEND
        final_retrieval_backend = update_data.get("retrieval_backend", current.retrieval_backend) or LEGACY_BACKEND
        final_self_ingestion_enabled = update_data.get(
            "self_ingestion_enabled",
            bool(current.self_ingestion_enabled),
        )
        final_self_retriever_enabled = update_data.get(
            "self_retriever_enabled",
            bool(current.self_retriever_enabled),
        )
        validate_backend_isolation(
            ingestion_backend=final_ingestion_backend,
            retrieval_backend=final_retrieval_backend,
            self_ingestion_enabled=bool(final_self_ingestion_enabled),
            self_retriever_enabled=bool(final_self_retriever_enabled),
        )
        
        if not update_data:
            return await KnowledgeBaseCRUD.get_by_id(session, kb_id)
        
        await session.execute(
            update(KnowledgeBase)
            .where(KnowledgeBase.kb_id == kb_id)
            .values(**update_data)
        )
        await flush_or_commit(session)
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
        await flush_or_commit(session)
        return await KnowledgeBaseCRUD.get_by_id(session, kb_id)

    @staticmethod
    async def delete(session: AsyncSession, kb_id: str) -> bool:
        """删除知识库"""
        # Production databases contain a mixture of historical foreign keys:
        # some cascade on KB deletion and some (notably the self-ingestion
        # tables) do not.  Delete every KB-owned row explicitly so this works
        # against both schemas and does not leave non-FK bindings orphaned.
        child_models = (
            SelfIngestChunk,
            SelfIngestJob,
            PendingFile,
            KnowledgeBaseDocument,
            KnowledgeBaseDirectory,
            AgentKnowledgeBase,
        )

        try:
            for model in child_models:
                await session.execute(delete(model).where(model.kb_id == kb_id))

            result = await session.execute(
                delete(KnowledgeBase).where(KnowledgeBase.kb_id == kb_id)
            )
            await flush_or_commit(session)
            return result.rowcount > 0
        except Exception:
            # Restore the session before the manager attempts to reset the KB
            # status after a failed database cleanup.
            await session.rollback()
            raise


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
        source_hash: Optional[str] = None,
        object_bucket: Optional[str] = None,
        object_key: Optional[str] = None,
        object_storage_uri: Optional[str] = None,
        mime_type: Optional[str] = None,
        file_ext: Optional[str] = None,
        ingestion_backend: str = LEGACY_BACKEND,
        latest_job_id: Optional[str] = None,
        lineage_json: Optional[dict] = None,
        extra_metadata_json: Optional[dict] = None,
        chunk_count: int = 0,
        entity_count: int = 0,
        relation_count: int = 0,
    ) -> KnowledgeBaseDocument:
        """创建文档记录"""
        if ingestion_backend not in VALID_INGESTION_BACKENDS:
            raise ValueError(f"无效的入库后端: {ingestion_backend}")

        doc = KnowledgeBaseDocument(
            kb_id=kb_id,
            doc_id=doc_id,
            file_name=file_name,
            file_path=file_path,
            file_size=file_size,
            document_date=document_date,
            category_path=category_path,
            source_hash=source_hash,
            object_bucket=object_bucket,
            object_key=object_key,
            object_storage_uri=object_storage_uri,
            mime_type=mime_type,
            file_ext=file_ext,
            ingestion_backend=ingestion_backend,
            latest_job_id=latest_job_id,
            lineage_json=lineage_json,
            extra_metadata_json=extra_metadata_json,
            chunk_count=chunk_count,
            entity_count=entity_count,
            relation_count=relation_count,
            parse_status=1,
            storage_status=1,
            doc_status="visible",
            uploaded_at=datetime.now(),
            stored_at=datetime.now(),
            ingest_finished_at=datetime.now(),
            committed_at=datetime.now(),
            visible_at=datetime.now(),
        )
        session.add(doc)
        await flush_or_commit(session)
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
        conditions.append(
            or_(
                KnowledgeBaseDocument.doc_status.is_(None),
                KnowledgeBaseDocument.doc_status != "deleted",
            )
        )
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
        await flush_or_commit(session)
        return await KnowledgeBaseDocumentCRUD.get_by_doc_id(session, kb_id, doc_id)

    @staticmethod
    async def delete(session: AsyncSession, kb_id: str, doc_id: str) -> bool:
        """删除文档记录"""
        result = await session.execute(
            delete(KnowledgeBaseDocument)
            .where(KnowledgeBaseDocument.kb_id == kb_id)
            .where(KnowledgeBaseDocument.doc_id == doc_id)
        )
        await flush_or_commit(session)
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
        """原子创建或更新文档记录。"""
        values = {
            "kb_id": kb_id,
            "doc_id": doc_id,
            "file_name": file_name,
            "file_path": file_path,
            "file_size": file_size,
            "document_date": document_date,
            "category_path": category_path,
            "chunk_count": chunk_count,
            "entity_count": entity_count,
            "relation_count": relation_count,
        }
        dialect_name = session.get_bind().dialect.name
        update_fields = {
            key: value for key, value in values.items() if key not in {"kb_id", "doc_id"}
        }
        if dialect_name == "mysql":
            statement = mysql_insert(KnowledgeBaseDocument).values(**values)
            await session.execute(
                statement.on_duplicate_key_update(
                    **{
                        key: statement.inserted[key]
                        for key in values
                        if key not in {"kb_id", "doc_id"}
                    },
                    updated_at=datetime.now(),
                )
            )
        elif dialect_name == "sqlite":
            statement = sqlite_insert(KnowledgeBaseDocument).values(**values)
            await session.execute(
                statement.on_conflict_do_update(
                    index_elements=["kb_id", "doc_id"],
                    set_={
                        **{
                            key: statement.excluded[key]
                            for key in update_fields
                        },
                        "updated_at": datetime.now(),
                    },
                )
            )
        elif dialect_name == "postgresql":
            statement = postgresql_insert(KnowledgeBaseDocument).values(**values)
            await session.execute(
                statement.on_conflict_do_update(
                    index_elements=["kb_id", "doc_id"],
                    set_={
                        **{
                            key: statement.excluded[key]
                            for key in update_fields
                        },
                        "updated_at": datetime.now(),
                    },
                )
            )
        else:
            # Generic fallback: update first; a savepoint makes a concurrent
            # insert recoverable without rolling back the caller's transaction.
            updated = await session.execute(
                update(KnowledgeBaseDocument)
                .where(
                    KnowledgeBaseDocument.kb_id == kb_id,
                    KnowledgeBaseDocument.doc_id == doc_id,
                )
                .values(**update_fields, updated_at=datetime.now())
            )
            if updated.rowcount == 0:
                try:
                    async with session.begin_nested():
                        session.add(KnowledgeBaseDocument(**values))
                        await session.flush()
                except IntegrityError:
                    await session.execute(
                        update(KnowledgeBaseDocument)
                        .where(
                            KnowledgeBaseDocument.kb_id == kb_id,
                            KnowledgeBaseDocument.doc_id == doc_id,
                        )
                        .values(**update_fields, updated_at=datetime.now())
                    )
        await flush_or_commit(session)
        result = await KnowledgeBaseDocumentCRUD.get_by_doc_id(session, kb_id, doc_id)
        if result is None:
            raise RuntimeError(f"文档 upsert 后无法读取: {kb_id}/{doc_id}")
        return result


knowledge_base_crud = KnowledgeBaseCRUD()
kb_document_crud = KnowledgeBaseDocumentCRUD()
