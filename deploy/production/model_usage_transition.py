#!/usr/bin/env python3
"""Prepare and safely activate the production LLM usage boundaries.

The preparation phase clones the existing chat rows inside MySQL so the new
code can be tested without changing what the old production code sees.  The
activation phase only disables the two legacy chat rows after the new release
is healthy.  Its private journal contains no API keys and can restore those
flags if the release must be rolled back.
"""

from __future__ import annotations

import argparse
import asyncio
import json
import os
import sys
import tempfile
from collections.abc import Callable
from pathlib import Path
from typing import Any

ROOT = Path(__file__).resolve().parents[2]
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

from sqlalchemy import or_, select
from sqlalchemy.orm import Session

from backend.db.models.llm_model import LlmModel

_TRANSITION_VERSION = 1
_TARGETS = (
    {
        "name": "qwen3.5-35b",
        "scope": "ingestion",
        "supports_vision": None,
    },
    {
        "name": "qwen3.6-35b",
        "scope": "image_analysis",
        "supports_vision": True,
    },
)
_CLONE_EXCLUDED_COLUMNS = frozenset(
    {"id", "usage_scope", "created_at", "updated_at"}
)

SessionFactory = Callable[[], Session]


class ModelUsageTransitionError(RuntimeError):
    """The model catalog cannot be moved to the requested safe phase."""


def _runtime_session_factory() -> SessionFactory:
    from backend.db.connection import get_session

    return get_session


def _atomic_json(path: Path, payload: dict[str, Any]) -> None:
    path = path.resolve(strict=False)
    path.parent.mkdir(parents=True, exist_ok=True, mode=0o700)
    os.chmod(path.parent, 0o700)
    descriptor, temporary_name = tempfile.mkstemp(
        prefix=f".{path.name}.", suffix=".tmp", dir=path.parent
    )
    temporary = Path(temporary_name)
    try:
        with os.fdopen(descriptor, "w", encoding="utf-8") as output:
            json.dump(payload, output, ensure_ascii=True, indent=2)
            output.write("\n")
        os.chmod(temporary, 0o600)
        temporary.replace(path)
        os.chmod(path, 0o600)
    finally:
        if temporary.exists():
            temporary.unlink()


def _clone_values(source: LlmModel, *, scope: str) -> dict[str, Any]:
    values = {
        column.name: getattr(source, column.name)
        for column in LlmModel.__table__.columns
        if column.name not in _CLONE_EXCLUDED_COLUMNS
    }
    values.update(
        usage_scope=scope,
        is_active=True,
        is_default=True,
        created_by="model-usage-transition",
        updated_by="model-usage-transition",
    )
    return values


def _catalog_rows(session: Session, *, lock: bool) -> list[LlmModel]:
    names = [str(target["name"]) for target in _TARGETS]
    scopes = [str(target["scope"]) for target in _TARGETS]
    statement = select(LlmModel).where(
        or_(LlmModel.name.in_(names), LlmModel.usage_scope.in_(scopes))
    )
    if lock:
        statement = statement.with_for_update()
    return list(
        session.scalars(statement).all()
    )


def _index_rows(rows: list[LlmModel]) -> dict[tuple[str, str], LlmModel]:
    return {(str(row.name), str(row.usage_scope)): row for row in rows}


def _validate_prepared(index: dict[tuple[str, str], LlmModel]) -> None:
    for target in _TARGETS:
        name = str(target["name"])
        scope = str(target["scope"])
        row = index.get((name, scope))
        if row is None:
            raise ModelUsageTransitionError(
                f"缺少 {name} 的 {scope} 用途配置"
            )
        if not row.is_active or not row.is_default:
            raise ModelUsageTransitionError(
                f"{name} 的 {scope} 用途必须启用并设为默认"
            )
        required_vision = target["supports_vision"]
        if required_vision is True and not row.supports_vision:
            raise ModelUsageTransitionError(
                f"{name} 的 {scope} 用途必须启用视觉能力"
            )
        defaults = [
            candidate
            for candidate in index.values()
            if candidate.usage_scope == scope
            and candidate.is_active
            and candidate.is_default
        ]
        if len(defaults) != 1 or defaults[0].id != row.id:
            raise ModelUsageTransitionError(
                f"{scope} 用途必须且只能由 {name} 作为默认模型"
            )


