"""实验数据集 CRUD 操作"""

from __future__ import annotations

from typing import Optional

from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from backend.db.models.dataset import ExperimentDataset
from backend.db.unit_of_work import flush_or_commit


def _agent_code_aliases(agent_code: str | None) -> set[str]:
    normalized = str(agent_code or "").strip().lower()
    if not normalized:
        return set()
    aliases = {normalized}
    legacy_to_runtime = {
        "ne_full_feature": "ne",
        "csh_full_feature": "csh",
    }
    runtime_to_legacy = {value: key for key, value in legacy_to_runtime.items()}
    if normalized in legacy_to_runtime:
        aliases.add(legacy_to_runtime[normalized])
    if normalized in runtime_to_legacy:
        aliases.add(runtime_to_legacy[normalized])
    return aliases


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
        await flush_or_commit(session)
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
        status: str | None = "active",
        is_public: bool | None = None,
        offset: int = 0,
        limit: int = 200,
    ) -> list[ExperimentDataset]:
        offset = max(0, int(offset))
        limit = min(500, max(1, int(limit)))
        stmt = select(ExperimentDataset)
        if status:
            stmt = stmt.where(ExperimentDataset.status == status)
        if domain:
            stmt = stmt.where(ExperimentDataset.domain == domain)
        if is_public is not None:
            stmt = stmt.where(ExperimentDataset.is_public == is_public)
        # JSON filters remain in Python for cross-database compatibility, but
        # the candidate scan is bounded so this method can never materialize
        # the whole table.
        candidate_limit = min(2000, max(limit + offset, limit * 5))
        result = await session.execute(
            stmt.order_by(ExperimentDataset.created_at.desc()).limit(candidate_limit)
        )
        datasets = result.scalars().all()
        # Python 层过滤（JSON 字段）
        if tags:
            datasets = [
                ds for ds in datasets
                if ds.tags and any(t in ds.tags for t in tags)
            ]
        if agent_code:
            # agent_codes 为空列表 = 全局可见；非空则只对列表内 agent 可见
            agent_aliases = _agent_code_aliases(agent_code)
            datasets = [
                ds for ds in datasets
                if not ds.agent_codes
                or bool(agent_aliases.intersection({str(code).strip().lower() for code in ds.agent_codes}))
            ]
        return list(datasets[offset : offset + limit])

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
        await flush_or_commit(session)
        await session.refresh(ds)
        return ds

    @staticmethod
    async def update_details(
        session: AsyncSession,
        dataset_id: str,
        **updates,
    ) -> Optional[ExperimentDataset]:
        """更新数据集管理字段。"""
        ds = await DatasetCRUD.get_by_id(session, dataset_id)
        if not ds:
            return None

        allowed_fields = {
            "name",
            "description",
            "domain",
            "tags",
            "agent_codes",
            "is_public",
            "status",
        }
        for field, value in updates.items():
            if field in allowed_fields:
                setattr(ds, field, value)

        await flush_or_commit(session)
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
        await flush_or_commit(session)
        await session.refresh(ds)
        return ds

    @staticmethod
    async def delete(session: AsyncSession, dataset_id: str) -> bool:
        ds = await DatasetCRUD.get_by_id(session, dataset_id)
        if not ds:
            return False
        await session.delete(ds)
        await flush_or_commit(session)
        return True
