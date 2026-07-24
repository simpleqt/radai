"""模型管理 API（CRUD）"""

from typing import List, Optional
from fastapi import APIRouter, Depends, HTTPException, Query, Request
from pydantic import BaseModel, ConfigDict
from sqlalchemy import select

from backend.api.dependencies.auth import ApiKeyAuth, require_admin
from backend.db.connection import get_async_session_factory
from backend.db.models.llm_model import LlmModel
from backend.core.models.model_cache import notify_model_change
from backend.services.admin_audit import write_admin_audit

router = APIRouter()


class ModelCreateRequest(BaseModel):
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


class ModelResponse(ModelCreateRequest):
    id: int
    usage_scope: str

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
    return ModelResponse(id=model.id, usage_scope=model.usage_scope, **payload)


@router.get("", response_model=List[ModelResponse])
async def list_admin_models(
    usage_scope: str = "chat",
    limit: int = Query(default=100, ge=1, le=500),
    offset: int = Query(default=0, ge=0),
    admin: ApiKeyAuth = Depends(require_admin),
):
    """获取所有模型列表（管理后台用，可能包含 api_key 等敏感信息，需校验管理员权限，此处简化处理）"""
    session_factory = get_async_session_factory()
    async with session_factory() as session:
        stmt = (
            select(LlmModel)
            .where(LlmModel.usage_scope == usage_scope)
            .order_by(LlmModel.sort_order)
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
                LlmModel.usage_scope == "chat",
            )
        )
        if existing:
            raise HTTPException(status_code=409, detail="模型标识已存在")
        # 如果 is_default=True，要先清空其他 is_default
        if payload.is_default:
            await session.execute(
                LlmModel.__table__.update()
                .where(LlmModel.usage_scope == "chat")
                .values(is_default=False)
            )
        
        record = LlmModel(**payload.model_dump(), usage_scope="chat")
        session.add(record)
        await session.flush()
        await write_admin_audit(
            session, request, admin,
            action="model.create", target_type="model", target_id=str(record.id),
            detail={"name": record.name, "provider": record.provider},
        )
        await session.commit()
        await session.refresh(record)

        # 通知刷新
        await notify_model_change(record.name, "chat")
        return _model_response(record)


@router.put("/{model_id}", response_model=ModelResponse)
async def update_model(model_id: int, payload: ModelUpdateRequest, request: Request, admin: ApiKeyAuth = Depends(require_admin)):
    session_factory = get_async_session_factory()
    async with session_factory() as session:
        model = await session.get(LlmModel, model_id)
        if not model:
            raise HTTPException(status_code=404, detail="Model not found")
        
        if payload.is_default and not model.is_default:
            await session.execute(
                LlmModel.__table__.update()
                .where(LlmModel.usage_scope == "chat")
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

        await notify_model_change(model.name, "chat")
        await notify_model_change(None, "chat") # 也清一下 default
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
            detail={"name": model.name},
        )
        await session.delete(model)
        await session.commit()
        
        await notify_model_change(model.name, "chat")
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
        await notify_model_change(model.name, "chat")
        return {"status": "success", "is_active": model.is_active}