def _validate_runtime_scopes(rows: list[LlmModel]) -> dict[str, LlmModel]:
    """Validate capabilities while allowing administrators to replace models."""

    defaults: dict[str, LlmModel] = {}
    for scope in ("ingestion", "image_analysis"):
        candidates = [
            row
            for row in rows
            if row.usage_scope == scope and row.is_active and row.is_default
        ]
        if len(candidates) != 1:
            raise ModelUsageTransitionError(
                f"{scope} 用途必须且只能配置一个启用的默认模型"
            )
        defaults[scope] = candidates[0]
    if not defaults["image_analysis"].supports_vision:
        raise ModelUsageTransitionError("image_analysis 默认模型必须启用视觉能力")
    return defaults


def prepare_scoped_models(
    session_factory: SessionFactory | None = None,
) -> dict[str, Any]:
    """Create internal-scope shadows while leaving frontend chat unchanged."""

    session_factory = session_factory or _runtime_session_factory()
    created: list[dict[str, Any]] = []
    session = session_factory()
    try:
        with session.begin():
            rows = _catalog_rows(session, lock=True)
            index = _index_rows(rows)
            for target in _TARGETS:
                name = str(target["name"])
                scope = str(target["scope"])
                scoped = index.get((name, scope))
                if scoped is None:
                    source = index.get((name, "chat"))
                    if source is None:
                        raise ModelUsageTransitionError(
                            f"缺少可克隆的 {name} 前端对话配置"
                        )
                    scoped = LlmModel(**_clone_values(source, scope=scope))
                    required_vision = target["supports_vision"]
                    if required_vision is not None:
                        scoped.supports_vision = bool(required_vision)
                    session.add(scoped)
                    session.flush()
                    index[(name, scope)] = scoped
                    created.append(
                        {"id": int(scoped.id), "name": name, "scope": scope}
                    )
                scoped.is_active = True
                scoped.is_default = True
                required_vision = target["supports_vision"]
                if required_vision is not None:
                    scoped.supports_vision = bool(required_vision)
                for row in rows:
                    if row.usage_scope == scope and row.id != scoped.id:
                        row.is_default = False
            _validate_prepared(index)
    finally:
        session.close()
    return {
        "status": "prepared",
        "transition_version": _TRANSITION_VERSION,
        "created": created,
    }


def verify_scoped_models(
    session_factory: SessionFactory | None = None,
) -> dict[str, Any]:
    """Read-only verification used by the formal release preflight."""

    session_factory = session_factory or _runtime_session_factory()
    session = session_factory()
    try:
        rows = _catalog_rows(session, lock=False)
        defaults = _validate_runtime_scopes(rows)
        internal = [
            {
                "id": int(defaults[scope].id),
                "name": str(defaults[scope].name),
                "scope": scope,
            }
            for scope in ("ingestion", "image_analysis")
        ]
    finally:
        session.rollback()
        session.close()
    return {
        "status": "verified",
        "transition_version": _TRANSITION_VERSION,
        "internal_models": internal,
    }


def activate_frontend_isolation(
    journal_path: Path,
    session_factory: SessionFactory | None = None,
) -> dict[str, Any]:
    """Disable legacy chat rows and record only reversible, non-secret flags."""

    session_factory = session_factory or _runtime_session_factory()
    journal_path = journal_path.resolve(strict=False)
    if journal_path.exists():
        raise ModelUsageTransitionError("激活日志已存在，拒绝覆盖")

    session = session_factory()
    try:
        with session.begin():
            rows = _catalog_rows(session, lock=True)
            index = _index_rows(rows)
            _validate_prepared(index)
            originals: list[dict[str, Any]] = []
            for target in _TARGETS:
                name = str(target["name"])
                row = index.get((name, "chat"))
                if row is None:
                    raise ModelUsageTransitionError(
                        f"缺少可回滚的 {name} 前端对话配置"
                    )
                originals.append(
                    {
                        "id": int(row.id),
                        "name": name,
                        "usage_scope": "chat",
                        "is_active": bool(row.is_active),
                        "is_default": bool(row.is_default),
                    }
                )
                row.is_active = False
                row.is_default = False
                row.updated_by = "model-usage-transition"

            journal = {
                "format_version": 1,
                "transition_version": _TRANSITION_VERSION,
                "status": "activated",
                "originals": originals,
            }
            _atomic_json(journal_path, journal)
    finally:
        session.close()
    return {
        "status": "activated",
        "transition_version": _TRANSITION_VERSION,
        "journal": str(journal_path),
        "disabled_chat_models": [item["name"] for item in originals],
    }


