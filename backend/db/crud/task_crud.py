"""任务 CRUD 操作"""

from datetime import datetime
from typing import Any

from sqlalchemy import and_, delete, func, select, update
from sqlalchemy.ext.asyncio import AsyncSession

from backend.db.models import Task, TaskStatus


class TaskCRUD:
    """任务 CRUD 操作类"""

    @staticmethod
    async def create(
        session: AsyncSession,
        doc_id: str,
        user_id: str | None = None,
        batch_id: str | None = None,
        options: dict[str, Any] | None = None,
        priority: int = 0,
    ) -> Task:
        """创建任务记录"""
        task = Task(
            doc_id=doc_id,
            user_id=user_id,
            batch_id=batch_id,
            status=TaskStatus.PENDING.value,
            progress=0,
            options=options,
            priority=priority,
        )
        session.add(task)
        await session.commit()
        await session.refresh(task)
        return task

    @staticmethod
    async def get_by_id(session: AsyncSession, task_id: str) -> Task | None:
        """根据 ID 获取任务"""
        result = await session.execute(select(Task).where(Task.task_id == task_id))
        return result.scalar_one_or_none()

    @staticmethod
    async def get_by_doc_id(session: AsyncSession, doc_id: str) -> list[Task]:
        """根据文档 ID 获取任务列表"""
        result = await session.execute(
            select(Task).where(Task.doc_id == doc_id).order_by(Task.created_at.desc())
        )
        return list(result.scalars().all())

    @staticmethod
    async def get_by_batch_id(session: AsyncSession, batch_id: str) -> list[Task]:
        """根据批次 ID 获取任务列表"""
        result = await session.execute(
            select(Task)
            .where(Task.batch_id == batch_id)
            .order_by(Task.created_at.desc())
        )
        return list(result.scalars().all())

    @staticmethod
    async def get_by_user(
        session: AsyncSession,
        user_id: str,
        status: str | None = None,
        page: int = 1,
        page_size: int = 20,
    ) -> list[Task]:
        """获取用户的任务列表"""
        query = select(Task).where(Task.user_id == user_id)

        if status:
            query = query.where(Task.status == status)

        query = query.order_by(Task.created_at.desc())
        query = query.offset((page - 1) * page_size).limit(page_size)

        result = await session.execute(query)
        return list(result.scalars().all())

    @staticmethod
    async def update_status(
        session: AsyncSession,
        task_id: str,
        status: str,
        progress: int | None = None,
        error: str | None = None,
        error_detail: str | None = None,
        result: dict[str, Any] | None = None,
        output_path: str | None = None,
    ) -> Task | None:
        """更新任务状态"""
        update_data = {
            "status": status,
            "updated_at": datetime.now(),
        }

        if progress is not None:
            update_data["progress"] = progress

        if error:
            update_data["error"] = error

        if error_detail:
            update_data["error_detail"] = error_detail

        if result:
            update_data["result"] = result

        if output_path:
            update_data["output_path"] = output_path

        if status == TaskStatus.PROCESSING.value and not update_data.get("started_at"):
            update_data["started_at"] = datetime.now()

        if status in [TaskStatus.COMPLETED.value, TaskStatus.FAILED.value]:
            update_data["completed_at"] = datetime.now()

        await session.execute(
            update(Task).where(Task.task_id == task_id).values(**update_data)
        )
        await session.commit()

        return await TaskCRUD.get_by_id(session, task_id)

    @staticmethod
    async def update_progress(
        session: AsyncSession,
        task_id: str,
        progress: int,
    ) -> Task | None:
        """更新任务进度"""
        await session.execute(
            update(Task)
            .where(Task.task_id == task_id)
            .values(
                progress=min(100, max(0, progress)),
                updated_at=datetime.now(),
            )
        )
        await session.commit()

        return await TaskCRUD.get_by_id(session, task_id)

    @staticmethod
    async def increment_retry(session: AsyncSession, task_id: str) -> Task | None:
        """增加重试次数"""
        task = await TaskCRUD.get_by_id(session, task_id)
        if task:
            new_retry = task.retry_count + 1
            await session.execute(
                update(Task)
                .where(Task.task_id == task_id)
                .values(
                    retry_count=new_retry,
                    status=TaskStatus.PENDING.value,
                    updated_at=datetime.now(),
                )
            )
            await session.commit()
            return await TaskCRUD.get_by_id(session, task_id)
        return None

    @staticmethod
    async def delete(session: AsyncSession, task_id: str) -> bool:
        """删除任务"""
        result = await session.execute(delete(Task).where(Task.task_id == task_id))
        await session.commit()
        return result.rowcount > 0

    @staticmethod
    async def count(
        session: AsyncSession,
        user_id: str | None = None,
        status: str | None = None,
    ) -> int:
        """统计任务数量"""
        query = select(func.count(Task.task_id))

        if user_id:
            query = query.where(Task.user_id == user_id)
        if status:
            query = query.where(Task.status == status)

        result = await session.execute(query)
        return result.scalar() or 0

    @staticmethod
    async def get_pending_tasks(
        session: AsyncSession,
        limit: int = 100,
    ) -> list[Task]:
        """获取待处理任务"""
        result = await session.execute(
            select(Task)
            .where(Task.status == TaskStatus.PENDING.value)
            .order_by(Task.priority.desc(), Task.created_at.asc())
            .limit(limit)
        )
        return list(result.scalars().all())

    @staticmethod
    async def get_processing_tasks(
        session: AsyncSession,
        timeout_seconds: int = 3600,
    ) -> list[Task]:
        """获取处理中任务（超时检测）"""
        timeout_threshold = datetime.now().timestamp() - timeout_seconds
        threshold_time = datetime.fromtimestamp(timeout_threshold)

        result = await session.execute(
            select(Task)
            .where(
                and_(
                    Task.status == TaskStatus.PROCESSING.value,
                    Task.started_at < threshold_time,
                )
            )
            .order_by(Task.started_at.asc())
        )
        return list(result.scalars().all())

    @staticmethod
    async def get_statistics(
        session: AsyncSession,
        user_id: str | None = None,
    ) -> dict[str, int]:
        """获取任务统计"""
        query = select(Task.status, func.count(Task.task_id)).group_by(Task.status)

        if user_id:
            query = query.where(Task.user_id == user_id)

        result = await session.execute(query)
        stats = {"total": 0, "pending": 0, "processing": 0, "completed": 0, "failed": 0}

        for status, count in result.all():
            stats[status] = count
            stats["total"] += count

        return stats


task_crud = TaskCRUD()
