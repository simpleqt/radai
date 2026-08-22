"""Excel 引擎安全域:只读 SQL 校验(掩码字面量/注释)、本地表路径白名单、文件哈希与表名清洗。(拆自 excel_engine.py)"""

from __future__ import annotations

from backend.config.loggers import get_logger

from collections.abc import Sequence
from pathlib import Path
import duckdb
import hashlib
import os
import re

logger = get_logger(__name__)


_MAX_QUERY_ROWS = 5000


_UNSAFE_SQL = re.compile(
    r"\b(attach|create|delete|detach|drop|export|export_database|import|insert|install|load|"
    r"glob|httpfs|parquet_scan|postgres_scan|pragma|"
    r"read_csv(?:_auto)?|read_json(?:_auto)?|read_parquet|read_text|read_blob|"
    r"replace|secret|sqlite_scan|update|"
    r"write_csv|write_parquet|write_text|write_blob|vacuum|checkpoint|set|reset|"
    r"copy_database)\b|"
    r"\bcopy\s+(?:to|from)\b",
    re.IGNORECASE,
)


_SQL_LITERAL_OR_COMMENT = re.compile(
    r"'(?:''|[^'])*'|\"(?:\"\"|[^\"])*\"|--[^\n]*(?:\n|$)|/\*.*?\*/",
    re.DOTALL,
)


def _mask_sql_literals_and_comments(sql: str) -> str:
    """Hide literal/comment text before the deny-list scan.

    A column or a string containing words such as ``copy`` or ``read_csv``
    is harmless.  Masking those regions keeps the security check strict for
    actual DuckDB functions/statements without rejecting valid data queries.
    """
    return _SQL_LITERAL_OR_COMMENT.sub(" ", sql)


_MEMORY_LIMIT_RE = re.compile(r"^\d+(?:\.\d+)?\s*(?:KB|MB|GB|TB)?$", re.IGNORECASE)


def _resource_limit_settings() -> tuple[str, int]:
    """Read the env-configured DuckDB memory/thread caps (never user input)."""

    memory_limit = os.getenv("EXCEL_DUCKDB_MEMORY_LIMIT", "1GB").strip()
    if not _MEMORY_LIMIT_RE.match(memory_limit):
        memory_limit = "1GB"
    try:
        threads = max(1, min(8, int(os.getenv("EXCEL_DUCKDB_THREADS", "2"))))
    except (TypeError, ValueError):
        threads = 2
    return memory_limit, threads


def _new_safe_connection() -> duckdb.DuckDBPyConnection:
    """Create a read-only-in-practice DuckDB connection for user data queries."""
    conn = duckdb.connect()
    # DuckDB's external access is disabled on every generic connection.  The
    # named-table path used by the groundwater tools applies the same setting;
    # keeping it here prevents future providers from accidentally bypassing it.
    conn.execute("SET enable_external_access = false")
    # Model-authored SQL can otherwise build unbounded intermediate results
    # (cartesian joins, huge aggregates); the outer row LIMIT does not cap
    # compute. A memory ceiling and a bounded thread count keep one query from
    # OOMing or pinning every core in the shared Agent API process. The values
    # come from env config, not tool input; memory_limit is format-validated.
    memory_limit, threads = _resource_limit_settings()
    try:
        conn.execute(f"SET memory_limit='{memory_limit}'")
        conn.execute(f"SET threads={threads}")
    except Exception:
        logger.debug("[ExcelEngine] 应用 DuckDB 资源上限失败", exc_info=True)
    return conn


