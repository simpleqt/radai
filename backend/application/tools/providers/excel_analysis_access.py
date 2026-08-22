"""Excel 分析访问层:访问上下文、数据集授权与别名匹配、文件路径解析、工具参数清洗。(拆自 excel_data_analysis_tool.py)"""

from __future__ import annotations

from backend.config.loggers import get_logger

from backend.application.tools.providers.excel_engine import is_allowed_local_table_path
from contextvars import ContextVar
from dataclasses import dataclass
from datetime import datetime
from pathlib import Path
from sqlalchemy import or_
from sqlalchemy import select
from sqlalchemy.exc import SQLAlchemyError
from typing import Any
import asyncio
import json
import re

logger = get_logger(__name__)


_TOOL_PARAM_TAG_RE = re.compile(r"</?parameter\b[^>]*(?:>|$)", re.IGNORECASE)


@dataclass(frozen=True)
class _ExcelAccessContext:
    user_id: str = ""
    role: str = "user"
    agent_code: str = ""


_EXCEL_ACCESS_CONTEXT: ContextVar[_ExcelAccessContext | None] = ContextVar(
    "excel_access_context",
    default=None,
)


def _current_access_context() -> _ExcelAccessContext:
    return _EXCEL_ACCESS_CONTEXT.get() or _ExcelAccessContext()


def _clean_tool_string(value: Any) -> str | None:
    """Normalize LLM/tool-call string values that may contain XML tag fragments."""
    if value is None:
        return None
    raw = str(value).strip()
    if not raw:
        return None
    raw = _TOOL_PARAM_TAG_RE.split(raw, maxsplit=1)[0].strip()
    raw = raw.strip().strip("`").strip()
    if len(raw) >= 2 and raw[0] == raw[-1] and raw[0] in {"'", '"'}:
        raw = raw[1:-1].strip()
    return raw or None


def _clean_string_list(value: Any) -> list[str]:
    if value is None:
        return []
    if isinstance(value, (list, tuple, set)):
        return [cleaned for item in value if (cleaned := _clean_tool_string(item))]
    raw = _clean_tool_string(value)
    if not raw:
        return []
    try:
        parsed = json.loads(raw)
    except Exception:
        parsed = None
    if isinstance(parsed, list):
        return [cleaned for item in parsed if (cleaned := _clean_tool_string(item))]
    return [item.strip() for item in raw.split(",") if item.strip()]


def _is_guest_access(access: _ExcelAccessContext) -> bool:
    user_id = str(access.user_id or "").strip()
    return not user_id or user_id == "guest" or user_id.startswith("guest_")


def _agent_aliases(agent_code: str | None) -> set[str]:
    normalized = str(agent_code or "").strip().lower()
    if not normalized:
        return set()
    aliases = {normalized}
    pairs = {
        "ne": "ne_full_feature",
        "csh": "csh_full_feature",
    }
    if normalized in pairs:
        aliases.add(pairs[normalized])
    for short_code, legacy_code in pairs.items():
        if normalized == legacy_code:
            aliases.add(short_code)
    return aliases


def _dataset_matches_agent(dataset, agent_code: str) -> bool:
    bound_codes = {
        str(code or "").strip().lower()
        for code in (getattr(dataset, "agent_codes", None) or [])
        if str(code or "").strip()
    }
    if not bound_codes:
        return True
    aliases = _agent_aliases(agent_code)
    return bool(aliases and aliases.intersection(bound_codes))


async def _dataset_grants(
    session,
    dataset_ids: list[str],
    access: _ExcelAccessContext,
) -> set[str]:
    if not dataset_ids or _is_guest_access(access):
        return set()

    from backend.db.models.dataset import ExperimentDatasetAcl

    subjects = [
        (
            (ExperimentDatasetAcl.subject_type == "user")
            & (ExperimentDatasetAcl.subject_id == access.user_id)
        )
    ]
    role = str(access.role or "user").lower()
    if role:
        subjects.append(
            (ExperimentDatasetAcl.subject_type == "role")
            & (ExperimentDatasetAcl.subject_id == role)
        )
    stmt = select(ExperimentDatasetAcl.dataset_id, ExperimentDatasetAcl.effect).where(
        ExperimentDatasetAcl.dataset_id.in_(dataset_ids),
        or_(*subjects),
        ExperimentDatasetAcl.permission.in_(("read", "use")),
        or_(
            ExperimentDatasetAcl.expires_at.is_(None),
            ExperimentDatasetAcl.expires_at > datetime.utcnow(),
        ),
    )
    try:
        async with session.begin_nested():
            rows = (
                await asyncio.wait_for(session.execute(stmt), timeout=10.0)
            ).all()
    except (SQLAlchemyError, AttributeError) as exc:
        logger.warning("[ExcelTool] 数据集 ACL 查询失败，按无授权处理: %s", exc)
        return set()

    allowed = {
        str(row.dataset_id)
        for row in rows
        if str(getattr(row, "effect", "allow") or "allow").lower() == "allow"
    }
    denied = {
        str(row.dataset_id)
        for row in rows
        if str(getattr(row, "effect", "") or "").lower() == "deny"
    }
    return allowed - denied


async def _filter_accessible_datasets(
    session,
    datasets: list,
    *,
    access: _ExcelAccessContext,
    agent_code: str,
) -> list:
    active = [
        ds
        for ds in datasets
        if str(getattr(ds, "status", "") or "").lower() == "active"
    ]
    if str(access.role or "user").lower() == "admin":
        granted_ids = {str(ds.id) for ds in active}
    else:
        private_ids = [
            str(ds.id)
            for ds in active
            if not bool(getattr(ds, "is_public", False))
            and str(getattr(ds, "created_by", "") or "") != access.user_id
        ]
        granted_ids = await _dataset_grants(session, private_ids, access)

    result = []
    for ds in active:
        allowed = (
            str(access.role or "user").lower() == "admin"
            or bool(getattr(ds, "is_public", False))
            or (
                not _is_guest_access(access)
                and str(getattr(ds, "created_by", "") or "") == access.user_id
            )
            or str(ds.id) in granted_ids
        )
        if allowed and _dataset_matches_agent(ds, agent_code):
            result.append(ds)
    return result


