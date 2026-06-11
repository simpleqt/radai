"""实验数据集管理 API

数据集与智能体绑定策略（与 agent_kb_bindings 对齐）：
  - agent_codes 为空列表 → 全局可见（所有 agent 均可使用）
  - agent_codes 非空 → 仅对列表内的 agent_code 可见

文件存储策略：
  - 用户上传 → 优先存 MinIO（storage_type="minio"），失败降级本地目录
  - 预置数据集 → 由 init_datasets.py 上传 MinIO，本地保留缓存供 DuckDB 读取

路由列表：
  GET    /api/v1/datasets                     列出数据集（支持 domain/agent_code 过滤）
  POST   /api/v1/datasets                     注册已有文件为数据集
  POST   /api/v1/datasets/upload              上传 Excel/CSV 并注册（存 MinIO）
  GET    /api/v1/datasets/{id}                数据集详情
  DELETE /api/v1/datasets/{id}               删除数据集（可选同时删除文件）
  GET    /api/v1/datasets/{id}/preview        数据预览（前 N 行）
  POST   /api/v1/datasets/{id}/refresh        重新解析 schema
  PUT    /api/v1/datasets/{id}/agents         设置绑定的智能体（覆盖）
  GET    /api/v1/datasets/by-agent/{code}     获取指定智能体可见的数据集
"""

from __future__ import annotations

import json as _json
import os
import uuid
from pathlib import Path
from typing import Any, List, Optional

from fastapi import APIRouter, Depends, File, Form, HTTPException, UploadFile
from pydantic import BaseModel, ConfigDict

from backend.api.dependencies.auth import ApiKeyAuth, verify_api_key as get_api_key_auth
from backend.config.loggers import get_logger
from backend.db.connection import get_async_session_factory

logger = get_logger(__name__)
router = APIRouter()

_DATASETS_DIR = Path(__file__).resolve().parents[2] / "datasets"
_DATASETS_DIR.mkdir(parents=True, exist_ok=True)

_ALLOWED_EXTENSIONS = {".xlsx", ".xls", ".csv"}
_MAX_FILE_SIZE = 100 * 1024 * 1024  # 100 MB
_MINIO_DATASET_PREFIX = "datasets/uploads"


# ── Pydantic 模型 ─────────────────────────────────────────────────────

class DatasetCreateRequest(BaseModel):
    code: str
    name: str
    description: Optional[str] = None
    file_path: str
    file_type: str = "xlsx"
    storage_type: str = "local"
    domain: Optional[str] = None
    tags: List[str] = []
    agent_codes: List[str] = []
    is_public: bool = True


class SetAgentCodesRequest(BaseModel):
    agent_codes: List[str]


def _to_response(ds: Any) -> dict:
    return {
        "id": ds.id,
        "code": ds.code,
        "name": ds.name,
        "description": ds.description or "",
        "file_type": ds.file_type or "xlsx",
        "file_size": ds.file_size,
        "storage_type": ds.storage_type or "local",
        "sheet_names": ds.sheet_names or [],
        "row_counts": ds.row_counts or {},
        "column_schema": ds.column_schema or {},
        "domain": ds.domain or "",
        "tags": ds.tags or [],
        "agent_codes": ds.agent_codes or [],
        "is_public": ds.is_public,
        "status": ds.status,
        "created_at": ds.created_at.isoformat() if ds.created_at else None,
    }


# ── 内部辅助 ──────────────────────────────────────────────────────────

