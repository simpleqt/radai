"""Excel 实验数据分析工具集

为智能体提供 5 个 LangChain 工具：
  1. list_experiment_datasets  — 发现服务器上预注册的实验数据集
  2. inspect_excel_data        — 探查文件/数据集的表结构（支持多数据集）
  3. query_excel_data          — SQL 查询数据（支持跨数据集 JOIN）
  4. summarize_excel_data      — 生成统计摘要
  5. visualize_excel_data      — 可视化图表生成

数据源支持（三种模式）：
  - dataset_code:  使用单个服务器预注册数据集
  - dataset_codes: 使用多个数据集（跨数据集 JOIN 模式）
  - file_path:     用户上传的文件（绝对路径）
"""

from __future__ import annotations

import json
import time
import uuid
from pathlib import Path
from typing import Optional

import pandas as pd
from langchain_core.tools import tool

from backend.config.loggers import get_logger
from backend.application.tools.providers.excel_engine import (
    df_to_markdown,
    execute_sql,
    execute_multi_sql,
    get_schema,
    get_multi_schema,
    get_summary,
    schema_to_text,
    multi_schema_to_text,
)

logger = get_logger(__name__)

# 单次查询最大返回行数（Markdown 表格）
_MAX_MD_ROWS = 50


# ── 数据源解析辅助 ────────────────────────────────────────────────────

async def _resolve_file_path(
    dataset_code: str | None,
    file_path: str | None,
    file_id: str | None = None,
) -> tuple[str, str]:
    """解析数据源，返回 (abs_file_path, source_label)

    优先使用 dataset_code，其次 file_id，最后 file_path。
    """
    if dataset_code:
        try:
            from backend.db.connection import get_async_session_factory
            from backend.db.crud.dataset_crud import DatasetCRUD

            factory = get_async_session_factory()
            async with factory() as session:
                ds = await DatasetCRUD.get_by_code(session, dataset_code)
            if ds:
                return ds.file_path, ds.name
        except Exception as e:
            logger.warning("[ExcelTool] 数据库查询数据集失败: %s", e)
        return "", f"未找到数据集: {dataset_code}"

    if file_id:
        try:
            from backend.services.table_analysis_files import resolve_table_file_source

            source, info = resolve_table_file_source(file_id)
            return source, info.get("file_name") or f"上传表格: {file_id}"
        except Exception as e:
            logger.warning("[ExcelTool] 解析上传表格 file_id 失败: %s", e)
            return "", f"无法解析上传表格: {e}"

    if file_path:
        p = Path(file_path)
        if p.exists():
            return str(p.resolve()), p.name
        return "", f"文件不存在: {file_path}"

    return "", "未指定数据源（需提供 dataset_code 或 file_path）"


async def _resolve_multi_file_paths(
    dataset_codes: list[str],
) -> tuple[list[str], list[str], str]:
    """解析多个 dataset_code，返回 (file_paths, prefixes, error_msg)。
    
    error_msg 非空表示有数据集不存在，此时 file_paths/prefixes 为空列表。
    """
    try:
        from backend.db.connection import get_async_session_factory
        from backend.db.crud.dataset_crud import DatasetCRUD

        factory = get_async_session_factory()
        async with factory() as session:
            file_paths, prefixes = [], []
            missing = []
            for code in dataset_codes:
                ds = await DatasetCRUD.get_by_code(session, code)
                if not ds:
                    missing.append(code)
                else:
                    file_paths.append(ds.file_path)
                    prefixes.append(code)
            if missing:
                return [], [], f"以下数据集不存在: {', '.join(missing)}"
            return file_paths, prefixes, ""
    except Exception as e:
        return [], [], f"数据库查询失败: {e}"


def _error(msg: str) -> str:
    return json.dumps({"error": msg}, ensure_ascii=False)


