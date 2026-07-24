"""Unified prompt template management APIs."""

from __future__ import annotations

import asyncio
from typing import Any, Literal

from fastapi import APIRouter, Depends, HTTPException, Query
from pydantic import BaseModel, Field

from backend.api.dependencies.auth import ApiKeyAuth, require_admin

router = APIRouter(prefix="/prompts", tags=["提示词管理"])

PromptCategory = Literal["extraction_schema"]


class PromptTemplateSummary(BaseModel):
    """Prompt template list item for admin UI."""

    category: PromptCategory
    template_id: str
    label: str
    subject: str | None = None
    version: str
    template_version: str
    content_format: Literal["yaml", "text"] = "yaml"
    description: str | None = None
    metadata: dict[str, Any] = Field(default_factory=dict)


class PromptTemplateDetail(PromptTemplateSummary):
    """Prompt template detail with editable content."""

    content: str


class PromptTemplateCreateRequest(BaseModel):
    """Create a prompt template."""

    category: PromptCategory
    template_id: str = Field(..., min_length=2, max_length=64)
    label: str = Field(..., min_length=1, max_length=80)
    subject: str | None = Field(None, max_length=80)
    version: str = Field("1.0", max_length=20)
    content: str | None = None


class PromptTemplateUpdateRequest(BaseModel):
    """Update prompt template content."""

    content: str = Field(..., min_length=1)


def _schema_summary(item: dict[str, Any]) -> PromptTemplateSummary:
    return PromptTemplateSummary(
        category="extraction_schema",
        template_id=item["schema_id"],
        label=item["label"],
        subject=item.get("subject"),
        version=item["version"],
        template_version=item["schema_version"],
        content_format="yaml",
        description="自研入库实体/关系抽取 Schema，用于生成 LLM 抽取提示词。",
        metadata={
            "entity_type_count": item.get("entity_type_count", 0),
            "relation_type_count": item.get("relation_type_count", 0),
        },
    )


def _schema_detail(item: dict[str, Any]) -> PromptTemplateDetail:
    summary = _schema_summary(item)
    return PromptTemplateDetail(**summary.model_dump(), content=item["raw_yaml"])


@router.get("/categories")
async def list_prompt_categories(
    auth: ApiKeyAuth = Depends(require_admin),
) -> list[dict[str, str]]:
    """List prompt template categories supported by the unified manager."""
    return [
        {
            "category": "extraction_schema",
            "label": "入库抽取 Schema",
            "description": "管理自研入库的实体/关系抽取提示词模板。",
        }
    ]


@router.get("/templates", response_model=list[PromptTemplateSummary])
async def list_prompt_templates(
    category: PromptCategory | None = Query(None),
    auth: ApiKeyAuth = Depends(require_admin),
):
    """List prompt templates across supported categories."""
    from backend.rag.ingestion.schema import list_extraction_schemas

    items: list[PromptTemplateSummary] = []
    if category in (None, "extraction_schema"):
        schemas = await asyncio.to_thread(list_extraction_schemas)
        items.extend(_schema_summary(item) for item in schemas)
    return items


@router.get("/templates/{category}/{template_id}", response_model=PromptTemplateDetail)
async def get_prompt_template(
    category: PromptCategory,
    template_id: str,
    auth: ApiKeyAuth = Depends(require_admin),
):
    """Get one editable prompt template."""
    if category == "extraction_schema":
        from backend.rag.ingestion.schema import get_extraction_schema

        try:
            schema = await asyncio.to_thread(get_extraction_schema, template_id)
            return _schema_detail(schema)
        except FileNotFoundError:
            raise HTTPException(status_code=404, detail="提示词模板不存在") from None
        except ValueError as exc:
            raise HTTPException(status_code=400, detail=str(exc)) from exc
    raise HTTPException(status_code=400, detail="不支持的提示词分类")


@router.post("/templates", response_model=PromptTemplateDetail)
async def create_prompt_template(
    payload: PromptTemplateCreateRequest,
    auth: ApiKeyAuth = Depends(require_admin),
):
    """Create one prompt template through the unified manager."""
    if payload.category == "extraction_schema":
        from backend.rag.ingestion.schema import (
            build_extraction_schema_yaml,
            create_extraction_schema,
        )

        content = payload.content or await asyncio.to_thread(
            build_extraction_schema_yaml,
            payload.template_id,
            label=payload.label,
            subject=payload.subject,
            version=payload.version,
        )
        try:
            schema = await asyncio.to_thread(
                create_extraction_schema,
                payload.template_id,
                content,
            )
            return _schema_detail(schema)
        except FileExistsError:
            raise HTTPException(status_code=409, detail="提示词模板 ID 已存在") from None
        except ValueError as exc:
            raise HTTPException(status_code=400, detail=str(exc)) from exc
    raise HTTPException(status_code=400, detail="不支持的提示词分类")


@router.put("/templates/{category}/{template_id}", response_model=PromptTemplateDetail)
async def update_prompt_template(
    category: PromptCategory,
    template_id: str,
    payload: PromptTemplateUpdateRequest,
    auth: ApiKeyAuth = Depends(require_admin),
):
    """Update one prompt template through the unified manager."""
    if category == "extraction_schema":
        from backend.rag.ingestion.schema import update_extraction_schema

        try:
            schema = await asyncio.to_thread(
                update_extraction_schema,
                template_id,
                payload.content,
            )
            return _schema_detail(schema)
        except FileNotFoundError:
            raise HTTPException(status_code=404, detail="提示词模板不存在") from None
        except ValueError as exc:
            raise HTTPException(status_code=400, detail=str(exc)) from exc
    raise HTTPException(status_code=400, detail="不支持的提示词分类")
