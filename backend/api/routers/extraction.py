"""内容抽取路由"""

import uuid
from datetime import datetime
from enum import Enum
from typing import Any

from fastapi import APIRouter, Depends, HTTPException, Query
from pydantic import BaseModel

from backend.api.dependencies.auth import ApiKeyAuth, verify_api_key
from backend.config.loggers import get_logger


class ExtractionType(str, Enum):
    """抽取类型"""

    TEXT = "text"
    TABLE = "table"
    FORMULA = "formula"
    IMAGE = "image"
    ALL = "all"


class ExtractionMode(str, Enum):
    """抽取模式"""

    FAST = "fast"
    BALANCED = "balanced"
    ACCURATE = "accurate"


class ExtractionStatus(str, Enum):
    """抽取状态"""

    PENDING = "pending"
    RUNNING = "running"
    COMPLETED = "completed"
    FAILED = "failed"


class TextBlock(BaseModel):
    """文本块"""

    block_id: str
    text: str
    bbox: list[float] | None = None
    page_num: int | None = None
    confidence: float = 1.0


class TableBlock(BaseModel):
    """表格块"""

    block_id: str
    table_id: str | None = None
    html: str | None = None
    markdown: str | None = None
    cells: list[list[str]] | None = None
    bbox: list[float] | None = None
    page_num: int | None = None
    caption: str | None = None


class FormulaBlock(BaseModel):
    """公式块"""

    block_id: str
    latex: str
    is_inline: bool = False
    bbox: list[float] | None = None
    page_num: int | None = None
    confidence: float = 1.0


class ImageBlock(BaseModel):
    """图片块"""

    block_id: str
    image_id: str | None = None
    image_url: str | None = None
    local_path: str | None = None
    bbox: list[float] | None = None
    page_num: int | None = None
    caption: str | None = None
    image_type: str | None = None


class ExtractionResult(BaseModel):
    """抽取结果"""

    doc_id: str
    extraction_id: str
    status: ExtractionStatus
    document_type: str
    page_count: int

    text_blocks: list[TextBlock] = []
    table_blocks: list[TableBlock] = []
    formula_blocks: list[FormulaBlock] = []
    image_blocks: list[ImageBlock] = []

    total_text_blocks: int = 0
    total_tables: int = 0
    total_formulas: int = 0
    total_images: int = 0

    processing_time: float | None = None
    error: str | None = None


class ExtractionRequest(BaseModel):
    """抽取请求"""

    doc_id: str
    extraction_types: list[ExtractionType] = [ExtractionType.ALL]
    mode: ExtractionMode = ExtractionMode.BALANCED
    include_coords: bool = True
    include_confidence: bool = True


class BatchExtractionRequest(BaseModel):
    """批量抽取请求"""

    doc_ids: list[str]
    extraction_types: list[ExtractionType] = [ExtractionType.ALL]
    mode: ExtractionMode = ExtractionMode.BALANCED


class BatchExtractionResponse(BaseModel):
    """批量抽取响应"""

    batch_id: str
    total: int
    extractions: list[ExtractionResult]


router = APIRouter()
logger = get_logger(__name__)


EXTRACTION_STORE: dict = {}


@router.post("/extract", response_model=ExtractionResult)
async def extract_content(
    request: ExtractionRequest,
    auth: ApiKeyAuth = Depends(verify_api_key),
):
    """执行内容抽取"""
    logger.info(f"接收抽取请求: doc_id={request.doc_id}")

    extraction_id = str(uuid.uuid4())

    try:
        extraction_info = {
            "extraction_id": extraction_id,
            "doc_id": request.doc_id,
            "extraction_types": [t.value for t in request.extraction_types],
            "mode": request.mode.value,
            "status": ExtractionStatus.PENDING,
            "include_coords": request.include_coords,
            "include_confidence": request.include_confidence,
            "created_at": str(datetime.now()),
        }

        EXTRACTION_STORE[extraction_id] = extraction_info

        extraction_info["status"] = ExtractionStatus.RUNNING

        mock_result = generate_mock_extraction_result(request, extraction_id)

        extraction_info.update(mock_result)
        extraction_info["status"] = ExtractionStatus.COMPLETED

        logger.info(f"抽取完成: extraction_id={extraction_id}")

        return ExtractionResult(**extraction_info)

    except Exception as e:
        logger.exception(f"抽取失败: {e}")
        raise HTTPException(status_code=500, detail=f"内容抽取失败: {str(e)}")


