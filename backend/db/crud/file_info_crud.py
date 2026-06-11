"""FileInfo 表的 CRUD 操作"""

from typing import List, Optional

from sqlalchemy import select, update
from sqlalchemy.orm import Session

from backend.db.models import FileInfo


class FileInfoCRUD:
    """FileInfo 表的 CRUD 操作类"""

    def __init__(self, session: Session):
        self.session = session

    def get_pending_files(self, limit: int = 100) -> List[FileInfo]:
        """
        获取待入库的文件列表
        查询条件: parse_status = 1 且 storage_status = 0

        Args:
            limit: 返回的最大记录数

        Returns:
            符合条件的 FileInfo 列表
        """
        stmt = (
            select(FileInfo)
            .where(FileInfo.parse_status == 1)
            .where(FileInfo.storage_status == 0)
            .limit(limit)
        )
        result = self.session.execute(stmt)
        return result.scalars().all()

    def claim_pending_files(self, limit: int = 100) -> List[FileInfo]:
        """
        原子获取并认领待入库的文件列表
        将 storage_status = 0 更新为 storage_status = 2 (处理中) 
        以防止被其他 Worker 重复获取
        """
        stmt = (
            select(FileInfo)
            .where(FileInfo.parse_status == 1)
            .where(FileInfo.storage_status == 0)
            .limit(limit)
            .with_for_update(skip_locked=True)
        )
        files = self.session.execute(stmt).scalars().all()
        
        # 标记为处理中
        for f in files:
            f.storage_status = 2
            
        self.session.commit()
        return files

    def get_by_task_id(self, task_id: str) -> Optional[FileInfo]:
        """
        根据 task_id 获取文件信息

        Args:
            task_id: 任务ID

        Returns:
            FileInfo 对象或 None
        """
        stmt = select(FileInfo).where(FileInfo.task_id == task_id)
        result = self.session.execute(stmt)
        return result.scalar_one_or_none()

    def update_storage_status(
        self, task_id: str, storage_status: int
    ) -> bool:
        """
        更新文件的 storage_status

        Args:
            task_id: 任务ID
            storage_status: 新的存储状态 (0: 未入库, 1: 已入库)

        Returns:
            是否更新成功
        """
        stmt = (
            update(FileInfo)
            .where(FileInfo.task_id == task_id)
            .values(storage_status=storage_status)
        )
        result = self.session.execute(stmt)
        self.session.commit()
        return result.rowcount > 0

    def mark_as_stored(self, task_id: str) -> bool:
        """
        将文件标记为已入库 (storage_status = 1)

        Args:
            task_id: 任务ID

        Returns:
            是否更新成功
        """
        return self.update_storage_status(task_id, 1)

    def revert_storage_status(self, task_id: str) -> bool:
        """
        处理失败时，将 documents 恢复为未处理状态 (0)
        （如果是彻底失败，也可考虑新增如 -1 状态，目前回滚为 0）
        """
        return self.update_storage_status(task_id, 0)

    def create(self, task_id: str, file_name: str, parse_status: int = 0,
               storage_status: int = 0, md_file_path: str = "") -> FileInfo:
        """
        创建新的文件信息记录

        Args:
            task_id: 任务ID
            file_name: 文件名
            parse_status: 解析状态 (默认0)
            storage_status: 存储状态 (默认0)
            md_file_path: Markdown文件路径

        Returns:
            创建的 FileInfo 对象
        """
        file_info = FileInfo(
            task_id=task_id,
            file_name=file_name,
            parse_status=parse_status,
            storage_status=storage_status,
            md_file_path=md_file_path,
        )
        self.session.add(file_info)
        self.session.commit()
        self.session.refresh(file_info)
        return file_info

    def update_parse_status(
        self, task_id: str, parse_status: int
    ) -> bool:
        """
        更新文件的 parse_status

        Args:
            task_id: 任务ID
            parse_status: 新的解析状态

        Returns:
            是否更新成功
        """
        stmt = (
            update(FileInfo)
            .where(FileInfo.task_id == task_id)
            .values(parse_status=parse_status)
        )
        result = self.session.execute(stmt)
        self.session.commit()
        return result.rowcount > 0

    def update_md_file_path(
        self, task_id: str, md_file_path: str
    ) -> bool:
        """
        更新文件的 md_file_path

        Args:
            task_id: 任务ID
            md_file_path: Markdown文件路径

        Returns:
            是否更新成功
        """
        stmt = (
            update(FileInfo)
            .where(FileInfo.task_id == task_id)
            .values(md_file_path=md_file_path)
        )
        result = self.session.execute(stmt)
        self.session.commit()
        return result.rowcount > 0

    def get_all(
        self, 
        skip: int = 0, 
        limit: int = 100,
        parse_status: Optional[int] = None,
        storage_status: Optional[int] = None,
        file_name_like: Optional[str] = None,
    ) -> tuple[List[FileInfo], int]:
        """
        获取文件信息（支持分页和过滤）

        Args:
            skip: 跳过的记录数
            limit: 返回的最大记录数
            parse_status: 按解析状态过滤
            storage_status: 按存储状态过滤
            file_name_like: 按文件名模糊匹配

        Returns:
            (FileInfo列表, 总数)
        """
        from sqlalchemy import func
        
        conditions = []
        if parse_status is not None:
            conditions.append(FileInfo.parse_status == parse_status)
        if storage_status is not None:
            conditions.append(FileInfo.storage_status == storage_status)
        if file_name_like:
            conditions.append(FileInfo.file_name.like(f"%{file_name_like}%"))
        
        count_stmt = select(func.count(FileInfo.task_id))
        if conditions:
            count_stmt = count_stmt.where(*conditions)
        total = self.session.execute(count_stmt).scalar() or 0
        
        stmt = select(FileInfo)
        if conditions:
            stmt = stmt.where(*conditions)
        stmt = stmt.offset(skip).limit(limit).order_by(FileInfo.task_id.desc())
        
        result = self.session.execute(stmt)
        return result.scalars().all(), total

    def get_all_files(
        self, 
        skip: int = 0, 
        limit: int = 100,
        parse_status: Optional[int] = None,
        storage_status: Optional[int] = None,
        file_name_like: Optional[str] = None,
    ) -> tuple[List[FileInfo], int]:
        """
        获取所有文件信息（支持分页和过滤）

        Args:
            skip: 跳过的记录数
            limit: 返回的最大记录数
            parse_status: 按解析状态过滤
            storage_status: 按存储状态过滤
            file_name_like: 按文件名模糊匹配

        Returns:
            (FileInfo列表, 总数)
        """
        return self.get_all(
            skip=skip, 
            limit=limit,
            parse_status=parse_status,
            storage_status=storage_status,
            file_name_like=file_name_like,
        )

    def delete(self, task_id: str) -> bool:
        """
        删除文件信息记录

        Args:
            task_id: 任务ID

        Returns:
            是否删除成功
        """
        file_info = self.get_by_task_id(task_id)
        if file_info:
            self.session.delete(file_info)
            self.session.commit()
            return True
        return False


# 全局实例（用于依赖注入或直接导入使用）
def get_file_info_crud(session: Session) -> FileInfoCRUD:
    """获取 FileInfoCRUD 实例"""
    return FileInfoCRUD(session)
