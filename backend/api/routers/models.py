"""模型管理 API（CRUD）"""

import asyncio
import os
from typing import List, Literal, Optional
from fastapi import APIRouter, Depends, HTTPException, Query, Request
from pydantic import BaseModel, ConfigDict, model_validator
from sqlalchemy import select

from backend.api.dependencies.auth import ApiKeyAuth, require_admin
from backend.config.loggers import get_logger
from backend.core.models.model_cache import invalidate_cache, notify_model_change
from backend.db.connection import get_async_session_factory
from backend.db.models.llm_model import LlmModel
from backend.services.admin_audit import write_admin_audit

logger = get_logger(__name__)
router = APIRouter()
_MODEL_CHANGE_NOTIFY_TIMEOUT_SECONDS = max(
    0.1,
    float(os.getenv("MODEL_CHANGE_NOTIFY_TIMEOUT_SECONDS", "5") or "5"),
)


async def _notify_model_change_bounded(
    name: str | None,
    usage_scope: str = "chat",
) -> None:
    invalidate_cache(name, usage_scope)
    try:
        await asyncio.wait_for(
            notify_model_change(name, usage_scope),
            timeout=_MODEL_CHANGE_NOTIFY_TIMEOUT_SECONDS,
        )
    except asyncio.TimeoutError:
        logger.warning(
            "模型缓存变更通知超时: name=%s scope=%s timeout=%.1fs",
            name,
            usage_scope,
            _MODEL_CHANGE_NOTIFY_TIMEOUT_SECONDS,
        )


ModelUsageScope = Literal[
    "chat",
    "image_analysis",
    "ingestion",
    "asr",
    "embedding",
    "rerank",
]


class ModelCreateRequest(BaseModel):
    usage_scope: ModelUsageScope = "chat"
    name: str
    display_name: str
    description: Optional[str] = None
    provider: str = "custom"
    icon_url: Optional[str] = None
    use: str = "langchain_openai:ChatOpenAI"
    model: str
    api_key: str
    base_url: str
    api_key_env_var: Optional[str] = None
    base_url_env_var: Optional[str] = None
    request_timeout: float = 600.0
    max_retries: int = 2
    max_tokens: int = 16384
    max_input_tokens: int = 131072
    temperature: float = 0.1
    top_p: Optional[float] = None
    frequency_penalty: Optional[float] = None
    presence_penalty: Optional[float] = None
    extra_body: Optional[dict] = None
    when_thinking_enabled: Optional[dict] = None
    thinking_config: Optional[dict] = None
    supports_vision: bool = False
    supports_thinking: bool = False
    supports_reasoning_effort: bool = False
    supports_streaming: bool = True
    use_responses_api: Optional[bool] = None
    output_version: Optional[str] = None
    is_active: bool = True
    is_default: bool = False
    sort_order: int = 0

    @model_validator(mode="after")
    def validate_usage_capabilities(self):
        if self.usage_scope == "image_analysis" and not self.supports_vision:
            raise ValueError("图片分析用途模型必须启用视觉能力")
        return self


class ModelResponse(ModelCreateRequest):
    id: int

    model_config = ConfigDict(from_attributes=True)


class ModelUpdateRequest(ModelCreateRequest):
    api_key: Optional[str] = None


def _model_response(model: LlmModel) -> ModelResponse:
    payload = {
        key: getattr(model, key)
        for key in ModelCreateRequest.model_fields
    }
    raw_key = str(model.api_key or "")
    payload["api_key"] = "" if not raw_key else f"{raw_key[:3]}***{raw_key[-3:]}"
    return ModelResponse(id=model.id, **payload)


@router.get("", response_model=List[ModelResponse])
async def list_admin_models(
    usage_scope: ModelUsageScope | None = None,
    limit: int = Query(default=100, ge=1, le=500),
    offset: int = Query(default=0, ge=0),
    admin: ApiKeyAuth = Depends(require_admin),
):
    """获取所有模型列表（管理后台用，可能包含 api_key 等敏感信息，需校验管理员权限，此处简化处理）"""
    session_factory = get_async_session_factory()
    async with session_factory() as session:
        stmt = select(LlmModel)
        if usage_scope is not None:
            stmt = stmt.where(LlmModel.usage_scope == usage_scope)
        stmt = (
            stmt.order_by(LlmModel.usage_scope, LlmModel.sort_order, LlmModel.id)
            .offset(offset)
            .limit(limit)
        )
        result = await session.execute(stmt)
        models = result.scalars().all()
        return [_model_response(model) for model in models]


