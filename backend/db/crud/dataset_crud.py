"""实验数据集 CRUD 操作"""

from __future__ import annotations

from typing import Optional

from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from backend.db.models.dataset import ExperimentDataset


class DatasetCRUD:
    """实验数据集 CRUD"""

    @staticmethod
    async def create(
        session: AsyncSession,
        *,
        code: str,
        name: str,
        file_path: str,
        description: str | None = None,
        file_type: str = "xlsx",
        storage_type: str = "local",
        domain: str | None = None,
        tags: list[str] | None = None,
        agent_codes: list[str] | None = None,
        is_public: bool = True,
        created_by: str | None = None,
    ) -> ExperimentDataset:
        ds = ExperimentDataset(
            code=code,
            name=name,
            file_path=file_path,
            description=description,
            file_type=file_type,
            storage_type=storage_type,
            domain=domain,
            tags=tags or [],
            agent_codes=agent_codes or [],
            is_public=is_public,
            created_by=created_by,
            status="active",
        )
        session.add(ds)
        await session.commit()
        await session.refresh(ds)
        return ds

    @staticmethod
    async def get_by_id(session: AsyncSession, dataset_id: str) -> Optional[ExperimentDataset]:
        result = await session.execute(
            select(ExperimentDataset).where(ExperimentDataset.id == dataset_id)
        )
        return result.scalar_one_or_none()

    @staticmethod
    async def get_by_code(session: AsyncSession, code: str) -> Optional[ExperimentDataset]:
        result = await session.execute(
            select(ExperimentDataset).where(ExperimentDataset.code == code)
        )
        return result.scalar_one_or_none()

    @staticmethod
    async def list_all(
        session: AsyncSession,
        *,
        domain: str | None = None,
        tags: list[str] | None = None,
        agent_code: str | None = None,
        status: str = "active",
        is_public: bool | None = None,
    ) -> list[ExperimentDataset]:
        stmt = select(ExperimentDataset).where(ExperimentDataset.status == status)
        if domain:
            stmt = stmt.where(ExperimentDataset.domain == domain)
        if is_public is not None:
            stmt = stmt.where(ExperimentDataset.is_public == is_public)
        result = await session.execute(stmt.order_by(ExperimentDataset.created_at.desc()))
        datasets = result.scalars().all()
        # Python 层过滤（JSON 字段）
        if tags:
            datasets = [
                ds for ds in datasets
                if ds.tags and any(t in ds.tags for t in tags)
            ]
        if agent_code:
            # agent_codes 为空列表 = 全局可见；非空则只对列表内 agent 可见
            datasets = [
                ds for ds in datasets
                if not ds.agent_codes or agent_code in ds.agent_codes
            ]
        return list(datasets)

    @staticmethod
    async def set_agent_codes(
        session: AsyncSession,
        dataset_id: str,
        agent_codes: list[str],
    ) -> Optional[ExperimentDataset]:
        """设置数据集绑定的智能体列表"""
        ds = await DatasetCRUD.get_by_id(session, dataset_id)
        if not ds:
            return None
        ds.agent_codes = agent_codes
        await session.commit()
        await session.refresh(ds)
        return ds

    @staticmethod
    async def update_metadata(
        session: AsyncSession,
        dataset_id: str,
        *,
        sheet_names: list[str] | None = None,
        column_schema: dict | None = None,
        row_counts: dict | None = None,
        file_size: int | None = None,
        status: str | None = None,
    ) -> Optional[ExperimentDataset]:
        ds = await DatasetCRUD.get_by_id(session, dataset_id)
        if not ds:
            return None
        if sheet_names is not None:
            ds.sheet_names = sheet_names
        if column_schema is not None:
            ds.column_schema = column_schema
        if row_counts is not None:
            ds.row_counts = row_counts
        if file_size is not None:
            ds.file_size = file_size
        if status is not None:
            ds.status = status
        await session.commit()
        await session.refresh(ds)
        return ds

    @staticmethod
    async def delete(session: AsyncSession, dataset_id: str) -> bool:
        ds = await DatasetCRUD.get_by_id(session, dataset_id)
        if not ds:
            return False
        await session.delete(ds)
        await session.commit()
        return True