def _parse_schema(file_path: str) -> dict:
    """解析文件 schema（本地路径）"""
    try:
        import pandas as pd

        file_size = os.path.getsize(file_path)
        ext = Path(file_path).suffix.lower()

        if ext == ".csv":
            df = pd.read_csv(file_path, nrows=1000)
            sheets = {"data": df}
        else:
            sheets = pd.read_excel(file_path, sheet_name=None, nrows=1000)

        sheet_names = list(sheets.keys())
        column_schema: dict = {}
        row_counts: dict = {}

        for sheet_name, df in sheets.items():
            row_counts[sheet_name] = len(df)
            cols = []
            for col in df.columns:
                sample = [str(v) for v in df[col].dropna().head(3).tolist()]
                cols.append({
                    "name": str(col),
                    "dtype": str(df[col].dtype),
                    "non_null": int(df[col].notna().sum()),
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
        logger.warning("[DatasetsAPI] schema 解析失败: %s", e)
        return {}


async def _upload_content_to_minio(content: bytes, filename: str, ext: str) -> Optional[str]:
    """上传文件内容到 MinIO，返回 object_name；失败返回 None"""
    try:
        from backend.storage import get_user_minio_bucket_name, upload_bytes_to_minio

        safe_name = f"{uuid.uuid4().hex}{ext}"
        object_name = f"{_MINIO_DATASET_PREFIX}/{safe_name}"
        bucket = get_user_minio_bucket_name()

        result = await upload_bytes_to_minio(
            content,
            object_name,
            content_type="application/vnd.openxmlformats-officedocument.spreadsheetml.sheet",
            bucket_name=bucket,
        )
        if result:
            logger.info("[DatasetsAPI] MinIO 上传成功: %s", object_name)
            return object_name
    except Exception as e:
        logger.warning("[DatasetsAPI] MinIO 上传失败: %s", e)
    return None


def _save_local_fallback(content: bytes, ext: str) -> str:
    """MinIO 上传失败时保存到本地 datasets/ 目录，返回绝对路径"""
    safe_name = f"{uuid.uuid4().hex}{ext}"
    dest = _DATASETS_DIR / safe_name
    dest.write_bytes(content)
    logger.info("[DatasetsAPI] 文件已保存到本地（MinIO 降级）: %s", dest)
    return str(dest)


def _get_local_file(ds: Any) -> Optional[str]:
    """获取数据集的本地可读路径（用于 preview 和 schema 解析）"""
    if ds.storage_type == "local" and Path(ds.file_path).exists():
        return ds.file_path
    # MinIO 路径：尝试从本地缓存获取
    try:
        from backend.application.tools.providers.excel_engine import _resolve_local_path
        return _resolve_local_path(ds.file_path)
    except Exception:
        return None


# ── 路由实现 ──────────────────────────────────────────────────────────

@router.get("/datasets", summary="列出实验数据集")
async def list_datasets(
    domain: Optional[str] = None,
    agent_code: Optional[str] = None,
    status: str = "active",
    auth: ApiKeyAuth = Depends(get_api_key_auth),
):
    """列出数据集，支持按领域和智能体过滤。
    
    - agent_code: 只返回该智能体可见的数据集（agent_codes 为空 = 全局可见）
    """
    from backend.db.crud.dataset_crud import DatasetCRUD

    factory = get_async_session_factory()
    async with factory() as session:
        datasets = await DatasetCRUD.list_all(
            session,
            domain=domain,
            agent_code=agent_code,
            status=status,
            is_public=True if auth.role != "admin" else None,
        )
    return {"total": len(datasets), "datasets": [_to_response(ds) for ds in datasets]}


@router.get("/datasets/by-agent/{agent_code}", summary="获取指定智能体可见的数据集")
async def list_datasets_by_agent(
    agent_code: str,
    status: str = "active",
    auth: ApiKeyAuth = Depends(get_api_key_auth),
):
    """返回 agent_code 对应智能体可见的所有数据集（全局 + 专属）"""
    from backend.db.crud.dataset_crud import DatasetCRUD

    factory = get_async_session_factory()
    async with factory() as session:
        datasets = await DatasetCRUD.list_all(
            session, agent_code=agent_code, status=status, is_public=True,
        )
    return {"agent_code": agent_code, "total": len(datasets), "datasets": [_to_response(ds) for ds in datasets]}


@router.get("/datasets/{dataset_id}", summary="获取数据集详情")
async def get_dataset(
    dataset_id: str,
    auth: ApiKeyAuth = Depends(get_api_key_auth),
):
    from backend.db.crud.dataset_crud import DatasetCRUD

    factory = get_async_session_factory()
    async with factory() as session:
        ds = await DatasetCRUD.get_by_id(session, dataset_id)
    if not ds:
        raise HTTPException(status_code=404, detail="数据集不存在")
    return _to_response(ds)


@router.post("/datasets", summary="注册新数据集（引用已有本地/MinIO 文件）")
async def create_dataset(
    req: DatasetCreateRequest,
    auth: ApiKeyAuth = Depends(get_api_key_auth),
):
    from backend.db.crud.dataset_crud import DatasetCRUD

    factory = get_async_session_factory()
    async with factory() as session:
        existing = await DatasetCRUD.get_by_code(session, req.code)
        if existing:
            raise HTTPException(status_code=409, detail=f"数据集编码已存在: {req.code}")

        ds = await DatasetCRUD.create(
            session,
            code=req.code,
            name=req.name,
            file_path=req.file_path,
            description=req.description,
            file_type=req.file_type,
            storage_type=req.storage_type,
            domain=req.domain,
            tags=req.tags,
            agent_codes=req.agent_codes,
            is_public=req.is_public,
            created_by=auth.user_id,
        )

        # 解析 schema（本地文件才能解析）
        if req.storage_type == "local":
            schema = _parse_schema(req.file_path)
            if schema:
                await DatasetCRUD.update_metadata(
                    session, ds.id,
                    sheet_names=schema.get("sheet_names"),
                    column_schema=schema.get("column_schema"),
                    row_counts=schema.get("row_counts"),
                    file_size=schema.get("file_size"),
                )
                await session.refresh(ds)

    return _to_response(ds)


@router.post("/datasets/upload", summary="上传 Excel/CSV 文件并注册数据集（优先存 MinIO）")
async def upload_dataset(
    file: UploadFile = File(...),
    code: str = Form(...),
    name: str = Form(...),
    description: str = Form(""),
    domain: str = Form(""),
    tags: str = Form("[]"),
    agent_codes: str = Form("[]"),
    is_public: bool = Form(True),
    auth: ApiKeyAuth = Depends(get_api_key_auth),
):
    """上传 Excel/CSV 数据集文件：
    1. 文件优先上传到 MinIO（storage_type=minio）
    2. MinIO 不可用时降级保存到服务器本地 datasets/ 目录（storage_type=local）
    3. 自动解析并存储表结构 schema
    """
    from backend.db.crud.dataset_crud import DatasetCRUD

    ext = Path(file.filename or "").suffix.lower()
    if ext not in _ALLOWED_EXTENSIONS:
        raise HTTPException(status_code=400, detail=f"不支持的文件类型: {ext}，仅支持 xlsx/xls/csv")

    content = await file.read()
    if len(content) > _MAX_FILE_SIZE:
        raise HTTPException(status_code=413, detail="文件过大，最大支持 100MB")

    try:
        tags_list = _json.loads(tags) if tags.strip() else []
    except Exception:
        tags_list = [t.strip() for t in tags.split(",") if t.strip()]

    try:
        agent_codes_list = _json.loads(agent_codes) if agent_codes.strip() else []
    except Exception:
        agent_codes_list = [c.strip() for c in agent_codes.split(",") if c.strip()]

    # 优先存 MinIO
    minio_object = await _upload_content_to_minio(content, file.filename or "", ext)
    if minio_object:
        stored_path = minio_object
        storage_type = "minio"
    else:
        stored_path = _save_local_fallback(content, ext)
        storage_type = "local"

    factory = get_async_session_factory()
    async with factory() as session:
        existing = await DatasetCRUD.get_by_code(session, code)
        if existing:
            raise HTTPException(status_code=409, detail=f"数据集编码已存在: {code}")

        ds = await DatasetCRUD.create(
            session,
            code=code,
            name=name,
            file_path=stored_path,
            description=description or None,
            file_type=ext.lstrip("."),
            storage_type=storage_type,
            domain=domain or None,
            tags=tags_list,
            agent_codes=agent_codes_list,
            is_public=is_public,
            created_by=auth.user_id,
        )

        # 解析 schema（本地文件直接解析；MinIO 文件写临时文件后解析）
        try:
            if storage_type == "local":
                schema = _parse_schema(stored_path)
            else:
                import tempfile
                with tempfile.NamedTemporaryFile(suffix=ext, delete=False) as tmp:
                    tmp.write(content)
                    tmp_path = tmp.name
                schema = _parse_schema(tmp_path)
                Path(tmp_path).unlink(missing_ok=True)

            if schema:
                await DatasetCRUD.update_metadata(
                    session, ds.id,
                    sheet_names=schema.get("sheet_names"),
                    column_schema=schema.get("column_schema"),
                    row_counts=schema.get("row_counts"),
                    file_size=schema.get("file_size"),
                )
                await session.refresh(ds)
        except Exception as e:
            logger.warning("[DatasetsAPI] schema 解析失败（不影响注册）: %s", e)

    logger.info("[DatasetsAPI] 上传成功: code=%s, storage=%s", code, storage_type)
    return _to_response(ds)


@router.get("/datasets/{dataset_id}/preview", summary="预览数据集（前 N 行）")
async def preview_dataset(
    dataset_id: str,
    sheet: Optional[str] = None,
    rows: int = 20,
    auth: ApiKeyAuth = Depends(get_api_key_auth),
):
    import pandas as pd
    from backend.db.crud.dataset_crud import DatasetCRUD

    factory = get_async_session_factory()
    async with factory() as session:
        ds = await DatasetCRUD.get_by_id(session, dataset_id)
    if not ds:
        raise HTTPException(status_code=404, detail="数据集不存在")

    local_path = _get_local_file(ds)
    if not local_path:
        raise HTTPException(status_code=404, detail="数据集文件不可访问（MinIO 下载失败或本地文件不存在）")

    rows = min(rows, 200)
    try:
        ext = Path(local_path).suffix.lower()
        if ext == ".csv":
            df = pd.read_csv(local_path, nrows=rows)
            sheet_data = {"data": df.to_dict(orient="records")}
        else:
            sheets = pd.read_excel(local_path, sheet_name=None, nrows=rows)
            if sheet and sheet in sheets:
                sheet_data = {sheet: sheets[sheet].to_dict(orient="records")}
            else:
                sheet_data = {k: v.to_dict(orient="records") for k, v in sheets.items()}
    except Exception as e:
        raise HTTPException(status_code=500, detail=f"读取文件失败: {e}")

    return {
        "dataset_id": dataset_id,
        "name": ds.name,
        "storage_type": ds.storage_type,
        "rows_preview": rows,
        "sheets": sheet_data,
    }


@router.post("/datasets/{dataset_id}/refresh", summary="重新解析数据集 schema")
async def refresh_dataset_schema(
    dataset_id: str,
    auth: ApiKeyAuth = Depends(get_api_key_auth),
):
    from backend.db.crud.dataset_crud import DatasetCRUD
    from backend.application.tools.providers.excel_engine import invalidate_cache

    factory = get_async_session_factory()
    async with factory() as session:
        ds = await DatasetCRUD.get_by_id(session, dataset_id)
        if not ds:
            raise HTTPException(status_code=404, detail="数据集不存在")

        try:
            invalidate_cache(ds.file_path)
        except Exception:
            pass

        local_path = _get_local_file(ds)
        if not local_path:
            raise HTTPException(status_code=404, detail="数据集文件不可访问")

        schema = _parse_schema(local_path)
        if not schema:
            raise HTTPException(status_code=500, detail="schema 解析失败")

        await DatasetCRUD.update_metadata(
            session, ds.id,
            sheet_names=schema.get("sheet_names"),
            column_schema=schema.get("column_schema"),
            row_counts=schema.get("row_counts"),
            file_size=schema.get("file_size"),
        )
        await session.refresh(ds)

    return {"message": "schema 已刷新", "dataset": _to_response(ds)}


@router.put("/datasets/{dataset_id}/agents", summary="设置数据集绑定的智能体（覆盖）")
async def set_dataset_agents(
    dataset_id: str,
    req: SetAgentCodesRequest,
    auth: ApiKeyAuth = Depends(get_api_key_auth),
):
    """设置数据集对哪些智能体可见。
    
    - agent_codes 为空列表 → 全局可见（所有 agent 均可使用）
    - agent_codes 非空 → 仅对列表内 agent_code 对应的智能体可见

    示例: {"agent_codes": ["csh_full_feature"]} 仅 CSH 智能体可见
    """
    from backend.db.crud.dataset_crud import DatasetCRUD

    factory = get_async_session_factory()
    async with factory() as session:
        ds = await DatasetCRUD.set_agent_codes(session, dataset_id, req.agent_codes)
    if not ds:
        raise HTTPException(status_code=404, detail="数据集不存在")
    return _to_response(ds)


@router.delete("/datasets/{dataset_id}", summary="删除数据集（可选删除文件）")
async def delete_dataset(
    dataset_id: str,
    delete_file: bool = False,
    auth: ApiKeyAuth = Depends(get_api_key_auth),
):
    """物理删除数据集记录。
    
    - delete_file=true 时同时删除对应文件（本地文件或 MinIO 对象）
    - 建议先确认无 Agent 正在使用该数据集
    """
    from backend.db.crud.dataset_crud import DatasetCRUD

    factory = get_async_session_factory()
    async with factory() as session:
        ds = await DatasetCRUD.get_by_id(session, dataset_id)
        if not ds:
            raise HTTPException(status_code=404, detail="数据集不存在")

        file_path = ds.file_path
        storage_type = ds.storage_type or "local"
        ok = await DatasetCRUD.delete(session, dataset_id)

    if ok and delete_file:
        try:
            if storage_type == "minio":
                from backend.storage import get_user_minio_bucket_name
                from minio import Minio
                from backend.config.settings import settings
                client = Minio(
                    settings.minio_endpoint,
                    access_key=settings.minio_access_key,
                    secret_key=settings.minio_secret_key,
                    secure=getattr(settings, "minio_secure", False),
                )
                bucket = get_user_minio_bucket_name()
                client.remove_object(bucket, file_path)
                logger.info("[DatasetsAPI] MinIO 对象已删除: %s", file_path)
            else:
                Path(file_path).unlink(missing_ok=True)
                logger.info("[DatasetsAPI] 本地文件已删除: %s", file_path)
        except Exception as e:
            logger.warning("[DatasetsAPI] 文件删除失败（记录已删除）: %s", e)

    return {"message": "数据集已删除", "id": dataset_id}
