"""知识库目录服务"""

from typing import Any, Dict, List, Optional, Tuple

from sqlalchemy import func, select, update
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
        for path, node in path_to_dir.items():
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
        if not old_path:
            return False, 0, ""
        
        new_name = new_name.strip().replace("/", "")
        if not new_name:
            return False, 0, ""
        
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
        
        for directory in descendants:
            if directory.category_path == old_path:
                updated_path = new_path
            else:
                updated_path = directory.category_path.replace(f"{old_path}/", f"{new_path}/", 1)
            
            parent_path, depth, name = self.crud._parse_path_info(updated_path)
            
            await session.execute(
                update(KnowledgeBaseDirectory)
                .where(KnowledgeBaseDirectory.id == directory.id)
                .values(
                    category_path=updated_path,
                    parent_path=parent_path,
                    depth=depth,
                    name=name,
                )
            )
        
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
        if not source_path:
            return False, 0, ""
        
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
            await self.crud.ensure_parent_directories(session, kb_id, target_parent_path)
        
        descendants = await self.crud.get_descendants(session, kb_id, source_path)
        affected_count = len(descendants)
        
        for directory in descendants:
            if directory.category_path == source_path:
                updated_path = new_path
            else:
                updated_path = directory.category_path.replace(f"{source_path}/", f"{new_path}/", 1)
            
            parent_path, depth, name = self.crud._parse_path_info(updated_path)
            
            await session.execute(
                update(KnowledgeBaseDirectory)
                .where(KnowledgeBaseDirectory.id == directory.id)
                .values(
                    category_path=updated_path,
                    parent_path=parent_path,
                    depth=depth,
                    name=name,
                )
            )
        
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
        
        created_count = 0
        for path in sorted(all_paths, key=lambda x: x.count("/")):
            existing = await self.crud.get_by_path(session, kb_id, path)
            if not existing:
                await self.crud.create(session, kb_id, path)
                created_count += 1
        
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
        
        path_to_dir = {d.category_path: d for d in directories}
        
        for directory in directories:
            result = await session.execute(
                select(func.count()).where(
                    KnowledgeBaseDocument.kb_id == kb_id,
                    KnowledgeBaseDocument.category_path == directory.category_path,
                )
            )
            doc_count = result.scalar() or 0
            
            result = await session.execute(
                select(func.count()).where(
                    KnowledgeBaseDocument.kb_id == kb_id,
                    (KnowledgeBaseDocument.category_path == directory.category_path) |
                    (KnowledgeBaseDocument.category_path.like(f"{directory.category_path}/%")),
                )
            )
            total_doc_count = result.scalar() or 0
            
            await session.execute(
                update(KnowledgeBaseDirectory)
                .where(KnowledgeBaseDirectory.id == directory.id)
                .values(doc_count=doc_count, total_doc_count=total_doc_count)
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
        
        for path in paths_to_update:
            directory = await self.crud.get_by_path(session, kb_id, path)
            if not directory:
                continue
            
            result = await session.execute(
                select(func.count()).where(
                    KnowledgeBaseDocument.kb_id == kb_id,
                    KnowledgeBaseDocument.category_path == path,
                )
            )
            doc_count = result.scalar() or 0
            
            result = await session.execute(
                select(func.count()).where(
                    KnowledgeBaseDocument.kb_id == kb_id,
                    (KnowledgeBaseDocument.category_path == path) |
                    (KnowledgeBaseDocument.category_path.like(f"{path}/%")),
                )
            )
            total_doc_count = result.scalar() or 0
            
            await session.execute(
                update(KnowledgeBaseDirectory)
                .where(KnowledgeBaseDirectory.id == directory.id)
                .values(doc_count=doc_count, total_doc_count=total_doc_count)
            )
        
        await session.commit()


directory_service = DirectoryService()
