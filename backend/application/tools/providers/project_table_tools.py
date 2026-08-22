"""Permission-scoped DuckDB tools for structured project tables."""

from __future__ import annotations

import json
import re
from typing import Any

from langchain_core.tools import tool

from backend.application.groundwater import GroundwaterProjectService
from backend.application.tools.providers.excel_engine import (
    _UNSAFE_SQL,
    _mask_sql_literals_and_comments,
    execute_named_tables_sql,
    get_named_tables_schema,
)
from backend.config.loggers import get_logger
from backend.core.tools.tool_arguments import clean_malformed_tool_text
from backend.domain.tools.runtime_context import ToolRuntimeContext

logger = get_logger(__name__)

# Project packages are standardized over time, while models often use generic
# hydrogeology names.  Keep this compatibility layer deliberately small and
# project-scoped: it only substitutes a relation after FROM/JOIN and only when
# the target dataset is present in the current authorized project.
_LEGACY_TABLE_ALIASES: dict[str, tuple[str, str]] = {
    "well_construction": ("constructed_wells", "constructed_wells"),
    "wells": ("constructed_wells", "constructed_wells"),
    "well_locations": ("points", "points"),
    "monitoring_wells": (
        "points",
        "(SELECT * FROM points WHERE point_type = 'monitoring_point')",
    ),
    "monitoring_points": (
        "points",
        "(SELECT * FROM points WHERE point_type = 'monitoring_point')",
    ),
    "boreholes": ("geotechnical_boreholes", "geotechnical_boreholes"),
    "stratigraphy": ("geology_layers", "geology_layers"),
    "contamination_assessment": (
        "water_quality_measurements",
        "water_quality_measurements",
    ),
}
_PROJECT_CATALOG_QUERY = re.compile(
    r"\b(?:duckdb_tables|information_schema\s*\.\s*tables|sqlite_master|pg_catalog)\b",
    re.IGNORECASE,
)
_RELATION_REFERENCE = re.compile(
    r'\b(?:from|join)\s+(?:"(?P<quoted>[^"]+)"|(?P<bare>[A-Za-z_][A-Za-z0-9_$]*))',
    re.IGNORECASE,
)
_CTE_NAME = re.compile(r"\b(?P<name>[A-Za-z_][A-Za-z0-9_$]*)\s+AS\s*\(", re.IGNORECASE)


class ProjectSqlValidationError(ValueError):
    """Raised when a query references a relation outside the project contract."""

    def __init__(self, unknown_tables: list[str], available_tables: list[str]):
        self.unknown_tables = unknown_tables
        self.available_tables = available_tables
        super().__init__(f"项目中不存在表: {', '.join(unknown_tables)}")


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
    normalized = clean_malformed_tool_text(str(sql or "")).lstrip("\ufeff").strip()
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
    masked = _mask_sql_literals_and_comments(normalized)
    if ";" in masked or _UNSAFE_SQL.search(masked):
        logger.warning(
            "[ProjectTableTool] 拒绝不安全 SQL: prefix=%r length=%d",
            normalized[:120],
            len(normalized),
        )
        raise ValueError("SQL 包含不允许的文件访问、扩展或写操作")
    return normalized


def _normalize_project_table_aliases(
    sql: str,
    table_names: set[str],
) -> tuple[str, dict[str, str]]:
    """Replace documented legacy relation names in FROM/JOIN clauses only."""
    normalized = sql
    applied: dict[str, str] = {}
    available = {name.casefold() for name in table_names}

    for legacy_name, (required_table, replacement) in _LEGACY_TABLE_ALIASES.items():
        if required_table.casefold() not in available:
            continue
        pattern = re.compile(
            rf"(\b(?:from|join)\s+)(?:\"{re.escape(legacy_name)}\"|{re.escape(legacy_name)})\b",
            re.IGNORECASE,
        )
        normalized, count = pattern.subn(rf"\1{replacement}", normalized)
        if count:
            applied[legacy_name] = replacement

    # The legacy strata schema used layer_order; the standardized project uses
    # layer_no. Only alter this field when the legacy relation was actually used.
    if "stratigraphy" in applied:
        normalized = re.sub(r"\blayer_order\b", "layer_no", normalized, flags=re.IGNORECASE)
        applied["stratigraphy"] = "geology_layers (layer_order -> layer_no)"
    return normalized, applied


