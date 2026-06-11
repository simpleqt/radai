"""知识库目录 CRUD 操作"""

from datetime import datetime
from typing import List, Optional, Tuple

from sqlalchemy import delete, func, select, update
from sqlalchemy.ext.asyncio import AsyncSession

from backend.db.models import KnowledgeBaseDirectory


class DirectoryCRUD:
    """知识库目录 CRUD 操作类"""

    @staticmethod
    def _parse_path_info(category_path: str) -> Tuple[Optional[str], int, str]:
        """
        解析目录路径信息
        
        Args:
            category_path: 目录完整路径，如 '期刊/2024'
            
        Returns:
            (parent_path, depth, name)
        """
        if not category_path:
            return None, 1, ""
        
        parts = [p for p in category_path.split("/") if p]
        if not parts:
            return None, 1, ""
        
        name = parts[-1]
        depth = len(parts)
        parent_path = "/".join(parts[:-1]) if len(parts) > 1 else None
        
        return parent_path, depth, name

    @staticmethod
    async def create(
        session: AsyncSession,
        kb_id: str,
        category_path: str,
        description: Optional[str] = None,
        sort_order: int = 0,
        icon: Optional[str] = None,
        color: Optional[str] = None,
        created_by: Optional[str] = None,
        is_system: bool = False,
    ) -> KnowledgeBaseDirectory:
        """创建目录"""
        parent_path, depth, name = DirectoryCRUD._parse_path_info(category_path)
        
        directory = KnowledgeBaseDirectory(
            kb_id=kb_id,
            category_path=category_path,
            parent_path=parent_path,
            depth=depth,
            name=name,
            description=description,
            sort_order=sort_order,
            icon=icon,
            color=color,
            created_by=created_by,
            is_system=is_system,
        )
        session.add(directory)
        await session.commit()
        await session.refresh(directory)
        return directory

    @staticmethod
    async def get_by_path(
        session: AsyncSession, kb_id: str, category_path: str
    ) -> Optional[KnowledgeBaseDirectory]:
        """根据路径获取目录"""
        result = await session.execute(
            select(KnowledgeBaseDirectory).where(
                KnowledgeBaseDirectory.kb_id == kb_id,
                KnowledgeBaseDirectory.category_path == category_path,
            )
        )
        return result.scalar_one_or_none()

    @staticmethod
    async def get_by_id(
        session: AsyncSession, directory_id: int
    ) -> Optional[KnowledgeBaseDirectory]:
        """根据 ID 获取目录"""
        result = await session.execute(
            select(KnowledgeBaseDirectory).where(KnowledgeBaseDirectory.id == directory_id)
        )
        return result.scalar_one_or_none()

    @staticmethod
    async def list_by_kb(
        session: AsyncSession,
        kb_id: str,
        parent_path: Optional[str] = None,
        skip: int = 0,
        limit: int = 100,
    ) -> Tuple[List[KnowledgeBaseDirectory], int]:
        """获取知识库的目录列表"""
        conditions = [KnowledgeBaseDirectory.kb_id == kb_id]
        
        if parent_path is not None:
            conditions.append(KnowledgeBaseDirectory.parent_path == parent_path)
        
        count_stmt = select(func.count(KnowledgeBaseDirectory.id)).where(*conditions)
        total = await session.execute(count_stmt)
        total = total.scalar() or 0
        
        stmt = (
            select(KnowledgeBaseDirectory)
            .where(*conditions)
            .order_by(KnowledgeBaseDirectory.sort_order, KnowledgeBaseDirectory.name)
            .offset(skip)
            .limit(limit)
        )
        result = await session.execute(stmt)
        return list(result.scalars().all()), total

    @staticmethod
    async def list_all_by_kb(
        session: AsyncSession, kb_id: str
    ) -> List[KnowledgeBaseDirectory]:
        """获取知识库的所有目录"""
        result = await session.execute(
            select(KnowledgeBaseDirectory)
            .where(KnowledgeBaseDirectory.kb_id == kb_id)
            .order_by(KnowledgeBaseDirectory.depth, KnowledgeBaseDirectory.sort_order, KnowledgeBaseDirectory.name)
        )
        return list(result.scalars().all())

    @staticmethod
    async def get_children(
        session: AsyncSession, kb_id: str, parent_path: Optional[str]
    ) -> List[KnowledgeBaseDirectory]:
        """获取子目录列表"""
        result = await session.execute(
            select(KnowledgeBaseDirectory)
            .where(
                KnowledgeBaseDirectory.kb_id == kb_id,
                KnowledgeBaseDirectory.parent_path == parent_path,
            )
            .order_by(KnowledgeBaseDirectory.sort_order, KnowledgeBaseDirectory.name)
        )
        return list(result.scalars().all())

    @staticmethod
    async def get_descendants(
        session: AsyncSession, kb_id: str, category_path: str
    ) -> List[KnowledgeBaseDirectory]:
        """获取所有子孙目录（包括自身）"""
        result = await session.execute(
            select(KnowledgeBaseDirectory)
            .where(
                KnowledgeBaseDirectory.kb_id == kb_id,
                (KnowledgeBaseDirectory.category_path == category_path) |
                (KnowledgeBaseDirectory.category_path.like(f"{category_path}/%")),
            )
            .order_by(KnowledgeBaseDirectory.depth, KnowledgeBaseDirectory.category_path)
        )
        return list(result.scalars().all())

    @staticmethod
    async def update(
        session: AsyncSession,
        kb_id: str,
        category_path: str,
        **kwargs
    ) -> Optional[KnowledgeBaseDirectory]:
        """更新目录信息"""
        allowed_fields = ["description", "sort_order", "icon", "color"]
        update_data = {"updated_at": datetime.now()}
        
        for field in allowed_fields:
            if field in kwargs and kwargs[field] is not None:
                update_data[field] = kwargs[field]
        
        if len(update_data) == 1:
            return await DirectoryCRUD.get_by_path(session, kb_id, category_path)
        
        await session.execute(
            update(KnowledgeBaseDirectory)
            .where(
                KnowledgeBaseDirectory.kb_id == kb_id,
                KnowledgeBaseDirectory.category_path == category_path,
            )
            .values(**update_data)
        )
        await session.commit()
        return await DirectoryCRUD.get_by_path(session, kb_id, category_path)

    @staticmethod
    async def update_doc_count(
        session: AsyncSession,
        kb_id: str,
        category_path: str,
        doc_count: int,
        total_doc_count: int,
    ) -> Optional[KnowledgeBaseDirectory]:
        """更新目录文档计数"""
        await session.execute(
            update(KnowledgeBaseDirectory)
            .where(
                KnowledgeBaseDirectory.kb_id == kb_id,
                KnowledgeBaseDirectory.category_path == category_path,
            )
            .values(
                doc_count=doc_count,
                total_doc_count=total_doc_count,
                updated_at=datetime.now(),
            )
        )
        await session.commit()
        return await DirectoryCRUD.get_by_path(session, kb_id, category_path)

    @staticmethod
    async def update_path(
        session: AsyncSession,
        kb_id: str,
        old_path: str,
        new_path: str,
    ) -> bool:
        """更新目录路径（用于重命名或移动）"""
        parent_path, depth, name = DirectoryCRUD._parse_path_info(new_path)
        
        result = await session.execute(
            update(KnowledgeBaseDirectory)
            .where(
                KnowledgeBaseDirectory.kb_id == kb_id,
                KnowledgeBaseDirectory.category_path == old_path,
            )
            .values(
                category_path=new_path,
                parent_path=parent_path,
                depth=depth,
                name=name,
                updated_at=datetime.now(),
            )
        )
        await session.commit()
        return result.rowcount > 0

    @staticmethod
    async def batch_update_paths(
        session: AsyncSession,
        kb_id: str,
        old_prefix: str,
        new_prefix: str,
    ) -> int:
        """批量更新路径前缀（用于移动目录及其子目录）"""
        directories = await DirectoryCRUD.get_descendants(session, kb_id, old_prefix)
        updated_count = 0
        
        for directory in directories:
            if directory.category_path == old_prefix:
                new_path = new_prefix
            else:
                new_path = directory.category_path.replace(f"{old_prefix}/", f"{new_prefix}/", 1)
            
            parent_path, depth, name = DirectoryCRUD._parse_path_info(new_path)
            
            await session.execute(
                update(KnowledgeBaseDirectory)
                .where(KnowledgeBaseDirectory.id == directory.id)
                .values(
                    category_path=new_path,
                    parent_path=parent_path,
                    depth=depth,
                    name=name,
                    updated_at=datetime.now(),
                )
            )
            updated_count += 1
        
        await session.commit()
        return updated_count

    @staticmethod
    async def delete(
        session: AsyncSession, kb_id: str, category_path: str
    ) -> bool:
        """删除目录（仅删除单个目录，不删除子目录）"""
        result = await session.execute(
            delete(KnowledgeBaseDirectory).where(
                KnowledgeBaseDirectory.kb_id == kb_id,
                KnowledgeBaseDirectory.category_path == category_path,
            )
        )
        await session.commit()
        return result.rowcount > 0

    @staticmethod
    async def delete_with_descendants(
        session: AsyncSession, kb_id: str, category_path: str
    ) -> int:
        """删除目录及其所有子孙目录"""
        result = await session.execute(
            delete(KnowledgeBaseDirectory).where(
                KnowledgeBaseDirectory.kb_id == kb_id,
                (KnowledgeBaseDirectory.category_path == category_path) |
                (KnowledgeBaseDirectory.category_path.like(f"{category_path}/%")),
            )
        )
        await session.commit()
        return result.rowcount

    @staticmethod
    async def upsert(
        session: AsyncSession,
        kb_id: str,
        category_path: str,
        description: Optional[str] = None,
        sort_order: int = 0,
        icon: Optional[str] = None,
        color: Optional[str] = None,
        created_by: Optional[str] = None,
    ) -> KnowledgeBaseDirectory:
        """创建或更新目录"""
        existing = await DirectoryCRUD.get_by_path(session, kb_id, category_path)
        
        if existing:
            update_data = {"updated_at": datetime.now()}
            if description is not None:
                update_data["description"] = description
            if sort_order != 0:
                update_data["sort_order"] = sort_order
            
            if len(update_data) > 1:
                await session.execute(
                    update(KnowledgeBaseDirectory)
                    .where(KnowledgeBaseDirectory.id == existing.id)
                    .values(**update_data)
                )
                await session.commit()
                await session.refresh(existing)
            return existing
        else:
            return await DirectoryCRUD.create(
                session=session,
                kb_id=kb_id,
                category_path=category_path,
                description=description,
                sort_order=sort_order,
                icon=icon,
                color=color,
                created_by=created_by,
            )

    @staticmethod
    async def ensure_parent_directories(
        session: AsyncSession,
        kb_id: str,
        category_path: str,
        created_by: Optional[str] = None,
    ) -> List[KnowledgeBaseDirectory]:
        """确保父目录存在，不存在则创建"""
        if not category_path:
            return []
        
        parts = [p for p in category_path.split("/") if p]
        if not parts:
            return []
        
        created = []
        current_path = ""
        
        for i, part in enumerate(parts[:-1]):
            current_path = f"{current_path}/{part}" if current_path else part
            
            existing = await DirectoryCRUD.get_by_path(session, kb_id, current_path)
            if not existing:
                directory = await DirectoryCRUD.create(
                    session=session,
                    kb_id=kb_id,
                    category_path=current_path,
                    created_by=created_by,
                )
                created.append(directory)
        
        return created


directory_crud = DirectoryCRUD()
