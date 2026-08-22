"""Filesystem project packages with MySQL-backed ownership and authorization."""

from __future__ import annotations

import json
import os
import re
import shutil
import tempfile
import uuid
import zipfile
from dataclasses import dataclass
from datetime import datetime
from pathlib import Path
from typing import Any

PROJECT_ID_PATTERN = re.compile(r"^[a-zA-Z0-9][a-zA-Z0-9_-]{0,63}$")
RESOURCE_FILES = {
    "skill": "SKILL.md",
    "manifest": "manifest.json",
    "background": "docs/background.md",
    "data_quality": "docs/data_quality.md",
    "table_schema": "docs/table_schema.md",
}


@dataclass(frozen=True)
class GroundwaterProjectContext:
    project_id: str
    user_id: str
    project_name: str
    project_root: Path
    manifest: dict[str, Any]
    read_only: bool = False

    @property
    def data_version(self) -> int:
        return int(self.manifest.get("data_version") or 1)


# 项目包解压尺寸上限：包本体已限 2GiB，此处再限制解压后的单条目与
# 累计尺寸，防高压缩比 zip 炸弹耗尽磁盘。
_MAX_ARCHIVE_MEMBER_BYTES = 4 * 1024 * 1024 * 1024
_MAX_ARCHIVE_TOTAL_BYTES = 16 * 1024 * 1024 * 1024