def _validate_project_relations(sql: str, table_names: set[str]) -> None:
    """Reject guessed relation names before DuckDB executes the SQL.

    This deliberately validates only concrete FROM/JOIN relations.  Nested
    SELECTs and CTEs remain valid, while a typo is turned into a compact,
    actionable tool response instead of a database catalogue error.
    """
    scrubbed = re.sub(r"--[^\n]*|/\*.*?\*/", "", sql, flags=re.DOTALL)
    available = {name.casefold() for name in table_names}
    ctes = {match.group("name").casefold() for match in _CTE_NAME.finditer(scrubbed)}
    unknown = {
        (match.group("quoted") or match.group("bare") or "").strip()
        for match in _RELATION_REFERENCE.finditer(scrubbed)
        if (match.group("quoted") or match.group("bare") or "").casefold()
        not in available | ctes
    }
    if unknown:
        raise ProjectSqlValidationError(sorted(unknown), sorted(table_names))


def _prepare_project_sql(context, sql: str) -> tuple[dict[str, str], str, dict[str, str]]:
    table_paths = _table_paths(context)
    validated = _validate_read_only_sql(sql)
    normalized, applied = _normalize_project_table_aliases(validated, set(table_paths))
    _validate_project_relations(normalized, set(table_paths))
    return table_paths, normalized, applied


def _is_project_catalog_query(sql: str) -> bool:
    return bool(_PROJECT_CATALOG_QUERY.search(sql))


def _project_catalog_payload(context) -> dict[str, Any]:
    schema = get_named_tables_schema(
        _table_paths(context),
        authorized_roots=[context.project_root],
    )
    rows = [
        {
            "table_name": table_name,
            "row_count": columns[0]["total"] if columns else 0,
            "column_count": len(columns),
        }
        for table_name, columns in sorted(schema.items())
    ]
    return {
        "project_id": context.project_id,
        "data_version": context.data_version,
        "engine": "duckdb",
        "row_count": len(rows),
        "columns": ["table_name", "row_count", "column_count"],
        "rows": rows,
        "query_note": "项目表清单由 inspect_project_tables 提供；已返回当前授权表及行数。",
        "query_kind": "schema_catalog",
        "counts_toward_query_budget": False,
    }


def _execute(context, sql: str, max_rows: int):
    table_paths, prepared_sql, _ = _prepare_project_sql(context, sql)
    return execute_named_tables_sql(
        table_paths,
        prepared_sql,
        max_rows=max(1, min(max_rows, 1000)),
        authorized_roots=[context.project_root],
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
            schema = get_named_tables_schema(
                _table_paths(context),
                authorized_roots=[context.project_root],
            )
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
            logger.exception("[ProjectTableTool] 检查项目表结构失败")
            return _json({"error": str(exc)})

    @tool("query_project_tables")
    def query_project_tables(sql: str, max_rows: int = 200) -> str:
        """使用只读 DuckDB SQL 查询当前授权项目表；未知表名会在执行前被拒绝。"""
        try:
            context = _project_context(runtime_context)
            if _is_project_catalog_query(sql):
                return _json(_project_catalog_payload(context))
            table_paths, prepared_sql, aliases_applied = _prepare_project_sql(context, sql)
            df = execute_named_tables_sql(
                table_paths,
                prepared_sql,
                max_rows=max(1, min(max_rows, 1000)),
                authorized_roots=[context.project_root],
            )
            payload: dict[str, Any] = {
                "project_id": context.project_id,
                "data_version": context.data_version,
                "engine": "duckdb",
                "row_count": len(df),
                "columns": list(df.columns),
                "rows": _records(df),
                "query_kind": "data_query",
                "counts_toward_query_budget": True,
            }
            if aliases_applied:
                payload["table_aliases_applied"] = aliases_applied
                payload["query_note"] = "已将旧表名映射到当前项目的标准化数据表。"
            return _json(payload)
        except ProjectSqlValidationError as exc:
            return _json(
                {
                    "error": "unknown_project_table",
                    "message": str(exc),
                    "unknown_tables": exc.unknown_tables,
                    "available_tables": exc.available_tables,
                    "instruction": (
                        "不要猜测表名。请使用 groundwater_project_bootstrap 或 "
                        "inspect_project_tables 返回的真实表名；监测点查 points，"
                        "治理井查 constructed_wells，污染物查 water_quality_measurements。"
                    ),
                    "query_kind": "validation_error",
                    "counts_toward_query_budget": False,
                }
            )
        except Exception as exc:
            logger.exception("[ProjectTableTool] 执行项目表查询失败")
            return _json(
                {
                    "error": str(exc),
                    "instruction": "请调用 inspect_project_tables 后仅使用返回的真实表名和字段。",
                    "query_kind": "execution_error",
                    "counts_toward_query_budget": False,
                }
            )

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
            logger.exception("[ProjectTableTool] 汇总项目表失败")
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
