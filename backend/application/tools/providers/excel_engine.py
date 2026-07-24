"""Excel DuckDB 查询引擎

基于 DuckDB 提供高性能 SQL 查询能力：
- 单文件：将 Excel 各工作表加载为 DuckDB 视图（按文件哈希缓存）
- 多文件 JOIN：将多个数据集加载到同一 DuckDB 连接，表名带数据集前缀
- 支持 MinIO 路径（自动下载到本地缓存）
- 线程安全，支持并发查询

多文件表命名规则：
  {dataset_prefix}__{sanitized_sheet_name}
  例：biochar_degradation__Sheet1, hydroxyl_radical_rates__Sheet1
  无冲突时同时注册不带前缀的短名（便于 SQL 书写）
"""

from __future__ import annotations

import hashlib
import os
import re
import threading
import time
from collections import OrderedDict
from collections.abc import Sequence
from contextlib import contextmanager
from pathlib import Path
from typing import Any

import duckdb
import pandas as pd

from backend.config.loggers import get_logger
from backend.services.minio_paths import parse_minio_source

logger = get_logger(__name__)

_cache: "OrderedDict[str, duckdb.DuckDBPyConnection]" = OrderedDict()
_multi_table_maps: dict[str, dict[str, list[str]]] = {}  # cache_key → table_map
_connection_query_locks: dict[int, threading.RLock] = {}
_cache_sources: dict[str, set[str]] = {}
_cache_last_used: dict[str, float] = {}
_cache_lock = threading.RLock()
_MAX_QUERY_ROWS = 5000
_CACHE_MAX_ENTRIES = max(1, int(os.getenv("EXCEL_ENGINE_CACHE_MAX_ENTRIES", "32")))
_CACHE_TTL_SECONDS = max(60.0, float(os.getenv("EXCEL_ENGINE_CACHE_TTL_SECONDS", "900")))