@router.post("", response_model=ModelResponse)
async def create_model(payload: ModelCreateRequest, request: Request, admin: ApiKeyAuth = Depends(require_admin)):
    session_factory = get_async_session_factory()
    async with session_factory() as session:
        existing = await session.scalar(
            select(LlmModel).where(
                LlmModel.name == payload.name,
                LlmModel.usage_scope == payload.usage_scope,
            )
        )
        if existing:
            raise HTTPException(status_code=409, detail="模型标识已存在")
        # 如果 is_default=True，要先清空其他 is_default
        if payload.is_default:
            await session.execute(
                LlmModel.__table__.update()
                .where(LlmModel.usage_scope == payload.usage_scope)
                .values(is_default=False)
            )
        
        record = LlmModel(**payload.model_dump())
        session.add(record)
        await session.flush()
        await write_admin_audit(
            session, request, admin,
            action="model.create", target_type="model", target_id=str(record.id),
            detail={
                "name": record.name,
                "provider": record.provider,
                "usage_scope": record.usage_scope,
            },
        )
        await session.commit()
        await session.refresh(record)

        # 通知刷新
        await asyncio.gather(
            _notify_model_change_bounded(record.name, record.usage_scope),
            _notify_model_change_bounded(None, record.usage_scope),
        )
        return _model_response(record)


@router.put("/{model_id}", response_model=ModelResponse)
async def update_model(model_id: int, payload: ModelUpdateRequest, request: Request, admin: ApiKeyAuth = Depends(require_admin)):
    session_factory = get_async_session_factory()
    async with session_factory() as session:
        model = await session.get(LlmModel, model_id)
        if not model:
            raise HTTPException(status_code=404, detail="Model not found")
        
        old_name = model.name
        old_scope = model.usage_scope
        duplicate = await session.scalar(
            select(LlmModel).where(
                LlmModel.id != model_id,
                LlmModel.name == payload.name,
                LlmModel.usage_scope == payload.usage_scope,
            )
        )
        if duplicate:
            raise HTTPException(status_code=409, detail="模型标识在该用途域中已存在")

        if payload.is_default:
            await session.execute(
                LlmModel.__table__.update()
                .where(
                    LlmModel.usage_scope == payload.usage_scope,
                    LlmModel.id != model_id,
                )
                .values(is_default=False)
            )
            
        updates = payload.model_dump()
        if not updates.get("api_key") or "***" in str(updates.get("api_key")):
            updates.pop("api_key", None)
        for k, v in updates.items():
            setattr(model, k, v)
        await write_admin_audit(
            session, request, admin,
            action="model.update", target_type="model", target_id=str(model_id),
            detail={key: value for key, value in updates.items() if key != "api_key"},
        )
            
        await session.commit()
        await session.refresh(model)

        await asyncio.gather(
            _notify_model_change_bounded(old_name, old_scope),
            _notify_model_change_bounded(None, old_scope),
            _notify_model_change_bounded(model.name, model.usage_scope),
            _notify_model_change_bounded(None, model.usage_scope),
        )
        return _model_response(model)

@router.delete("/{model_id}")
async def delete_model(model_id: int, request: Request, admin: ApiKeyAuth = Depends(require_admin)):
    session_factory = get_async_session_factory()
    async with session_factory() as session:
        model = await session.get(LlmModel, model_id)
        if not model:
            raise HTTPException(status_code=404, detail="Model not found")
        
        await write_admin_audit(
            session, request, admin,
            action="model.delete", target_type="model", target_id=str(model_id),
            detail={"name": model.name, "usage_scope": model.usage_scope},
        )
        await session.delete(model)
        await session.commit()
        
        await asyncio.gather(
            _notify_model_change_bounded(model.name, model.usage_scope),
            _notify_model_change_bounded(None, model.usage_scope),
        )
        return {"status": "success"}

@router.post("/{model_id}/toggle")
async def toggle_model(model_id: int, is_active: bool, request: Request, admin: ApiKeyAuth = Depends(require_admin)):
    session_factory = get_async_session_factory()
    async with session_factory() as session:
        model = await session.get(LlmModel, model_id)
        if not model:
            raise HTTPException(status_code=404, detail="Model not found")
            
        model.is_active = is_active
        await write_admin_audit(
            session, request, admin,
            action="model.toggle", target_type="model", target_id=str(model_id),
            detail={"is_active": is_active},
        )
        await session.commit()
        await asyncio.gather(
            _notify_model_change_bounded(model.name, model.usage_scope),
            _notify_model_change_bounded(None, model.usage_scope),
        )
        return {"status": "success", "is_active": model.is_active}
