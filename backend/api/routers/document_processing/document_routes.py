"""文档信息路由"""

from datetime import datetime

from fastapi import APIRouter, Depends, HTTPException, Query
from sqlalchemy.ext.asyncio import AsyncSession

from backend.api.dependencies.auth import ApiKeyAuth, get_db_session, verify_api_key
from backend.api.routers.document_processing.models import (
    DocumentInfo,
    DocumentType,
)
from backend.config.loggers import get_logger
from backend.db.crud.document_crud import document_crud

router = APIRouter()
logger = get_logger(__name__)


@router.get("/{doc_id}", response_model=DocumentInfo)
async def get_document_info(
    doc_id: str,
    auth: ApiKeyAuth = Depends(verify_api_key),
    session: AsyncSession = Depends(get_db_session),
):
    """获取文档信息"""
    doc = await document_crud.get_by_id(session, doc_id)

    if not doc:
        raise HTTPException(status_code=404, detail=f"文档不存在: {doc_id}")

    if doc.user_id != auth.user_id:
        raise HTTPException(status_code=403, detail="无权访问此文档")

    return DocumentInfo(
        doc_id=doc.doc_id,
        filename=doc.filename,
        document_type=DocumentType(doc.document_type),
        file_size=doc.file_size,
        upload_time=(
            doc.upload_time.isoformat()
            if isinstance(doc.upload_time, datetime)
            else str(doc.upload_time)
        ),
        status=doc.status,
    )


@router.get("/", response_model=list[DocumentInfo])
async def list_documents(
    status: str | None = Query(None),
    page: int = Query(1, ge=1),
    page_size: int = Query(20, ge=1, le=100),
    auth: ApiKeyAuth = Depends(verify_api_key),
    session: AsyncSession = Depends(get_db_session),
):
    """获取文档列表"""
    docs = await document_crud.get_by_user(
        session=session,
        user_id=auth.user_id,
        status=status,
        page=page,
        page_size=page_size,
    )

    return [
        DocumentInfo(
            doc_id=doc.doc_id,
            filename=doc.filename,
            document_type=DocumentType(doc.document_type),
            file_size=doc.file_size,
            upload_time=(
                doc.upload_time.isoformat()
                if isinstance(doc.upload_time, datetime)
                else str(doc.upload_time)
            ),
            status=doc.status,
        )
        for doc in docs
    ]