@router.post("/batch", response_model=BatchExtractionResponse)
async def batch_extract_content(
    request: BatchExtractionRequest,
    auth: ApiKeyAuth = Depends(verify_api_key),
):
    """批量内容抽取"""
    logger.info(f"接收批量抽取请求: {len(request.doc_ids)} 个文档")

    batch_id = str(uuid.uuid4())
    extractions = []

    for doc_id in request.doc_ids:
        try:
            extraction_id = str(uuid.uuid4())

            extraction_info = {
                "extraction_id": extraction_id,
                "doc_id": doc_id,
                "extraction_types": [t.value for t in request.extraction_types],
                "mode": request.mode.value,
                "status": ExtractionStatus.RUNNING,
                "include_coords": request.include_coords,
                "include_confidence": request.include_confidence,
                "created_at": str(datetime.now()),
            }

            EXTRACTION_STORE[extraction_id] = extraction_info

            mock_result = generate_mock_extraction_result(
                ExtractionRequest(
                    doc_id=doc_id,
                    extraction_types=request.extraction_types,
                    mode=request.mode,
                ),
                extraction_id,
            )

            extraction_info.update(mock_result)
            extraction_info["status"] = ExtractionStatus.COMPLETED

            extractions.append(ExtractionResult(**extraction_info))

        except Exception as e:
            logger.exception(f"抽取文档 {doc_id} 失败: {e}")
            extractions.append(
                ExtractionResult(
                    doc_id=doc_id,
                    extraction_id="",
                    status=ExtractionStatus.FAILED,
                    document_type="unknown",
                    page_count=0,
                    error=str(e),
                )
            )

    return BatchExtractionResponse(
        batch_id=batch_id,
        total=len(request.doc_ids),
        extractions=extractions,
    )


@router.get("/results/{extraction_id}", response_model=ExtractionResult)
async def get_extraction_result(
    extraction_id: str,
    auth: ApiKeyAuth = Depends(verify_api_key),
):
    """获取抽取结果"""
    if extraction_id not in EXTRACTION_STORE:
        raise HTTPException(status_code=404, detail=f"抽取任务不存在: {extraction_id}")

    return ExtractionResult(**EXTRACTION_STORE[extraction_id])


@router.get("/results/{extraction_id}/text")
async def get_extracted_text(
    extraction_id: str,
    format: str = Query("text", pattern="^(text|markdown|json)$"),
    auth: ApiKeyAuth = Depends(verify_api_key),
):
    """获取抽取的文本内容"""
    if extraction_id not in EXTRACTION_STORE:
        raise HTTPException(status_code=404, detail=f"抽取任务不存在: {extraction_id}")

    extraction = EXTRACTION_STORE[extraction_id]

    if extraction["status"] != ExtractionStatus.COMPLETED:
        raise HTTPException(status_code=400, detail="抽取任务未完成")

    text_blocks = extraction.get("text_blocks", [])

    if format == "text":
        text_content = "\n\n".join([block["text"] for block in text_blocks])
        return {"content": text_content}

    elif format == "markdown":
        markdown_content = generate_markdown(text_blocks)
        return {"content": markdown_content}

    else:
        return {"text_blocks": text_blocks}


@router.get("/results/{extraction_id}/tables")
async def get_extracted_tables(
    extraction_id: str,
    format: str = Query("json", pattern="^(json|markdown|html)$"),
    auth: ApiKeyAuth = Depends(verify_api_key),
):
    """获取抽取的表格"""
    if extraction_id not in EXTRACTION_STORE:
        raise HTTPException(status_code=404, detail=f"抽取任务不存在: {extraction_id}")

    extraction = EXTRACTION_STORE[extraction_id]

    if extraction["status"] != ExtractionStatus.COMPLETED:
        raise HTTPException(status_code=400, detail="抽取任务未完成")

    table_blocks = extraction.get("table_blocks", [])

    return {
        "tables": table_blocks,
        "total": len(table_blocks),
    }


