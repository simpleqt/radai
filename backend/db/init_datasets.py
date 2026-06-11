"""实验数据集表初始化与预注册脚本

负责：
1. 创建 experiment_datasets 表
2. 将服务器上已有的实验数据集文件预注册到数据库
3. 自动解析文件 schema（工作表名、列定义、行数）
"""

from __future__ import annotations

import asyncio
import os
from pathlib import Path

from sqlalchemy import text

from backend.config.loggers import get_logger
from backend.db.connection import get_async_engine, get_async_session_factory

logger = get_logger(__name__)

# 服务器预置数据集定义
# agent_codes 为空列表表示对所有 agent 可见；填写 agent_code 则只对指定 agent 开放
DEFAULT_DATASETS = [
    {
        "code": "biochar_degradation",
        "name": "生物炭制备条件与反应体系条件对污染物降解性能的影响",
        "description": (
            "包含不同热解温度、生物炭种类、催化剂浓度、pH 等制备与反应条件下，"
            "目标污染物降解效率的实验数据。适用于多因素相关性分析、条件优化、"
            "降解性能对比等研究场景。"
        ),
        "file_path": "datasets/数据集1_生物炭制备条件与反应体系条件对污染物降解性能的影响.xlsx",
        "file_type": "xlsx",
        "domain": "environmental",
        "tags": ["生物炭", "污染物降解", "高级氧化", "催化", "AOPs", "热解"],
        # 绑定到 CSH 智能体（场地污染/水文地质方向使用）
        # 若将来 NE 智能体也需要使用，追加 "ne_full_feature" 即可
        "agent_codes": ["csh_full_feature"],
    },
    {
        "code": "hydroxyl_radical_rates",
        "name": "不同污染物与羟基自由基的反应速率",
        "description": (
            "收录不同有机污染物与羟基自由基（·OH）的二级反应速率常数，"
            "用于评估高级氧化工艺对目标污染物的去除潜力及反应动力学比较。"
        ),
        "file_path": "datasets/数据集2_不同污染物与羟基自由基的反应速率0507.xlsx",
        "file_type": "xlsx",
        "domain": "environmental",
        "tags": ["羟基自由基", "反应速率", "AOPs", "动力学", "有机污染物", "·OH"],
        # 绑定到 CSH 智能体
        "agent_codes": ["csh_full_feature"],
    },
]


async def init_dataset_table() -> None:
    """创建 experiment_datasets 表（IF NOT EXISTS）"""
    engine = get_async_engine()
    async with engine.begin() as conn:
        await conn.execute(text("""
            CREATE TABLE IF NOT EXISTS experiment_datasets (
                id VARCHAR(36) PRIMARY KEY,
                code VARCHAR(100) UNIQUE NOT NULL,
                name VARCHAR(255) NOT NULL,
                description TEXT,
                file_path VARCHAR(512) NOT NULL,
                file_type VARCHAR(10) DEFAULT 'xlsx',
                file_size BIGINT,
                storage_type VARCHAR(20) DEFAULT 'local',
                sheet_names JSON,
                column_schema JSON,
                row_counts JSON,
                domain VARCHAR(50),
                tags JSON,
                agent_codes JSON COMMENT '绑定的智能体编码列表，为空则全局可见',
                is_public TINYINT(1) DEFAULT 1,
                created_by VARCHAR(36),
                status VARCHAR(20) DEFAULT 'active',
                created_at DATETIME DEFAULT CURRENT_TIMESTAMP,
                updated_at DATETIME DEFAULT CURRENT_TIMESTAMP ON UPDATE CURRENT_TIMESTAMP,
                INDEX idx_dataset_code (code),
                INDEX idx_dataset_domain (domain),
                INDEX idx_dataset_status (status)
            ) ENGINE=InnoDB DEFAULT CHARSET=utf8mb4 COMMENT='实验数据集注册表'
        """))
        # 为已存在的旧表补列（幂等）
        try:
            await conn.execute(text(
                "ALTER TABLE experiment_datasets ADD COLUMN IF NOT EXISTS "
                "agent_codes JSON COMMENT '绑定的智能体编码列表'"
            ))
        except Exception:
            pass
        logger.info("[数据集初始化] experiment_datasets 表创建/确认成功")


async def _upload_to_minio(local_path: str) -> str | None:
    """将本地文件上传到 MinIO，返回 object_name；失败返回 None"""
    try:
        from pathlib import Path
        from backend.storage import get_user_minio_bucket_name, upload_bytes_to_minio

        content = Path(local_path).read_bytes()
        filename = Path(local_path).name
        object_name = f"datasets/prebuilt/{filename}"
        bucket = get_user_minio_bucket_name()
        result = await upload_bytes_to_minio(
            content, object_name, content_type="application/vnd.openxmlformats-officedocument.spreadsheetml.sheet",
            bucket_name=bucket,
        )
        if result:
            logger.info("[数据集初始化] MinIO 上传成功: %s", object_name)
            return object_name
    except Exception as e:
        logger.warning("[数据集初始化] MinIO 上传失败，降级本地: %s", e)
    return None