# ── 工具 1：列出数据集 ────────────────────────────────────────────────

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
        from backend.db.connection import get_async_session_factory
        from backend.db.crud.dataset_crud import DatasetCRUD

        factory = get_async_session_factory()
        async with factory() as session:
            datasets = await DatasetCRUD.list_all(
                session, domain=domain, tags=tags, agent_code=agent_code,
                status="active", is_public=True,
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


# ── 工具 2：探查表结构 ────────────────────────────────────────────────

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
    # ── 多文件模式 ────────────────────────────────────────────────────
    if dataset_codes and len(dataset_codes) > 1:
        file_paths, prefixes, err = await _resolve_multi_file_paths(dataset_codes)
        if err:
            return _error(err)
        try:
            multi_schema = get_multi_schema(file_paths, prefixes)
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
        schema = get_schema(abs_path)
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


# ── 工具 3：SQL 查询 ──────────────────────────────────────────────────

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
    # ── 多文件 JOIN 模式 ─────────────────────────────────────────────
    if dataset_codes and len(dataset_codes) > 1:
        file_paths, prefixes, err = await _resolve_multi_file_paths(dataset_codes)
        if err:
            return _error(err)
        try:
            df = execute_multi_sql(file_paths, prefixes, sql)
            label = " ⋈ ".join(dataset_codes)
            if output_format == "json":
                return df.to_json(orient="records", force_ascii=False, indent=2)
            elif output_format == "csv":
                return df.to_csv(index=False)
            else:
                header = f"**跨数据集查询结果** | {label} | 共 {len(df)} 行\n\n"
                return header + df_to_markdown(df, max_rows=_MAX_MD_ROWS)
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
        df = execute_sql(abs_path, sql)
        if output_format == "json":
            return df.to_json(orient="records", force_ascii=False, indent=2)
        elif output_format == "csv":
            return df.to_csv(index=False)
        else:
            header = f"**查询结果** | 数据集: {label} | 共 {len(df)} 行\n\n"
            return header + df_to_markdown(df, max_rows=_MAX_MD_ROWS)

    except ValueError as e:
        return _error(str(e))
    except Exception as e:
        logger.exception("[ExcelTool] query_excel_data 失败")
        return _error(f"SQL 执行时出错: {e}")


# ── 工具 4：统计摘要 ──────────────────────────────────────────────────

@tool("summarize_excel_data")
async def summarize_excel_data(
    dataset_code: Optional[str] = None,
    dataset_codes: Optional[list[str]] = None,
    file_id: Optional[str] = None,
    file_path: Optional[str] = None,
    sheet_name: Optional[str] = None,
    columns: Optional[list[str]] = None,
) -> str:
    """生成 Excel 数据的统计摘要。

    对数值列：count、均值(mean)、标准差(std)、最小值、四分位数、最大值
    对分类列：count、唯一值数、最高频值及频次
    还会自动计算：缺失率、数值列之间的相关系数矩阵

    支持多数据集模式（dataset_codes），对每个数据集分别生成摘要。

    Args:
        dataset_code:  单数据集编码（与其他来源三选一）
        dataset_codes: 多数据集编码，分别生成各自摘要
        file_id:       用户上传表格文件 ID（推荐）
        file_path:     文件路径或 MinIO object_name
        sheet_name:    指定工作表名（不填则对所有表生成摘要）
        columns:       指定要分析的列名列表（不填则分析所有列）
    """
    # 多数据集模式：对每个数据集分别生成摘要
    if dataset_codes and len(dataset_codes) >= 1:
        all_lines = [f"## 统计摘要（{len(dataset_codes)} 个数据集）\n"]
        for code in dataset_codes:
            abs_path_i, label_i = await _resolve_file_path(code, None)
            if not abs_path_i:
                all_lines.append(f"\n⚠️ 数据集 `{code}` 不存在，跳过")
                continue
            all_lines.append(f"\n---\n### 数据集：{label_i} (`{code}`)\n")
            schema_i = get_schema(abs_path_i)
            if not schema_i:
                all_lines.append("⚠️ 无法获取表结构")
                continue
            tables_i = [sheet_name] if sheet_name and sheet_name in schema_i else list(schema_i.keys())
            for table in tables_i:
                try:
                    df_t = execute_sql(abs_path_i, f'SELECT * FROM "{table}"')
                    if columns:
                        df_t = df_t[[c for c in columns if c in df_t.columns]]
                    all_lines.append(f"\n#### 表：`{table}`（共 {len(df_t)} 行，{len(df_t.columns)} 列）")
                    all_lines.append(df_t.describe(include="all").round(4).to_markdown())
                    num_c = df_t.select_dtypes("number").columns.tolist()
                    if len(num_c) >= 2:
                        all_lines.append("\n**相关系数矩阵：**")
                        all_lines.append(df_t[num_c].corr().round(3).to_markdown())
                except Exception as e:
                    all_lines.append(f"\n⚠️ 表 `{table}` 分析失败: {e}")
        return "\n".join(all_lines)

    effective_code = dataset_code or (dataset_codes[0] if dataset_codes else None)
    abs_path, label = await _resolve_file_path(effective_code, file_path, file_id)
    if not abs_path:
        return _error(label)

    try:
        from backend.application.tools.providers.excel_engine import get_schema

        schema = get_schema(abs_path)
        if not schema:
            return _error("无法获取文件结构")

        # 确定要分析的表
        tables = [sheet_name] if sheet_name and sheet_name in schema else list(schema.keys())
        lines = [f"## 统计摘要：{label}\n"]

        for table in tables:
            try:
                df_full = execute_sql(abs_path, f'SELECT * FROM "{table}"')
                if columns:
                    df_full = df_full[[c for c in columns if c in df_full.columns]]

                lines.append(f"\n### 表：`{table}`（共 {len(df_full)} 行，{len(df_full.columns)} 列）\n")

                # 描述统计
                desc = df_full.describe(include="all").round(4)
                lines.append("**描述统计：**")
                lines.append(desc.to_markdown())

                # 缺失值统计
                null_info = df_full.isnull().sum()
                null_pct = (null_info / len(df_full) * 100).round(2)
                null_df = pd.DataFrame({"缺失数": null_info, "缺失率(%)": null_pct})
                null_df = null_df[null_df["缺失数"] > 0]
                if not null_df.empty:
                    lines.append("\n**缺失值统计（仅列出有缺失的列）：**")
                    lines.append(null_df.to_markdown())
                else:
                    lines.append("\n✅ 无缺失值")

                # 数值列相关性
                num_cols = df_full.select_dtypes("number").columns.tolist()
                if len(num_cols) >= 2:
                    corr = df_full[num_cols].corr().round(3)
                    lines.append("\n**数值列相关系数矩阵：**")
                    lines.append(corr.to_markdown())

            except Exception as e:
                lines.append(f"\n⚠️ 表 `{table}` 分析失败: {e}")

        return "\n".join(lines)

    except Exception as e:
        logger.exception("[ExcelTool] summarize_excel_data 失败")
        return _error(f"统计摘要生成时出错: {e}")


# ── 工具 5：可视化 ────────────────────────────────────────────────────

@tool("visualize_excel_data")
async def visualize_excel_data(
    dataset_code: Optional[str] = None,
    dataset_codes: Optional[list[str]] = None,
    file_id: Optional[str] = None,
    file_path: Optional[str] = None,
    chart_type: str = "auto",
    x_column: Optional[str] = None,
    y_columns: Optional[list[str]] = None,
    group_by: Optional[str] = None,
    title: Optional[str] = None,
    sql: Optional[str] = None,
    sheet_name: Optional[str] = None,
    max_rows: int = 500,
    style: Optional[dict] = None,
) -> str:
    """生成实验数据的可视化图表，支持跨数据集 JOIN 结果可视化。

    支持图表类型（chart_type）：
      - auto      根据数据特征自动选择（推荐）
      - scatter   散点图（两连续变量相关性）
      - bar       柱状图（均值±标准差，适合分类比较）
      - barh      水平柱状图（适合反应速率排序等）
      - line      折线图（趋势分析）
      - box       箱线图（分布比较）
      - violin    小提琴图（分布形态）
      - histogram 直方图（频率分布）
      - heatmap   热力图（相关性矩阵 或 双因素交互）
      - bubble    气泡图（三维关系）
      - radar     雷达图（多维对比）

    跨数据集可视化：
      dataset_codes=["biochar_degradation", "hydroxyl_radical_rates"]
      sql='SELECT a."污染物名称", b."OH reaction rate", a."Degradation rate降解速率"
           FROM biochar_degradation__Sheet1 a
           JOIN hydroxyl_radical_rates__Sheet1 b ON a."污染物名称" = b."名字"'
      → 对 JOIN 结果直接生成散点图等

    Args:
        dataset_code:  单数据集编码
        dataset_codes: 多数据集编码（JOIN 模式，必须同时提供 sql 参数）
        file_id:       用户上传表格文件 ID（推荐）
        file_path:     文件路径或 MinIO object_name
        chart_type:    图表类型
        x_column:      X 轴列名
        y_columns:     Y 轴列名列表
        group_by:      分组/着色列名
        title:         图表标题
        sql:           先执行 SQL（支持 JOIN），再可视化结果
        sheet_name:    指定工作表（不填则用第一个表，多文件模式无效）
        max_rows:      最多取多少行参与绘图（默认 500）
        style:         matplotlib rcParams 覆盖
    """
    # ── 多文件 JOIN 模式（必须有 sql）────────────────────────────────
    if dataset_codes and len(dataset_codes) > 1:
        if not sql:
            return _error("多数据集可视化必须提供 sql 参数（JOIN 查询），用于指定要绘制的数据")
        file_paths, prefixes, err = await _resolve_multi_file_paths(dataset_codes)
        if err:
            return _error(err)
        try:
            from backend.application.tools.providers.excel_chart_generator import generate_chart, save_chart
            df = execute_multi_sql(file_paths, prefixes, sql + f" LIMIT {max_rows}")
            if df.empty:
                return _error("JOIN 查询结果为空，无法生成图表")
            label = " ⋈ ".join(dataset_codes)
            title_str = title or f"{label} — {chart_type} 图"
            num_cols = df.select_dtypes("number").columns.tolist()
            cat_cols = df.select_dtypes(exclude="number").columns.tolist()
            if not x_column and cat_cols:
                x_column = cat_cols[0]
            if not y_columns and num_cols:
                y_columns = num_cols[:2]
            png_bytes = generate_chart(df, chart_type=chart_type, x_col=x_column,
                                       y_cols=y_columns or [], group_by=group_by,
                                       title=title_str, style=style)
            chart_url = await save_chart(png_bytes, chart_id=uuid.uuid4().hex[:16])
            return (f"**跨数据集图表已生成** ✅\n\n"
                    f"- 数据集：{label}\n- 图表类型：{chart_type}\n"
                    f"- 数据行数：{len(df)}\n\n![{title_str}]({chart_url})\n")
        except Exception as e:
            logger.exception("[ExcelTool] visualize_excel_data 多文件失败")
            return _error(f"多文件图表生成时出错: {e}")

    # ── 单文件模式 ────────────────────────────────────────────────────
    effective_code = dataset_code or (dataset_codes[0] if dataset_codes else None)
    abs_path, label = await _resolve_file_path(effective_code, file_path, file_id)
    if not abs_path:
        return _error(label)

    try:
        from backend.application.tools.providers.excel_engine import get_schema
        from backend.application.tools.providers.excel_chart_generator import generate_chart, save_chart

        schema = get_schema(abs_path)
        if not schema:
            return _error("无法获取文件结构")

        # 获取数据
        if sql:
            df = execute_sql(abs_path, sql)
        else:
            table = sheet_name if sheet_name and sheet_name in schema else next(iter(schema))
            df = execute_sql(abs_path, f'SELECT * FROM "{table}" LIMIT {max_rows}')

        if df.empty:
            return _error("查询到的数据为空，无法生成图表")

        # 自动补全缺失的 x/y 配置
        num_cols = df.select_dtypes("number").columns.tolist()
        cat_cols = df.select_dtypes(exclude="number").columns.tolist()

        if not x_column and cat_cols:
            x_column = cat_cols[0]
        if not y_columns and num_cols:
            y_columns = num_cols[:2]

        # 生成图表
        title_str = title or f"{label} — {chart_type} 图"
        png_bytes = generate_chart(
            df,
            chart_type=chart_type,
            x_col=x_column,
            y_cols=y_columns or [],
            group_by=group_by,
            title=title_str,
            style=style,
        )

        # 保存并返回 API 代理 URL（不暴露 MinIO 直链）
        chart_url = await save_chart(png_bytes, chart_id=uuid.uuid4().hex[:16])
        logger.info("[ExcelTool] 图表已生成: url=%s", chart_url)

        output = f"**图表已生成** ✅\n\n"
        output += f"- 数据集：{label}\n"
        output += f"- 图表类型：{chart_type}\n"
        output += f"- 数据行数：{len(df)}\n"
        output += f"- 图表标题：{title_str}\n\n"
        output += f"![{title_str}]({chart_url})\n"

        return output

    except Exception as e:
        logger.exception("[ExcelTool] visualize_excel_data 失败")
        return _error(f"生成图表时出错: {e}")


# ── 工具集导出 ────────────────────────────────────────────────────────

def get_excel_analysis_tools() -> list:
    """返回所有 Excel 分析工具列表（供 ToolAssemblyPipeline 注入）"""
    return [
        list_experiment_datasets,
        inspect_excel_data,
        query_excel_data,
        summarize_excel_data,
        visualize_excel_data,
    ]
