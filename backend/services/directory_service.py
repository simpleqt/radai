"""知识库目录服务"""

from datetime import date, datetime
import re
from typing import Any, Dict, List, Optional, Tuple

from sqlalchemy import func, or_, select, update
from sqlalchemy.ext.asyncio import AsyncSession

from backend.config.loggers import get_logger
from backend.db.crud.directory_crud import DirectoryCRUD
from backend.db.models import KnowledgeBaseDocument, KnowledgeBaseDirectory

logger = get_logger(__name__)


class DirectoryService:
    """知识库目录服务 - 提供目录管理的业务逻辑"""

    def __init__(self):
        self.crud = DirectoryCRUD()

    async def get_directory_tree(
        self,
        session: AsyncSession,
        kb_id: str,
    ) -> List[Dict[str, Any]]:
        """
        获取目录树结构 (包含基于 document_date 的虚拟日期子目录)
        
        Returns:
            树形结构的目录列表，每个节点包含 path, name, doc_count, children
        """
        # 1. 获取物理目录
        directories = await self.crud.list_all_by_kb(session, kb_id)
        
        path_to_dir: Dict[str, Dict[str, Any]] = {}
        for d in directories:
            path_to_dir[d.category_path] = {
                "id": d.id,
                "path": d.category_path,
                "name": d.name,
                "description": d.description,
                "icon": d.icon,
                "color": d.color,
                "sort_order": d.sort_order,
                "doc_count": d.doc_count,
                "total_doc_count": d.total_doc_count,
                "depth": d.depth,
                "parent_path": d.parent_path,
                "children": [],
                "is_virtual": False
            }
        
        # 2. 获取文档中定义的目录路径，确保即使物理目录不存在也能显示
        doc_paths_stmt = select(
            KnowledgeBaseDocument.category_path,
            func.count(KnowledgeBaseDocument.id).label("doc_count")
        ).where(
            KnowledgeBaseDocument.kb_id == kb_id,
            KnowledgeBaseDocument.category_path.isnot(None),
            KnowledgeBaseDocument.category_path != ""
        ).group_by(KnowledgeBaseDocument.category_path)
        
        doc_paths_results = (await session.execute(doc_paths_stmt)).all()
        
        for cat_path, d_count in doc_paths_results:
            if not cat_path:
                continue
                
            # 分解路径，确保每一级都存在
            parts = [p for p in cat_path.split("/") if p]
            for i in range(len(parts)):
                current_path = "/".join(parts[:i+1])
                parent_path = "/".join(parts[:i]) if i > 0 else None
                
                if current_path not in path_to_dir:
                    path_to_dir[current_path] = {
                        "id": f"implied-{current_path}",
                        "path": current_path,
                        "name": parts[i],
                        "description": "自动发现的分类",
                        "icon": "Folder",
                        "color": None,
                        "sort_order": 0,
                        "doc_count": d_count if current_path == cat_path else 0,
                        "total_doc_count": d_count,
                        "depth": i + 1,
                        "parent_path": parent_path,
                        "children": [],
                        "is_virtual": False # 这种属于“隐含物理目录”
                    }
                elif current_path != cat_path:
                    # 如果已存在，更新其 total_doc_count（累加来自此文档路径的分支计数）
                    path_to_dir[current_path]["total_doc_count"] += d_count
                else:
                    # 如果已存在且正是该路径，更新其基础 doc_count
                    # 注意：物理目录的 doc_count 可能已经包含了这个，这里取决于数据库是否同步
                    # 为了稳妥，我们可以合并计数或以此处为准
                    pass

        # 3. 获取文档中的日期信息，构建虚拟日期目录
        date_stmt = select(
            KnowledgeBaseDocument.category_path,
            KnowledgeBaseDocument.document_date,
            func.count(KnowledgeBaseDocument.id).label("doc_count")
        ).where(
            KnowledgeBaseDocument.kb_id == kb_id,
            KnowledgeBaseDocument.document_date.isnot(None)
        ).group_by(
            KnowledgeBaseDocument.category_path,
            KnowledgeBaseDocument.document_date
        )
        
        date_results = (await session.execute(date_stmt)).all()
        
        # 将虚拟日期节点加入树中
        for cat_path, doc_date, d_count in date_results:
            if not doc_date:
                continue
                
            date_str = doc_date.isoformat()
            virtual_path = f"{cat_path}/{date_str}" if cat_path else date_str
            
            # 虚拟节点信息
            virtual_node = {
                "id": f"v-{virtual_path}",
                "path": virtual_path,
                "name": date_str,
                "description": f"日期分类: {date_str}",
                "icon": "Clock",
                "color": None,
                "sort_order": 999, # 日期目录排在最后
                "doc_count": d_count,
                "total_doc_count": d_count,
                "depth": (len(cat_path.split("/")) + 1) if cat_path else 1,
                "parent_path": cat_path or None,
                "children": [],
                "is_virtual": True
            }
            
            # 挂载到父目录
            if not cat_path:
                path_to_dir[virtual_path] = virtual_node
            elif cat_path in path_to_dir:
                path_to_dir[cat_path]["children"].append(virtual_node)
                # 虚拟目录的计数通常已经包含在 cat_path 的 total_doc_count 中（通过物理查询）
            else:
                # 即使父路径是隐含的，由于上面的步骤 2，这里 cat_path 理论上一定在 path_to_dir 中
                path_to_dir[virtual_path] = virtual_node

        # 3. 组装成树
        root_nodes: List[Dict[str, Any]] = []
        
        # 注意：这里我们需要重新处理 parent/child 关系，因为加入了虚拟节点
        for node in path_to_dir.values():
            if node.get("is_virtual"):
                # 虚拟节点如果父路径为 NULL 或 parent_path 不在物理目录中，则作为根节点
                if node["parent_path"] is None:
                    # 检查是否已经作为根加入过（如果是虚拟根日期）
                    if node not in root_nodes:
                        root_nodes.append(node)
                continue # 虚拟节点的 parent/child 已经在上面绑定过了 (如果是挂载在物理节点下)
            
            parent_path = node["parent_path"]
            if parent_path is None:
                root_nodes.append(node)
            elif parent_path in path_to_dir:
                # 只有物理节点到物理节点的连接在这里处理
                # 确保不重复添加
                if node not in path_to_dir[parent_path]["children"]:
                    path_to_dir[parent_path]["children"].append(node)
        
        def sort_children(nodes: List[Dict[str, Any]]) -> List[Dict[str, Any]]:
            # 日期目录通常按时间倒序排列比较好，或者跟普通目录一样按名称
            nodes.sort(key=lambda x: (x.get("sort_order", 0), x["name"]))
            for node in nodes:
                if node["children"]:
                    node["children"] = sort_children(node["children"])
            return nodes
        
        return sort_children(root_nodes)

    async def create_directory(
        self,
        session: AsyncSession,
        kb_id: str,
        category_path: str,
        description: Optional[str] = None,
        sort_order: int = 0,
        icon: Optional[str] = None,
        color: Optional[str] = None,
        created_by: Optional[str] = None,
    ) -> KnowledgeBaseDirectory:
        """
        创建目录，自动创建父目录
        """
        await self.crud.ensure_parent_directories(session, kb_id, category_path, created_by)
        
        directory = await self.crud.upsert(
            session=session,
            kb_id=kb_id,
            category_path=category_path,
            description=description,
            sort_order=sort_order,
            icon=icon,
            color=color,
            created_by=created_by,
        )
        
        logger.info(f"创建目录: kb_id={kb_id}, path={category_path}")
        return directory

    async def update_directory(
        self,
        session: AsyncSession,
        kb_id: str,
        category_path: str,
        description: Optional[str] = None,
        sort_order: Optional[int] = None,
        icon: Optional[str] = None,
        color: Optional[str] = None,
    ) -> Optional[KnowledgeBaseDirectory]:
        """更新目录属性"""
        update_data = {}
        if description is not None:
            update_data["description"] = description
        if sort_order is not None:
            update_data["sort_order"] = sort_order
        if icon is not None:
            update_data["icon"] = icon
        if color is not None:
            update_data["color"] = color
        
        if not update_data:
            return await self.crud.get_by_path(session, kb_id, category_path)
        
        directory = await self.crud.update(session, kb_id, category_path, **update_data)
        logger.info(f"更新目录: kb_id={kb_id}, path={category_path}")
        return directory

    async def reorder_directories(
        self,
        session: AsyncSession,
        kb_id: str,
        parent_path: Optional[str],
        paths: List[str],
    ) -> int:
        """按给定顺序批量更新同级目录 sort_order。"""
        normalized_parent = "/".join(part for part in (parent_path or "").split("/") if part).strip()
        normalized_paths = [
            self._normalize_path(raw_path)
            for raw_path in paths
            if self._normalize_path(raw_path)
        ]
        result = await session.execute(
            select(KnowledgeBaseDirectory).where(
                KnowledgeBaseDirectory.kb_id == kb_id,
                KnowledgeBaseDirectory.category_path.in_(normalized_paths),
            )
        )
        directories = {
            directory.category_path: directory
            for directory in result.scalars().all()
        }
        updated = 0
        for idx, category_path in enumerate(normalized_paths):
            if not category_path:
                continue
            directory = directories.get(category_path)
            if not directory or directory.is_system:
                continue
            dir_parent = directory.parent_path or ""
            if dir_parent != normalized_parent:
                raise ValueError(f"目录 {category_path} 不属于父目录 {normalized_parent or '根目录'}")
            if directory.sort_order != idx:
                directory.sort_order = idx
                directory.updated_at = datetime.now()
                updated += 1
        if updated:
            await session.commit()
        return updated

    @staticmethod
    def _normalize_path(path: Optional[str]) -> str:
        """Normalize a category path before using it in database queries."""
        return "/".join(part for part in (path or "").split("/") if part).strip()

    async def _path_has_documents(
        self,
        session: AsyncSession,
        kb_id: str,
        category_path: str,
    ) -> bool:
        result = await session.execute(
            select(func.count()).where(
                KnowledgeBaseDocument.kb_id == kb_id,
                (KnowledgeBaseDocument.category_path == category_path)
                | (KnowledgeBaseDocument.category_path.like(f"{category_path}/%")),
            )
        )
        return bool(result.scalar() or 0)

    @staticmethod
    def _virtual_directory_context(path: Optional[str]) -> Optional[Tuple[Optional[str], date]]:
        """Parse a generated date-directory path into its real category and date."""
        normalized = DirectoryService._normalize_path(path)
        if not normalized:
            return None

        parts = normalized.rsplit("/", 1)
        date_text = parts[-1]
        if not re.fullmatch(r"\d{4}-\d{2}-\d{2}", date_text):
            return None

        try:
            directory_date = datetime.strptime(date_text, "%Y-%m-%d").date()
        except ValueError:
            return None

        parent_path = parts[0] if len(parts) == 2 else None
        return parent_path or None, directory_date

    @staticmethod
    def _virtual_category_condition(category_path: Optional[str]):
        if category_path:
            return KnowledgeBaseDocument.category_path == category_path
        return or_(
            KnowledgeBaseDocument.category_path.is_(None),
            KnowledgeBaseDocument.category_path == "",
        )

    async def _virtual_document_count(
        self,
        session: AsyncSession,
        kb_id: str,
        category_path: Optional[str],
        directory_date: date,
    ) -> int:
        result = await session.execute(
            select(func.count()).where(
                KnowledgeBaseDocument.kb_id == kb_id,
                KnowledgeBaseDocument.document_date == directory_date,
                self._virtual_category_condition(category_path),
            )
        )
        return int(result.scalar() or 0)

    async def _rename_virtual_directory(
        self,
        session: AsyncSession,
        kb_id: str,
        old_path: str,
        new_name: str,
    ) -> Tuple[bool, int, str]:
        context = self._virtual_directory_context(old_path)
        if not context:
            return False, 0, old_path

        category_path, old_date = context
        try:
            new_date = datetime.strptime(new_name.strip(), "%Y-%m-%d").date()
        except ValueError:
            return False, 0, old_path

        affected_documents = await self._virtual_document_count(
            session, kb_id, category_path, old_date
        )
        if not affected_documents:
            return False, 0, old_path

        new_path = f"{category_path}/{new_date.isoformat()}" if category_path else new_date.isoformat()
        if old_date == new_date:
            return True, affected_documents, old_path

        if await self.crud.get_by_path(session, kb_id, new_path):
            return False, 0, new_path
        if await self._virtual_document_count(session, kb_id, category_path, new_date):
            return False, 0, new_path

        await session.execute(
            update(KnowledgeBaseDocument)
            .where(
                KnowledgeBaseDocument.kb_id == kb_id,
                KnowledgeBaseDocument.document_date == old_date,
                self._virtual_category_condition(category_path),
            )
            .values(document_date=new_date)
        )
        await session.commit()
        logger.info(
            f"重命名虚拟日期目录: {old_path} -> {new_path}, "
            f"影响 {affected_documents} 个文档"
        )
        return True, affected_documents, new_path

    async def _move_virtual_directory(
        self,
        session: AsyncSession,
        kb_id: str,
        source_path: str,
        target_parent_path: Optional[str],
    ) -> Tuple[bool, int, str]:
        context = self._virtual_directory_context(source_path)
        if not context:
            return False, 0, source_path

        source_category_path, directory_date = context
        target_parent_path = self._normalize_path(target_parent_path) or None
        new_path = (
            f"{target_parent_path}/{directory_date.isoformat()}"
            if target_parent_path
            else directory_date.isoformat()
        )

        affected_documents = await self._virtual_document_count(
            session, kb_id, source_category_path, directory_date
        )
        if not affected_documents:
            return False, 0, source_path
        if target_parent_path == source_category_path:
            return True, affected_documents, source_path

        if target_parent_path:
            target_directory = await self.crud.get_by_path(session, kb_id, target_parent_path)
            if target_directory and target_directory.is_system:
                return False, 0, new_path
            if not target_directory and not await self._path_has_documents(
                session, kb_id, target_parent_path
            ):
                return False, 0, new_path

        if await self.crud.get_by_path(session, kb_id, new_path):
            return False, 0, new_path

        # A virtual date directory is derived from document_date/category_path,
        # so an existing target date group is not a path conflict.  Moving the
        # source documents into it naturally merges the two virtual groups.

        await session.execute(
            update(KnowledgeBaseDocument)
            .where(
                KnowledgeBaseDocument.kb_id == kb_id,
                KnowledgeBaseDocument.document_date == directory_date,
                self._virtual_category_condition(source_category_path),
            )
            .values(category_path=target_parent_path)
        )
        await session.commit()
        logger.info(
            f"移动虚拟日期目录: {source_path} -> {new_path}, "
            f"影响 {affected_documents} 个文档"
        )
        return True, affected_documents, new_path

    async def rename_directory(
        self,
        session: AsyncSession,
        kb_id: str,
        old_path: str,
        new_name: str,
    ) -> Tuple[bool, int, str]:
        """
        重命名目录
        
        Returns:
            (success, affected_count, new_path)
        """
        old_path = self._normalize_path(old_path)
        if not old_path:
            return False, 0, ""
        
        new_name = new_name.strip().replace("/", "")
        if not new_name:
            return False, 0, ""
        
        source_directory = await self.crud.get_by_path(session, kb_id, old_path)
        if source_directory and source_directory.is_system:
            return False, 0, old_path
        if not source_directory:
            if self._virtual_directory_context(old_path):
                return await self._rename_virtual_directory(
                    session, kb_id, old_path, new_name
                )
            if not await self._path_has_documents(session, kb_id, old_path):
                return False, 0, old_path

        parts = old_path.split("/")
        parts[-1] = new_name
        new_path = "/".join(parts)
        
        if old_path == new_path:
            return True, 0, old_path
        
        existing = await self.crud.get_by_path(session, kb_id, new_path)
        if existing:
            return False, 0, new_path
        
        descendants = await self.crud.get_descendants(session, kb_id, old_path)
        affected_count = len(descendants)
        
        changed_at = datetime.now()
        for directory in descendants:
            if directory.category_path == old_path:
                updated_path = new_path
            else:
                updated_path = directory.category_path.replace(f"{old_path}/", f"{new_path}/", 1)
            
            parent_path, depth, name = self.crud._parse_path_info(updated_path)
            directory.category_path = updated_path
            directory.parent_path = parent_path
            directory.depth = depth
            directory.name = name
            directory.updated_at = changed_at
        
        await session.execute(
            update(KnowledgeBaseDocument)
            .where(KnowledgeBaseDocument.kb_id == kb_id)
            .where(
                (KnowledgeBaseDocument.category_path == old_path) |
                (KnowledgeBaseDocument.category_path.like(f"{old_path}/%"))
            )
            .values(category_path=func.replace(
                KnowledgeBaseDocument.category_path,
                old_path,
                new_path
            ))
        )
        
        await session.commit()
        
        logger.info(f"重命名目录: {old_path} -> {new_path}, 影响 {affected_count} 个目录")
        return True, affected_count, new_path

    async def move_directory(
        self,
        session: AsyncSession,
        kb_id: str,
        source_path: str,
        target_parent_path: Optional[str],
    ) -> Tuple[bool, int, str]:
        """
        移动目录到新的父目录下
        
        Args:
            source_path: 源目录路径
            target_parent_path: 目标父目录路径，None 表示移动到根目录
            
        Returns:
            (success, affected_count, new_path)
        """
        source_path = self._normalize_path(source_path)
        target_parent_path = self._normalize_path(target_parent_path) or None
        if not source_path:
            return False, 0, ""

        source_directory = await self.crud.get_by_path(session, kb_id, source_path)
        if source_directory and source_directory.is_system:
            return False, 0, source_path
        if not source_directory:
            if self._virtual_directory_context(source_path):
                return await self._move_virtual_directory(
                    session, kb_id, source_path, target_parent_path
                )
            if not await self._path_has_documents(session, kb_id, source_path):
                return False, 0, source_path
        
        source_name = source_path.split("/")[-1]
        new_path = f"{target_parent_path}/{source_name}" if target_parent_path else source_name
        
        if source_path == new_path:
            return True, 0, source_path
        
        if new_path.startswith(f"{source_path}/"):
            return False, 0, new_path
        
        existing = await self.crud.get_by_path(session, kb_id, new_path)
        if existing:
            return False, 0, new_path
        
        if target_parent_path:
            target_directory = await self.crud.get_by_path(session, kb_id, target_parent_path)
            if not target_directory and not await self._path_has_documents(session, kb_id, target_parent_path):
                return False, 0, new_path
        
        descendants = await self.crud.get_descendants(session, kb_id, source_path)
        affected_count = len(descendants)
        
        changed_at = datetime.now()
        for directory in descendants:
            if directory.category_path == source_path:
                updated_path = new_path
            else:
                updated_path = directory.category_path.replace(f"{source_path}/", f"{new_path}/", 1)
            
            parent_path, depth, name = self.crud._parse_path_info(updated_path)
            directory.category_path = updated_path
            directory.parent_path = parent_path
            directory.depth = depth
            directory.name = name
            directory.updated_at = changed_at
        
        await session.execute(
            update(KnowledgeBaseDocument)
            .where(KnowledgeBaseDocument.kb_id == kb_id)
            .where(
                (KnowledgeBaseDocument.category_path == source_path) |
                (KnowledgeBaseDocument.category_path.like(f"{source_path}/%"))
            )
            .values(category_path=func.replace(
                KnowledgeBaseDocument.category_path,
                source_path,
                new_path
            ))
        )
        
        await session.commit()
        
        logger.info(f"移动目录: {source_path} -> {new_path}, 影响 {affected_count} 个目录")
        return True, affected_count, new_path

    async def delete_directory(
        self,
        session: AsyncSession,
        kb_id: str,
        category_path: str,
        delete_documents: bool = False,
    ) -> Tuple[bool, int, int]:
        """
        删除目录
        
        Args:
            delete_documents: 是否同时删除目录下的文档
            
        Returns:
            (success, deleted_directories, deleted_documents)
        """
        directory = await self.crud.get_by_path(session, kb_id, category_path)
        if not directory:
            return False, 0, 0
        
        if directory.is_system:
            logger.warning(f"尝试删除系统目录: {category_path}")
            return False, 0, 0
        
        children = await self.crud.get_children(session, kb_id, category_path)
        if children:
            logger.warning(f"目录 {category_path} 有子目录，无法删除")
            return False, 0, 0
        
        doc_count = directory.doc_count
        if doc_count > 0 and not delete_documents:
            logger.warning(f"目录 {category_path} 有 {doc_count} 个文档，无法删除")
            return False, 0, 0
        
        deleted_docs = 0
        if delete_documents and doc_count > 0:
            from sqlalchemy import delete
            result = await session.execute(
                delete(KnowledgeBaseDocument).where(
                    KnowledgeBaseDocument.kb_id == kb_id,
                    KnowledgeBaseDocument.category_path == category_path,
                )
            )
            deleted_docs = result.rowcount
        
        deleted_dirs = await self.crud.delete(session, kb_id, category_path)
        await session.commit()
        
        logger.info(f"删除目录: {category_path}, 删除文档: {deleted_docs}")
        return True, 1 if deleted_dirs else 0, deleted_docs

    async def sync_from_documents(
        self,
        session: AsyncSession,
        kb_id: str,
    ) -> int:
        """
        从文档同步目录结构
        
        扫描所有文档的 category_path，创建缺失的目录记录
        
        Returns:
            创建的目录数量
        """
        result = await session.execute(
            select(KnowledgeBaseDocument.category_path, func.count().label("cnt"))
            .where(KnowledgeBaseDocument.kb_id == kb_id)
            .where(KnowledgeBaseDocument.category_path.isnot(None))
            .where(KnowledgeBaseDocument.category_path != "")
            .group_by(KnowledgeBaseDocument.category_path)
        )
        path_counts = {row[0]: row[1] for row in result.all()}
        
        all_paths = set()
        for path in path_counts.keys():
            parts = [p for p in path.split("/") if p]
            for i in range(len(parts)):
                all_paths.add("/".join(parts[: i + 1]))
        
        existing_result = await session.execute(
            select(KnowledgeBaseDirectory.category_path).where(
                KnowledgeBaseDirectory.kb_id == kb_id,
                KnowledgeBaseDirectory.category_path.in_(all_paths),
            )
        )
        existing_paths = set(existing_result.scalars().all())
        missing_paths = sorted(all_paths - existing_paths, key=lambda value: value.count("/"))
        for path in missing_paths:
            parent_path, depth, name = self.crud._parse_path_info(path)
            session.add(
                KnowledgeBaseDirectory(
                    kb_id=kb_id,
                    category_path=path,
                    parent_path=parent_path,
                    depth=depth,
                    name=name,
                )
            )
        created_count = len(missing_paths)
        
        await session.commit()
        logger.info(f"同步目录: kb_id={kb_id}, 创建 {created_count} 个目录")
        return created_count

    async def recalculate_doc_counts(
        self,
        session: AsyncSession,
        kb_id: str,
    ) -> int:
        """
        重新计算所有目录的文档计数
        
        Returns:
            更新的目录数量
        """
        directories = await self.crud.list_all_by_kb(session, kb_id)
        result = await session.execute(
            select(
                KnowledgeBaseDocument.category_path,
                func.count().label("count"),
            )
            .where(KnowledgeBaseDocument.kb_id == kb_id)
            .group_by(KnowledgeBaseDocument.category_path)
        )
        category_counts = {
            str(path or ""): int(count or 0) for path, count in result.all()
        }
        for directory in directories:
            path = directory.category_path
            directory.doc_count = category_counts.get(path, 0)
            prefix = f"{path}/"
            directory.total_doc_count = sum(
                count
                for category_path, count in category_counts.items()
                if category_path == path or category_path.startswith(prefix)
            )

        await session.commit()
        logger.info(f"重新计算文档计数: kb_id={kb_id}, 更新 {len(directories)} 个目录")
        return len(directories)

    async def update_doc_counts_for_path(
        self,
        session: AsyncSession,
        kb_id: str,
        category_path: Optional[str],
    ) -> None:
        """
        更新指定路径及其所有父目录的文档计数
        """
        if not category_path:
            return
        
        parts = [p for p in category_path.split("/") if p]
        paths_to_update = []
        current = ""
        for part in parts:
            current = f"{current}/{part}" if current else part
            paths_to_update.append(current)
        
        directories_result = await session.execute(
            select(KnowledgeBaseDirectory).where(
                KnowledgeBaseDirectory.kb_id == kb_id,
                KnowledgeBaseDirectory.category_path.in_(paths_to_update),
            )
        )
        directories = list(directories_result.scalars().all())
        counts_result = await session.execute(
            select(
                KnowledgeBaseDocument.category_path,
                func.count().label("count"),
            )
            .where(KnowledgeBaseDocument.kb_id == kb_id)
            .where(
                or_(
                    *[
                        or_(
                            KnowledgeBaseDocument.category_path == path,
                            KnowledgeBaseDocument.category_path.like(f"{path}/%"),
                        )
                        for path in paths_to_update
                    ]
                )
            )
            .group_by(KnowledgeBaseDocument.category_path)
        )
        category_counts = {
            str(path or ""): int(count or 0)
            for path, count in counts_result.all()
        }
        for directory in directories:
            path = directory.category_path
            directory.doc_count = category_counts.get(path, 0)
            prefix = f"{path}/"
            directory.total_doc_count = sum(
                count
                for category_path, count in category_counts.items()
                if category_path == path or category_path.startswith(prefix)
            )

        await session.commit()


directory_service = DirectoryService()