def validate_read_only_sql(sql: str) -> str:
    """Normalize and reject SQL that can access the host or mutate state.

    Excel tools only expose data frames registered by this module.  A simple
    SELECT/WITH allow-list is intentionally preferred over trying to blacklist
    every DuckDB extension function.  The additional keyword deny-list blocks
    file readers, catalog/extension operations and writes that DuckDB accepts
    even when external access is disabled on older versions.
    """
    normalized = str(sql or "").lstrip("\ufeff").strip()
    fenced = re.fullmatch(
        r"```(?:sql|duckdb)?\s*\n?(.*?)\n?```",
        normalized,
        flags=re.IGNORECASE | re.DOTALL,
    )
    if fenced:
        normalized = fenced.group(1).strip()
    normalized = re.sub(
        r"^(?:sql|duckdb\s+sql|查询语句|查询sql)\s*[:：]\s*",
        "",
        normalized,
        count=1,
        flags=re.IGNORECASE,
    ).strip()
    # Remove leading comments but keep the body unchanged for the keyword scan.
    while True:
        without_comment = re.sub(
            r"^(?:--[^\n]*(?:\n|$)|/\*.*?\*/\s*)",
            "",
            normalized,
            count=1,
            flags=re.DOTALL,
        ).strip()
        if without_comment == normalized:
            break
        normalized = without_comment
    if normalized.endswith(";"):
        normalized = normalized[:-1].rstrip()
    if not normalized or len(normalized) > 100_000:
        raise ValueError("SQL 不能为空且不能超过 100000 字符")
    if ";" in normalized or not re.match(r"^(select|with)\b", normalized, re.IGNORECASE):
        raise ValueError("Excel 数据仅允许单条 SELECT 或 WITH 查询")
    if _UNSAFE_SQL.search(_mask_sql_literals_and_comments(normalized)):
        raise ValueError("SQL 包含不允许的文件访问、扩展或写操作")
    return normalized


def _bounded_max_rows(max_rows: int) -> int:
    try:
        normalized = int(max_rows)
    except (TypeError, ValueError) as exc:
        raise ValueError("max_rows 必须是整数") from exc
    if normalized < 0:
        raise ValueError("max_rows 不能为负数")
    return min(normalized, _MAX_QUERY_ROWS)


def _allowed_local_roots() -> list[Path]:
    from backend.config.settings import settings

    project_root = Path(__file__).resolve().parents[4]
    candidates = [
        settings.upload_temp_dir,
        settings.storage_base_path,
        settings.documents_path,
        settings.cache_path,
        project_root / "datasets",
        project_root / "backend" / "datasets",
        project_root / "backend" / "data" / "dataset_cache",
        # Packaged, read-only demo/project data used by the groundwater tools.
        # Keep this explicit instead of widening access to the repository root.
        project_root / "六塘项目数据信息" / "standardized",
    ]
    roots: list[Path] = []
    for candidate in candidates:
        if not candidate:
            continue
        try:
            roots.append(Path(candidate).expanduser().resolve())
        except OSError:
            continue
    return roots


def _resolved_roots(roots: Sequence[str | os.PathLike[str]]) -> list[Path]:
    resolved_roots: list[Path] = []
    for root in roots:
        try:
            resolved_roots.append(Path(root).expanduser().resolve())
        except OSError:
            continue
    return resolved_roots


def is_allowed_local_table_path(
    file_path: str,
    *,
    authorized_roots: Sequence[str | os.PathLike[str]] = (),
) -> bool:
    """Return whether a local table is globally or explicitly authorized.

    ``authorized_roots`` is intentionally caller-scoped.  Project tools first
    resolve user access and constrain manifest paths to the selected project,
    then pass only that project's root here.  This avoids both the stale global
    allow-list bug and widening generic table tools to every user's project.
    """
    try:
        resolved = Path(file_path).expanduser().resolve()
    except OSError:
        return False
    return any(
        _is_relative_to(resolved, root)
        for root in [*_allowed_local_roots(), *_resolved_roots(authorized_roots)]
    )


def _is_relative_to(path: Path, root: Path) -> bool:
    try:
        path.relative_to(root)
        return True
    except ValueError:
        return False


def _file_hash(file_path: str) -> str:
    """计算文件内容哈希（用于缓存键）"""
    h = hashlib.md5()
    try:
        mtime = str(os.path.getmtime(file_path))
        size = str(os.path.getsize(file_path))
        h.update((file_path + mtime + size).encode())
    except OSError:
        h.update(file_path.encode())
    return h.hexdigest()


def _sanitize_table_name(name: str) -> str:
    """将工作表名转换为合法的 DuckDB 表名"""
    safe = re.sub(r"[^\w\u4e00-\u9fff]", "_", name)
    if safe and safe[0].isdigit():
        safe = "t_" + safe
    return safe or "sheet"
