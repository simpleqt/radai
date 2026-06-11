"""文档 CRUD 操作"""

from datetime import datetime

from sqlalchemy import delete, func, select, update
from sqlalchemy.ext.asyncio import AsyncSession

from backend.db.models import Document


class DocumentCRUD:
    """文档 CRUD 操作类"""

    @staticmethod
    async def create(
        session: AsyncSession,
        filename: str,
        document_type: str,
        file_size: int,
        file_path: str | None = None,
        user_id: str | None = None,
        file_hash: str | None = None,
        mime_type: str | None = None,
    ) -> Document:
        """创建文档记录"""
        doc = Document(
            filename=filename,
            document_type=document_type,
            file_size=file_size,
            file_path=file_path,
            user_id=user_id,
            file_hash=file_hash,
            mime_type=mime_type,
            status="pending",
        )
        session.add(doc)
        await session.commit()
        await session.refresh(doc)
        return doc

    @staticmethod
    async def get_by_id(session: AsyncSession, doc_id: str) -> Document | None:
        """根据 ID 获取文档"""
        result = await session.execute(
            select(Document).where(Document.doc_id == doc_id)
        )
        return result.scalar_one_or_none()

    @staticmethod
    async def get_by_file_hash(
        session: AsyncSession, file_hash: str
    ) -> Document | None:
        """根据文件 hash 获取文档"""
        result = await session.execute(
            select(Document).where(Document.file_hash == file_hash)
        )
        return result.scalar_one_or_none()

    @staticmethod
    async def get_by_user(
        session: AsyncSession,
        user_id: str,
        status: str | None = None,
        page: int = 1,
        page_size: int = 20,
    ) -> list[Document]:
        """获取用户的文档列表"""
        query = select(Document).where(Document.user_id == user_id)

        if status:
            query = query.where(Document.status == status)

        query = query.order_by(Document.upload_time.desc())
        query = query.offset((page - 1) * page_size).limit(page_size)

        result = await session.execute(query)
        return list(result.scalars().all())

    @staticmethod
    async def update_status(
        session: AsyncSession,
        doc_id: str,
        status: str,
        error_message: str | None = None,
        page_count: int | None = None,
    ) -> Document | None:
        """更新文档状态"""
        update_data = {
            "status": status,
            "updated_at": datetime.now(),
        }

        if error_message:
            update_data["error_message"] = error_message

        if page_count is not None:
            update_data["page_count"] = page_count

        await session.execute(
            update(Document).where(Document.doc_id == doc_id).values(**update_data)
        )
        await session.commit()

        return await DocumentCRUD.get_by_id(session, doc_id)

    @staticmethod
    async def update_file_path(
        session: AsyncSession,
        doc_id: str,
        file_path: str,
    ) -> Document | None:
        """更新文件路径"""
        await session.execute(
            update(Document)
            .where(Document.doc_id == doc_id)
            .values(
                file_path=file_path,
                updated_at=datetime.now(),
            )
        )
        await session.commit()

        return await DocumentCRUD.get_by_id(session, doc_id)

    @staticmethod
    async def delete(session: AsyncSession, doc_id: str) -> bool:
        """删除文档"""
        result = await session.execute(
            delete(Document).where(Document.doc_id == doc_id)
        )
        await session.commit()
        return result.rowcount > 0

    @staticmethod
    async def count(
        session: AsyncSession,
        user_id: str | None = None,
        status: str | None = None,
    ) -> int:
        """统计文档数量"""
        query = select(func.count(Document.doc_id))

        if user_id:
            query = query.where(Document.user_id == user_id)
        if status:
            query = query.where(Document.status == status)

        result = await session.execute(query)
        return result.scalar() or 0

    @staticmethod
    async def list_recent(
        session: AsyncSession,
        user_id: str | None = None,
        limit: int = 10,
    ) -> list[Document]:
        """获取最近文档"""
        query = select(Document)

        if user_id:
            query = query.where(Document.user_id == user_id)

        query = query.order_by(Document.upload_time.desc()).limit(limit)

        result = await session.execute(query)
        return list(result.scalars().all())


document_crud = DocumentCRUD()
