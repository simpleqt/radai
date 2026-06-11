"""
文档处理器 - 整合数据库查询和MD入库

功能：
1. 从数据库查询待入库文档（parse_status=1 AND storage_status=0）
2. 使用MDIngestor进行入库
3. 更新数据库状态（storage_status=1）
"""

import asyncio
import sys
from pathlib import Path
from typing import Any, Dict, List, Optional

# 添加项目路径
sys.path.insert(0, str(Path(__file__).parent.parent))

from backend.config.loggers import get_logger
from backend.db.connection import get_session
from backend.db.crud.file_info_crud import FileInfoCRUD
from backend.db.models import FileInfo
from backend.rag.raganything.md_ingestor import MDIngestor
from backend.rag.raganything.raganything import RAGAnything

logger = get_logger(__name__)


class DocumentProcessor:
    """文档处理器 - 整合数据库查询和MD入库"""

    def __init__(self, rag_config: Dict = None):
        """
        初始化文档处理器

        Args:
            rag_config: RAG配置
        """
        self.db_session = get_session()
        self.file_info_crud = FileInfoCRUD(self.db_session)
        self.rag_config = rag_config or {}
        self._rag_anything: Optional[RAGAnything] = None
        self._md_ingestor: Optional[MDIngestor] = None

    async def _get_rag_anything(self) -> RAGAnything:
        """获取RAGAnything实例（懒加载）- 复用RagService"""
        if self._rag_anything is None:
            logger.info("初始化RAGAnything...")

            # 复用RagService的初始化逻辑
            from backend.rag.service import get_rag_service

            service = get_rag_service()
            await service.initialize()
            self._rag_anything = service._rag

            logger.info("RAGAnything初始化完成")

        return self._rag_anything

    async def _get_md_ingestor(self) -> MDIngestor:
        """获取MDIngestor实例（懒加载）"""
        if self._md_ingestor is None:
            rag = await self._get_rag_anything()
            self._md_ingestor = MDIngestor(
                rag_anything_instance=rag,
                config=self.rag_config.get('md_ingestor', {})
            )
        return self._md_ingestor

    async def process_pending_documents(
        self,
        limit: int = 10,
        dry_run: bool = False
    ) -> List[Dict[str, Any]]:
        """
        处理待入库文档（主入口）

        流程：
        1. 查询数据库 parse_status=1 AND storage_status=0
        2. 逐个处理MD文件
        3. 更新 storage_status=1

        Args:
            limit: 最大处理数量
            dry_run: 仅预览，不实际入库

        Returns:
            处理结果列表
        """
        logger.info(f"开始处理待入库文档，限制数量: {limit}")

        # 1. 原子“认领”待入库文件 (使用线程池防止阻塞，状态修改为2)
        import asyncio
        if dry_run:
            pending_files = await asyncio.to_thread(self.file_info_crud.get_pending_files, limit)
        else:
            pending_files = await asyncio.to_thread(self.file_info_crud.claim_pending_files, limit)
            
        logger.info(f"找到并锁定 {len(pending_files)} 个待入库文件")

        if not pending_files:
            logger.info("没有待入库的文档")
            return []

        # 2. 逐个处理
        results = []
        for file_info in pending_files:
            result = await self._process_single_document(file_info, dry_run=dry_run)
            results.append(result)

        logger.info(f"文档处理完成，成功: {sum(1 for r in results if r.get('success'))}/{len(results)}")
        return results

    async def _process_single_document(
        self,
        file_info: FileInfo,
        dry_run: bool = False
    ) -> Dict[str, Any]:
        """
        处理单个文档

        Args:
            file_info: 文件信息
            dry_run: 仅预览模式

        Returns:
            处理结果
        """
        task_id = file_info.task_id
        file_name = file_info.file_name
        md_url = file_info.md_file_path

        logger.info(f"处理文档: {file_name} (task_id: {task_id})")
        logger.info(f"MD文件URL: {md_url}")

        try:
            if dry_run:
                # 预览模式，仅返回信息不实际处理
                return {
                    'task_id': task_id,
                    'file_name': file_name,
                    'md_url': md_url,
                    'success': True,
                    'dry_run': True,
                    'message': '预览模式，未实际入库'
                }

            # 1. 使用MDIngestor入库
            ingestor = await self._get_md_ingestor()
            result = await ingestor.ingest_from_url(
                md_url=md_url,
                doc_id=task_id,
                file_name=file_name
            )

            # 2. 更新数据库状态
            import asyncio
            if result.get('success'):
                await asyncio.to_thread(self.file_info_crud.mark_as_stored, task_id)
                logger.info(f"文档入库成功，已标记为已存储: {task_id}")
            else:
                await asyncio.to_thread(self.file_info_crud.revert_storage_status, task_id)
                logger.error(f"文档入库失败, 回滚状态: {result.get('error')}")

            return {
                'task_id': task_id,
                'file_name': file_name,
                'success': result.get('success'),
                'doc_id': result.get('doc_id'),
                'text_length': result.get('text_length'),
                'images_count': result.get('images_count'),
                'elapsed_time': result.get('elapsed_time'),
                'error': result.get('error')
            }

        except Exception as e:
            import asyncio
            await asyncio.to_thread(self.file_info_crud.revert_storage_status, task_id)
            logger.error(f"处理文档异常 {task_id}, 回滚状态: {e}", exc_info=True)
            return {
                'task_id': task_id,
                'file_name': file_name,
                'success': False,
                'error': str(e)
            }

    async def process_document_by_task_id(
        self,
        task_id: str,
        dry_run: bool = False
    ) -> Dict[str, Any]:
        """
        根据task_id处理单个文档

        Args:
            task_id: 任务ID
            dry_run: 仅预览模式

        Returns:
            处理结果
        """
        import asyncio
        file_info = await asyncio.to_thread(self.file_info_crud.get_by_task_id, task_id)
        if not file_info:
            return {
                'task_id': task_id,
                'success': False,
                'error': '文档不存在'
            }

        return await self._process_single_document(file_info, dry_run=dry_run)

    def get_pending_count(self) -> int:
        """获取待入库文档数量"""
        pending_files = self.file_info_crud.get_pending_files(limit=10000)
        return len(pending_files)

    def list_pending_documents(self, limit: int = 100) -> List[Dict]:
        """列出待入库文档"""
        pending_files = self.file_info_crud.get_pending_files(limit=limit)
        return [
            {
                'task_id': f.task_id,
                'file_name': f.file_name,
                'parse_status': f.parse_status,
                'storage_status': f.storage_status,
                'md_file_path': f.md_file_path
            }
            for f in pending_files
        ]

    def close(self):
        """关闭资源"""
        if self.db_session:
            self.db_session.close()
            logger.info("数据库会话已关闭")


