"""Materialize an active managed Skill package into the local runtime cache."""

from __future__ import annotations

import json
import re
import shutil
import tempfile
import threading
from pathlib import Path

from backend.application.skills.package_validator import (
    SKILL_KEY_RE,
    SkillPackageValidator,
)
from backend.core.skills.loader import get_managed_skills_root_path


def get_managed_skills_root() -> Path:
    return get_managed_skills_root_path()


class SkillMaterializer:
    def __init__(
        self,
        *,
        root: Path | None = None,
        validator: SkillPackageValidator | None = None,
    ) -> None:
        self.root = root or get_managed_skills_root()
        self.validator = validator or SkillPackageValidator()
        self._pending_backups: dict[str, Path] = {}
        self._lock = threading.RLock()

    def publish(
        self,
        *,
        skill_key: str,
        content: bytes,
        root_prefix: str,
        owner_id: str | None = None,
    ) -> Path:
        if not SKILL_KEY_RE.fullmatch(str(skill_key or "")):
            raise ValueError("非法 Skill key")
        category_root = self.root / "custom"
        category_root.mkdir(parents=True, exist_ok=True)
        target = category_root / skill_key
        with self._lock:
            # A previous failed DB transaction must not leave a hidden backup
            # that can be overwritten by the next publish.
            if skill_key in self._pending_backups:
                self.rollback(skill_key)
            backup = category_root / f".{skill_key}.previous"
            moved_old = False
            try:
                with tempfile.TemporaryDirectory(prefix=f"skill-{skill_key}-", dir=category_root) as tmp:
                    staged = Path(tmp) / skill_key
                    self.validator.extract(content, staged, root_prefix=root_prefix)
                    if not (staged / "SKILL.md").is_file():
                        raise ValueError("Materialized Skill is missing SKILL.md")
                    if owner_id:
                        self._inject_owner_metadata(staged / "SKILL.md", owner_id)
                    if backup.exists():
                        shutil.rmtree(backup)
                    if target.exists():
                        target.rename(backup)
                        moved_old = True
                    staged.rename(target)
                if moved_old:
                    self._pending_backups[skill_key] = backup
            except Exception:
                # Restore the previous active package immediately when staging
                # fails; callers can still invoke rollback for DB failures.
                if target.exists():
                    shutil.rmtree(target)
                if moved_old and backup.exists():
                    backup.rename(target)
                self._pending_backups.pop(skill_key, None)
                raise
        return target

    def finalize(self, skill_key: str) -> None:
        with self._lock:
            backup = self._pending_backups.pop(skill_key, None)
            if backup and backup.exists():
                shutil.rmtree(backup)

    def rollback(self, skill_key: str) -> None:
        with self._lock:
            category_root = self.root / "custom"
            target = category_root / skill_key
            backup = self._pending_backups.pop(skill_key, None)
            if target.exists():
                shutil.rmtree(target)
            if backup and backup.exists():
                backup.rename(target)

    @staticmethod
    def _inject_owner_metadata(path: Path, owner_id: str) -> None:
        content = path.read_text(encoding="utf-8")
        normalized_owner = str(owner_id).strip()
        if not normalized_owner:
            return
        if not content.startswith("---"):
            raise ValueError("SKILL.md 缺少 YAML frontmatter")
        match = re.match(r"^(---\s*\n)(.*?)(\n---\s*\n)", content, re.DOTALL)
        if not match:
            raise ValueError("SKILL.md frontmatter 格式无效")
        frontmatter = match.group(2)
        owner_value = (
            normalized_owner
            if re.fullmatch(r"[A-Za-z0-9_.:@-]{1,128}", normalized_owner)
            else json.dumps(normalized_owner, ensure_ascii=False)
        )
        owner_line = f"owner_id: {owner_value}"
        if re.search(r"^owner_id\s*:", frontmatter, flags=re.MULTILINE):
            frontmatter = re.sub(
                r"^owner_id\s*:.*$",
                owner_line,
                frontmatter,
                count=1,
                flags=re.MULTILINE,
            )
        else:
            frontmatter = f"{frontmatter}\n{owner_line}"
        path.write_text(
            f"{match.group(1)}{frontmatter}{match.group(3)}{content[match.end():]}",
            encoding="utf-8",
        )
