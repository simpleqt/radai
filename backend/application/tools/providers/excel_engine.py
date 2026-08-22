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

import os
import re
from collections.abc import Sequence
from typing import Any

import duckdb
import pandas as pd

from backend.config.loggers import get_logger

from backend.application.tools.providers.excel_engine_safety import (  # noqa: F401  (兼容 re-export)
    _MAX_QUERY_ROWS,
    _SQL_LITERAL_OR_COMMENT,
    _UNSAFE_SQL,
    _allowed_local_roots,
    _bounded_max_rows,
    _file_hash,
    _is_relative_to,
    _mask_sql_literals_and_comments,
    _new_safe_connection,
    _resolved_roots,
    _sanitize_table_name,
    is_allowed_local_table_path,
    validate_read_only_sql,
)
from backend.application.tools.providers.excel_engine_connections import (  # noqa: F401  (兼容 re-export)
    _CACHE_MAX_ENTRIES,
    _CACHE_TTL_SECONDS,
    _cache,
    _cache_last_used,
    _cache_lock,
    _cache_sources,
    _clean_dataframe,
    _close_cache_entry_locked,
    _connection_query_locks,
    _leased_connection,
    _load_file_to_duckdb,
    _load_multi_files_to_duckdb,
    _multi_cache_key,
    _multi_table_maps,
    _named_tables_cache_key,
    _prune_cache_locked,
    _remember_cache_entry_locked,
    _resolve_local_path,
    _touch_cache_entry_locked,
    get_connection,
    get_multi_connection,
    get_named_tables_connection,
    invalidate_cache,
)

logger = get_logger(__name__)


# ── 多文件联合连接 ────────────────────────────────────────────────────


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
