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
import json
import os
import re
import threading
from typing import Any

import duckdb
import pandas as pd

from backend.config.loggers import get_logger
from backend.services.minio_paths import normalize_minio_endpoint, parse_minio_source

logger = get_logger(__name__)

_cache: dict[str, duckdb.DuckDBPyConnection] = {}
_multi_table_maps: dict[str, dict[str, list[str]]] = {}  # cache_key → table_map
_connection_query_locks: dict[int, threading.RLock] = {}
_cache_lock = threading.Lock()
_MAX_QUERY_ROWS = 5000


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
    3. 字符串列数值推断（宽松模式）：
       - 若 ≥50% 的非空值能转为数值，整列推断为 float64（非数字变 NaN）
       - 解决 Excel 中列头混入数据行的"混合列"问题
       - 解决 "\xa0" 等污染字符导致 DuckDB CAST to DOUBLE 失败的问题
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

            success_rate = numeric[non_null_mask].notna().mean()

            if success_rate >= 0.5:
                # 多数为数值：整列转 float64，不可转的（如表头）变 NaN
                df[col] = numeric
            else:
                df[col] = cleaned
        except Exception:
            df[col] = cleaned

    return df


def _load_file_to_duckdb(file_path: str) -> duckdb.DuckDBPyConnection:
    """将 Excel/CSV 文件加载到新的 DuckDB 内存连接"""
    conn = duckdb.connect()
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


def _resolve_local_path(file_path: str) -> str:
    """将 MinIO object_name 下载到本地缓存目录，返回本地路径；本地路径直接返回"""
    from pathlib import Path

    # 已是本地文件
    if Path(file_path).exists():
        return file_path

    # 视为 MinIO object_name，下载到本地缓存
    try:
        import asyncio
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

        from minio import Minio
        from backend.config.settings import settings

        client = Minio(
            normalize_minio_endpoint(settings.minio_endpoint),
            access_key=settings.minio_access_key,
            secret_key=settings.minio_secret_key,
            secure=getattr(settings, "minio_secure", False),
        )
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
        if cache_key not in _cache:
            logger.info("[ExcelEngine] 创建新 DuckDB 连接: %s", local_path)
            _cache[cache_key] = _load_file_to_duckdb(local_path)
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
    conn = duckdb.connect()
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
        if cache_key not in _cache:
            logger.info("[ExcelEngine] 创建多文件 DuckDB 连接: prefixes=%s", prefixes)
            conn, table_map = _load_multi_files_to_duckdb(local_paths, prefixes)
            _cache[cache_key] = conn
            _multi_table_maps[cache_key] = table_map
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
    conn, _ = get_multi_connection(file_paths, prefixes)
    try:
        result = conn.execute(sql).fetchdf()
        if len(result) > max_rows:
            logger.warning("[ExcelEngine] 多文件查询结果截断: %d → %d 行", len(result), max_rows)
            result = result.head(max_rows)
        return result
    except duckdb.Error as e:
        raise ValueError(f"SQL 执行错误: {e}") from e


def _named_tables_cache_key(table_paths: dict[str, str]) -> str:
    parts = [f"{name}:{_file_hash(path)}" for name, path in sorted(table_paths.items())]
    return "named_" + hashlib.md5("|".join(parts).encode()).hexdigest()


def get_named_tables_connection(
    table_paths: dict[str, str],
) -> duckdb.DuckDBPyConnection:
    """Load authorized CSV/Excel files as explicitly named DuckDB tables."""
    if not table_paths:
        raise ValueError("没有可查询的表格")

    local_paths = {name: _resolve_local_path(path) for name, path in table_paths.items()}
    cache_key = _named_tables_cache_key(local_paths)
    with _cache_lock:
        if cache_key in _cache:
            return _cache[cache_key]

        conn = duckdb.connect()
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
        conn.execute("SET enable_external_access = false")
        _cache[cache_key] = conn
        _connection_query_locks[id(conn)] = threading.RLock()
        return conn


def _query_lock(conn: duckdb.DuckDBPyConnection) -> threading.RLock:
    with _cache_lock:
        return _connection_query_locks.setdefault(id(conn), threading.RLock())