@router.get("/results/{extraction_id}/formulas")
async def get_extracted_formulas(
    extraction_id: str,
    format: str = Query("json", pattern="^(json|latex)$"),
    include_inline: bool = True,
    auth: ApiKeyAuth = Depends(verify_api_key),
):
    """获取抽取的公式"""
    if extraction_id not in EXTRACTION_STORE:
        raise HTTPException(status_code=404, detail=f"抽取任务不存在: {extraction_id}")

    extraction = EXTRACTION_STORE[extraction_id]

    if extraction["status"] != ExtractionStatus.COMPLETED:
        raise HTTPException(status_code=400, detail="抽取任务未完成")

    formula_blocks = extraction.get("formula_blocks", [])

    if not include_inline:
        formula_blocks = [f for f in formula_blocks if not f["is_inline"]]

    if format == "latex":
        latex_content = "\n\n".join([f["latex"] for f in formula_blocks])
        return {"content": latex_content}

    return {
        "formulas": formula_blocks,
        "total": len(formula_blocks),
    }


@router.get("/results/{extraction_id}/images")
async def get_extracted_images(
    extraction_id: str,
    include_metadata: bool = True,
    auth: ApiKeyAuth = Depends(verify_api_key),
):
    """获取抽取的图片"""
    if extraction_id not in EXTRACTION_STORE:
        raise HTTPException(status_code=404, detail=f"抽取任务不存在: {extraction_id}")

    extraction = EXTRACTION_STORE[extraction_id]

    if extraction["status"] != ExtractionStatus.COMPLETED:
        raise HTTPException(status_code=400, detail="抽取任务未完成")

    image_blocks = extraction.get("image_blocks", [])

    return {
        "images": image_blocks,
        "total": len(image_blocks),
    }


@router.delete("/results/{extraction_id}")
async def delete_extraction_result(
    extraction_id: str,
    auth: ApiKeyAuth = Depends(verify_api_key),
):
    """删除抽取结果"""
    if extraction_id not in EXTRACTION_STORE:
        raise HTTPException(status_code=404, detail=f"抽取任务不存在: {extraction_id}")

    EXTRACTION_STORE.pop(extraction_id)

    return {"message": "抽取结果已删除"}


def generate_mock_extraction_result(
    request: ExtractionRequest,
    extraction_id: str,
) -> dict[str, Any]:
    """生成模拟抽取结果（实际实现时替换为真实抽取逻辑）"""

    return {
        "extraction_id": extraction_id,
        "doc_id": request.doc_id,
        "status": ExtractionStatus.COMPLETED,
        "document_type": "pdf",
        "page_count": 5,
        "text_blocks": [
            TextBlock(
                block_id=str(uuid.uuid4()),
                text="这是一段示例文本，用于测试抽取功能。",
                bbox=[100, 100, 400, 150],
                page_num=1,
                confidence=0.95,
            ).model_dump()
        ],
        "table_blocks": [
            TableBlock(
                block_id=str(uuid.uuid4()),
                table_id="table-1",
                markdown="| 列1 | 列2 |\n|------|------|\n| 数据1 | 数据2 |",
                bbox=[100, 200, 400, 300],
                page_num=1,
                caption="示例表格",
            ).model_dump()
        ],
        "formula_blocks": [
            FormulaBlock(
                block_id=str(uuid.uuid4()),
                latex="E = mc^2",
                is_inline=False,
                bbox=[100, 350, 200, 380],
                page_num=1,
                confidence=0.90,
            ).model_dump()
        ],
        "image_blocks": [
            ImageBlock(
                block_id=str(uuid.uuid4()),
                image_id="image-1",
                image_url="http://example.com/image.png",
                bbox=[100, 400, 300, 500],
                page_num=1,
                caption="示例图片",
                image_type="流程图",
            ).model_dump()
        ],
        "total_text_blocks": 1,
        "total_tables": 1,
        "total_formulas": 1,
        "total_images": 1,
        "processing_time": 1.5,
    }


def generate_markdown(text_blocks: list[dict[str, Any]]) -> str:
    """生成 Markdown 格式的文本"""
    lines = []
    for block in text_blocks:
        lines.append(block.get("text", ""))
    return "\n\n".join(lines)
