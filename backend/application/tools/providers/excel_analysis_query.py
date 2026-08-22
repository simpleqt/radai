"""Excel 分析查询工具:数据集清单、结构探查与行级查询。(拆自 excel_data_analysis_tool.py)"""

from __future__ import annotations

from backend.config.loggers import get_logger

from backend.application.tools.providers.excel_engine import df_to_markdown
from backend.application.tools.providers.excel_engine import execute_multi_sql
from backend.application.tools.providers.excel_engine import execute_sql
from backend.application.tools.providers.excel_engine import get_multi_schema
from backend.application.tools.providers.excel_engine import get_schema
from backend.application.tools.providers.excel_engine import multi_schema_to_text
from backend.application.tools.providers.excel_engine import schema_to_text
from langchain_core.tools import tool
from typing import Any
from typing import Optional
import asyncio
import json
from backend.application.tools.providers.excel_analysis_access import (
    _clean_string_list,
    _clean_tool_string,
    _current_access_context,
    _error,
    _filter_accessible_datasets,
    _resolve_file_path,
    _resolve_multi_file_paths,
)

logger = get_logger(__name__)


# 单次查询最大返回行数（Markdown 表格）
_MAX_MD_ROWS = 50


def _serialize_query_result(
    df: Any,
    output_format: str,
    markdown_header: str,
) -> str:
    """Serialize a bounded query result outside the event-loop thread."""
    if output_format == "json":
        return df.to_json(orient="records", force_ascii=False, indent=2)
    if output_format == "csv":
        return df.to_csv(index=False)
    return markdown_header + df_to_markdown(df, max_rows=_MAX_MD_ROWS)


@tool("list_experiment_datasets")
async def list_experiment_datasets(
    domain: Optional[str] = None,
    tags: Optional[list[str]] = None,
    agent_code: Optional[str] = None,
) -> str:
    """列出服务器上可用的实验数据集。

    在分析实验数据前，先调用本工具了解有哪些数据集可用。
    返回数据集编码（code）、名称、描述、领域、标签、工作表数等信息。

    Args:
        domain:     按领域过滤（如 environmental / chemistry / physics / biology）
        tags:       按标签过滤（如 ["生物炭", "AOPs"]），任意一个标签匹配即可
        agent_code: 当前智能体编码（如 ne_full_feature / csh_full_feature），
                    用于过滤仅对本智能体可见的数据集；不填则返回所有全局可见数据集
    """
    try:
        domain = _clean_tool_string(domain)
        tags = _clean_string_list(tags) if tags else None
        agent_code = _clean_tool_string(agent_code)

        from backend.db.connection import get_async_session_factory
        from backend.db.crud.dataset_crud import DatasetCRUD

        factory = get_async_session_factory()
        async with factory() as session:
            datasets = await asyncio.wait_for(
                DatasetCRUD.list_all(
                    session,
                    domain=domain,
                    tags=tags,
                    status="active",
                    is_public=None,
                ),
                timeout=10.0,
            )
            access = _current_access_context()
            effective_agent_code = access.agent_code or (agent_code or "")
            datasets = await _filter_accessible_datasets(
                session,
                datasets,
                access=access,
                agent_code=effective_agent_code,
            )

        if not datasets:
            return json.dumps({"message": "暂无可用数据集", "datasets": []}, ensure_ascii=False)

        result = []
        for ds in datasets:
            result.append({
                "code": ds.code,
                "name": ds.name,
                "description": ds.description or "",
                "domain": ds.domain or "",
                "tags": ds.tags or [],
                "sheet_names": ds.sheet_names or [],
                "row_counts": ds.row_counts or {},
                "file_type": ds.file_type,
                "status": ds.status,
            })

        return json.dumps({
            "total": len(result),
            "datasets": result,
        }, ensure_ascii=False, indent=2)

    except Exception as e:
        logger.exception("[ExcelTool] list_experiment_datasets 失败")
        return _error(f"列出数据集时出错: {e}")