async def main():
    """命令行入口"""
    import argparse

    parser = argparse.ArgumentParser(description='文档处理器 - MD文件入库')
    parser.add_argument('--limit', type=int, default=10, help='最大处理数量')
    parser.add_argument('--dry-run', action='store_true', help='预览模式，不实际入库')
    parser.add_argument('--task-id', type=str, help='指定处理单个文档')
    parser.add_argument('--list', action='store_true', help='列出待入库文档')

    args = parser.parse_args()

    processor = DocumentProcessor()

    try:
        if args.list:
            # 列出待入库文档
            pending = processor.list_pending_documents(limit=args.limit)
            print(f"\n待入库文档列表 (共 {len(pending)} 个):\n")
            print(f"{'Task ID':<20} {'File Name':<40} {'MD Path':<50}")
            print("-" * 110)
            for doc in pending:
                print(f"{doc['task_id']:<20} {doc['file_name']:<40} {doc['md_file_path'][:50]:<50}")
            return

        if args.task_id:
            # 处理单个文档
            result = await processor.process_document_by_task_id(
                task_id=args.task_id,
                dry_run=args.dry_run
            )
            print("\n处理结果:")
            print(f"  Task ID: {result['task_id']}")
            print(f"  Success: {result['success']}")
            if result.get('error'):
                print(f"  Error: {result['error']}")
            if result.get('doc_id'):
                print(f"  Doc ID: {result['doc_id']}")
            if result.get('text_length'):
                print(f"  Text Length: {result['text_length']}")
            if result.get('images_count') is not None:
                print(f"  Images Count: {result['images_count']}")
        else:
            # 批量处理
            results = await processor.process_pending_documents(
                limit=args.limit,
                dry_run=args.dry_run
            )
            print(f"\n批量处理完成，共处理 {len(results)} 个文档:\n")
            for result in results:
                status = "✓" if result['success'] else "✗"
                print(f"  {status} {result['task_id']}: {result.get('file_name', 'Unknown')}")
                if result.get('error'):
                    print(f"      Error: {result['error']}")

    finally:
        processor.close()


if __name__ == "__main__":
    asyncio.run(main())