def _resolve_file_path(relative_path: str) -> str | None:
    """将相对路径解析为绝对路径，查找数据集文件"""
    # 项目根目录
    project_root = Path(__file__).resolve().parents[2]
    # backend 目录
    backend_root = Path(__file__).resolve().parent

    candidates = [
        backend_root / relative_path,
        project_root / relative_path,
        Path(relative_path),  # 直接当绝对路径
    ]
    for candidate in candidates:
        if candidate.exists():
            return str(candidate.resolve())
    return None


def _parse_excel_schema(file_path: str) -> dict:
    """解析 Excel 文件结构，返回 {sheet_names, column_schema, row_counts, file_size}"""
    try:
        import pandas as pd

        file_size = os.path.getsize(file_path)
        all_sheets = pd.read_excel(file_path, sheet_name=None, nrows=1000)

        sheet_names = list(all_sheets.keys())
        column_schema: dict = {}
        row_counts: dict = {}

        for sheet_name, df in all_sheets.items():
            row_counts[sheet_name] = len(df)
            cols = []
            for col in df.columns:
                dtype = str(df[col].dtype)
                non_null = int(df[col].notna().sum())
                # 收集非空样本值（最多 3 个）
                sample_vals = df[col].dropna().head(3).tolist()
                sample = [str(v) for v in sample_vals]
                cols.append({
                    "name": str(col),
                    "dtype": dtype,
                    "non_null": non_null,
                    "total": len(df),
                    "sample": sample,
                })
            column_schema[sheet_name] = cols

        return {
            "sheet_names": sheet_names,
            "column_schema": column_schema,
            "row_counts": row_counts,
            "file_size": file_size,
        }
    except Exception as e:
        logger.warning("[数据集初始化] 解析文件 schema 失败: %s, error=%s", file_path, e)
        return {}


async def register_default_datasets() -> None:
    """预注册默认实验数据集"""
    from backend.db.crud.dataset_crud import DatasetCRUD

    factory = get_async_session_factory()
    async with factory() as session:
        for ds_def in DEFAULT_DATASETS:
            code = ds_def["code"]

            # 已存在则跳过
            existing = await DatasetCRUD.get_by_code(session, code)
            if existing:
                logger.info("[数据集初始化] 数据集已存在，跳过: code=%s", code)
                continue

            # 解析文件路径
            abs_path = _resolve_file_path(ds_def["file_path"])
            if abs_path is None:
                logger.warning(
                    "[数据集初始化] 文件未找到，跳过注册: code=%s, path=%s",
                    code, ds_def["file_path"],
                )
                continue

            # 尝试上传到 MinIO（失败则降级为本地存储）
            minio_object = await _upload_to_minio(abs_path)
            storage_type = "minio" if minio_object else "local"
            stored_path = minio_object if minio_object else abs_path

            # 创建记录
            ds = await DatasetCRUD.create(
                session,
                code=code,
                name=ds_def["name"],
                file_path=stored_path,
                description=ds_def.get("description"),
                file_type=ds_def.get("file_type", "xlsx"),
                storage_type=storage_type,
                domain=ds_def.get("domain"),
                tags=ds_def.get("tags", []),
                agent_codes=ds_def.get("agent_codes", []),
                is_public=True,
            )
            logger.info(
                "[数据集初始化] 已注册: code=%s, storage=%s, path=%s",
                code, storage_type, stored_path,
            )
            logger.info("[数据集初始化] 已注册数据集: code=%s, id=%s", code, ds.id)

            # 异步解析 schema 并更新
            try:
                schema = _parse_excel_schema(abs_path)
                if schema:
                    await DatasetCRUD.update_metadata(
                        session,
                        ds.id,
                        sheet_names=schema.get("sheet_names"),
                        column_schema=schema.get("column_schema"),
                        row_counts=schema.get("row_counts"),
                        file_size=schema.get("file_size"),
                        status="active",
                    )
                    logger.info(
                        "[数据集初始化] Schema 解析完成: code=%s, sheets=%s",
                        code, schema.get("sheet_names"),
                    )
            except Exception as e:
                logger.warning("[数据集初始化] Schema 解析异常: code=%s, error=%s", code, e)


async def main() -> None:
    print("=" * 50)
    print("实验数据集初始化")
    print("=" * 50)
    await init_dataset_table()
    await register_default_datasets()
    print("\n完成！")


if __name__ == "__main__":
    asyncio.run(main())