def rollback_frontend_isolation(
    journal_path: Path,
    session_factory: SessionFactory | None = None,
) -> dict[str, Any]:
    """Restore chat visibility flags recorded by ``activate``."""

    session_factory = session_factory or _runtime_session_factory()
    journal_path = journal_path.resolve(strict=True)
    mode = journal_path.stat().st_mode & 0o777
    if mode & 0o077:
        raise ModelUsageTransitionError("激活日志权限不安全")
    journal = json.loads(journal_path.read_text(encoding="utf-8"))
    if (
        journal.get("format_version") != 1
        or journal.get("transition_version") != _TRANSITION_VERSION
        or journal.get("status") != "activated"
    ):
        raise ModelUsageTransitionError("激活日志格式无效")
    originals = journal.get("originals")
    if not isinstance(originals, list) or len(originals) != len(_TARGETS):
        raise ModelUsageTransitionError("激活日志模型集合无效")

    restored: list[str] = []
    session = session_factory()
    try:
        with session.begin():
            rows = _catalog_rows(session, lock=True)
            by_id = {int(row.id): row for row in rows}
            for item in originals:
                row_id = int(item.get("id"))
                row = by_id.get(row_id)
                if (
                    row is None
                    or str(row.name) != str(item.get("name"))
                    or str(row.usage_scope) != "chat"
                ):
                    raise ModelUsageTransitionError("激活日志与当前模型目录不匹配")
                row.is_active = bool(item.get("is_active"))
                row.is_default = bool(item.get("is_default"))
                row.updated_by = "model-usage-transition-rollback"
                restored.append(str(row.name))
        journal["status"] = "rolled_back"
        _atomic_json(journal_path, journal)
    finally:
        session.close()
    return {
        "status": "rolled_back",
        "transition_version": _TRANSITION_VERSION,
        "restored_chat_models": restored,
    }


async def _notify_catalog_change() -> None:
    from backend.core.models.model_cache import invalidate_cache, notify_model_change

    for target in _TARGETS:
        name = str(target["name"])
        scope = str(target["scope"])
        for selected_scope in ("chat", scope):
            invalidate_cache(name, selected_scope)
            invalidate_cache(None, selected_scope)
            await notify_model_change(name, selected_scope)
            await notify_model_change(None, selected_scope)


def _print_safe(payload: dict[str, Any]) -> None:
    print(json.dumps(payload, ensure_ascii=False, indent=2))


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(description=__doc__)
    commands = parser.add_subparsers(dest="command", required=True)
    commands.add_parser("prepare")
    commands.add_parser("verify")
    activate = commands.add_parser("activate")
    activate.add_argument("--journal", type=Path, required=True)
    rollback = commands.add_parser("rollback")
    rollback.add_argument("--journal", type=Path, required=True)
    return parser


def main() -> int:
    args = build_parser().parse_args()
    if args.command == "prepare":
        payload = prepare_scoped_models()
        asyncio.run(_notify_catalog_change())
    elif args.command == "verify":
        payload = verify_scoped_models()
    elif args.command == "activate":
        payload = activate_frontend_isolation(args.journal)
        asyncio.run(_notify_catalog_change())
    else:
        payload = rollback_frontend_isolation(args.journal)
        asyncio.run(_notify_catalog_change())
    _print_safe(payload)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
