"""Permission-scoped DuckDB tools for structured project tables."""

from __future__ import annotations

import json
import re
from pathlib import Path
from typing import Any

from langchain_core.tools import tool

from backend.application.groundwater import GroundwaterProjectService
from backend.config.loggers import get_logger
from backend.application.tools.providers.excel_engine import (
    execute_named_tables_sql,
    get_named_tables_schema,
)
from backend.domain.tools.runtime_context import ToolRuntimeContext


logger = get_logger(__name__)

_UNSAFE_SQL = re.compile(
    r"\b(attach|copy|create|delete|detach|drop|export|import|insert|install|load|"
    r"glob|httpfs|parquet_scan|postgres_scan|pragma|read_csv|read_json|"
    r"read_parquet|read_text|replace|secret|sqlite_scan|update|"
    r"write_csv|write_parquet)\b",
    re.IGNORECASE,
)


def _json(value: Any) -> str:
    return json.dumps(value, ensure_ascii=False, indent=2, default=str)


def _project_context(runtime_context: ToolRuntimeContext | None):
    if runtime_context is None:
        raise ValueError("缺少工具运行时上下文")
    project_id = str(runtime_context.domain_context.get("project_id") or "").strip()
    if not project_id:
        raise ValueError("项目上下文未经过 AgentService 解析")
    service = GroundwaterProjectService()
    return service.resolve(runtime_context.user_id, project_id)


def _table_paths(context) -> dict[str, str]:
    datasets = context.manifest.get("datasets") or {}
    if not isinstance(datasets, dict):
        raise ValueError("项目 manifest.datasets 格式无效")

    root = context.project_root.resolve()
    result: dict[str, str] = {}
    for name, descriptor in datasets.items():
        if not isinstance(descriptor, dict):
            continue
        relative = str(descriptor.get("csv") or "").strip()
        if not relative:
            continue
        path = (root / relative).resolve()
        if root not in path.parents or not path.is_file():
            continue
        result[str(name)] = str(path)
    if not result:
        raise ValueError("当前项目没有 manifest 声明的 CSV 表格")
    return result


def _validate_read_only_sql(sql: str) -> str:
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
    )
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

    normalized = normalized.rstrip(";").strip()
    if not normalized or not re.match(r"^(select|with)\b", normalized, re.IGNORECASE):
        logger.warning(
            "[ProjectTableTool] 拒绝非查询 SQL: prefix=%r length=%d",
            normalized[:120],
            len(normalized),
        )
        raise ValueError("项目表格仅允许 SELECT 或 WITH 查询")
    if ";" in normalized or _UNSAFE_SQL.search(normalized):
        logger.warning(
            "[ProjectTableTool] 拒绝不安全 SQL: prefix=%r length=%d",
            normalized[:120],
            len(normalized),
        )
        raise ValueError("SQL 包含不允许的文件访问、扩展或写操作")
    return normalized


def _execute(context, sql: str, max_rows: int):
    return execute_named_tables_sql(
        _table_paths(context),
        _validate_read_only_sql(sql),
        max_rows=max(1, min(max_rows, 1000)),
    )


def _records(df) -> list[dict[str, Any]]:
    clean = df.astype(object).where(df.notna(), None)
    return clean.to_dict(orient="records")


def get_project_table_tools(*, runtime_context: ToolRuntimeContext | None = None):
    @tool("inspect_project_tables")
    def inspect_project_tables(table_name: str = "") -> str:
        """查看当前授权项目的 DuckDB 表名、字段类型和总行数；SQL 查询前先调用。"""
        try:
            context = _project_context(runtime_context)
            schema = get_named_tables_schema(_table_paths(context))
            if table_name:
                schema = {table_name: schema[table_name]} if table_name in schema else {}
            return _json(
                {
                    "project_id": context.project_id,
                    "data_version": context.data_version,
                    "engine": "duckdb",
                    "tables": schema,
                }
            )
        except Exception as exc:
            return _json({"error": str(exc)})

    @tool("query_project_tables")
    def query_project_tables(sql: str, max_rows: int = 200) -> str:
        """使用只读 DuckDB SQL 查询当前授权项目表。sql 传原始 SELECT/WITH，不要附加解释文字。"""
        try:
            context = _project_context(runtime_context)
            df = _execute(context, sql, max_rows)
            return _json(
                {
                    "project_id": context.project_id,
                    "data_version": context.data_version,
                    "engine": "duckdb",
                    "row_count": len(df),
                    "columns": list(df.columns),
                    "rows": _records(df),
                }
            )
        except Exception as exc:
            return _json({"error": str(exc)})

    @tool("summarize_project_table")
    def summarize_project_table(table_name: str) -> str:
        """使用 DuckDB 汇总当前项目单表的总行数，供完整性检查使用。"""
        try:
            context = _project_context(runtime_context)
            tables = _table_paths(context)
            if table_name not in tables:
                raise ValueError(f"未知项目表: {table_name}")
            df = _execute(
                context,
                f'SELECT COUNT(*) AS row_count FROM "{table_name}"',
                1,
            )
            return _json(
                {
                    "project_id": context.project_id,
                    "data_version": context.data_version,
                    "engine": "duckdb",
                    "table": table_name,
                    "row_count": int(df.iloc[0]["row_count"]),
                }
            )
        except Exception as exc:
            return _json({"error": str(exc)})

    return [inspect_project_tables, query_project_tables, summarize_project_table]


def _get_tool(name: str, runtime_context: ToolRuntimeContext | None):
    return next(
        item
        for item in get_project_table_tools(runtime_context=runtime_context)
        if item.name == name
    )


def get_inspect_project_tables_tool(*, runtime_context: ToolRuntimeContext | None = None):
    return _get_tool("inspect_project_tables", runtime_context)


def get_query_project_tables_tool(*, runtime_context: ToolRuntimeContext | None = None):
    return _get_tool("query_project_tables", runtime_context)


def get_summarize_project_table_tool(*, runtime_context: ToolRuntimeContext | None = None):
    return _get_tool("summarize_project_table", runtime_context)