async def _lookup_dataset_path(
    dataset_reference: str | None,
    *,
    match_file_path: bool = False,
) -> tuple[str, str] | None:
    reference = _clean_tool_string(dataset_reference)
    if not reference:
        return None
    try:
        from backend.db.connection import get_async_session_factory
        from backend.db.models.dataset import ExperimentDataset

        access = _current_access_context()
        factory = get_async_session_factory()
        async with factory() as session:
            predicate = (
                or_(
                    ExperimentDataset.code == reference,
                    ExperimentDataset.file_path == reference,
                )
                if match_file_path
                else ExperimentDataset.code == reference
            )
            rows = (
                await asyncio.wait_for(
                    session.execute(select(ExperimentDataset).where(predicate)),
                    timeout=10.0,
                )
            ).scalars().all()
            accessible = await _filter_accessible_datasets(
                session,
                list(rows),
                access=access,
                agent_code=access.agent_code,
            )
        if accessible:
            ds = accessible[0]
            return str(ds.file_path), str(ds.name)
    except Exception as e:
        logger.warning("[ExcelTool] 数据库查询数据集失败: %s", e)
    return None


async def _resolve_file_path(
    dataset_code: str | None,
    file_path: str | None,
    file_id: str | None = None,
) -> tuple[str, str]:
    """解析数据源，返回 (abs_file_path, source_label)

    优先使用 dataset_code，其次 file_id，最后 file_path。
    """
    dataset_code = _clean_tool_string(dataset_code)
    file_id = _clean_tool_string(file_id)
    file_path = _clean_tool_string(file_path)

    if dataset_code:
        dataset = await _lookup_dataset_path(dataset_code)
        if dataset:
            return dataset
        if dataset_code.startswith("uf_") and not file_id:
            file_id = dataset_code
        else:
            return "", f"数据集不存在或无权访问: {dataset_code}"

    if file_id:
        try:
            from backend.services.table_analysis_files import resolve_table_file_source

            access = _current_access_context()
            source, info = await asyncio.to_thread(
                resolve_table_file_source,
                file_id,
                user_id=access.user_id,
                role=access.role,
            )
            return source, info.get("file_name") or f"上传表格: {file_id}"
        except Exception as e:
            logger.warning("[ExcelTool] 解析上传表格 file_id 失败: %s", e)
            return "", "上传表格不存在或无权访问"

    if file_path:
        access = _current_access_context()
        if file_path.startswith("uf_"):
            try:
                from backend.services.table_analysis_files import (
                    resolve_table_file_source,
                )

                source, info = await asyncio.to_thread(
                    resolve_table_file_source,
                    file_path,
                    user_id=access.user_id,
                    role=access.role,
                )
                return source, info.get("file_name") or f"上传表格: {file_path}"
            except Exception as e:
                logger.warning("[ExcelTool] 将 file_path 作为 file_id 解析失败: %s", e)
                return "", "文件不存在或无权访问"

        dataset = await _lookup_dataset_path(file_path, match_file_path=True)
        if dataset:
            return dataset

        try:
            from backend.services.table_analysis_files import (
                resolve_table_file_reference,
            )

            source, info = await asyncio.to_thread(
                resolve_table_file_reference,
                file_path,
                user_id=access.user_id,
                role=access.role,
            )
            return source, info.get("file_name") or "已授权上传表格"
        except Exception:
            logger.warning(
                "[ExcelTool] 授权表格引用解析失败: %s",
                file_path,
                exc_info=True,
            )

        p = Path(file_path).expanduser()
        if p.exists() and _is_builtin_project_table_path(p):
            return str(p.resolve()), p.name
        return "", "文件来源未注册或无权访问"

    return "", "未指定数据源（需提供 dataset_code 或 file_path）"


async def _resolve_multi_file_paths(
    dataset_codes: list[str],
) -> tuple[list[str], list[str], str]:
    """解析多个 dataset_code，返回 (file_paths, prefixes, error_msg)。

    error_msg 非空表示有数据集不存在，此时 file_paths/prefixes 为空列表。
    """
    cleaned_codes = _clean_string_list(dataset_codes)
    file_paths: list[str] = []
    prefixes: list[str] = []
    denied: list[str] = []
    for code in cleaned_codes:
        dataset = await _lookup_dataset_path(code)
        if dataset is None:
            denied.append(code)
            continue
        path, _label = dataset
        file_paths.append(path)
        prefixes.append(code)
    if denied:
        return [], [], f"以下数据集不存在或无权访问: {', '.join(denied)}"
    return file_paths, prefixes, ""


def _is_builtin_project_table_path(path: Path) -> bool:
    """Allow only packaged, read-only project tables as raw legacy paths."""
    try:
        from backend.application.groundwater import GroundwaterProjectService

        resolved = path.resolve()
        root = GroundwaterProjectService().builtin_root.resolve()
        resolved.relative_to(root)
    except (ImportError, OSError, ValueError):
        return False
    return (
        resolved.is_file()
        and resolved.suffix.lower() in {".csv", ".xls", ".xlsx"}
        and is_allowed_local_table_path(str(resolved))
    )


def _error(msg: str) -> str:
    return json.dumps({"error": msg}, ensure_ascii=False)