@tool("inspect_excel_data")
async def inspect_excel_data(
    dataset_code: Optional[str] = None,
    dataset_codes: Optional[list[str]] = None,
    file_id: Optional[str] = None,
    file_path: Optional[str] = None,
) -> str:
    """探查 Excel/CSV 文件的表结构。

    返回：工作表列表、各表的列名与数据类型、行数、样本数据。
    **在执行 SQL 查询前必须先调用本工具**，以确认正确的表名和列名。

    支持三种数据来源（三选一）：
      - dataset_code:  单个数据集编码（单文件模式）
      - dataset_codes: 多个数据集编码列表（多文件 JOIN 模式，同时显示可用表名和 JOIN 示例）
      - file_id:       用户上传表格的文件 ID（推荐）
      - file_path:     文件路径或 MinIO object_name（兼容旧流程）

    Args:
        dataset_code:  单个数据集编码
        dataset_codes: 多个数据集编码，用于准备跨数据集 JOIN 查询
        file_id:       用户上传表格文件 ID
        file_path:     文件路径或 MinIO object_name
    """
    dataset_code = _clean_tool_string(dataset_code)
    dataset_codes = _clean_string_list(dataset_codes)
    file_id = _clean_tool_string(file_id)
    file_path = _clean_tool_string(file_path)

    # ── 多文件模式 ────────────────────────────────────────────────────
    if dataset_codes and len(dataset_codes) > 1:
        file_paths, prefixes, err = await _resolve_multi_file_paths(dataset_codes)
        if err:
            return _error(err)
        try:
            multi_schema = await asyncio.to_thread(
                get_multi_schema,
                file_paths,
                prefixes,
            )
            lines = [f"## 多数据集结构（{len(dataset_codes)} 个）\n"]
            lines.append(f"**数据集**: {', '.join(dataset_codes)}\n")
            lines.append(multi_schema_to_text(multi_schema))
            return "\n".join(lines)
        except Exception as e:
            logger.exception("[ExcelTool] inspect_excel_data 多文件失败")
            return _error(f"探查多文件结构时出错: {e}")

    # ── 单文件模式 ────────────────────────────────────────────────────
    # dataset_codes 只有 1 个元素时退化为单文件
    effective_code = dataset_code or (dataset_codes[0] if dataset_codes else None)
    abs_path, label = await _resolve_file_path(effective_code, file_path, file_id)
    if not abs_path:
        return _error(label)

    try:
        schema = await asyncio.to_thread(get_schema, abs_path)
        if not schema:
            return _error("文件解析失败，未获取到任何表结构")

        lines = [f"## 数据集结构：{label}\n"]
        lines.append(f"**工作表数量**: {len(schema)}\n")
        lines.append(schema_to_text(schema))

        lines.append("\n---\n**查询提示**:")
        lines.append("- SQL 表名使用上方列出的表名（工作表名）")
        lines.append("- 含特殊字符或空格的列名需用双引号包裹，如 `\"热解温度\"`")
        lines.append("- 示例查询:")
        first_table = next(iter(schema))
        first_cols = [c["name"] for c in schema[first_table][:3]]
        if first_cols:
            quoted_cols = ", ".join('"' + c + '"' for c in first_cols)
            sample_sql = f'SELECT {quoted_cols} FROM "{first_table}" LIMIT 5'
            lines.append(f'  ```sql\n  {sample_sql}\n  ```')
        lines.append("- 如需跨数据集 JOIN，改用 dataset_codes=[...] 参数")

        return "\n".join(lines)

    except Exception as e:
        logger.exception("[ExcelTool] inspect_excel_data 失败")
        return _error(f"探查文件结构时出错: {e}")


@tool("query_excel_data")
async def query_excel_data(
    sql: str,
    dataset_code: Optional[str] = None,
    dataset_codes: Optional[list[str]] = None,
    file_id: Optional[str] = None,
    file_path: Optional[str] = None,
    output_format: str = "markdown",
) -> str:
    """使用 SQL 查询 Excel/CSV 实验数据，支持跨数据集 JOIN。

    底层使用 DuckDB 引擎，支持完整 SQL 语法：
    聚合(GROUP BY/HAVING)、连接(JOIN)、窗口函数、子查询、CTE(WITH)等。

    ── 单数据集查询 ──
      dataset_code="biochar_degradation"
      sql='SELECT "Biomass 生物质种类", AVG(...) FROM Sheet1 GROUP BY ...'
      表名 = 工作表名（建议用双引号包裹）

    ── 跨数据集 JOIN 查询 ──
      dataset_codes=["biochar_degradation", "hydroxyl_radical_rates"]
      sql='SELECT a.*, b."OH reaction rate"
           FROM biochar_degradation__Sheet1 a
           JOIN hydroxyl_radical_rates__Sheet1 b ON a."污染物名称" = b."名字"'
      多文件表名格式：{dataset_code}__{sheet_name}
      （先调 inspect_excel_data(dataset_codes=[...]) 查看完整表名）

    Args:
        sql:            SQL 查询语句
        dataset_code:   单数据集编码（单文件模式）
        dataset_codes:  多数据集编码列表（JOIN 模式，与 dataset_code 二选一）
        file_id:        用户上传表格文件 ID（推荐）
        file_path:      文件路径或 MinIO object_name（与上两者互斥）
        output_format:  输出格式：markdown / json / csv（默认 markdown）
    """
    sql = _clean_tool_string(sql) or ""
    dataset_code = _clean_tool_string(dataset_code)
    dataset_codes = _clean_string_list(dataset_codes)
    file_id = _clean_tool_string(file_id)
    file_path = _clean_tool_string(file_path)
    output_format = (_clean_tool_string(output_format) or "markdown").lower()

    # ── 多文件 JOIN 模式 ─────────────────────────────────────────────
    if dataset_codes and len(dataset_codes) > 1:
        file_paths, prefixes, err = await _resolve_multi_file_paths(dataset_codes)
        if err:
            return _error(err)
        try:
            df = await asyncio.to_thread(
                execute_multi_sql,
                file_paths,
                prefixes,
                sql,
            )
            label = " ⋈ ".join(dataset_codes)
            header = f"**跨数据集查询结果** | {label} | 共 {len(df)} 行\n\n"
            return await asyncio.to_thread(
                _serialize_query_result,
                df,
                output_format,
                header,
            )
        except ValueError as e:
            return _error(str(e))
        except Exception as e:
            logger.exception("[ExcelTool] query_excel_data 多文件失败")
            return _error(f"多文件 SQL 执行时出错: {e}")

    # ── 单文件模式 ────────────────────────────────────────────────────
    effective_code = dataset_code or (dataset_codes[0] if dataset_codes else None)
    abs_path, label = await _resolve_file_path(effective_code, file_path, file_id)
    if not abs_path:
        return _error(label)

    try:
        df = await asyncio.to_thread(execute_sql, abs_path, sql)
        header = f"**查询结果** | 数据集: {label} | 共 {len(df)} 行\n\n"
        return await asyncio.to_thread(
            _serialize_query_result,
            df,
            output_format,
            header,
        )

    except ValueError as e:
        return _error(str(e))
    except Exception as e:
        logger.exception("[ExcelTool] query_excel_data 失败")
        return _error(f"SQL 执行时出错: {e}")
