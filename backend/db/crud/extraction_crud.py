"""抽取结果 CRUD 操作"""

from typing import Any

from sqlalchemy import delete, func, select, update
from sqlalchemy.ext.asyncio import AsyncSession

from backend.db.models import ExtractionResult
from backend.db.unit_of_work import flush_or_commit


class ExtractionCRUD:
    """抽取结果 CRUD 操作类"""

    @staticmethod
    async def create(
        session: AsyncSession,
        doc_id: str,
        task_id: str | None = None,
        user_id: str | None = None,
        document_type: str | None = None,
        page_count: int = 0,
        text_blocks_count: int = 0,
        tables_count: int = 0,
        formulas_count: int = 0,
        images_count: int = 0,
        result_data: dict[str, Any] | None = None,
        text_content: str | None = None,
        markdown_content: str | None = None,
        processing_time: float | None = None,
        confidence_score: float | None = None,
    ) -> ExtractionResult:
        """创建抽取结果记录"""
        result = ExtractionResult(
            task_id=task_id,
            doc_id=doc_id,
            user_id=user_id,
            document_type=document_type,
            page_count=page_count,
            text_blocks_count=text_blocks_count,
            tables_count=tables_count,
            formulas_count=formulas_count,
            images_count=images_count,
            result_data=result_data,
            text_content=text_content,
            markdown_content=markdown_content,
            processing_time=processing_time,
            confidence_score=confidence_score,
        )
        session.add(result)
        await flush_or_commit(session)
        await session.refresh(result)
        return result

    @staticmethod
    async def get_by_id(
        session: AsyncSession, extraction_id: str
    ) -> ExtractionResult | None:
        """根据 ID 获取抽取结果"""
        result = await session.execute(
            select(ExtractionResult).where(
                ExtractionResult.extraction_id == extraction_id
            )
        )
        return result.scalar_one_or_none()

    @staticmethod
    async def get_by_doc_id(
        session: AsyncSession, doc_id: str
    ) -> ExtractionResult | None:
        """根据文档 ID 获取抽取结果"""
        result = await session.execute(
            select(ExtractionResult)
            .where(ExtractionResult.doc_id == doc_id)
            .order_by(ExtractionResult.created_at.desc())
            .limit(1)
        )
        return result.scalar_one_or_none()

    @staticmethod
    async def get_by_task_id(
        session: AsyncSession, task_id: str
    ) -> ExtractionResult | None:
        """根据任务 ID 获取抽取结果"""
        result = await session.execute(
            select(ExtractionResult)
            .where(ExtractionResult.task_id == task_id)
            .order_by(ExtractionResult.created_at.desc())
            .limit(1)
        )
        return result.scalar_one_or_none()

    @staticmethod
    async def get_by_task_id_for_update(
        session: AsyncSession, task_id: str
    ) -> ExtractionResult | None:
        """获取并锁定任务结果，供只允许执行一次的结果物化操作使用。"""
        result = await session.execute(
            select(ExtractionResult)
            .where(ExtractionResult.task_id == task_id)
            .order_by(ExtractionResult.created_at.desc())
            .limit(1)
            .with_for_update()
        )
        return result.scalar_one_or_none()

    @staticmethod
    async def get_by_user(
        session: AsyncSession,
        user_id: str,
        page: int = 1,
        page_size: int = 20,
    ) -> list[ExtractionResult]:
        """获取用户的抽取结果列表"""
        query = select(ExtractionResult).where(ExtractionResult.user_id == user_id)
        query = query.order_by(ExtractionResult.created_at.desc())
        query = query.offset((page - 1) * page_size).limit(page_size)

        result = await session.execute(query)
        return list(result.scalars().all())

    @staticmethod
    async def update_content(
        session: AsyncSession,
        extraction_id: str,
        text_content: str | None = None,
        markdown_content: str | None = None,
    ) -> ExtractionResult | None:
        """更新抽取内容"""
        update_data = {}

        if text_content is not None:
            update_data["text_content"] = text_content
        if markdown_content is not None:
            update_data["markdown_content"] = markdown_content

        if update_data:
            await session.execute(
                update(ExtractionResult)
                .where(ExtractionResult.extraction_id == extraction_id)
                .values(**update_data)
            )
            await flush_or_commit(session)

        return await ExtractionCRUD.get_by_id(session, extraction_id)

    @staticmethod
    async def delete(session: AsyncSession, extraction_id: str) -> bool:
        """删除抽取结果"""
        result = await session.execute(
            delete(ExtractionResult).where(
                ExtractionResult.extraction_id == extraction_id
            )
        )
        await flush_or_commit(session)
        return result.rowcount > 0

    @staticmethod
    async def delete_by_doc_id(session: AsyncSession, doc_id: str) -> int:
        """根据文档 ID 删除抽取结果"""
        result = await session.execute(
            delete(ExtractionResult).where(ExtractionResult.doc_id == doc_id)
        )
        await flush_or_commit(session)
        return result.rowcount

    @staticmethod
    async def count(
        session: AsyncSession,
        user_id: str | None = None,
    ) -> int:
        """统计抽取结果数量"""
        query = select(func.count(ExtractionResult.extraction_id))

        if user_id:
            query = query.where(ExtractionResult.user_id == user_id)

        result = await session.execute(query)
        return result.scalar() or 0

    @staticmethod
    async def get_statistics(
        session: AsyncSession,
        user_id: str | None = None,
    ) -> dict[str, Any]:
        """获取抽取统计"""
        query = select(
            func.count(ExtractionResult.extraction_id).label("total"),
            func.avg(ExtractionResult.processing_time).label("avg_processing_time"),
            func.avg(ExtractionResult.confidence_score).label("avg_confidence"),
            func.sum(ExtractionResult.text_blocks_count).label("total_text_blocks"),
            func.sum(ExtractionResult.tables_count).label("total_tables"),
            func.sum(ExtractionResult.formulas_count).label("total_formulas"),
            func.sum(ExtractionResult.images_count).label("total_images"),
        )

        if user_id:
            query = query.where(ExtractionResult.user_id == user_id)

        result = await session.execute(query)
        row = result.one()

        return {
            "total": row.total or 0,
            "avg_processing_time": row.avg_processing_time or 0,
            "avg_confidence": row.avg_confidence or 0,
            "total_text_blocks": row.total_text_blocks or 0,
            "total_tables": row.total_tables or 0,
            "total_formulas": row.total_formulas or 0,
            "total_images": row.total_images or 0,
        }


extraction_crud = ExtractionCRUD()