class GroundwaterProjectService:
    BUILTIN_PROJECT_ID = "liutang-groundwater"

    def __init__(
        self,
        *,
        projects_root: Path | None = None,
        builtin_root: Path | None = None,
    ):
        repo_root = Path(__file__).resolve().parents[3]
        configured_root = os.getenv("GROUNDWATER_PROJECT_ROOT", "").strip()
        configured_builtin_root = os.getenv(
            "GROUNDWATER_BUILTIN_ROOT", ""
        ).strip()
        self.projects_root = projects_root or (
            Path(configured_root)
            if configured_root
            else repo_root / "data" / "groundwater_projects"
        )
        self.builtin_root = builtin_root or (
            Path(configured_builtin_root)
            if configured_builtin_root
            else repo_root / "六塘项目数据信息" / "standardized"
        )
        # Explicit roots are used by isolated unit tests and do not require MySQL.
        catalog_disabled = os.getenv("GROUNDWATER_CATALOG_DISABLED", "").strip().lower()
        self.catalog_enabled = (
            projects_root is None
            and builtin_root is None
            and catalog_disabled not in {"1", "true", "yes"}
        )

    def list_projects(self, user_id: str, *, is_admin: bool = False) -> list[dict[str, Any]]:
        if not self.catalog_enabled:
            return self._list_filesystem_projects(user_id)
        self.bootstrap_catalog()
        from backend.db.connection import get_session
        from backend.db.models import GroundwaterProject, GroundwaterProjectUser

        with get_session() as session:
            query = session.query(GroundwaterProject).filter(
                GroundwaterProject.status == "active",
                GroundwaterProject.is_builtin.is_(False),
            )
            if not is_admin:
                query = (
                    query.outerjoin(
                        GroundwaterProjectUser,
                        GroundwaterProjectUser.project_id == GroundwaterProject.project_id,
                    )
                    .filter(GroundwaterProjectUser.user_id == user_id)
                )
            records = query.order_by(GroundwaterProject.created_at.desc()).all()
            return [self._record_summary(record) for record in records]

    def list_admin_projects(self) -> list[dict[str, Any]]:
        projects = self.list_projects("", is_admin=True)
        from sqlalchemy import func

        from backend.db.connection import get_session
        from backend.db.models import GroundwaterProjectUser

        with get_session() as session:
            counts = dict(
                session.query(
                    GroundwaterProjectUser.project_id,
                    func.count(GroundwaterProjectUser.id),
                )
                .group_by(GroundwaterProjectUser.project_id)
                .all()
            )
        for project in projects:
            project["authorized_user_count"] = int(counts.get(project["project_id"], 0))
        return projects

    def list_users(self) -> list[dict[str, Any]]:
        from backend.db.connection import get_session
        from backend.db.models import User

        with get_session() as session:
            users = session.query(User).filter(User.is_active.is_(True)).order_by(User.username).all()
            return [
                {
                    "user_id": user.user_id,
                    "username": user.username,
                    "email": user.email,
                    "role": user.role or "user",
                }
                for user in users
            ]

    def list_bindings(self, project_id: str) -> list[dict[str, Any]]:
        from backend.db.connection import get_session
        from backend.db.models import GroundwaterProjectUser, User

        with get_session() as session:
            rows = (
                session.query(GroundwaterProjectUser, User)
                .join(User, User.user_id == GroundwaterProjectUser.user_id)
                .filter(GroundwaterProjectUser.project_id == project_id)
                .order_by(User.username)
                .all()
            )
            return [
                {
                    "user_id": user.user_id,
                    "username": user.username,
                    "email": user.email,
                    "permission": binding.permission,
                }
                for binding, user in rows
            ]

    def replace_bindings(
        self,
        project_id: str,
        user_ids: list[str],
        *,
        granted_by: str,
    ) -> list[dict[str, Any]]:
        self._catalog_record(project_id)
        from backend.db.connection import get_session
        from backend.db.models import GroundwaterProjectUser, User

        normalized = list(dict.fromkeys(value.strip() for value in user_ids if value.strip()))
        with get_session() as session:
            existing_users = {
                value
                for (value,) in session.query(User.user_id).filter(User.user_id.in_(normalized)).all()
            }
            missing = [value for value in normalized if value not in existing_users]
            if missing:
                raise ValueError(f"用户不存在: {', '.join(missing)}")
            session.query(GroundwaterProjectUser).filter(
                GroundwaterProjectUser.project_id == project_id
            ).delete(synchronize_session=False)
            for user_id in normalized:
                session.add(
                    GroundwaterProjectUser(
                        id=str(uuid.uuid4()),
                        project_id=project_id,
                        user_id=user_id,
                        permission="viewer",
                        granted_by=granted_by,
                    )
                )
            session.commit()
        return self.list_bindings(project_id)

    def create_project(
        self,
        user_id: str,
        *,
        name: str,
        project_id: str | None = None,
        region: str = "",
        coordinate_system: str = "",
    ) -> GroundwaterProjectContext:
        normalized_id = project_id or f"gw-{uuid.uuid4().hex[:12]}"
        self._validate_project_id(normalized_id)
        root = (
            self.projects_root / normalized_id
            if self.catalog_enabled
            else self._user_root(user_id) / normalized_id
        )
        if root.exists():
            raise ValueError(f"项目已存在: {normalized_id}")
        if self.catalog_enabled:
            self.bootstrap_catalog()
            try:
                self._catalog_record(normalized_id)
            except FileNotFoundError:
                pass
            else:
                raise ValueError(f"项目已存在: {normalized_id}")
        for relative in ("data", "docs", "images", "attachments"):
            (root / relative).mkdir(parents=True, exist_ok=True)
        now = datetime.now().astimezone().isoformat()
        manifest = {
            "schema_version": "1.0",
            "project": {
                "name": name.strip(),
                "code": normalized_id,
                "region": region.strip(),
                "status": "created",
            },
            "data_version": 1,
            "generated_at": now,
            "coordinate_reference": {
                "horizontal_crs": coordinate_system.strip() or None,
                "confirmed": bool(coordinate_system.strip()),
            },
            "record_counts": {},
            "datasets": {},
            "quality_status": "empty",
        }
        self._write_json(root / "manifest.json", manifest)
        (root / "SKILL.md").write_text(
            self._build_skill(normalized_id, name.strip(), region.strip()),
            encoding="utf-8",
        )
        (root / "docs" / "background.md").write_text(
            f"# {name.strip()}背景信息\n\n区域：{region.strip() or '待补充'}\n",
            encoding="utf-8",
        )
        if self.catalog_enabled:
            self._upsert_catalog(normalized_id, root, manifest, created_by=user_id)
        return self._context(user_id, normalized_id, root)

    def update_project(
        self,
        project_id: str,
        *,
        name: str | None = None,
        region: str | None = None,
        coordinate_system: str | None = None,
    ) -> GroundwaterProjectContext:
        if not self.catalog_enabled:
            raise PermissionError("当前运行模式不支持管理端项目更新")
        from backend.db.connection import get_session
        from backend.db.models import GroundwaterProject

        self.bootstrap_catalog()
        with get_session() as session:
            record = session.get(GroundwaterProject, project_id)
            if record is None or record.status != "active":
                raise FileNotFoundError(f"项目不存在: {project_id}")
            if record.is_builtin:
                raise PermissionError("内置示例项目为只读")

            root = Path(record.storage_path)
            manifest_path = self._safe_child(root, "manifest.json")
            manifest = json.loads(manifest_path.read_text(encoding="utf-8-sig"))
            project = manifest.setdefault("project", {})

            if name is not None and name.strip():
                project["name"] = name.strip()
                record.name = name.strip()
            if region is not None:
                project["region"] = region.strip()
                record.region = region.strip()
            if coordinate_system is not None:
                coordinate_reference = manifest.setdefault("coordinate_reference", {})
                coordinate_reference["horizontal_crs"] = coordinate_system.strip() or None
                coordinate_reference["confirmed"] = bool(coordinate_system.strip())

            manifest["generated_at"] = datetime.now().astimezone().isoformat()
            self._write_json(manifest_path, manifest)
            record.manifest_snapshot = manifest
            session.commit()

        return self._context("", project_id, root)

    def archive_project(self, project_id: str) -> dict[str, Any]:
        if not self.catalog_enabled:
            raise PermissionError("当前运行模式不支持管理端项目删除")
        from backend.db.connection import get_session
        from backend.db.models import GroundwaterProject

        self.bootstrap_catalog()
        with get_session() as session:
            record = session.get(GroundwaterProject, project_id)
            if record is None or record.status != "active":
                raise FileNotFoundError(f"项目不存在: {project_id}")
            if record.is_builtin:
                raise PermissionError("内置示例项目为只读，不能删除")
            record.status = "archived"
            session.commit()
            return {
                "project_id": project_id,
                "status": "archived",
                "storage_path": record.storage_path,
            }

    def resolve(
        self,
        user_id: str,
        project_id: str,
        *,
        is_admin: bool = False,
    ) -> GroundwaterProjectContext:
        self._validate_project_id(project_id)
        if not self.catalog_enabled:
            if project_id == self.BUILTIN_PROJECT_ID and self.builtin_root.exists():
                return self._context(user_id, project_id, self.builtin_root, read_only=True)
            root = self._user_root(user_id) / project_id
            if not root.exists():
                raise FileNotFoundError(f"项目不存在或无权访问: {project_id}")
            return self._context(user_id, project_id, root)

        self.bootstrap_catalog()
        record = self._catalog_record(project_id)
        if not is_admin and not self._user_can_access(user_id, project_id):
            raise PermissionError(f"用户未获得项目访问权限: {project_id}")
        root = Path(record.storage_path)
        if not root.exists():
            raise FileNotFoundError(f"项目数据目录不存在: {project_id}")
        return self._context(user_id, project_id, root, read_only=bool(record.is_builtin))

    def read_resource(
        self,
        context: GroundwaterProjectContext,
        resource: str,
        *,
        limit: int = 100,
        offset: int = 0,
    ) -> dict[str, Any]:
        relative = RESOURCE_FILES.get(resource)
        if not relative:
            raise ValueError(f"不支持的项目资源: {resource}")
        path = self._safe_child(context.project_root, relative)
        if not path.exists():
            return {
                "project_id": context.project_id,
                "resource": resource,
                "data_version": context.data_version,
                "exists": False,
            }
        if path.suffix.lower() == ".json":
            value = json.loads(path.read_text(encoding="utf-8-sig"))
            if isinstance(value, list):
                total_count = len(value)
                safe_offset = max(0, offset)
                value = value[
                    safe_offset : safe_offset + max(1, min(limit, 500))
                ]
            else:
                total_count = None
            content: Any = value
        else:
            total_count = None
            content = path.read_text(encoding="utf-8")[:50000]
        return {
            "project_id": context.project_id,
            "project_name": context.project_name,
            "resource": resource,
            "data_version": context.data_version,
            "exists": True,
            "source": relative,
            "content": content,
            "offset": max(0, offset),
            "returned_count": len(content) if isinstance(content, list) else None,
            "total_count": total_count,
        }

    def import_zip(
        self,
        context: GroundwaterProjectContext,
        archive_path: Path,
    ) -> GroundwaterProjectContext:
        if context.read_only:
            raise PermissionError("内置示例项目为只读")
        with tempfile.TemporaryDirectory(prefix="groundwater-import-") as temp_dir:
            temp_root = Path(temp_dir)
            with zipfile.ZipFile(archive_path) as archive:
                total_uncompressed = 0
                for member in archive.infolist():
                    target = (temp_root / member.filename).resolve()
                    if temp_root.resolve() not in target.parents and target != temp_root.resolve():
                        raise ValueError(f"ZIP 包含不安全路径: {member.filename}")
                    if not member.is_dir():
                        # 解压炸弹防护：单条目与累计解压尺寸封顶（包本体已限
                        # 2GiB，但压缩比可放大百倍）。
                        if member.file_size > _MAX_ARCHIVE_MEMBER_BYTES:
                            raise ValueError(
                                f"ZIP 条目解压后超过上限: {member.filename}"
                            )
                        total_uncompressed += member.file_size
                if total_uncompressed > _MAX_ARCHIVE_TOTAL_BYTES:
                    raise ValueError("ZIP 解压总量超过上限，疑似压缩炸弹")
                archive.extractall(temp_root)
            package_root = self._find_package_root(temp_root)
            for item in package_root.iterdir():
                target = context.project_root / item.name
                if item.is_dir():
                    shutil.copytree(item, target, dirs_exist_ok=True)
                else:
                    shutil.copy2(item, target)
        manifest_path = context.project_root / "manifest.json"
        if not manifest_path.exists():
            raise ValueError("项目包缺少 manifest.json")
        manifest = json.loads(manifest_path.read_text(encoding="utf-8-sig"))
        manifest["data_version"] = int(manifest.get("data_version") or 0) + 1
        manifest["generated_at"] = datetime.now().astimezone().isoformat()
        self._write_json(manifest_path, manifest)
        if self.catalog_enabled:
            self._upsert_catalog(
                context.project_id,
                context.project_root,
                manifest,
                created_by=None,
            )
        return self._context(context.user_id, context.project_id, context.project_root)

    def bootstrap_catalog(self) -> None:
        if not self.catalog_enabled:
            return
        from backend.db.connection import get_session
        from backend.db.models import GroundwaterProject

        candidates: list[tuple[str, Path, bool]] = []
        if self.builtin_root.exists():
            candidates.append((self.BUILTIN_PROJECT_ID, self.builtin_root, True))
        if self.projects_root.exists():
            for manifest_path in self.projects_root.glob("*/manifest.json"):
                candidates.append((manifest_path.parent.name, manifest_path.parent, False))
            # Import packages stored in the earlier user-scoped filesystem layout.
            for manifest_path in self.projects_root.glob("*/*/manifest.json"):
                candidates.append((manifest_path.parent.name, manifest_path.parent, False))
        with get_session() as session:
            known = {
                value
                for (value,) in session.query(GroundwaterProject.project_id)
                .filter(GroundwaterProject.project_id.in_([item[0] for item in candidates]))
                .all()
            }
        for project_id, root, is_builtin in candidates:
            if project_id in known or not PROJECT_ID_PATTERN.fullmatch(project_id):
                continue
            manifest = json.loads((root / "manifest.json").read_text(encoding="utf-8-sig"))
            self._upsert_catalog(
                project_id,
                root,
                manifest,
                created_by=None,
                is_builtin=is_builtin,
            )

    def _upsert_catalog(
        self,
        project_id: str,
        root: Path,
        manifest: dict[str, Any],
        *,
        created_by: str | None,
        is_builtin: bool = False,
    ) -> None:
        from backend.db.connection import get_session
        from backend.db.models import GroundwaterProject

        project = manifest.get("project") or {}
        with get_session() as session:
            record = session.get(GroundwaterProject, project_id)
            if record is None:
                record = GroundwaterProject(project_id=project_id)
                session.add(record)
            record.name = str(project.get("name") or project_id)
            record.region = str(project.get("region") or "")
            record.storage_path = str(root.resolve())
            record.status = "active"
            record.is_builtin = is_builtin or bool(record.is_builtin)
            record.data_version = str(manifest.get("data_version") or 1)
            record.quality_status = str(manifest.get("quality_status") or "unknown")
            record.manifest_snapshot = manifest
            if record.created_by is None and created_by:
                record.created_by = created_by
            session.commit()

    def _catalog_record(self, project_id: str):
        from backend.db.connection import get_session
        from backend.db.models import GroundwaterProject

        with get_session() as session:
            record = session.get(GroundwaterProject, project_id)
            if record is None or record.status != "active":
                raise FileNotFoundError(f"项目不存在: {project_id}")
            session.expunge(record)
            return record

    def _user_can_access(self, user_id: str, project_id: str) -> bool:
        from backend.db.connection import get_session
        from backend.db.models import GroundwaterProjectUser, User

        with get_session() as session:
            user = session.get(User, user_id)
            if user and str(user.role or "").lower() == "admin":
                return True
            return (
                session.query(GroundwaterProjectUser.id)
                .filter(
                    GroundwaterProjectUser.project_id == project_id,
                    GroundwaterProjectUser.user_id == user_id,
                )
                .first()
                is not None
            )

    def _record_summary(self, record: Any) -> dict[str, Any]:
        manifest = record.manifest_snapshot or {}
        return {
            "project_id": record.project_id,
            "name": record.name,
            "region": record.region or "",
            "data_version": int(record.data_version or 1),
            "quality_status": record.quality_status or "unknown",
            "record_counts": manifest.get("record_counts") or {},
            "read_only": bool(record.is_builtin),
        }

    def _list_filesystem_projects(self, user_id: str) -> list[dict[str, Any]]:
        projects: list[dict[str, Any]] = []
        if self.builtin_root.exists():
            projects.append(self._summary(self.resolve(user_id, self.BUILTIN_PROJECT_ID)))
        user_root = self._user_root(user_id)
        if user_root.exists():
            for child in sorted(user_root.iterdir()):
                if child.is_dir() and (child / "manifest.json").exists():
                    projects.append(self._summary(self._context(user_id, child.name, child)))
        return projects

    def _find_package_root(self, root: Path) -> Path:
        if (root / "manifest.json").exists():
            return root
        candidates = [path.parent for path in root.rglob("manifest.json")]
        if len(candidates) != 1:
            raise ValueError("ZIP 中必须包含唯一的 manifest.json")
        return candidates[0]

    def _context(
        self,
        user_id: str,
        project_id: str,
        root: Path,
        *,
        read_only: bool = False,
    ) -> GroundwaterProjectContext:
        manifest_path = self._safe_child(root, "manifest.json")
        manifest = json.loads(manifest_path.read_text(encoding="utf-8-sig"))
        project = manifest.get("project") or {}
        return GroundwaterProjectContext(
            project_id=project_id,
            user_id=user_id,
            project_name=str(project.get("name") or project_id),
            project_root=root.resolve(),
            manifest=manifest,
            read_only=read_only,
        )

    def _summary(self, context: GroundwaterProjectContext) -> dict[str, Any]:
        return {
            "project_id": context.project_id,
            "name": context.project_name,
            "region": (context.manifest.get("project") or {}).get("region") or "",
            "data_version": context.data_version,
            "quality_status": context.manifest.get("quality_status") or "unknown",
            "record_counts": context.manifest.get("record_counts") or {},
            "read_only": context.read_only,
        }

    def _user_root(self, user_id: str) -> Path:
        safe_user = re.sub(r"[^a-zA-Z0-9_-]", "_", user_id or "anonymous")
        return self.projects_root / safe_user

    def _safe_child(self, root: Path, relative: str) -> Path:
        root_resolved = root.resolve()
        path = (root_resolved / relative).resolve()
        if root_resolved not in path.parents and path != root_resolved:
            raise ValueError("项目资源路径越界")
        return path

    def _validate_project_id(self, project_id: str) -> None:
        if not PROJECT_ID_PATTERN.fullmatch(project_id or ""):
            raise ValueError("project_id 只能包含字母、数字、下划线和短横线")

    def _write_json(self, path: Path, value: Any) -> None:
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text(
            json.dumps(value, ensure_ascii=False, indent=2),
            encoding="utf-8",
        )

    def _build_skill(self, project_id: str, name: str, region: str) -> str:
        return f"""---
name: groundwater-project-{project_id}
description: {name}项目数据与背景信息
---

你正在分析项目：{name}。

- 项目编号：`{project_id}`
- 项目区域：{region or "待补充"}
- 数据清单：`manifest.json`
- 项目背景：`docs/background.md`

## 强制规则

1. 项目事实必须通过地下水项目工具读取。
2. 规范要求必须检索绑定的地下水标准知识库。
3. 坐标系未确认时禁止跨坐标组距离计算和空间插值。
4. 预测必须说明证据、假设、不确定性和数据版本。
"""
