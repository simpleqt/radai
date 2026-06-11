"""模型管理 API（CRUD）"""

from typing import List, Optional
from fastapi import APIRouter, Depends, HTTPException
from pydantic import BaseModel, ConfigDict
from sqlalchemy import select

from backend.api.dependencies.auth import ApiKeyAuth, verify_api_key, require_admin
from backend.db.connection import get_async_session_factory
from backend.db.models.llm_model import LlmModel
from backend.core.models.model_cache import notify_model_change

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


@router.get("", response_model=List[ModelResponse])
async def list_admin_models(usage_scope: str = "chat", admin: ApiKeyAuth = Depends(require_admin)):
    """获取所有模型列表（管理后台用，可能包含 api_key 等敏感信息，需校验管理员权限，此处简化处理）"""
    session_factory = get_async_session_factory()
    async with session_factory() as session:
        stmt = select(LlmModel).where(LlmModel.usage_scope == usage_scope).order_by(LlmModel.sort_order)
        result = await session.execute(stmt)
        models = result.scalars().all()
        # 内部系统管理，不隐去 api_key
        return models


@router.post("", response_model=ModelResponse)
async def create_model(request: ModelCreateRequest, admin: ApiKeyAuth = Depends(require_admin)):
    session_factory = get_async_session_factory()
    async with session_factory() as session:
        # TODO: 验证重名等
        # 如果 is_default=True，要先清空其他 is_default
        if request.is_default:
            await session.execute(
                LlmModel.__table__.update()
                .where(LlmModel.usage_scope == "chat")
                .values(is_default=False)
            )
        
        # 简单写入，AES加解密略
        record = LlmModel(**request.model_dump(), usage_scope="chat")
        session.add(record)
        await session.commit()
        await session.refresh(record)

        # 通知刷新
        await notify_model_change(record.name, "chat")
        return record


@router.put("/{model_id}", response_model=ModelResponse)
async def update_model(model_id: int, request: ModelCreateRequest, admin: ApiKeyAuth = Depends(require_admin)):
    session_factory = get_async_session_factory()
    async with session_factory() as session:
        model = await session.get(LlmModel, model_id)
        if not model:
            raise HTTPException(status_code=404, detail="Model not found")
        
        if request.is_default and not model.is_default:
            await session.execute(
                LlmModel.__table__.update()
                .where(LlmModel.usage_scope == "chat")
                .values(is_default=False)
            )
            
        for k, v in request.model_dump().items():
            setattr(model, k, v)
            
        await session.commit()
        await session.refresh(model)

        await notify_model_change(model.name, "chat")
        await notify_model_change(None, "chat") # 也清一下 default
        return model

@router.delete("/{model_id}")
async def delete_model(model_id: int, admin: ApiKeyAuth = Depends(require_admin)):
    session_factory = get_async_session_factory()
    async with session_factory() as session:
        model = await session.get(LlmModel, model_id)
        if not model:
            raise HTTPException(status_code=404, detail="Model not found")
        
        await session.delete(model)
        await session.commit()
        
        await notify_model_change(model.name, "chat")
        return {"status": "success"}

@router.post("/{model_id}/toggle")
async def toggle_model(model_id: int, is_active: bool, admin: ApiKeyAuth = Depends(require_admin)):
    session_factory = get_async_session_factory()
    async with session_factory() as session:
        model = await session.get(LlmModel, model_id)
        if not model:
            raise HTTPException(status_code=404, detail="Model not found")
            
        model.is_active = is_active
        await session.commit()
        await notify_model_change(model.name, "chat")
        return {"status": "success", "is_active": model.is_active}
