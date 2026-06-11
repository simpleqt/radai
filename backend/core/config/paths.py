from __future__ import annotations

import os
import re
import shutil
from pathlib import Path, PureWindowsPath

from backend.core.thread_key import build_internal_thread_key, parse_internal_thread_key

THREAD_STORAGE_PREFIX = "/threads"


def _default_local_base_dir() -> Path:
    backend_dir = Path(__file__).resolve().parents[2]
    return backend_dir / ".runtime"


def _join_host_path(base: str, *parts: str) -> str:
    if not parts:
        return base

    if re.match(r"^[A-Za-z]:[\\/]", base) or base.startswith("\\\\") or "\\" in base:
        result = PureWindowsPath(base)
        for part in parts:
            result /= part
        return str(result)

    result = Path(base)
    for part in parts:
        result /= part
    return str(result)


def join_host_path(base: str, *parts: str) -> str:
    return _join_host_path(base, *parts)


class Paths:
    def __init__(self, base_dir: str | Path | None = None) -> None:
        self._base_dir = Path(base_dir).resolve() if base_dir is not None else None

    @property
    def host_base_dir(self) -> Path:
        if env := os.getenv("BACKEND_HOST_BASE_DIR"):
            return Path(env)
        return self.base_dir

    def _host_base_dir_str(self) -> str:
        if env := os.getenv("BACKEND_HOST_BASE_DIR"):
            return env
        return str(self.base_dir)

    @property
    def base_dir(self) -> Path:
        if self._base_dir is not None:
            return self._base_dir
        if env_home := os.getenv("BACKEND_HOME"):
            return Path(env_home).resolve()
        return _default_local_base_dir()

    @property
    def user_md_file(self) -> Path:
        return self.base_dir / "USER.md"

    @property
    def agents_dir(self) -> Path:
        return self.base_dir / "agents"

    def agent_dir(self, name: str) -> Path:
        return self.agents_dir / name.lower()

    def thread_dir(self, thread_id: str) -> Path:
        user_id, external_thread_id = parse_internal_thread_key(thread_id)
        if user_id is None:
            return self.base_dir / "threads" / external_thread_id
        return self.base_dir / "users" / user_id / "threads" / external_thread_id

    def thread_files_dir(self, thread_id: str) -> Path:
        return self.thread_dir(thread_id) / "files"

    def thread_uploads_dir(self, thread_id: str) -> Path:
        return self.thread_files_dir(thread_id) / "uploads"

    def thread_artifacts_dir(self, thread_id: str) -> Path:
        return self.thread_files_dir(thread_id) / "artifacts"

    def thread_cache_dir(self, thread_id: str) -> Path:
        return self.thread_dir(thread_id) / "cache"

    def thread_exports_dir(self, thread_id: str) -> Path:
        return self.thread_dir(thread_id) / "exports"

    def host_thread_dir(self, thread_id: str) -> str:
        user_id, external_thread_id = parse_internal_thread_key(thread_id)
        if user_id is None:
            return _join_host_path(self._host_base_dir_str(), "threads", external_thread_id)
        return _join_host_path(self._host_base_dir_str(), "users", user_id, "threads", external_thread_id)

    def host_thread_files_dir(self, thread_id: str) -> str:
        return _join_host_path(self.host_thread_dir(thread_id), "files")

    def host_thread_uploads_dir(self, thread_id: str) -> str:
        return _join_host_path(self.host_thread_files_dir(thread_id), "uploads")

    def host_thread_artifacts_dir(self, thread_id: str) -> str:
        return _join_host_path(self.host_thread_files_dir(thread_id), "artifacts")

    def host_thread_cache_dir(self, thread_id: str) -> str:
        return _join_host_path(self.host_thread_dir(thread_id), "cache")

    def host_thread_exports_dir(self, thread_id: str) -> str:
        return _join_host_path(self.host_thread_dir(thread_id), "exports")

    def ensure_thread_dirs(self, thread_id: str) -> None:
        for path in [
            self.thread_uploads_dir(thread_id),
            self.thread_artifacts_dir(thread_id),
            self.thread_cache_dir(thread_id),
            self.thread_exports_dir(thread_id),
        ]:
            path.mkdir(parents=True, exist_ok=True)
            path.chmod(0o777)

    def delete_thread_dir(self, thread_id: str) -> None:
        thread_dir = self.thread_dir(thread_id)
        if thread_dir.exists():
            shutil.rmtree(thread_dir)

    def resolve_thread_file_path(self, thread_id: str, virtual_path: str) -> Path:
        stripped = virtual_path.lstrip("/")
        prefix = THREAD_STORAGE_PREFIX.lstrip("/")
        if stripped != prefix and not stripped.startswith(prefix + "/"):
            raise ValueError(f"Path must start with /{prefix}")

        relative = stripped[len(prefix) :].lstrip("/")
        base = self.thread_dir(thread_id).resolve()
        actual = (base / relative).resolve()

        try:
            actual.relative_to(base)
        except ValueError as exc:
            raise ValueError("Access denied: path traversal detected") from exc

        return actual


_paths: Paths | None = None


def get_paths() -> Paths:
    global _paths
    if _paths is None:
        _paths = Paths()
    return _paths


def resolve_path(path: str) -> Path:
    resolved = Path(path)
    if not resolved.is_absolute():
        resolved = get_paths().base_dir / path
    return resolved.resolve()
