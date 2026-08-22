"""本地文件存储:目录规划、原子写入、哈希、过期/缓存清理、统计与备份。(拆自 backend/storage/__init__.py)"""

from __future__ import annotations

from backend.config.loggers import get_logger

from backend.config import settings
from collections.abc import Generator
from contextlib import asynccontextmanager
from datetime import datetime
from pathlib import Path
from typing import Any
import asyncio
import hashlib
import os
import shutil
import time
import uuid

logger = get_logger(__name__)


class StorageManager:
    """文件存储管理器"""

    def __init__(self):
        self.base_path = Path(settings.storage_base_path)
        self.documents_path = Path(settings.documents_path)
        self.results_path = Path(settings.results_path)
        self.cache_path = Path(settings.cache_path)

        self._ensure_directories()

    def _ensure_directories(self):
        """确保存储目录存在"""
        directories = [
            self.base_path,
            self.documents_path,
            self.results_path,
            self.cache_path,
        ]

        for directory in directories:
            directory.mkdir(parents=True, exist_ok=True)
            logger.info(f"存储目录已准备: {directory}")

    def generate_storage_path(
        self, filename: str, storage_type: str = "documents"
    ) -> tuple[str, str]:
        """生成存储路径

        Args:
            filename: 原始文件名
            storage_type: 存储类型 (documents/results/cache)

        Returns:
            (文件ID, 完整路径)
        """
        file_id = str(uuid.uuid4())
        file_ext = Path(filename).suffix.lower()
        storage_dir = self._get_storage_dir(storage_type)
        stored_filename = f"{file_id}{file_ext}"

        storage_path = storage_dir / stored_filename

        return file_id, str(storage_path)

    def _get_storage_dir(self, storage_type: str) -> Path:
        """获取存储目录"""
        type_mapping = {
            "documents": self.documents_path,
            "results": self.results_path,
            "cache": self.cache_path,
        }
        return type_mapping.get(storage_type, self.documents_path)

    async def save_file(
        self, content: bytes, filename: str, storage_type: str = "documents"
    ) -> tuple[str, str]:
        """保存文件

        Args:
            content: 文件内容
            filename: 原始文件名
            storage_type: 存储类型

        Returns:
            (文件ID, 存储路径)
        """
        file_id, storage_path = self.generate_storage_path(filename, storage_type)

        async with self._atomic_write(storage_path) as temp_path:
            await asyncio.to_thread(Path(temp_path).write_bytes, content)

        file_hash = await asyncio.to_thread(self.calculate_file_hash, storage_path)
        logger.info(
            f"文件已保存: {storage_path} (ID: {file_id}, Hash: {file_hash[:16]}...)"
        )

        return file_id, storage_path

    @asynccontextmanager
    async def _atomic_write(self, target_path: str):
        """原子写入上下文管理器

        先写入临时文件，成功后移动到目标位置
        """
        temp_path = f"{target_path}.tmp.{os.getpid()}"
        try:
            yield temp_path
            await asyncio.to_thread(shutil.move, temp_path, target_path)
        except Exception as e:
            if os.path.exists(temp_path):
                await asyncio.to_thread(os.remove, temp_path)
            raise e

    def calculate_file_hash(self, file_path: str) -> str:
        """计算文件哈希值

        Args:
            file_path: 文件路径

        Returns:
            文件哈希值 (SHA256)
        """
        sha256_hash = hashlib.sha256()

        with open(file_path, "rb") as f:
            for chunk in iter(lambda: f.read(8192), b""):
                sha256_hash.update(chunk)

        return sha256_hash.hexdigest()

    async def get_file(self, file_path: str) -> bytes | None:
        """读取文件内容

        Args:
            file_path: 文件路径

        Returns:
            文件内容
        """
        if not os.path.exists(file_path):
            logger.warning(f"文件不存在: {file_path}")
            return None

        return await asyncio.to_thread(Path(file_path).read_bytes)

    async def delete_file(self, file_path: str) -> bool:
        """删除文件

        Args:
            file_path: 文件路径

        Returns:
            是否删除成功
        """
        if not os.path.exists(file_path):
            logger.warning(f"文件不存在，无法删除: {file_path}")
            return False

        try:
            os.remove(file_path)
            logger.info(f"文件已删除: {file_path}")
            return True
        except Exception as e:
            logger.error(f"删除文件失败: {file_path}, {e}")
            return False

    def file_exists(self, file_path: str) -> bool:
        """检查文件是否存在"""
        return os.path.exists(file_path)

    def get_file_size(self, file_path: str) -> int:
        """获取文件大小"""
        if not os.path.exists(file_path):
            return 0
        return os.path.getsize(file_path)

    def get_file_info(self, file_path: str) -> dict:
        """获取文件信息

        Args:
            file_path: 文件路径

        Returns:
            文件信息字典
        """
        if not os.path.exists(file_path):
            return {}

        stat = os.stat(file_path)

        return {
            "path": file_path,
            "size": stat.st_size,
            "created_at": datetime.fromtimestamp(stat.st_ctime).isoformat(),
            "modified_at": datetime.fromtimestamp(stat.st_mtime).isoformat(),
            "hash": (
                self.calculate_file_hash(file_path)
                if os.path.isfile(file_path)
                else None
            ),
        }

    async def cleanup_expired_files(
        self, storage_type: str = "documents", max_age_days: int | None = None
    ):
        """清理过期文件

        Args:
            storage_type: 存储类型
            max_age_days: 最大保留天数
        """
        if max_age_days is None:
            max_age_days = settings.file_expire_days

        storage_dir = self._get_storage_dir(storage_type)

        if not storage_dir.exists():
            return

        cutoff_time = time.time() - (max_age_days * 24 * 60 * 60)

        def _cleanup_sync() -> tuple[int, int]:
            deleted_count = 0
            freed_space = 0
            for file_path in storage_dir.rglob("*"):
                if not file_path.is_file():
                    continue
                try:
                    file_stat = file_path.stat()
                    if file_stat.st_mtime < cutoff_time:
                        file_path.unlink()
                        deleted_count += 1
                        freed_space += file_stat.st_size
                except FileNotFoundError:
                    continue
                except Exception as exc:
                    logger.warning("清理文件时出错: %s, %s", file_path, exc)
            return deleted_count, freed_space

        deleted_count, freed_space = await asyncio.to_thread(_cleanup_sync)

        logger.info(
            f"清理完成: {storage_type}, "
            f"删除 {deleted_count} 个文件, "
            f"释放 {freed_space / 1024 / 1024:.2f} MB"
        )

        return {"deleted_count": deleted_count, "freed_space": freed_space}

    async def cleanup_cache(self, max_age_hours: int | None = None):
        """清理缓存文件

        Args:
            max_age_hours: 最大保留小时数
        """
        if max_age_hours is None:
            max_age_hours = settings.cache_expire_hours

        return await self.cleanup_expired_files("cache", max_age_hours / 24)

    async def cleanup_all(self):
        """清理所有过期文件"""
        results = {}

        for storage_type in ["documents", "results", "cache"]:
            results[storage_type] = await self.cleanup_expired_files(storage_type)

        return results

    def get_storage_stats(self, storage_type: str | None = None) -> dict[str, Any]:
        """获取存储统计信息

        Args:
            storage_type: 存储类型，None 表示所有

        Returns:
            统计信息字典
        """
        stats: dict[str, Any] = {
            "total_size": 0,
            "file_count": 0,
            "directories": {},
        }

        target_types = (
            [storage_type] if storage_type else ["documents", "results", "cache"]
        )

        for type_name in target_types:
            storage_dir = self._get_storage_dir(type_name)

            if storage_dir.exists():
                dir_size = 0
                file_count = 0

                for file_path in storage_dir.rglob("*"):
                    try:
                        if file_path.is_file():
                            file_size = file_path.stat().st_size
                            dir_size += file_size
                            file_count += 1
                    except Exception:
                        pass

                stats["directories"][type_name] = {
                    "path": str(storage_dir),
                    "size": dir_size,
                    "size_human": self._format_size(dir_size),
                    "file_count": file_count,
                }

                stats["total_size"] += dir_size
                stats["file_count"] += file_count

        stats["total_size_human"] = self._format_size(stats["total_size"])

        return stats

    def _format_size(self, size_bytes: int) -> str:
        """格式化文件大小"""
        size = float(size_bytes)
        for unit in ["B", "KB", "MB", "GB", "TB"]:
            if size < 1024:
                return f"{size:.2f} {unit}"
            size /= 1024

        return f"{size:.2f} PB"

    def check_disk_usage(self) -> dict[str, Any]:
        """检查磁盘使用情况

        Returns:
            磁盘使用信息
        """
        base_path = str(self.base_path)

        if not os.path.exists(base_path):
            return {"error": "存储目录不存在"}

        total, used, free = shutil.disk_usage(base_path)

        usage_ratio = used / total if total > 0 else 0
        warning_threshold = settings.disk_usage_warning_threshold

        return {
            "total": self._format_size(total),
            "used": self._format_size(used),
            "free": self._format_size(free),
            "usage_ratio": f"{usage_ratio * 100:.1f}%",
            "warning": usage_ratio >= warning_threshold,
            "threshold": f"{warning_threshold * 100:.0f}%",
        }

    async def cleanup_by_policy(self):
        """按策略自动清理"""
        stats = self.check_disk_usage()

        if stats.get("warning", False):
            logger.warning(f"磁盘空间不足: {stats['usage_ratio']}")

            aggressive_cleanup = await self.cleanup_all()
            logger.warning(f"已执行紧急清理: {aggressive_cleanup}")

        await self.cleanup_cache()

    def get_files_by_type(
        self, storage_type: str, extensions: list
    ) -> Generator[Path, None, None]:
        """按扩展名获取文件

        Args:
            storage_type: 存储类型
            extensions: 扩展名列表

        Yields:
            匹配的 文件路径
        """
        storage_dir = self._get_storage_dir(storage_type)

        if not storage_dir.exists():
            return

        extensions = [
            ext.lower() if ext.startswith(".") else f".{ext.lower()}"
            for ext in extensions
        ]

        for file_path in storage_dir.rglob("*"):
            if file_path.is_file() and file_path.suffix.lower() in extensions:
                yield file_path

    async def backup_files(self, backup_dir: str, storage_type: str = "documents"):
        """备份文件

        Args:
            backup_dir: 备份目录
            storage_type: 存储类型
        """
        storage_dir = self._get_storage_dir(storage_type)

        if not storage_dir.exists():
            logger.warning(f"存储目录不存在: {storage_dir}")
            return

        def _backup_sync() -> tuple[Path, int]:
            backup_path = (
                Path(backup_dir)
                / f"{storage_type}_{datetime.now().strftime('%Y%m%d_%H%M%S')}"
            )
            backup_path.mkdir(parents=True, exist_ok=True)
            backup_count = 0
            for file_path in storage_dir.rglob("*"):
                if not file_path.is_file():
                    continue
                relative_path = file_path.relative_to(storage_dir)
                dest_path = backup_path / relative_path
                dest_path.parent.mkdir(parents=True, exist_ok=True)
                shutil.copy2(file_path, dest_path)
                backup_count += 1
            return backup_path, backup_count

        backup_path, backup_count = await asyncio.to_thread(_backup_sync)
        logger.info(f"备份完成: {backup_path}, 共 {backup_count} 个文件")

        return str(backup_path)


# 全局单例(拆分前定义于 backend/storage/__init__.py 末尾)
storage_manager = StorageManager()
