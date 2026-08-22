"""资料管理与版本（M-01）。

上传链路：前端先调 /api/v1/files/upload 拿 file_id，再调 /course-eval/semesters/{id}/materials
建立业务关联。本服务不直接收文件。
同类型重传自动升版本：旧版本 is_current=False。
"""

from __future__ import annotations

from datetime import datetime

from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from backend.application.course_eval.task_service import assert_can_edit_data, get_task
from backend.config.loggers import get_logger
from backend.db.models.course_eval import CeMaterial

logger = get_logger(__name__)


async def register_material(
    session: AsyncSession,
    *,
    task_id: str,
    material_type: str,
    file_id: str,
    uploader_id: str,
    original_filename: str | None = None,
    mime: str | None = None,
    size_bytes: int | None = None,
) -> CeMaterial:
    """关联 user_files.file_id 落库；同类型重传自动升版本。"""
    # 任务行锁串行化"max(version)+1 + 旧版置非当前 + 插新版":无锁时并行
    # attach_material(LLM 对多文件 fan-out 是常态)会双写同一版本号、留下两条
    # is_current=True,版本链静默损坏。锁定读带 populate_existing,状态校验
    # 不会拿身份映射里的旧对象。
    task = await get_task(session, task_id, for_update=True)
    assert_can_edit_data(task)

    # 版本号基于该类型全部历史记录（含软删）的最大值，replace/重传都正确递增
    all_versions = (
        (
            await session.execute(
                select(CeMaterial).where(
                    CeMaterial.semester_task_id == task_id,
                    CeMaterial.material_type == material_type,
                )
            )
        )
        .scalars()
        .all()
    )
    next_version = (
        max(int(m.version_no or 1) for m in all_versions) + 1 if all_versions else 1
    )
    # 未删除的当前记录置为 is_current=False
    active = [m for m in all_versions if m.is_current and m.deleted_at is None]
    for m in active:
        m.is_current = False
        m.updated_at = datetime.now()

    material = CeMaterial(
        semester_task_id=task_id,
        material_type=material_type,
        file_id=file_id,
        original_filename=original_filename,
        mime=mime,
        size_bytes=size_bytes,
        version_no=next_version,
        is_current=True,
        uploaded_by=uploader_id,
    )
    session.add(material)
    await session.flush()
    logger.info(
        "登记资料: task_id=%s type=%s file_id=%s version=%d uploader=%s",
        task_id,
        material_type,
        file_id,
        next_version,
        uploader_id,
    )
    return material