_UNSAFE_SQL = re.compile(
    r"\b(attach|create|delete|detach|drop|export|import|insert|install|load|"
    r"glob|httpfs|parquet_scan|postgres_scan|pragma|"
    r"read_csv(?:_auto)?|read_json(?:_auto)?|read_parquet|read_text|"
    r"replace|secret|sqlite_scan|update|"
    r"write_csv|write_parquet|vacuum|checkpoint|set|reset)\b|"
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


def _new_safe_connection() -> duckdb.DuckDBPyConnection:
    """Create a read-only-in-practice DuckDB connection for user data queries."""
    conn = duckdb.connect()
    # DuckDB's external access is disabled on every generic connection.  The
    # named-table path used by the groundwater tools applies the same setting;
    # keeping it here prevents future providers from accidentally bypassing it.
    conn.execute("SET enable_external_access = false")
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


def _close_cache_entry_locked(cache_key: str) -> bool:
    """Close an idle entry without waiting while the global cache lock is held."""

    conn = _cache.get(cache_key)
    if conn is not None:
        query_lock = _connection_query_locks.get(id(conn))
        if query_lock is not None and not query_lock.acquire(blocking=False):
            return False
        _cache.pop(cache_key, None)
        try:
            conn.close()
        except Exception:
            logger.debug("[ExcelEngine] 关闭缓存连接失败", exc_info=True)
        finally:
            if query_lock is not None:
                query_lock.release()
            _connection_query_locks.pop(id(conn), None)
    _multi_table_maps.pop(cache_key, None)
    _cache_sources.pop(cache_key, None)
    _cache_last_used.pop(cache_key, None)
    return True


def _prune_cache_locked() -> None:
    now = time.monotonic()
    expired = [
        key
        for key, last_used in _cache_last_used.items()
        if now - last_used > _CACHE_TTL_SECONDS
    ]
    for key in expired:
        _close_cache_entry_locked(key)
    while len(_cache) > _CACHE_MAX_ENTRIES:
        for oldest_key in list(_cache):
            if _close_cache_entry_locked(oldest_key):
                break
        else:
            # All entries are leased. A later lookup will retry pruning.
            break


def _remember_cache_entry_locked(cache_key: str, conn, source_paths: set[str]) -> None:
    _cache[cache_key] = conn
    _cache.move_to_end(cache_key)
    _cache_sources[cache_key] = set(source_paths)
    _cache_last_used[cache_key] = time.monotonic()
    _prune_cache_locked()


def _touch_cache_entry_locked(cache_key: str) -> None:
    if cache_key in _cache:
        _cache.move_to_end(cache_key)
        _cache_last_used[cache_key] = time.monotonic()


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


def _clean_dataframe(df: pd.DataFrame) -> pd.DataFrame:
    """清洗 DataFrame：处理 Excel 中常见的数据质量问题。

    主要处理：
    1. 非标准空白字符（\xa0 不间断空格、\u3000 全角空格等）
       → 统一替换为普通空格后 strip()
    2. 空字符串统一转为 NaN
    3. 字符串列数值推断（无损模式）：
       - 仅当全部非空值都能转为数值时才转换整列
       - 混合文本/数值列保持原值，避免静默把合法文本改成 NaN
    """
    _WHITESPACE = str.maketrans({
        "\xa0": " ",   # non-breaking space（最常见原因）
        "\u3000": " ", # 全角空格
        "\u200b": "",  # zero-width space
        "\u200c": "",  # zero-width non-joiner
        "\u200d": "",  # zero-width joiner
        "\ufeff": "",  # BOM
        "\t": " ",     # 制表符
    })

    for col in df.columns:
        if df[col].dtype != object:
            continue

        # Step 1: 清洗非标准空白
        cleaned = df[col].apply(
            lambda x: x.translate(_WHITESPACE).strip() if isinstance(x, str) else x
        )

        # Step 2: 空字符串 → NaN
        cleaned = cleaned.replace("", pd.NA)

        # Step 3: 尝试数值推断
        try:
            numeric = pd.to_numeric(cleaned, errors="coerce")
            non_null_mask = cleaned.notna()
            if not non_null_mask.any():
                df[col] = cleaned
                continue

            all_numeric = bool(numeric[non_null_mask].notna().all())

            if all_numeric:
                df[col] = numeric
            else:
                df[col] = cleaned
        except Exception:
            df[col] = cleaned

    return df


def _load_file_to_duckdb(file_path: str) -> duckdb.DuckDBPyConnection:
    """将 Excel/CSV 文件加载到新的 DuckDB 内存连接"""
    conn = _new_safe_connection()
    ext = os.path.splitext(file_path)[1].lower()

    if ext == ".csv":
        df = pd.read_csv(file_path)
        df.columns = [str(c).strip() for c in df.columns]
        df = _clean_dataframe(df)
        conn.register("data", df)
        logger.info("[ExcelEngine] CSV 已加载: %s, rows=%d", file_path, len(df))
    else:
        # Excel：逐 sheet 加载
        try:
            sheets = pd.read_excel(file_path, sheet_name=None)
        except Exception as e:
            raise ValueError(f"无法读取 Excel 文件 {file_path}: {e}") from e

        for sheet_name, df in sheets.items():
            table_name = _sanitize_table_name(sheet_name)
            # 标准化列名：去除首尾空格和非标准空白
            df.columns = [str(c).strip().replace("\xa0", " ") for c in df.columns]
            # 清洗数据值
            df = _clean_dataframe(df)
            conn.register(table_name, df)
            # 同时注册原始名（如果不同）
            if table_name != sheet_name:
                try:
                    conn.register(sheet_name, df)
                except Exception:
                    pass
            logger.info(
                "[ExcelEngine] Sheet 已加载: '%s' -> table '%s', rows=%d, cols=%d",
                sheet_name, table_name, len(df), len(df.columns),
            )

    return conn


def _resolve_local_path(
    file_path: str,
    *,
    authorized_roots: Sequence[str | os.PathLike[str]] = (),
) -> str:
    """将 MinIO object_name 下载到本地缓存目录，返回本地路径；本地路径直接返回"""
    # 已是本地文件
    if Path(file_path).exists():
        if not is_allowed_local_table_path(
            file_path,
            authorized_roots=authorized_roots,
        ):
            raise PermissionError("本地表格路径不在允许的上传/数据集目录内")
        return str(Path(file_path).expanduser().resolve())

    # 视为 MinIO object_name，下载到本地缓存
    try:
        from backend.storage import get_user_minio_bucket_name

        bucket_from_source, object_name = parse_minio_source(file_path)
        if not object_name:
            raise FileNotFoundError("MinIO object_name 为空")

        cache_dir = Path(__file__).resolve().parents[2] / "data" / "dataset_cache"
        cache_dir.mkdir(parents=True, exist_ok=True)
        local_fname = Path(object_name).name or "table_file"
        cache_key = hashlib.md5(f"{bucket_from_source or ''}/{object_name}".encode()).hexdigest()[:12]
        local_path = cache_dir / f"{cache_key}_{local_fname}"

        if local_path.exists():
            return str(local_path)

        from backend.storage import _get_minio_client

        client = _get_minio_client()
        bucket = bucket_from_source or get_user_minio_bucket_name()
        client.fget_object(bucket, object_name, str(local_path))
        logger.info("[ExcelEngine] MinIO 文件已下载到本地缓存: bucket=%s, object=%s, local=%s", bucket, object_name, local_path)
        return str(local_path)
    except Exception as e:
        logger.warning("[ExcelEngine] MinIO 下载失败: path=%s, error=%s", file_path, e)
        raise FileNotFoundError(f"文件不存在且 MinIO 下载失败: {file_path}") from e


def get_connection(file_path: str) -> duckdb.DuckDBPyConnection:
    """获取（或创建）文件对应的 DuckDB 连接（线程安全、哈希缓存）
    
    file_path 可以是：
    - 本地绝对路径
    - MinIO object_name（自动下载到本地缓存再加载）
    """
    local_path = _resolve_local_path(file_path)
    cache_key = _file_hash(local_path)
    with _cache_lock:
        _prune_cache_locked()
        if cache_key not in _cache:
            logger.info("[ExcelEngine] 创建新 DuckDB 连接: %s", local_path)
            _remember_cache_entry_locked(
                cache_key,
                _load_file_to_duckdb(local_path),
                {local_path},
            )
        else:
            _touch_cache_entry_locked(cache_key)
        return _cache[cache_key]


# ── 多文件联合连接 ────────────────────────────────────────────────────

def _multi_cache_key(file_paths: list[str], prefixes: list[str]) -> str:
    """为多文件联合连接生成唯一缓存键"""
    parts = [_file_hash(fp) + ":" + pfx for fp, pfx in zip(file_paths, prefixes)]
    combined = "|".join(sorted(parts))
    return "multi_" + hashlib.md5(combined.encode()).hexdigest()


def _load_multi_files_to_duckdb(
    file_paths: list[str],
    prefixes: list[str],
) -> tuple[duckdb.DuckDBPyConnection, dict[str, list[str]]]:
    """将多个 Excel/CSV 文件加载到同一 DuckDB 连接。

    表命名策略：
    1. 每个 sheet 必注册带前缀名：{prefix}__{safe_sheet_name}
    2. 若同名 sheet 只出现在一个文件中，额外注册不带前缀的短名（方便 SQL）

    Returns:
        (conn, table_map)  其中 table_map = {prefix: [table_names_in_this_file]}
    """
    conn = _new_safe_connection()
    # 收集所有 sheet 名（用于冲突检测）
    all_sheet_names: list[tuple[str, str, str, pd.DataFrame]] = []  # (prefix, sheet_name, safe_name, df)

    for file_path, prefix in zip(file_paths, prefixes):
        ext = os.path.splitext(file_path)[1].lower()
        if ext == ".csv":
            df = pd.read_csv(file_path)
            df.columns = [str(c).strip().replace("\xa0", " ") for c in df.columns]
            df = _clean_dataframe(df)
            all_sheet_names.append((prefix, "data", _sanitize_table_name("data"), df))
        else:
            try:
                sheets = pd.read_excel(file_path, sheet_name=None)
            except Exception as e:
                raise ValueError(f"无法读取 Excel 文件 {file_path}: {e}") from e
            for sheet_name, df in sheets.items():
                df.columns = [str(c).strip().replace("\xa0", " ") for c in df.columns]
                df = _clean_dataframe(df)
                all_sheet_names.append((prefix, sheet_name, _sanitize_table_name(sheet_name), df))

    # 统计 safe_name 出现次数（跨文件冲突检测）
    from collections import Counter
    name_count = Counter(safe for _, _, safe, _ in all_sheet_names)

    table_map: dict[str, list[str]] = {p: [] for p in prefixes}

    for prefix, sheet_name, safe_name, df in all_sheet_names:
        # 带前缀的完整表名（必定唯一）
        full_name = f"{prefix}__{safe_name}"
        conn.register(full_name, df)
        table_map[prefix].append(full_name)

        # 若短名无冲突，额外注册不带前缀的别名
        if name_count[safe_name] == 1:
            try:
                conn.register(safe_name, df)
                # sheet 名与 safe_name 不同时也注册原始名
                if safe_name != sheet_name:
                    conn.register(sheet_name, df)
            except Exception:
                pass

        logger.info(
            "[ExcelEngine] Multi-Sheet 已加载: '%s' -> '%s' (alias: %s), rows=%d, cols=%d",
            sheet_name, full_name,
            safe_name if name_count[safe_name] == 1 else "无短名（冲突）",
            len(df), len(df.columns),
        )

    return conn, table_map


def get_multi_connection(
    file_paths: list[str],
    prefixes: list[str],
) -> tuple[duckdb.DuckDBPyConnection, dict[str, list[str]]]:
    """获取多文件联合 DuckDB 连接（线程安全、哈希缓存）。

    Args:
        file_paths: 本地文件路径列表（按顺序与 prefixes 对应）
        prefixes:   每个文件的表名前缀（通常为 dataset_code），必须唯一

    Returns:
        (conn, table_map)  其中 table_map = {prefix: [full_table_names]}
    """
    if len(file_paths) != len(prefixes):
        raise ValueError("file_paths 和 prefixes 长度必须一致")
    if len(set(prefixes)) != len(prefixes):
        raise ValueError("prefixes 中存在重复值，每个数据集前缀必须唯一")

    local_paths = [_resolve_local_path(fp) for fp in file_paths]
    cache_key = _multi_cache_key(local_paths, prefixes)

    with _cache_lock:
        _prune_cache_locked()
        if cache_key not in _cache:
            logger.info("[ExcelEngine] 创建多文件 DuckDB 连接: prefixes=%s", prefixes)
            conn, table_map = _load_multi_files_to_duckdb(local_paths, prefixes)
            _remember_cache_entry_locked(cache_key, conn, set(local_paths))
            _multi_table_maps[cache_key] = table_map
        else:
            _touch_cache_entry_locked(cache_key)
        conn = _cache[cache_key]
        table_map = _multi_table_maps.get(cache_key, {})
    return conn, table_map


def execute_multi_sql(
    file_paths: list[str],
    prefixes: list[str],
    sql: str,
    max_rows: int = _MAX_QUERY_ROWS,
) -> pd.DataFrame:
    """在多文件联合连接上执行 SQL，返回 DataFrame。

    表名使用 {prefix}__{sheet_name} 格式，无冲突时也可用短名。
    """
    normalized_sql = validate_read_only_sql(sql)
    bounded_rows = _bounded_max_rows(max_rows)
    try:
        with _leased_connection(
            lambda: get_multi_connection(file_paths, prefixes)
        ) as loaded:
            conn, _ = loaded
            result, truncated = _fetch_bounded_query(
                conn,
                normalized_sql,
                bounded_rows,
            )
        if truncated:
            logger.warning("[ExcelEngine] 多文件查询结果已截断至 %d 行", bounded_rows)
        return result
    except duckdb.Error as e:
        raise ValueError(f"SQL 执行错误: {e}") from e


def _named_tables_cache_key(table_paths: dict[str, str]) -> str:
    parts = [f"{name}:{_file_hash(path)}" for name, path in sorted(table_paths.items())]
    return "named_" + hashlib.md5("|".join(parts).encode()).hexdigest()


def get_named_tables_connection(
    table_paths: dict[str, str],
    *,
    authorized_roots: Sequence[str | os.PathLike[str]] = (),
) -> duckdb.DuckDBPyConnection:
    """Load authorized CSV/Excel files as explicitly named DuckDB tables."""
    if not table_paths:
        raise ValueError("没有可查询的表格")

    local_paths = {
        name: _resolve_local_path(path, authorized_roots=authorized_roots)
        for name, path in table_paths.items()
    }
    cache_key = _named_tables_cache_key(local_paths)
    with _cache_lock:
        _prune_cache_locked()
        if cache_key in _cache:
            _touch_cache_entry_locked(cache_key)
            return _cache[cache_key]

        conn = _new_safe_connection()
        for raw_name, file_path in local_paths.items():
            table_name = _sanitize_table_name(raw_name)
            ext = os.path.splitext(file_path)[1].lower()
            if ext == ".csv":
                df = pd.read_csv(file_path)
                df.columns = [str(c).strip().replace("\xa0", " ") for c in df.columns]
                df = _clean_dataframe(df)
                conn.register(table_name, df)
            else:
                sheets = pd.read_excel(file_path, sheet_name=None)
                if len(sheets) != 1:
                    raise ValueError(
                        f"命名表 {raw_name} 必须对应 CSV 或单工作表 Excel"
                    )
                df = next(iter(sheets.values()))
                df.columns = [str(c).strip().replace("\xa0", " ") for c in df.columns]
                conn.register(table_name, _clean_dataframe(df))
            logger.info(
                "[ExcelEngine] 命名表已加载: %s -> %s, rows=%d",
                file_path,
                table_name,
                len(df),
            )
        _remember_cache_entry_locked(cache_key, conn, set(local_paths.values()))
        _connection_query_locks[id(conn)] = threading.RLock()
        return conn


@contextmanager
def _leased_connection(loader):
    """Pin a cached connection from lookup through the end of its query."""

    while True:
        with _cache_lock:
            loaded = loader()
            conn = loaded[0] if isinstance(loaded, tuple) else loaded
            query_lock = _connection_query_locks.setdefault(
                id(conn),
                threading.RLock(),
            )
            if query_lock.acquire(blocking=False):
                break
        query_lock.acquire()
        with _cache_lock:
            if any(cached is conn for cached in _cache.values()):
                break
        query_lock.release()
    try:
        yield loaded
    finally:
        query_lock.release()


def _bounded_query_sql(sql: str, max_rows: int) -> str:
    fetch_rows = max_rows + 1
    return (
        'SELECT * FROM ('
        + sql
        + f') AS "__agent_bounded_query" LIMIT {fetch_rows}'
    )


def _fetch_bounded_query(
    conn: duckdb.DuckDBPyConnection,
    sql: str,
    max_rows: int,
) -> tuple[pd.DataFrame, bool]:
    result = conn.execute(_bounded_query_sql(sql, max_rows)).fetchdf()
    truncated = len(result) > max_rows
    return (result.head(max_rows) if truncated else result), truncated


def _quote_identifier(value: str) -> str:
    return '"' + str(value).replace('"', '""') + '"'


def _table_schema_details(
    conn: duckdb.DuckDBPyConnection,
    table_name: str,
) -> list[dict[str, Any]]:
    """Read column metadata, counts, and samples with O(1) queries per table."""

    quoted_table = _quote_identifier(table_name)
    columns = conn.execute(f"DESCRIBE {quoted_table}").fetchdf()
    if columns.empty:
        return []

    column_names = [str(value) for value in columns["column_name"].tolist()]
    count_parts = ["COUNT(*) AS __total"]
    count_parts.extend(
        f"COUNT({_quote_identifier(name)}) AS {_quote_identifier(f'__non_null_{idx}')}"
        for idx, name in enumerate(column_names)
    )
    counts = conn.execute(
        f"SELECT {', '.join(count_parts)} FROM {quoted_table}"
    ).fetchone()
    samples = conn.execute(f"SELECT * FROM {quoted_table} LIMIT 100").fetchdf()
    total = int(counts[0])

    details: list[dict[str, Any]] = []
    for idx, (_, row) in enumerate(columns.iterrows()):
        column_name = column_names[idx]
        sample_values = (
            [str(value) for value in samples[column_name].dropna().head(3).tolist()]
            if column_name in samples
            else []
        )
        details.append(
            {
                "name": column_name,
                "dtype": str(row["column_type"]),
                "non_null": int(counts[idx + 1]),
                "total": total,
                "sample": sample_values,
            }
        )
    return details


def get_named_tables_schema(
    table_paths: dict[str, str],
    *,
    authorized_roots: Sequence[str | os.PathLike[str]] = (),
) -> dict[str, list[dict[str, Any]]]:
    result: dict[str, list[dict[str, Any]]] = {}
    with _leased_connection(
        lambda: get_named_tables_connection(
            table_paths,
            authorized_roots=authorized_roots,
        )
    ) as conn:
        for raw_name in table_paths:
            table_name = _sanitize_table_name(raw_name)
            columns = conn.execute(f'DESCRIBE "{table_name}"').fetchdf()
            total = int(
                conn.execute(f'SELECT COUNT(*) AS n FROM "{table_name}"').fetchone()[0]
            )
            result[table_name] = [
                {
                    "name": str(row["column_name"]),
                    "dtype": str(row["column_type"]),
                    "total": total,
                }
                for _, row in columns.iterrows()
            ]
    return result


def execute_named_tables_sql(
    table_paths: dict[str, str],
    sql: str,
    max_rows: int = _MAX_QUERY_ROWS,
    *,
    authorized_roots: Sequence[str | os.PathLike[str]] = (),
) -> pd.DataFrame:
    """Execute SQL against a caller-authorized mapping of logical table names."""
    normalized_sql = validate_read_only_sql(sql)
    bounded_rows = _bounded_max_rows(max_rows)
    try:
        with _leased_connection(
            lambda: get_named_tables_connection(
                table_paths,
                authorized_roots=authorized_roots,
            )
        ) as conn:
            result, truncated = _fetch_bounded_query(
                conn,
                normalized_sql,
                bounded_rows,
            )
        if truncated:
            logger.warning(
                "[ExcelEngine] 命名表查询结果已截断至 %d 行",
                bounded_rows,
            )
        return result
    except duckdb.Error as e:
        raise ValueError(f"SQL 执行错误: {e}") from e


def get_multi_schema(
    file_paths: list[str],
    prefixes: list[str],
) -> dict[str, dict[str, list[dict]]]:
    """获取多文件联合连接的 schema。

    Returns:
        {prefix: {table_name: [{name, dtype, non_null, total, sample}]}}
    """
    result: dict[str, dict[str, list[dict]]] = {}

    with _leased_connection(
        lambda: get_multi_connection(file_paths, prefixes)
    ) as loaded:
        conn, table_map = loaded
        for prefix, table_names in table_map.items():
            result[prefix] = {}
            for table_name in table_names:
                try:
                    result[prefix][table_name] = _table_schema_details(
                        conn,
                        table_name,
                    )
                except Exception as e:
                    logger.warning("[ExcelEngine] 多文件 schema 获取失败: table=%s, error=%s", table_name, e)

    return result


def multi_schema_to_text(multi_schema: dict[str, dict[str, list[dict]]]) -> str:
    """将多文件 schema 格式化为可读文本，并生成 JOIN 提示"""
    lines = []
    for prefix, tables in multi_schema.items():
        lines.append(f"\n### 数据集：`{prefix}`")
        for table_name, cols in tables.items():
            total = cols[0]["total"] if cols else 0
            lines.append(f"\n**表名**: `{table_name}`（共 {total} 行）")
            lines.append("| 列名 | 数据类型 | 非空数 | 样本值 |")
            lines.append("|------|---------|-------|--------|")
            for col in cols:
                sample_str = ", ".join(col["sample"]) if col["sample"] else "-"
                lines.append(f"| {col['name']} | {col['dtype']} | {col['non_null']}/{col['total']} | {sample_str} |")

    # 自动生成 JOIN 提示
    all_prefixes = list(multi_schema.keys())
    if len(all_prefixes) >= 2:
        lines.append("\n---\n**JOIN 查询提示**：")
        lines.append("- 表名格式：`{数据集编码}__{sheet名}`，如 `biochar_degradation__Sheet1`")
        lines.append("- 无冲突的 sheet 名也可直接使用短名")
        lines.append("- JOIN 示例（按污染物名称关联两个数据集）：")
        p1, p2 = all_prefixes[0], all_prefixes[1]
        t1 = next(iter(multi_schema[p1]))
        t2 = next(iter(multi_schema[p2]))
        lines.append(f'  ```sql\n  SELECT a.*, b.*\n  FROM "{t1}" a\n  JOIN "{t2}" b ON a."污染物名称" = b."name"\n  LIMIT 20\n  ```')

    return "\n".join(lines)


def invalidate_cache(file_path: str) -> None:
    """清除指定文件的缓存（文件更新后调用）"""
    try:
        local_path = _resolve_local_path(file_path)
    except Exception:
        local_path = str(Path(file_path).expanduser().resolve())
    with _cache_lock:
        matching = [
            key for key, paths in _cache_sources.items()
            if local_path in paths
        ]
        closed = 0
        pending = 0
        for cache_key in matching:
            if _close_cache_entry_locked(cache_key):
                closed += 1
            else:
                # Make the busy entry immediately eligible for pruning after
                # its current lease ends.
                _cache_last_used[cache_key] = 0.0
                pending += 1
        if matching:
            logger.info(
                "[ExcelEngine] 缓存失效: %s closed=%d pending=%d",
                file_path,
                closed,
                pending,
            )


def execute_sql(file_path: str, sql: str, max_rows: int = _MAX_QUERY_ROWS) -> pd.DataFrame:
    """执行 SQL 查询，返回 DataFrame

    Args:
        file_path: Excel/CSV 文件路径
        sql: SQL 查询语句（表名为工作表名或其 sanitized 版本）
        max_rows: 最大返回行数限制
    """
    normalized_sql = validate_read_only_sql(sql)
    bounded_rows = _bounded_max_rows(max_rows)
    try:
        with _leased_connection(lambda: get_connection(file_path)) as conn:
            result, truncated = _fetch_bounded_query(
                conn,
                normalized_sql,
                bounded_rows,
            )
        if truncated:
            logger.warning(
                "[ExcelEngine] 查询结果已截断至 %d 行",
                bounded_rows,
            )
        return result
    except duckdb.Error as e:
        raise ValueError(f"SQL 执行错误: {e}") from e


def get_schema(file_path: str) -> dict[str, list[dict]]:
    """获取文件的表结构

    Returns:
        {table_name: [{name, dtype, non_null_count, total_count, sample_values}]}
    """
    schema: dict[str, list[dict]] = {}
    with _leased_connection(lambda: get_connection(file_path)) as conn:
        # 列出所有已注册的视图
        tables_df = conn.execute("SHOW TABLES").fetchdf()

        for table_name in tables_df["name"].tolist():
            try:
                schema[table_name] = _table_schema_details(conn, table_name)
            except Exception as e:
                logger.warning("[ExcelEngine] 获取表结构失败: table=%s, error=%s", table_name, e)

    return schema


def get_summary(file_path: str, table_name: str) -> pd.DataFrame:
    """对指定表生成统计摘要（均值、标准差、分位数等）"""
    try:
        with _leased_connection(lambda: get_connection(file_path)) as conn:
            return conn.execute(
                f"SUMMARIZE SELECT * FROM {_quote_identifier(table_name)}"
            ).fetchdf()
    except Exception as e:
        raise ValueError(f"统计摘要生成失败: {e}") from e


_NUMERIC_DUCKDB_TYPE = re.compile(
    r"^(?:TINYINT|SMALLINT|INTEGER|BIGINT|HUGEINT|UTINYINT|USMALLINT|"
    r"UINTEGER|UBIGINT|UHUGEINT|FLOAT|REAL|DOUBLE|DECIMAL)(?:\(|$)",
    re.IGNORECASE,
)
_MAX_CORRELATION_COLUMNS = 20


def get_table_statistics(
    file_path: str,
    table_name: str,
    columns: list[str] | None = None,
) -> dict[str, Any]:
    """Compute bounded-output statistics over the complete table in DuckDB."""

    try:
        with _leased_connection(lambda: get_connection(file_path)) as conn:
            tables = set(conn.execute("SHOW TABLES").fetchdf()["name"].astype(str))
            if table_name not in tables:
                raise ValueError(f"工作表不存在: {table_name}")

            quoted_table = _quote_identifier(table_name)
            described = conn.execute(f"DESCRIBE {quoted_table}").fetchdf()
            available = described["column_name"].astype(str).tolist()
            requested = list(dict.fromkeys(str(item) for item in (columns or [])))
            unknown = [item for item in requested if item not in available]
            if unknown:
                raise ValueError(
                    f"列不存在: {', '.join(unknown)}；可用列: {', '.join(available)}"
                )
            selected = requested or available
            if not selected:
                raise ValueError("工作表没有可分析的列")

            projection = ", ".join(_quote_identifier(item) for item in selected)
            summary = conn.execute(
                f"SUMMARIZE SELECT {projection} FROM {quoted_table}"
            ).fetchdf()

            null_expressions = ["COUNT(*) AS row_count"] + [
                "COUNT(*) FILTER (WHERE "
                + _quote_identifier(column)
                + f" IS NULL) AS null_{index}"
                for index, column in enumerate(selected)
            ]
            null_row = conn.execute(
                f"SELECT {', '.join(null_expressions)} FROM {quoted_table}"
            ).fetchone()
            row_count = int(null_row[0]) if null_row else 0
            null_counts = {
                column: int(null_row[index + 1])
                for index, column in enumerate(selected)
            }
            summary["null_count"] = summary["column_name"].map(null_counts).fillna(0).astype(int)

            types_by_column = {
                str(row["column_name"]): str(row["column_type"])
                for _, row in described.iterrows()
            }
            numeric_columns = [
                column
                for column in selected
                if _NUMERIC_DUCKDB_TYPE.match(types_by_column.get(column, ""))
            ]
            correlation_columns = numeric_columns[:_MAX_CORRELATION_COLUMNS]
            correlation = pd.DataFrame()
            if len(correlation_columns) >= 2:
                expressions: list[str] = []
                pairs: list[tuple[int, int]] = []
                for left_index, left in enumerate(correlation_columns):
                    for right_index in range(left_index, len(correlation_columns)):
                        right = correlation_columns[right_index]
                        alias = f"corr_{left_index}_{right_index}"
                        expressions.append(
                            "corr(CAST("
                            + _quote_identifier(left)
                            + " AS DOUBLE), CAST("
                            + _quote_identifier(right)
                            + f" AS DOUBLE)) AS {_quote_identifier(alias)}"
                        )
                        pairs.append((left_index, right_index))
                values = conn.execute(
                    f"SELECT {', '.join(expressions)} FROM {quoted_table}"
                ).fetchone()
                correlation = pd.DataFrame(
                    float("nan"),
                    index=correlation_columns,
                    columns=correlation_columns,
                )
                for (left_index, right_index), value in zip(
                    pairs,
                    values or [],
                    strict=False,
                ):
                    correlation.iat[left_index, right_index] = value
                    correlation.iat[right_index, left_index] = value

            return {
                "row_count": row_count,
                "column_count": len(selected),
                "summary": summary,
                "correlation": correlation,
                "correlation_omitted_columns": numeric_columns[
                    _MAX_CORRELATION_COLUMNS:
                ],
            }
    except ValueError:
        raise
    except Exception as exc:
        raise ValueError(f"统计摘要生成失败: {exc}") from exc


def df_to_markdown(df: pd.DataFrame, max_rows: int = 50) -> str:
    """将 DataFrame 转换为 Markdown 表格字符串"""
    if df.empty:
        return "_查询结果为空_"
    if len(df) > max_rows:
        head = df.head(max_rows)
        suffix = f"\n\n_（共 {len(df)} 行，仅显示前 {max_rows} 行）_"
    else:
        head = df
        suffix = f"\n\n_（共 {len(df)} 行）_"
    return head.to_markdown(index=False) + suffix


def schema_to_text(schema: dict[str, list[dict]]) -> str:
    """将 schema 格式化为可读文本"""
    lines = []
    for table_name, cols in schema.items():
        total = cols[0]["total"] if cols else 0
        lines.append(f"\n**表名**: `{table_name}`  （共 {total} 行）")
        lines.append("| 列名 | 数据类型 | 非空数 | 样本值 |")
        lines.append("|------|---------|-------|--------|")
        for col in cols:
            sample_str = ", ".join(col["sample"]) if col["sample"] else "-"
            lines.append(
                f"| {col['name']} | {col['dtype']} | {col['non_null']}/{col['total']} | {sample_str} |"
            )
    return "\n".join(lines)
