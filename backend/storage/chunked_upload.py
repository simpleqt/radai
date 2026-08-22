"""分块上传会话:目录/会话文件管理、分块写入、完成合并、过期清理与进度。(拆自 backend/storage/__init__.py)"""

from __future__ import annotations

from backend.config.loggers import get_logger

from backend.config import settings
from contextlib import contextmanager
from datetime import datetime
from datetime import timedelta
from pathlib import Path
from typing import Any
import fcntl
import json
import os
import shutil
import uuid

from backend.storage.local_storage import storage_manager

logger = get_logger(__name__)


class ChunkedUploadManager:
    """分块上传管理器"""

    def __init__(self):
        self.temp_dir = Path(settings.upload_temp_dir)
        self.chunk_size = settings.chunk_size
        self.expire_hours = settings.upload_expire_hours
        self._ensure_directories()

    def _ensure_directories(self):
        """确保临时目录存在"""
        self.temp_dir.mkdir(parents=True, exist_ok=True)
        logger.info(f"分块上传临时目录已准备: {self.temp_dir}")

    def _get_upload_session_dir(self, upload_id: str) -> Path:
        """获取上传会话目录"""
        try:
            normalized = str(uuid.UUID(str(upload_id)))
        except (ValueError, AttributeError, TypeError) as exc:
            raise ValueError("无效的上传会话 ID") from exc
        if normalized != str(upload_id).lower():
            raise ValueError("无效的上传会话 ID")
        return self.temp_dir / normalized

    @contextmanager
    def _session_lock(self, upload_id: str):
        session_dir = self._get_upload_session_dir(upload_id)
        if not session_dir.exists():
            yield False
            return
        lock_path = session_dir / ".lock"
        with open(lock_path, "a+b") as lock_file:
            fcntl.flock(lock_file.fileno(), fcntl.LOCK_EX)
            try:
                yield True
            finally:
                fcntl.flock(lock_file.fileno(), fcntl.LOCK_UN)

    def _read_session_unlocked(self, upload_id: str) -> dict[str, Any] | None:
        info_file = self._get_upload_session_dir(upload_id) / "session.json"
        if not info_file.exists():
            return None
        with open(info_file, encoding="utf-8") as f:
            return self._normalize_session(json.load(f))

    def _write_session_unlocked(self, upload_id: str, session: dict[str, Any]) -> None:
        session_dir = self._get_upload_session_dir(upload_id)
        info_file = session_dir / "session.json"
        temp_file = session_dir / f"session.{os.getpid()}.tmp"
        with open(temp_file, "w", encoding="utf-8") as f:
            json.dump(self._serialize_session(session), f, ensure_ascii=False, indent=2)
            f.flush()
            os.fsync(f.fileno())
        os.replace(temp_file, info_file)

    @staticmethod
    def _owned_by(session: dict[str, Any] | None, user_id: str) -> bool:
        return bool(session) and str(session.get("user_id") or "") == str(user_id or "")

    def _serialize_session(self, session: dict[str, Any]) -> dict[str, Any]:
        """将会话对象转换为可 JSON 序列化的结构。"""
        serialized = dict(session)
        uploaded_chunks = serialized.get("uploaded_chunks", set())
        if isinstance(uploaded_chunks, set):
            serialized["uploaded_chunks"] = sorted(uploaded_chunks)
        elif isinstance(uploaded_chunks, list):
            serialized["uploaded_chunks"] = sorted(set(uploaded_chunks))
        else:
            serialized["uploaded_chunks"] = []
        return serialized

    def _normalize_session(self, session: dict[str, Any]) -> dict[str, Any]:
        """将持久化会话转换为运行时结构。"""
        normalized = dict(session)
        uploaded_chunks = normalized.get("uploaded_chunks", [])
        if isinstance(uploaded_chunks, list):
            normalized["uploaded_chunks"] = set(uploaded_chunks)
        elif isinstance(uploaded_chunks, set):
            normalized["uploaded_chunks"] = uploaded_chunks
        else:
            normalized["uploaded_chunks"] = set()
        return normalized

    def init_upload(
        self,
        filename: str,
        total_size: int,
        content_type: str = None,
        *,
        user_id: str,
        metadata: dict[str, Any] | None = None,
    ) -> dict[str, Any]:
        """初始化分块上传

        Args:
            filename: 文件名
            total_size: 文件总大小
            content_type: 内容类型

        Returns:
            上传会话信息
        """
        if total_size <= 0:
            raise ValueError("文件大小必须大于 0")
        if not str(user_id or "").strip():
            raise ValueError("上传会话必须绑定用户")
        upload_id = str(uuid.uuid4())
        session_dir = self._get_upload_session_dir(upload_id)
        session_dir.mkdir(parents=True, exist_ok=True)

        total_chunks = (total_size + self.chunk_size - 1) // self.chunk_size

        session_info = {
            "upload_id": upload_id,
            "filename": filename,
            "total_size": total_size,
            "total_chunks": total_chunks,
            "content_type": content_type,
            "user_id": str(user_id),
            "uploaded_chunks": set(),
            "created_at": datetime.now().isoformat(),
            "chunk_size": self.chunk_size,
            "metadata": dict(metadata or {}),
            "state": "open",
        }

        self._write_session_unlocked(upload_id, session_info)

        logger.info(
            f"分块上传已初始化: upload_id={upload_id}, filename={filename}, chunks={total_chunks}"
        )

        return session_info

    def get_upload_session(
        self,
        upload_id: str,
        *,
        user_id: str,
    ) -> dict[str, Any] | None:
        """获取上传会话信息"""
        with self._session_lock(upload_id) as exists:
            if not exists:
                return None
            session = self._read_session_unlocked(upload_id)
            return session if self._owned_by(session, user_id) else None

    def upload_session_exists(self, upload_id: str) -> bool:
        """Return whether a persisted session exists without exposing its owner."""
        with self._session_lock(upload_id) as exists:
            return bool(exists and self._read_session_unlocked(upload_id))

    def save_chunk(
        self,
        upload_id: str,
        chunk_index: int,
        content: bytes,
        *,
        user_id: str,
    ) -> dict[str, Any]:
        """保存分块

        Args:
            upload_id: 上传ID
            chunk_index: 分块索引
            content: 分块内容

        Returns:
            保存结果
        """
        try:
            with self._session_lock(upload_id) as exists:
                if not exists:
                    return {"success": False, "error": "上传会话不存在"}
                session = self._read_session_unlocked(upload_id)
                if not self._owned_by(session, user_id):
                    return {"success": False, "error": "上传会话不存在"}
                if session.get("state", "open") != "open":
                    return {"success": False, "error": "上传会话正在完成"}
                if chunk_index < 0 or chunk_index >= session["total_chunks"]:
                    return {"success": False, "error": "分块索引超出范围"}
                remaining = session["total_size"] - chunk_index * session["chunk_size"]
                max_chunk_size = min(session["chunk_size"], remaining)
                if not content or len(content) > max_chunk_size:
                    return {"success": False, "error": "分块大小无效"}
                if chunk_index in session["uploaded_chunks"]:
                    progress = (
                        len(session["uploaded_chunks"]) / session["total_chunks"] * 100
                    )
                    return {
                        "success": True,
                        "message": "分块已存在",
                        "duplicate": True,
                        "uploaded": len(session["uploaded_chunks"]),
                        "total": session["total_chunks"],
                        "progress": progress,
                    }

                session_dir = self._get_upload_session_dir(upload_id)
                chunk_file = session_dir / f"chunk_{chunk_index:06d}"
                with open(chunk_file, "wb") as f:
                    f.write(content)

                session["uploaded_chunks"].add(chunk_index)
                self._write_session_unlocked(upload_id, session)

                progress = (
                    len(session["uploaded_chunks"]) / session["total_chunks"] * 100
                )

            logger.debug(
                f"分块已保存: upload_id={upload_id}, chunk={chunk_index}, progress={progress:.1f}%"
            )

            return {
                "success": True,
                "chunk_index": chunk_index,
                "uploaded": len(session["uploaded_chunks"]),
                "total": session["total_chunks"],
                "progress": progress,
            }

        except Exception as e:
            logger.error(
                f"保存分块失败: upload_id={upload_id}, chunk={chunk_index}, {e}"
            )
            return {"success": False, "error": str(e)}

    def complete_upload(
        self,
        upload_id: str,
        storage_type: str = "documents",
        *,
        user_id: str,
        cleanup_on_success: bool = True,
    ) -> tuple[bool, str, str | None]:
        """完成上传，合并所有分块

        Args:
            upload_id: 上传ID
            storage_type: 存储类型

        Returns:
            (是否成功, 消息, 文件路径)
        """
        try:
            with self._session_lock(upload_id) as exists:
                if not exists:
                    return False, "上传会话不存在", None
                session = self._read_session_unlocked(upload_id)
                if not self._owned_by(session, user_id):
                    return False, "上传会话不存在", None
                if session.get("state") == "completed":
                    return False, "上传会话正在完成", None
                if session.get("state", "open") != "open":
                    return False, "上传会话正在完成", None
                if len(session["uploaded_chunks"]) < session["total_chunks"]:
                    missing = (
                        set(range(session["total_chunks"])) - session["uploaded_chunks"]
                    )
                    return False, f"还有 {len(missing)} 个分块未上传", None

                session_dir = self._get_upload_session_dir(upload_id)
                filename = session["filename"]
                file_id, final_path = storage_manager.generate_storage_path(
                    filename,
                    storage_type,
                )
                with open(final_path, "wb") as outfile:
                    for i in range(session["total_chunks"]):
                        chunk_file = session_dir / f"chunk_{i:06d}"
                        with open(chunk_file, "rb") as infile:
                            outfile.write(infile.read())

                total_size = os.path.getsize(final_path)

                if total_size != session["total_size"]:
                    os.remove(final_path)
                    return (
                        False,
                        f"文件大小不匹配: 期望 {session['total_size']}, 实际 {total_size}",
                        None,
                    )

                if cleanup_on_success:
                    # The route captures session metadata before this call. Cleanup is
                    # intentionally the final operation while the lock is held.
                    shutil.rmtree(session_dir)
                else:
                    session["state"] = "completed"
                    session["merged_path"] = final_path
                    self._write_session_unlocked(upload_id, session)

            logger.info(
                f"分块上传完成: upload_id={upload_id}, file_id={file_id}, path={final_path}"
            )

            return True, "文件上传成功", final_path

        except Exception as e:
            logger.error(f"合并分块失败: upload_id={upload_id}, {e}")
            return False, f"合并分块失败: {str(e)}", None

    def reset_completed_upload(self, upload_id: str, *, user_id: str) -> bool:
        """下游持久化失败后恢复分块会话，使客户端可以安全重试。"""
        try:
            with self._session_lock(upload_id) as exists:
                if not exists:
                    return False
                session = self._read_session_unlocked(upload_id)
                if not self._owned_by(session, user_id):
                    return False
                merged_path = str(session.pop("merged_path", "") or "")
                session["state"] = "open"
                self._write_session_unlocked(upload_id, session)
                if merged_path:
                    try:
                        os.unlink(merged_path)
                    except FileNotFoundError:
                        pass
            return True
        except Exception as exc:
            logger.error("恢复分块上传会话失败: upload_id=%s, %s", upload_id, exc)
            return False

    def cancel_upload(self, upload_id: str, *, user_id: str) -> bool:
        """取消上传"""
        try:
            with self._session_lock(upload_id) as exists:
                if not exists:
                    return False
                session = self._read_session_unlocked(upload_id)
                if not self._owned_by(session, user_id):
                    return False
                shutil.rmtree(self._get_upload_session_dir(upload_id))
            logger.info(f"上传已取消: upload_id={upload_id}")
            return True
        except Exception as e:
            logger.error(f"取消上传失败: upload_id={upload_id}, {e}")
            return False

    def cleanup_upload_session(self, upload_id: str):
        """清理上传会话"""
        session_dir = self._get_upload_session_dir(upload_id)
        if session_dir.exists():
            shutil.rmtree(session_dir)

    def cleanup_expired_sessions(self):
        """清理过期的上传会话。

        必须先取会话排它锁再删:无锁 rmtree 会撕掉正在续传/合并的会话
        (合并中读 chunk 失败留半文件;与锁文件重建交错留下无 session.json
        的永久孤儿目录)。过期判据用 session.json 的 mtime(最后活动时间)
        而非 created_at:慢链路大文件自创建起可能超时但仍在活跃续传。
        """
        if not self.temp_dir.exists():
            return

        cutoff_ts = (datetime.now() - timedelta(hours=self.expire_hours)).timestamp()
        cleaned_count = 0

        for session_dir in self.temp_dir.iterdir():
            if not session_dir.is_dir():
                continue
            try:
                info_file = session_dir / "session.json"
                if not info_file.exists():
                    # 早前无锁清理可能留下的残目录:同样按 mtime 超期回收,
                    # 否则永远无人认领。
                    if session_dir.stat().st_mtime < cutoff_ts:
                        shutil.rmtree(session_dir, ignore_errors=True)
                        cleaned_count += 1
                    continue
                if info_file.stat().st_mtime >= cutoff_ts:
                    continue
                upload_id = session_dir.name
                with self._session_lock(upload_id) as locked:
                    if not locked:
                        continue
                    # 持锁后复核:等待锁期间会话可能刚被续传/合并刷新。
                    if (
                        not info_file.exists()
                        or info_file.stat().st_mtime >= cutoff_ts
                    ):
                        continue
                    shutil.rmtree(session_dir)
                    cleaned_count += 1
            except Exception as e:
                logger.warning(f"清理过期会话时出错: {session_dir}, {e}")

        if cleaned_count > 0:
            logger.info(f"已清理 {cleaned_count} 个过期上传会话")

        return cleaned_count

    def get_upload_progress(
        self,
        upload_id: str,
        *,
        user_id: str,
    ) -> dict[str, Any] | None:
        """获取上传进度"""
        session = self.get_upload_session(upload_id, user_id=user_id)

        if not session:
            return None

        return {
            "upload_id": upload_id,
            "filename": session["filename"],
            "total_size": session["total_size"],
            "total_chunks": session["total_chunks"],
            "uploaded_chunks": len(session["uploaded_chunks"]),
            "progress": len(session["uploaded_chunks"]) / session["total_chunks"] * 100,
            "created_at": session["created_at"],
        }


# 全局单例(拆分前定义于 backend/storage/__init__.py 末尾)
chunked_upload_manager = ChunkedUploadManager()