def get_named_tables_schema(
    table_paths: dict[str, str],
) -> dict[str, list[dict[str, Any]]]:
    conn = get_named_tables_connection(table_paths)
    result: dict[str, list[dict[str, Any]]] = {}
    with _query_lock(conn):
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
) -> pd.DataFrame:
    """Execute SQL against a caller-authorized mapping of logical table names."""
    conn = get_named_tables_connection(table_paths)
    try:
        with _query_lock(conn):
            result = conn.execute(sql).fetchdf()
        if len(result) > max_rows:
            logger.warning(
                "[ExcelEngine] 命名表查询结果截断: %d -> %d 行",
                len(result),
                max_rows,
            )
            result = result.head(max_rows)
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
    conn, table_map = get_multi_connection(file_paths, prefixes)
    result: dict[str, dict[str, list[dict]]] = {}

    for prefix, table_names in table_map.items():
        result[prefix] = {}
        for table_name in table_names:
            try:
                cols_df = conn.execute(f'DESCRIBE "{table_name}"').fetchdf()
                count_df = conn.execute(f'SELECT COUNT(*) as n FROM "{table_name}"').fetchdf()
                total = int(count_df["n"].iloc[0])
                cols = []
                for _, row in cols_df.iterrows():
                    col_name = str(row["column_name"])
                    dtype = str(row["column_type"])
                    try:
                        sample_df = conn.execute(
                            f'SELECT "{col_name}" FROM "{table_name}" WHERE "{col_name}" IS NOT NULL LIMIT 3'
                        ).fetchdf()
                        sample = [str(v) for v in sample_df[col_name].tolist()]
                        non_null_df = conn.execute(
                            f'SELECT COUNT(*) as n FROM "{table_name}" WHERE "{col_name}" IS NOT NULL'
                        ).fetchdf()
                        non_null = int(non_null_df["n"].iloc[0])
                    except Exception:
                        sample, non_null = [], 0
                    cols.append({"name": col_name, "dtype": dtype, "non_null": non_null, "total": total, "sample": sample})
                result[prefix][table_name] = cols
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
    cache_key = _file_hash(file_path)
    with _cache_lock:
        if cache_key in _cache:
            try:
                _cache[cache_key].close()
            except Exception:
                pass
            del _cache[cache_key]
            logger.info("[ExcelEngine] 缓存已清除: %s", file_path)


def execute_sql(file_path: str, sql: str, max_rows: int = _MAX_QUERY_ROWS) -> pd.DataFrame:
    """执行 SQL 查询，返回 DataFrame

    Args:
        file_path: Excel/CSV 文件路径
        sql: SQL 查询语句（表名为工作表名或其 sanitized 版本）
        max_rows: 最大返回行数限制
    """
    conn = get_connection(file_path)
    try:
        result = conn.execute(sql).fetchdf()
        if len(result) > max_rows:
            logger.warning(
                "[ExcelEngine] 查询结果超过限制，截断至 %d 行（原 %d 行）",
                max_rows, len(result),
            )
            result = result.head(max_rows)
        return result
    except duckdb.Error as e:
        raise ValueError(f"SQL 执行错误: {e}") from e


def get_schema(file_path: str) -> dict[str, list[dict]]:
    """获取文件的表结构

    Returns:
        {table_name: [{name, dtype, non_null_count, total_count, sample_values}]}
    """
    conn = get_connection(file_path)
    # 列出所有已注册的视图
    tables_df = conn.execute("SHOW TABLES").fetchdf()
    schema: dict[str, list[dict]] = {}

    for table_name in tables_df["name"].tolist():
        try:
            cols_df = conn.execute(f'DESCRIBE "{table_name}"').fetchdf()
            count_df = conn.execute(f'SELECT COUNT(*) as n FROM "{table_name}"').fetchdf()
            total = int(count_df["n"].iloc[0])

            cols = []
            for _, row in cols_df.iterrows():
                col_name = str(row["column_name"])
                dtype = str(row["column_type"])
                # 采样非空值（最多 3 个）
                try:
                    sample_df = conn.execute(
                        f'SELECT "{col_name}" FROM "{table_name}" WHERE "{col_name}" IS NOT NULL LIMIT 3'
                    ).fetchdf()
                    sample = [str(v) for v in sample_df[col_name].tolist()]
                    non_null_df = conn.execute(
                        f'SELECT COUNT(*) as n FROM "{table_name}" WHERE "{col_name}" IS NOT NULL'
                    ).fetchdf()
                    non_null = int(non_null_df["n"].iloc[0])
                except Exception:
                    sample = []
                    non_null = 0
                cols.append({
                    "name": col_name,
                    "dtype": dtype,
                    "non_null": non_null,
                    "total": total,
                    "sample": sample,
                })
            schema[table_name] = cols
        except Exception as e:
            logger.warning("[ExcelEngine] 获取表结构失败: table=%s, error=%s", table_name, e)

    return schema


def get_summary(file_path: str, table_name: str) -> pd.DataFrame:
    """对指定表生成统计摘要（均值、标准差、分位数等）"""
    conn = get_connection(file_path)
    try:
        df = conn.execute(f'SELECT * FROM "{table_name}"').fetchdf()
        return df.describe(include="all").round(4)
    except Exception as e:
        raise ValueError(f"统计摘要生成失败: {e}") from e


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
